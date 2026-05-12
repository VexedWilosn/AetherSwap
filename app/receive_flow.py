import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import requests
from utils.delay import jittered_sleep
def steam_request(retries: int, fn: Callable[[], requests.Response]) -> requests.Response:
    last_exc: Optional[Exception] = None
    attempts = max(1, int(retries) or 1)
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt + 1 < attempts:
                jittered_sleep(1)
    raise last_exc
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
BUFF_STEAM_TRADE_URL = "https://buff.163.com/api/market/steam_trade"
STEAM_ACCEPT_REFERER = "https://steamcommunity.com/tradeoffer/{trade_offer_id}/"
STEAM_ACCEPT_URL = "https://steamcommunity.com/tradeoffer/{trade_offer_id}/accept"
def _cookies_str_to_dict(cookie_str: str) -> Dict[str, str]:
    out = {}
    for part in (cookie_str or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out
def fetch_buff_steam_trade(buff_cookies: str) -> Tuple[bool, List[Dict[str, Any]], str]:
    from utils.proxy_manager import get_proxy_manager
    pm = get_proxy_manager()
    try:
        cookies = _cookies_str_to_dict(buff_cookies)
        if not cookies:
            return False, [], "未配置 Buff cookies"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "Referer": "https://buff.163.com/market/buy_order/to_receive?game=csgo",
            "X-Requested-With": "XMLHttpRequest",
        }
        proxies = pm.get_proxies_for_request()
        r = requests.get(BUFF_STEAM_TRADE_URL, headers=headers, cookies=cookies, proxies=proxies, verify=False, timeout=10)
        try:
            data = r.json() if r.text else {}
        except (ValueError, TypeError):
            return False, [], f"JSON 解析失败, status={r.status_code}"
        if data.get("code") != "OK":
            return False, [], data.get("msg", "请求失败")
        raw = data.get("data") or []
        if not isinstance(raw, list):
            return False, [], "数据格式异常"
        pending = []
        for x in raw:
            if x.get("state") != 1 or not x.get("tradeofferid"):
                continue
            created_at = int(x.get("created_at", 0)) if x.get("created_at") is not None else 0
            goods_list = x.get("items_to_trade") or []
            if not goods_list:
                continue
            items_in_trade = []
            for g in goods_list:
                asset_id = str(g.get("assetid", ""))
                gid = str(g.get("goods_id", ""))
                goods_id_buff = None
                if gid and gid != "0":
                    try:
                        goods_id_buff = int(gid)
                    except (ValueError, TypeError):
                        pass
                info = (x.get("goods_infos") or {}).get(gid) or {}
                if isinstance(info, dict):
                    item_name = info.get("name", "未知物品") or "未知物品"
                    market_hash_name = (info.get("market_hash_name") or "").strip()
                else:
                    item_name = "未知物品"
                    market_hash_name = ""
                items_in_trade.append({
                    "assetid": asset_id,
                    "name": item_name,
                    "market_hash_name": market_hash_name,
                    "goods_id": goods_id_buff,
                })
            pending.append({
                "tradeofferid": x.get("tradeofferid"),
                "created_at": created_at,
                "items": items_in_trade,
            })
        return True, pending, ""
    except Exception as e:
        return False, [], str(e)[:120]
def _classify_steam_offer_accept_failure(*, status_code: int = 0, body: dict | None = None, text: str = "") -> str:
    raw = " ".join(
        str(value or "")
        for value in (
            status_code,
            text,
            (body or {}).get("strError") if isinstance(body, dict) else "",
            (body or {}).get("message") if isinstance(body, dict) else "",
            (body or {}).get("eresult") if isinstance(body, dict) else "",
        )
    ).lower()
    if status_code in {401, 403} or any(token in raw for token in ("login", "session", "not logged", "access denied")):
        return "steam_auth_required"
    if status_code == 404 or any(token in raw for token in ("expired", "not available", "invalid trade offer", "not found", "already")):
        return "offer_expired"
    if any(token in raw for token in ("declined", "rejected", "cancelled", "canceled")):
        return "offer_declined"
    if any(token in raw for token in ("mobile", "confirmation", "confirm")):
        return "mobile_confirm_required"
    if status_code == 429 or "rate" in raw or "timeout" in raw:
        return "transient_error"
    return "trade_offer_accept_failed"


