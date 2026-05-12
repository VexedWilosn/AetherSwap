from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column, Float, Integer, String
from sqlmodel import SQLModel, create_engine

import app.api as api
from app.database import PlatformAction, TradeExecutionRecord
from DataEngine.database import Base, ItemBase, PlatformMapping


def _patch_api_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trading_api.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, TradeExecutionRecord.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    return SessionLocal


def test_platform_capabilities_api_lists_core_platforms():
    client = TestClient(api.app)
    resp = client.get("/api/trade/platform_capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert {"buff", "uuyp", "eco", "c5game", "steam"}.issubset(data["platforms"])


def test_create_platform_action_api_records_risk_blocked_action(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 3100,
            "quantity": 1,
            "expected_profit_rate": 0.2,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["risk"]["allowed"] is False
    assert data["item"]["state"] == "risk_blocked"
    assert data["item"]["error_code"] == "single_item_budget_exceeded"

    list_resp = client.get("/api/trade/platform_actions")
    assert list_resp.status_code == 200
    rows = list_resp.json()["items"]
    assert len(rows) == 1
    assert rows[0]["locked_budget_cny"] == 3100
    assert rows[0]["risk_category"] == "ak-47 | redline"

    summary = client.get("/api/trade/platform_action_summary").json()
    assert summary["by_state"]["risk_blocked"]["count"] == 1


def test_create_platform_action_api_routes_sell_actions_without_buy_budget(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "platform_listing",
            "platform": "eco",
            "item_id": 3,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 3100,
            "quantity": 1,
            "expected_profit_rate": 0.2,
            "assetid": "asset-eco-1",
            "steam_id": "7656",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["risk"]["allowed"] is True
    assert data["item"]["locked_budget_cny"] == 0
    assert data["item"]["state"] == "queued"
    assert data["item"]["assetid"] == "asset-eco-1"


def test_seller_actions_api_batches_delivery_and_reprice(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/seller_actions",
        json={
            "channel": "test_batch",
            "actions": [
                {
                    "action_type": "deliver_order",
                    "platform": "c5",
                    "item_id": 4,
                    "market_hash_name": "AWP | Asiimov (Field-Tested)",
                    "platform_order_id": "c5-order-1",
                },
                {
                    "action_type": "reprice_listing",
                    "platform": "buff",
                    "item_id": 5,
                    "market_hash_name": "M4A1-S | Printstream (Field-Tested)",
                    "target_price": 66.6,
                    "platform_listing_id": "sell-order-1",
                    "expected_profit_rate": -0.01,
                },
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["created"] == 2
    assert data["items"][0]["item"]["platform"] == "c5game"
    assert data["items"][0]["item"]["action_type"] == "deliver_order"
    assert data["items"][0]["item"]["locked_budget_cny"] == 0
    assert data["items"][1]["item"]["state"] == "risk_blocked"
    assert data["items"][1]["item"]["error_code"] == "profit_floor_lock"


def test_seller_actions_plan_api_supports_dry_run_and_commit(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)
    payload = {
        "listing_platform": "eco",
        "steam_id": "7656",
        "inventory": [
            {
                "item_id": 6,
                "market_hash_name": "AK-47 | Slate (Field-Tested)",
                "assetid": "asset-6",
                "can_sell": True,
                "target_price": 55.5,
            }
        ],
        "orders": [
            {
                "item_id": 7,
                "market_hash_name": "AWP | Asiimov (Field-Tested)",
                "orderId": "c5-order-7",
                "status": 2,
                "orderConfirmInfoDTO": {"offerId": "offer-7"},
            }
        ],
    }

    dry = client.post("/api/trade/seller_actions/plan", json=payload)
    assert dry.status_code == 200
    dry_data = dry.json()
    assert dry_data["success"] is True
    assert dry_data["committed"] is False
    assert dry_data["planned"] == 2
    assert [row["action_type"] for row in dry_data["actions"]] == ["platform_listing", "accept_trade_offer"]
    assert client.get("/api/trade/platform_actions").json()["total"] == 0

    commit = client.post("/api/trade/seller_actions/plan", json={**payload, "commit": True})
    assert commit.status_code == 200
    commit_data = commit.json()
    assert commit_data["success"] is True
    assert commit_data["committed"] is True
    assert commit_data["created"] == 2
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert {row["action_type"] for row in rows} == {"platform_listing", "accept_trade_offer"}
    assert all(row["locked_budget_cny"] == 0 for row in rows)


def test_seller_actions_api_dry_run_uses_snapshot_planner(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/seller_actions",
        json={
            "dry_run": True,
            "listing_platform": "steam",
            "inventory": [
                {
                    "item_id": 8,
                    "market_hash_name": "M4A1-S | Cyrex (Field-Tested)",
                    "assetid": "asset-8",
                    "can_sell": True,
                    "target_price": 44.4,
                }
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["planned"] == 1
    assert data["actions"][0]["action_type"] == "steam_listing"


def test_seller_actions_scan_api_dry_run_and_commit(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)

    class FakeScanner:
        def __init__(self):
            from app.services.trading.sell_actions import SellerActionService

            self.service = SellerActionService()
            self.snapshot = {
                "inventory": [],
                "active_assetids": [],
                "orders": [
                    {
                        "item_id": 9,
                        "market_hash_name": "AWP | Asiimov (Field-Tested)",
                        "orderId": "c5-order-9",
                        "status": 1,
                    }
                ],
            }
            self.diagnostics = {"sources": {"fake": {"ok": True, "count": 1, "error": ""}}}

        def plan(self, payload):
            from app.services.trading.sell_scanner import SellerSnapshotRunResult, SellerSnapshotScan

            scan = SellerSnapshotScan(snapshot=self.snapshot, diagnostics=self.diagnostics)
            return SellerSnapshotRunResult(scan=scan, plan=self.service.plan_from_snapshot(self.snapshot), created=[])

        def plan_and_create(self, session_factory, payload):
            from app.services.trading.sell_scanner import SellerSnapshotRunResult, SellerSnapshotScan

            scan = SellerSnapshotScan(snapshot=self.snapshot, diagnostics=self.diagnostics)
            with session_factory() as session:
                created = self.service.plan_and_create(session, self.snapshot)
            return SellerSnapshotRunResult(scan=scan, plan=created.plan, created=created.created)

    monkeypatch.setattr(api, "SellerSnapshotScanner", FakeScanner)
    client = TestClient(api.app)

    dry = client.post("/api/trade/seller_actions/scan", json={})
    assert dry.status_code == 200
    dry_data = dry.json()
    assert dry_data["committed"] is False
    assert dry_data["planned"] == 1
    assert dry_data["actions"][0]["action_type"] == "deliver_order"

    commit = client.post("/api/trade/seller_actions/scan", json={"commit": True})
    assert commit.status_code == 200
    commit_data = commit.json()
    assert commit_data["committed"] is True
    assert commit_data["created"] == 1
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert len(rows) == 1
    assert rows[0]["action_type"] == "deliver_order"


def test_run_once_api_processes_due_action_in_safe_mode(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    create_resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 2,
            "market_hash_name": "AWP | Asiimov (Field-Tested)",
            "target_price": 100,
            "quantity": 1,
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
        },
    )
    assert create_resp.status_code == 200

    run_resp = client.post("/api/trade/platform_actions/run_once", json={"safe_mode": True, "limit": 5})
    assert run_resp.status_code == 200
    assert run_resp.json()["result"]["succeeded"] == 1

    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows[0]["state"] == "succeeded"


def test_platform_action_smoke_api_checks_capability_readiness(monkeypatch):
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(api, "load_app_config", lambda: {})
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/platform_actions/smoke",
        json={"safe_mode": True, "platforms": ["eco"], "capabilities": ["purchase_order", "order_status"]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["ok"] is True
    assert data["items"][0]["platform"] == "eco"
    assert data["items"][0]["safe_mode"] is True
    assert data["items"][0]["missing_capabilities"] == []


def test_platform_action_smoke_api_reports_missing_capability(monkeypatch):
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(api, "load_app_config", lambda: {})
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/platform_actions/smoke",
        json={"safe_mode": True, "platforms": ["c5game"], "capabilities": ["direct_buy"]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["items"][0]["missing_capabilities"] == ["direct_buy"]


def test_platform_action_worker_api_controls_runtime(monkeypatch):
    class FakeRuntime:
        def __init__(self):
            self.running = False
            self.woke = False

        def status(self):
            return {
                "running": self.running,
                "safe_mode": True,
                "batch_size": 2,
            }

        def start(self, config):
            self.running = True
            self.config = config
            return True

        def stop(self, timeout_seconds=5):
            self.running = False
            self.timeout_seconds = timeout_seconds
            return True

        def wake(self):
            self.woke = self.running
            return self.woke

    fake = FakeRuntime()
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", fake)
    monkeypatch.setattr(api, "load_app_config", lambda: {"trading_worker": {"safe_mode": True}})

    client = TestClient(api.app)
    status = client.get("/api/trade/platform_actions/worker_status")
    assert status.status_code == 200
    assert status.json()["worker"]["running"] is False

    start = client.post(
        "/api/trade/platform_actions/worker_start",
        json={"safe_mode": True, "batch_size": 2, "poll_interval_seconds": 0.2},
    )
    assert start.status_code == 200
    assert start.json()["started"] is True
    assert start.json()["worker"]["running"] is True
    assert fake.config["trading_worker"]["batch_size"] == 2

    wake = client.post("/api/trade/platform_actions/worker_wake")
    assert wake.status_code == 200
    assert wake.json()["woke"] is True

    stop = client.post("/api/trade/platform_actions/worker_stop", json={"timeout_seconds": 1})
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    assert stop.json()["worker"]["running"] is False


def test_automation_overview_api_combines_runtimes_and_action_alerts(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)

    class FakeWorkerRuntime:
        def status(self):
            return {"running": False, "safe_mode": True, "last_error": ""}

    class FakeScannerRuntime:
        def status(self):
            return {"running": False, "commit": False, "last_error": ""}

    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeWorkerRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())

    client = TestClient(api.app)
    create_resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 10,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 100,
            "quantity": 1,
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
        },
    )
    assert create_resp.status_code == 200

    resp = client.get("/api/trade/automation_overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["worker"]["running"] is False
    assert data["scanner"]["running"] is False
    assert data["summary"]["due_count"] == 1
    assert data["summary"]["active_locked_budget_cny"] == 100
    assert any(alert["kind"] == "due_worker_stopped" for alert in data["alerts"])
    assert any(alert["kind"] == "locked_budget" for alert in data["alerts"])


def test_seller_action_scanner_api_controls_runtime(monkeypatch):
    class FakeRuntime:
        def __init__(self):
            self.running = False
            self.woke = False

        def status(self):
            return {
                "running": self.running,
                "commit": False,
                "interval_seconds": 60,
            }

        def start(self, config):
            self.running = True
            self.config = config
            return True

        def stop(self, timeout_seconds=5):
            self.running = False
            self.timeout_seconds = timeout_seconds
            return True

        def wake(self):
            self.woke = self.running
            return self.woke

    fake = FakeRuntime()
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", fake)
    monkeypatch.setattr(api, "load_app_config", lambda: {"seller_snapshot_scanner": {"commit": False}})

    client = TestClient(api.app)
    status = client.get("/api/trade/seller_actions/scanner_status")
    assert status.status_code == 200
    assert status.json()["scanner"]["running"] is False

    start = client.post(
        "/api/trade/seller_actions/scanner_start",
        json={"commit": False, "interval_seconds": 120, "listing_platform": "steam"},
    )
    assert start.status_code == 200
    assert start.json()["started"] is True
    assert start.json()["scanner"]["running"] is True
    assert fake.config["seller_snapshot_scanner"]["interval_seconds"] == 120

    wake = client.post("/api/trade/seller_actions/scanner_wake")
    assert wake.status_code == 200
    assert wake.json()["woke"] is True

    stop = client.post("/api/trade/seller_actions/scanner_stop", json={"timeout_seconds": 1})
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    assert stop.json()["scanner"]["running"] is False


def test_seller_action_scanner_run_once_api_uses_runtime(monkeypatch):
    class FakePlan:
        actions = [{"action_type": "steam_listing"}]
        skipped = [{"reason": "already_listed"}]

    class FakeScan:
        snapshot = {"inventory": [{"assetid": "asset-1"}], "active_assetids": [], "orders": []}
        diagnostics = {"sources": {"fake": {"ok": True, "count": 1, "error": ""}}}

    class FakeResult:
        scan = FakeScan()
        plan = FakePlan()
        created = []

    class FakeRuntime:
        def run_once(self, config):
            self.config = config
            return FakeResult()

        def status(self):
            return {"running": False, "total_runs": 1}

    fake = FakeRuntime()
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", fake)
    monkeypatch.setattr(api, "load_app_config", lambda: {"seller_snapshot_scanner": {"commit": False}})

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/seller_actions/scanner_run_once",
        json={
            "commit": False,
            "include_inventory": False,
            "inventory": [{"assetid": "asset-1"}],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["committed"] is False
    assert data["planned"] == 1
    assert data["created"] == 0
    assert data["actions"][0]["action_type"] == "steam_listing"
    assert fake.config.snapshot_payload["inventory"] == [{"assetid": "asset-1"}]


def test_manual_buy_bridge_records_non_claimable_platform_action(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_bridge.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {})

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123)
        session.add(item)
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={
            "action": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "buy_price": 100,
            "quantity": 1,
        },
    )
    assert resp.status_code == 400

    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert rows[0]["error_code"] == "missing_credentials"
