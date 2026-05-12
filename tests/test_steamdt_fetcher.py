from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from DataEngine.steamdt_fetcher import (
    DEFAULT_STRATEGIES,
    _opportunity_rows,
    _prepare_capsule_request,
    extract_uuyp_template_id_from_steamdt_row,
    get_uuyp_id_from_steamdt,
    normalize_steamdt_platform_rows,
    normalize_steamdt_row,
    parse_relative_time,
)


def _item() -> SimpleNamespace:
    return SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)", cn_name="AK-47 | Slate")


def _sample_row() -> dict:
    return {
        "marketHashName": "AK-47 | Slate (Field-Tested)",
        "steamPrice": 12.34,
        "transactionCount": "149",
        "steamUpdateTime": "1777966021",
        "platformUpdateTime": 1777967702,
        "platformList": [
            {"platformEnum": "STEAM", "price": 12.34, "sellNum": 12, "biddingPrice": 11.5, "biddingCount": 5},
            {"platformEnum": "BUFF", "price": 10.1, "sellNum": 88, "biddingPrice": 9.9, "biddingCount": 7},
            {"platformEnum": "YOUPIN", "price": 10.2, "sellNum": 77, "biddingPrice": 9.8, "biddingCount": 6},
            {"platformEnum": "C5", "price": 10.3, "sellNum": 66, "biddingPrice": 9.7, "biddingCount": 5},
        ],
    }


def test_parse_numeric_string_timestamp_as_epoch_seconds():
    parsed = parse_relative_time("1777966021")
    assert parsed == datetime.fromtimestamp(1777966021)


def test_normalize_steamdt_row_uses_transaction_count_and_steam_time():
    row = normalize_steamdt_row(_sample_row(), _item())
    assert row["platform_name"] == "steam"
    assert row["sell_min"] == 12.34
    assert row["buy_max"] == 11.5
    assert row["volume"] == 149
    assert row["sell_volume"] == 12
    assert row["buy_volume"] == 5
    assert row["new_timestamp"] == datetime.fromtimestamp(1777966021)
    assert row["quote_quality"] == "high"


def test_normalize_steamdt_platform_rows_splits_supported_platforms_only():
    rows = normalize_steamdt_platform_rows(_sample_row(), _item())
    by_platform = {row["platform_name"]: row for row in rows}
    assert set(by_platform) == {"steam", "buff", "uuyp"}
    assert by_platform["buff"]["sell_min"] == 10.1
    assert by_platform["buff"]["buy_max"] == 9.9
    assert by_platform["buff"]["volume"] == 88
    assert by_platform["buff"]["sell_volume"] == 88
    assert by_platform["buff"]["buy_volume"] == 7
    assert by_platform["uuyp"]["sell_min"] == 10.2
    assert "c5" not in by_platform


def test_non_cny_price_marker_is_rejected_before_db_write():
    row = _sample_row()
    row["steamPrice"] = "$12.34"
    normalized = normalize_steamdt_row(row, _item())
    assert normalized["source_currency_state"] == "NON_CNY"
    assert normalized["quote_quality"] == "invalid_currency"


def test_strategy_opportunity_uses_steam_buy_against_platform_sell():
    strategy = next(s for s in DEFAULT_STRATEGIES if s.name == "platform_cash_steam_buy_platform_sell")
    rows = _opportunity_rows(_sample_row(), _item(), strategy)
    by_platform = {row["platform_name"]: row for row in rows}
    assert round(by_platform["buff"]["profit_rate"], 4) == round((11.5 - 10.1) / 10.1 * 100, 4)
    assert by_platform["buff"]["transaction_count_24h"] == 149


def test_extract_uuyp_template_id_from_steamdt_row_reads_youpin_link_url():
    row = _sample_row()
    row["platformList"][2]["linkUrl"] = "https://www.youpin898.com/market/goods-list?listType=10&templateId=1397&gameId=730"
    assert extract_uuyp_template_id_from_steamdt_row(row) == "1397"