def accept_steam_trade_offer_result(trade_offer_id: str, steam_cookies: Dict[str, str]) -> dict:
    from utils.proxy_manager import get_proxy_manager
    pm = get_proxy_manager()
    try:
        url = STEAM_ACCEPT_URL.format(trade_offer_id=trade_offer_id)
        referer = STEAM_ACCEPT_REFERER.format(trade_offer_id=trade_offer_id)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "Origin": "https://steamcommunity.com",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
        }
        session_id = steam_cookies.get("sessionid", "").strip()
        data = {
            "sessionid": session_id,
            "serverid": "1",
            "tradeofferid": str(trade_offer_id),
            "partner": "",
            "captcha": "",
        }
        context = {"attempt": 0}
        def _call():
            context["attempt"] += 1
            proxies = pm.get_proxies_for_request(failed=context["attempt"] > 1)
            return requests.post(url, headers=headers, cookies=steam_cookies, proxies=proxies, data=data, verify=False, timeout=15)
        r = steam_request(3, _call)
        if r.status_code != 200:
            reason = _classify_steam_offer_accept_failure(status_code=r.status_code, text=r.text or "")
            return {"success": False, "reason": reason, "status_code": r.status_code, "body": (r.text or "")[:500]}
        raw_text = (r.text or "").strip()
        if not raw_text:
            return {"success": False, "reason": "empty_response", "status_code": r.status_code}
        try:
            body = r.json()
        except Exception:
            reason = _classify_steam_offer_accept_failure(status_code=r.status_code, text=raw_text)
            return {"success": False, "reason": reason, "status_code": r.status_code, "body": raw_text[:500]}
        if not isinstance(body, dict):
            return {"success": False, "reason": "malformed_response", "status_code": r.status_code, "body": raw_text[:500]}
        if body.get("tradeid"):
            return {"success": True, "reason": "", "tradeid": body.get("tradeid"), "status_code": r.status_code, "body": body}
        if body.get("strError"):
            reason = _classify_steam_offer_accept_failure(status_code=r.status_code, body=body, text=raw_text)
            return {"success": False, "reason": reason, "status_code": r.status_code, "body": body}
        success = "tradeid" in body or body.get("success") == 1
        reason = "" if success else _classify_steam_offer_accept_failure(status_code=r.status_code, body=body, text=raw_text)
        return {"success": bool(success), "reason": reason, "status_code": r.status_code, "body": body}
    except requests.Timeout:
        return {"success": False, "reason": "transient_error", "msg": "steam offer accept timeout"}
    except Exception as exc:
        return {"success": False, "reason": "trade_offer_accept_exception", "msg": str(exc)[:300]}


def accept_steam_trade_offer(trade_offer_id: str, steam_cookies: Dict[str, str]) -> bool:
    return bool(accept_steam_trade_offer_result(trade_offer_id, steam_cookies).get("success"))
def _match_purchase_for_item(
    item: dict,
    pending_purchases: List[dict],
    assigned_db_ids: set,
) -> Optional[dict]:
    """Return the best matching purchase record dict (containing _db_id), or None.
    Matching priority:
      1. goods_id exact match (most reliable)
      2. name substring match (fallback)
    Among candidates, prefer the oldest (smallest ``at`` timestamp).
    """
    goods_id_buff = item.get("goods_id")
    name_for_match = (item.get("market_hash_name") or item.get("name") or "").strip()
    candidates: List[dict] = []
    for p in pending_purchases:
        db_id = p.get("_db_id")
        if not db_id or db_id in assigned_db_ids:
            continue
        if p.get("assetid"):  
            continue
        if goods_id_buff is not None and p.get("goods_id") is not None:
            try:
                if int(goods_id_buff) == int(p.get("goods_id")):
                    candidates.append(p)
                    continue
            except (ValueError, TypeError):
                pass
        pname = (p.get("name") or "").strip()
        if pname and name_for_match and (pname == name_for_match or name_for_match in pname or pname in name_for_match):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x.get("at") or 0))
    return candidates[0]
