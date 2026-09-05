import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_新版订单簿支持INR转CNY(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"amtMinSellOrder\\\\\\":6091,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2,6516,4]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {"INR": 0.0709})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert error is None
    assert result["lowest_price"] == 4.32
    assert result["sell_orders"] == [(4.32, 2), (4.62, 4)]


def test_新版订单簿缺少非CNY汇率会报清晰原因(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert result is None
    assert "INR" in error
    assert "exchange_rate.json" in error


def test_汇率文件里的Steam市场币种都有ECurrency映射():
    from steam import market_orders

    rate_codes = {
        "USD", "INR", "RUB", "HKD", "EUR", "KZT", "UAH", "TRY", "ARS",
        "VND", "IDR", "BRL", "CLP", "JPY", "PHP",
    }
    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert rate_codes <= steam_codes


def test_汇率文件包含的非Steam本地市场币种不会误映射():
    from steam import market_orders

    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert "PKR" not in steam_codes
    assert "AZN" not in steam_codes


def test_新版订单簿按querykey精确匹配请求的market_hash_name():
    import json

    from steam import market_orders

    ctx = {
        "queryData": json.dumps(
            {
                "queries": [
                    {
                        "queryKey": ["market", "orderbook", 730, "AK-47 | Redline (Factory New)"],
                        "state": {
                            "data": {
                                "eCurrency": 23,
                                "amtMinSellOrder": 999999,
                                "rgCompactSellOrders": [999999, 1],
                            }
                        },
                    },
                    {
                        "queryKey": ["market", "orderbook", 730, "AK-47 | Redline (Minimal Wear)"],
                        "state": {
                            "data": {
                                "eCurrency": 23,
                                "amtMinSellOrder": 158684,
                                "rgCompactSellOrders": [158684, 1, 158888, 1],
                            }
                        },
                    },
                ]
            }
        ),
        "localizationSettings": {},
    }
    html = f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"

    result, error = market_orders._extract_ssr_orderbook_cny(
        html,
        market_hash_name="AK-47 | Redline (Minimal Wear)",
    )

    assert error is None
    assert result["lowest_price"] == 1586.84
    assert result["sell_orders"] == [(1586.84, 1), (1588.88, 1)]


def test_get_sell_orders_cny_优先使用新版ssr而不是旧item_nameid缓存(monkeypatch):
    from steam import market_orders

    market_orders.clear_caches()
    monkeypatch.setattr(market_orders, "db_get_item_nameid", lambda name: "stale-id")
    monkeypatch.setattr(
        market_orders,
        "_fetch_ssr_sell_orders_cny",
        lambda *args, **kwargs: ({"lowest_price": 12.34, "sell_orders": [(12.34, 2)]}, None),
    )
    monkeypatch.setattr(
        market_orders,
        "fetch_item_orders_histogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("histogram should not be used")),
    )

    result = market_orders.get_sell_orders_cny(object(), "AK-47 | Redline (Minimal Wear)", use_cache=False)

    assert result == {"lowest_price": 12.34, "sell_orders": [(12.34, 2)]}


def test_get_sell_orders_cny_聚合页会回退到带筛选的分组页面(monkeypatch):
    import json

    from steam import market_orders

    def _make_html(queries):
        ctx = {
            "queryData": json.dumps({"queries": queries}),
            "localizationSettings": {},
        }
        return f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"

    first_html = _make_html(
        [
            {
                "queryKey": ["market", "description", 730, "MP7 | Just Smile (Minimal Wear)"],
                "state": {
                    "data": {
                        "market_hash_name": "MP7 | Just Smile (Minimal Wear)",
                        "market_bucket_group_id": "G1821208B093004",
                        "descriptions": [
                            {"name": "exterior_wear", "value": "Exterior: Minimal Wear"},
                        ],
                    }
                },
            },
            {
                "queryKey": ["market", "orderbook", 730, "MP7 | Just Smile (Factory New)"],
                "state": {
                    "data": {
                        "eCurrency": 23,
                        "amtMinSellOrder": 4983,
                        "rgCompactSellOrders": [4983, 1],
                    }
                },
            },
        ]
    )
    second_html = _make_html(
        [
            {
                "queryKey": ["market", "orderbook", 730, "MP7 | Just Smile (Minimal Wear)"],
                "state": {
                    "data": {
                        "eCurrency": 23,
                        "amtMinSellOrder": 990,
                        "rgCompactSellOrders": [990, 2, 1001, 1],
                    }
                },
            },
        ]
    )

    requested_urls = []

    class DummyResponse:
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    class DummySession:
        def get(self, url, headers=None, timeout=None, proxies=None, allow_redirects=True):
            requested_urls.append(url)
            if "category_730_Exterior=tag_WearCategory1" in url:
                return DummyResponse(second_html)
            return DummyResponse(first_html)

    monkeypatch.setattr(market_orders, "get_proxy_manager", lambda: type("PM", (), {"get_proxies_for_request": lambda self, failed=False: None})())

    result, error = market_orders._fetch_ssr_sell_orders_cny(
        DummySession(),
        "MP7 | Just Smile (Minimal Wear)",
        730,
    )

    assert error is None
    assert result["lowest_price"] == 9.9
    assert result["sell_orders"] == [(9.9, 2), (10.01, 1)]
    assert any("G1821208B093004" in url for url in requested_urls)
    assert any("category_730_Exterior=tag_WearCategory1" in url for url in requested_urls)


def test_构造分组筛选页优先使用描述里的内部tag():
    import json

    from steam import market_orders

    ctx = {
        "queryData": json.dumps(
            {
                "queries": [
                    {
                        "queryKey": ["market", "description", 730, "MP7 | Just Smile (Minimal Wear)"],
                        "state": {
                            "data": {
                                "market_hash_name": "MP7 | Just Smile (Minimal Wear)",
                                "market_bucket_group_id": "G1821208B093004",
                                "descriptions": [
                                    {"name": "exterior_wear", "value": "Exterior: 略有磨损"},
                                ],
                                "tags": [
                                    {"internal_name": "tag_WearCategory1"},
                                    {"internal_name": "tag_normal"},
                                ],
                            }
                        },
                    }
                ]
            }
        ),
        "localizationSettings": {},
    }
    html = f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"

    url = market_orders._build_filtered_group_listing_url(
        html,
        "MP7 | Just Smile (Minimal Wear)",
        730,
    )

    assert "G1821208B093004" in url
    assert "category_730_Exterior=tag_WearCategory1" in url
    assert "category_730_Quality=tag_normal" in url


def test_构造分组筛选页能识别星号stattrak品质():
    from steam import market_orders

    assert market_orders._infer_cs2_quality_filter_tag("★ StatTrak™ Bayonet | Night (Field-Tested)") == "tag_strange"
    assert market_orders._infer_cs2_quality_filter_tag("★ Bayonet | Night (Field-Tested)") == "tag_unusual"


def test_新版_orderbook_action_按精确名称拉取目标变体(monkeypatch):
    import json

    from steam import market_orders

    target = "Glock-18 | Umbral Rabbit (Battle-Scarred)"
    requested = {}

    class DummyResponse:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": {
                    "eCurrency": 23,
                    "amtMinSellOrder": 890,
                    "rgCompactSellOrders": [890, 1, 900, 2, 930, 4],
                },
            }

    class DummySession:
        def get(self, url, params=None, headers=None, timeout=None, proxies=None, allow_redirects=True):
            requested["url"] = url
            requested["params"] = params
            requested["headers"] = headers or {}
            return DummyResponse()

    monkeypatch.setattr(
        market_orders,
        "get_proxy_manager",
        lambda: type("PM", (), {"get_proxies_for_request": lambda self, failed=False: None})(),
    )

    result, error = market_orders._fetch_action_orderbook_cny(DummySession(), target, 730)

    assert error is None
    assert result == {"lowest_price": 8.9, "sell_orders": [(8.9, 1), (9.0, 2), (9.3, 4)]}
    assert requested["url"] == "https://steamcommunity.com/market/orderbook"
    assert requested["params"]["q"] == "Load"
    assert json.loads(requested["params"]["qp"]) == [730, target]
    assert requested["headers"]["x-valve-request-type"] == "queryAction"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "data": {
                "success": True,
                "data": {
                    "eCurrency": 23,
                    "amtMinSellOrder": 340,
                    "rgCompactSellOrders": [340, 2, 351, 4],
                },
            }
        },
        {
            "data": {
                "eCurrency": 23,
                "amtMinSellOrder": 340,
                "rgCompactSellOrders": [340, 2, 351, 4],
            }
        },
    ],
    ids=["double-envelope", "data-without-success"],
)
def test_get_sell_orders_cny_兼容新版_orderbook_响应格式(monkeypatch, payload):
    from steam import market_orders

    target = "Sawed-Off | Analog Input (Minimal Wear)"

    class DummyResponse:
        status_code = 200
        text = ""

        def __init__(self, payload=None):
            self._payload = payload

        def json(self):
            return self._payload

    class DummySession:
        def get(
            self,
            url,
            params=None,
            headers=None,
            timeout=None,
            proxies=None,
            allow_redirects=True,
        ):
            if url.endswith("/market/orderbook"):
                return DummyResponse(payload)
            return DummyResponse()

    monkeypatch.setattr(
        market_orders,
        "get_proxy_manager",
        lambda: type("PM", (), {"get_proxies_for_request": lambda self, failed=False: None})(),
    )

    result, error = market_orders.get_sell_orders_cny(
        DummySession(),
        target,
        use_cache=False,
        return_error=True,
    )

    assert error is None
    assert result == {"lowest_price": 3.4, "sell_orders": [(3.4, 2), (3.51, 4)]}


def test_get_sell_orders_cny_ssr预取错变体时回退新版_orderbook_action(monkeypatch):
    from steam import market_orders

    market_orders.clear_caches()
    monkeypatch.setattr(
        market_orders,
        "_fetch_ssr_sell_orders_cny",
        lambda *args, **kwargs: (None, "Steam 新版页面未预取目标变体订单簿"),
    )
    monkeypatch.setattr(
        market_orders,
        "_fetch_action_orderbook_cny",
        lambda *args, **kwargs: ({"lowest_price": 8.9, "sell_orders": [(8.9, 1)]}, None),
    )
    monkeypatch.setattr(
        market_orders,
        "get_item_nameid",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("item_nameid should not be used")),
    )

    result = market_orders.get_sell_orders_cny(
        object(),
        "Glock-18 | Umbral Rabbit (Battle-Scarred)",
        use_cache=False,
    )

    assert result == {"lowest_price": 8.9, "sell_orders": [(8.9, 1)]}
