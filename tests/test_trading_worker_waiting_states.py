import json

import pytest
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine
from sqlmodel import select

from app.database import PlatformAction, Purchase
from app.services.trading.actions import create_platform_action, transition_action
from app.services.trading.adapters import (
    RESULT_CANCELLED,
    RESULT_AUTH_REQUIRED,
    RESULT_LISTING_SUBMITTED,
    RESULT_MOBILE_CONFIRM_REQUIRED,
    RESULT_NOT_FOUND,
    RESULT_OFFER_DECLINED,
    RESULT_OFFER_EXPIRED,
    RESULT_ORDER_COMPLETED,
    RESULT_ORDER_PENDING,
    RESULT_REPRICE_SUBMITTED,
    RESULT_TRANSIENT,
    NormalizedResult,
    PlatformAdapterBase,
)
from app.services.trading.states import PlatformActionState, PlatformActionType
from app.services.trading.worker import PlatformActionWorker


class PollingAdapter(PlatformAdapterBase):
    platform = "eco"

    def __init__(self, result):
        self.result = result
        self.submits = []
        self.polls = []
        self.offers = []
        self.cancels = []
        self.listings = []
        self.reprices = []

    def create_purchase_order(self, action):
        self.submits.append(action.id)
        return NormalizedResult(True, platform_order_id="submitted-order")

    def create_direct_buy(self, action):
        self.submits.append(action.id)
        return self.result

    def poll_order(self, action):
        self.polls.append(action.id)
        return self.result

    def accept_trade_offer(self, action):
        self.offers.append(action.id)
        return self.result

    def cancel_order(self, action):
        self.cancels.append(action.id)
        return self.result

    def create_listing(self, action):
        self.listings.append(action.id)
        return self.result

    def change_price(self, action):
        self.reprices.append(action.id)
        return self.result

    def deliver_order(self, action):
        self.submits.append(action.id)
        return self.result


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'waiting.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__])
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def test_worker_polls_waiting_platform_instead_of_resubmitting_order(tmp_path):
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
        transition_action(
            action,
            PlatformActionState.PROCESSING,
            now=1,
        )
        transition_action(
            action,
            PlatformActionState.WAITING_PLATFORM,
            now=2,
            next_check_at=10,
            platform_order_id="eco-order-1",
        )
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_COMPLETED, platform_order_id="eco-order-1"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert adapter.polls == [action_id]
    assert adapter.submits == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.platform_order_id == "eco-order-1"
        purchases = session.execute(select(Purchase)).scalars().all()
        assert len(purchases) == 1
        assert purchases[0].name == "AK-47 | Redline (Field-Tested)"
        assert purchases[0].price == 100
        assert purchases[0].source_platform == "eco"
        assert purchases[0].source_action_id == action_id
        assert purchases[0].source_order_id == "eco-order-1"
        assert purchases[0].source_fill_index == 1
        assert purchases[0].pending_receipt is True

    rerun = worker.run_once(now=12, limit=5)
    assert rerun.claimed == 0
    with SessionLocal() as session:
        assert len(session.execute(select(Purchase)).scalars().all()) == 1


def test_worker_keeps_pending_order_in_waiting_platform(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=2,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=10, platform_order_id="eco-order-2")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="eco-order-2"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert action.next_check_at == 71


def test_worker_releases_budget_for_partial_fill_while_order_stays_waiting(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=22,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=5,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=10, platform_order_id="eco-order-partial")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-order-partial",
            filled_quantity=2,
            remaining_quantity=3,
            filled_amount_cny=200,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert action.filled_quantity == 2
        assert action.remaining_quantity == 3
        assert action.filled_amount_cny == 200
        assert action.locked_budget_cny == 300
        assert action.released_budget_cny == 200


def test_worker_marks_pending_result_completed_when_fill_progress_is_complete(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=23,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=2,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=10, platform_order_id="eco-order-filled")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-order-filled",
            filled_quantity=2,
            remaining_quantity=0,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.succeeded == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.filled_quantity == 2
        assert action.remaining_quantity == 0
        assert action.locked_budget_cny == 0
        assert action.released_budget_cny == 200
        purchases = session.execute(select(Purchase).order_by(Purchase.source_fill_index)).scalars().all()
        assert len(purchases) == 2
        assert [row.price for row in purchases] == [100, 100]
        assert [row.source_fill_index for row in purchases] == [1, 2]


