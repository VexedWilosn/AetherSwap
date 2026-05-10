from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_active_browsers: dict[str, dict[str, Any]] = {}

_PLATFORM_URLS = {
    "buff": "https://buff.163.com/",
    "uuyp": "https://www.youpin898.com/",
    "eco": "https://www.ecosteam.cn/",
    "steam": "https://steamcommunity.com/market/",
    "steamdt": "https://www.steamdt.com/hanging",
}


def _normalize_platform(platform: str) -> str:
    return (platform or "").strip().lower()


async def start_login_browser(platform: str):
    platform = _normalize_platform(platform)
    if platform not in _PLATFORM_URLS:
        raise ValueError(f"unsupported platform: {platform}")
    if platform in _active_browsers:
        return _active_browsers[platform]["page"]

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(_PLATFORM_URLS[platform], wait_until="domcontentloaded")
    _active_browsers[platform] = {
        "playwright": playwright,
        "browser": browser,
        "context": context,
        "page": page,
    }
    return page


def _serialize_storage_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(value)
    return str(value)


async def finish_login_and_extract(platform: str) -> str:
    capsule = await finish_login_and_extract_capsule(platform)
    cookie_header = str(capsule.get("cookie_header") or "").strip()
    if cookie_header and not cookie_header.endswith(";"):
        cookie_header += ";"
    return cookie_header


async def finish_login_and_extract_capsule(platform: str, *, proxy_binding: str = "direct") -> dict[str, Any]:
    platform = _normalize_platform(platform)
    entry = _active_browsers.get(platform)
    if not entry:
        raise ValueError(f"browser not started for platform: {platform}")

    page = entry["page"]
    context = entry["context"]
    browser = entry["browser"]
    playwright = entry["playwright"]

    try:
        cookies = await context.cookies()
        cookie_map: dict[str, str] = {}
        for item in cookies:
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", "")).strip()
            if name:
                cookie_map[name] = value

        local_storage_raw = await page.evaluate("() => JSON.stringify(window.localStorage)")
        session_storage_raw = await page.evaluate("() => JSON.stringify(window.sessionStorage)")
        try:
            local_storage: dict[str, Any] = json.loads(local_storage_raw or "{}") or {}
        except Exception:
            local_storage = {}
        try:
            session_storage: dict[str, Any] = json.loads(session_storage_raw or "{}") or {}
        except Exception:
            session_storage = {}

        merged_storage = dict(local_storage)
        merged_storage.update(session_storage)

        try:
            user_agent = str(await page.evaluate("() => navigator.userAgent") or "").strip()
        except Exception:
            user_agent = ""

        headers_out: dict[str, str] = {}

        key_whitelist = {
            "buff": {"cookies", "cookie", "csrf_token"},
            "uuyp": {
                "authorization",
                "auth",
                "token",
                "uu_token",
                "u_token",
                "uk",
                "deviceId",
                "deviceid",
                "deviceUk",
                "deviceuk",
                "deviceToken",
                "devicetoken",
                "appType",
                "apptype",
                "platform",
                "secret-v",
                "secret_v",
                "App-Version",
                "AppVersion",
                "userInfo",
                "userinfo",
            },
            "eco": {"partnerid", "partner_id", "PartnerId", "rsaKey", "RSAKey", "token", "authorization"},
        }

        whitelist = key_whitelist.get(platform)
        storage_snapshot: dict[str, str] = {}
        for key, value in merged_storage.items():
            if whitelist is not None and key not in whitelist:
                continue
            serialized = _serialize_storage_value(value)
            if serialized:
                storage_snapshot[str(key)] = serialized

        if platform == "steamdt":
            # SteamDT capsules should preserve the full browser-derived local/session state.
            # We do not append storage keys into the cookie header, but we keep them on the capsule.
            pass
        else:
            for key, serialized in storage_snapshot.items():
                cookie_map[key] = serialized

        if platform == "uuyp":
            header_aliases = {
                "authorization": "authorization",
                "uk": "uk",
                "deviceUk": "deviceUk",
                "deviceuk": "deviceUk",
                "deviceId": "deviceId",
                "deviceid": "deviceId",
                "deviceToken": "deviceToken",
                "devicetoken": "deviceToken",
                "App-Version": "App-Version",
                "AppVersion": "App-Version",
                "secret-v": "secret-v",
                "secret_v": "secret-v",
                "platform": "platform",
                "appType": "appType",
                "apptype": "appType",
            }
            for key, out_key in header_aliases.items():
                raw = merged_storage.get(key)
                if raw is not None and str(raw).strip():
                    headers_out[out_key] = str(raw).strip()

            headers_path = Path(__file__).resolve().parent.parent.parent / "DataEngine" / "uuyp_headers.json"
            if headers_out:
                existing: dict[str, Any] = {}
                try:
                    if headers_path.exists():
                        existing = json.loads(headers_path.read_text(encoding="utf-8") or "{}") or {}
                except Exception:
                    existing = {}
                existing.update(headers_out)
                headers_path.parent.mkdir(parents=True, exist_ok=True)
                headers_path.write_text(json.dumps(existing, ensure_ascii=False, indent=4), encoding="utf-8")

        if user_agent:
            headers_out["user-agent"] = user_agent

        if platform == "steamdt":
            device_id = (
                cookie_map.get("SDT_DeviceId")
                or str(merged_storage.get("SDT_DeviceId") or "").strip()
                or str(merged_storage.get("deviceId") or "").strip()
                or str(merged_storage.get("device_id") or "").strip()
                or str(uuid.uuid4())
            )
            cookie_map["SDT_DeviceId"] = device_id
            cookie_map.setdefault("i18n_redirected", "zh")
            cookie_map.setdefault("SDT_HideAgreement", "1")
            headers_out.update(
                {
                    "accept": "application/json",
                    "accept-language": "zh-CN,zh;q=0.9",
                    "content-type": "application/json",
                    "language": "zh_CN",
                    "origin": "https://www.steamdt.com",
                    "referer": "https://www.steamdt.com/hanging",
                    "x-currency": "CNY",
                    "x-device": "1",
                    "x-device-id": device_id,
                    "x-app-version": "1.0.0",
                }
            )
        else:
            device_id = (
                cookie_map.get("deviceId")
                or cookie_map.get("deviceid")
                or str(merged_storage.get("deviceId") or "").strip()
                or str(merged_storage.get("deviceid") or "").strip()
            )

        cookie_header = "; ".join(
            f"{key}={value}" for key, value in cookie_map.items() if str(key).strip()
        ).strip()

        return {
            "platform": platform,
            "cookies": cookie_map,
            "cookie_header": cookie_header,
            "device_id": device_id,
            "user_agent": user_agent,
            "headers": headers_out,
            "local_storage": local_storage if platform != "steamdt" else merged_storage,
            "session_storage": session_storage if platform != "steamdt" else {},
            "proxy_binding": proxy_binding,
        }
    finally:
        _active_browsers.pop(platform, None)
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await playwright.stop()
        except Exception:
            pass
