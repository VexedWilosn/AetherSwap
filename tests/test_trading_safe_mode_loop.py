from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine

from app.database import PlatformAction
from app.services.trading.actions import create_platform_action
from app.services.trading.states import PlatformActionState, PlatformActionType
from app.services.trading.worker import PlatformActionWorker


def test_safe_mode_worker_completes_without_live_adapter(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'safe_mode.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.PURCHASE_ORDER,
            platform="buff",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
        )
        action_id = action.id

    worker = PlatformActionWorker(SessionLocal, safe_mode=True)
    result = worker.run_once(now=1000, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert len(worker.safe_adapter.calls) == 1

    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.SUCCEEDED
        assert action.platform_order_id == f"safe-{action_id}"
        assert action.finished_at == 1000


def test_safe_mode_cancel_action_marks_cancelled(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'safe_mode_cancel.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.CANCEL_ORDER,
            platform="steam",
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=0,
            quantity=1,
            expected_profit_rate=0.2,
            next_check_at=0,
            request_payload={"order_id": "steam-order-1"},
        )
        action.platform_order_id = "steam-order-1"
        action_id = action.id
        session.add(action)
        session.commit()

    worker = PlatformActionWorker(SessionLocal, safe_mode=True)
    result = worker.run_once(now=1000, limit=5)

    assert result.claimed == 1
    assert result.succeeded == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.CANCELLED
        assert action.platform_order_id == "steam-order-1"
