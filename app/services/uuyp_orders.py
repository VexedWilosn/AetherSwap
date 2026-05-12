from __future__ import annotations

import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from app.state import append_purchase, get_purchases, log
from uuyp.buyer import UuypBuyer


@dataclass
class ManualDirectState:
    enabled: bool = False
    paused: bool = True
    last_url: str = ""
    last_listing: Optional[dict] = None
    last_template_id: str = ""
    last_market_hash_name: str = ""
    last_target_price: Optional[float] = None
    opened_at: Optional[float] = None
    message: str = "manual direct navigator is paused"
    browser_running: bool = False


_state = ManualDirectState()
_lock = threading.Lock()
_playwright = None
_browser_context = None
_browser_page = None


def get_manual_status() -> dict:
    with _lock:
        data = asdict(_state)
    data["browser_running"] = _browser_context is not None
    return data


def set_manual_control(enabled: Optional[bool] = None, paused: Optional[bool] = None) -> dict:
    with _lock:
        if enabled is not None:
            _state.enabled = bool(enabled)
            if enabled and paused is None:
                _state.paused = False
            if not enabled and paused is None:
                _state.paused = True
        if paused is not None:
            _state.paused = bool(paused)
            if not paused:
                _state.enabled = True
        _state.message = "manual direct navigator ready" if _state.enabled and not _state.paused else "manual direct navigator is paused"
    return get_manual_status()


