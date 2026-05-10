from app.config_schema import DEFAULTS, validate_and_fill


def test_trading_worker_config_defaults_disabled_safe_mode():
    result = validate_and_fill({}, DEFAULTS)

    assert result["trading_worker"]["enabled"] is False
    assert result["trading_worker"]["safe_mode"] is True
    assert result["trading_worker"]["batch_size"] == 10


def test_seller_snapshot_scanner_config_defaults_disabled_dry_run():
    result = validate_and_fill({}, DEFAULTS)

    assert result["seller_snapshot_scanner"]["enabled"] is False
    assert result["seller_snapshot_scanner"]["commit"] is False
    assert result["seller_snapshot_scanner"]["interval_seconds"] == 3600
    assert result["seller_snapshot_scanner"]["listing_platform"] == "steam"
    assert result["seller_snapshot_scanner"]["delivery_platform"] == "c5game"
