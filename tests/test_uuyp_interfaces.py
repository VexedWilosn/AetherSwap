import asyncio


def test_uuyp_public_monitor_refreshes_dynamic_uk(monkeypatch):
    from DataEngine import uuyp_public_monitor
    import uuyp.buyer as buyer_module

    monkeypatch.setattr(uuyp_public_monitor, "_uuyp_uk_cache_value", "")
    monkeypatch.setattr(uuyp_public_monitor, "_uuyp_uk_cache_time", 0.0)
    monkeypatch.setattr(uuyp_public_monitor, "_load_uuyp_credentials", lambda: ({}, {"uk": "stale-uk"}))
    monkeypatch.setattr(buyer_module, "_fetch_uuyp_uk", lambda headers=None: "fresh-uk")

    headers, cookies = uuyp_public_monitor._build_uuyp_request_context("110797")

    assert headers["platform"] == "pc"
    assert headers["uk"] == "fresh-uk"
    assert "templateId=110797" in headers["Referer"]
    assert cookies["currency"] == "CNY"


def test_uuyp_sniper_uses_pc_sale_list_endpoint(monkeypatch):
    from DataEngine import sniper_manager
    from DataEngine import uuyp_public_monitor

    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "Code": 0,
                "TotalCount": 2,
                "Data": [
                    {"commodityNo": "expensive", "price": "0.04"},
                    {"commodityNo": "cheap", "price": "0.02"},
                ],
            }

    class FakeSession:
        async def post(self, url, headers=None, cookies=None, json=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["cookies"] = cookies
            captured["json"] = json
            captured["timeout"] = timeout
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(sniper_manager, "DEFAULT_RANDOM_SLEEP_MIN", 0)
    monkeypatch.setattr(sniper_manager, "DEFAULT_RANDOM_SLEEP_MAX", 0)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "_build_uuyp_request_context",
        lambda template_id: (
            {
                "platform": "pc",
                "Referer": f"https://www.youpin898.com/market/goods-list?listType=10&templateId={template_id}&gameId=730",
                "uk": "fresh-uk",
            },
            {"currency": "CNY"},
        ),
    )
    sniper = sniper_manager.UuypSniper(session=FakeSession(), semaphore=asyncio.Semaphore(1))

    result = asyncio.run(sniper.fetch_price("110797"))

    assert captured["url"].endswith("/api/homepage/pc/goods/market/queryOnSaleCommodityList")
    assert captured["headers"]["platform"] == "pc"
    assert captured["headers"]["uk"] == "fresh-uk"
    assert "templateId=110797" in captured["headers"]["Referer"]
    assert captured["cookies"] == {"currency": "CNY"}
    assert captured["kwargs"]["default_headers"] is True
    assert captured["json"]["templateId"] == "110797"
    assert result == {"sell_min": 0.02, "buy_max": 0.0, "sell_volume": 2, "buy_volume": 0}
