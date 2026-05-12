import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy import Column, Float, Integer, String
from sqlmodel import SQLModel, create_engine, select

import app.api as api
from app.database import PlatformAction, Purchase, TradeExecutionRecord
from app.services.trading.adapters import NormalizedResult, RESULT_NOT_FOUND, RESULT_ORDER_COMPLETED, RESULT_ORDER_PENDING, PlatformAdapterBase
from app.services.trading.canary import LiveCanarySmokeRegistry
from app.services.trading.states import PlatformActionState
from DataEngine.database import ArbitrageOpportunity, Base, ItemBase, MarketPrice, PlatformMapping, RadarSnapshot


def _patch_api_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trading_api.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
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


def test_platform_actions_api_filters_by_channel(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    for channel in ("live_canary", "auto"):
        resp = client.post(
            "/api/trade/platform_actions",
            json={
                "action_type": "purchase_order",
                "platform": "buff",
                "item_id": 1,
                "market_hash_name": f"AK-47 | Redline ({channel})",
                "target_price": 0.5,
                "quantity": 1,
                "expected_profit_rate": 0.2,
                "channel": channel,
            },
        )
        assert resp.status_code == 200

    resp = client.get("/api/trade/platform_actions?channel=live_canary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["channel"] == "live_canary"


def test_platform_actions_clear_terminal_actions_preserves_active_and_execution_records(monkeypatch, tmp_path):
    SessionLocal = _patch_api_db(monkeypatch, tmp_path)
    with SessionLocal() as session:
        session.add(
            PlatformAction(
                action_type="purchase_order",
                platform="buff",
                state=PlatformActionState.SUCCEEDED,
                item_id=1,
                market_hash_name="AK-47 | Redline (Field-Tested)",
            )
        )
        session.add(
            PlatformAction(
                action_type="direct_buy",
                platform="uuyp",
                state=PlatformActionState.FAILED,
                item_id=2,
                market_hash_name="AWP | Asiimov (Field-Tested)",
                error_message="test failure",
            )
        )
        active = PlatformAction(
            action_type="purchase_order",
            platform="eco",
            state=PlatformActionState.WAITING_PLATFORM,
            item_id=3,
            market_hash_name="M4A1-S | Cyrex (Field-Tested)",
            locked_budget_cny=5,
        )
        session.add(active)
        session.add(
            TradeExecutionRecord(
                created_at=1,
                action="direct_buy",
                channel="manual",
                item_id=1,
                market_hash_name="AK-47 | Redline (Field-Tested)",
                platform="buff",
                status="submitted",
            )
        )
        session.commit()
        active_id = active.id

    client = TestClient(api.app)
    resp = client.post("/api/trade/platform_actions/clear", json={"scope": "terminal"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["deleted"] == 0
    assert data["archived"] == 2
    assert data["cancelled"] == 0
    assert data["skipped"] == 0
    assert data["preserved_audit_records"] is True

    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert len(rows) == 1
    assert rows[0]["id"] == active_id
    assert rows[0]["state"] == PlatformActionState.WAITING_PLATFORM
    archived_rows = client.get("/api/trade/platform_actions?include_archived=true").json()["items"]
    assert len(archived_rows) == 3
    assert sum(1 for row in archived_rows if row["archived_at"]) == 2

    records = client.get("/api/trade/execution_records").json()["items"]
    assert len(records) == 1
    assert records[0]["status"] == "submitted"


def test_platform_actions_clear_active_requires_force_and_force_cancels_without_delete(monkeypatch, tmp_path):
    SessionLocal = _patch_api_db(monkeypatch, tmp_path)
    with SessionLocal() as session:
        active = PlatformAction(
            action_type="purchase_order",
            platform="buff",
            state=PlatformActionState.WAITING_PLATFORM,
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            locked_budget_cny=7.5,
            released_budget_cny=1.0,
        )
        session.add(active)
        session.commit()
        action_id = active.id

    client = TestClient(api.app)
    skipped = client.post("/api/trade/platform_actions/clear", json={"ids": [action_id]})

    assert skipped.status_code == 200
    skipped_data = skipped.json()
    assert skipped_data["deleted"] == 0
    assert skipped_data["cancelled"] == 0
    assert skipped_data["skipped"] == 1
    assert skipped_data["skipped_items"][0]["reason"] == "non_terminal_action_requires_force"

    forced = client.post("/api/trade/platform_actions/clear", json={"ids": [action_id], "force": True})

    assert forced.status_code == 200
    forced_data = forced.json()
    assert forced_data["deleted"] == 0
    assert forced_data["archived"] == 1
    assert forced_data["cancelled"] == 1
    assert forced_data["cancelled_ids"] == [action_id]

    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows == []
    archived_rows = client.get("/api/trade/platform_actions?include_archived=true").json()["items"]
    assert len(archived_rows) == 1
    assert archived_rows[0]["state"] == PlatformActionState.CANCELLED
    assert archived_rows[0]["locked_budget_cny"] == 0
    assert archived_rows[0]["released_budget_cny"] == 8.5
    assert archived_rows[0]["archived_at"]


def test_trade_reconcile_api_updates_waiting_order_and_materializes_purchase(monkeypatch, tmp_path):
    class FakeAdapter(PlatformAdapterBase):
        platform = "eco"

        def __init__(self):
            self.polls = []

        def poll_order(self, action):
            self.polls.append(action.id)
            return NormalizedResult(True, RESULT_ORDER_COMPLETED, platform_order_id=action.platform_order_id)

    adapter = FakeAdapter()
    SessionLocal = _patch_api_db(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "load_app_config", lambda: {})
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(api, "build_platform_adapters", lambda credentials=None, config=None: {"eco": adapter})
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", type("Runtime", (), {"status": lambda self: {"running": False}})())
    with SessionLocal() as session:
        action = PlatformAction(
            action_type="purchase_order",
            platform="eco",
            state=PlatformActionState.WAITING_PLATFORM,
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=1,
            platform_order_id="eco-order-1",
        )
        session.add(action)
        session.commit()
        action_id = action.id

    client = TestClient(api.app)
    resp = client.post("/api/trade/reconcile", json={"limit": 10})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["result"]["checked"] == 1
    assert data["result"]["succeeded"] == 1
    assert data["result"]["materialized"] == 1
    assert adapter.polls == [action_id]
    with SessionLocal() as session:
        action = session.get(PlatformAction, action_id)
        purchases = session.execute(select(Purchase)).scalars().all()
        assert action.state == PlatformActionState.SUCCEEDED
        assert len(purchases) == 1
        assert purchases[0].source_action_id == action_id


def test_trade_reconcile_api_can_align_inventory_after_materializing_purchase(monkeypatch, tmp_path):
    class FakeAdapter(PlatformAdapterBase):
        platform = "eco"

        def poll_order(self, action):
            return NormalizedResult(
                True,
                RESULT_ORDER_COMPLETED,
                platform_order_id=action.platform_order_id,
                filled_quantity=1,
                filled_amount_cny=100,
            )

    SessionLocal = _patch_api_db(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "load_app_config", lambda: {})
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(api, "build_platform_adapters", lambda credentials=None, config=None: {"eco": FakeAdapter()})
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", type("Runtime", (), {"status": lambda self: {"running": False}})())
    with SessionLocal() as session:
        action = PlatformAction(
            action_type="purchase_order",
            platform="eco",
            state=PlatformActionState.WAITING_PLATFORM,
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=1,
            platform_order_id="eco-order-1",
        )
        session.add(action)
        session.commit()
        action_id = action.id

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/reconcile",
        json={
            "limit": 10,
            "align_inventory": True,
            "inventory": [
                {"assetid": "asset-redline-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"},
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["materialized"] == 1
    assert data["inventory_alignment"]["matched"] == 1
    assert data["inventory_alignment"]["updated_actions"] == 1
    with SessionLocal() as session:
        purchase = session.execute(select(Purchase)).scalars().one()
        action = session.get(PlatformAction, action_id)
        assert purchase.assetid == "asset-redline-1"
        assert purchase.pending_receipt is False
        assert action.assetid == "asset-redline-1"


def test_trade_inventory_align_api_binds_pending_purchase_and_action(monkeypatch, tmp_path):
    SessionLocal = _patch_api_db(monkeypatch, tmp_path)
    with SessionLocal() as session:
        action = PlatformAction(
            action_type="purchase_order",
            platform="eco",
            state=PlatformActionState.SUCCEEDED,
            item_id=1,
            market_hash_name="AK-47 | Redline (Field-Tested)",
            target_price=100,
            quantity=1,
            platform_order_id="eco-order-1",
            filled_quantity=1,
            remaining_quantity=0,
        )
        session.add(action)
        session.flush()
        session.add(
            Purchase(
                name="AK-47 | Redline (Field-Tested)",
                goods_id=1,
                price=100,
                at=21,
                pending_receipt=True,
                source_platform="eco",
                source_action_id=action.id,
                source_order_id="eco-order-1",
                source_fill_index=1,
            )
        )
        session.commit()
        action_id = action.id

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/inventory_align",
        json={
            "inventory": [
                {"assetid": "asset-redline-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"},
            ],
            "limit": 10,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["result"]["scanned"] == 1
    assert data["result"]["pending"] == 1
    assert data["result"]["matched"] == 1
    assert data["result"]["updated_actions"] == 1
    with SessionLocal() as session:
        purchase = session.execute(select(Purchase)).scalars().one()
        action = session.get(PlatformAction, action_id)
        assert purchase.assetid == "asset-redline-1"
        assert purchase.pending_receipt is False
        assert action.assetid == "asset-redline-1"


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


def test_live_run_once_api_requires_canary_gate_when_safe_mode_false(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(api, "load_app_config", lambda: {})
    monkeypatch.setattr(api, "LIVE_CANARY_SMOKE_REGISTRY", LiveCanarySmokeRegistry())
    client = TestClient(api.app)

    create_resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 0.5,
            "quantity": 1,
            "channel": "live_canary",
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
        },
    )
    assert create_resp.status_code == 200

    run_resp = client.post("/api/trade/platform_actions/run_once", json={"safe_mode": False, "limit": 1})

    assert run_resp.status_code == 400
    data = run_resp.json()
    assert data["success"] is False
    assert data["reason"] == "live_canary_disabled"


def test_live_run_once_api_allows_after_canary_config_and_smoke(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    registry = LiveCanarySmokeRegistry()
    monkeypatch.setattr(api, "LIVE_CANARY_SMOKE_REGISTRY", registry)
    monkeypatch.setattr(api, "_load_credentials", lambda: {})
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "trading_live_canary": {
                "enabled": True,
                "kill_switch": False,
                "max_action_cny": 1.0,
                "max_daily_cny": 10.0,
                "allowed_platforms": ["buff"],
                "allowed_action_types": ["purchase_order"],
                "allowed_item_ids": [1],
                "require_recent_smoke_seconds": 900,
                "require_manual_run_once": True,
            },
            "trading_worker": {"safe_mode": True, "lease_seconds": 30},
        },
    )
    registry.record_results(
        [
            {
                "platform": "buff",
                "ok": True,
                "live_preflight": True,
                "ready_capabilities": ["purchase_order"],
            }
        ]
    )
    client = TestClient(api.app)
    create_resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 0.5,
            "quantity": 1,
            "channel": "live_canary",
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
            "request_payload": {"goods_id": "buff-goods-1"},
        },
    )
    assert create_resp.status_code == 200

    run_resp = client.post("/api/trade/platform_actions/run_once", json={"safe_mode": False, "limit": 1})

    assert run_resp.status_code == 200
    data = run_resp.json()
    assert data["success"] is True
    assert data["safe_mode"] is False
    assert data["result"]["failed"] == 1
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows[0]["state"] == "retry_wait"
    assert rows[0]["error_code"] == "auth_required"


def test_live_canary_status_reports_blocking_gate(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "LIVE_CANARY_SMOKE_REGISTRY", LiveCanarySmokeRegistry())
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", type("Runtime", (), {"status": lambda self: {"running": False}})())
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "trading_live_canary": {
                "enabled": True,
                "kill_switch": False,
                "max_action_cny": 1.0,
                "max_daily_cny": 10.0,
                "allowed_platforms": ["buff"],
                "allowed_action_types": ["purchase_order"],
                "allowed_item_ids": [1],
                "require_recent_smoke_seconds": 900,
            }
        },
    )
    client = TestClient(api.app)
    create_resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 0.5,
            "quantity": 1,
            "channel": "live_canary",
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
        },
    )
    assert create_resp.status_code == 200

    resp = client.get("/api/trade/live_canary/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["config"]["enabled"] is True
    assert data["worker"]["running"] is False
    assert data["gate"]["allowed"] is False
    assert data["gate"]["reason"] == "live_canary_smoke_required"
    assert data["gate"]["required_capability"] == "purchase_order"
    assert data["gate"]["next_action"]["channel"] == "live_canary"
    assert data["smoke"]["items"] == []


def test_live_canary_precheck_recommends_single_run_after_smoke(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    registry = LiveCanarySmokeRegistry()
    registry.record_results(
        [
            {
                "platform": "buff",
                "ok": True,
                "live_preflight": True,
                "ready_capabilities": ["purchase_order"],
            }
        ]
    )
    monkeypatch.setattr(api, "LIVE_CANARY_SMOKE_REGISTRY", registry)
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "trading_live_canary": {
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
        },
    )
    client = TestClient(api.app)
    client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 0.5,
            "quantity": 1,
            "channel": "live_canary",
            "expected_profit_rate": 0.2,
            "next_check_at": 0,
        },
    )

    resp = client.post("/api/trade/live_canary/precheck", json={"limit": 1})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["safe_to_call_run_once"] is True
    assert data["recommended_run_once_payload"] == {"safe_mode": False, "limit": 1}
    assert data["gate"]["allowed"] is True
    assert data["gate"]["smoke_recent"] is True


