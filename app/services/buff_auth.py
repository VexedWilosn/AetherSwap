"""
Buff authentication service for background keep-alive.
"""
import threading
import time
from pathlib import Path
from app.state import log, set_buff_auth_expired
from app.account_sessions import PROVIDER_BUFF, should_retry_session, update_account_session_status
from app.config_loader import get_buff_credentials, update_buff_creds
from app.services.playwright_cookies import cookies_to_header, parse_cookie_string_for_url
_buff_lock_guard = threading.Lock()
_buff_auto_relogin_locks: dict[str, threading.Lock] = {}
_buff_auto_relogin_last_success: dict[str, float] = {}


def _buff_lock_key(account_id: str = "") -> str:
    return (account_id or "").strip() or "default"


def _get_buff_lock(account_id: str = "") -> tuple[str, threading.Lock]:
    key = _buff_lock_key(account_id)
    with _buff_lock_guard:
        lock = _buff_auto_relogin_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _buff_auto_relogin_locks[key] = lock
        return key, lock


def safe_buff_profile_segment(account_id: str = "") -> str:
    text = (account_id or "").strip() or "default"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text) or "default"


def try_buff_auto_relogin(account_id: str = "", force: bool = False) -> tuple:
    lock_key, lock = _get_buff_lock(account_id)
    if not lock.acquire(blocking=False):
        log(f"buff_relogin[{lock_key}]: 该账号另一个保活任务正在进行，跳过", "info", category="buff")
        if time.time() - _buff_auto_relogin_last_success.get(lock_key, 0.0) < 60:
            return True, "auto_ok", "另一个自动登录刚刚完成"
        return False, "busy", "另一个自动登录正在进行"
    try:
        return _try_buff_auto_relogin_impl(account_id=account_id, force=force, lock_key=lock_key)
    finally:
        lock.release()


def _try_buff_auto_relogin_impl(account_id: str = "", force: bool = False, lock_key: str = "default") -> tuple:
    if not force:
        retry_ok, retry_msg = should_retry_session(account_id or None, PROVIDER_BUFF)
        if not retry_ok:
            log(f"buff_relogin: {retry_msg}", "info", category="buff")
            return False, "cooldown", retry_msg
    cred = get_buff_credentials(account_id)
    if not cred or not cred.get("cookies"):
        log("buff_relogin: 未保存凭证，无法保活", "warn", category="buff")
        return False, "no_creds", "未配置初始凭证，无法无感保活"
    profile_account_id = safe_buff_profile_segment(account_id or cred.get("account_id") or lock_key)
    profile_dir = Path(__file__).resolve().parent.parent.parent / "config" / "playwright_buff" / profile_account_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    log(f"buff_relogin[{profile_account_id}]: 开始自动保活/刷新 Cookie…", "info", category="buff")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            saved_cookies = parse_cookie_string_for_url(cred.get("cookies", ""), "https://buff.163.com/")
            if saved_cookies:
                try:
                    context.add_cookies(saved_cookies)
                except Exception as ce:
                    log(f"buff_relogin: 注入已保存 Cookie 失败 {ce}", "warn", category="buff")
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://buff.163.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            cookies = context.cookies(["https://buff.163.com/"])
            has_login = any(c.get("name") == "session" for c in cookies)
            if has_login:
                cookie_str = cookies_to_header(cookies)
                update_buff_creds(cookie_str, account_id=account_id)
                update_account_session_status(account_id or None, PROVIDER_BUFF, status="ok", error=None)
                set_buff_auth_expired(False)
                log(f"buff_relogin[{profile_account_id}]: Cookie 刷新成功，会话已延长", "info", category="buff")
                context.close()
                _buff_auto_relogin_last_success[lock_key] = time.time()
                return True, "auto_ok", "Buff 会话刷新成功"
            else:
                log("buff_relogin: 发现会话已失效 (未携带 session)，需要手动重新扫码登录", "warn", category="buff")
                update_account_session_status(account_id or None, PROVIDER_BUFF, status="expired", error="登录状态已失效")
                set_buff_auth_expired(True)
                try:
                    from app.notify import notify_manual_intervention_required
                    notify_manual_intervention_required("Buff", "登录状态已失效，可能触发了保护冻结，请尽快前往界面重新扫码登录")
                except Exception as ne:
                    log(f"buff_relogin: 发送报警通知失败 {ne}", "warn", category="buff")
                context.close()
                return False, "expired", "登录状态已失效，请在界面右上角点击重新登录"
    except Exception as e:
        log(f"buff_relogin: 异常 {e}", "warn", category="buff")
        update_account_session_status(account_id or None, PROVIDER_BUFF, status="error", error=str(e)[:120])
        return False, "error", (str(e)[:80] or "自动保活异常")
