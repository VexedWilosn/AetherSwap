"""Auth routes – Steam & Buff relogin, Steam Guard."""
import base64
import hashlib
import hmac
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel
from app.state import log, set_buff_auth_expired, set_buff_verification_required
from app.config_loader import (
    get_steam_credentials,
    load_app_config_validated,
    update_buff_creds,
    update_steam_creds,
)
from app.accounts import get_current_account, get_profile_dir, set_current, update_account
from app.services.steam_auth import (
    fetch_steam_profile_via_api,
    try_steam_auto_relogin,
)
from app.runtime_env import get_runtime_profile
router = APIRouter()
_relogin_lock = threading.Lock()
_relogin_type = None
_relogin_playwright = None
_relogin_browser = None
_relogin_context = None
_relogin_ready = threading.Event()
_relogin_wake = threading.Event()
_relogin_done = threading.Event()
_relogin_success = False
_relogin_error = None
def _manual_cookie_required_response(relogin_type: str, reason: str):
    label = "Steam" if relogin_type == "steam" else "Buff"
    return {
        "ok": False,
        "code": "manual_cookie_required",
        "manual_cookie_required": True,
        "error": f"{label} 登录需要图形浏览器，但当前运行环境不支持。请使用手动 Cookie 登录。",
        "reason": reason,
        "runtime": get_runtime_profile().as_dict(),
    }
class ReloginFinishBody(BaseModel):
    success: bool
class ManualCookieBody(BaseModel):
    cookies: str
    session_id: str = ""
    steam_id: str = ""
