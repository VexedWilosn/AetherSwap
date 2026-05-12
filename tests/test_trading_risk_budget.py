from sqlmodel import Session, SQLModel, create_engine

from app.database import PlatformAction
from app.services.trading.actions import create_platform_action
from app.services.trading.risk_budget import RiskBudgetConfig, RiskBudgetService
from app.services.trading.states import PlatformActionType


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'risk.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return Session(engine)


def test_risk_budget_blocks_single_item_budget(tmp_path):
    service = RiskBudgetService()
    with _session(tmp_path) as session:
        decision = service.check_new_action(
            session,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=3100,
            quantity=1,
            expected_profit_rate=0.15,
        )
        assert not decision.allowed
        assert decision.reason == "single_item_budget_exceeded"


def test_risk_budget_counts_active_same_item_and_platform_daily(tmp_path):
    service = RiskBudgetService(RiskBudgetConfig(max_platform_daily_auto_cny=5000))
    with _session(tmp_path) as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="item-a",
            target_price=2500,
            quantity=1,
        )

        same_item = service.check_new_action(
            session,
            platform="buff",
            item_id=1,
            market_hash_name="item-a",
            target_price=600,
            quantity=1,
            expected_profit_rate=0.15,
        )
        assert not same_item.allowed
        assert same_item.reason == "single_item_active_budget_exceeded"

        daily = service.check_new_action(
            session,
            platform="buff",
            item_id=2,
            market_hash_name="item-b",
            target_price=2600,
            quantity=1,
            expected_profit_rate=0.15,
        )
        assert not daily.allowed
        assert daily.reason == "platform_daily_budget_exceeded"


def test_risk_budget_locks_negative_profit_reprice(tmp_path):
    service = RiskBudgetService()
    with _session(tmp_path) as session:
        decision = service.check_new_action(
            session,
            platform="steam",
            item_id=1,
            market_hash_name="item-a",
            target_price=100,
            quantity=1,
            expected_profit_rate=-0.01,
        )
        assert not decision.allowed
        assert decision.reason == "profit_floor_lock"


def test_risk_budget_excludes_current_action_when_worker_checks_it(tmp_path):
    service = RiskBudgetService()
    with _session(tmp_path) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="item-a",
            target_price=2500,
            quantity=1,
            expected_profit_rate=0.15,
        )
        decision = service.check_action(session, action)
        assert decision.allowed is True


def test_platform_daily_budget_counts_filled_amount_after_partial_release(tmp_path):
    service = RiskBudgetService(RiskBudgetConfig(max_platform_daily_auto_cny=5000))
    with _session(tmp_path) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=2,
            market_hash_name="item-b",
            target_price=1000,
            quantity=5,
            expected_profit_rate=0.15,
        )
        action.locked_budget_cny = 3000
        action.filled_amount_cny = 2000
        session.add(action)
        session.commit()

        decision = service.check_new_action(
            session,
            platform="buff",
            item_id=3,
            market_hash_name="item-c",
            target_price=1,
            quantity=1,
            expected_profit_rate=0.15,
        )

        assert not decision.allowed
        assert decision.reason == "platform_daily_budget_exceeded"
        assert decision.current_platform_daily_cny == 5000


def test_risk_budget_groups_exposure_by_normalized_item_category(tmp_path):
    service = RiskBudgetService(RiskBudgetConfig(max_single_category_budget_cny=5000))
    with _session(tmp_path) as session:
        create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=10,
            market_hash_name="StatTrak\u2122 AK-47 | Redline (Field-Tested)",
            target_price=2500,
            quantity=1,
            expected_profit_rate=0.15,
        )

        decision = service.check_new_action(
            session,
            platform="uuyp",
            item_id=11,
            market_hash_name="AK-47 | Redline (Minimal Wear)",
            target_price=2600,
            quantity=1,
            expected_profit_rate=0.15,
        )

        assert not decision.allowed
        assert decision.reason == "single_category_budget_exceeded"
        assert decision.current_category_budget_cny == 2500


def test_risk_budget_includes_legacy_rows_without_risk_category(tmp_path):
    service = RiskBudgetService(RiskBudgetConfig(max_single_category_budget_cny=5000))
    with _session(tmp_path) as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=12,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=2500,
            quantity=1,
            expected_profit_rate=0.15,
        )
        action.risk_category = ""
        session.add(action)
        session.commit()

        decision = service.check_new_action(
            session,
            platform="uuyp",
            item_id=13,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=2600,
            quantity=1,
            expected_profit_rate=0.15,
        )

        assert not decision.allowed
        assert decision.reason == "single_category_budget_exceeded"
        assert decision.current_category_budget_cny == 2500
