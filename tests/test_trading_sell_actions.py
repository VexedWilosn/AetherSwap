from sqlmodel import Session, SQLModel, create_engine

from app.database import PlatformAction
from app.services.trading.sell_actions import SellerActionService
from app.services.trading.states import PlatformActionState, PlatformActionType


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sell_actions.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return Session(engine)


def test_seller_action_service_creates_platform_listing_without_budget(tmp_path):
    service = SellerActionService()
    with _session(tmp_path) as session:
        result = service.create_action(
            session,
            {
                "action_type": PlatformActionType.PLATFORM_LISTING,
                "platform": "eco",
                "item_id": 1,
                "market_hash_name": "AK-47 | Redline (Field-Tested)",
                "target_price": 88.8,
                "assetid": "asset-1",
                "steam_id": "7656",
                "expected_profit_rate": 0.12,
            },
        )

        assert result.created is True
        assert result.risk.allowed is True
        assert result.action.state == PlatformActionState.QUEUED
        assert result.action.locked_budget_cny == 0
        assert result.action.assetid == "asset-1"
        assert '"steam_id": "7656"' in (result.action.request_payload or "")


def test_seller_action_service_profit_locks_negative_reprice(tmp_path):
    service = SellerActionService()
    with _session(tmp_path) as session:
        result = service.create_action(
            session,
            {
                "action_type": PlatformActionType.REPRICE_LISTING,
                "platform": "buff",
                "item_id": 2,
                "market_hash_name": "M4A1-S | Printstream (Field-Tested)",
                "target_price": 77.7,
                "platform_listing_id": "sell-order-1",
                "expected_profit_rate": -0.01,
            },
        )

        assert result.created is True
        assert result.risk.allowed is False
        assert result.action.state == PlatformActionState.RISK_BLOCKED
        assert result.action.error_code == "profit_floor_lock"
        assert result.action.locked_budget_cny == 0


def test_seller_action_service_is_idempotent_by_platform_object(tmp_path):
    service = SellerActionService()
    payload = {
        "action_type": PlatformActionType.DELIVER_ORDER,
        "platform": "c5",
        "item_id": 3,
        "market_hash_name": "AWP | Asiimov (Field-Tested)",
        "platform_order_id": "c5-order-1",
    }
    with _session(tmp_path) as session:
        first = service.create_action(session, payload)
        second = service.create_action(session, dict(payload))

        assert first.created is True
        assert second.created is False
        assert first.action.id == second.action.id
        assert second.action.platform == "c5game"
        assert second.action.platform_order_id == "c5-order-1"


def test_seller_action_planner_builds_inventory_and_delivery_actions(tmp_path):
    service = SellerActionService()
    payload = {
        "listing_platform": "eco",
        "steam_id": "7656",
        "channel": "snapshot",
        "active_assetids": ["asset-listed"],
        "inventory": [
            {
                "item_id": 10,
                "market_hash_name": "AK-47 | Redline (Field-Tested)",
                "assetid": "asset-1",
                "can_sell": True,
                "target_price": 88.8,
            },
            {
                "item_id": 11,
                "market_hash_name": "M4A1-S | Printstream (Field-Tested)",
                "assetid": "asset-listed",
                "can_sell": True,
                "target_price": 77.7,
            },
            {
                "item_id": 12,
                "market_hash_name": "AWP | Asiimov (Field-Tested)",
                "assetid": "asset-cooldown",
                "can_sell": False,
                "target_price": 66.6,
            },
        ],
        "orders": [
            {
                "item_id": 13,
                "market_hash_name": "USP-S | Kill Confirmed (Field-Tested)",
                "orderId": "c5-order-1",
                "status": 1,
            },
            {
                "item_id": 14,
                "market_hash_name": "Desert Eagle | Blaze (Factory New)",
                "orderId": "c5-order-2",
                "status": 2,
                "orderConfirmInfoDTO": {"offerId": "offer-2"},
            },
        ],
    }

    plan = service.plan_from_snapshot(payload)

    assert [row["action_type"] for row in plan.actions] == [
        PlatformActionType.PLATFORM_LISTING,
        PlatformActionType.DELIVER_ORDER,
        PlatformActionType.ACCEPT_TRADE_OFFER,
    ]
    assert plan.actions[0]["platform"] == "eco"
    assert plan.actions[0]["request_payload"]["steam_id"] == "7656"
    assert plan.actions[1]["platform_order_id"] == "c5-order-1"
    assert plan.actions[2]["trade_offer_id"] == "offer-2"
    assert {row["reason"] for row in plan.skipped} == {"already_listed", "not_sellable"}


def test_seller_action_planner_can_commit_snapshot(tmp_path):
    service = SellerActionService()
    with _session(tmp_path) as session:
        result = service.plan_and_create(
            session,
            {
                "listing_platform": "steam",
                "inventory": [
                    {
                        "item_id": 20,
                        "market_hash_name": "AK-47 | Slate (Field-Tested)",
                        "assetid": "asset-20",
                        "can_sell": True,
                        "target_price": 99.9,
                    }
                ],
            },
        )

        assert len(result.plan.actions) == 1
        assert len(result.created) == 1
        action = result.created[0].action
        assert action.action_type == PlatformActionType.STEAM_LISTING
        assert action.platform == "steam"
        assert action.assetid == "asset-20"
        assert action.locked_budget_cny == 0