def _normalize_cookie_input(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I)
    pieces = []
    for part in re.split(r";|\r?\n", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            pieces.append(f"{name}={value}")
    return "; ".join(pieces)
def _cookie_value(cookie_str: str, name: str) -> str:
    wanted = name.lower()
    for part in (cookie_str or "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip().lower() == wanted:
            return value.strip()
    return ""
def _steam_id_from_cookie_str(cookie_str: str) -> str:
    value = _cookie_value(cookie_str, "steamLoginSecure")
    if "%7C%7C" in value:
        return value.split("%7C%7C", 1)[0].strip()
    if "||" in value:
        return value.split("||", 1)[0].strip()
    return value.strip() if value.strip().isdigit() else ""
def _maybe_resume_after_buff_cookie_update() -> None:
    set_buff_auth_expired(False)
    set_buff_verification_required(False)
    try:
        from app.state import get_status
        from app.pipeline import start_pipeline
        st = get_status()
        err_msg = str(st.get("step") or "")
        if st.get("status") == "error" and err_msg in ("BUFF_AUTH_EXPIRED", "BUFF_VERIFICATION_REQUIRED"):
            log("检测到 Buff Cookie 已手动更新，尝试自动恢复流水线...", "info", category="system")
            try:
                start_pipeline(load_app_config_validated())
            except Exception as resume_err:
                log(f"自动恢复流水线失败: {resume_err}", "warn", category="system")
    except Exception as resume_err:
        log(f"Buff Cookie 更新后的恢复检查失败: {resume_err}", "warn", category="system")
def _relogin_worker(relogin_type: str) -> None:
    global _relogin_playwright, _relogin_browser, _relogin_context, _relogin_error, _relogin_success
    try:
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        if relogin_type == "steam":
            cur = get_current_account()
            profile_dir = get_profile_dir(cur.get("id") if cur else None)
        else:
            profile_dir = Path(__file__).resolve().parent.parent.parent / "config" / "playwright_buff"
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        url = "https://store.steampowered.com/login/" if relogin_type == "steam" else "https://buff.163.com/"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if relogin_type == "steam":
            pass
        with _relogin_lock:
            _relogin_playwright, _relogin_browser, _relogin_context = p, None, context
        _relogin_ready.set()
        _relogin_wake.wait()
        if _relogin_success:
            if relogin_type == "steam":
                try:
                    page.goto("https://steamcommunity.com/market/", wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass
            cookies = context.cookies()
            if relogin_type == "steam":
                steam_cookies = [c for c in cookies if "steamcommunity" in (c.get("domain") or "") or "steampowered" in (c.get("domain") or "")]
                selected = steam_cookies if steam_cookies else cookies
                has_secure = any(c.get("name") == "steamLoginSecure" for c in selected)
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in selected)
                session_id = next((c["value"] for c in selected if c.get("name") == "sessionid"), None) or next((c["value"] for c in cookies if c.get("name") == "sessionid"), None)
                if session_id and has_secure:
                    update_steam_creds(cookie_str, session_id)
                cur = get_current_account()
                if cur:
                    steam_id = None
                    for c in cookies:
                        if c.get("name") == "steamLoginSecure":
                            v = c.get("value", "")
                            if "%7C%7C" in v:
                                steam_id = v.split("%7C%7C")[0].strip()
                            elif "||" in v:
                                steam_id = v.split("||")[0].strip()
                            break
                    display_name, avatar_url = fetch_steam_profile_via_api(steam_id or "", cookie_str)
                    update_account(cur["id"], steam_id=steam_id or "", display_name=display_name, avatar_url=avatar_url)
            else:
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                update_buff_creds(cookie_str)
                set_buff_auth_expired(False)
                set_buff_verification_required(False)
                from app.state import get_status
                from app.pipeline import start_pipeline
                from app.config_loader import load_app_config_validated
                st = get_status()
                err_msg = str(st.get("step") or "")
                if st.get("status") == "error" and err_msg in ("BUFF_AUTH_EXPIRED", "BUFF_VERIFICATION_REQUIRED"):
                    from app.state import log
                    log("检测到 Buff 状态已更新，尝试自动恢复挂刀流水线...", "info", category="system")
                    try:
                        start_pipeline(load_app_config_validated())
                    except Exception as resume_err:
                        log(f"自动恢复流水线失败: {resume_err}", "warn", category="system")
        try:
            context.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass
    except Exception as e:
        _relogin_error = str(e)
        _relogin_ready.set()
    finally:
        with _relogin_lock:
            _relogin_playwright = None
            _relogin_browser = None
            _relogin_context = None
        _relogin_done.set()
def _relogin_start(relogin_type: str):
    global _relogin_type, _relogin_error, _relogin_success, _relogin_playwright, _relogin_browser, _relogin_context
    profile = get_runtime_profile()
    if not profile.can_launch_headful_browser:
        return _manual_cookie_required_response(relogin_type, profile.reason)
    with _relogin_lock:
        if _relogin_context or _relogin_browser:
            try:
                if _relogin_browser:
                    _relogin_browser.close()
                elif _relogin_context:
                    _relogin_context.close()
            except Exception:
                pass
            try:
                if _relogin_playwright:
                    _relogin_playwright.stop()
            except Exception:
                pass
            _relogin_playwright = None
            _relogin_browser = None
            _relogin_context = None
        _relogin_type = relogin_type
        _relogin_error = None
        _relogin_success = False
    _relogin_ready.clear()
    _relogin_done.clear()
    _relogin_wake.clear()
    t = threading.Thread(target=_relogin_worker, args=(relogin_type,), daemon=True)
    t.start()
    if not _relogin_ready.wait(timeout=60):
        return {"ok": False, "error": "打开浏览器超时"}
    if _relogin_error:
        return {"ok": False, "error": _relogin_error}
    msg = "请在弹出的浏览器中完成 Steam 登录" if relogin_type == "steam" else "请在弹出的浏览器中完成 Buff 登录/验证"
    return {"ok": True, "message": msg}
def _relogin_finish(success: bool):
    global _relogin_success, _relogin_context
    with _relogin_lock:
        if not _relogin_context:
            return {"ok": False, "error": "未在重新登录流程中"}
        _relogin_success = success
    _relogin_wake.set()
    _relogin_done.wait(timeout=15)
    return {"ok": True}
def _normalize_secret(raw: str) -> str:
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), raw)
def _generate_steam_guard_code(shared_secret: str) -> Optional[str]:
    if not shared_secret:
        return None
    secret_str = _normalize_secret(shared_secret.strip())
    try:
        secret_bytes = base64.b64decode(secret_str)
    except Exception:
        return None
    ts = int(time.time())
    time_buffer = struct.pack(">Q", ts // 30)
    hmac_hash = hmac.new(secret_bytes, time_buffer, hashlib.sha1).digest()
    offset = hmac_hash[19] & 0xF
    code_int = struct.unpack(">I", hmac_hash[offset : offset + 4])[0] & 0x7FFFFFFF
    chars = "23456789BCDFGHJKMNPQRTVWXY"
    out = []
    for _ in range(5):
        out.append(chars[code_int % 26])
        code_int //= 26
    return "".join(out)
@router.post("/api/auth/steam/relogin_start")
def api_auth_steam_relogin_start():
    return _relogin_start("steam")
@router.post("/api/auth/steam/relogin_finish")
def api_auth_steam_relogin_finish(body: ReloginFinishBody):
    return _relogin_finish(body.success)
@router.post("/api/auth/buff/relogin_start")
def api_auth_buff_relogin_start():
    return _relogin_start("buff")
@router.post("/api/auth/buff/relogin_finish")
def api_auth_buff_relogin_finish(body: ReloginFinishBody):
    return _relogin_finish(body.success)
@router.post("/api/auth/{relogin_type}/manual_cookie")
def api_auth_manual_cookie(relogin_type: str, body: ManualCookieBody):
    cookie_str = _normalize_cookie_input(body.cookies)
    if not cookie_str:
        return {"ok": False, "error": "Cookie 不能为空"}
    if relogin_type == "steam":
        session_id = (body.session_id or "").strip() or _cookie_value(cookie_str, "sessionid")
        if not _cookie_value(cookie_str, "steamLoginSecure"):
            return {"ok": False, "error": "Steam Cookie 缺少 steamLoginSecure"}
        if not session_id:
            return {"ok": False, "error": "Steam Cookie 缺少 sessionid"}
        if not _cookie_value(cookie_str, "sessionid"):
            cookie_str = f"{cookie_str}; sessionid={session_id}"
        steam_id = (body.steam_id or "").strip() or _steam_id_from_cookie_str(cookie_str)
        update_steam_creds(cookie_str, session_id, steam_id or None)
        cur = get_current_account()
        if cur:
            display_name, avatar_url = fetch_steam_profile_via_api(steam_id or cur.get("steam_id", ""), cookie_str)
            update_account(cur["id"], steam_id=steam_id or cur.get("steam_id", ""), display_name=display_name, avatar_url=avatar_url)
        return {"ok": True, "message": "Steam Cookie 已保存", "steam_id": steam_id}
    if relogin_type == "buff":
        update_buff_creds(cookie_str)
        _maybe_resume_after_buff_cookie_update()
        return {"ok": True, "message": "Buff Cookie 已保存"}
    return {"ok": False, "error": "未知登录类型"}
@router.get("/api/steam_guard")
def api_steam_guard():
    cfg = load_app_config_validated()
    sg = cfg.get("steam_guard") or {}
    shared_secret = (sg.get("shared_secret") or "").strip()
    if not shared_secret:
        return {"ok": False, "error": "未配置 shared_secret"}
    code = _generate_steam_guard_code(shared_secret)
    if not code:
        return {"ok": False, "error": "shared_secret 无效"}
    now_ts = int(time.time())
    return {"ok": True, "code": code, "server_time": now_ts, "period": 30}
