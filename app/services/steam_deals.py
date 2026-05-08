"""
Steam 折扣游戏数据抓取服务。
通过 CheapShark API 获取打折游戏列表，然后并发查询 Steam API
获取 16 个区域的价格和评论数据，存入 SQLite 数据库。
"""
import re
import random
import threading
import time
import requests
import urllib3
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
REGIONS = {
    '中国': 'cn', '俄罗斯': 'ru', '哈萨克斯坦': 'kz', '乌克兰': 'ua',
    '南亚': 'pk', '土耳其': 'tr', '阿根廷': 'ar', '阿塞拜疆': 'az',
    '越南': 'vn', '印尼': 'id', '印度': 'in', '巴西': 'br',
    '智利': 'cl', '日本': 'jp', '中国香港': 'hk', '菲律宾': 'ph'
}
REGION_NAMES = {v: k for k, v in REGIONS.items()}
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/122.0.0.0 Safari/537.36",
]
_fetch_lock = threading.Lock()
_fetch_state = {
    "running": False,
    "progress": 0,        
    "total": 0,           
    "failed": 0,          
    "message": "",        
    "cancel_requested": False,
    "saved": 0,
}
def get_fetch_state() -> dict:
    with _fetch_lock:
        return dict(_fetch_state)
def _update_state(**kwargs):
    with _fetch_lock:
        _fetch_state.update(kwargs)
def request_cancel_fetch() -> bool:
    with _fetch_lock:
        if not _fetch_state.get("running"):
            return False
        _fetch_state["cancel_requested"] = True
        _fetch_state["message"] = "正在停止获取，等待已发出的请求结束..."
        return True
def _cancel_requested() -> bool:
    with _fetch_lock:
        return bool(_fetch_state.get("cancel_requested"))
def _build_valid_proxies() -> list:
    """Read proxies from app config, test them, return sorted valid ones."""
    try:
        from app.config_loader import load_app_config_validated
        cfg = load_app_config_validated()
        pool_cfg = cfg.get("proxy_pool", {})
        proxies_raw = pool_cfg.get("proxies", [])
        test_url = pool_cfg.get("test_url", "https://ipv4.webshare.io/")
        timeout = int(pool_cfg.get("timeout_seconds", 10))
    except Exception:
        return []
    if not proxies_raw:
        return []
    all_proxies = []
    for p in proxies_raw:
        host = p.get("host", "")
        port = p.get("port", 0)
        user = p.get("username", "")
        pwd = p.get("password", "")
        if not host:
            continue
        if user and pwd:
            url = f"http://{user}:{pwd}@{host}:{port}/"
        else:
            url = f"http://{host}:{port}/"
        all_proxies.append({"http": url, "https": url})
    _update_state(message=f"⏳ 正在测速代理池 ({len(all_proxies)} 个)...")
    def _test(proxy_dict):
        steam_test = "https://store.steampowered.com/api/appdetails?appids=10&cc=us&filters=basic"
        start = time.time()
        try:
            resp = requests.get(steam_test, proxies=proxy_dict, timeout=min(timeout, 8), verify=False)
            if resp.status_code == 200:
                return (proxy_dict, time.time() - start)
        except Exception:
            pass
        return None
    valid = []
    with ThreadPoolExecutor(max_workers=min(len(all_proxies), 30)) as executor:
        futures = [executor.submit(_test, p) for p in all_proxies]
        for f in as_completed(futures):
            res = f.result()
            if res:
                valid.append(res)
    valid.sort(key=lambda x: x[1])
    result = [p[0] for p in valid]
    _update_state(message=f"✅ 代理测速完成，可用 {len(result)} 个")
    return result
def _load_configured_proxies() -> list:
    """Read proxy pool config without testing it."""
    try:
        from app.config_loader import load_app_config_validated
        cfg = load_app_config_validated()
        proxies_raw = cfg.get("proxy_pool", {}).get("proxies", [])
    except Exception:
        return []
    proxies = []
    for p in proxies_raw:
        host = p.get("host", "")
        port = p.get("port", 0)
        user = p.get("username", "")
        pwd = p.get("password", "")
        if not host:
            continue
        if user and pwd:
            url = f"http://{user}:{pwd}@{host}:{port}/"
        else:
            url = f"http://{host}:{port}/"
        proxies.append({"http": url, "https": url})
    return proxies
def _fetch_with_proxy(url: str, valid_proxies: list, max_retries: int = 5):
    """Fetch JSON from URL using random proxy from pool."""
    headers = {'User-Agent': random.choice(USER_AGENTS)}
    for attempt in range(max_retries):
        proxy = random.choice(valid_proxies) if valid_proxies else None
        try:
            resp = requests.get(url, proxies=proxy, headers=headers, timeout=8, verify=False)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(1)
                continue
        except Exception:
            continue
    return None
