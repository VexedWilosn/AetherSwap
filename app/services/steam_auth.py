"""
Steam authentication service – extracted from api.py.
Contains steampy-based login automation, profile fetching,
and auto-relogin logic.
"""
import re
import threading
import time
from typing import Optional, Tuple
import urllib3
import requests as _req
from app.state import log
from app.config_loader import (
    get_steam_credentials,
    load_app_config_validated,
    update_steam_creds,
)
from app.accounts import (
    get_account,
    get_current_account,
    get_profile_dir,
    set_current,
    update_account,
)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
def _verify_steam_cookies_valid(cookie_str: str, steam_id: str = "") -> bool:
    """Use an actual HTTP request to verify if Steam cookies are truly valid.
    Returns True if cookies are valid, False if expired/invalid.
    Uses the Steam Store JSON API for reliable detection without page rendering.
    """
    cookie_dict = {}
    for part in (cookie_str or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            cookie_dict[k.strip()] = v.strip()
    if not cookie_dict.get("steamLoginSecure"):
        return False
    session = _req.Session()
    session.verify = False
    session.cookies.update(cookie_dict)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    try:
        r = session.get(
            "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig",
            timeout=12,
        )
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict):
                    if data.get("logged_in") is True:
                        return True
                    if data.get("logged_in") is False:
                        return False
            except Exception:
                pass
    except Exception:
        pass
    try:
        r2 = session.get(
            "https://steamcommunity.com/my/profile",
            timeout=12,
            allow_redirects=True,
        )
        final_url = (r2.url or "").lower()
        if "login" in final_url:
            return False
        if r2.status_code in (401, 403):
            return False
        return True
    except Exception:
        return True
def fetch_steam_profile_via_api(steam_id: str, cookies_str: str) -> tuple:
    if not steam_id:
        return "", ""
    display_name, avatar_url = "", ""
    session = _req.Session()
    session.verify = False
    cookie_dict = {}
    for part in (cookies_str or "").split(";"):
        s = part.strip()
        if "=" in s:
            k, _, v = s.partition("=")
            cookie_dict[k.strip()] = v.strip()
    session.cookies.update(cookie_dict)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    })
    try:
        r = session.get(f"https://steamcommunity.com/miniprofile/{int(steam_id) - 76561197960265728}/json", timeout=15)
        if r.status_code == 200:
            data = r.json()
            display_name = (data.get("persona_name") or "").strip()
            avatar_url = (data.get("avatar_url") or "").strip()
            if avatar_url and not avatar_url.startswith("http"):
                avatar_url = "https://avatars.steamstatic.com/" + avatar_url
            if avatar_url and "_medium" in avatar_url:
                avatar_url = avatar_url.replace("_medium", "_full")
    except Exception:
        pass
    if display_name and avatar_url:
        return display_name, avatar_url
    try:
        r = session.get(f"https://steamcommunity.com/profiles/{steam_id}", params={"xml": "1"}, timeout=15)
        if r.status_code == 200 and r.text:
            if not display_name:
                name_m = re.search(r"<steamID><!\[CDATA\[(.+?)\]\]></steamID>", r.text)
                if name_m:
                    display_name = name_m.group(1).strip()
            if not avatar_url:
                avatar_m = re.search(r"<avatarFull><!\[CDATA\[(.+?)\]\]></avatarFull>", r.text)
                if avatar_m:
                    avatar_url = avatar_m.group(1).strip()
    except Exception:
        pass
    if display_name and avatar_url:
        return display_name, avatar_url
    try:
        r = session.get(f"https://steamcommunity.com/profiles/{steam_id}", timeout=15)
        if r.status_code == 200 and r.text:
            html = r.text
            if not display_name:
                for pat in [
                    r'class="actual_persona_name"[^>]*>([^<]+)<',
                    r'"personaname"\s*:\s*"([^"]+)"',
                    r'<title>Steam Community :: (.+?)</title>',
                ]:
                    m = re.search(pat, html)
                    if m:
                        display_name = m.group(1).strip()
                        break
            if not avatar_url:
                for pat in [
                    r'class="playerAvatarAutoSizeInner"[^>]*>\s*<img[^>]+src="([^"]+)"',
                    r'"avatarfull"\s*:\s*"([^"]+)"',
                    r'property="og:image"[^>]+content="([^"]+)"',
                ]:
                    m = re.search(pat, html)
                    if m:
                        avatar_url = m.group(1).strip().replace("\\/", "/")
                        break
    except Exception:
        pass
    return display_name, avatar_url