def test_create_platform_action_api_stores_fake_profit_only_in_raw_context(monkeypatch, tmp_path):
    _patch_api_db(monkeypatch, tmp_path)
    client = TestClient(api.app)

    resp = client.post(
        "/api/trade/platform_actions",
        json={
            "action_type": "purchase_order",
            "platform": "buff",
            "item_id": 1,
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "target_price": 0.5,
            "quantity": 1,
            "channel": "live_canary",
            "expected_profit_rate": 0.2,
            "fake_profit_rate": 9.9,
            "canary_profit_boost": 30,
            "raw_context": {"source": "api_test"},
        },
    )

    assert resp.status_code == 200
    item = resp.json()["item"]
    raw = json.loads(item["raw_context"])
    assert item["target_price"] == 0.5
    assert item["locked_budget_cny"] == 0.5
    assert item["expected_profit_rate"] == 0.2
    assert raw["source"] == "api_test"
    assert raw["test_signal"]["fake_profit_rate"] == 9.9
    assert raw["test_signal"]["canary_profit_boost"] == 30


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
    saved = {}
    config = {
        "automation_modules": {"auto_trading_enabled": True},
        "trading_worker": {"safe_mode": True},
    }
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", fake)
    monkeypatch.setattr(api, "load_app_config", lambda: config)
    monkeypatch.setattr(api, "save_app_config", lambda cfg: saved.update(cfg))

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
    assert saved["automation_modules"]["auto_trading_enabled"] is True
    assert saved["trading_worker"]["enabled"] is True

    wake = client.post("/api/trade/platform_actions/worker_wake")
    assert wake.status_code == 200
    assert wake.json()["woke"] is True

    stop = client.post("/api/trade/platform_actions/worker_stop", json={"timeout_seconds": 1})
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    assert stop.json()["worker"]["running"] is False
    assert saved["trading_worker"]["enabled"] is False


