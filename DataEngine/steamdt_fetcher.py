from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
import uuid
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import (
    ItemBase,
    SessionLocal,
    SteamDTOpportunity,
    normalize_data_timestamp,
    upsert_market_price_if_fresh,
)
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.proxy_pool import get_request_proxies, proxy_log_tag
from DataEngine.stop_signal import raise_if_stop_requested
from app.services.notifier import notify_webhook
from app.services.session_capsule_pool import SessionCapsule, SessionCapsulePool

setup_dataengine_logging()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
WAF_COOLDOWN_PATH = BASE_DIR / "config" / "steamdt_waf_cooldown.json"
SESSION_STATE_PATH = BASE_DIR / "config" / "steamdt_session_state.json"
CAPSULE_STATE_PATH = BASE_DIR / "config" / "session_capsules.json"
DEFAULT_ENDPOINT = "https://www.steamdt.com/api/user/ranking/v1/hanging-knife"
DEFAULT_TIMEOUT = 15
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 1
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_SAMPLE_PATHS = (BASE_DIR / "DataEngine" / "steamdt.txt", BASE_DIR / "DataEngine" / "payload.txt")

SUPPORTED_MARKET_PLATFORMS = {"steam", "buff", "uuyp", "eco"}
STEAMDT_PLATFORM_MAP = {
    "STEAM": "steam",
    "BUFF": "buff",
    "BUFF163": "buff",
    "UUYP": "uuyp",
    "YOUPIN": "uuyp",
    "\u60a0\u60a0": "uuyp",
    "\u60a0\u60a0\u6709\u54c1": "uuyp",
    "ECO": "eco",
    "ECOSTEAM": "eco",
    "C5": "c5",
    "C5GAME": "c5",
}

NON_CNY_MARKERS = ("USD", "US$", "$", "EUR", "EURO", "HKD", "TWD", "JPY", "RUB", "\u20ac", "\u20bd")
CNY_MARKERS = ("CNY", "RMB", "CN\u00a5", "\u00a5", "\uffe5", "\u4eba\u6c11\u5e01", "\u5143")


@dataclass(frozen=True)
class SteamDTStrategy:
    name: str
    want_to_get: str
    purchase_plan: str
    sale_plan: str
    steam_price_role: str | None = None
    platform_price_role: str | None = None
    source_kind: str = "hanging"

    def payload(self, cfg: dict[str, Any], page: int) -> dict[str, Any]:
        return {
            "page": page,
            "pageSize": int(cfg["page_size"]),
            "type": str(cfg["type"]),
            "wantToGet": self.want_to_get,
            "purchasePlan": self.purchase_plan,
            "salePlan": self.sale_plan,
            "minSellPrice": str(cfg["min_sell_price"]),
            "maxSellPrice": str(cfg["max_sell_price"]),
            "minTransactionCount": str(cfg["min_transaction_count"]),
            "platformList": list(cfg["platform_list"]),
            "timestamp": str(int(time.time() * 1000)),
        }


DEFAULT_STRATEGIES = [
    SteamDTStrategy(
        name="platform_cash_steam_sell_platform_buy",
        want_to_get="PLATFORM_BALANCE",
        purchase_plan="STEAM_SELL_PRICE",
        sale_plan="PLATFORM_PURCHASE_PRICE",
        steam_price_role="sell_min",
        platform_price_role="buy_max",
    ),
    SteamDTStrategy(
        name="platform_cash_steam_buy_platform_sell",
        want_to_get="PLATFORM_BALANCE",
        purchase_plan="STEAM_PURCHASE_PRICE",
        sale_plan="PLATFORM_SELL_PRICE",
        steam_price_role="buy_max",
        platform_price_role="sell_min",
    ),
    SteamDTStrategy(
        name="steam_balance_steam_sell",
        want_to_get="STEAM_BALANCE",
        purchase_plan="",
        sale_plan="STEAM_SELL_PRICE",
        steam_price_role="sell_min",
        platform_price_role=None,
    ),
    SteamDTStrategy(
        name="steam_balance_steam_buy_platform_sell",
        want_to_get="STEAM_BALANCE",
        purchase_plan="STEAM_PURCHASE_PRICE",
        sale_plan="PLATFORM_SELL_PRICE",
        steam_price_role="buy_max",
        platform_price_role="sell_min",
    ),
]

_WAF_COOLDOWN_UNTIL = 0.0


def _load_waf_cooldown_until() -> float:
    try:
        if not WAF_COOLDOWN_PATH.exists():
            return 0.0
        data = json.loads(WAF_COOLDOWN_PATH.read_text(encoding="utf-8") or "{}")
        return float(data.get("cooldown_until") or 0)
    except Exception as exc:
        logger.warning("[steamdt] waf cooldown load failed: %s", exc)
        return 0.0


def _save_waf_cooldown_until(cooldown_until: float) -> None:
    try:
        WAF_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cooldown_until": float(cooldown_until),
            "cooldown_until_iso": datetime.fromtimestamp(float(cooldown_until)).isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        WAF_COOLDOWN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[steamdt] waf cooldown save failed: %s", exc)


