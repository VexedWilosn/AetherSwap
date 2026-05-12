import time

from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine

from app.database import PlatformAction
from app.services.trading.actions import create_platform_action
from app.services.trading.runtime import (
    PlatformActionWorkerRuntime,
    PlatformActionWorkerRuntimeConfig,
    platform_action_worker_config_from_app_config,
)
from app.services.trading.states import PlatformActionState, PlatformActionType
from app.services.trading.worker import PlatformActionWorker


def test_platform_action_worker_config_defaults_to_disabled_safe_mode():
    config = platform_action_worker_config_from_app_config({})

    assert config.enabled is False
    assert config.safe_mode is True
    assert config.batch_size == 10
    assert config.poll_interval_seconds == 10


def test_platform_action_worker_runtime_processes_due_action_in_safe_mode(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
    )
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

    runtime = PlatformActionWorkerRuntime(SessionLocal)
    started = runtime.start(
        PlatformActionWorkerRuntimeConfig(
            enabled=True,
            safe_mode=True,
            poll_interval_seconds=0.1,
            batch_size=5,
            lease_seconds=30,
            error_backoff_seconds=1,
        )
    )

    try:
        assert started is True
        for _ in range(30):
            with SessionLocal() as session:
                action = session.get(PlatformAction, action_id)
                if action.state == PlatformActionState.SUCCEEDED:
                    break
            runtime.wake()
            time.sleep(0.05)
        else:
            raise AssertionError("runtime did not process due action")
    finally:
        assert runtime.stop(timeout_seconds=2) is True

    status = runtime.status()
    assert status["safe_mode"] is True
    assert status["total_claimed"] >= 1
    assert status["total_succeeded"] >= 1


def test_platform_action_worker_blocks_low_price_exposure_before_submit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime_exposure_guard.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with SessionLocal() as session:
        action, _ = create_platform_action(
            session,
            action_type=PlatformActionType.DIRECT_BUY,
            platform="buff",
            item_id=1,
            market_hash_name="Cheap Skin",
            target_price=0.01,
            quantity=1,
            next_check_at=0,
        )
        action_id = action.id

    worker = PlatformActionWorker(
        SessionLocal,
        safe_mode=True,
        app_config={"low_price_exposure_guard": {"enabled": True, "rule": "0-0-0.05", "block_execution": True}},
    )
    result = worker.run_once(now=10, limit=1)

    assert result.risk_blocked == 1
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        assert action.state == PlatformActionState.RISK_BLOCKED
        assert action.error_code == "low_price_exposure_quota"


def test_platform_action_worker_runtime_start_from_config_respects_disabled():
    runtime = PlatformActionWorkerRuntime(
        lambda: None,
        config_loader=lambda: {"trading_worker": {"enabled": False, "safe_mode": True}},
    )

    assert runtime.start_from_config() is False
    assert runtime.status()["running"] is False


def test_platform_action_worker_runtime_start_from_config_blocks_live_background_by_default():
    runtime = PlatformActionWorkerRuntime(
        lambda: None,
        config_loader=lambda: {
            "trading_worker": {"enabled": True, "safe_mode": False},
            "trading_live_canary": {"enabled": True, "kill_switch": False, "allow_background_worker": False},
        },
    )

    assert runtime.start_from_config() is False
    assert runtime.status()["running"] is False
    assert runtime.status()["safe_mode"] is False