def test_platform_action_worker_start_blocks_live_background_by_default(monkeypatch):
    class FakeRuntime:
        def status(self):
            return {"running": False}

        def start(self, config):
            raise AssertionError("live background worker should be blocked before runtime start")

    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeRuntime())
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "trading_worker": {"safe_mode": True},
            "trading_live_canary": {
                "enabled": True,
                "kill_switch": False,
                "allow_background_worker": False,
            },
        },
    )
    client = TestClient(api.app)

    resp = client.post("/api/trade/platform_actions/worker_start", json={"safe_mode": False})

    assert resp.status_code == 400
    data = resp.json()
    assert data["success"] is False
    assert data["reason"] == "live_canary_background_worker_blocked"


def test_platform_action_worker_start_respects_paused_auto_trading(monkeypatch):
    class FakeRuntime:
        def status(self):
            return {"running": False}

        def start(self, config):
            raise AssertionError("paused auto trading should block worker start")

    saved = {}
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: {"automation_modules": {"auto_trading_enabled": False}})
    monkeypatch.setattr(api, "save_app_config", lambda cfg: saved.update(cfg))
    client = TestClient(api.app)

    resp = client.post("/api/trade/platform_actions/worker_start", json={"safe_mode": True})

    assert resp.status_code == 409
    assert resp.json()["reason"] == "auto_trading_disabled"
    assert saved == {}