def try_receive_once(
    get_purchases: Callable[[], List[dict]],
    update_purchase: Callable[[int, dict], bool],
    get_buff_cookies: Callable[[], str],
    get_steam_credentials: Callable[[], dict],
    scan_inventory: Optional[Callable[[], Tuple[bool, List[dict], str]]] = None,
    update_purchase_by_id: Optional[Callable[[int, dict], bool]] = None,
) -> int:
    # NOTE: 保留接收逻辑实现，但当前启动流程已不再自动调度该循环任务。
    """Accept pending Buff→Steam trade offers and update purchase records.
    Uses ``update_purchase_by_id`` (O(1), keyed on SQLite primary key) when
    available to avoid the race condition where positional indices shift
    between the time they are read and when the update is applied.
    Falls back to positional ``update_purchase`` only if``update_purchase_by_id``
    is not supplied (backward-compatibility).
    """
    purchases = get_purchases()
    pending_records: List[dict] = [
        p for p in purchases
        if p.get("pending_receipt") and not p.get("assetid") and p.get("_db_id")
    ]
    if not pending_records:
        return 0
    buff_cookies = get_buff_cookies()
    steam_cred = get_steam_credentials()
    steam_cookies_str = steam_cred.get("cookies") or ""
    steam_cookies = _cookies_str_to_dict(steam_cookies_str)
    session_id = (steam_cred.get("session_id") or "").strip()
    if session_id:
        steam_cookies["sessionid"] = session_id
    if not steam_cookies.get("sessionid") or not steam_cookies.get("steamLoginSecure"):
        return 0
    ok, pending_tasks, err = fetch_buff_steam_trade(buff_cookies)
    if not ok or not pending_tasks:
        return 0
    pending_tasks = sorted(pending_tasks, key=lambda t: (t.get("created_at") or 0, t.get("tradeofferid") or ""))
    received = 0
    def _do_update(db_id: int, positional_idx: int, data: dict) -> bool:
        """Update a purchase record, preferring _db_id-based O(1) update."""
        if update_purchase_by_id and db_id:
            return update_purchase_by_id(db_id, data)
        return update_purchase(positional_idx, data)
    for task in pending_tasks:
        offer_id = task.get("tradeofferid")
        if not offer_id:
            continue
        if not accept_steam_trade_offer(str(offer_id), steam_cookies):
            continue
        received += 1
        if scan_inventory:
            jittered_sleep(2)
        purchases = get_purchases()
        pending_records = [
            p for p in purchases
            if p.get("pending_receipt") and not p.get("assetid") and p.get("_db_id")
        ]
        assigned_db_ids: set = set()
        pairs: List[Tuple[dict, dict]] = []  
        for it in task.get("items") or []:
            matched = _match_purchase_for_item(it, pending_records, assigned_db_ids)
            if matched is not None:
                assigned_db_ids.add(matched["_db_id"])
                pairs.append((matched, it))
        pairs.sort(key=lambda x: (x[0].get("at") or 0, x[0].get("_db_id") or 0))
        already_used = {str(p.get("assetid")) for p in get_purchases() if p.get("assetid")}
        inv_by_name: Dict[str, List[dict]] = {}
        if scan_inventory:
            ok_inv, inv_list, _ = scan_inventory()
            if ok_inv and inv_list:
                for inv_item in inv_list:
                    aid = str(inv_item.get("assetid") or "")
                    if not aid or aid in already_used:
                        continue
                    mhn = (inv_item.get("market_hash_name") or "").strip()
                    if mhn:
                        inv_by_name.setdefault(mhn, []).append(inv_item)
                for mhn in inv_by_name:
                    inv_by_name[mhn].sort(key=lambda x: x.get("assetid") or "")
        for purchase_rec, it in pairs:
            mhn = (it.get("market_hash_name") or "").strip()
            our_assetid = None
            if mhn and inv_by_name.get(mhn):
                for inv_item in inv_by_name[mhn][:]:
                    aid = str(inv_item.get("assetid") or "")
                    if aid in already_used:
                        continue
                    our_assetid = aid
                    already_used.add(aid)
                    inv_by_name[mhn].remove(inv_item)
                    break
            if not our_assetid:
                our_assetid = (it.get("assetid") or "").strip()
            if our_assetid:
                db_id = purchase_rec.get("_db_id") or 0
                pos_idx = next(
                    (i for i, p in enumerate(purchases) if p.get("_db_id") == db_id),
                    -1,
                )
                _do_update(db_id, pos_idx, {"assetid": our_assetid, "pending_receipt": False})
                already_used.add(our_assetid)
        jittered_sleep(1)
    return received
