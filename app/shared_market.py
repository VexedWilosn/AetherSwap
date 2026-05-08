"""共享 Steam 市场价查询工具.
统一的批量价格查询逻辑，供库存管理和持有饥品两个模块共用，
避免同一物品被重复查询两次。
"""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Set
from urllib.parse import quote
from app.config_loader import get_steam_credentials, load_app_config_validated
_BATCH_MAX_WORKERS = 4
PRICE_SOURCE_SMART = "smart"
PRICE_SOURCE_STEAM_LOWEST = "steam_lowest"
PRICE_SOURCE_LABELS = {
    PRICE_SOURCE_SMART: "智能价",
    PRICE_SOURCE_STEAM_LOWEST: "最低价/中位价摘要",
}
def _parse_price_value(text: str) -> Optional[float]:
    m = re.search(r"[\d,.]+", text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None
def _fetch_priceoverview_lowest_cny(session, market_hash_name: str, app_id: int = 730) -> Optional[float]:
    """Fallback market price source when itemordershistogram is unavailable."""
    from utils.proxy_manager import get_proxy_manager
    name = (market_hash_name or "").strip()
    if not name:
        return None
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "country": "CN",
        "currency": 23,
        "appid": int(app_id),
        "market_hash_name": name,
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"https://steamcommunity.com/market/listings/{int(app_id)}/{quote(name, safe='')}",
        "X-Requested-With": "XMLHttpRequest",
    }
    pm = get_proxy_manager()
    for attempt in range(3):
        proxies = None if attempt == 0 else pm.get_proxies_for_request(failed=True)
        try:
            resp = session.get(url, params=params, headers=headers, timeout=15, proxies=proxies, verify=False)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, dict) or not data.get("success"):
                continue
            price = _parse_price_value(data.get("lowest_price") or data.get("median_price") or "")
            if price is not None and price > 0:
                return round(price, 2)
        except Exception:
            continue
    return None
def get_market_price_context() -> dict:
    cfg = load_app_config_validated()
    proxy_cfg = cfg.get("proxy_pool", {})
    proxies = [p for p in proxy_cfg.get("proxies", []) if p.get("host")]
    try:
        from steam.market_orders import get_market_circuit_state
        circuit = get_market_circuit_state()
    except Exception:
        circuit = {}
    proxy_enabled = bool(proxy_cfg.get("enabled")) and int(proxy_cfg.get("strategy", 1) or 1) != 3
    warning = None
    if circuit.get("open"):
        warning = f"Steam 智能价熔断中，约 {circuit.get('remaining_seconds', 0)} 秒后恢复；当前仅能使用最低价/中位价摘要。"
    return {
        "proxy_enabled": proxy_enabled,
        "proxy_strategy": int(proxy_cfg.get("strategy", 1) or 1),
        "configured_proxy_count": len(proxies),
        "circuit": circuit,
        "proxy_hint": "已配置代理但代理池未启用；如果直连频繁 429，可在代理池切到策略 1 或 2。" if proxies and not proxy_enabled else None,
        "warning": warning,
    }
def get_steam_market_price_detail(session, market_hash_name: str, app_id: int = 730, force_smart: bool = False) -> dict:
    from steam.market_orders import compute_smart_list_price, get_sell_orders_cny
    cfg = load_app_config_validated().get("pipeline", {})
    wall_volume = int(cfg.get("sell_price_wall_volume", 20))
    max_ignore = int(cfg.get("sell_price_max_ignore_volume", 4))
    result = get_sell_orders_cny(
        session,
        market_hash_name,
        app_id=app_id,
        request_delay=0.2 if force_smart else 1.0,
        use_cache=not force_smart,
        force_refresh=force_smart,
        ignore_circuit=force_smart,
    )
    if result and result.get("sell_orders"):
        price, reason = compute_smart_list_price(
            result["sell_orders"],
            wall_volume_threshold=wall_volume,
            max_ignore_volume=max_ignore,
            min_step=0,
            offset=0,
        )
        if price is not None and price > 0:
            return {
                "price": round(float(price), 2),
                "source": PRICE_SOURCE_SMART,
                "source_label": PRICE_SOURCE_LABELS[PRICE_SOURCE_SMART],
                "reason": reason,
            }
    fallback = _fetch_priceoverview_lowest_cny(session, market_hash_name, app_id=app_id)
    if fallback is not None and fallback > 0:
        return {
            "price": round(float(fallback), 2),
            "source": PRICE_SOURCE_STEAM_LOWEST,
            "source_label": PRICE_SOURCE_LABELS[PRICE_SOURCE_STEAM_LOWEST],
            "reason": "itemordershistogram 不可用，使用 priceoverview.lowest_price/median_price",
        }
    return {"price": None, "source": None, "source_label": "", "reason": "Steam 市场价获取失败"}
def get_steam_smart_price_cny(session, market_hash_name: str, app_id: int = 730) -> Optional[float]:
    """获取单个物品的 Steam 市场智能报价（CNY）.
    同时被 inventory.py 和 transactions.py 使用的核心函数。
    """
    detail = get_steam_market_price_detail(session, market_hash_name, app_id=app_id)
    return detail.get("price")
def batch_fetch_price_details(names: Set[str], app_id: int = 730, force_smart: bool = False) -> Dict[str, dict]:
    """Batch query market prices with source metadata."""
    from steam.session import create_market_session
    if not names:
        return {}
    cred = get_steam_credentials()
    cookies = cred.get("cookies", "")
    steam_id = cred.get("steam_id", "")
    if not cookies or not steam_id:
        return {}
    def _fetch_one(name: str) -> tuple:
        try:
            session = create_market_session(cookies, steam_id)
            detail = get_steam_market_price_detail(session, name, app_id=app_id, force_smart=force_smart)
            return name, detail
        except Exception as e:
            try:
                from app.state import log
                log(f"现市场价获取失败: {name} ({type(e).__name__})", "debug", category="market_price")
            except Exception:
                pass
            return name, {"price": None, "source": None, "source_label": "", "reason": type(e).__name__}
    details: Dict[str, dict] = {}
    valid_names = [n for n in names if n]
    with ThreadPoolExecutor(max_workers=_BATCH_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, name): name for name in valid_names}
        for future in as_completed(futures):
            try:
                name, detail = future.result()
                price = detail.get("price") if isinstance(detail, dict) else None
                if price is not None and price > 0:
                    details[name] = {
                        **detail,
                        "price": round(float(price), 2),
                    }
            except Exception:
                pass
    return details
def batch_fetch_prices(names: Set[str], app_id: int = 730) -> Dict[str, float]:
    """批量查询一组物品名称的市场价，返回 {name: price_cny}.
    - PERF-01: 使用 ThreadPoolExecutor 并发查询（最多 4 线程），速度远快于串行
    - 每个线程使用独立的 session，避免 requests.Session 线程安全问题
    - 返回字典中只包含成功取到价格的条目
    """
    return {name: detail["price"] for name, detail in batch_fetch_price_details(names, app_id=app_id).items()}