def _load_or_create_device_id(configured_device_id: str | None = None) -> str:
    configured = str(configured_device_id or "").strip()
    if configured:
        return configured
    try:
        if SESSION_STATE_PATH.exists():
            data = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8") or "{}")
            device_id = str(data.get("device_id") or "").strip()
            if device_id:
                return device_id
    except Exception as exc:
        logger.warning("[steamdt] session state load failed: %s", exc)

    device_id = str(uuid.uuid4())
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_STATE_PATH.write_text(
            json.dumps({"device_id": device_id, "created_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[steamdt] session state save failed: %s", exc)
    return device_id


def _env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        logger.warning("[steamdt] config load failed: %s", exc)
    steamdt = cfg.get("steamdt") if isinstance(cfg.get("steamdt"), dict) else {}
    session_capsules = cfg.get("session_capsules") if isinstance(cfg.get("session_capsules"), dict) else {}
    steamdt_capsules = session_capsules.get("steamdt") if isinstance(session_capsules.get("steamdt"), dict) else {}
    return {
        "enabled": _env_bool(os.getenv("STEAMDT_ENABLED"), bool(steamdt.get("enabled", False))),
        "endpoint": os.getenv(
            "STEAMDT_ENDPOINT",
            str(steamdt.get("endpoint") or steamdt.get("api_url") or DEFAULT_ENDPOINT),
        ).strip(),
        "timeout": int(os.getenv("STEAMDT_TIMEOUT", steamdt.get("timeout", DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT),
        "page_size": int(os.getenv("STEAMDT_PAGE_SIZE", steamdt.get("page_size", steamdt.get("limit", DEFAULT_PAGE_SIZE))) or DEFAULT_PAGE_SIZE),
        "max_pages": int(os.getenv("STEAMDT_MAX_PAGES", steamdt.get("max_pages", DEFAULT_MAX_PAGES)) or DEFAULT_MAX_PAGES),
        "min_sell_price": os.getenv("STEAMDT_MIN_SELL_PRICE", str(steamdt.get("min_sell_price", "1"))),
        "max_sell_price": os.getenv("STEAMDT_MAX_SELL_PRICE", str(steamdt.get("max_sell_price", "10"))),
        "min_transaction_count": os.getenv("STEAMDT_MIN_TRANSACTION_COUNT", str(steamdt.get("min_transaction_count", "100"))),
        "platform_list": steamdt.get("platform_list", ["C5", "YOUPIN", "BUFF"]),
        "type": steamdt.get("type", "swap"),
        "cooldown_seconds_on_waf": int(steamdt.get("cooldown_seconds_on_waf", DEFAULT_COOLDOWN_SECONDS) or DEFAULT_COOLDOWN_SECONDS),
        "sleep_min_seconds": float(steamdt.get("sleep_min_seconds", 1.0) or 1.0),
        "sleep_max_seconds": float(steamdt.get("sleep_max_seconds", 3.0) or 3.0),
        "use_proxy": _env_bool(os.getenv("STEAMDT_USE_PROXY"), bool(steamdt.get("use_proxy", False))),
        "device_id": _load_or_create_device_id(os.getenv("STEAMDT_DEVICE_ID") or steamdt.get("device_id")),
        "cookie": os.getenv("STEAMDT_COOKIE", str(steamdt.get("cookie") or "")).strip(),
        "min_volume_for_high_quality": int(os.getenv("STEAMDT_MIN_VOLUME_HIGH_QUALITY", steamdt.get("min_volume_for_high_quality", 1)) or 1),
        "strategies": steamdt.get("strategies"),
        "capsules_enabled": _env_bool(os.getenv("STEAMDT_CAPSULES_ENABLED"), bool(steamdt_capsules.get("enabled", True))),
        "capsule_lease_ttl_seconds": int(os.getenv("STEAMDT_CAPSULE_LEASE_TTL", steamdt_capsules.get("lease_ttl_seconds", 45)) or 45),
        "capsule_timeout_cooldown_seconds": int(steamdt_capsules.get("timeout_cooldown_seconds", 60) or 60),
        "capsule_empty_cooldown_seconds": int(steamdt_capsules.get("empty_soft_block_cooldown_seconds", 120) or 120),
        "capsule_waf_cooldown_seconds": int(steamdt_capsules.get("waf_block_cooldown_seconds", 300) or 300),
        "capsule_auth_cooldown_seconds": int(steamdt_capsules.get("auth_invalid_cooldown_seconds", 1800) or 1800),
        "capsule_auto_retire_after": int(steamdt_capsules.get("auto_retire_after", 3) or 3),
        "capsule_auto_retire_reasons": set(steamdt_capsules.get("auto_retire_reasons", ["waf_block", "empty_soft_block"])),
        "capsule_min_ready": int(steamdt_capsules.get("min_ready_capsules", 1) or 1),
        "capsule_alert_interval_seconds": int(steamdt_capsules.get("recapture_alert_interval_seconds", 3600) or 3600),
    }


def _load_strategies(cfg: dict[str, Any]) -> list[SteamDTStrategy]:
    only = {
        name.strip()
        for name in str(os.getenv("STEAMDT_STRATEGIES") or "").split(",")
        if name.strip()
    }
    raw = cfg.get("strategies")
    if not isinstance(raw, list) or not raw:
        return [strategy for strategy in DEFAULT_STRATEGIES if not only or strategy.name in only]
    strategies: list[SteamDTStrategy] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            strategies.append(
                SteamDTStrategy(
                    name=str(row["name"]),
                    want_to_get=str(row["wantToGet"]),
                    purchase_plan=str(row.get("purchasePlan") or ""),
                    sale_plan=str(row.get("salePlan") or ""),
                    steam_price_role=row.get("steam_price_role"),
                    platform_price_role=row.get("platform_price_role"),
                    source_kind=str(row.get("source_kind") or "hanging"),
                )
            )
        except Exception:
            logger.warning("[steamdt] invalid strategy skipped: %s", row)
    strategies = strategies or DEFAULT_STRATEGIES
    return [strategy for strategy in strategies if not only or strategy.name in only]


def parse_relative_time(value: Any, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if value is None:
        return now
    if isinstance(value, (int, float)):
        raw = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(raw)

    text = str(value).strip()
    if not text:
        return now
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        raw = float(text)
        raw = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(raw)

    units = "\u79d2|\u79d2\u949f|\u5206\u949f|\u5206|\u5c0f\u65f6|\u65f6|\u5929|\u65e5|second|minute|hour|day|sec|min|h|d"
    match = re.search(rf"(\d+)\s*({units})", text, re.I)
    if not match:
        try:
            return normalize_data_timestamp(text)
        except Exception:
            return now

    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit in {"\u79d2", "\u79d2\u949f", "second", "sec"}:
        return now - timedelta(seconds=amount)
    if unit in {"\u5206\u949f", "\u5206", "minute", "min"}:
        return now - timedelta(minutes=amount)
    if unit in {"\u5c0f\u65f6", "\u65f6", "hour", "h"}:
        return now - timedelta(hours=amount)
    if unit in {"\u5929", "\u65e5", "day", "d"}:
        return now - timedelta(days=amount)
    return now


def _first_present(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _strip_currency_markers(value: str) -> str:
    cleaned = value.strip().replace(",", "")
    for marker in CNY_MARKERS + NON_CNY_MARKERS:
        cleaned = cleaned.replace(marker, "")
    return cleaned.strip()


def _safe_float(value: Any) -> float:
    try:
        if isinstance(value, str):
            return float(_strip_currency_markers(value) or 0)
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_platform_name(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    key = raw.upper().replace(" ", "").replace("-", "").replace("_", "")
    return STEAMDT_PLATFORM_MAP.get(key)


def _currency_marker(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if any(marker in text for marker in NON_CNY_MARKERS):
        return "NON_CNY"
    if any(marker.upper() in text for marker in CNY_MARKERS):
        return "CNY"
    return None


def _detect_currency(*rows: dict[str, Any]) -> str:
    currency_keys = (
        "currency",
        "currencyCode",
        "currency_code",
        "currencyType",
        "currency_type",
        "priceCurrency",
        "price_currency",
        "unit",
        "priceUnit",
        "price_unit",
    )
    price_keys = (
        "steamPrice",
        "steam_price",
        "platformPrice",
        "platform_price",
        "price",
        "biddingPrice",
        "bidding_price",
        "sell_min",
        "buy_max",
    )
    saw_cny = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in currency_keys:
            marker = _currency_marker(row.get(key))
            if marker == "NON_CNY":
                return "NON_CNY"
            if marker == "CNY":
                saw_cny = True
        for key in price_keys:
            marker = _currency_marker(row.get(key))
            if marker == "NON_CNY":
                return "NON_CNY"
            if marker == "CNY":
                saw_cny = True
    return "CNY" if saw_cny else "UNKNOWN_ASSUMED_CNY"


def _quote_quality(sell_min: float, buy_max: float, volume: int, timestamp: datetime, currency: str, min_volume: int = 1) -> str:
    if currency == "NON_CNY":
        return "invalid_currency"
    if sell_min <= 0 and buy_max <= 0:
        return "invalid"
    if timestamp and volume >= min_volume:
        return "high"
    if timestamp:
        return "medium"
    return "low"


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def load_sample_rows(
    path: str | Path | None = None,
    strategy_name: str = "platform_cash_steam_buy_platform_sell",
    *,
    refresh_time: bool = False,
) -> list[dict[str, Any]]:
    """Load captured SteamDT payloads for parser/write-path regression tests only."""

    candidates = [Path(path)] if path else list(DEFAULT_SAMPLE_PATHS)
    for candidate in candidates:
        if not candidate.exists():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        rows = _extract_items(payload)
        now_ts = str(int(time.time()))
        for row in rows:
            row["_steamdt_strategy"] = strategy_name
            row["_steamdt_request"] = {
                "wantToGet": "PLATFORM_BALANCE",
                "purchasePlan": "STEAM_PURCHASE_PRICE",
                "salePlan": "PLATFORM_SELL_PRICE",
            }
            if refresh_time:
                row["updateTime"] = now_ts
                row["platformUpdateTime"] = now_ts
                row["steamUpdateTime"] = now_ts
        logger.info("[steamdt] loaded sample rows=%s path=%s", len(rows), candidate)
        return rows
    logger.warning("[steamdt] sample payload not found | path=%s", path or DEFAULT_SAMPLE_PATHS)
    return []


def _parse_cookie(raw_cookie: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in str(raw_cookie or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            parsed[key] = value.strip()
    return parsed


def _steamdt_rate_cookie() -> str:
    rates = [
        {"name": "\u4eba\u6c11\u5e01", "icon": "\u00a5", "currency": "CNY", "rate": 1},
        {"name": "\u7f8e\u5143", "icon": "$", "currency": "USD", "rate": 0.1465},
        {"name": "\u5362\u5e03", "icon": "\u20bd", "currency": "RUB", "rate": 11.0619},
        {"name": "\u6b27\u5143", "icon": "\u20ac", "currency": "EUR", "rate": 0.1248},
    ]
    return quote(json.dumps(rates, ensure_ascii=False, separators=(",", ":")))


def _build_cookie(device_id: str, raw_cookie: str = "") -> str:
    cookies = {
        "SDT_DeviceId": device_id,
        "i18n_redirected": "zh",
        "SDT_HideAgreement": "1",
        "SDT_RateList": _steamdt_rate_cookie(),
    }
    cookies.update(_parse_cookie(raw_cookie))
    cookies["SDT_DeviceId"] = cookies.get("SDT_DeviceId") or device_id
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _headers(device_id: str, raw_cookie: str = "") -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "accept-language": "zh-CN,zh;q=0.9",
        "access-token": "undefined",
        "content-type": "application/json",
        "language": "zh_CN",
        "origin": "https://www.steamdt.com",
        "referer": "https://www.steamdt.com/hanging",
        "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
        "x-app-version": "1.0.0",
        "x-currency": "CNY",
        "x-device": "1",
        "x-device-id": device_id,
        "priority": "u=1, i",
        "cookie": _build_cookie(device_id, raw_cookie),
    }


def _merge_headers(base: dict[str, str], extra: dict[str, Any] | None = None) -> dict[str, str]:
    headers = dict(base or {})
    for key, value in dict(extra or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            headers[str(key).strip().lower()] = text
    return headers


def _ensure_steamdt_cookie_header(device_id: str, raw_cookie: str = "") -> str:
    return _build_cookie(device_id, raw_cookie)


def _fallback_capsule(cfg: dict[str, Any]) -> SessionCapsule:
    device_id = str(cfg["device_id"])
    cookie_header = _ensure_steamdt_cookie_header(device_id, str(cfg.get("cookie") or ""))
    headers = _headers(device_id, str(cfg.get("cookie") or ""))
    return SessionCapsule(
        capsule_id="steamdt-fallback",
        platform="steamdt",
        cookies=_parse_cookie(cookie_header),
        cookie_header=cookie_header,
        device_id=device_id,
        user_agent=headers.get("user-agent", ""),
        headers=headers,
        proxy_binding="pool" if cfg.get("use_proxy") else "direct",
        tls_profile="requests",
        notes="autogenerated fallback capsule",
    )


def _prepare_capsule_request(capsule: SessionCapsule, cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, str] | None]:
    base_headers = _headers(capsule.device_id or str(cfg["device_id"]), capsule.cookie_header or str(cfg.get("cookie") or ""))
    headers = _merge_headers(base_headers, capsule.headers)
    headers["cookie"] = _ensure_steamdt_cookie_header(
        capsule.device_id or str(cfg["device_id"]),
        capsule.cookie_header or str(cfg.get("cookie") or ""),
    )
    headers["x-device-id"] = capsule.device_id or str(cfg["device_id"])
    if capsule.user_agent:
        headers["user-agent"] = capsule.user_agent
    proxy_binding = str(getattr(capsule, "proxy_binding", "") or "").strip().lower()
    use_proxy = bool(cfg["use_proxy"]) or proxy_binding in {"pool", "global"}
    proxies = get_request_proxies(force=True, platform="global") if use_proxy else None
    return headers, proxies


def _classify_steamdt_failure(
    *,
    response: requests.Response | None = None,
    payload: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> tuple[str, int]:
    if exc is not None:
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text:
            return "timeout", 60
        if "proxy" in text and "407" in text:
            return "proxy_auth", 300
        return "network", 120

    data = payload if isinstance(payload, dict) else {}
    code = str(data.get("errorCode") or data.get("errorCodeStr") or "")
    msg = str(data.get("errorMsg") or data.get("message") or "")
    lowered = f"{code} {msg}".lower()
    if response is not None and response.status_code == 429:
        return "rate_limited", 120
    if str(code) == "108" or "环境异常" in msg:
        return "waf_block", 300
    if any(token in lowered for token in ("login", "auth", "token", "登录", "鉴权")):
        return "auth_invalid", 1800
    if "empty" in lowered:
        return "empty_soft_block", 120
    return "business_error", 120


def _steamdt_url(endpoint: str, timestamp_ms: str) -> str:
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}timestamp={timestamp_ms}"


def register_steamdt_capsule_from_cookie(
    raw_cookie: str,
    *,
    user_agent: str = "",
    device_id: str = "",
    proxy_binding: str = "direct",
    notes: str = "",
) -> SessionCapsule:
    cfg = _load_config()
    pool = SessionCapsulePool(CAPSULE_STATE_PATH)
    parsed = _parse_cookie(raw_cookie)
    resolved_device_id = (
        str(device_id or "").strip()
        or str(parsed.get("SDT_DeviceId") or "").strip()
        or str(cfg.get("device_id") or "").strip()
        or _load_or_create_device_id(None)
    )
    cookie_header = _ensure_steamdt_cookie_header(resolved_device_id, raw_cookie)
    headers = _headers(resolved_device_id, cookie_header)
    if user_agent:
        headers["user-agent"] = user_agent
    capsule = pool.register_capsule(
        platform="steamdt",
        cookies=_parse_cookie(cookie_header),
        cookie_header=cookie_header,
        device_id=resolved_device_id,
        user_agent=user_agent or headers.get("user-agent", ""),
        headers=headers,
        proxy_binding=proxy_binding,
        tls_profile="browser-imported",
        notes=notes or "manual import",
    )
    logger.info("[steamdt] capsule registered | capsule=%s summary=%s", capsule.capsule_id, pool.status_summary("steamdt"))
    return capsule


def _mark_capsule_failure(capsule_pool: SessionCapsulePool, capsule: SessionCapsule, reason: str, cooldown_seconds: int, cfg: dict[str, Any], *, auth_failure: bool = False) -> SessionCapsule | None:
    updated = capsule_pool.mark_failure(
        "steamdt",
        capsule.capsule_id,
        reason=reason,
        cooldown_seconds=cooldown_seconds,
        auth_failure=auth_failure,
        auto_retire_reasons=set(cfg.get("capsule_auto_retire_reasons") or []),
        auto_retire_after=int(cfg.get("capsule_auto_retire_after") or 0),
    )
    if updated and updated.status == "retired":
        logger.warning(
            "[steamdt] capsule auto-retired | capsule=%s reason=%s streak=%s retire_reason=%s",
            updated.capsule_id,
            reason,
            updated.failure_streak_count,
            updated.retire_reason,
        )
    return updated


def _maybe_alert_recapture_needed(capsule_pool: SessionCapsulePool, cfg: dict[str, Any]) -> None:
    needed, reason = capsule_pool.recapture_needed(
        "steamdt",
        min_ready=int(cfg.get("capsule_min_ready") or 1),
        alert_interval_seconds=int(cfg.get("capsule_alert_interval_seconds") or 3600),
    )
    if not needed:
        return
    summary = capsule_pool.status_summary("steamdt")
    msg = (
        "SteamDT session capsules need recapture.\n"
        f"Reason: {reason}\n"
        f"Summary: total={summary.get('total', 0)}, ready={summary.get('ready', 0)}, "
        f"cooldown={summary.get('cooldown', 0)}, retired={summary.get('retired', 0)}"
    )
    logger.warning("[steamdt] recapture needed | reason=%s summary=%s", reason, summary)
    notify_webhook("AetherSwap SteamDT session recapture needed", msg, extra={"platform": "steamdt", "reason": reason, "summary": summary})
    capsule_pool.mark_maintenance_alerted("steamdt")


def fetch_steamdt_rows() -> list[dict[str, Any]]:
    """Fetch all configured SteamDT strategy pages via the hanging-knife POST API."""

    global _WAF_COOLDOWN_UNTIL
    cfg = _load_config()
    if not cfg["enabled"]:
        logger.info("[steamdt] skipped: disabled")
        return []
    if not cfg["endpoint"]:
        logger.warning("[steamdt] skipped: endpoint not configured")
        return []
    _WAF_COOLDOWN_UNTIL = max(_WAF_COOLDOWN_UNTIL, _load_waf_cooldown_until())
    if time.time() < _WAF_COOLDOWN_UNTIL:
        logger.warning("[steamdt] skipped_by_waf_cooldown remaining=%ss", round(_WAF_COOLDOWN_UNTIL - time.time(), 1))
        return []

    rows: list[dict[str, Any]] = []
    capsule_pool = SessionCapsulePool(CAPSULE_STATE_PATH)
    leased_capsule = None
    using_capsule_pool = bool(cfg.get("capsules_enabled"))
    if using_capsule_pool:
        leased_capsule = capsule_pool.lease_capsule("steamdt", lease_ttl_seconds=int(cfg["capsule_lease_ttl_seconds"]))
        if leased_capsule:
            logger.info(
                "[steamdt] leased capsule=%s summary=%s",
                leased_capsule.capsule_id,
                capsule_pool.status_summary("steamdt"),
            )
        else:
            logger.info("[steamdt] no ready capsule available, using fallback session")
    request_capsule = leased_capsule or _fallback_capsule(cfg)
    headers, proxies = _prepare_capsule_request(request_capsule, cfg)
    strategies = _load_strategies(cfg)

    try:
        for strategy in strategies:
            for page in range(1, max(1, int(cfg["max_pages"])) + 1):
                raise_if_stop_requested()
                timestamp_ms = str(int(time.time() * 1000))
                payload = strategy.payload(cfg, page)
                payload["timestamp"] = timestamp_ms
                url = _steamdt_url(str(cfg["endpoint"]), timestamp_ms)
                logger.info(
                    "[steamdt] request strategy=%s page=%s %s capsule=%s",
                    strategy.name,
                    page,
                    proxy_log_tag(proxies),
                    request_capsule.capsule_id,
                )
                try:
                    response = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=cfg["timeout"])
                    response.raise_for_status()
                    data = response.json()
                except Exception as exc:
                    reason, cooldown_seconds = _classify_steamdt_failure(exc=exc)
                    logger.warning(
                        "[steamdt] request failed | strategy=%s capsule=%s reason=%s err=%s",
                        strategy.name,
                        request_capsule.capsule_id,
                        reason,
                        exc,
                    )
                    if leased_capsule:
                        _mark_capsule_failure(
                            capsule_pool,
                            leased_capsule,
                            reason,
                            min(cooldown_seconds, int(cfg["capsule_timeout_cooldown_seconds"])) if reason == "timeout" else cooldown_seconds,
                            cfg,
                        )
                    _maybe_alert_recapture_needed(capsule_pool, cfg)
                    return rows

                if isinstance(data, dict) and data.get("success") is False:
                    code = data.get("errorCode") or data.get("errorCodeStr")
                    msg = data.get("errorMsg") or data.get("message") or ""
                    reason, cooldown_seconds = _classify_steamdt_failure(response=response, payload=data)
                    if reason == "waf_block":
                        _WAF_COOLDOWN_UNTIL = time.time() + int(cfg["cooldown_seconds_on_waf"])
                        _save_waf_cooldown_until(_WAF_COOLDOWN_UNTIL)
                    logger.warning(
                        "[steamdt] business blocked | strategy=%s capsule=%s reason=%s code=%s msg=%s response=%s",
                        strategy.name,
                        request_capsule.capsule_id,
                        reason,
                        code,
                        msg,
                        str(data)[:500],
                    )
                    if leased_capsule:
                        mapped_cooldown = {
                            "timeout": int(cfg["capsule_timeout_cooldown_seconds"]),
                            "empty_soft_block": int(cfg["capsule_empty_cooldown_seconds"]),
                            "waf_block": int(cfg["capsule_waf_cooldown_seconds"]),
                            "auth_invalid": int(cfg["capsule_auth_cooldown_seconds"]),
                        }.get(reason, cooldown_seconds)
                        _mark_capsule_failure(
                            capsule_pool,
                            leased_capsule,
                            reason,
                            mapped_cooldown,
                            cfg,
                            auth_failure=reason == "auth_invalid",
                        )
                    _maybe_alert_recapture_needed(capsule_pool, cfg)
                    return rows

                page_rows = _extract_items(data)
                if not page_rows:
                    logger.info(
                        "[steamdt] empty page reached | strategy=%s page=%s capsule=%s",
                        strategy.name,
                        page,
                        request_capsule.capsule_id,
                    )
                for row in page_rows:
                    row["_steamdt_strategy"] = strategy.name
                    row["_steamdt_request"] = payload
                    row["_steamdt_capsule_id"] = request_capsule.capsule_id
                rows.extend(page_rows)
                logger.info("[steamdt] fetched strategy=%s page=%s rows=%s", strategy.name, page, len(page_rows))
                if not page_rows or len(page_rows) < int(cfg["page_size"]):
                    break
                time.sleep(random.uniform(float(cfg["sleep_min_seconds"]), float(cfg["sleep_max_seconds"])))
        if leased_capsule:
            capsule_pool.mark_success("steamdt", leased_capsule.capsule_id)
        _maybe_alert_recapture_needed(capsule_pool, cfg)
        return rows
    finally:
        if leased_capsule:
            capsule_pool.release_capsule("steamdt", leased_capsule.capsule_id)


def _build_name_index(db) -> dict[str, ItemBase]:
    rows = db.query(ItemBase).filter(ItemBase.is_active.is_(True)).all()
    index: dict[str, ItemBase] = {}
    for item in rows:
        if item.market_hash_name:
            index[item.market_hash_name.lower()] = item
        if item.cn_name:
            index[item.cn_name.lower()] = item
    return index


def _platform_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    platform_list = row.get("platformList") or row.get("platform_list") or row.get("platforms") or []
    return [platform for platform in platform_list if isinstance(platform, dict)] if isinstance(platform_list, list) else []


def _normalize_market_hash_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _extract_uuyp_template_id_from_platform_row(platform_row: dict[str, Any]) -> str | None:
    direct_keys = (
        "templateId",
        "template_id",
        "uuypTemplateId",
        "uuyp_template_id",
        "youpinId",
        "youpin_id",
        "uuypId",
        "uuyp_id",
        "goodsId",
        "goods_id",
    )
    for key in direct_keys:
        value = platform_row.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text

    link = str(_first_present(platform_row, ("linkUrl", "link_url", "url"), "")).strip()
    if not link:
        return None
    try:
        parsed = urlparse(link)
        params = parse_qs(parsed.query)
    except Exception:
        return None

    for key in ("templateId", "template_id", "goodsId", "goods_id", "id"):
        values = params.get(key)
        if values:
            text = str(values[0]).strip()
            if text:
                return text
    return None


def extract_uuyp_template_id_from_steamdt_row(row: dict[str, Any]) -> str | None:
    for platform_row in _platform_rows(row):
        platform_name = _normalize_platform_name(
            _first_present(platform_row, ("platformEnum", "platform", "platformName", "name", "source"))
        )
        if platform_name != "uuyp":
            continue
        template_id = _extract_uuyp_template_id_from_platform_row(platform_row)
        if template_id:
            return template_id
    return None


def get_uuyp_id_from_steamdt(market_hash_name: str, *, max_pages: int | None = None) -> str | None:
    expected = _normalize_market_hash_name(market_hash_name)
    if not expected:
        return None

    global _WAF_COOLDOWN_UNTIL
    cfg = _load_config()
    if not cfg["endpoint"]:
        logger.warning("[steamdt] uuyp-id lookup skipped: endpoint not configured")
        return None

    previous_enabled = cfg["enabled"]
    cfg["enabled"] = True
    original_max_pages = int(cfg["max_pages"])
    cfg["max_pages"] = max(1, int(max_pages or original_max_pages or 1))

    _WAF_COOLDOWN_UNTIL = max(_WAF_COOLDOWN_UNTIL, _load_waf_cooldown_until())
    if time.time() < _WAF_COOLDOWN_UNTIL:
        logger.warning(
            "[steamdt] uuyp-id lookup skipped by waf cooldown | remaining=%ss name=%s",
            round(_WAF_COOLDOWN_UNTIL - time.time(), 1),
            market_hash_name,
        )
        return None

    capsule_pool = SessionCapsulePool(CAPSULE_STATE_PATH)
    leased_capsule = None
    using_capsule_pool = bool(cfg.get("capsules_enabled"))
    if using_capsule_pool:
        leased_capsule = capsule_pool.lease_capsule("steamdt", lease_ttl_seconds=int(cfg["capsule_lease_ttl_seconds"]))
        if leased_capsule:
            logger.info(
                "[steamdt] uuyp-id lookup leased capsule=%s summary=%s",
                leased_capsule.capsule_id,
                capsule_pool.status_summary("steamdt"),
            )
        else:
            logger.info("[steamdt] uuyp-id lookup using fallback session")
    request_capsule = leased_capsule or _fallback_capsule(cfg)
    headers, proxies = _prepare_capsule_request(request_capsule, cfg)
    strategies = _load_strategies(cfg)

    try:
        for strategy in strategies:
            for page in range(1, max(1, int(cfg["max_pages"])) + 1):
                raise_if_stop_requested()
                timestamp_ms = str(int(time.time() * 1000))
                payload = strategy.payload(cfg, page)
                payload["timestamp"] = timestamp_ms
                url = _steamdt_url(str(cfg["endpoint"]), timestamp_ms)
                logger.info(
                    "[steamdt] uuyp-id lookup request | name=%s strategy=%s page=%s %s capsule=%s",
                    market_hash_name,
                    strategy.name,
                    page,
                    proxy_log_tag(proxies),
                    request_capsule.capsule_id,
                )
                try:
                    response = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=cfg["timeout"])
                    response.raise_for_status()
                    data = response.json()
                except Exception as exc:
                    reason, cooldown_seconds = _classify_steamdt_failure(exc=exc)
                    logger.warning(
                        "[steamdt] uuyp-id lookup request failed | name=%s strategy=%s capsule=%s reason=%s err=%s",
                        market_hash_name,
                        strategy.name,
                        request_capsule.capsule_id,
                        reason,
                        exc,
                    )
                    if leased_capsule:
                        _mark_capsule_failure(
                            capsule_pool,
                            leased_capsule,
                            reason,
                            min(cooldown_seconds, int(cfg["capsule_timeout_cooldown_seconds"])) if reason == "timeout" else cooldown_seconds,
                            cfg,
                        )
                    _maybe_alert_recapture_needed(capsule_pool, cfg)
                    return None

                if isinstance(data, dict) and data.get("success") is False:
                    code = data.get("errorCode") or data.get("errorCodeStr")
                    msg = data.get("errorMsg") or data.get("message") or ""
                    reason, cooldown_seconds = _classify_steamdt_failure(response=response, payload=data)
                    if reason == "waf_block":
                        _WAF_COOLDOWN_UNTIL = time.time() + int(cfg["cooldown_seconds_on_waf"])
                        _save_waf_cooldown_until(_WAF_COOLDOWN_UNTIL)
                    logger.warning(
                        "[steamdt] uuyp-id lookup blocked | name=%s strategy=%s capsule=%s reason=%s code=%s msg=%s",
                        market_hash_name,
                        strategy.name,
                        request_capsule.capsule_id,
                        reason,
                        code,
                        msg,
                    )
                    if leased_capsule:
                        mapped_cooldown = {
                            "timeout": int(cfg["capsule_timeout_cooldown_seconds"]),
                            "empty_soft_block": int(cfg["capsule_empty_cooldown_seconds"]),
                            "waf_block": int(cfg["capsule_waf_cooldown_seconds"]),
                            "auth_invalid": int(cfg["capsule_auth_cooldown_seconds"]),
                        }.get(reason, cooldown_seconds)
                        _mark_capsule_failure(
                            capsule_pool,
                            leased_capsule,
                            reason,
                            mapped_cooldown,
                            cfg,
                            auth_failure=reason == "auth_invalid",
                        )
                    _maybe_alert_recapture_needed(capsule_pool, cfg)
                    return None

                page_rows = _extract_items(data)
                for row in page_rows:
                    candidate_name = _normalize_market_hash_name(
                        _first_present(row, ("market_hash_name", "marketHashName", "hash_name", "hashName", "name", "item_name", "itemName"), "")
                    )
                    if candidate_name != expected:
                        continue
                    template_id = extract_uuyp_template_id_from_steamdt_row(row)
                    if template_id:
                        logger.info(
                            "[steamdt] uuyp-id lookup resolved | name=%s template_id=%s strategy=%s page=%s capsule=%s",
                            market_hash_name,
                            template_id,
                            strategy.name,
                            page,
                            request_capsule.capsule_id,
                        )
                        if leased_capsule:
                            capsule_pool.mark_success("steamdt", leased_capsule.capsule_id)
                        return template_id
                if not page_rows or len(page_rows) < int(cfg["page_size"]):
                    break
                time.sleep(random.uniform(float(cfg["sleep_min_seconds"]), float(cfg["sleep_max_seconds"])))

        if leased_capsule:
            capsule_pool.mark_success("steamdt", leased_capsule.capsule_id)
        logger.info("[steamdt] uuyp-id lookup miss | name=%s", market_hash_name)
        return None
    finally:
        cfg["enabled"] = previous_enabled
        cfg["max_pages"] = original_max_pages
        if leased_capsule:
            capsule_pool.release_capsule("steamdt", leased_capsule.capsule_id)


def _steam_platform_row(row: dict[str, Any]) -> dict[str, Any] | None:
    for platform_row in _platform_rows(row):
        if _normalize_platform_name(_first_present(platform_row, ("platformEnum", "platform", "platformName", "name", "source"))) == "steam":
            return platform_row
    return None


def _strategy_from_row(row: dict[str, Any]) -> SteamDTStrategy:
    name = str(row.get("_steamdt_strategy") or "")
    for strategy in DEFAULT_STRATEGIES:
        if strategy.name == name:
            return strategy
    request = row.get("_steamdt_request") if isinstance(row.get("_steamdt_request"), dict) else {}
    for strategy in DEFAULT_STRATEGIES:
        if (
            strategy.want_to_get == request.get("wantToGet")
            and strategy.purchase_plan == request.get("purchasePlan", "")
            and strategy.sale_plan == request.get("salePlan", "")
        ):
            return strategy
    return DEFAULT_STRATEGIES[0]


def normalize_steamdt_row(row: dict[str, Any], item: ItemBase, strategy: SteamDTStrategy | None = None) -> dict[str, Any]:
    strategy = strategy or _strategy_from_row(row)
    steam_row = _steam_platform_row(row) or {}
    steam_sell = _safe_float(_first_present(steam_row, ("price", "sell_min", "sellMin"), _first_present(row, ("steam_sell_min", "steamSellMin"))))
    steam_buy = _safe_float(_first_present(steam_row, ("biddingPrice", "bidding_price", "buy_max", "buyMax"), _first_present(row, ("steam_buy_max", "steamBuyMax"))))
    if not steam_sell and strategy.steam_price_role == "sell_min":
        steam_sell = _safe_float(_first_present(row, ("steamPrice", "steam_price", "steamPriceCny", "price")))
    if not steam_buy and strategy.steam_price_role == "buy_max":
        steam_buy = _safe_float(_first_present(row, ("steamPrice", "steam_price", "steamBuyPrice", "buy_max")))
    volume_24h = _safe_int(_first_present(row, ("transactionCount", "transaction_count", "volume", "vol")))
    sell_volume = _safe_int(_first_present(steam_row, ("sellNum", "sell_num", "sellVolume", "sell_volume")))
    buy_volume = _safe_int(_first_present(steam_row, ("biddingCount", "bidding_count", "buyVolume", "buy_volume")))
    data_timestamp = parse_relative_time(_first_present(row, ("steamUpdateTime", "steam_update_time", "updated_at", "updatedAt")))
    currency_state = _detect_currency(row, steam_row)
    return {
        "item_id": item.id,
        "platform_name": "steam",
        "data_source": "steamdt",
        "sell_min": steam_sell,
        "sell_top5_avg": steam_sell,
        "buy_max": steam_buy,
        "buy_top5_avg": steam_buy,
        "volume": volume_24h,
        "sell_volume": sell_volume,
        "buy_volume": buy_volume,
        "currency": "CNY",
        "source_currency_state": currency_state,
        "new_timestamp": data_timestamp,
        "quote_quality": _quote_quality(steam_sell, steam_buy, volume_24h, data_timestamp, currency_state),
    }


def normalize_steamdt_platform_rows(
    row: dict[str, Any], item: ItemBase, strategy: SteamDTStrategy | None = None
) -> list[dict[str, Any]]:
    strategy = strategy or _strategy_from_row(row)
    rows: list[dict[str, Any]] = [normalize_steamdt_row(row, item, strategy)]
    for platform_row in _platform_rows(row):
        platform_name = _normalize_platform_name(
            _first_present(platform_row, ("platformEnum", "platform", "platformName", "name", "source"))
        )
        if platform_name not in SUPPORTED_MARKET_PLATFORMS or platform_name == "steam":
            continue
        sell_min = _safe_float(_first_present(platform_row, ("price", "sell_min", "sellMin", "lowestPrice", "lowest_price")))
        buy_max = _safe_float(_first_present(platform_row, ("biddingPrice", "bidding_price", "buy_max", "buyMax", "orderPrice", "order_price")))
        sell_volume = _safe_int(_first_present(platform_row, ("sellNum", "sell_num", "sellVolume", "sell_volume")))
        buy_volume = _safe_int(_first_present(platform_row, ("biddingCount", "bidding_count", "buyVolume", "buy_volume")))
        volume = sell_volume or _safe_int(_first_present(row, ("transactionCount", "transaction_count", "volume", "vol")))
        raw_time = _first_present(platform_row, ("updateTime", "updatedAt", "updated_at", "platformUpdateTime", "time"), None)
        if raw_time is None:
            raw_time = _first_present(row, ("platformUpdateTime", "platform_update_time", "updateTime", "updatedAt", "updated_at"))
        data_timestamp = parse_relative_time(raw_time)
        currency_state = _detect_currency(row, platform_row)
        rows.append(
            {
                "item_id": item.id,
                "platform_name": platform_name,
                "data_source": "steamdt",
                "sell_min": sell_min,
                "sell_top5_avg": sell_min,
                "buy_max": buy_max,
                "buy_top5_avg": buy_max,
                "volume": volume,
                "sell_volume": sell_volume,
                "buy_volume": buy_volume,
                "currency": "CNY",
                "source_currency_state": currency_state,
                "new_timestamp": data_timestamp,
                "quote_quality": _quote_quality(sell_min, buy_max, volume, data_timestamp, currency_state),
            }
        )
    return rows


def _opportunity_rows(row: dict[str, Any], item: ItemBase, strategy: SteamDTStrategy) -> list[dict[str, Any]]:
    steam_row = normalize_steamdt_row(row, item, strategy)
    steam_sell = float(steam_row.get("sell_min") or 0)
    steam_buy = float(steam_row.get("buy_max") or 0)
    steam_ts = steam_row.get("new_timestamp")
    platform_ts = parse_relative_time(_first_present(row, ("platformUpdateTime", "platform_update_time", "updateTime")))
    steamdt_ts = parse_relative_time(_first_present(row, ("updateTime", "updatedAt", "updated_at"), None))
    volume_24h = int(steam_row.get("volume") or 0)
    opportunities: list[dict[str, Any]] = []
    for platform_row in _platform_rows(row):
        platform_name = _normalize_platform_name(
            _first_present(platform_row, ("platformEnum", "platform", "platformName", "name", "source"))
        )
        if platform_name not in {"buff", "uuyp", "eco"}:
            continue
        platform_sell = _safe_float(_first_present(platform_row, ("price", "sell_min", "sellMin", "lowestPrice", "lowest_price")))
        platform_buy = _safe_float(_first_present(platform_row, ("biddingPrice", "bidding_price", "buy_max", "buyMax")))
        sell_volume = _safe_int(_first_present(platform_row, ("sellNum", "sell_num", "sellVolume", "sell_volume")))
        buy_volume = _safe_int(_first_present(platform_row, ("biddingCount", "bidding_count", "buyVolume", "buy_volume")))
        source_price = steam_buy if strategy.steam_price_role == "buy_max" else steam_sell
        target_price = platform_sell if strategy.platform_price_role == "sell_min" else platform_buy
        if source_price <= 0 or target_price <= 0:
            profit_cny = 0.0
            profit_rate = 0.0
        elif strategy.platform_price_role == "buy_max":
            profit_cny = target_price - source_price
            profit_rate = profit_cny / source_price * 100.0
        else:
            profit_cny = source_price - target_price
            profit_rate = profit_cny / target_price * 100.0
        opportunities.append(
            {
                "item_id": item.id,
                "strategy_name": strategy.name,
                "market_hash_name": item.market_hash_name,
                "item_name": item.cn_name or item.market_hash_name,
                "platform_name": platform_name,
                "steam_sell_min": steam_sell or None,
                "steam_buy_max": steam_buy or None,
                "platform_sell_min": platform_sell or None,
                "platform_buy_max": platform_buy or None,
                "transaction_count_24h": volume_24h,
                "platform_sell_volume": sell_volume,
                "platform_buy_volume": buy_volume,
                "profit_cny": round(profit_cny, 4),
                "profit_rate": round(profit_rate, 4),
                "currency": "CNY",
                "link_url": _first_present(platform_row, ("linkUrl", "link_url", "url")),
                "steam_updated_at": steam_ts,
                "platform_updated_at": platform_ts,
                "steamdt_updated_at": steamdt_ts,
            }
        )
    return opportunities


def _upsert_steamdt_opportunities(db, rows: list[dict[str, Any]]) -> int:
    updated = 0
    for row in rows:
        stmt = sqlite_insert(SteamDTOpportunity).values(**row)
        update_values = {
            key: stmt.excluded[key]
            for key in row
            if key not in {"item_id", "strategy_name", "platform_name"}
        }
        update_values["updated_at"] = datetime.now()
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["item_id", "strategy_name", "platform_name"],
                set_=update_values,
            )
        )
        updated += 1
    return updated


def save_steamdt_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    updated = 0
    skipped = 0
    skipped_currency = 0
    opportunity_updates = 0
    touched_item_ids: set[int] = set()
    db = SessionLocal()
    try:
        name_index = _build_name_index(db)
        for row in rows:
            raise_if_stop_requested()
            strategy = _strategy_from_row(row)
            name = str(_first_present(row, ("market_hash_name", "marketHashName", "hash_name", "hashName", "name", "item_name", "itemName"), "")).strip()
            item = name_index.get(name.lower())
            if item is None:
                skipped += 1
                continue
            touched_item_ids.add(int(item.id))
            for normalized in normalize_steamdt_platform_rows(row, item, strategy):
                if normalized["quote_quality"] == "invalid_currency":
                    skipped_currency += 1
                    logger.warning(
                        "[steamdt] non-CNY quote skipped | item_id=%s platform=%s source_currency=%s",
                        normalized["item_id"],
                        normalized["platform_name"],
                        normalized.get("source_currency_state"),
                    )
                    continue
                if normalized["quote_quality"] == "invalid":
                    skipped += 1
                    continue
                changed = upsert_market_price_if_fresh(
                    db,
                    item_id=normalized["item_id"],
                    platform_name=normalized["platform_name"],
                    data_source=normalized["data_source"],
                    sell_min=normalized["sell_min"],
                    sell_top5_avg=normalized["sell_top5_avg"],
                    buy_max=normalized["buy_max"],
                    buy_top5_avg=normalized["buy_top5_avg"],
                    volume=normalized["volume"],
                    sell_volume=normalized.get("sell_volume"),
                    buy_volume=normalized.get("buy_volume"),
                    currency="CNY",
                    new_timestamp=normalized["new_timestamp"],
                    log=logger,
                )
                if changed:
                    updated += 1
            opportunity_updates += _upsert_steamdt_opportunities(db, _opportunity_rows(row, item, strategy))
        db.commit()
        if touched_item_ids:
            try:
                from DataEngine.priority_scheduler import recalculate_priorities

                recalculate_priorities(item_ids=touched_item_ids)
            except Exception as exc:
                logger.warning("[steamdt] priority scheduler update failed | items=%s err=%s", len(touched_item_ids), exc)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    logger.info(
        "[steamdt] saved market_rows=%s opportunities=%s skipped=%s skipped_currency=%s",
        updated,
        opportunity_updates,
        skipped,
        skipped_currency,
    )
    return updated


def run_once() -> int:
    start = time.perf_counter()
    rows = fetch_steamdt_rows()
    updated = save_steamdt_rows(rows)
    logger.info("[steamdt] round done | updated=%s cost=%.2fs", updated, time.perf_counter() - start)
    return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch or validate SteamDT hanging-knife data.")
    parser.add_argument("--sample", action="store_true", help="Use local captured sample payload instead of live SteamDT.")
    parser.add_argument("--sample-path", default="", help="Optional sample JSON path.")
    parser.add_argument("--sample-refresh-time", action="store_true", help="Mark sample rows as current for local UI smoke tests.")
    args = parser.parse_args()
    if args.sample:
        sample_rows = load_sample_rows(args.sample_path or None, refresh_time=args.sample_refresh_time)
        updated = save_steamdt_rows(sample_rows)
        logger.info("[steamdt] sample round done | updated=%s", updated)
    else:
        run_once()
