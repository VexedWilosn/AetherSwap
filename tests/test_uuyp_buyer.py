from uuyp.buyer import UuypBuyer


def test_query_order_status_matches_template_purchase_order(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    calls = []

    def fake_get_template_purchase_order_pc(**kwargs):
        calls.append(kwargs)
        return {
            "Code": 0,
            "Msg": "ok",
            "Data": {
                "list": [
                    {"OrderNo": "other-order", "orderStatus": "pending"},
                    {"OrderNo": "uuyp-order-1", "orderStatus": "completed"},
                ]
            },
        }

    monkeypatch.setattr(buyer, "get_template_purchase_order_pc", fake_get_template_purchase_order_pc)

    result = buyer.query_order_status(order_nums=["uuyp-order-1"], template_id="tpl-1")

    assert result["success"] is True
    assert result["data"]["OrderNo"] == "uuyp-order-1"
    assert result["data"]["orderStatus"] == "completed"
    assert calls == [{"template_id": "tpl-1", "page_index": 1, "page_size": 30}]


def test_query_order_status_missing_template_id_stays_pending():
    buyer = UuypBuyer(cookie_str={})

    result = buyer.query_order_status(order_nums=["uuyp-order-1"])

    assert result["success"] is True
    assert result["data"][0]["OrderNo"] == "uuyp-order-1"
    assert result["data"][0]["orderStatus"] == "pending"
    assert result["data"][0]["missing_template_id"] is True


def test_query_order_status_auth_or_risk_error(monkeypatch):
    buyer = UuypBuyer(cookie_str={})

    def fake_get_template_purchase_order_pc(**kwargs):
        return {"Code": 401, "Msg": "login expired"}

    monkeypatch.setattr(buyer, "get_template_purchase_order_pc", fake_get_template_purchase_order_pc)

    result = buyer.query_order_status(order_nums=["uuyp-order-1"], template_id="tpl-1")

    assert result["success"] is False
    assert result["auth_required"] is True


def test_change_price_uses_price_change_api(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"Code": 0, "Msg": "ok", "Data": {"Commoditys": [{"IsSuccess": 1}]}}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.change_price({"123": 45.67})

    assert result["success"] is True
    assert result["data"]["changed"] == ["123"]
    assert result["data"]["order_status"] == "reprice_submitted"
    assert calls[0][0] == "PUT"
    assert calls[0][2]["json"]["Commoditys"][0]["CommodityId"] == 123
    assert calls[0][2]["json"]["Commoditys"][0]["Price"] == "45.67"


def test_off_shelf_uses_commodity_ids(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"Code": 0, "Msg": "ok", "Data": True}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.off_shelf(["123", "456"])

    assert result["success"] is True
    assert result["data"]["cancelled"] == ["123", "456"]
    assert result["data"]["order_status"] == "cancelled"
    assert calls[0][0] == "PUT"
    assert calls[0][2]["json"]["Ids"] == "123,456"
