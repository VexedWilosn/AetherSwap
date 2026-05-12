import uuyp.buyer as uuyp_buyer
import pytest
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


def test_change_price_includes_sessionid_from_device_token(monkeypatch):
    buyer = UuypBuyer(cookie_str={"deviceId": "device-1", "DeviceToken": "session-1"})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"Code": 0, "Msg": "ok", "Data": {"Commoditys": [{"IsSuccess": 1}]}}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.change_price({"123": 45.67})

    assert result["success"] is True
    assert calls[0][2]["json"]["Sessionid"] == "session-1"


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


def test_select_best_listing_uses_pc_market_headers(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {
            "Code": 0,
            "Msg": "success",
            "Data": [
                {"commodityNo": "expensive", "price": "0.04", "commodityHashName": "P250 | Copper Oxide (Field-Tested)"},
                {"commodityNo": "cheap", "price": "0.02", "commodityHashName": "P250 | Copper Oxide (Field-Tested)"},
            ],
        }

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.select_best_listing("110797", max_price=0.02)

    assert result["_selected_commodity_no"] == "cheap"
    assert result["_selected_price"] == 0.02
    headers = calls[0][2]["headers"]
    assert headers["platform"] == "pc"
    assert headers["App-Version"] == "5.26.0"
    assert "templateId=110797" in headers["Referer"]
    assert "Chrome/" in headers["User-Agent"]
    assert calls[0][2]["uk_verify"] is True
    assert calls[0][2]["pc_platform"] is True


def test_select_best_listing_records_business_errors(monkeypatch):
    buyer = UuypBuyer(cookie_str={})

    def fake_request(method, url, **kwargs):
        return {"Code": 85100, "Msg": "当前app版本过低，请前往更新", "status_code": 429}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.select_best_listing("110797", max_price=0.02)

    assert result is None
    assert buyer.last_select_listing_error["code"] == 85100
    assert buyer.last_select_listing_error["status_code"] == 429


def test_pc_request_refreshes_dynamic_uk(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    observed = {}

    monkeypatch.setattr(uuyp_buyer, "_fetch_uuyp_uk", lambda headers=None: "fresh-uk")

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"Code": 0, "Msg": "ok"}

    def fake_request(method, url, **kwargs):
        observed["headers"] = kwargs["headers"]
        return FakeResponse()

    monkeypatch.setattr(buyer.session, "request", fake_request)

    result = buyer._request("POST", "https://api.youpin898.com/api/test", json={}, pc_platform=True, uk_verify=True)

    assert result["Code"] == 0
    assert observed["headers"]["platform"] == "pc"
    assert observed["headers"]["uk"] == "fresh-uk"


def test_template_purchase_order_pc_uses_pc_uk_flow(monkeypatch):
    buyer = UuypBuyer(cookie_str={})
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"Code": 0, "Msg": "ok", "Data": {"list": []}}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.get_template_purchase_order_pc("110797")

    assert result["Code"] == 0
    assert calls[0][2]["uk_verify"] is True
    assert calls[0][2]["pc_platform"] is True
    assert calls[0][2]["headers"]["platform"] == "pc"


def test_buy_listing_reports_uuyp_direct_buy_unsupported(monkeypatch):
    buyer = UuypBuyer(cookie_str={})

    def fake_request(method, url, **kwargs):
        return {"Code": 84004, "Msg": "Not Found"}

    monkeypatch.setattr(buyer, "_request", fake_request)

    result = buyer.buy_listing("1962138329", 0.02)

    assert result["success"] is False
    assert result["reason"] == "direct_buy_unsupported"
    assert "purchase_order" in result["msg"]


def test_search_item_id_by_name_uses_local_uuyp_mapper(monkeypatch):
    buyer = UuypBuyer(cookie_str={})

    def fail_request(*args, **kwargs):
        pytest.fail("local UUYP mapper should avoid the removed remote search endpoint")

    monkeypatch.setattr(buyer, "_request", fail_request)

    assert buyer.search_item_id_by_name("  p250 | copper oxide (field-tested)  ") == "110797"


def test_header_like_uuyp_credentials_are_promoted_from_cookie_blob():
    buyer = UuypBuyer(
        cookie_str={
            "authorization": "token-1",
            "DeviceId": "device-1",
            "DeviceToken": "device-token-1",
            "Sessionid": "session-1",
            "uk": "uk-1",
        }
    )

    assert buyer.headers["Authorization"] == "Bearer token-1"
    assert buyer.headers["deviceId"] == "device-1"
    assert buyer.headers["DeviceToken"] == "device-token-1"
    assert buyer.headers["Sessionid"] == "session-1"
    assert buyer.headers["uk"] == "uk-1"
    assert "DeviceToken" not in buyer.cookies_dict