def test_module_trading_stop_disables_worker_scanner_and_config(monkeypatch):
    class FakeWorkerRuntime:
        def __init__(self):
            self.running = True

        def status(self):
            return {"running": self.running, "safe_mode": True}

        def stop(self, timeout_seconds=5):
            self.running = False
            return True

    class FakeScannerRuntime:
        def __init__(self):
            self.running = True

        def status(self):
            return {"running": self.running, "commit": False}

        def stop(self, timeout_seconds=5):
            self.running = False
            return True

    saved = {}
    config = {
        "automation_modules": {"auto_trading_enabled": True},
        "trading_worker": {"enabled": True, "safe_mode": True},
        "seller_snapshot_scanner": {"enabled": True, "commit": False},
    }
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeWorkerRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: config)
    monkeypatch.setattr(api, "save_app_config", lambda cfg: saved.update(cfg))

    client = TestClient(api.app)
    resp = client.post("/api/modules/trading/stop")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["trading"]["enabled"] is False
    assert data["trading"]["worker_running"] is False
    assert saved["automation_modules"]["auto_trading_enabled"] is False
    assert saved["trading_worker"]["enabled"] is False
    assert saved["seller_snapshot_scanner"]["enabled"] is False


def test_module_trading_start_persists_only_after_worker_runs(monkeypatch):
    class FakeWorkerRuntime:
        def __init__(self):
            self.running = False

        def status(self):
            return {"running": self.running, "safe_mode": True}

        def start(self, config):
            self.running = True
            self.config = config
            return True

    class FakeScannerRuntime:
        def status(self):
            return {"running": False, "commit": False}

    saved = {}
    config = {
        "automation_modules": {"auto_trading_enabled": False},
        "trading_worker": {"enabled": False, "safe_mode": True},
        "seller_snapshot_scanner": {"enabled": False, "commit": False},
    }
    fake_worker = FakeWorkerRuntime()
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", fake_worker)
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: config)
    monkeypatch.setattr(api, "save_app_config", lambda cfg: saved.update(cfg))
    client = TestClient(api.app)

    resp = client.post("/api/modules/trading/start", json={"safe_mode": True, "batch_size": 3})

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["trading"]["enabled"] is True
    assert fake_worker.config["trading_worker"]["batch_size"] == 3
    assert saved["automation_modules"]["auto_trading_enabled"] is True
    assert saved["trading_worker"]["enabled"] is True
    assert saved["trading_worker"]["batch_size"] == 3
    assert saved["seller_snapshot_scanner"]["enabled"] is False


def test_module_trading_start_canary_failure_does_not_persist_enabled(monkeypatch):
    class FakeWorkerRuntime:
        def status(self):
            return {"running": False}

        def start(self, config):
            raise AssertionError("canary gate should block before runtime start")

    class FakeScannerRuntime:
        def status(self):
            return {"running": False}

    saved = {}
    config = {
        "automation_modules": {"auto_trading_enabled": False},
        "trading_worker": {"enabled": False, "safe_mode": True},
        "trading_live_canary": {"allow_background_worker": False},
    }
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeWorkerRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: config)
    monkeypatch.setattr(api, "save_app_config", lambda cfg: saved.update(cfg))
    client = TestClient(api.app)

    resp = client.post("/api/modules/trading/start", json={"safe_mode": False})

    assert resp.status_code == 400
    assert resp.json()["reason"] == "live_canary_background_worker_blocked"
    assert saved == {}