def test_worker_releases_budget_when_buy_fails_before_remote_order(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=26,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=2,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(False, RESULT_NOT_FOUND, "listing disappeared"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.failed == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.FAILED
        assert action.locked_budget_cny == 0
        assert action.released_budget_cny == 200
        assert action.remaining_quantity == 2


def test_worker_keeps_budget_when_failed_buy_has_remote_order(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=27,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(False, RESULT_NOT_FOUND, "remote uncertainty", platform_order_id="eco-order-uncertain"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.failed == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.FAILED
        assert action.platform_order_id == "eco-order-uncertain"
        assert action.locked_budget_cny == 100
        assert action.released_budget_cny == 0


def test_worker_cancels_unsubmitted_target_orders_after_target_filled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        direct, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=24,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-1", "target_quantity": 1},
        )
        order, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=24,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=999,
            raw_context={"purchase_target_id": "target-1", "target_quantity": 1},
        )
        direct_id = direct.id
        order_id = order.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-direct-1",
            filled_quantity=1,
            remaining_quantity=0,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=1)

    assert result.succeeded == 1
    with SessionLocal() as session:
        direct = session.get(PlatformAction, direct_id)
        order = session.get(PlatformAction, order_id)
        assert direct.state == PlatformActionState.SUCCEEDED
        assert order.state == PlatformActionState.CANCELLED
        assert order.error_code == "purchase_target_filled"
        assert order.locked_budget_cny == 0


def test_worker_does_not_submit_order_cancelled_by_earlier_batch_action(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        direct, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=242,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-same-batch", "target_quantity": 1},
        )
        order, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=242,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-same-batch", "target_quantity": 1},
        )
        direct_id = direct.id
        order_id = order.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-direct-filled",
            filled_quantity=1,
            remaining_quantity=0,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 2
    assert result.succeeded == 1
    assert adapter.submits == [direct_id]
    with SessionLocal() as session:
        direct = session.get(PlatformAction, direct_id)
        order = session.get(PlatformAction, order_id)
        assert direct.state == PlatformActionState.SUCCEEDED
        assert order.state == PlatformActionState.CANCELLED
        assert order.error_code == "purchase_target_filled"
        assert order.platform_order_id is None


def test_worker_defers_purchase_order_until_direct_buy_settles(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        direct, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=240,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=999,
            raw_context={"purchase_target_id": "target-wait-direct", "target_quantity": 1},
        )
        transition_action(
            direct,
            PlatformActionState.PROCESSING,
            now=1,
        )
        transition_action(
            direct,
            PlatformActionState.WAITING_PLATFORM,
            now=2,
            next_check_at=999,
            platform_order_id="eco-direct-open",
        )
        order, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=240,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-wait-direct", "target_quantity": 1},
        )
        session.add(direct)
        session.commit()
        order_id = order.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="eco-order-queued"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    assert adapter.submits == []
    with SessionLocal() as session:
        order = session.get(PlatformAction, order_id)
        assert order.state == PlatformActionState.RETRY_WAIT
        assert order.error_code == "waiting_direct_buy"
        assert order.next_check_at == 41


def test_worker_purchase_order_proceeds_after_direct_buy_is_terminal(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        direct, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=241,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=999,
            raw_context={"purchase_target_id": "target-direct-done", "target_quantity": 1},
        )
        transition_action(direct, PlatformActionState.FAILED, now=2, error_code="direct_failed", error_message="direct failed")
        order, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=241,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-direct-done", "target_quantity": 1},
        )
        session.add(direct)
        session.commit()
        order_id = order.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="eco-order-after-direct"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    assert adapter.submits == [order_id]
    with SessionLocal() as session:
        order = session.get(PlatformAction, order_id)
        assert order.state == PlatformActionState.WAITING_PLATFORM
        assert order.platform_order_id == "submitted-order"
        assert order.error_code == ""


