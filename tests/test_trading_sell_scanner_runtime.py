import time

from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, create_engine, select

from app.database import PlatformAction
from app.services.trading.sell_scanner import (
    SellerSnapshotScanner,
    SellerSnapshotScannerRuntime,
    SellerSnapshotScannerRuntimeConfig,
    seller_snapshot_scanner_config_from_app_config,
)


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sell_scanner_runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _scanner():
    return SellerSnapshotScanner(
        inventory_scanner=lambda: (
            True,
            [
                {
                    "item_id": 11,
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    "assetid": "asset-11",
                    "can_sell": True,
                    "target_price": 88.8,
                }
            ],
            "",
        ),
        steam_listings_scanner=lambda cookies: (True, set(), "", {}),
        credentials_loader=lambda: {"steam": {"cookies": "", "steam_id": "7656"}},
    )


def test_seller_snapshot_scanner_config_defaults_to_disabled_dry_run():
    config = seller_snapshot_scanner_config_from_app_config({})

    assert config.enabled is False
    assert config.commit is False
    assert config.interval_seconds == 3600
    assert config.listing_platform == "steam"
    assert config.delivery_platform == "c5game"


def test_seller_snapshot_scanner_runtime_run_once_dry_run_does_not_write(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    runtime = SellerSnapshotScannerRuntime(SessionLocal, scanner_factory=_scanner)

    result = runtime.run_once(
        SellerSnapshotScannerRuntimeConfig(
            commit=False,
            include_inventory=True,
            include_steam_listings=False,
            include_c5_orders=False,
            listing_platform="steam",
        )
    )

    assert len(result.plan.actions) == 1
    with SessionLocal() as session:
        rows = session.execute(select(PlatformAction)).scalars().all()
        assert rows == []
    status = runtime.status()
    assert status["total_runs"] == 1
    assert status["total_planned"] == 1
    assert status["total_created"] == 0


def test_seller_snapshot_scanner_runtime_run_once_commit_writes_actions(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    runtime = SellerSnapshotScannerRuntime(SessionLocal, scanner_factory=_scanner)

    result = runtime.run_once(
        SellerSnapshotScannerRuntimeConfig(
            commit=True,
            include_inventory=True,
            include_steam_listings=False,
            include_c5_orders=False,
            listing_platform="steam",
        )
    )

    assert len(result.created) == 1
    with SessionLocal() as session:
        rows = session.execute(select(PlatformAction)).scalars().all()
        assert len(rows) == 1
        assert rows[0].action_type == "steam_listing"
        assert rows[0].locked_budget_cny == 0
    status = runtime.status()
    assert status["total_runs"] == 1
    assert status["total_created"] == 1


def test_seller_snapshot_scanner_runtime_start_from_config_respects_disabled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    runtime = SellerSnapshotScannerRuntime(
        SessionLocal,
        config_loader=lambda: {"seller_snapshot_scanner": {"enabled": False}},
        scanner_factory=_scanner,
    )

    assert runtime.start_from_config() is False
    assert runtime.status()["running"] is False


def test_seller_snapshot_scanner_runtime_loop_runs_when_enabled(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    runtime = SellerSnapshotScannerRuntime(SessionLocal, scanner_factory=_scanner)
    started = runtime.start(
        SellerSnapshotScannerRuntimeConfig(
            enabled=True,
            commit=True,
            interval_seconds=0.1,
            error_backoff_seconds=0.1,
            include_inventory=True,
            include_steam_listings=False,
            include_c5_orders=False,
        )
    )

    try:
        assert started is True
        for _ in range(30):
            with SessionLocal() as session:
                rows = session.execute(select(PlatformAction)).scalars().all()
                if rows:
                    break
            runtime.wake()
            time.sleep(0.05)
        else:
            raise AssertionError("scanner runtime did not create a planned action")
    finally:
        assert runtime.stop(timeout_seconds=2) is True

    assert runtime.status()["total_runs"] >= 1