def _get_discounted_appids(max_count: int = 20000, valid_proxies: Optional[list] = None) -> List[str]:
    """Fetch discounted game AppIDs from Steam search API, up to max_count.
    Uses Steam's native search endpoint with specials=1 filter, paginated via
    the `start` offset parameter (max 100 per page). Every request is routed
    through the proxy pool to avoid rate-limiting (HTTP 429).
    """
    _update_state(message=f"🕵️ 正在从 Steam 获取打折游戏列表 (上限 {max_count})...")
    appids: List[str] = []
    seen: set = set()
    start = 0
    page_size = 100
    total_count: Optional[int] = None
    base_url = (
        "https://store.steampowered.com/search/results/"
        "?specials=1"          
        "&category1=998"       
        "&sort_by=Global_Topsellers"  
        "&json=1"
        "&count={count}&start={start}"
    )
    consecutive_empty = 0
    while len(appids) < max_count:
        if _cancel_requested():
            _update_state(message=f"已停止获取游戏 ID，当前 {len(appids)} 个", total=len(appids))
            break
        url = base_url.format(count=page_size, start=start)
        try:
            data = _fetch_with_proxy(url, valid_proxies or [], max_retries=5)
            if data is None:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                time.sleep(2)
                continue
            items = data.get("items", [])
            if total_count is None:
                total_count = data.get("total_count", 0)
            if not items:
                break
            consecutive_empty = 0
            new_count = 0
            for item in items:
                logo = item.get("logo", "")
                m = re.search(r"/apps/(\d+)/", logo)
                if not m:
                    continue
                appid = m.group(1)
                if appid and appid not in seen:
                    seen.add(appid)
                    appids.append(appid)
                    new_count += 1
                    if len(appids) >= max_count:
                        break
            _update_state(
                message=(
                    f"🔄 已获取 {len(appids)}"
                    + (f"/{total_count}" if total_count else "")
                    + " 个打折游戏 ID..."
                ),
                total=len(appids),
            )
            start += page_size
            time.sleep(0.5)
        except Exception:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            time.sleep(2)
    _update_state(message=f"✅ 获取到 {len(appids)} 个打折游戏 ID", total=len(appids))
    return appids
def _fetch_region_data(appid: str, cc_code: str, valid_proxies: list, max_region_retries: int = 3):
    """Fetch price data for a single game in a single region.
    On network failure (data is None) rotates to a different proxy and retries
    up to max_region_retries times so transient blocks don't produce empty cells.
    """
    lang = "schinese" if cc_code == "cn" else "english"
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={cc_code}&l={lang}&filters=price_overview,basic"
    data = None
    for _attempt in range(max(1, max_region_retries)):
        data = _fetch_with_proxy(url, valid_proxies, max_retries=3)
        if data is not None:
            break  
        time.sleep(0.5)
    price_str = None
    discount_str = "0%"
    original_str = None
    game_name = None
    final_cents = None  
    header_image = None
    if data:
        app_info = data.get(str(appid), {})
        if app_info.get('success'):
            game_name = app_info['data'].get('name')
            header_image = app_info['data'].get('header_image')
            price_info = app_info['data'].get('price_overview')
            if price_info:
                original_str = price_info.get('initial_formatted', price_info.get('final_formatted', None))
                price_str = price_info.get('final_formatted', None)
                final_cents = price_info.get('final', None)  
                discount = price_info.get('discount_percent', 0)
                discount_str = f"-{discount}%" if discount > 0 else "0%"
        else:
            price_str = "锁区"
    return cc_code, price_str, discount_str, original_str, game_name, final_cents, header_image
def refresh_banner_url(appid: str, valid_proxies: Optional[list] = None) -> Optional[str]:
    """Fetch the current Steam header image URL for an app.

    Some newer apps no longer expose the legacy /steam/apps/{appid}/header.jpg
    asset, so the frontend retry path refreshes the URL from appdetails.
    """
    appid = str(appid).strip()
    if not appid:
        return None
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=english&filters=basic"
    proxies = valid_proxies if valid_proxies is not None else _load_configured_proxies()
    data = _fetch_with_proxy(url, proxies or [], max_retries=5)
    if not data:
        return None
    app_info = data.get(appid, {})
    if not app_info.get("success"):
        return None
    header_image = app_info.get("data", {}).get("header_image")
    return header_image if header_image else None