def test_worker_queues_remote_cancel_for_submitted_target_order_when_target_filled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        direct, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="eco",
            item_id=25,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            raw_context={"purchase_target_id": "target-2", "target_quantity": 1},
        )
        order, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=25,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=999,
            raw_context={"purchase_target_id": "target-2", "target_quantity": 1},
        )
        transition_action(order, PlatformActionState.PROCESSING, now=1)
        transition_action(order, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=999, platform_order_id="eco-order-open")
        session.add(order)
        session.commit()
        direct_id = direct.id
        order_id = order.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_ORDER_PENDING,
            platform_order_id="eco-direct-2",
            filled_quantity=1,
            remaining_quantity=0,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=1)

    assert result.succeeded == 1
    with SessionLocal() as session:
        direct = session.get(PlatformAction, direct_id)
        order = session.get(PlatformAction, order_id)
        assert direct.state == PlatformActionState.SUCCEEDED
        assert order.state == PlatformActionState.WAITING_PLATFORM
        assert order.error_code == "purchase_target_remote_cancel_requested"
        assert order.platform_order_id == "eco-order-open"
        cancel_actions = session.execute(
            select(PlatformAction).where(PlatformAction.action_type == PlatformActionType.CANCEL_ORDER)
        ).scalars().all()
        assert len(cancel_actions) == 1
        cancel_action = cancel_actions[0]
        assert cancel_action.platform == "eco"
        assert cancel_action.state == PlatformActionState.QUEUED
        assert cancel_action.platform_order_id is None
        assert json.loads(cancel_action.request_payload)["order_id"] == "eco-order-open"
        assert json.loads(cancel_action.raw_context)["target_action_id"] == order_id


def test_worker_settles_parent_after_purchase_target_cancel_action_succeeds(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        parent, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=26,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=95,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=999,
            raw_context={"purchase_target_id": "target-cancel-settle", "target_quantity": 1},
        )
        transition_action(parent, PlatformActionState.PROCESSING, now=1)
        transition_action(parent, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=999, platform_order_id="eco-order-open")
        session.add(parent)
        session.flush()
        cancel, _ = create_platform_action(
            session,
            action_type=PlatformActionType.CANCEL_ORDER,
            platform="eco",
            item_id=26,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=0,
            quantity=1,
            expected_profit_rate=0.0,
            locked_budget_cny=0,
            next_check_at=0,
            request_payload={"target_action_id": parent.id, "order_id": "eco-order-open"},
            raw_context={
                "purchase_target_id": "target-cancel-settle",
                "target_action_id": parent.id,
                "reason": "purchase_target_filled",
            },
        )
        session.add(parent)
        session.commit()
        parent_id = parent.id
        cancel_id = cancel.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_CANCELLED, "cancelled", platform_order_id="eco-order-open"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert adapter.cancels == [cancel_id]
    with SessionLocal() as session:
        parent = session.get(PlatformAction, parent_id)
        cancel = session.get(PlatformAction, cancel_id)
        assert cancel.state == PlatformActionState.CANCELLED
        assert parent.state == PlatformActionState.CANCELLED
        assert parent.locked_budget_cny == 0
        assert parent.released_budget_cny == 95
        assert parent.error_code == "remote_cancelled_by_action"