def _get_shared_secret() -> str:
    try:
        cfg = load_app_config_validated()
        raw = ((cfg.get("steam_guard") or {}).get("shared_secret") or "").strip()
        if raw:
            return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), raw)
        return ""
    except Exception:
        return ""
def _build_steam_guard_dict(cur: dict, cfg: dict) -> Optional[dict]:
    """Build the steam_guard dict that steampy SteamClient.login() expects.
    steampy accepts either a path to a .maFile or a dict with these fields:
    {
        "steamid": "...",
        "shared_secret": "...",
        "identity_secret": "...",
        "device_id": "...",
    }
    We assemble this from the app config and account info.
    """
    shared_secret = ((cfg.get("steam_guard") or {}).get("shared_secret") or "").strip()
    if shared_secret:
        shared_secret = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), shared_secret)
    identity_secret = ((cfg.get("steam_confirm") or {}).get("identity_secret") or "").strip()
    device_id       = ((cfg.get("steam_confirm") or {}).get("device_id") or "").strip()
    steam_id        = (cur.get("steam_id") or "").strip()
    if not shared_secret:
        return None  
    return {
        "steamid": steam_id,
        "shared_secret": shared_secret,
        "identity_secret": identity_secret,
        "device_id": device_id,
    }
def _short_error_detail(exc: Exception, limit: int = 220) -> str:
    detail = str(exc).strip()
    detail = re.sub(r"\s+", " ", detail)
    if len(detail) > limit:
        detail = detail[: limit - 3] + "..."
    return detail
def _classify_steam_login_exception(exc: Exception) -> str:
    detail = _short_error_detail(exc)
    err = detail.lower()
    network_markers = (
        "max retries exceeded",
        "newconnectionerror",
        "failed to establish a new connection",
        "connection refused",
        "connection reset",
        "connection aborted",
        "name resolution",
        "temporary failure in name resolution",
        "getaddrinfo failed",
        "timed out",
        "read timed out",
        "connect timeout",
    )
    request_network_types = (
        _req.exceptions.ConnectionError,
        _req.exceptions.Timeout,
        _req.exceptions.ProxyError,
    )
    if isinstance(exc, request_network_types) or any(m in err for m in network_markers):
        return (
            "network_error: Steam 登录网络连接失败，程序没有成功连上 "
            "steamcommunity.com:443；这不是账号密码或 Steam Guard 错误。"
            "请检查本机直连、加速器/代理、DNS 或稍后重试。"
            f" 原始错误: {detail}"
        )
    if isinstance(exc, _req.exceptions.SSLError) or "ssl" in err or "certificate" in err:
        return (
            "network_error: Steam 登录 HTTPS/SSL 握手失败；通常是代理、加速器、"
            "证书拦截或本机网络环境导致。"
            f" 原始错误: {detail}"
        )
    return detail[:120]