def _fetch_historical_low(appid: str, valid_proxies: list) -> Optional[dict]:
    """Fetch historical low info (price in USD and timestamp) from CheapShark."""
    lookup_url = f"https://www.cheapshark.com/api/1.0/games?steamAppID={appid}"
    data = _fetch_with_proxy(lookup_url, valid_proxies, max_retries=2)
    if data and isinstance(data, list) and len(data) > 0:
        cs_game_id = data.get("gameID") if isinstance(data, dict) else data[0].get("gameID")
        if cs_game_id:
            details_url = f"https://www.cheapshark.com/api/1.0/games?id={cs_game_id}"
            details = _fetch_with_proxy(details_url, valid_proxies, max_retries=2)
            if details:
                cheapest_info = details.get("cheapestPriceEver", {})
                price = cheapest_info.get("price")
                date_ts = cheapest_info.get("date")
                if price is not None and date_ts is not None:
                    return {"price": float(price), "date": int(date_ts)}
    return None
def _get_deal_status(current_price_usd: float, lowest_usd: float, lowest_ts: int) -> str:
    """Determine the deal status based on 24 hour threshold."""
    now_ts = time.time()
    diff_seconds = now_ts - lowest_ts
    diff_price = current_price_usd - lowest_usd
    if diff_price < -0.05:
        return "新史低"
    elif abs(diff_price) <= 0.05:
        if diff_seconds <= (24 * 3600):
            return "新史低"
        else:
            return "平史低"
    else:
        return "普通打折"
_MIN_VALID_REGIONS = 8
def _process_single_game(appid: str, valid_proxies: list, max_region_threads: int = 16, rates: dict = None) -> Optional[dict]:
    """Process a single game: fetch reviews + 16 region prices + cheapshark.
    Returns None if the record fails the quality gate (game name still unknown
    or fewer than _MIN_VALID_REGIONS regions returned a real price), so the
    caller can discard it without writing garbage to the database.
    """
    game_data = {
        "app_id": str(appid),
        "name": "Unknown",
        "name_en": "Unknown",
        "banner_url": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        "positive_rate": None,
        "total_reviews": 0,
        "discount_percent": 0,
        "deal_status": None,
        "fetched_at": time.time(),
    }
    fetched_name_cn = None
    fetched_name_en = None
    fetched_header_image = None
    review_url = f"https://store.steampowered.com/appreviews/{appid}?json=1&language=all"
    review_resp = _fetch_with_proxy(review_url, valid_proxies, max_retries=3)
    if review_resp:
        summary = review_resp.get('query_summary', {})
        total_rev = summary.get('total_reviews', 0)
        total_pos = summary.get('total_positive', 0)
        if total_rev > 0:
            game_data["positive_rate"] = round((total_pos / total_rev) * 100, 1)
            game_data["total_reviews"] = total_rev
    with ThreadPoolExecutor(max_workers=max_region_threads) as executor:
        regions_to_fetch = list(REGIONS.values())
        if "us" not in regions_to_fetch:
            regions_to_fetch.append("us")
        futures = [
            executor.submit(_fetch_region_data, appid, code, valid_proxies)
            for code in regions_to_fetch
        ]
        for future in as_completed(futures):
            try:
                cc, price, discount, original, name, cents, header_image = future.result()
                if cc != "us":
                    game_data[f"price_{cc}"] = price
                    game_data[f"discount_{cc}"] = discount
                if cents is not None:
                    game_data[f"price_cents_{cc}"] = cents
                if cc == "cn" and original:
                    game_data["original_cn"] = original
                if name:
                    if cc == "cn":
                        fetched_name_cn = name
                    else:
                        if not fetched_name_en:
                            fetched_name_en = name
                if header_image and not fetched_header_image:
                    fetched_header_image = header_image
                if cc == "cn" and discount and discount != "0%":
                    try:
                        game_data["discount_percent"] = int(discount.replace("%", "").replace("+", ""))
                    except ValueError:
                        pass
            except Exception:
                pass
    final_name = fetched_name_cn if fetched_name_cn else fetched_name_en
    final_name_en = fetched_name_en if fetched_name_en else fetched_name_cn
    game_data["name"] = final_name or "Unknown"
    game_data["name_en"] = final_name_en or "Unknown"
    if fetched_header_image:
        game_data["banner_url"] = fetched_header_image
    cs_his_low = _fetch_historical_low(appid, valid_proxies)
    if cs_his_low and game_data.get("price_cents_us") is not None:
        current_us_price = game_data["price_cents_us"] / 100.0
        game_data["deal_status"] = _get_deal_status(
            current_us_price, 
            cs_his_low["price"], 
            cs_his_low["date"]
        )
    elif game_data.get("price_cents_cn") is not None:
        game_data["deal_status"] = "普通打折"
    if game_data["name"] == "Unknown":
        return None
    valid_price_count = sum(
        1 for cc in REGIONS.values()
        if game_data.get(f"price_{cc}") not in (None, "锁区")
    )
    if valid_price_count < _MIN_VALID_REGIONS:
        return None
    return game_data