def test_modules_status_separates_scrape_and_trading(monkeypatch):
    class FakeWorkerRuntime:
        def status(self):
            return {"running": False, "safe_mode": True}

    class FakeScannerRuntime:
        def status(self):
            return {"running": False, "commit": False}

    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeWorkerRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: {"automation_modules": {"auto_trading_enabled": False}})
    monkeypatch.setattr(api, "ENGINE_PROCESS", None)

    client = TestClient(api.app)
    resp = client.get("/api/modules/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["scrape"]["running"] is False
    assert data["trading"]["enabled"] is False
    assert data["trading"]["running"] is False


def test_legacy_status_includes_trading_runtime(monkeypatch):
    class FakeWorkerRuntime:
        def status(self):
            return {"running": True, "safe_mode": True}

    class FakeScannerRuntime:
        def status(self):
            return {"running": False, "commit": False}

    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeWorkerRuntime())
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeScannerRuntime())
    monkeypatch.setattr(api, "load_app_config", lambda: {"automation_modules": {"auto_trading_enabled": True}})
    monkeypatch.setattr(api, "ENGINE_PROCESS", None)
    client = TestClient(api.app)

    resp = client.get("/api/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["running"] is True
    assert data["status"] == "running"
    assert data["engine_running"] is False
    assert data["trading"]["worker_running"] is True


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
    assert any(alert["message"] == "1 个动作已到期，但执行 worker 未运行" for alert in data["alerts"])
    assert any(alert["message"] == "当前动作占用预算 ¥100.00" for alert in data["alerts"])

    with api.get_session() as session:
        action = session.get(api.PlatformAction, create_resp.json()["item"]["id"])
        action.state = api.PlatformActionState.WAITING_PLATFORM
        session.add(action)
        session.commit()

    waiting_resp = client.get("/api/trade/automation_overview")
    assert waiting_resp.status_code == 200
    waiting_alerts = waiting_resp.json()["alerts"]
    assert any(alert["kind"] == "waiting_actions" for alert in waiting_alerts)
    assert any(alert["message"] == "1 个动作正在等待或重试" for alert in waiting_alerts)


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
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
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


def test_manual_buy_direct_uses_platform_adapter_and_tracks_offer(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_direct.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "cash_platform_trading": {
                "platforms": {"buff": {"order_poll_interval_seconds": 15}},
            }
        },
    )

    calls = []

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            calls.append((self.platform, action.action_type, action.request_payload))
            return NormalizedResult(
                True,
                RESULT_ORDER_PENDING,
                "BUFF direct buy submitted",
                platform_order_id="buff-direct-1",
                response_payload={
                    "success": True,
                    "msg": "BUFF direct buy submitted",
                    "order_id": "buff-direct-1",
                    "data": {"bill_order_ids": ["buff-direct-1"]},
                    "seller_offer_request": {"success": True},
                },
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123)
        session.add(item)
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={
            "action": "direct_buy",
            "platform": "buff",
            "item_id": 1,
            "buy_price": 100,
            "quantity": 1,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["action"] == "direct_buy"
    assert calls and calls[0][1] == "direct_buy"
    assert '"goods_id": 123' in calls[0][2]
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows[0]["state"] == PlatformActionState.WAITING_PLATFORM
    assert rows[0]["platform_order_id"] == "buff-direct-1"
    assert "seller_offer_request" in rows[0]["response_payload"]
    assert rows[0]["cost_basis_cny"] == 100
    assert "cost_batch_id" in rows[0]["raw_context"]
    records = client.get("/api/trade/execution_records").json()["items"]
    assert records[0]["status"] == "submitted"
    with SessionLocal() as session:
        assert session.execute(select(Purchase)).scalars().all() == []


def test_manual_buy_completed_direct_materializes_purchase(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_direct_completed.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(api, "load_app_config", lambda: {})

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            return NormalizedResult(
                True,
                RESULT_ORDER_COMPLETED,
                "completed",
                platform_order_id="buff-direct-complete-1",
                trade_offer_id="offer-complete-1",
                assetid="asset-complete-1",
                filled_quantity=1,
                filled_amount_cny=99,
                response_payload={"success": True, "category": RESULT_ORDER_COMPLETED},
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123)
        session.add(item)
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={
            "action": "direct_buy",
            "platform": "buff",
            "item_id": 1,
            "buy_price": 100,
            "quantity": 1,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows[0]["state"] == PlatformActionState.SUCCEEDED
    with SessionLocal() as session:
        purchases = session.execute(select(Purchase)).scalars().all()
        assert len(purchases) == 1
        assert purchases[0].name == "AK-47 | Redline (Field-Tested)"
        assert purchases[0].price == 99
        assert purchases[0].assetid == "asset-complete-1"
        assert purchases[0].pending_receipt is False
        assert purchases[0].source_platform == "buff"
        assert purchases[0].source_action_id == data["platform_action_id"]
        assert purchases[0].source_order_id == "buff-direct-complete-1"
        assert purchases[0].source_trade_offer_id == "offer-complete-1"
        assert purchases[0].source_fill_index == 1


def test_manual_buy_purchase_order_stays_purchase_order(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_purchase_order.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(api, "load_app_config", lambda: {})

    submitted = []

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            submitted.append(action.action_type)
            return NormalizedResult(
                True,
                RESULT_ORDER_PENDING,
                "purchase order submitted",
                platform_order_id="buff-order-1",
                response_payload={"success": True, "order_id": "buff-order-1"},
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123)
        session.add(item)
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={
            "action": "platform_order",
            "platform": "buff",
            "item_id": 1,
            "buy_price": 100,
            "quantity": 1,
        },
    )

    assert resp.status_code == 200
    assert submitted == ["purchase_order"]
    rows = client.get("/api/trade/platform_actions").json()["items"]
    assert rows[0]["action_type"] == "purchase_order"
    assert rows[0]["state"] == PlatformActionState.WAITING_PLATFORM
    assert "cost_batch_id" in rows[0]["raw_context"]
    records = client.get("/api/trade/execution_records").json()["items"]
    assert records[0]["status"] == "submitted"


def test_manual_buy_low_price_exposure_guard_blocks_before_adapter(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_low_price_guard.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {"low_price_exposure_guard": {"enabled": True, "rule": "0-0-0.05", "block_execution": True}},
    )

    calls = []

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, action):
            calls.append(action)
            return NormalizedResult(True, RESULT_ORDER_PENDING, "submitted")

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="Cheap Skin", buff_goods_id=123))
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={"action": "direct_buy", "platform": "buff", "item_id": 1, "buy_price": 0.01, "quantity": 1},
    )

    assert resp.status_code == 409
    assert resp.json()["reason"] == "low_price_exposure_quota"
    assert calls == []
    with SessionLocal() as session:
        assert session.execute(select(PlatformAction)).scalars().all() == []
        assert session.execute(select(TradeExecutionRecord)).scalars().all() == []