def _do_steampy_login(username: str, password: str, steam_guard_dict: Optional[dict]) -> Tuple[bool, str, dict]:
    """Core Steam login using steampy's SteamClient with JWT/Protobuf protocol.
    Uses class-level requests.Session.request monkey-patch to bypass SSL
    verification for ALL internal steampy requests (including those made
    by LoginExecutor), exactly matching the user's reference implementation.
    Returns (ok, error_code, cookie_dict).
    """
    import json
    import requests as _req
    import requests.utils as rutils
    import urllib3
    urllib3.disable_warnings()
    _old_request = _req.Session.request
    def _bypass_ssl(self, method, url, **kwargs):
        kwargs['verify'] = False
        kwargs.setdefault('proxies', {})
        kwargs['proxies'] = {}
        return _old_request(self, method, url, **kwargs)
    _req.Session.request = _bypass_ssl
    try:
        from steampy.client import SteamClient
        sg_str = json.dumps(steam_guard_dict) if steam_guard_dict else None
        client = SteamClient(api_key="", username=username, password=password,
                             steam_guard=sg_str)
        client._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Origin': 'https://steamcommunity.com',
            'Referer': 'https://steamcommunity.com/',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        client.login()
        if not client.is_session_alive():
            return False, 'session_dead', {}
        comm_cookies = client._session.cookies.get_dict(domain='steamcommunity.com')
        store_cookies = client._session.cookies.get_dict(domain='store.steampowered.com')
        merged = {**store_cookies, **comm_cookies}
        if not merged.get('steamLoginSecure'):
            merged = rutils.dict_from_cookiejar(client._session.cookies)
        return True, '', merged
    except Exception as e:
        err = str(e).lower()
        network_error = _classify_steam_login_exception(e)
        if network_error.startswith("network_error:"):
            return False, network_error, {}
        if 'invalid' in err or 'incorrect' in err or 'wrong' in err or 'bad credentials' in err or 'client_id' in err or 'client id' in err:
            return False, 'wrong_creds', {}
        if 'two-factor' in err or 'twofactor' in err or '2fa' in err or 'guard' in err:
            return False, 'need_2fa', {}
        if 'captcha' in err:
            return False, 'captcha', {}
        if 'expecting value' in err or 'no response' in err:
            return False, 'ip_blocked: Steam API无响应，请尝试重启加速器或更换IP', {}
        return False, network_error, {}
    finally:
        _req.Session.request = _old_request
def _extract_creds_from_cookie_dict(cookie_dict: dict) -> Tuple[str, str, str]:
    """From a cookie dict return (cookie_str, session_id, steam_id)."""
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    session_id = cookie_dict.get("sessionid", "")
    steam_id = ""
    slc = cookie_dict.get("steamLoginSecure", "")
    if "%7C%7C" in slc:
        steam_id = slc.split("%7C%7C")[0].strip()
    elif "||" in slc:
        steam_id = slc.split("||")[0].strip()
    return cookie_str, session_id, steam_id
_auto_relogin_lock = threading.Lock()
_auto_relogin_last_success = 0.0
def try_steam_auto_relogin() -> tuple:
    global _auto_relogin_last_success
    if not _auto_relogin_lock.acquire(blocking=False):
        log("auto_relogin: 另一个自动登录正在进行，跳过", "info", category="steam")
        if time.time() - _auto_relogin_last_success < 30:
            return True, "auto_ok", "另一个自动登录刚刚完成"
        return False, "busy", "另一个自动登录正在进行"
    try:
        return _try_steam_auto_relogin_impl()
    finally:
        _auto_relogin_lock.release()