def _ok_code(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("code", "Code"):
        if key in data:
            return data.get(key) == 0
    return bool(data.get("ok") is True or data.get("success") is True)


def _message(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    return str(data.get("msg") or data.get("Msg") or data.get("message") or data.get("error") or "")


def _find_order_no(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in ("orderNo", "purchaseNo", "orderId", "id"):
                val = cur.get(key)
                if val:
                    return str(val)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return ""


def _resolve_template_id(buyer: UuypBuyer, market_hash_name: str, template_id: str = "") -> str:
    tid = str(template_id or "").strip()
    if tid:
        return tid
    found = buyer.search_item_id_by_name(market_hash_name)
    return str(found or "").strip()


def _purchase_record(
    market_hash_name: str,
    template_id: str,
    price: float,
    *,
    order_no: str = "",
    listing_status_prefix: str = "uuyp_purchase_order",
    market_price: Optional[float] = None,
) -> dict:
    rec: dict[str, Any] = {
        "name": market_hash_name,
        "goods_id": int(template_id) if str(template_id).isdigit() else 0,
        "price": round(float(price), 2),
        "at": time.time(),
        "pending_receipt": True,
        "listing": False,
        "listing_status": f"{listing_status_prefix}:{order_no}" if order_no else listing_status_prefix,
    }
    if market_price is not None and float(market_price) > 0:
        rec["market_price"] = round(float(market_price), 2)
    return rec


def submit_purchase_order(
    market_hash_name: str,
    price: float,
    quantity: int = 1,
    template_id: str = "",
    *,
    buyer: Optional[UuypBuyer] = None,
) -> dict:
    name = (market_hash_name or "").strip()
    if not name:
        return {"ok": False, "error": "market_hash_name is required"}
    if float(price) <= 0:
        return {"ok": False, "error": "price must be greater than 0"}
    qty = max(1, int(quantity or 1))
    client = buyer or UuypBuyer()
    tid = _resolve_template_id(client, name, template_id)
    if not tid:
        return {"ok": False, "error": "UUYP template id not found"}
    data = client.create_buy_order(tid, round(float(price), 2), qty, market_hash_name=name, commodity_name=name)
    if not _ok_code(data):
        return {"ok": False, "error": _message(data) or "UUYP purchase order failed", "raw": data}
    order_no = _find_order_no(data)
    for _ in range(qty):
        append_purchase(_purchase_record(name, tid, price, order_no=order_no))
    log(f"UUYP purchase order submitted: {name} x{qty} @ {price}", "info", category="uuyp")
    return {"ok": True, "template_id": tid, "order_no": order_no, "quantity": qty, "raw": data}


def _open_persistent_browser(url: str) -> dict:
    global _playwright, _browser_context, _browser_page
    try:
        from playwright.sync_api import sync_playwright

        if _browser_context is None:
            _playwright = sync_playwright().start()
            profile = Path(__file__).resolve().parent.parent.parent / "config" / "playwright_uuyp_manual"
            profile.mkdir(parents=True, exist_ok=True)
            _browser_context = _playwright.chromium.launch_persistent_context(str(profile), headless=False)
            _browser_page = _browser_context.pages[0] if _browser_context.pages else _browser_context.new_page()
            _seed_cookies(_browser_context)
        elif _browser_page is None or _browser_page.is_closed():
            _browser_page = _browser_context.new_page()
        _browser_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            _browser_page.bring_to_front()
        except Exception:
            pass
        return {"ok": True, "browser": "playwright"}
    except Exception as exc:
        try:
            webbrowser.open(url, new=1)
            return {"ok": True, "browser": "webbrowser", "warning": str(exc)}
        except Exception as fallback_exc:
            return {"ok": False, "error": f"{exc}; fallback failed: {fallback_exc}"}


def _seed_cookies(context) -> None:
    try:
        from config import get

        cookie_str = str((get("uuyp", default={}) or {}).get("cookies") or "")
    except Exception:
        cookie_str = ""
    cookies = []
    for part in cookie_str.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".youpin898.com", "path": "/"})
    if cookies:
        context.add_cookies(cookies)


def prepare_manual_direct(
    market_hash_name: str,
    target_price: float,
    quantity: int = 1,
    template_id: str = "",
    *,
    open_browser: bool = True,
    buyer: Optional[UuypBuyer] = None,
) -> dict:
    with _lock:
        if not _state.enabled or _state.paused:
            return {"ok": False, "error": "UUYP manual direct navigator is paused"}
    name = (market_hash_name or "").strip()
    if not name:
        return {"ok": False, "error": "market_hash_name is required"}
    client = buyer or UuypBuyer()
    tid = _resolve_template_id(client, name, template_id)
    if not tid:
        return {"ok": False, "error": "UUYP template id not found"}
    listing = None
    try:
        listing = client.select_best_listing(tid, max_price=float(target_price) if target_price else None)
    except Exception as exc:
        log(f"UUYP listing lookup failed before manual navigation: {exc}", "warn", category="uuyp")
    if target_price and listing is None:
        return {"ok": False, "error": "UUYP listing not found within target price", "template_id": tid}
    url = UuypBuyer.market_url(tid)
    browser_result = {"ok": True, "browser": "not_opened"}
    if open_browser:
        browser_result = _open_persistent_browser(url)
        if not browser_result.get("ok"):
            return {"ok": False, "error": browser_result.get("error") or "failed to open browser", "url": url}
    with _lock:
        _state.last_url = url
        _state.last_listing = listing
        _state.last_template_id = tid
        _state.last_market_hash_name = name
        _state.last_target_price = float(target_price) if target_price else None
        _state.opened_at = time.time()
        _state.message = "manual checkout page opened"
    return {
        "ok": True,
        "url": url,
        "template_id": tid,
        "listing": listing,
        "quantity": max(1, int(quantity or 1)),
        "browser": browser_result.get("browser"),
        "warning": browser_result.get("warning"),
    }


def record_manual_direct_order(
    market_hash_name: str,
    price: float,
    quantity: int = 1,
    template_id: str = "",
    order_no: str = "",
) -> dict:
    name = (market_hash_name or "").strip()
    with _lock:
        tid = str(template_id or _state.last_template_id or "").strip()
        if not name:
            name = _state.last_market_hash_name
        if not price and _state.last_target_price:
            price = _state.last_target_price
    if not name:
        return {"ok": False, "error": "market_hash_name is required"}
    if float(price) <= 0:
        return {"ok": False, "error": "price must be greater than 0"}
    if not tid:
        tid = "0"
    qty = max(1, int(quantity or 1))
    for _ in range(qty):
        append_purchase(
            _purchase_record(
                name,
                tid,
                float(price),
                order_no=order_no,
                listing_status_prefix="uuyp_manual_direct",
            )
        )
    log(f"UUYP manual direct order recorded: {name} x{qty} @ {price}", "info", category="uuyp")
    return {"ok": True, "template_id": tid, "quantity": qty}


def list_uuyp_order_records() -> dict:
    rows = []
    for item in get_purchases() or []:
        status = str(item.get("listing_status") or "")
        if status.startswith("uuyp_purchase_order") or status.startswith("uuyp_manual_direct"):
            rows.append(item)
    return {"ok": True, "orders": rows}
