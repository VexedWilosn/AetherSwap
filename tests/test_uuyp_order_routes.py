import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.uuyp_orders import router
from app.services import uuyp_orders


class FakeBuyer:
    def search_item_id_by_name(self, name):
        return "110797"

    def create_buy_order(self, template_id, price, quantity, market_hash_name="", commodity_name=""):
        return {"code": 0, "data": {"orderNo": "PO-1"}}

    def select_best_listing(self, template_id, max_price=None):
        return {"commodityNo": "C-1", "price": 0.02, "templateId": template_id}


def test_submit_purchase_order_appends_pending_purchase(monkeypatch):
    purchases = []
    monkeypatch.setattr(uuyp_orders, "append_purchase", purchases.append)

    result = uuyp_orders.submit_purchase_order(
        "P250 | Copper Oxide (Field-Tested)",
        price=0.02,
        quantity=2,
        buyer=FakeBuyer(),
    )

    assert result["ok"] is True
    assert result["template_id"] == "110797"
    assert result["order_no"] == "PO-1"
    assert len(purchases) == 2
    assert purchases[0]["pending_receipt"] is True
    assert purchases[0]["listing_status"] == "uuyp_purchase_order:PO-1"


def test_manual_direct_requires_start_and_returns_buyable_url(monkeypatch):
    monkeypatch.setattr(uuyp_orders, "_open_persistent_browser", lambda url: {"ok": True, "browser": "test"})

    uuyp_orders.set_manual_control(enabled=False, paused=True)
    paused = uuyp_orders.prepare_manual_direct("Test Item", target_price=1.0, buyer=FakeBuyer())
    assert paused["ok"] is False

    uuyp_orders.set_manual_control(enabled=True, paused=False)
    result = uuyp_orders.prepare_manual_direct("Test Item", target_price=1.0, buyer=FakeBuyer())
    assert result["ok"] is True
    assert result["template_id"] == "110797"
    assert "templateId=110797" in result["url"]
    assert result["listing"]["commodityNo"] == "C-1"


def test_manual_record_appends_pending_purchase(monkeypatch):
    purchases = []
    monkeypatch.setattr(uuyp_orders, "append_purchase", purchases.append)

    result = uuyp_orders.record_manual_direct_order(
        "Test Item",
        price=1.23,
        quantity=1,
        template_id="42",
        order_no="M-1",
    )

    assert result["ok"] is True
    assert purchases[0]["pending_receipt"] is True
    assert purchases[0]["listing_status"] == "uuyp_manual_direct:M-1"


def test_uuyp_routes_are_available(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(
        "app.routes.uuyp_orders.submit_purchase_order",
        lambda market_hash_name, price, quantity=1, template_id="": {"ok": True, "template_id": "42", "quantity": quantity},
    )

    status = client.get("/api/uuyp/manual-direct/status")
    assert status.status_code == 200
    assert status.json()["ok"] is True

    control = client.post("/api/uuyp/manual-direct/control", json={"enabled": True, "paused": False})
    assert control.status_code == 200
    assert control.json()["status"]["enabled"] is True

    order = client.post(
        "/api/uuyp/purchase-order",
        json={"market_hash_name": "Test Item", "price": 1.0, "quantity": 3},
    )
    assert order.status_code == 200
    assert order.json()["ok"] is True
