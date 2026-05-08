"""Auth routes – Steam & Buff relogin, Steam Guard."""
import base64
import hashlib
import hmac
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel
from app.state import log, set_buff_auth_expired
from app.config_loader import (
    get_buff_credentials,
    get_steam_credentials,
    load_app_config_validated,
    update_buff_creds,
    update_steam_creds,
)
from app.accounts import get_account, get_account_steam_guard, get_current_account, get_profile_dir, set_current, update_account
from app.services.steam_auth import (
    fetch_steam_profile_via_api,
    try_steam_auto_relogin,
)
from app.services.browser_profile import release_chromium_profile
from app.services.playwright_cookies import cookies_to_header, parse_cookie_string_for_url
from app.services.buff_auth import safe_buff_profile_segment
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


def _host_without_port(host: str) -> str:
    host = (host or "").strip()
    if host.startswith("[") and "]" in host:
        return host[: host.index("]") + 1]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _public_novnc_url(request: Request) -> str:
    configured = (os.environ.get("AETHERSWAP_NOVNC_URL") or "").strip()
    if configured and configured.lower() not in {"auto", "derive"}:
        return configured

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    proto = forwarded_proto or request.url.scheme or "http"
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host_header = forwarded_host or request.headers.get("host") or "localhost"
    host = _host_without_port(host_header)
    port = (os.environ.get("AETHERSWAP_NOVNC_PORT") or os.environ.get("NOVNC_PORT") or "6080").strip()
    return f"{proto}://{host}:{port}/vnc.html?autoconnect=1"
class ReloginFinishBody(BaseModel):
    success: bool
def _relogin_worker(relogin_type: str, account_id: str = "") -> None:
    global _relogin_playwright, _relogin_browser, _relogin_context, _relogin_error, _relogin_success
    try:
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        target_account_id = (account_id or "").strip()
        if relogin_type == "steam":
            cur = get_current_account()
            target_account_id = target_account_id or (cur.get("id") if cur else "")
            profile_dir = get_profile_dir(target_account_id or None)
        else:
            cur = get_current_account()
            target_account_id = target_account_id or (cur.get("id") if cur else "")
            profile_dir = Path(__file__).resolve().parent.parent.parent / "config" / "playwright_buff" / safe_buff_profile_segment(target_account_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        if relogin_type == "buff" and os.environ.get("AETHERSWAP_DOCKER") == "1":
            stopped = release_chromium_profile(
                profile_dir,
                lambda msg: log(f"buff_relogin: {msg}", "warn", category="buff"),
            )
            if stopped:
                log(f"buff_relogin: 已清理 {stopped} 个占用 Buff 浏览器 profile 的旧 Chromium 进程", "info", category="buff")
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        if relogin_type == "buff":
            saved = (get_buff_credentials(target_account_id) or {}).get("cookies", "")
            saved_cookies = parse_cookie_string_for_url(saved, "https://buff.163.com/")
            if saved_cookies:
                try:
                    context.add_cookies(saved_cookies)
                except Exception as ce:
                    log(f"buff_relogin: 注入已保存 Cookie 失败 {ce}", "warn", category="buff")
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
                    update_steam_creds(cookie_str, session_id, account_id=target_account_id)
                cur = get_account(target_account_id) if target_account_id else get_current_account()
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
                    try:
                        from app.services.account_region import sync_account_currency_region
                        sync_account_currency_region(cur["id"], cookie_str)
                    except Exception as sync_err:
                        log(f"steam_relogin: 同步账号币种/地区失败 {sync_err}", "warn", category="account")
            else:
                buff_cookies = context.cookies(["https://buff.163.com/"])
                cookie_str = cookies_to_header(buff_cookies)
                update_buff_creds(cookie_str, account_id=target_account_id)
                set_buff_auth_expired(False)
                from app.state import get_status
                from app.pipeline import start_pipeline
                from app.config_loader import load_app_config_validated
                st = get_status()
                err_msg = str(st.get("step") or "")
                if st.get("status") == "error" and "Buff" in err_msg and "过期" in err_msg:
                    from app.state import log
                    log("检测到 Buff 权限更新，尝试自动恢复挂刀流水线...", "info", category="system")
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
def _relogin_start(relogin_type: str, request: Request, account_id: str = ""):
    global _relogin_type, _relogin_error, _relogin_success, _relogin_playwright, _relogin_browser, _relogin_context
    had_existing = False
    with _relogin_lock:
        if _relogin_context or _relogin_browser:
            had_existing = True
            _relogin_success = False
            _relogin_wake.set()
    if had_existing:
        _relogin_done.wait(timeout=5)
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
    t = threading.Thread(target=_relogin_worker, args=(relogin_type, account_id), daemon=True)
    t.start()
    if not _relogin_ready.wait(timeout=60):
        return {"ok": False, "error": "打开浏览器超时"}
    if _relogin_error:
        return {"ok": False, "error": _relogin_error}
    if os.environ.get("AETHERSWAP_DOCKER") == "1":
        novnc_url = _public_novnc_url(request)
        target = "Steam" if relogin_type == "steam" else "Buff"
        msg = f"请打开 noVNC 页面完成 {target} 登录: {novnc_url}"
        return {"ok": True, "message": msg, "novnc_url": novnc_url}
    msg = "请在弹出的浏览器中完成 Steam 登录" if relogin_type == "steam" else "请在弹出的浏览器中完成 Buff 登录"
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
def api_auth_steam_relogin_start(request: Request, account_id: str = ""):
    return _relogin_start("steam", request, account_id=account_id)
@router.post("/api/auth/steam/relogin_finish")
def api_auth_steam_relogin_finish(body: ReloginFinishBody):
    return _relogin_finish(body.success)
@router.post("/api/auth/buff/relogin_start")
def api_auth_buff_relogin_start(request: Request, account_id: str = ""):
    return _relogin_start("buff", request, account_id=account_id)
@router.post("/api/auth/buff/relogin_finish")
def api_auth_buff_relogin_finish(body: ReloginFinishBody):
    return _relogin_finish(body.success)
@router.get("/api/steam_guard")
def api_steam_guard(account_id: str = ""):
    cfg = load_app_config_validated()
    account = get_account(account_id) if account_id else get_current_account()
    if account_id and not account:
        return {"ok": False, "error": "账号不存在"}
    guard = get_account_steam_guard(account, cfg)
    shared_secret = (guard.get("shared_secret") or "").strip()
    if not shared_secret:
        return {"ok": False, "error": "未配置 shared_secret"}
    code = _generate_steam_guard_code(shared_secret)
    if not code:
        return {"ok": False, "error": "shared_secret 无效"}
    now_ts = int(time.time())
    return {"ok": True, "code": code, "server_time": now_ts, "period": 30}
