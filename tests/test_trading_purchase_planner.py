from app.services.trading.purchase_planner import PlatformMarketQuote, build_purchase_plan, build_quotes_from_config
from app.services.trading.purchase_targets import create_purchase_target_actions
from app.database import PlatformAction
from app.services.trading.states import PlatformActionType
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine


def test_purchase_plan_uses_direct_orders_before_buy_orders():
    plan = build_purchase_plan(
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_quantity=10,
        max_unit_price=100,
        default_order_price=95,
        quotes=[
            PlatformMarketQuote("buff", sell_orders=((110, 10),), buy_max=101, target_order_price=100, weight=1),
            PlatformMarketQuote("uuyp", sell_orders=((102, 10),), buy_max=98, target_order_price=95, weight=2),
            PlatformMarketQuote("eco", sell_orders=((98, 4),), buy_max=90, target_order_price=95, weight=3),
        ],
    )

    assert plan.direct_quantity == 4
    assert plan.order_quantity == 6
    assert [(a.action_type, a.platform, a.price, a.quantity) for a in plan.actions] == [
        ("direct_buy", "eco", 98, 4),
        ("purchase_order", "eco", 95, 3),
        ("purchase_order", "uuyp", 95, 2),
        ("purchase_order", "buff", 100, 1),
    ]


def test_purchase_plan_does_not_create_buy_orders_when_direct_fills_target():
    plan = build_purchase_plan(
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_quantity=3,
        max_unit_price=100,
        quotes=[
            {"platform": "eco", "sell_orders": [{"price": 97, "quantity": 2}], "target_order_price": 95},
            {"platform": "uuyp", "sell_orders": [{"price": 98, "quantity": 2}], "target_order_price": 95},
        ],
    )

    assert plan.direct_quantity == 3
    assert plan.order_quantity == 0
    assert [a.action_type for a in plan.actions] == ["direct_buy", "direct_buy"]
    assert sum(a.quantity for a in plan.actions) == 3


def test_build_quotes_from_config_ignores_uuyp_direct_even_if_config_enables_it():
    quotes = build_quotes_from_config(
        {
            "uuyp": {"sell_min": 0.02, "sell_volume": 5, "target_order_price": 0.02},
            "buff": {"sell_min": 0.03, "sell_volume": 2, "target_order_price": 0.02},
        },
        {
            "cash_platform_trading": {
                "platforms": {
                    "uuyp": {"allow_direct_buy": True, "allow_purchase_order": True},
                    "buff": {"allow_direct_buy": True, "allow_purchase_order": True},
                }
            }
        },
    )

    by_platform = {quote.platform: quote for quote in quotes}
    assert by_platform["uuyp"].sell_orders == ()
    assert by_platform["uuyp"].target_order_price == 0.02
    assert by_platform["buff"].sell_orders == ((0.03, 2),)


def test_purchase_plan_tracks_unfilled_remaining_when_no_order_platform_available():
    plan = build_purchase_plan(
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_quantity=5,
        max_unit_price=100,
        quotes=[PlatformMarketQuote("buff", sell_orders=((99, 2),))],
    )

    assert plan.direct_quantity == 2
    assert plan.order_quantity == 0
    assert plan.remaining_quantity == 3


def test_create_purchase_target_actions_records_cost_batch_and_plan(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'purchase_target.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with SessionLocal() as session:
        result = create_purchase_target_actions(
            session,
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_quantity=3,
            max_unit_price=100,
            target_id="target-42",
            quotes=[
                {"platform": "eco", "sell_orders": [{"price": 98, "quantity": 1}], "target_order_price": 95, "weight": 2},
                {"platform": "buff", "target_order_price": 100, "weight": 1},
            ],
            request_payload={"platform_ids": {"buff": 123}},
        )

    assert result.created_count == 3
    assert [a.action_type for a in result.actions] == [
        PlatformActionType.DIRECT_BUY,
        PlatformActionType.PURCHASE_ORDER,
        PlatformActionType.PURCHASE_ORDER,
    ]
    assert result.actions[0].raw_context and "target-42" in result.actions[0].raw_context
    assert result.actions[-1].request_payload and '"platform_item_id": 123' in result.actions[-1].request_payload
