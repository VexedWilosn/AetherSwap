from typing import Any, Optional
DEFAULTS = {
    "iflow": {
        "page_num": 1,
        "page_size": 200,
        "platforms": "buff",
        "sort_by": "sell",
        "min_price": 2,
        "max_price": 5000,
        "min_volume": 200,
        "type": "swap",
        "want_to_get": "STEAM_BALANCE",
        "sale_plan": "STEAM_SELL_PRICE",
        "fetch_timeout": 15,
    },
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
        "iflow_top_n": 50,
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
        "resell_ratio": 0.85,
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
        "proxies": [],
    },
    "steam_deals": {
        "enabled": False,
        "auto_refresh_days": 7,
        "max_game_threads": 5,
        "max_region_threads": 16,
    },
    "strategies": {
        "active_buy_strategy_id": "system.buy.default",
        "active_sell_strategy_id": "",
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
