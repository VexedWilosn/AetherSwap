from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import requests


TRADE_LINK_PRIVACY_URL = "https://steamcommunity.com/my/tradeoffers/privacy"
TRADE_LINK_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?partner=\d+&(?:amp;)?token=[A-Za-z0-9_-]+"
)


@dataclass(frozen=True)
class SteamTradeLinkResult:
    ok: bool
    trade_link: str = ""
    reason: str = ""
    status_code: int = 0


def extract_steam_trade_link_from_text(text: str) -> str:
    normalized = html.unescape(str(text or ""))
    match = TRADE_LINK_RE.search(normalized)
    return match.group(0).replace("&amp;", "&") if match else ""


def cookie_header_to_dict(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(cookie_header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key:
            cookies[key.strip()] = value.strip()
    return cookies


def steam_id_from_cookie_header(cookie_header: str) -> str:
    steam_login_secure = cookie_header_to_dict(cookie_header).get("steamLoginSecure", "")
    if "%7C%7C" in steam_login_secure:
        return steam_login_secure.split("%7C%7C", 1)[0].strip()
    if "||" in steam_login_secure:
        return steam_login_secure.split("||", 1)[0].strip()
    return steam_login_secure.strip() if steam_login_secure.strip().isdigit() else ""


def fetch_steam_trade_link(
    cookie_header: str,
    *,
    steam_id: str = "",
    timeout: int = 12,
    session: Any | None = None,
) -> SteamTradeLinkResult:
    cookies = cookie_header_to_dict(cookie_header)
    if not cookies.get("steamLoginSecure"):
        return SteamTradeLinkResult(False, reason="steam_auth_required")

    client = session or requests.Session()
    try:
        client.cookies.update(cookies)
    except Exception:
        pass
    try:
        client.verify = False
    except Exception:
        pass
    try:
        client.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://steamcommunity.com/market/",
            }
        )
    except Exception:
        pass

    resolved_steam_id = str(steam_id or steam_id_from_cookie_header(cookie_header) or "").strip()
    urls = []
    if resolved_steam_id:
        urls.append(f"https://steamcommunity.com/profiles/{resolved_steam_id}/tradeoffers/privacy")
    urls.append(TRADE_LINK_PRIVACY_URL)

    last_status = 0
    for url in dict.fromkeys(urls):
        try:
            response = client.get(url, timeout=timeout, allow_redirects=True)
        except Exception as exc:
            return SteamTradeLinkResult(False, reason=f"request_failed:{str(exc)[:80]}", status_code=last_status)
        last_status = int(getattr(response, "status_code", 0) or 0)
        final_url = str(getattr(response, "url", "") or "").lower()
        if "login" in final_url or last_status in {401, 403}:
            continue
        trade_link = extract_steam_trade_link_from_text(str(getattr(response, "text", "") or ""))
        if trade_link:
            return SteamTradeLinkResult(True, trade_link=trade_link, status_code=last_status)
    return SteamTradeLinkResult(False, reason="trade_link_not_found", status_code=last_status)
