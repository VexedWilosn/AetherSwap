import json

import pytest

from app.services.platform_sessions import resolve_buff_pay_method
from buff.buyer import API_ASK_SELLER_SEND, API_BUY_ORDER_CREATE, API_HISTORY, API_SELL_ORDER_CANCEL, API_SELL_ORDER_CHANGE, BuffAuthExpired, BuffBuyer, PAY_METHOD_WALLET


def test_check_wait_pay_orders_propagates_auth_expired(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=expired")

    def raise_auth(*args, **kwargs):
        raise BuffAuthExpired()

    monkeypatch.setattr(buyer, "_make_request", raise_auth)

    with pytest.raises(BuffAuthExpired):
        buyer.check_wait_pay_orders()


def test_query_order_status_matches_history_without_fetching_payment(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert "state" not in kwargs["params"]
        return {
            "code": "OK",
            "data": {
                "items": [
                    {
                        "id": "buff-order-1",
                        "state": "success",
                        "price": "100.00",
                    }
                ]
            },
        }

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)
    monkeypatch.setattr(buyer, "_fetch_pay_url", lambda *args, **kwargs: pytest.fail("payment URL fetch should not run"))
    monkeypatch.setattr(buyer, "_fetch_wechat_url", lambda *args, **kwargs: pytest.fail("wechat payment URL fetch should not run"))

    result = buyer.query_order_status(order_nums=["buff-order-1"], game="csgo")

    assert result["success"] is True
    assert result["data"]["id"] == "buff-order-1"
    assert result["data"]["order_status"] == "success"
    assert all(call[0] == "GET" and call[1] == API_HISTORY for call in calls)


def test_query_order_status_returns_fail_state_from_unfiltered_history(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")

    def fake_make_request(method, url, **kwargs):
        return {
            "code": "OK",
            "data": {
                "items": [
                    {
                        "id": "buff-order-2",
                        "state": "FAIL",
                        "error_text": "买家取消",
                    }
                ]
            },
        }

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.query_order_status(order_nums=["buff-order-2"], game="csgo")

    assert result["success"] is True
    assert result["data"]["id"] == "buff-order-2"
    assert result["data"]["state"] == "FAIL"
    assert result["data"]["order_status"] == "FAIL"


def test_query_order_status_not_found_stays_pending(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")

    def fake_make_request(method, url, **kwargs):
        return {"code": "OK", "data": {"items": []}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.query_order_status(order_nums=["missing-order"], game="csgo")

    assert result["success"] is True
    assert result["data"][0]["order_id"] == "missing-order"
    assert result["data"][0]["order_status"] == "pending"
    assert result["data"][0]["not_found_in_recent_history"] is True


def test_wallet_pay_method_alias_resolves_to_buff_wallet():
    assert resolve_buff_pay_method("wallet") == PAY_METHOD_WALLET
    assert resolve_buff_pay_method("platform_wallet") == PAY_METHOD_WALLET


def test_create_buy_order_uses_wallet_pay_method(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok", pay_method=PAY_METHOD_WALLET)
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"id": "wallet-order-1"}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.create_buy_order(goods_id=1116886, price=0.02, num=1)

    payload = json.loads(calls[0][2]["data"])
    assert result["success"] is True
    assert calls[0][0] == "POST"
    assert calls[0][1] == API_BUY_ORDER_CREATE
    assert payload["pay_method"] == PAY_METHOD_WALLET


def test_direct_buy_locks_lowest_sell_order_at_target(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok", pay_method=PAY_METHOD_WALLET)
    locked = []

    monkeypatch.setattr(
        buyer,
        "get_sell_orders",
        lambda goods_id, game="csgo": [
            {"id": "sell-order-1", "price": "0.02"},
            {"id": "sell-order-2", "price": "0.03"},
        ],
    )

    def fake_lock(game, goods_id, sell_order_id, price):
        locked.append((game, goods_id, sell_order_id, price))
        return {"success": True, "order_id": "bill-1", "pay_type": "wallet", "pay_url": None}

    monkeypatch.setattr(buyer, "lock_and_get_pay_url", fake_lock)
    monkeypatch.setattr("buff.buyer.jittered_sleep", lambda *_args, **_kwargs: None)

    result = buyer.direct_buy(goods_id=1116886, price=0.02, num=1)

    assert result["success"] is True
    assert result["order_id"] == "bill-1"
    assert result["sell_order_id"] == "sell-order-1"
    assert result["filled_quantity"] == 0
    assert result["remaining_quantity"] == 1
    assert result["remaining_amount_cny"] == 0.02
    assert locked == [("csgo", 1116886, "sell-order-1", "0.02")]


def test_direct_buy_rejects_sell_order_above_target(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok", pay_method=PAY_METHOD_WALLET)
    monkeypatch.setattr(
        buyer,
        "get_sell_orders",
        lambda goods_id, game="csgo": [{"id": "sell-order-1", "price": "0.03"}],
    )
    monkeypatch.setattr(buyer, "lock_and_get_pay_url", lambda *args, **kwargs: pytest.fail("should not lock above target"))

    result = buyer.direct_buy(goods_id=1116886, price=0.02, num=1)

    assert result["success"] is False
    assert result["reason"] == "not_found"


def test_ask_seller_to_send_returns_structured_result(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok", pay_method=PAY_METHOD_WALLET)
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"ok": True}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.ask_seller_to_send("bill-1", game="csgo")

    payload = json.loads(calls[0][2]["data"])
    assert result["success"] is True
    assert result["data"]["requested"] == ["bill-1"]
    assert result["data"]["order_status"] == "wait_buyer_confirm"
    assert calls[0][0] == "POST"
    assert calls[0][1] == API_ASK_SELLER_SEND
    assert payload["bill_orders"] == ["bill-1"]


def test_cancel_sale_batches_and_normalizes_success(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        payload = __import__("json").loads(kwargs["data"])
        return {
            "code": "OK",
            "data": {order_id: "OK" for order_id in payload["sell_orders"]},
        }

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.cancel_sale(["sell-order-1", "sell-order-2"], game="csgo")

    assert result["success"] is True
    assert result["data"]["cancelled"] == ["sell-order-1", "sell-order-2"]
    assert result["data"]["order_status"] == "cancelled"
    assert calls[0][0] == "POST"
    assert calls[0][1] == API_SELL_ORDER_CANCEL


def test_cancel_sale_partial_failure_keeps_pending(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")

    def fake_make_request(method, url, **kwargs):
        return {"code": "OK", "data": {"sell-order-1": "OK", "sell-order-2": "not_found"}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.cancel_sale(["sell-order-1", "sell-order-2"], game="csgo")

    assert result["success"] is False
    assert result["data"]["cancelled"] == ["sell-order-1"]
    assert result["data"]["problems"] == {"sell-order-2": "not_found"}
    assert result["data"]["order_status"] == "pending"


def test_change_price_normalizes_success(monkeypatch):
    buyer = BuffBuyer(cookie_str="session=ok")
    calls = []

    def fake_make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"code": "OK", "data": {"sell-order-1": "OK"}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)

    result = buyer.change_price([{"sell_order_id": "sell-order-1", "price": 123.45}], game="csgo")

    assert result["success"] is True
    assert result["data"]["changed"] == ["sell-order-1"]
    assert result["data"]["order_status"] == "reprice_submitted"
    assert calls[0][0] == "POST"
    assert calls[0][1] == API_SELL_ORDER_CHANGE
