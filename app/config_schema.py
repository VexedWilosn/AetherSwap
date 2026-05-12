from typing import Any, Optional
DEFAULTS = {
    "buff": {
        "pay_method": "alipay",
        "game": "csgo",
        "price_tolerance": 0.5,
    },
    "stability": {
        "days": 30,
        "cv_threshold": 0.05,
        "r2_threshold": 0.6,
        "min_daily_trades": 5,
        "price_percentile_ceil": 0.8,
        "r2_rising_threshold": 0.8,
        "slope_pct_ceil": 0.01,
        "ma_deviation_ceil": 1.1,
        "last_price_ma30_ceil": 1.05,
        "slope_stable_floor": -0.005,
        "price_percentile_ceil_rising": 0.5,
        "use_vwap": True,
        "request_interval_seconds": 2.5,
        "request_failure_delay_seconds": 5,
    },
    "pipeline": {
        "target_balance": 100,
        "max_discount": 0.9,
        "huge_profit_offset": 0.05,
        "exclude_keywords": ["印花"],
        "sell_price_ratio": 1.0,
        "verbose_debug": False,
        "sell_strategy": 4,
        "sell_price_offset": 0,
        "sell_price_wall_volume": 20,
        "sell_price_max_ignore_volume": 4,
        "sell_trend_days": 7,
        "retry_interval_seconds": 300,
        
        "buff_retry_delay_seconds": 5,
        "current_price_refresh_minutes": 10,
        "max_staleness_minutes": 10,
        "purchase_order_jit_bypass_minutes": 10,
        "resell_ratio": 0.85,
        "steam_balance_cost_ratio": 0.85,
        "safe_purchase_hard_qty_cap": 50,
        "safe_purchase_liquidity_ratio": 0.05,
        "safe_purchase_low_price_threshold": 5.0,
        "safe_purchase_low_price_penalty": 0.5,
        "safe_purchase_low_price_hard_cap": 30,
        "sell_pressure_orders_n": 5,
        "sell_pressure_threshold": 2.0,
        "receive_poll_interval_seconds": 30,
        "listing_check_interval_seconds": 600,
        "max_listings_per_item": 5,
        "listing_delay_seconds": 3,
        "steam_listings_debug": False,
        "start_time_limit_enabled": False,
        "start_time_hour": 8,
        "end_time_hour": 22,
    },
    "inventory": {
        "refresh_seconds": 60,
    },
    "notify": {
        "pushplus_token": "",
        "holdings_report_interval_hours": 0,
        "holdings_report_change_threshold_pct": 20,
        "holdings_report_drop_enabled": True,
        "email_user": "",
        "email_pass": "",
        "imap_server": "imap.qq.com",
        "target_sender": "",
        "subject_success": "已确认成功付款",
        "subject_fail": "已确认付款失败",
        "allowed_sender": "",
        "email_timeout_seconds": 300,
    },
    "steam_guard": {
        "shared_secret": "",
    },
    "steam_confirm": {
        "enabled": False,
        "identity_secret": "",
        "device_id": "",
    },
    "system": {
        "exchange_rate_refresh_hours": 24,
        "ui_scale": "0.7",
    },
    "proxy_pool": {
        "enabled": False,
        "strategy": 3,
        "test_url": "https://ipv4.webshare.io/",
        "timeout_seconds": 10,
        "webshare_api_key": "",
        "global_proxies": [],
        "steam_proxies": [],
        "buff_proxies": [],
        "uuyp_proxies": [],
        "proxies": [],
    },
    "steam_deals": {
        "enabled": False,
        "auto_refresh_days": 7,
        "max_game_threads": 5,
        "max_region_threads": 16,
    },
    "steamdt": {
        "enabled": False,
        "endpoint": "https://www.steamdt.com/api/user/ranking/v1/hanging-knife",
        "interval_seconds": 600,
        "timeout": 15,
        "page_size": 200,
        "max_pages": 1,
        "min_sell_price": "1",
        "max_sell_price": "10",
        "min_transaction_count": "100",
        "platform_list": ["C5", "YOUPIN", "BUFF"],
        "type": "swap",
        "cooldown_seconds_on_waf": 300,
        "sleep_min_seconds": 1.0,
        "sleep_max_seconds": 3.0,
        "use_proxy": False,
        "device_id": "",
        "cookie": "",
        "min_volume_for_high_quality": 1,
        "strategies": [],
        "openapi_enabled": False,
        "openapi_sync_interval_seconds": 86400,
        "openapi_endpoint": "",
        "openapi_base_url": "https://open.steamdt.com",
        "openapi_base_path": "/open/cs2/v1/base",
        "openapi_timeout_seconds": 20,
        "openapi_use_proxy": True,
        "openapi": {
            "enabled": False,
            "sync_interval_seconds": 86400,
            "endpoint": "",
            "base_url": "https://open.steamdt.com",
            "base_path": "/open/cs2/v1/base",
            "timeout_seconds": 20,
            "max_retries": 2,
            "use_proxy": True,
            "api_key": "",
            "tracked_platforms": ["steam", "buff", "uuyp", "eco"],
        },
        "openapi_price": {
            "enabled": True,
            "base_url": "https://open.steamdt.com",
            "timeout_seconds": 20,
            "batch_requests_per_minute": 1,
            "single_requests_per_minute": 60,
            "single_reserved_for_jit": 15,
            "batch_size": 100,
            "p2_target_minutes": 60,
            "p3_target_minutes": 30,
            "mode": "stable",
            "stable_pool_cycle_minutes": 60,
            "discovery_target_minutes": 720,
            "custom_pool_share_pct": 70,
            "auto_switch_to_stable_on_idle_complete": True,
            "use_proxy": True,
            "tracked_platforms": ["steam", "buff", "uuyp", "eco"],
            "api_key": "",
        },
    },
    "session_capsules": {
        "steamdt": {
            "enabled": True,
            "lease_ttl_seconds": 45,
            "timeout_cooldown_seconds": 60,
            "empty_soft_block_cooldown_seconds": 120,
            "waf_block_cooldown_seconds": 300,
            "auth_invalid_cooldown_seconds": 1800,
            "auto_retire_after": 3,
            "auto_retire_reasons": ["waf_block", "empty_soft_block"],
            "min_ready_capsules": 1,
            "recapture_alert_interval_seconds": 3600,
        }
    },
    "priority_scheduler": {
        "enabled": True,
        "global_interval_seconds": 900,
        "min_volume_24h": 10,
        "min_liquidity_score": 0.6,
        "min_net_profit_rate": 0.03,
        "p1_to_p2_score": 25,
        "p2_to_p3_score": 50,
        "p2_to_p1_score": 12,
        "p3_to_p2_score": 18,
        "p3_to_p2_no_profit_rounds": 3,
        "p2_to_p1_no_hit_rounds": 3,
        "p2_to_p3_hit_rounds": 2,
        "steamdt_fresh_minutes": 60,
        "jit_ttl_minutes": 10,
        "respect_manual_watch": True,
        "manual_watch_min_priority": 3,
        "respect_ttl": True,
        "respect_cooldown": True,
    },
    "crawl_layers": {
        "low_interval_seconds": 28800,
        "mid_interval_seconds": 900,
        "low_limit": 500,
        "mid_limit": 200,
        "high_limit": 100,
    },
    "action_policy": {
        "enabled": True,
        "decision_ttl_minutes": 15,
        "direct_buy_min_profit_rate": 0.08,
        "buy_order_min_profit_rate": 0.12,
        "sell_min_profit_rate": 0.03,
        "min_24h_volume": 20,
        "direct_buy_requires_jit": True,
        "sell_requires_jit": True,
        "allow_direct_buy": True,
        "allow_buy_order": True,
        "allow_auto_sell": False,
        "risk_segment_count": 3,
        "risk_segments": [
            {"min_price": 0, "max_price": 10, "max_capital_per_item": 80, "max_inventory_per_item": 8},
            {"min_price": 10, "max_price": 100, "max_capital_per_item": 300, "max_inventory_per_item": 3},
            {"min_price": 100, "max_price": None, "max_capital_per_item": 800, "max_inventory_per_item": 1},
        ],
    },
    "trading_worker": {
        "enabled": False,
        "safe_mode": True,
        "poll_interval_seconds": 10,
        "batch_size": 10,
        "lease_seconds": 60,
        "error_backoff_seconds": 60,
    },
    "seller_snapshot_scanner": {
        "enabled": False,
        "commit": False,
        "interval_seconds": 3600,
        "error_backoff_seconds": 300,
        "include_inventory": True,
        "include_steam_listings": True,
        "include_c5_orders": True,
        "listing_platform": "steam",
        "delivery_platform": "c5game",
        "channel": "seller_snapshot_scanner",
        "snapshot_payload": {},
    },
}
def merge(default: dict, overrides: dict) -> dict:
    out = dict(default)
    for k, v in overrides.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out
