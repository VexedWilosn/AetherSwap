import json
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import requests
from bs4 import BeautifulSoup
from DataEngine.profit_model import steam_sale_gross_price_from_net
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
CREDENTIALS_FILE = ROOT / "config" / "credentials.json"
TRANSACTIONS_FILE = ROOT / "config" / "transactions.json"
MYHISTORY_RENDER_URL = "https://steamcommunity.com/market/myhistory/render/"
HOVER_PATTERN = re.compile(
    r"CreateItemHoverFromContainer\s*\(\s*g_rgAssets\s*,\s*'(history_row_\d+_\d+)_name'\s*,\s*(\d+)\s*,\s*'(\d+)'\s*,\s*'(\d+)'"
)
TIMEOUT = 25
def _cookies_to_dict(cookies) -> dict:
    if isinstance(cookies, dict):
        return dict(cookies)
    out = {}
    for part in (cookies or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
def _fetch_sold_with_names(cookies: dict) -> tuple:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {"query": "", "start": 0, "count": 500, "contextid": 2, "appid": 730}
    r = requests.get(MYHISTORY_RENDER_URL, params=params, headers=headers, cookies=cookies, verify=False, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json() if r.text else {}
    if not data.get("success"):
        raise RuntimeError(data.get("message", "请求失败"))
    row_to_assetid = {}
    for m in HOVER_PATTERN.finditer(data.get("hovers") or ""):
        row_to_assetid[m.group(1)] = str(m.group(4))
    sold = {}
    sold_names = {}
    html = data.get("results_html") or ""
    soup = BeautifulSoup(html, "html.parser")
    rate_map = {}
    try:
        fx_file = ROOT / "config" / "exchange_rate.json"
        if fx_file.exists():
            with open(fx_file, "r", encoding="utf-8") as f:
                fx = json.load(f)
            if isinstance(fx, dict) and isinstance(fx.get("rates"), dict):
                rate_map = {k: float(v) for k, v in fx["rates"].items() if isinstance(v, (int, float))}
    except Exception:
        rate_map = {}
    def _currency_code_from_price_text(text: str) -> str:
        s = text or ""
        if "¥" in s or "￥" in s or "CNY" in s or "RMB" in s:
            return "CNY"
        if "HK" in s and "$" in s:
            return "HKD"
        if "₹" in s:
            return "INR"
        if "₽" in s:
            return "RUB"
        if "€" in s:
            return "EUR"
        if "USD" in s or "US$" in s:
            return "USD"
        if "$" in s:
            return "USD"
        return "CNY"
    for row in soup.find_all("div", class_="market_listing_row"):
        row_id = row.get("id") or ""
        if not row_id.startswith("history_row_"):
            continue
        assetid = row_to_assetid.get(row_id)
        if not assetid:
            fallback = re.search(r"assetid[\"']?\s*[:=]\s*[\"']?(\d+)[\"']?", str(row), re.I)
            if fallback:
                assetid = str(fallback.group(1))
            else:
                link = row.find("a", href=re.compile(r"assetid=\d+"))
                if link and link.get("href"):
                    ma = re.search(r"assetid=(\d+)", link["href"])
                    if ma:
                        assetid = str(ma.group(1))
            if not assetid:
                continue
        else:
            assetid = str(assetid)
        status_div = row.find("div", class_="market_listing_listed_date_combined")
        status_text = (status_div.get_text(strip=True) or "") if status_div else ""
        if not any(s in status_text for s in ("Sold", "已售出", "出售")):
            continue
        name = ""
        name_el = row.find("span", class_="market_listing_item_name") or row.find("a", class_="market_listing_item_name_link")
        if name_el:
            name = (name_el.get_text(strip=True) or "").strip()
        if not name:
            link = row.find("a", href=re.compile(r"listings/730/"))
            if link and link.get("href"):
                m = re.search(r"listings/730/(.+)$", link["href"])
                if m:
                    name = urllib.parse.unquote(m.group(1)).strip() or "(未知)"
        if not name:
            name = "(未知)"
        price_el = row.find("span", class_="market_listing_price")
        if not price_el:
            continue
        raw_text = price_el.get_text() or ""
        cur_code = _currency_code_from_price_text(raw_text)
        text = raw_text.replace(",", ".")
        m = re.search(r"[\d.]+", text)
        if m:
            try:
                raw = float(m.group(0))
                cny_raw = raw
                if cur_code != "CNY":
                    rate = rate_map.get(cur_code)
                    if rate:
                        cny_raw = raw * rate
                sale_price = round(steam_sale_gross_price_from_net(cny_raw), 2)
                sold[assetid] = sale_price
                sold_names[assetid] = name
            except (ValueError, TypeError):
                pass
    return sold, sold_names
def _norm_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = unicodedata.normalize("NFC", s)
    s = s.replace("(Factory New)", "(FN)").replace("(Minimal Wear)", "(MW)")
    s = s.replace("(Field-Tested)", "(FT)").replace("(Well-Worn)", "(WW)").replace("(Battle-Scarred)", "(BS)")
    return s.strip()
def _build_merged(inv_items: list, sold_map: dict, sold_names: dict, listing_assetids: set, listing_name_by_assetid: dict) -> tuple:
    all_assetids = set()
    name_to_candidates = defaultdict(list)
    for it in inv_items or []:
        aid = str(it.get("assetid") or "").strip()
        name = _norm_name(it.get("market_hash_name") or it.get("name"))
        if not aid:
            continue
        all_assetids.add(aid)
        if name:
            name_to_candidates[name].append({"assetid": aid, "source": "inventory", "sale_price": None})
    for aid, price in (sold_map or {}).items():
        aid = str(aid).strip()
        name = _norm_name((sold_names or {}).get(aid, ""))
        if not aid:
            continue
        all_assetids.add(aid)
        name_to_candidates[name].append({"assetid": aid, "source": "sold", "sale_price": price})
    for aid in listing_assetids or set():
        aid = str(aid).strip()
        name = _norm_name((listing_name_by_assetid or {}).get(aid, ""))
        if not aid:
            continue
        all_assetids.add(aid)
        name_to_candidates[name].append({"assetid": aid, "source": "listing", "sale_price": None})
    for name in name_to_candidates:
        name_to_candidates[name].sort(key=lambda x: (0 if x["source"] == "sold" else 1 if x["source"] == "listing" else 2, x["assetid"]))
    return all_assetids, name_to_candidates
def _record_name_counts(purchases: list) -> dict:
    out = defaultdict(int)
    for p in purchases:
        name = _norm_name(p.get("name") or "")
        if name:
            out[name] += 1
    return out
def _gather_candidates_for_record_name(record_name: str, name_to_candidates: dict) -> list:
    exact = list(name_to_candidates.get(record_name) or [])
    prefix_match = []
    for list_name, cands in name_to_candidates.items():
        if list_name == record_name:
            continue
        if record_name.startswith(list_name + " "):
            prefix_match.extend(cands)
    combined = exact + prefix_match
    combined.sort(key=lambda x: (0 if x["source"] == "sold" else 1 if x["source"] == "listing" else 2, x["assetid"]))
    return combined
def _filter_list_by_record_names(name_to_candidates: dict, record_name_counts: dict) -> dict:
    filtered = defaultdict(list)
    for name, need_count in record_name_counts.items():
        if need_count <= 0:
            continue
        candidates = _gather_candidates_for_record_name(name, name_to_candidates)[:need_count]
        filtered[name] = list(candidates)
    return filtered
def _pick_candidate(name_to_candidates: dict, name: str, used_assetids: set) -> dict:
    candidates = name_to_candidates.get(name) or []
    for c in candidates:
        if c["assetid"] not in used_assetids:
            return c
    return None
def _apply_candidate(purchase: dict, c: dict, sold_at: float) -> None:
    purchase["assetid"] = c["assetid"]
    if c["source"] == "sold":
        purchase["sale_price"] = c["sale_price"]
        purchase["sold_at"] = sold_at
        purchase["listing"] = False
        purchase["listing_status"] = None
    elif c["source"] == "listing":
        purchase["listing"] = True
        purchase["listing_status"] = None
        if "sale_price" not in purchase:
            purchase["sale_price"] = None
        if "sold_at" not in purchase:
            purchase["sold_at"] = None
    else:
        purchase["listing"] = False
        purchase["listing_status"] = None
        if "sale_price" not in purchase:
            purchase["sale_price"] = None
        if "sold_at" not in purchase:
            purchase["sold_at"] = None
def _clear_assetids(purchases: list) -> None:
    for p in purchases:
        if "assetid" in p:
            del p["assetid"]
        p["listing"] = False
        p["listing_status"] = None
        if "sale_price" in p:
            p["sale_price"] = None
        if "sold_at" in p:
            p["sold_at"] = None
def _refill_from_list(purchases: list, name_to_candidates: dict, sold_at: float) -> int:
    used_assetids = set()
    filled = 0
    for p in purchases:
        name = _norm_name(p.get("name") or "")
        if not name:
            continue
        c = _pick_candidate(name_to_candidates, name, used_assetids)
        if not c:
            continue
        _apply_candidate(p, c, sold_at)
        used_assetids.add(c["assetid"])
        filled += 1
    return filled
def run(log_fn=None):
    if not CREDENTIALS_FILE.exists():
        err = f"未找到 {CREDENTIALS_FILE}"
        if log_fn:
            log_fn(err, "error")
        return False, {"error": err}
    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        cred = json.load(f)
    steam = cred.get("steam") or {}
    cookies_str = steam.get("cookies") or ""
    if not cookies_str:
        return False, {"error": "credentials 中无 steam.cookies"}
    c = _cookies_to_dict(cookies_str)
    if not c.get("steamLoginSecure"):
        return False, {"error": "Cookie 中无 steamLoginSecure"}
    from app.state import get_purchases, get_sales, replace_transactions
    purchases = list(get_purchases() or [])
    sales = list(get_sales() or [])
    _clear_assetids(purchases)
    if log_fn:
        log_fn("已清空所有操作记录的 assetid", "info")
    inv_items = []
    if log_fn:
        log_fn("正在拉取 CS2 库存…", "info")
    try:
        from app.inventory_cs2 import scan_cs2_inventory
        ok, inv_items, err = scan_cs2_inventory()
        if not ok and log_fn:
            log_fn(f"拉取库存: {err}", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"拉取库存异常: {e}", "warn")
    if log_fn:
        log_fn("正在拉取 Steam 市场历史 Sold 记录…", "info")
    try:
        sold_map, sold_names = _fetch_sold_with_names(c)
        if log_fn:
            log_fn(f"解析到售出 {len(sold_map)} 条", "info")
    except Exception as e:
        if log_fn:
            log_fn(f"拉取售出历史异常: {e}", "error")
        return False, {"error": str(e)[:200]}
    if log_fn:
        log_fn("正在拉取出售中列表…", "info")
    listing_assetids = set()
    listing_name_by_assetid = {}
    try:
        from app.steam_listings import fetch_my_listings
        ok, listing_assetids, err, listing_name_by_assetid = fetch_my_listings(c, debug_fn=None)
        if ok:
            if log_fn:
                log_fn(f"在售 {len(listing_assetids)} 条", "info")
        elif log_fn:
            log_fn(f"拉取在售列表: {err}", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"拉取在售列表异常: {e}", "warn")
    _, name_to_candidates = _build_merged(inv_items, sold_map, sold_names, listing_assetids, listing_name_by_assetid)
    record_name_counts = _record_name_counts(purchases)
    name_to_candidates = _filter_list_by_record_names(name_to_candidates, record_name_counts)
    list_total = sum(len(cands) for cands in name_to_candidates.values())
    record_total = sum(1 for p in purchases if _norm_name(p.get("name") or ""))
    if log_fn:
        log_fn(f"按操作记录名称筛选列表后，列表条数 {list_total}，待填记录数 {record_total}", "info")
    sold_at = time.time()
    filled = _refill_from_list(purchases, name_to_candidates, sold_at)
    missing = sum(1 for p in purchases if not p.get("assetid"))
    if log_fn:
        log_fn(f"按名称从列表重新填入 {filled} 条，状态已更新（持有中/已出售/出售中）", "info")
        if missing:
            log_fn(f"未填入 {missing} 条（名称在列表中无匹配或该名称列表数量不足）", "warn")
    replace_transactions(purchases, sales)
    if log_fn:
        log_fn(f"已将修复后的交易记录保存到数据库", "info")
    if log_fn:
        log_fn("--- 列表（仅操作记录中有的名称）各饰品数量 ---", "info")
        total_items = 0
        for name in sorted(name_to_candidates.keys()):
            cnt = len(name_to_candidates[name])
            total_items += cnt
            log_fn(f"  {name}: {cnt} 个", "info")
        log_fn(f"列表合计: {total_items} 个，不同饰品: {len(name_to_candidates)} 种", "info")
    return True, {"filled": filled, "missing": missing, "total": len(purchases), "list_by_name": {k: len(v) for k, v in name_to_candidates.items()}}
def main():
    def log(msg, level="info"):
        print(f"[{level}] {msg}")
    ok, result = run(log_fn=log)
    if not ok:
        print("修复失败:", result.get("error", ""))
        sys.exit(1)
    filled = result.get("filled", 0)
    missing = result.get("missing", 0)
    total = result.get("total", 0)
    print(f"完成. 填入 {filled}/{total} 条，状态已更新（持有中/已出售/出售中）. 已保存.")
    if missing:
        print(f"未填入 {missing} 条，请检查名称是否与列表一致或列表是否拉全（库存/售出500条/在售）. ")
if __name__ == "__main__":
    main()
