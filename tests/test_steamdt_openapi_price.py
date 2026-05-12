from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from DataEngine import steamdt_openapi_price as price_mod


def test_parse_update_time_rejects_invalid_values():
    assert price_mod._parse_update_time(None) is None
    assert price_mod._parse_update_time(0) is None
    assert price_mod._parse_update_time("bad") is None
    assert price_mod._parse_update_time(1) is None


def test_parse_update_time_accepts_epoch_ms():
    ts = price_mod._parse_update_time(1_735_689_600_000)
    assert isinstance(ts, datetime)
    assert ts.year >= 2024


def test_cash_platform_bid_outlier_uses_peer_cash_bids():
    assert price_mod.cash_platform_bid_is_outlier("uuyp", 3210.0, 18.1) is True
    assert price_mod.cash_platform_bid_is_outlier("eco", 215.0, 18.1) is True
    assert price_mod.cash_platform_bid_is_outlier("uuyp", 1930.0, 791.0) is True


def test_cash_platform_bid_outlier_keeps_plausible_bid_and_ignores_steam():
    assert price_mod.cash_platform_bid_is_outlier("buff", 101.0, 90.0) is False
    assert price_mod.cash_platform_bid_is_outlier("buff", 95.0, 90.0) is False
    assert price_mod.cash_platform_bid_is_outlier("buff", 880.0, 791.0) is False
    assert price_mod.cash_platform_bid_is_outlier("steam", 1000.0, 10.0) is False


def test_normalize_openapi_quote_ignores_prices_without_orders():
    quote = price_mod.normalize_openapi_quote_row(
        {
            "sellPrice": 10.01,
            "sellCount": 0,
            "biddingPrice": 6.3,
            "biddingCount": 3,
        }
    )

    assert quote["sell"] == 0.0
    assert quote["buy"] == 6.3
    assert quote["sell_volume"] == 0
    assert quote["buy_volume"] == 3


def test_cash_platform_bid_floor_ignores_zero_count_bids():
    rows = [
        {"platform": "YOUPIN", "biddingPrice": 999.0, "biddingCount": 0},
        {"platform": "BUFF", "biddingPrice": 20.0, "biddingCount": 4},
    ]

    assert price_mod._cash_platform_bid_floor(rows) == 20.0


def test_refresh_selected_items_does_not_spend_single_after_parsed_batch(monkeypatch):
    monkeypatch.setattr(
        price_mod,
        "_load_config",
        lambda: {
            "api_key": "test",
            "base_url": "https://open.steamdt.com",
            "timeout_seconds": 20,
            "use_proxy": False,
            "batch_rpm": 1,
            "single_rpm": 60,
            "batch_size": 100,
            "tracked_platforms": ["uuyp"],
        },
    )

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [SimpleNamespace(id=42, market_hash_name="Known Item")]

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def query(self, *args, **kwargs):
            return FakeQuery()

    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        if method == "GET":
            raise AssertionError("single fallback should not run after parsed batch")
        return {
            "success": True,
            "data": [
                {
                    "marketHashName": "Known Item",
                    "dataList": [
                        {
                            "platform": "YOUPIN",
                            "sellPrice": 1.2,
                            "sellCount": 1,
                            "biddingPrice": 1.0,
                            "biddingCount": 1,
                            "updateTime": 1_778_327_601,
                        }
                    ],
                }
            ],
        }, None

    monkeypatch.setattr(price_mod, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(price_mod, "_request_json", fake_request)
    monkeypatch.setattr(price_mod, "_save_openapi_prices", lambda items, tracked: (1, 1))

    result = price_mod.refresh_selected_items([42], platforms={"uuyp"}, urgent=True)

    assert result["batch_requests_used"] == 1
    assert result["single_requests_used"] == 0
    assert calls == ["POST"]


def test_quota_sliding_window_roundtrip(tmp_path: Path):
    old_path = price_mod.QUOTA_STATE_PATH
    try:
        price_mod.QUOTA_STATE_PATH = tmp_path / "quota.json"
        available_1 = price_mod._acquire_quota(key="single", limit_per_minute=3)
        assert available_1 == 3
        price_mod._consume_quota(key="single", consume=2)
        available_2 = price_mod._acquire_quota(key="single", limit_per_minute=3)
        assert available_2 == 1
    finally:
        price_mod.QUOTA_STATE_PATH = old_path