def test_manual_steam_order_low_price_exposure_guard_blocks_before_steam_buyer(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_steam_low_price_guard.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"steam": {"cookies": "sessionid=ok"}})
    monkeypatch.setattr("app.state.get_inventory", lambda: [])
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {"low_price_exposure_guard": {"enabled": True, "rule": "0-0-0.05", "block_execution": True}},
    )

    calls = []

    class FakeSteamBuyer:
        def __init__(self, *args, **kwargs):
            calls.append(("init", args, kwargs))

        def create_buy_order(self, *args, **kwargs):
            calls.append(("create_buy_order", args, kwargs))
            raise AssertionError("SteamBuyer should not be called when exposure guard blocks")

    monkeypatch.setattr(api, "SteamBuyer", FakeSteamBuyer)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="Cheap Steam Skin"))
        session.add(MarketPrice(item_id=1, platform_name="steam", data_source="test", buy_max=0.02, sell_min=0.03))
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_steam_order",
        json={"item_id": 1, "buy_price": 0.01, "quantity": 1},
    )

    assert resp.status_code == 409
    assert resp.json()["reason"] == "low_price_exposure_quota"
    assert calls == []
    with SessionLocal() as session:
        assert session.execute(select(TradeExecutionRecord)).scalars().all() == []


def test_opportunities_api_sorts_before_limit_and_returns_percent_rates(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunities_sort.db'}")
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "load_app_config", lambda: {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="High Profit Knife"))
        session.add(
            ArbitrageOpportunity(
                id=1,
                item_id=1,
                buy_platform="buff",
                buy_price=50.0,
                sell_platform="steam",
                sell_price=100.0,
                profit_cny=36.9565,
                profit_rate=0.73913,
                status="open",
            )
        )
        for idx in range(2, 62):
            session.add(ItemBase(id=idx, market_hash_name=f"Low Cashout {idx}"))
            session.add(
                ArbitrageOpportunity(
                    id=idx,
                    item_id=idx,
                    buy_platform="steam",
                    buy_price=100.0,
                    sell_platform="uuyp",
                    sell_price=77.0,
                    profit_cny=0.1,
                    profit_rate=0.001,
                    status="open",
                )
            )
        session.commit()

    client = TestClient(api.app)
    resp = client.get("/api/opportunities")

    assert resp.status_code == 200
    rows = resp.json()
    assert rows[0]["id"] == 1
    assert rows[0]["direction"] == "cash_to_steam"
    assert rows[0]["profit_rate"] > 60
    assert rows[0]["raw_profit_rate"] == 0.73913
    assert any(row["direction"] == "steam_to_cash" for row in rows)


