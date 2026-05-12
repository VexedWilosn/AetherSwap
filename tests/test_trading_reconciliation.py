from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine, select

from app.database import PlatformAction, Purchase
from app.services.trading.actions import create_platform_action, transition_action
from app.services.trading.adapters import (
    RESULT_CANCELLED,
    RESULT_ORDER_COMPLETED,
    RESULT_ORDER_PENDING,
    RESULT_TRADE_OFFER_ACCEPTED,
    NormalizedResult,
    PlatformAdapterBase,
)
from app.services.trading.reconciliation import PlatformActionReconciliationService
from app.services.trading.inventory_alignment import InventoryAlignmentService
from app.services.trading.states import PlatformActionState, PlatformActionType


class ReconAdapter(PlatformAdapterBase):
    platform = "eco"

    def __init__(self, result):
        self.result = result
        self.polls = []
        self.offers = []

    def poll_order(self, action):
        self.polls.append(action.id)
        return self.result

    def accept_trade_offer(self, action):
        self.offers.append(action.id)
        return self.result


class DiscoverThenAcceptAdapter(PlatformAdapterBase):
    platform = "buff"

    def __init__(self):
        self.polls = []
        self.offers = []

    def poll_order(self, action):
        self.polls.append(action.id)
        return NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="buff-order-1",
            trade_offer_id="offer-buff-1",
            assetid="asset-buff-1",
        )

    def accept_trade_offer(self, action):
        self.offers.append((action.id, action.trade_offer_id, action.assetid))
        return NormalizedResult(
            True,
            RESULT_TRADE_OFFER_ACCEPTED,
            trade_offer_id=action.trade_offer_id,
            assetid=action.assetid,
        )


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__])
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _waiting_purchase_action(session, *, state=PlatformActionState.WAITING_PLATFORM, platform="eco", offer_id=None):
    action, _ = create_platform_action(
        session,
        action_type=PlatformActionType.PURCHASE_ORDER,
        platform=platform,
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=100,
        quantity=1,
        expected_profit_rate=0.2,
        next_check_at=0,
    )
    transition_action(action, PlatformActionState.PROCESSING, now=1)
    transition_action(
        action,
        state,
        now=2,
        next_check_at=10,
        platform_order_id="eco-order-1",
        trade_offer_id=offer_id,
    )
    session.add(action)
    session.commit()
    return action.id


def test_reconciliation_polls_waiting_order_and_materializes_purchase(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session)

    adapter = ReconAdapter(NormalizedResult(True, RESULT_ORDER_COMPLETED, platform_order_id="eco-order-1"))
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.checked == 1
    assert result.updated == 1
    assert result.succeeded == 1
    assert result.materialized == 1
    assert adapter.polls == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        purchases = session.execute(select(Purchase)).scalars().all()
        assert action.state == PlatformActionState.SUCCEEDED
        assert len(purchases) == 1
        assert purchases[0].source_action_id == action_id


def test_reconciliation_keeps_pending_order_waiting(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session)

    adapter = ReconAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="eco-order-1"))
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.checked == 1
    assert result.waiting == 1
    assert result.materialized == 0
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert session.execute(select(Purchase)).scalars().all() == []


def test_reconciliation_cancelled_order_releases_budget_without_inventory(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session)
        action = session.get(PlatformAction, action_id)
        action.locked_budget_cny = 100
        session.add(action)
        session.commit()

    adapter = ReconAdapter(
        NormalizedResult(
            True,
            RESULT_CANCELLED,
            platform_order_id="eco-order-1",
            filled_quantity=0,
            remaining_quantity=1,
        )
    )
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.cancelled == 1
    assert result.materialized == 0
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.CANCELLED
        assert action.locked_budget_cny == 0
        assert action.released_budget_cny == 100
        assert session.execute(select(Purchase)).scalars().all() == []


