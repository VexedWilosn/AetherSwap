"""Inventory routes."""
from fastapi import APIRouter
from app.state import get_inventory, get_inventory_meta, is_steam_background_allowed, log, set_inventory
from app.inventory_cs2 import scan_cs2_inventory
from app.pipeline import run_sell_phase_on_inventory_update
from app.config_loader import get_steam_credentials, load_app_config_validated
from app.shared_market import batch_fetch_price_details, batch_fetch_prices, get_market_price_context, get_steam_smart_price_cny
router = APIRouter()
def _current_account_info(account_id: str = "") -> dict:
    try:
        from app.accounts import get_account, get_current_account
        account = get_account(account_id) if account_id else get_current_account()
        account = account or {}
        resolved_account_id = (account.get("id") or "").strip()
        label = (
            account.get("display_name")
            or account.get("username")
            or account.get("steam_id")
            or resolved_account_id
            or "当前账号"
        )
        note = (account.get("account_note") or "").strip()
        return {"account_id": resolved_account_id, "account_label": label, "account_note": note}
    except Exception:
        return {"account_id": "", "account_label": "当前账号", "account_note": ""}
def _with_account_info(items: list, account_id: str = "") -> list:
    account = _current_account_info(account_id)
    accounts_by_id = {}
    try:
        from app.accounts import list_accounts
        accounts_by_id = {str(a.get("id") or ""): a for a in list_accounts()}
    except Exception:
        accounts_by_id = {}
    out = []
    for it in items or []:
        row = dict(it)
        row.setdefault("account_id", account["account_id"])
        row.setdefault("account_label", account["account_label"])
        row_account = accounts_by_id.get(str(row.get("account_id") or ""))
        if row_account:
            row["account_note"] = (row_account.get("account_note") or "").strip()
        elif not row.get("account_note"):
            row["account_note"] = account["account_note"]
        out.append(row)
    return out
def _inventory_response(items=None, *, cached: bool = False, message: str = "", account_id: str = "", **extra):
    response_items = get_inventory(account_id) if items is None else items
    out = {
        "items": _with_account_info(response_items, account_id),
        "cached": cached,
        "message": message,
        "inventory_meta": get_inventory_meta(account_id),
    }
    out.update(extra)
    return out
def _get_steam_smart_price_cny(session, market_hash_name: str, app_id: int = 730):
    return get_steam_smart_price_cny(session, market_hash_name, app_id=app_id)
def _enrich_inventory_with_steam_prices(items: list, old_items: list) -> None:
    """Fill lowest_price on inventory items using shared batch_fetch_prices."""
    old_price_by_name: dict = {}
    for it in (old_items or []):
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        p = it.get("lowest_price")
        if name and p is not None and float(p) > 0:
            if name not in old_price_by_name:
                old_price_by_name[name] = float(p)
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        it["lowest_price"] = old_price_by_name.get(name, 0)
    names = set()
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        if name:
            names.add(name)
    if not names:
        return
    prices = batch_fetch_prices(names)
    for it in items:
        name = (it.get("market_hash_name") or it.get("name") or "").strip()
        if name in prices:
            it["lowest_price"] = prices[name]
def _try_steam_auto_relogin(account_id: str = ""):
    from app.services.steam_auth import try_steam_auto_relogin
    return try_steam_auto_relogin(account_id=account_id)
