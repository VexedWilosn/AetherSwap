import json

from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine

from app.database import PlatformAction
from app.services.trading.actions import create_platform_action
from app.services.trading.canary import (
    LiveCanarySmokeRegistry,
    live_canary_config_from_app_config,
    raw_context_with_test_signal,
    validate_live_canary_run,
)
from app.services.trading.states import PlatformActionType


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'live_canary.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _canary_config(**overrides):
    base = {
        "enabled": True,
        "kill_switch": False,
        "max_action_cny": 1.0,
        "max_daily_cny": 10.0,
        "allowed_platforms": ["buff"],
        "allowed_action_types": ["purchase_order"],
        "allowed_item_ids": [1],
        "require_recent_smoke_seconds": 900,
        "require_manual_run_once": True,
    }
    base.update(overrides)
    return {"trading_live_canary": base}


def test_live_canary_config_defaults_to_disabled_kill_switch():
    config = live_canary_config_from_app_config({})

    assert config.enabled is False
    assert config.kill_switch is True
    assert config.require_channel == "live_canary"
    assert config.max_action_cny == 1.0
    assert config.max_daily_cny == 10.0
    assert config.require_recent_smoke_seconds == 900
    assert config.require_manual_run_once is True
    assert config.allow_background_worker is False


def test_raw_context_with_test_signal_keeps_fake_profit_out_of_price_fields():
    payload = {
        "target_price": 0.37,
        "locked_budget_cny": 0.37,
        "expected_profit_rate": 0.2,
        "fake_profit_rate": 9.99,
        "canary_profit_boost": 30,
        "raw_context": {"source": "unit"},
    }

    raw = raw_context_with_test_signal(payload)

    assert raw["source"] == "unit"
    assert raw["test_signal"]["fake_profit_rate"] == 9.99
    assert raw["test_signal"]["canary_profit_boost"] == 30
    assert raw.get("target_price") is None
    assert payload["target_price"] == 0.37
    assert payload["locked_budget_cny"] == 0.37


def test_live_canary_run_blocks_when_disabled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=0.5,
            quantity=1,
            channel="live_canary",
            next_check_at=0,
        )

    with SessionLocal() as session:
        decision = validate_live_canary_run(session, {}, limit=1, now=1000)

    assert decision.allowed is False
    assert decision.reason == "live_canary_disabled"


def test_live_canary_run_blocks_without_recent_smoke(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=0.5,
            quantity=1,
            channel="live_canary",
            next_check_at=0,
        )

    with SessionLocal() as session:
        decision = validate_live_canary_run(
            session,
            _canary_config(),
            limit=1,
            smoke_registry=LiveCanarySmokeRegistry(),
            now=1000,
        )

    assert decision.allowed is False
    assert decision.reason == "live_canary_smoke_required"


def test_live_canary_run_allows_only_after_recent_live_smoke(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    registry = LiveCanarySmokeRegistry()
    registry.record_results(
        [
            {
                "platform": "buff",
                "ok": True,
                "live_preflight": True,
                "ready_capabilities": ["purchase_order"],
            }
        ],
        now=995,
    )
    with SessionLocal() as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=0.5,
            quantity=1,
            channel="live_canary",
            next_check_at=0,
        )

    with SessionLocal() as session:
        decision = validate_live_canary_run(
            session,
            _canary_config(),
            limit=1,
            smoke_registry=registry,
            now=1000,
        )

    assert decision.allowed is True


def test_live_canary_run_enforces_limit_one_and_action_cap(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    registry = LiveCanarySmokeRegistry()
    registry.record_results(
        [{"platform": "buff", "ok": True, "live_preflight": True, "ready_capabilities": ["purchase_order"]}],
        now=1000,
    )
    with SessionLocal() as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=1.5,
            quantity=1,
            channel="live_canary",
            next_check_at=0,
        )

    with SessionLocal() as session:
        limit_decision = validate_live_canary_run(session, _canary_config(), limit=2, smoke_registry=registry, now=1000)
        cap_decision = validate_live_canary_run(session, _canary_config(), limit=1, smoke_registry=registry, now=1000)

    assert limit_decision.allowed is False
    assert limit_decision.reason == "live_canary_limit_must_be_one"
    assert cap_decision.allowed is False
    assert cap_decision.reason == "live_canary_action_cap_exceeded"


def test_create_action_with_test_signal_serializes_under_raw_context(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    payload = {
        "raw_context": {"source": "canary_seed"},
        "fake_profit_rate": 4.2,
        "target_price": 0.5,
    }
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=payload["target_price"],
            quantity=1,
            channel="live_canary",
            raw_context=raw_context_with_test_signal(payload),
        )
        raw = json.loads(action.raw_context)

    assert raw["source"] == "canary_seed"
    assert raw["test_signal"]["fake_profit_rate"] == 4.2
    assert action.target_price == 0.5
    assert action.locked_budget_cny == 0.5