def _validate_ranges(cfg: dict) -> dict:
    # 简单校验一下，防止用户乱填配置搞崩程序
    import warnings
    pipe = cfg.get("pipeline") or {}
    stab = cfg.get("stability") or {}
    buff = cfg.get("buff") or {}

    if isinstance(pipe.get("max_discount"), (int, float)):
        v = pipe["max_discount"]
        if not (0 < v <= 1):
            warnings.warn(f"[config] pipeline.max_discount={v} 超出范围(0,1]，已修正为 {min(max(v, 0.001), 1.0):.4g}")
            pipe["max_discount"] = min(max(v, 0.001), 1.0)

    if isinstance(stab.get("cv_threshold"), (int, float)):
        v = stab["cv_threshold"]
        if not (0 < v < 1):
            warnings.warn(f"[config] stability.cv_threshold={v} 超出范围(0,1)，已修正")
            stab["cv_threshold"] = max(0.001, min(v, 0.999))

    if isinstance(stab.get("r2_threshold"), (int, float)):
        v = stab["r2_threshold"]
        if not (0 < v < 1):
            warnings.warn(f"[config] stability.r2_threshold={v} 超出范围(0,1)，已修正")
            stab["r2_threshold"] = max(0.001, min(v, 0.999))

    if isinstance(stab.get("price_percentile_ceil"), (int, float)):
        v = stab["price_percentile_ceil"]
        if not (0 < v <= 1):
            warnings.warn(f"[config] stability.price_percentile_ceil={v} 超出范围(0,1]，已修正")
            stab["price_percentile_ceil"] = max(0.001, min(v, 1.0))

    # price_percentile_ceil_rising 同上
    if isinstance(stab.get("price_percentile_ceil_rising"), (int, float)):
        v = stab["price_percentile_ceil_rising"]
        if not (0 < v <= 1):
            stab["price_percentile_ceil_rising"] = max(0.001, min(v, 1.0))

    if isinstance(buff.get("price_tolerance"), (int, float)):
        v = buff["price_tolerance"]
        if v < 0:
            warnings.warn(f"[config] buff.price_tolerance={v} 不能为负数，已修正为0")
            buff["price_tolerance"] = 0.0

    return cfg


def get_app_config(loaded: dict) -> dict:
    return _validate_ranges(merge(DEFAULTS, loaded.get("app", {})))
def validate_and_fill(data: dict, defaults: Optional[dict] = None) -> dict:
    if defaults is None:
        defaults = DEFAULTS
    out = {}
    for k, default in defaults.items():
        if k not in data:
            out[k] = dict(default) if isinstance(default, dict) else default
        elif isinstance(default, dict) and isinstance(data[k], dict):
            out[k] = validate_and_fill(merge(default, data[k]), default)
        else:
            val = data[k]
            if isinstance(default, bool) and not isinstance(val, bool):
                val = bool(val) if val is not None else default
            elif isinstance(default, int) and isinstance(val, (float, str)):
                try:
                    val = int(float(val))
                except (ValueError, TypeError):
                    val = default
            elif isinstance(default, float) and isinstance(val, (int, str)):
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    val = default
            elif isinstance(default, list) and not isinstance(val, list):
                val = default
            out[k] = val
    return out
