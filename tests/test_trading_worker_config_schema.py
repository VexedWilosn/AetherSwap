from app.config_schema import DEFAULTS, validate_and_fill


def test_trading_worker_config_defaults_disabled_safe_mode():
    result = validate_and_fill({}, DEFAULTS)

    assert result["trading_worker"]["enabled"] is False
    assert result["trading_worker"]["safe_mode"] is True
    assert result["trading_worker"]["batch_size"] == 10


def test_trading_live_canary_defaults_disabled_with_kill_switch():
    result = validate_and_fill({}, DEFAULTS)

    canary = result["trading_live_canary"]
    assert canary["enabled"] is False
    assert canary["kill_switch"] is True
    assert canary["require_channel"] == "live_canary"
    assert canary["max_action_cny"] == 1.0
    assert canary["max_daily_cny"] == 10.0
    assert canary["allowed_platforms"] == []
    assert canary["allowed_action_types"] == []
    assert canary["allowed_item_ids"] == []
    assert canary["allowed_market_hash_names"] == []
    assert canary["require_recent_smoke_seconds"] == 900
    assert canary["require_manual_run_once"] is True
    assert canary["allow_background_worker"] is False


def test_seller_snapshot_scanner_config_defaults_disabled_dry_run():
    result = validate_and_fill({}, DEFAULTS)

    assert result["seller_snapshot_scanner"]["enabled"] is False
    assert result["seller_snapshot_scanner"]["commit"] is False
    assert result["seller_snapshot_scanner"]["interval_seconds"] == 3600
    assert result["seller_snapshot_scanner"]["listing_platform"] == "steam"
    assert result["seller_snapshot_scanner"]["delivery_platform"] == "c5game"
