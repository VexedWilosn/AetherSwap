from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine

from app.database import PlatformAction, Purchase
from app.services.trading.actions import create_platform_action
from app.services.trading.exposure_guard import (
    LOW_PRICE_EXPOSURE_REASON,
    LowPriceExposureGuard,
    parse_low_price_exposure_rule,
)
from app.services.trading.states import PlatformActionState, PlatformActionType


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exposure_guard.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__])
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def test_parse_low_price_exposure_rule_builds_price_bands():
    intervals = parse_low_price_exposure_rule("0-0-0.02-2-0.05")

    assert [(i.min_price, i.max_price, i.max_quantity) for i in intervals] == [
        (0, 0.02, 0),
        (0.02, 0.05, 2),
    ]


@pytest.mark.parametrize("rule", ["0-1", "0-1-0.01-2", "0-a-0.01", "0.02-1-0.01"])
def test_parse_low_price_exposure_rule_rejects_invalid(rule):
    with pytest.raises(ValueError):
        parse_low_price_exposure_rule(rule)


def test_guard_counts_purchases_pending_receipt_and_active_orders(tmp_path, monkeypatch):
    SessionLocal = _session(tmp_path)
    monkeypatch.setattr(
        "app.state.get_inventory",
        lambda: [{"item_id": 10, "market_hash_name": "Cheap Skin", "quantity": 1}],
    )

    with SessionLocal() as session:
        session.add(Purchase(goods_id=10, name="Cheap Skin", price=0.02, pending_receipt=False))
        session.add(Purchase(goods_id=10, name="Cheap Skin", price=0.02, pending_receipt=True))
        session.add(Purchase(goods_id=10, name="Cheap Skin", price=0.02, sold_at=123))
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=10,
            market_hash_name="Cheap Skin",
            target_price=0.03,
            quantity=2,
        )
        guard = LowPriceExposureGuard(
            {"low_price_exposure_guard": {"rule": "0-10-0.05", "include_inventory": True}}
        )

        decision = guard.check(
            session,
            item_id=10,
            market_hash_name="Cheap Skin",
            unit_price=0.03,
            proposed_quantity=5,
            fail_closed=True,
        )

    assert decision.allowed is True
    assert decision.current_quantity == 4
    assert decision.breakdown.purchases == 1
    assert decision.breakdown.pending_receipt == 1
    assert decision.breakdown.active_orders == 2
    assert decision.breakdown.inventory == 1
    assert decision.breakdown.held_total == 1
    assert decision.breakdown.total == 4


def test_guard_dedupes_purchase_and_inventory_for_same_item(tmp_path, monkeypatch):
    SessionLocal = _session(tmp_path)
    monkeypatch.setattr(
        "app.state.get_inventory",
        lambda: [{"item_id": 10, "market_hash_name": "Cheap Skin", "quantity": 1}],
    )

    with SessionLocal() as session:
        session.add(Purchase(goods_id=10, name="Cheap Skin", price=0.02, pending_receipt=False))
        session.commit()
        guard = LowPriceExposureGuard(
            {"low_price_exposure_guard": {"rule": "0-2-0.05", "include_inventory": True}}
        )

        decision = guard.check(
            session,
            item_id=10,
            market_hash_name="Cheap Skin",
            unit_price=0.03,
            proposed_quantity=1,
            fail_closed=True,
        )

    assert decision.allowed is True
    assert decision.current_quantity == 1
    assert decision.projected_quantity == 2
    assert decision.breakdown.purchases == 1
    assert decision.breakdown.inventory == 1
    assert decision.breakdown.total == 1


def test_guard_blocks_execution_when_projected_quantity_exceeds_band(tmp_path, monkeypatch):
    SessionLocal = _session(tmp_path)
    monkeypatch.setattr("app.state.get_inventory", lambda: [])

    with SessionLocal() as session:
        session.add(Purchase(goods_id=10, name="Cheap Skin", price=0.02))
        session.commit()
        guard = LowPriceExposureGuard({"low_price_exposure_guard": {"rule": "0-1-0.05"}})

        decision = guard.check(
            session,
            item_id=10,
            market_hash_name="Cheap Skin",
            unit_price=0.03,
            proposed_quantity=1,
            fail_closed=True,
        )

    assert decision.allowed is False
    assert decision.reason == LOW_PRICE_EXPOSURE_REASON
    assert decision.current_quantity == 1
    assert decision.max_quantity == 1


def test_guard_does_not_count_terminal_actions(tmp_path, monkeypatch):
    SessionLocal = _session(tmp_path)
    monkeypatch.setattr("app.state.get_inventory", lambda: [])

    with SessionLocal() as session:
        active, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=10,
            market_hash_name="Cheap Skin",
            target_price=0.03,
            quantity=1,
        )
        terminal, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=10,
            market_hash_name="Cheap Skin",
            target_price=0.03,
            quantity=1,
            idempotency_key="terminal",
        )
        terminal.state = PlatformActionState.FAILED
        session.add(active)
        session.add(terminal)
        session.commit()

        guard = LowPriceExposureGuard({"low_price_exposure_guard": {"rule": "0-1-0.05"}})
        decision = guard.should_hide_signal(
            session,
            item_id=10,
            market_hash_name="Cheap Skin",
            unit_price=0.03,
        )

    assert decision.allowed is False
    assert decision.current_quantity == 1
