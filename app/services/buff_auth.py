"""
Buff authentication service for background keep-alive.
"""
import threading
import time
from pathlib import Path
from app.state import log, set_buff_auth_expired
from app.config_loader import get_buff_credentials, update_buff_creds
from app.services.playwright_cookies import cookies_to_header, parse_cookie_string_for_url
_buff_auto_relogin_lock = threading.Lock()
_buff_auto_relogin_last_success = 0.0
def try_buff_auto_relogin() -> tuple:
    global _buff_auto_relogin_last_success
    if not _buff_auto_relogin_lock.acquire(blocking=False):
        log("buff_relogin: 另一个保活任务正在进行，跳过", "info", category="buff")
        if time.time() - _buff_auto_relogin_last_success < 60:
            return True, "auto_ok", "另一个自动登录刚刚完成"
        return False, "busy", "另一个自动登录正在进行"
    try:
        return _try_buff_auto_relogin_impl()
    finally:
        _buff_auto_relogin_lock.release()
def _try_buff_auto_relogin_impl() -> tuple:
    global _buff_auto_relogin_last_success
    cred = get_buff_credentials()
    if not cred or not cred.get("cookies"):
        log("buff_relogin: 未保存凭证，无法保活", "warn", category="buff")
        return False, "no_creds", "未配置初始凭证，无法无感保活"
    profile_dir = Path(__file__).resolve().parent.parent.parent / "config" / "playwright_buff"
    profile_dir.mkdir(parents=True, exist_ok=True)
    log("buff_relogin: 开始自动保活/刷新 Cookie…", "info", category="buff")
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
                update_buff_creds(cookie_str)
                set_buff_auth_expired(False)
                log("buff_relogin: Cookie 刷新成功，会话已延长", "info", category="buff")
                context.close()
                _buff_auto_relogin_last_success = time.time()
                return True, "auto_ok", "Buff 会话刷新成功"
            else:
                log("buff_relogin: 发现会话已失效 (未携带 session)，需要手动重新扫码登录", "warn", category="buff")
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
        return False, "error", (str(e)[:80] or "自动保活异常")
