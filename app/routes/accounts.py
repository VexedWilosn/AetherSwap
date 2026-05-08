"""Account management routes."""
from typing import Any, Optional
from fastapi import APIRouter
from pydantic import BaseModel
from app.accounts import (
    add_account,
    delete_account,
    get_account,
    get_current_account,
    public_account,
    list_accounts,
    set_current,
    update_account,
)
from app.config_loader import load_app_config_validated
from app.services.steam_auth import verify_steam_auto_login
router = APIRouter()
class AccountBody(BaseModel):
    enabled: bool = True
    username: str = ""
    password: str = ""
    steam_id: str = ""
    display_name: str = ""
    account_note: str = ""
    avatar_url: str = ""
    steam_guard: Optional[dict[str, Any]] = None
class AccountUpdateBody(BaseModel):
    enabled: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None
    steam_id: Optional[str] = None
    display_name: Optional[str] = None
    account_note: Optional[str] = None
    avatar_url: Optional[str] = None
    steam_guard: Optional[dict[str, Any]] = None
    trade_config: Optional[dict[str, Any]] = None
@router.get("/api/accounts")
def api_list_accounts():
    cfg = load_app_config_validated()
    from app.account_sessions import public_session_summary
    accs = []
    for account in list_accounts():
        out = public_account(account, cfg)
        out["session_status"] = public_session_summary(account.get("id"))
        accs.append(out)
    cid = get_current_account()
    current_id = cid.get("id") if cid else None
    return {"accounts": accs, "current_id": current_id}
@router.post("/api/accounts")
def api_add_account(body: AccountBody):
    acc = add_account(
        username=body.username,
        password=body.password,
        steam_id=body.steam_id,
        display_name=body.display_name,
        account_note=body.account_note,
        avatar_url=body.avatar_url,
    )
    if body.steam_guard is not None:
        acc = update_account(acc["id"], steam_guard=body.steam_guard) or acc
    if body.enabled is not True:
        acc = update_account(acc["id"], enabled=body.enabled) or acc
    out = public_account(acc, load_app_config_validated())
    from app.account_sessions import public_session_summary
    out["session_status"] = public_session_summary(acc.get("id"))
    return {"ok": True, "account": out}
@router.put("/api/accounts/{account_id}")
def api_update_account(account_id: str, body: AccountUpdateBody):
    kwargs = {}
    if body.enabled is not None:
        kwargs["enabled"] = body.enabled
    if body.username is not None:
        kwargs["username"] = body.username
    if body.password is not None and body.password:
        kwargs["password"] = body.password
    if body.steam_id is not None:
        kwargs["steam_id"] = body.steam_id
    if body.display_name is not None:
        kwargs["display_name"] = body.display_name
    if body.account_note is not None:
        kwargs["account_note"] = body.account_note
    if body.avatar_url is not None:
        kwargs["avatar_url"] = body.avatar_url
    if body.steam_guard is not None:
        kwargs["steam_guard"] = body.steam_guard
    if body.trade_config is not None:
        kwargs["trade_config"] = body.trade_config
    acc = update_account(account_id, **kwargs) if kwargs else get_account(account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    out = public_account(acc, load_app_config_validated())
    from app.account_sessions import public_session_summary
    out["session_status"] = public_session_summary(acc.get("id"))
    return {"ok": True, "account": out}
@router.delete("/api/accounts/{account_id}")
def api_delete_account(account_id: str):
    ok = delete_account(account_id)
    return {"ok": ok, "error": None if ok else "删除失败"}
@router.post("/api/accounts/{account_id}/set_current")
def api_set_current_account(account_id: str):
    ok = set_current(account_id)
    return {"ok": ok, "error": None if ok else "账号不存在"}
@router.post("/api/accounts/{account_id}/verify")
def api_verify_account(account_id: str):
    result = verify_steam_auto_login(account_id)
    return {"ok": result.get("ok", False), "status": result.get("status", "error"), "message": result.get("message", "验证失败")}

@router.post("/api/accounts/{account_id}/sync_balance")
def api_sync_account_balance(account_id: str):
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    login = verify_steam_auto_login(account_id)
    if not login.get("ok"):
        return {
            "ok": False,
            "status": login.get("status", "error"),
            "error": login.get("message", "无法登录账号，余额同步失败"),
        }
    try:
        from app.services.account_region import sync_account_currency_region
        result = sync_account_currency_region(account_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "余额同步失败")}
    out = public_account(result["account"], load_app_config_validated())
    from app.account_sessions import public_session_summary
    out["session_status"] = public_session_summary(result["account"].get("id"))
    return {"ok": True, "account": out}

@router.post("/api/accounts/{account_id}/sessions/{provider}/refresh")
def api_refresh_account_session(account_id: str, provider: str):
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    provider = (provider or "").strip().lower()
    if provider == "steam":
        result = verify_steam_auto_login(account_id)
        ok = bool(result.get("ok"))
        msg = result.get("message", "验证完成" if ok else "验证失败")
    elif provider == "buff":
        from app.services.buff_auth import try_buff_auto_relogin
        ok, status, msg = try_buff_auto_relogin(account_id=account_id, force=True)
        result = {"ok": ok, "status": status, "message": msg}
    else:
        return {"ok": False, "error": "provider 须为 steam 或 buff"}
    from app.account_sessions import public_session_summary
    return {
        "ok": ok,
        "status": result.get("status", "ok" if ok else "error"),
        "message": msg,
        "session_status": public_session_summary(account_id),
    }

@router.delete("/api/accounts/{account_id}/sessions/{provider}")
def api_clear_account_session(account_id: str, provider: str):
    if not get_account(account_id):
        return {"ok": False, "error": "账号不存在"}
    provider = (provider or "").strip().lower()
    if provider not in {"steam", "buff"}:
        return {"ok": False, "error": "provider 须为 steam 或 buff"}
    from app.account_sessions import clear_account_session, public_session_summary
    ok = clear_account_session(account_id, provider)
    return {"ok": ok, "session_status": public_session_summary(account_id)}
