import re
import threading
import time
import logging
from typing import Any, List, Optional, Tuple

from steam.client import build_listing_url
from utils.delay import jittered_sleep
from utils.proxy_manager import get_proxy_manager
from app.database import db_get_item_nameid, db_set_item_nameid

logger = logging.getLogger(__name__)

_ITEM_NAMEID_TTL = 300
_SELL_ORDERS_TTL = 60
_item_nameid_cache: dict = {}
_sell_orders_cache: dict = {}
_item_nameid_cache_lock = threading.Lock()
_sell_orders_cache_lock = threading.Lock()
def clear_caches() -> None:
    with _item_nameid_cache_lock:
        _item_nameid_cache.clear()
    with _sell_orders_cache_lock:
        _sell_orders_cache.clear()
CURRENCY_CNY = 23
_ITEM_NAMEID_PATTERNS = [
    re.compile(r"Market_LoadOrderSpread\s*\(\s*(\d+)\s*\)", re.I),
    re.compile(r"item_nameid['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
]
def _extract_item_nameid(html: str) -> Optional[str]:
    for pat in _ITEM_NAMEID_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None
def get_item_nameid(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    timeout: int = 15,
    use_cache: bool = True,
) -> Optional[str]:
    key = (market_hash_name.strip(), app_id)
    db_nameid = db_get_item_nameid(key[0])
    if db_nameid:
        return db_nameid
    if use_cache:
        with _item_nameid_cache_lock:
            entry = _item_nameid_cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
    url = build_listing_url(market_hash_name, app_id)
    headers = {
        "Accept": "*/*",
        "Referer": url,
    }
    pm = get_proxy_manager()
    for attempt in range(3):
        failed = (attempt > 0)
        proxies = pm.get_proxies_for_request(failed=failed)
        try:
            r = session.get(url, headers=headers, timeout=timeout, proxies=proxies)
            if r.status_code == 200:
                nameid = _extract_item_nameid(r.text)
                if nameid:
                    db_set_item_nameid(key[0], nameid)
                    if use_cache:
                        with _item_nameid_cache_lock:
                            _item_nameid_cache[key] = (nameid, time.time() + _ITEM_NAMEID_TTL)
                return nameid
        except Exception as e:
            logger.debug("获取item_nameid失败 (attempt=%d/3) proxies=%s, error=%s", attempt+1, proxies, type(e).__name__)
        
        if attempt < 2:
            jittered_sleep(1.0)

    return None
_cb_lock = threading.Lock()  
_cb_fail_streak = 0          
_cb_open_until = 0.0         
_CB_FAIL_THRESHOLD = 5       
_CB_COOLDOWN_SEC = 300       

def _is_market_circuit_enabled() -> bool:
    try:
        from app.config_loader import load_app_config_validated
        cfg = load_app_config_validated().get("pipeline", {})
        return cfg.get("market_price_circuit_enabled", True) is not False
    except Exception:
        return True

def get_market_circuit_state() -> dict:
    enabled = _is_market_circuit_enabled()
    with _cb_lock:
        remaining = max(0, int(_cb_open_until - time.time())) if enabled else 0
        return {
            "enabled": enabled,
            "open": enabled and remaining > 0,
            "remaining_seconds": remaining,
            "fail_streak": _cb_fail_streak,
            "threshold": _CB_FAIL_THRESHOLD,
            "cooldown_seconds": _CB_COOLDOWN_SEC,
        }

def clear_market_circuit_state() -> dict:
    global _cb_fail_streak, _cb_open_until
    with _cb_lock:
        was_open = time.time() < _cb_open_until
        _cb_fail_streak = 0
        _cb_open_until = 0.0
    return {**get_market_circuit_state(), "was_open": was_open}

def fetch_item_orders_histogram(
    session,
    item_nameid: str,
    *,
    country: str = "CN",
    language: str = "english",
    currency: int = CURRENCY_CNY,
    timeout: int = 15,
    ignore_circuit: bool = False,
) -> Optional[dict]:
    global _cb_fail_streak, _cb_open_until
    circuit_enabled = _is_market_circuit_enabled()
    with _cb_lock:
        if circuit_enabled and time.time() < _cb_open_until and not ignore_circuit:
            return None
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": country,
        "language": language,
        "currency": currency,
        "item_nameid": item_nameid,
        "no_render": "1",
        "two_factor_hash": "",
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://steamcommunity.com/market/",
    }
    pm = get_proxy_manager()
    any_success = False
    
    for attempt in range(3):
        if attempt == 0:
            effective_proxies = None                                    
        else:
            effective_proxies = pm.get_proxies_for_request(failed=True) 
            
        try:
            r = session.get(
                url, params=params, headers=headers,
                timeout=timeout, proxies=effective_proxies, verify=False,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("success") == 1:
                    with _cb_lock:
                        _cb_fail_streak = 0
                        _cb_open_until = 0.0
                    return data
                
                logger.debug("直方图 success非1 resp=%s", str(data)[:150])
            else:
                logger.debug("直方图 HTTP %s (attempt=%d)", r.status_code, attempt+1)
        except Exception as e:
            logger.debug("直方图失败 (attempt=%d/3) proxy=%s err=%s: %s", attempt+1, effective_proxies is not None, type(e).__name__, str(e)[:60])
            
        if attempt < 2:
            jittered_sleep(1.0)
    if not circuit_enabled:
        with _cb_lock:
            _cb_fail_streak = 0
            _cb_open_until = 0.0
        return None
    with _cb_lock:
        _cb_fail_streak += 1
        if _cb_fail_streak >= _CB_FAIL_THRESHOLD:
            _cb_open_until = time.time() + _CB_COOLDOWN_SEC
            streak_snap = _cb_fail_streak
            _cb_fail_streak = 0
        else:
            streak_snap = None
    if streak_snap is not None:
        logger.warning(
            "Steam市场连续 %d 轮全部失败，熔断 %d 分钟。请确认加速器/代理是否正常。",
            streak_snap, _CB_COOLDOWN_SEC // 60
        )

    return None
def cents_to_yuan(cents: int) -> float:
    return cents / 100.0
def _parse_sell_order_graph(raw: Any) -> List[Tuple[float, int]]:
    if not isinstance(raw, list):
        return []
    out: List[Tuple[float, int]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = float(row[0])
            volume = int(row[1])
            out.append((price, volume))
        except (ValueError, TypeError):
            continue
    return out
def get_sell_orders_cny(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    country: str = "CN",
    language: str = "english",
    request_delay: float = 1.0,
    use_cache: bool = True,
    force_refresh: bool = False,
    ignore_circuit: bool = False,
) -> Optional[dict]:
    key = (market_hash_name.strip(), app_id)
    if use_cache and not force_refresh:
        with _sell_orders_cache_lock:
            entry = _sell_orders_cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
    item_nameid = get_item_nameid(session, market_hash_name, app_id)
    if not item_nameid:
        return None
    if request_delay > 0:
        jittered_sleep(request_delay)
    data = fetch_item_orders_histogram(
        session,
        item_nameid,
        country=country,
        language=language,
        currency=CURRENCY_CNY,
        ignore_circuit=ignore_circuit,
    )
    if not data:
        return None
    raw_lowest = data.get("lowest_sell_order")
    lowest_price: Optional[float] = None
    if raw_lowest is not None:
        try:
            lowest_price = cents_to_yuan(int(raw_lowest))
        except (ValueError, TypeError):
            pass
    sell_orders = _parse_sell_order_graph(data.get("sell_order_graph", []))
    result = {"lowest_price": lowest_price, "sell_orders": sell_orders}
    if use_cache:
        with _sell_orders_cache_lock:
            _sell_orders_cache[key] = (result, time.time() + _SELL_ORDERS_TTL)
    return result
STEAM_MIN_PRICE = 0.03
def _get_dynamic_thresholds(current_price: float) -> Tuple[float, float]:
    if current_price < 5.0:
        return 0.10, 0.08
    if current_price < 20.0:
        return 0.30, 0.05
    if current_price < 100.0:
        return 1.0, 0.03
    if current_price < 500.0:
        return 5.0, 0.02
    return 10.0, 0.015
def compute_smart_list_price(
    sell_orders: List[Tuple[float, int]],
    *,
    wall_volume_threshold: int = 20,
    max_ignore_volume: int = 4,
    min_lowest_tier_volume: int = 3,
    min_step: float = 0.01,
    min_floor_price: float = STEAM_MIN_PRICE,
    offset: float = 0.0,
) -> Tuple[Optional[float], str]:
    if not sell_orders:
        return None, "无卖单数据"
    sell_orders = sorted(sell_orders, key=lambda x: x[0])
    while len(sell_orders) >= 2 and sell_orders[0][1] <= min_lowest_tier_volume:
        sell_orders = sell_orders[1:]
    if not sell_orders:
        return None, "无卖单数据"
    wall_index = len(sell_orders) - 1
    cumulative = 0
    for i, (price, count) in enumerate(sell_orders):
        cumulative += count
        if cumulative >= wall_volume_threshold:
            wall_index = i
            break
    analysis_scope = sell_orders[: wall_index + 1]
    if len(analysis_scope) < 2:
        target = analysis_scope[0][0] - min_step
        final = max(min_floor_price, target + offset)
        return round(final, 2), "单档无断层"
    final_price = analysis_scope[0][0] - min_step
    reason = "常规压价"
    current_ignore_vol = 0
    for i in range(len(analysis_scope) - 1):
        p_curr, c_curr = analysis_scope[i]
        p_next, _ = analysis_scope[i + 1]
        current_ignore_vol += c_curr
        if current_ignore_vol > max_ignore_volume:
            reason = "阻挡量超阈值停"
            break
        gap_abs, gap_rel = _get_dynamic_thresholds(p_curr)
        diff = p_next - p_curr
        threshold = max(gap_abs, p_curr * gap_rel)
        if diff > threshold:
            final_price = p_next - min_step
            reason = f"断层跳跃({p_curr:.2f}→{p_next:.2f})"
    final = max(min_floor_price, final_price + offset)
    return round(final, 2), reason
def get_lowest_sell_price_cny(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    country: str = "CN",
    language: str = "english",
    request_delay: float = 1.0,
    use_cache: bool = True,
) -> Optional[float]:
    result = get_sell_orders_cny(
        session,
        market_hash_name,
        app_id,
        country=country,
        language=language,
        request_delay=request_delay,
        use_cache=use_cache,
    )
    return result.get("lowest_price") if result else None