def test_opportunities_api_keeps_verifying_rows_available_for_filter(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunities_status_mix.db'}")
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "load_app_config", lambda: {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    with SessionLocal() as session:
        for idx in range(1, 231):
            session.add(ItemBase(id=idx, market_hash_name=f"Open Signal {idx}"))
            session.add(
                ArbitrageOpportunity(
                    id=idx,
                    item_id=idx,
                    buy_platform="buff",
                    buy_price=50.0,
                    sell_platform="steam",
                    sell_price=70.0,
                    profit_cny=9.5,
                    profit_rate=0.19,
                    status="open",
                )
            )
        for idx in range(231, 234):
            session.add(ItemBase(id=idx, market_hash_name=f"Verifying Signal {idx}"))
            session.add(
                ArbitrageOpportunity(
                    id=idx,
                    item_id=idx,
                    buy_platform="steam",
                    buy_price=100.0,
                    sell_platform="uuyp",
                    sell_price=78.0,
                    profit_cny=1.44,
                    profit_rate=0.0192,
                    status="verifying",
                )
            )
        session.commit()

    client = TestClient(api.app)
    resp = client.get("/api/opportunities")

    assert resp.status_code == 200
    rows = resp.json()
    statuses = {row["status"] for row in rows}
    assert "open" in statuses
    assert "verifying" in statuses


def test_opportunities_api_hides_baseline_backed_signals(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunities_baseline_hidden.db'}")
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "load_app_config", lambda: {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="Baseline Item"))
        session.add_all(
            [
                MarketPrice(item_id=1, platform_name="steam", data_source="steamdt_openapi", sell_min=0.47, buy_max=0.0),
                MarketPrice(item_id=1, platform_name="buff", data_source="baseline", sell_min=0.01, buy_max=0.0),
                ArbitrageOpportunity(
                    id=1,
                    item_id=1,
                    buy_platform="buff",
                    buy_price=0.01,
                    sell_platform="steam",
                    sell_price=0.47,
                    profit_cny=0.1,
                    profit_rate=10.0,
                    status="open",
                ),
            ]
        )
        session.commit()

    client = TestClient(api.app)
    assert client.get("/api/opportunities").json() == []


def test_opportunities_api_hides_low_price_exposure_quota_signal(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunities_exposure_hidden.db'}")
    SQLModel.metadata.create_all(engine, tables=[Purchase.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {
            "pipeline": {"steam_balance_cost_ratio": 0.75},
            "low_price_exposure_guard": {"enabled": True, "rule": "0-1-0.05", "hide_signals": True},
        },
    )

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="Cheap Skin"))
        session.add(Purchase(goods_id=1, name="Cheap Skin", price=0.02))
        session.add(
            ArbitrageOpportunity(
                id=1,
                item_id=1,
                buy_platform="buff",
                buy_price=0.02,
                sell_platform="steam",
                sell_price=0.2,
                profit_cny=0.1,
                profit_rate=1.0,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    assert client.get("/api/opportunities").json() == []


def test_execute_opportunity_routes_to_manual_buy_bridge(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execute_opportunity_bridge.db'}")
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)

    captured: dict[str, object] = {}

    def fake_manual_buy(payload):
        captured.update(payload)
        return {"success": True, "msg": "ok"}

    monkeypatch.setattr(api, "api_trade_manual_buy", fake_manual_buy)

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123)
        session.add(item)
        session.add(
            ArbitrageOpportunity(
                id=7,
                item_id=1,
                buy_platform="BUFF",
                buy_price=88.8,
                sell_platform="steam",
                sell_price=99.9,
                profit_cny=11.1,
                profit_rate=12.5,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post("/api/execute_opportunity/7")

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert captured["item_id"] == 1
    assert captured["platform"] == "buff"
    assert captured["buy_price"] == 88.8
    assert captured["quantity"] == 1
    assert captured["action"] == "direct_buy"
    assert captured["opportunity_id"] == 7
    assert captured["trigger"] == "opportunity_execute"


def test_execute_opportunity_accepts_action_override(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execute_opportunity_override.db'}")
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)

    captured: dict[str, object] = {}

    def fake_manual_buy(payload):
        captured.update(payload)
        return {"success": True, "msg": "ok"}

    monkeypatch.setattr(api, "api_trade_manual_buy", fake_manual_buy)

    with SessionLocal() as session:
        item = ItemBase(id=1, market_hash_name="M4A1-S | Printstream (Field-Tested)")
        session.add(item)
        session.add(
            ArbitrageOpportunity(
                id=9,
                item_id=1,
                buy_platform="uu",
                buy_price=66.6,
                sell_platform="steam",
                sell_price=75.0,
                profit_cny=8.4,
                profit_rate=12.6,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/execute_opportunity/9",
        json={"action": "purchase_order", "quantity": 2},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert captured["platform"] == "uuyp"
    assert captured["action"] == "purchase_order"
    assert captured["quantity"] == 2


def test_execute_opportunity_closes_stale_opportunity_on_not_found(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execute_opportunity_stale.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(api, "load_app_config", lambda: {})

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            return NormalizedResult(
                False,
                RESULT_NOT_FOUND,
                "No BUFF sell order at or below target price",
                response_payload={
                    "success": False,
                    "reason": "not_found",
                    "msg": "No BUFF sell order at or below target price",
                },
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123))
        session.add(
            ArbitrageOpportunity(
                id=11,
                item_id=1,
                buy_platform="buff",
                buy_price=0.02,
                sell_platform="steam",
                sell_price=0.03,
                profit_cny=0.01,
                profit_rate=50,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post("/api/execute_opportunity/11")

    assert resp.status_code == 400
    data = resp.json()
    assert data["stale_opportunity"] is True
    assert data["closed_opportunity_id"] == 11
    assert data["refresh_opportunities"] is True

    with SessionLocal() as session:
        assert session.get(ArbitrageOpportunity, 11).status == "closed"
    assert client.get("/api/get_opportunities").json() == []


def test_execute_opportunity_blocks_baseline_signal_before_platform_call(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'execute_opportunity_baseline_block.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, MarketPrice.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)

    class UnexpectedAdapter:
        def __init__(self, platform, **kwargs):
            raise AssertionError("baseline signal should not reach platform adapter")

    monkeypatch.setattr(api, "PlatformClientAdapter", UnexpectedAdapter)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="Baseline Item", buff_goods_id=123))
        session.add_all(
            [
                MarketPrice(item_id=1, platform_name="steam", data_source="steamdt_openapi", sell_min=0.47, buy_max=0.0),
                MarketPrice(item_id=1, platform_name="buff", data_source="baseline", sell_min=0.01, buy_max=0.0),
                ArbitrageOpportunity(
                    id=21,
                    item_id=1,
                    buy_platform="buff",
                    buy_price=0.01,
                    sell_platform="steam",
                    sell_price=0.47,
                    profit_cny=0.1,
                    profit_rate=10.0,
                    status="open",
                ),
            ]
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post("/api/execute_opportunity/21")

    assert resp.status_code == 409
    data = resp.json()
    assert data["reason"] == "non_decision_market_price"
    assert data["stale_opportunity"] is True
    assert data["closed_opportunity_id"] == 21
    with SessionLocal() as session:
        assert session.get(ArbitrageOpportunity, 21).status == "closed"
        assert session.query(TradeExecutionRecord).count() == 0
        assert session.query(PlatformAction).count() == 0


def test_manual_buy_not_found_closes_matching_open_opportunity_without_id(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_buy_stale_fallback.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(api, "load_app_config", lambda: {})

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            return NormalizedResult(
                False,
                RESULT_NOT_FOUND,
                "No BUFF sell order at or below target price",
                response_payload={
                    "success": False,
                    "reason": "not_found",
                    "msg": "No BUFF sell order at or below target price",
                },
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123))
        session.add(
            ArbitrageOpportunity(
                id=12,
                item_id=1,
                buy_platform="buff",
                buy_price=0.02,
                sell_platform="steam",
                sell_price=0.03,
                profit_cny=0.01,
                profit_rate=50,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={"action": "direct_buy", "platform": "buff", "item_id": 1, "buy_price": 0.02, "quantity": 1},
    )

    assert resp.status_code == 400
    data = resp.json()
    assert data["stale_opportunity"] is True
    assert data["closed_opportunity_id"] == 12
    with SessionLocal() as session:
        assert session.get(ArbitrageOpportunity, 12).status == "closed"


def test_manual_buy_listing_missing_message_also_closes_opportunity(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual_buy_stale_message.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__, Purchase.__table__, TradeExecutionRecord.__table__])
    Base.metadata.create_all(engine, tables=[ItemBase.__table__, PlatformMapping.__table__, ArbitrageOpportunity.__table__, RadarSnapshot.__table__])
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "get_session", lambda: SessionLocal())
    monkeypatch.setattr(api, "SessionLocal", SessionLocal)
    monkeypatch.setattr(api, "_load_credentials", lambda: {"buff": {"cookies": "session=ok"}})
    monkeypatch.setattr(api, "load_app_config", lambda: {})

    class FakeAdapter:
        def __init__(self, platform, **kwargs):
            self.platform = platform

        def submit(self, action):
            return NormalizedResult(
                False,
                "fatal_error",
                "No listing available at or below target price",
                response_payload={
                    "success": False,
                    "reason": "remote_error",
                    "msg": "No listing available at or below target price",
                },
            )

    monkeypatch.setattr(api, "PlatformClientAdapter", FakeAdapter)

    with SessionLocal() as session:
        session.add(ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", buff_goods_id=123))
        session.add(
            ArbitrageOpportunity(
                id=13,
                item_id=1,
                buy_platform="buff",
                buy_price=0.02,
                sell_platform="steam",
                sell_price=0.03,
                profit_cny=0.01,
                profit_rate=50,
                status="open",
            )
        )
        session.commit()

    client = TestClient(api.app)
    resp = client.post(
        "/api/trade/manual_buy",
        json={"action": "direct_buy", "platform": "buff", "item_id": 1, "buy_price": 0.02, "quantity": 1},
    )

    assert resp.status_code == 400
    data = resp.json()
    assert data["stale_opportunity"] is True
    assert data["closed_opportunity_id"] == 13
    with SessionLocal() as session:
        assert session.get(ArbitrageOpportunity, 13).status == "closed"