def test_worker_moves_pending_order_with_offer_to_waiting_trade_offer(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DELIVER_ORDER,
            platform="eco",
            item_id=21,
            market_hash_name="M4A1-S | Printstream (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        action.platform_order_id = "delivery-order-21"
        action_id = action.id

    adapter = PollingAdapter(
        NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="delivery-order-21", trade_offer_id="offer-21")
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_TRADE_OFFER
        assert action.next_check_at == 41
        assert action.trade_offer_id == "offer-21"


def test_waiting_platform_retriable_error_restores_waiting_state(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=3,
            market_hash_name="USP-S | Kill Confirmed (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=10, platform_order_id="eco-order-3")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(False, RESULT_TRANSIENT, "timeout"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.failed == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert action.retry_count == 1
        assert action.next_check_at == 41


def test_worker_accepts_waiting_trade_offer(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=4,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_TRADE_OFFER, now=2, next_check_at=10, trade_offer_id="offer-1")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, trade_offer_id="offer-1", assetid="asset-1"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert adapter.offers == [action_id]
    assert adapter.submits == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.trade_offer_id == "offer-1"
        assert action.assetid == "asset-1"
        purchases = session.execute(select(Purchase)).scalars().all()
        assert len(purchases) == 1
        assert purchases[0].assetid == "asset-1"
        assert purchases[0].pending_receipt is False
        assert purchases[0].source_trade_offer_id == "offer-1"


def test_worker_discovers_trade_offer_id_before_accepting_when_only_order_id_exists(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="c5game",
            item_id=4,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_TRADE_OFFER, now=2, next_check_at=10, platform_order_id="c5-order-1")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="c5-order-1", trade_offer_id="offer-c5-1"))
    worker = PlatformActionWorker(SessionLocal, adapters={"c5game": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.waiting == 1
    assert adapter.offers == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_TRADE_OFFER
        assert action.trade_offer_id == "offer-c5-1"
        assert session.execute(select(Purchase)).scalars().all() == []


def test_worker_does_not_resubmit_recovered_processing_action_without_external_id(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=44,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1, lease_until=2)
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, platform_order_id="eco-order-new"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.risk_blocked == 1
    assert adapter.submits == []
    assert adapter.polls == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.RISK_BLOCKED
        assert action.error_code == "manual_reconcile_required"


def test_worker_blocks_unsafe_trade_offer_before_adapter_accept(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=41,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={
                "offer": {
                    "tradeofferid": "offer-unsafe",
                    "items_to_give": [{"assetid": "our-asset"}],
                    "items_to_receive": [{"assetid": "their-asset", "market_hash_name": "AWP | Asiimov (Field-Tested)"}],
                }
            },
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_TRADE_OFFER, now=2, next_check_at=10, trade_offer_id="offer-unsafe")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, trade_offer_id="offer-unsafe", assetid="asset-unsafe"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.failed == 1
    assert adapter.offers == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.FAILED
        assert action.error_code == "unsafe_offer"
        assert action.error_message == "offer_requires_our_items"
        assert session.execute(select(Purchase)).scalars().all() == []


@pytest.mark.parametrize(
    ("category", "reason", "expected_state", "expected_error_code"),
    [
        (RESULT_OFFER_EXPIRED, "offer_expired", PlatformActionState.FAILED, "offer_expired"),
        (RESULT_OFFER_DECLINED, "offer_declined", PlatformActionState.FAILED, "offer_declined"),
        (RESULT_MOBILE_CONFIRM_REQUIRED, "mobile_confirm_required", PlatformActionState.FAILED, "mobile_confirm_required"),
        (RESULT_AUTH_REQUIRED, "steam_auth_required", PlatformActionState.WAITING_TRADE_OFFER, "steam_auth_required"),
    ],
)
def test_worker_records_structured_trade_offer_failure_codes(
    tmp_path,
    category,
    reason,
    expected_state,
    expected_error_code,
):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=41,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_TRADE_OFFER, now=2, next_check_at=10, trade_offer_id="offer-structured")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(
        NormalizedResult(
            False,
            category,
            reason,
            trade_offer_id="offer-structured",
            response_payload={"success": False, "reason": reason, "category": category},
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.failed == 1
    assert adapter.offers == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == expected_state
        assert action.error_code == expected_error_code
        assert action.error_message == reason
        assert action.response_payload is not None


def test_worker_marks_cancel_action_cancelled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.CANCEL_ORDER,
            platform="eco",
            item_id=5,
            market_hash_name="AK-47 | Slate (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"order_id": "eco-order-5"},
        )
        action.platform_order_id = "eco-order-5"
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_CANCELLED, "cancelled", platform_order_id="eco-order-5"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert adapter.cancels == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.CANCELLED
        assert action.finished_at == 11
        assert action.platform_order_id == "eco-order-5"
        assert session.execute(select(Purchase)).scalars().all() == []


def test_worker_materializes_partial_fill_when_order_is_cancelled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="eco",
            item_id=55,
            market_hash_name="AK-47 | Slate (Field-Tested)",
            target_price=20,
            locked_budget_cny=100,
            quantity=5,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        transition_action(action, PlatformActionState.PROCESSING, now=1)
        transition_action(action, PlatformActionState.WAITING_PLATFORM, now=2, next_check_at=10, platform_order_id="eco-order-cancel-fill")
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(
        NormalizedResult(
            True,
            RESULT_CANCELLED,
            "cancelled after partial fill",
            platform_order_id="eco-order-cancel-fill",
            filled_quantity=2,
            remaining_quantity=3,
            filled_amount_cny=40,
            remaining_amount_cny=0,
        )
    )
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.CANCELLED
        assert action.filled_quantity == 2
        purchases = session.execute(select(Purchase).order_by(Purchase.source_fill_index)).scalars().all()
        assert len(purchases) == 2
        assert [row.price for row in purchases] == [20, 20]
        assert [row.source_fill_index for row in purchases] == [1, 2]


def test_worker_does_not_budget_block_cancel_action(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.CANCEL_ORDER,
            platform="eco",
            item_id=6,
            market_hash_name="AK-47 | Slate (Field-Tested)",
            target_price=9000,
            locked_budget_cny=9000,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"order_id": "eco-order-6"},
        )
        action.platform_order_id = "eco-order-6"
        session.add(action)
        session.commit()
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_CANCELLED, "cancelled", platform_order_id="eco-order-6"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.claimed == 1
    assert result.risk_blocked == 0
    assert result.succeeded == 1
    assert adapter.cancels == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.CANCELLED


def test_worker_marks_listing_and_reprice_submitted_as_succeeded(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        listing, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PLATFORM_LISTING,
            platform="eco",
            item_id=7,
            market_hash_name="AK-47 | Slate (Field-Tested)",
            target_price=9000,
            locked_budget_cny=9000,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"assetid": "asset-7"},
        )
        reprice, _ = create_platform_action(
            session,
            action_type=PlatformActionType.REPRICE_LISTING,
            platform="eco",
            item_id=8,
            market_hash_name="M4A1-S | Cyrex (Field-Tested)",
            target_price=9000,
            locked_budget_cny=9000,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"platform_listing_id": "listing-8"},
        )
        listing_id = listing.id
        reprice_id = reprice.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_LISTING_SUBMITTED, "listed", platform_listing_id="listing-7"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result1 = worker.run_once(now=11, limit=1)
    adapter.result = NormalizedResult(True, RESULT_REPRICE_SUBMITTED, "repriced", platform_listing_id="listing-8")
    result2 = worker.run_once(now=12, limit=1)

    assert result1.risk_blocked == 0
    assert result2.risk_blocked == 0
    assert adapter.listings == [listing_id]
    assert adapter.reprices == [reprice_id]
    with SessionLocal() as session:
        listing = session.get(PlatformAction, listing_id)
        reprice = session.get(PlatformAction, reprice_id)
        assert listing.state == PlatformActionState.SUCCEEDED
        assert listing.platform_listing_id == "listing-7"
        assert reprice.state == PlatformActionState.SUCCEEDED
        assert reprice.platform_listing_id == "listing-8"