def _try_steam_auto_relogin_impl() -> tuple:
    global _auto_relogin_last_success
    cur = get_current_account()
    if not cur:
        log("auto_relogin: 未设置当前账号", "warn", category="steam")
        return False, "no_account", "未设置当前 Steam 账号，无法自动登录"
    account_id = cur.get("id")
    username = (cur.get("username") or "").strip()
    password = (cur.get("password") or "").strip()
    if not username or not password:
        log("auto_relogin: 无账号或密码", "warn", category="steam")
        return False, "no_creds", "未保存账号或密码，无法自动登录"
    set_current(account_id)
    existing = get_steam_credentials()
    existing_cookies = existing.get("cookies") or existing.get("cookie") or ""
    if existing_cookies and "steamLoginSecure" in existing_cookies:
        log("auto_relogin: 检测到现有 steamLoginSecure cookie，用 HTTP API 验证是否仍有效…", "info", category="steam")
        if _verify_steam_cookies_valid(existing_cookies):
            log("auto_relogin: HTTP 验证通过，Cookie 仍有效，无需重新登录", "info", category="steam")
            _auto_relogin_last_success = time.time()
            return True, "auto_ok", "Cookie 验证有效，无需重新登录"
        log("auto_relogin: HTTP 验证显示现有 cookie 已过期，继续密码登录", "info", category="steam")
    log("auto_relogin: 开始自动登录…", "info", category="steam")
    cfg = load_app_config_validated()
    steam_guard_dict = _build_steam_guard_dict(cur, cfg)
    if steam_guard_dict:
        log("auto_relogin: 已检测到 shared_secret，将自动处理 2FA", "info", category="steam")
    else:
        log("auto_relogin: 未配置 shared_secret，以无 2FA 方式尝试登录", "info", category="steam")
    ok, err_code, cookie_dict = _do_steampy_login(username, password, steam_guard_dict)
    if ok and cookie_dict.get("steamLoginSecure"):
        cookie_str, session_id, steam_id = _extract_creds_from_cookie_dict(cookie_dict)
        update_steam_creds(cookie_str, session_id or "")
        try:
            dn, av = fetch_steam_profile_via_api(steam_id or cur.get("steam_id", ""), cookie_str)
            update_account(account_id,
                           steam_id=steam_id or cur.get("steam_id", ""),
                           display_name=dn or cur.get("display_name", ""),
                           avatar_url=av or cur.get("avatar_url", ""))
        except Exception:
            pass
        log("auto_relogin: 登录成功", "info", category="steam")
        _auto_relogin_last_success = time.time()
        return True, "auto_ok", "已自动登录并更新凭证"
    if err_code == "wrong_creds":
        log("auto_relogin: 账号或密码错误", "warn", category="steam")
        try:
            from app.notify import notify_manual_intervention_required
            notify_manual_intervention_required("Steam", "系统保存的账号或密码不正确，登录被拒绝，请立刻前往修改密码并手动干预登录")
        except Exception:
            pass
        return False, "wrong_creds", "账号或密码错误"
    if err_code == "need_2fa":
        log("auto_relogin: 需要 2FA 但无 shared_secret 或令牌有误", "warn", category="steam")
        try:
            from app.notify import notify_manual_intervention_required
            notify_manual_intervention_required("Steam", "账号需要 2FA 验证，但 shared_secret 未配置或格式有误，请前往设置页补充 Steam Guard 密钥")
        except Exception:
            pass
        return False, "need_2fa", "需要二次验证且未配置 shared_secret，请配置后重试"
    if err_code == "captcha":
        log("auto_relogin: Steam 要求人机验证（Captcha），自动登录暂时失败", "warn", category="steam")
        return False, "captcha", "Steam 触发了人机验证，请稍后重试或手动登录"
    if err_code.startswith("network_error:"):
        msg = err_code.split(": ", 1)[1] if ": " in err_code else err_code
        log(f"auto_relogin: {msg}", "warn", category="steam")
        return False, "network_error", msg
    log(f"auto_relogin: 登录失败 – {err_code}", "warn", category="steam")
    return False, "error", (err_code or "自动登录失败，请检查网络或手动重登")
def verify_steam_auto_login(account_id: str) -> dict:
    acc = get_account(account_id)
    if not acc:
        return {"ok": False, "status": "no_account", "message": "账号不存在"}
    username = (acc.get("username") or "").strip()
    password = (acc.get("password") or "").strip()
    if not username or not password:
        return {"ok": False, "status": "no_creds", "message": "未保存账号或密码，无法验证"}
    set_current(account_id)
    cfg = load_app_config_validated()
    steam_guard_dict = _build_steam_guard_dict(acc, cfg)
    ok, err_code, cookie_dict = _do_steampy_login(username, password, steam_guard_dict)
    if ok and cookie_dict.get("steamLoginSecure"):
        cookie_str, session_id, steam_id = _extract_creds_from_cookie_dict(cookie_dict)
        update_steam_creds(cookie_str, session_id or "")
        cur_acc = get_account(account_id)
        if cur_acc:
            try:
                dn, av = fetch_steam_profile_via_api(steam_id or "", cookie_str)
                update_account(account_id,
                               steam_id=steam_id or cur_acc.get("steam_id", ""),
                               display_name=dn or cur_acc.get("display_name", ""),
                               avatar_url=av or cur_acc.get("avatar_url", ""))
            except Exception:
                pass
        return {"ok": True, "status": "auto_ok", "message": "可自动登录"}
    if err_code == "need_2fa":
        return {"ok": False, "status": "need_2fa", "message": "需要二次验证，请配置 shared_secret 后重试"}
    if err_code == "wrong_creds":
        return {"ok": False, "status": "wrong_creds", "message": "账号或密码错误"}
    if err_code == "captcha":
        return {"ok": False, "status": "captcha", "message": "Steam 触发了人机验证，请稍后重试"}
    if err_code.startswith("network_error:"):
        msg = err_code.split(": ", 1)[1] if ": " in err_code else err_code
        return {"ok": False, "status": "network_error", "message": msg}
    return {"ok": False, "status": "error", "message": err_code or "验证失败"}