def run_fetch(max_game_threads: int = 5, max_region_threads: int = 16):
    """
    Main entry: build proxy pool, fetch appids, process games concurrently.
    Designed to be called in a background thread.
    """
    from app.database import db_delete_steam_deals_older_than, db_upsert_steam_deal
    with _fetch_lock:
        if _fetch_state["running"]:
            return
        run_started_at = time.time()
        _fetch_state.update({
            "running": True,
            "progress": 0,
            "total": 0,
            "failed": 0,
            "cancel_requested": False,
            "saved": 0,
            "message": "🚀 开始获取...",
        })
    try:
        valid_proxies = _build_valid_proxies()
        if not valid_proxies:
            _update_state(message="⚠️ 无可用代理，尝试直连...")
        appids = _get_discounted_appids(max_count=20000, valid_proxies=valid_proxies)
        if _cancel_requested():
            _update_state(
                running=False,
                cancel_requested=False,
                message=f"已停止。已获取 {len(appids)} 个游戏 ID，未开始详情抓取",
            )
            return
        if not appids:
            _update_state(running=False, cancel_requested=False, message="❌ 未获取到任何游戏 ID")
            return
        _update_state(
            total=len(appids),
            progress=0,
            message=f"⛏️ 开始处理 {len(appids)} 款游戏 ({max_game_threads}x{max_region_threads} 并发)...",
        )
        fx_file = Path(__file__).resolve().parent.parent.parent / "config" / "exchange_rate.json"
        rates = {}
        try:
            if fx_file.exists():
                with open(fx_file, "r", encoding="utf-8") as f:
                    rates = json.load(f).get("rates", {})
        except Exception:
            pass
        count = 0
        failed = 0
        saved = 0
        cancelled = False
        fused = False
        batch_size = max(1, max_game_threads) * 2
        with ThreadPoolExecutor(max_workers=max_game_threads) as executor:
            for start in range(0, len(appids), batch_size):
                if _cancel_requested():
                    cancelled = True
                    break
                batch = appids[start:start + batch_size]
                future_map = {
                    executor.submit(_process_single_game, appid, valid_proxies, max_region_threads, rates): appid
                    for appid in batch
                }
                for future in as_completed(future_map):
                    if _cancel_requested():
                        cancelled = True
                        break
                    count += 1
                    try:
                        game = future.result()
                        if game is None:
                            failed += 1
                            _update_state(
                                progress=count,
                                failed=failed,
                                saved=saved,
                                message=f"⏭️ [{count}/{len(appids)}] 质量不达标，跳过",
                            )
                        else:
                            db_upsert_steam_deal(game)
                            saved += 1
                            _update_state(
                                progress=count,
                                failed=failed,
                                saved=saved,
                                message=f"✅ [{count}/{len(appids)}] {game['name']}",
                            )
                    except Exception as e:
                        failed += 1
                        _update_state(
                            progress=count,
                            failed=failed,
                            saved=saved,
                            message=f"⚠️ [{count}/{len(appids)}] 处理失败: {str(e)[:60]}",
                        )
                    if count >= 100 and failed / max(count, 1) >= 0.8 and saved < 10:
                        fused = True
                        _update_state(
                            progress=count,
                            failed=failed,
                            saved=saved,
                            message=f"熔断：已处理 {count} 款，失败率 {failed / count:.0%}，请检查代理池或 Steam 访问",
                        )
                        break
                if cancelled or fused:
                    break
        if cancelled:
            _update_state(
                running=False,
                cancel_requested=False,
                message=f"已停止。已处理 {count}/{len(appids)} 款，保存 {saved} 款，失败 {failed} 款",
            )
            return
        if fused:
            _update_state(
                running=False,
                cancel_requested=False,
                message=f"已熔断。已处理 {count}/{len(appids)} 款，保存 {saved} 款，失败 {failed} 款",
            )
            return
        removed = db_delete_steam_deals_older_than(run_started_at)
        _update_state(
            running=False,
            cancel_requested=False,
            saved=saved,
            message=f"🎉 完成！共处理 {count} 款游戏，保存 {saved} 款，失败 {failed} 款，清理旧记录 {removed} 款",
        )
    except Exception as e:
        _update_state(running=False, cancel_requested=False, message=f"❌ 抓取出错: {str(e)[:100]}")