def test_get_uuyp_id_from_steamdt_matches_market_hash_name(monkeypatch):
    sample_rows = [
        {
            "marketHashName": "StatTrak™ Nova | Wood Fired (Battle-Scarred)",
            "platformList": [
                {"platformEnum": "YOUPIN", "linkUrl": "https://www.youpin898.com/market/goods-list?listType=10&templateId=43520&gameId=730"}
            ],
        },
        {
            "marketHashName": "Nova | Wood Fired (Battle-Scarred)",
            "platformList": [
                {"platformEnum": "YOUPIN", "linkUrl": "https://www.youpin898.com/market/goods-list?listType=10&templateId=1397&gameId=730"}
            ],
        },
    ]

    monkeypatch.setattr("DataEngine.steamdt_fetcher._load_config", lambda: {
        "enabled": False,
        "endpoint": "https://www.steamdt.com/api/user/ranking/v1/hanging-knife",
        "timeout": 15,
        "page_size": 200,
        "max_pages": 1,
        "min_sell_price": "1",
        "max_sell_price": "10",
        "min_transaction_count": "100",
        "platform_list": ["YOUPIN", "BUFF"],
        "type": "swap",
        "cooldown_seconds_on_waf": 300,
        "sleep_min_seconds": 0.0,
        "sleep_max_seconds": 0.0,
        "use_proxy": False,
        "device_id": "test-device",
        "cookie": "",
        "min_volume_for_high_quality": 1,
        "strategies": None,
        "capsules_enabled": False,
        "capsule_lease_ttl_seconds": 45,
        "capsule_timeout_cooldown_seconds": 60,
        "capsule_empty_cooldown_seconds": 120,
        "capsule_waf_cooldown_seconds": 300,
        "capsule_auth_cooldown_seconds": 1800,
        "capsule_auto_retire_after": 3,
        "capsule_auto_retire_reasons": {"waf_block", "empty_soft_block"},
        "capsule_min_ready": 1,
        "capsule_alert_interval_seconds": 3600,
    })
    monkeypatch.setattr("DataEngine.steamdt_fetcher._load_waf_cooldown_until", lambda: 0.0)
    monkeypatch.setattr("DataEngine.steamdt_fetcher._fallback_capsule", lambda cfg: SimpleNamespace(capsule_id="steamdt-fallback", device_id="test-device", cookie_header="", headers={}, user_agent="", proxy_binding="direct"))
    monkeypatch.setattr("DataEngine.steamdt_fetcher._prepare_capsule_request", lambda capsule, cfg: ({}, None))
    monkeypatch.setattr("DataEngine.steamdt_fetcher._load_strategies", lambda cfg: [DEFAULT_STRATEGIES[0]])
    monkeypatch.setattr("DataEngine.steamdt_fetcher.raise_if_stop_requested", lambda: None)
    monkeypatch.setattr("DataEngine.steamdt_fetcher._maybe_alert_recapture_needed", lambda pool, cfg: None)

    class FakeResponse:
        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {"success": True, "data": {"list": sample_rows}}

    monkeypatch.setattr("DataEngine.steamdt_fetcher.requests.post", lambda *args, **kwargs: FakeResponse())

    result = get_uuyp_id_from_steamdt("Nova | Wood Fired (Battle-Scarred)")

    assert result == "1397"


def test_prepare_capsule_request_uses_global_proxy_pool(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "DataEngine.steamdt_fetcher.get_request_proxies",
        lambda **kwargs: calls.append(kwargs) or {"http": "http://user:pass@global:80/", "https": "http://user:pass@global:80/"},
    )

    capsule = SimpleNamespace(
        device_id="test-device",
        cookie_header="SDT_DeviceId=test-device",
        headers={},
        user_agent="Mozilla/5.0",
        proxy_binding="pool",
    )
    cfg = {"device_id": "test-device", "cookie": "", "use_proxy": False}

    headers, proxies = _prepare_capsule_request(capsule, cfg)

    assert headers["x-device-id"] == "test-device"
    assert proxies["http"].startswith("http://user:pass@global:80")
    assert calls == [{"force": True, "platform": "global"}]