def test_reconciliation_accepts_waiting_trade_offer_and_materializes_asset(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(
            session,
            state=PlatformActionState.WAITING_TRADE_OFFER,
            offer_id="offer-1",
        )

    adapter = ReconAdapter(NormalizedResult(True, trade_offer_id="offer-1", assetid="asset-1"))
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.succeeded == 1
    assert result.materialized == 1
    assert adapter.offers == [action_id]
    with SessionLocal() as session:
        purchase = session.execute(select(Purchase)).scalars().one()
        assert purchase.assetid == "asset-1"
        assert purchase.pending_receipt is False


def test_reconciliation_accepts_trade_offer_discovered_from_buff_poll_and_materializes_asset(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session, platform="buff")

    adapter = DiscoverThenAcceptAdapter()
    service = PlatformActionReconciliationService(adapters={"buff": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.succeeded == 1
    assert result.materialized == 1
    assert adapter.polls == [action_id]
    assert adapter.offers == [(action_id, "offer-buff-1", "asset-buff-1")]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        purchase = session.execute(select(Purchase)).scalars().one()
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.trade_offer_id == "offer-buff-1"
        assert action.assetid == "asset-buff-1"
        assert purchase.source_trade_offer_id == "offer-buff-1"
        assert purchase.assetid == "asset-buff-1"
        assert purchase.pending_receipt is False


def test_reconciliation_can_disable_accepting_discovered_trade_offer(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session, platform="buff")

    adapter = DiscoverThenAcceptAdapter()
    service = PlatformActionReconciliationService(adapters={"buff": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11, accept_trade_offers=False)

    assert result.waiting == 1
    assert result.materialized == 0
    assert adapter.polls == [action_id]
    assert adapter.offers == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_TRADE_OFFER
        assert action.trade_offer_id == "offer-buff-1"
        assert action.assetid == "asset-buff-1"
        assert session.execute(select(Purchase)).scalars().all() == []


def test_reconciliation_does_not_reaccept_completed_platform_order_with_offer_id(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session, platform="eco")

    adapter = ReconAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_COMPLETED,
            platform_order_id="eco-order-1",
            trade_offer_id="offer-eco-accepted",
            assetid="asset-eco-1",
        )
    )
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.succeeded == 1
    assert result.materialized == 1
    assert adapter.polls == [action_id]
    assert adapter.offers == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        purchase = session.execute(select(Purchase)).scalars().one()
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.trade_offer_id == "offer-eco-accepted"
        assert action.assetid == "asset-eco-1"
        assert purchase.source_trade_offer_id == "offer-eco-accepted"
        assert purchase.assetid == "asset-eco-1"
        assert purchase.pending_receipt is False


def test_reconciliation_can_recover_failed_action_with_external_buff_order(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session, platform="buff")
        action = session.get(PlatformAction, action_id)
        transition_action(
            action,
            PlatformActionState.FAILED,
            now=5,
            error_code="legacy_poll_missing",
            error_message="adapter capability not implemented: poll_order",
        )
        session.add(action)
        session.commit()

    adapter = DiscoverThenAcceptAdapter()
    service = PlatformActionReconciliationService(adapters={"buff": adapter})
    with SessionLocal() as session:
        skipped = service.run(session, now=11)
    assert skipped.checked == 0

    with SessionLocal() as session:
        result = service.run(session, now=11, recover_failed=True)

    assert result.succeeded == 1
    assert result.materialized == 1
    assert adapter.polls == [action_id]
    assert adapter.offers == [(action_id, "offer-buff-1", "asset-buff-1")]


def test_reconciliation_discovers_c5_trade_offer_before_accepting(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(
            session,
            state=PlatformActionState.WAITING_TRADE_OFFER,
            platform="c5game",
            offer_id=None,
        )

    adapter = ReconAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-order-1",
            trade_offer_id="offer-c5-1",
        )
    )
    service = PlatformActionReconciliationService(adapters={"c5game": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.waiting == 1
    assert result.materialized == 0
    assert adapter.offers == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_TRADE_OFFER
        assert action.trade_offer_id == "offer-c5-1"


def test_reconciliation_dry_run_polls_but_rolls_back_state_and_inventory(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session)

    adapter = ReconAdapter(NormalizedResult(True, RESULT_ORDER_COMPLETED, platform_order_id="eco-order-1"))
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11, dry_run=True)

    assert result.checked == 1
    assert result.updated == 1
    assert result.materialized == 1
    assert adapter.polls == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert session.execute(select(Purchase)).scalars().all() == []


def test_reconciliation_skips_active_processing_lease(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1, lease_until=999, platform_order_id="eco-order-1")
        session.add(action)
        session.commit()

    adapter = ReconAdapter(NormalizedResult(True, RESULT_ORDER_COMPLETED, platform_order_id="eco-order-1"))
    service = PlatformActionReconciliationService(adapters={"eco": adapter})
    with SessionLocal() as session:
        result = service.run(session, now=11)

    assert result.checked == 0
    assert adapter.polls == []


def test_inventory_alignment_binds_pending_purchase_and_action_assetid(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action_id = _waiting_purchase_action(session)
        action = session.get(PlatformAction, action_id)
        transition_action(action, PlatformActionState.SUCCEEDED, now=20, filled_quantity=1, remaining_quantity=0)
        purchase = Purchase(
            name="AK-47 | Redline (Field-Tested)",
            goods_id=1,
            price=100,
            at=21,
            pending_receipt=True,
            source_action_id=action_id,
            source_order_id="eco-order-1",
            source_fill_index=1,
        )
        session.add(action)
        session.add(purchase)
        session.commit()

    service = InventoryAlignmentService()
    with SessionLocal() as session:
        result = service.run(
            session,
            inventory=[
                {"assetid": "asset-redline-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"},
            ],
            now=30,
        )

    assert result.scanned == 1
    assert result.pending == 1
    assert result.matched == 1
    assert result.updated_actions == 1
    with SessionLocal() as session:
        purchase = session.execute(select(Purchase)).scalars().one()
        action = session.get(PlatformAction, action_id)
        assert purchase.assetid == "asset-redline-1"
        assert purchase.pending_receipt is False
        assert action.assetid == "asset-redline-1"


def test_inventory_alignment_dry_run_rolls_back(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        session.add(
            Purchase(
                name="AK-47 | Redline (Field-Tested)",
                goods_id=1,
                price=100,
                at=21,
                pending_receipt=True,
            )
        )
        session.commit()

    service = InventoryAlignmentService()
    with SessionLocal() as session:
        result = service.run(
            session,
            inventory=[
                {"assetid": "asset-redline-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"},
            ],
            dry_run=True,
        )

    assert result.matched == 1
    with SessionLocal() as session:
        purchase = session.execute(select(Purchase)).scalars().one()
        assert purchase.assetid is None
        assert purchase.pending_receipt is True
