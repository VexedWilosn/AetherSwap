import pytest

from buff.buyer import API_HISTORY, API_SELL_ORDER_CANCEL, API_SELL_ORDER_CHANGE, BuffAuthExpired, BuffBuyer


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
        state = kwargs["params"]["state"]
        if state == "success":
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
        return {"code": "OK", "data": {"items": []}}

    monkeypatch.setattr(buyer, "_make_request", fake_make_request)
    monkeypatch.setattr(buyer, "_fetch_pay_url", lambda *args, **kwargs: pytest.fail("payment URL fetch should not run"))
    monkeypatch.setattr(buyer, "_fetch_wechat_url", lambda *args, **kwargs: pytest.fail("wechat payment URL fetch should not run"))

    result = buyer.query_order_status(order_nums=["buff-order-1"], game="csgo")

    assert result["success"] is True
    assert result["data"]["id"] == "buff-order-1"
    assert result["data"]["order_status"] == "success"
    assert all(call[0] == "GET" and call[1] == API_HISTORY for call in calls)


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
