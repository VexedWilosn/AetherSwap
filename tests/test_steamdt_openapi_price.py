from __future__ import annotations

import json
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


def test_load_config_enables_price_when_credentials_key_exists(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "app_config.json"
    credentials_path = tmp_path / "credentials.json"
    config_path.write_text(json.dumps({"steamdt": {"openapi_price": {}}}), encoding="utf-8")
    credentials_path.write_text(json.dumps({"steamdt_openapi": {"api_key": "test-key"}}), encoding="utf-8")

    monkeypatch.setattr(price_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(price_mod, "CREDENTIALS_PATH", credentials_path)
    monkeypatch.delenv("STEAMDT_OPENAPI_PRICE_ENABLED", raising=False)
    monkeypatch.delenv("STEAMDT_OPENAPI_API_KEY", raising=False)

    cfg = price_mod._load_config()

    assert cfg["enabled"] is True
    assert cfg["api_key"] == "test-key"


def test_platform_freshness_uses_oldest_tracked_platform():
    steam_old = datetime(2026, 5, 12, 12, 0, 0)
    buff_fresh = datetime(2026, 5, 12, 15, 30, 0)

    missing, oldest, newest = price_mod._platform_freshness(
        {"steam": steam_old, "buff": buff_fresh},
        {"steam", "buff"},
    )

    assert missing == 0
    assert oldest == steam_old
    assert newest == buff_fresh


def test_platform_freshness_does_not_pin_partial_platforms_to_epoch():
    steam_old = datetime(2026, 5, 12, 12, 0, 0)

    missing, oldest, newest = price_mod._platform_freshness(
        {"steam": steam_old},
        {"steam", "buff"},
    )

    assert missing == 1
    assert oldest == steam_old
    assert newest == steam_old


def test_due_sort_ignores_priority_and_uses_oldest_timestamp():
    rows = [
        {"item_id": 1, "crawl_priority": 3, "missing_platforms": 0, "oldest_updated_at": datetime(2026, 5, 12, 15, 0)},
        {"item_id": 2, "crawl_priority": 2, "missing_platforms": 0, "oldest_updated_at": datetime(2026, 5, 12, 10, 0)},
        {"item_id": 3, "crawl_priority": 2, "missing_platforms": 1, "oldest_updated_at": datetime.fromtimestamp(0)},
    ]

    rows.sort(key=price_mod._due_item_sort_key)

    assert [row["item_id"] for row in rows] == [3, 2, 1]


def test_select_round_items_uses_global_oldest_first_for_custom_mode():
    cfg = {"mode": "custom"}
    pool = [
        {"item_id": 1, "market_hash_name": "Pool Fresh", "oldest_updated_at": datetime(2026, 5, 12, 15, 0)},
        {"item_id": 2, "market_hash_name": "Pool Old", "oldest_updated_at": datetime(2026, 5, 12, 10, 0)},
    ]
    discovery = [
        {"item_id": 3, "market_hash_name": "Discovery Oldest", "oldest_updated_at": datetime(2026, 5, 12, 9, 0)}
    ]

    selected = price_mod._select_round_items(pool, discovery, cfg=cfg, capacity=2)

    assert [row["market_hash_name"] for row in selected] == ["Discovery Oldest", "Pool Old"]


def test_select_round_items_uses_global_oldest_first_for_idle_mode():
    cfg = {"mode": "idle"}
    pool = [{"item_id": 1, "market_hash_name": "Pool Oldest", "oldest_updated_at": datetime(2026, 5, 12, 8, 0)}]
    discovery = [{"item_id": 2, "market_hash_name": "Discovery Fresh", "oldest_updated_at": datetime(2026, 5, 12, 16, 0)}]

    selected = price_mod._select_round_items(pool, discovery, cfg=cfg, capacity=1)

    assert [row["market_hash_name"] for row in selected] == ["Pool Oldest"]


def test_checkpoint_times_override_source_timestamps(monkeypatch, tmp_path: Path):
    checkpoint_path = tmp_path / "checkpoints.json"
    checkpoint_path.write_text(
        json.dumps({"42:steam": "2026-05-12T16:00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(price_mod, "CHECKPOINT_STATE_PATH", checkpoint_path)

    state = price_mod._read_checkpoint_state()
    times = price_mod._merge_checkpoint_times({"steam": datetime(2026, 5, 12, 10, 0, 0)}, state, 42, {"steam"})
    missing, oldest, newest = price_mod._platform_freshness(times, {"steam"})

    assert missing == 0
    assert oldest == datetime(2026, 5, 12, 16, 0, 0)
    assert newest == datetime(2026, 5, 12, 16, 0, 0)


def test_checkpoint_times_do_not_override_newer_source_timestamps(monkeypatch, tmp_path: Path):
    checkpoint_path = tmp_path / "checkpoints.json"
    checkpoint_path.write_text(
        json.dumps({"42:steam": "2026-05-12T10:00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(price_mod, "CHECKPOINT_STATE_PATH", checkpoint_path)

    state = price_mod._read_checkpoint_state()
    times = price_mod._merge_checkpoint_times({"steam": datetime(2026, 5, 12, 16, 0, 0)}, state, 42, {"steam"})

    assert times["steam"] == datetime(2026, 5, 12, 16, 0, 0)


def test_run_once_requests_global_oldest_items_first(monkeypatch):
    requested_chunks: list[list[str]] = []
    cfg = {
        "enabled": True,
        "api_key": "test",
        "base_url": "https://open.steamdt.com",
        "timeout_seconds": 20,
        "batch_rpm": 1,
        "single_rpm": 0,
        "single_reserved_for_jit": 0,
        "batch_size": 10,
        "mode": "custom",
        "custom_pool_share_pct": 70,
        "use_proxy": False,
        "tracked_platforms": ["steam"],
    }

    monkeypatch.setattr(price_mod, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        price_mod,
        "_select_due_items",
        lambda _cfg: [
            {
                "item_id": 1,
                "market_hash_name": "Pool Fresh",
                "missing_platforms": 0,
                "oldest_updated_at": datetime(2026, 5, 12, 15, 0),
            },
            {
                "item_id": 2,
                "market_hash_name": "Pool Old",
                "missing_platforms": 0,
                "oldest_updated_at": datetime(2026, 5, 12, 10, 0),
            },
        ],
    )
    monkeypatch.setattr(
        price_mod,
        "_select_discovery_items",
        lambda _cfg: [
            {
                "item_id": 3,
                "market_hash_name": "Discovery Oldest",
                "missing_platforms": 0,
                "oldest_updated_at": datetime(2026, 5, 12, 9, 0),
            }
        ],
    )
    monkeypatch.setattr(price_mod, "_acquire_quota", lambda **kwargs: 1 if kwargs["key"] == "batch" else 0)
    monkeypatch.setattr(price_mod, "_consume_quota", lambda **kwargs: None)
    monkeypatch.setattr(price_mod, "_write_state", lambda payload: None)
    monkeypatch.setattr(price_mod, "_save_openapi_prices", lambda items, tracked: (0, 0))
    monkeypatch.setattr(price_mod, "raise_if_stop_requested", lambda: None)
    checked: list[tuple[list[int], set[str]]] = []
    monkeypatch.setattr(price_mod, "_mark_items_checked", lambda item_ids, platforms, **kwargs: checked.append((list(item_ids), set(platforms))))

    def fake_request(method, url, **kwargs):
        requested_chunks.append(list(kwargs["payload"]["marketHashNames"]))
        return {"success": True, "data": []}, None

    monkeypatch.setattr(price_mod, "_request_json", fake_request)

    result = price_mod.run_once()

    assert result["status"] == "no_data"
    assert requested_chunks == [["Discovery Oldest", "Pool Old", "Pool Fresh"]]
    assert len(checked) == 1
    assert set(checked[0][0]) == {1, 2, 3}
    assert checked[0][1] == {"steam"}


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
    checked: list[tuple[list[int], set[str]]] = []
    monkeypatch.setattr(price_mod, "_mark_items_checked", lambda item_ids, platforms, **kwargs: checked.append((list(item_ids), set(platforms))))

    result = price_mod.refresh_selected_items([42], platforms={"uuyp"}, urgent=True)

    assert result["batch_requests_used"] == 1
    assert result["single_requests_used"] == 0
    assert calls == ["POST"]
    assert checked == [([42], {"uuyp"})]


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