@router.get("/api/inventory")
def api_inventory(refresh: bool = False, account_id: str = ""):
    if not refresh and get_inventory(account_id):
        return _inventory_response(cached=True, message="当前显示缓存库存", account_id=account_id)
    if refresh or not get_inventory(account_id):
        if not is_steam_background_allowed():
            return _inventory_response(
                cached=True,
                message="主流程正在使用 Steam 后台请求，当前返回缓存库存",
                account_id=account_id,
            )
        ok, items, err = scan_cs2_inventory(account_id=account_id)
        if not ok and err and "登录已过期" in err:
            success, status, msg = _try_steam_auto_relogin(account_id)
            if status == "busy":
                import time as _time
                log("inventory: 检测到另一个自动登录正在进行，等待完成后重试库存…", "info", category="steam")
                for _wait in range(7):
                    _time.sleep(5)
                    ok2, items2, err2 = scan_cs2_inventory(account_id=account_id)
                    if ok2:
                        old = get_inventory(account_id)
                        _enrich_inventory_with_steam_prices(items2, old)
                        set_inventory(items2, account_id=account_id)
                        run_sell_phase_on_inventory_update(items2)
                        log("inventory: 等待后库存获取成功", "info", category="steam")
                        return _inventory_response(account_id=account_id)
                    if not err2 or "登录已过期" not in err2:
                        break
                log("inventory: 等待其他登录完成超时，返回缓存库存", "warn", category="steam")
                return _inventory_response(cached=True, message="等待自动登录完成超时，返回缓存库存", account_id=account_id)
            if success:
                import time as _time
                log("auto_relogin: 登录成功，等待 Steam 服务端会话生效 (8s)…", "info", category="steam")
                _time.sleep(8)
                ok, items, err = scan_cs2_inventory(account_id=account_id)
                if ok:
                    old = get_inventory(account_id)
                    _enrich_inventory_with_steam_prices(items, old)
                    set_inventory(items, account_id=account_id)
                    run_sell_phase_on_inventory_update(items)
                    return _inventory_response(account_id=account_id)
                if err and "登录已过期" in err:
                    log("auto_relogin: 首次重试仍过期，再等 7 秒…", "info", category="steam")
                    _time.sleep(7)
                    ok, items, err = scan_cs2_inventory(account_id=account_id)
                    if ok:
                        old = get_inventory(account_id)
                        _enrich_inventory_with_steam_prices(items, old)
                        set_inventory(items, account_id=account_id)
                        run_sell_phase_on_inventory_update(items)
                        return _inventory_response(account_id=account_id)
                log(f"auto_relogin: 登录成功但库存获取仍失败: {err}，返回缓存", "warn", category="steam")
                return _inventory_response(cached=True, message=f"登录成功但库存获取仍失败: {err or '未知错误'}", account_id=account_id)
            out = _inventory_response([], error=err, auth_expired=True, account_id=account_id)
            if status == "need_2fa":
                out["auth_expired_reason"] = "need_2fa"
                out["error"] = "需要二次验证（验证码），请到库存页手动重新登录 Steam"
            elif status == "no_creds":
                out["auth_expired_reason"] = "no_creds"
            return out
        if not ok:
            out = _inventory_response([], error=err, account_id=account_id)
            if err and ("登录已过期" in err or "未配置" in err):
                out["auth_expired"] = True
            return out
        old = get_inventory(account_id)
        _enrich_inventory_with_steam_prices(items, old)
        set_inventory(items, account_id=account_id)
        run_sell_phase_on_inventory_update(items)
    return _inventory_response(account_id=account_id)
@router.get("/api/market-prices")
def api_market_prices(account_id: str = ""):
    """统一批量市场价查询接口.
    一次性查出库存（lowest_price）和持有饰品（current_market_price）所需的全部
    唯一物品名称，每个名称只发一次 Steam API 请求，然后返回给前端同时刷新两个视图。
    """
    if not is_steam_background_allowed():
        return {"prices": {}, "error": "Steam 后台请求不可用"}
    from app.state import get_purchases
    inv_items = get_inventory(account_id) or []
    inv_names = {
        (it.get("market_hash_name") or it.get("name") or "").strip()
        for it in inv_items
    }
    purchases = get_purchases() or []
    holdings_names = {
        (p.get("name") or "").strip()
        for p in purchases
        if not (p.get("sale_price") is not None and float(p.get("sale_price") or 0) > 0)
    }
    all_names = {n for n in (inv_names | holdings_names) if n}
    if not all_names:
        return {"prices": {}}
    details = batch_fetch_price_details(all_names)
    prices = {name: detail["price"] for name, detail in details.items()}
    sources = {name: detail.get("source") for name, detail in details.items()}
    price_meta = get_market_price_context()
    if any(detail.get("source") == "steam_lowest" for detail in details.values()):
        price_meta["fallback_used"] = True
        price_meta["warning"] = price_meta.get("warning") or "部分市场价使用 Steam 最低价/中位价摘要兜底，不等同智能挂单价。"
    return {"prices": prices, "sources": sources, "price_meta": price_meta}