def test_worker_profit_blocks_negative_reprice(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.REPRICE_LISTING,
            platform="eco",
            item_id=9,
            market_hash_name="AWP | Asiimov (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=-0.01,
            next_check_at=0,
            request_payload={"platform_listing_id": "listing-9"},
        )
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_REPRICE_SUBMITTED, "repriced", platform_listing_id="listing-9"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.risk_blocked == 1
    assert adapter.reprices == []
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.RISK_BLOCKED
        assert action.error_code == "profit_floor_lock"


def test_worker_does_not_budget_block_delivery_action(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DELIVER_ORDER,
            platform="eco",
            item_id=10,
            market_hash_name="AK-47 | Slate (Field-Tested)",
            target_price=9000,
            locked_budget_cny=9000,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"order_id": "delivery-order-1"},
        )
        action.platform_order_id = "delivery-order-1"
        action_id = action.id

    adapter = PollingAdapter(NormalizedResult(True, RESULT_ORDER_PENDING, "delivering", platform_order_id="delivery-order-1"))
    worker = PlatformActionWorker(SessionLocal, adapters={"eco": adapter}, safe_mode=False)
    result = worker.run_once(now=11, limit=5)

    assert result.risk_blocked == 0
    assert result.waiting == 1
    assert adapter.submits == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.WAITING_PLATFORM
        assert action.platform_order_id == "delivery-order-1"
