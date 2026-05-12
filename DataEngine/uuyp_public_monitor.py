import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl

from curl_cffi.requests import AsyncSession
from sqlalchemy.orm import Session

from DataEngine.database import ItemBase, PlatformMapping, SessionLocal
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.proxy_observer import proxy_tag
from DataEngine.proxy_pool import (
    classify_request_failure,
    get_request_proxies,
    mark_proxy_failure,
    mark_proxy_success,
    proxy_cooldown_for_reason,
    proxy_log_tag,
)
from DataEngine.stop_signal import raise_if_stop_requested

setup_dataengine_logging()
logger = logging.getLogger(__name__)

PLATFORM_NAME = "uuyp"
UUYP_CNY_COOKIES = {"currency": "CNY"}
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
CONCURRENCY = 1
CIRCUIT_COOLDOWN_SECONDS = 180

BASE_DIR = Path(__file__).resolve().parent.parent
UUYP_HEADERS_PATH = BASE_DIR / "DataEngine" / "uuyp_headers.json"
UUYP_CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"

_auth_circuit_open_until = 0.0
_empty_quote_debug_count = 0
EMPTY_QUOTE_DEBUG_LIMIT = 8
UUYP_UK_TTL_SECONDS = 30
_uuyp_uk_cache_value = ""
_uuyp_uk_cache_time = 0.0


class UuypAuthRequired(RuntimeError):
    pass


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _loop_time() -> float:
    try:
        return asyncio.get_event_loop().time()
    except RuntimeError:
        return 0.0


def is_uuyp_auth_circuit_open() -> bool:
    return _auth_circuit_open_until > _loop_time()


def uuyp_auth_circuit_remaining_seconds() -> int:
    return max(0, int(_auth_circuit_open_until - _loop_time()))


def _open_uuyp_auth_circuit(reason: str) -> None:
    global _auth_circuit_open_until
    _auth_circuit_open_until = _loop_time() + CIRCUIT_COOLDOWN_SECONDS
    logger.warning(
        "[uuyp-app] auth/business circuit opened | cooldown=%ss reason=%s",
        CIRCUIT_COOLDOWN_SECONDS,
        reason,
    )


def _parse_cookie_str(cookie_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in parse_qsl((cookie_str or "").replace(";", "&"), keep_blank_values=True):
        if key:
            out[key.strip()] = value.strip()
    return out


def _load_uuyp_credentials() -> tuple[dict[str, str], dict[str, str]]:
    cookies: dict[str, str] = {}
    headers: dict[str, str] = {}

    raw_cookie = os.getenv("UUYP_COOKIE", "").strip()
    if raw_cookie:
        cookies.update(_parse_cookie_str(raw_cookie))

    try:
        if UUYP_CREDENTIALS_PATH.exists():
            data = json.loads(UUYP_CREDENTIALS_PATH.read_text(encoding="utf-8") or "{}")
            uuyp = data.get("uuyp") if isinstance(data, dict) else {}
            if isinstance(uuyp, dict):
                cookie_str = str(uuyp.get("cookies") or uuyp.get("cookie") or "").strip()
                if cookie_str:
                    cookies.update(_parse_cookie_str(cookie_str))
                for key, value in uuyp.items():
                    if value is not None and key not in {"cookies", "cookie"}:
                        cookies.setdefault(str(key), str(value))
    except Exception as exc:
        logger.warning("load UUYP credentials failed: %s", exc)

    try:
        if UUYP_HEADERS_PATH.exists():
            data = json.loads(UUYP_HEADERS_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                headers.update({str(k): str(v) for k, v in data.items() if v is not None and str(v).strip()})
    except Exception as exc:
        logger.warning("load UUYP headers failed: %s", exc)

    for key in ("deviceId", "deviceid", "deviceUk", "deviceuk", "uk", "clientType", "Client-Type"):
        if cookies.get(key):
            headers.setdefault(key, cookies[key])

    cookies["currency"] = "CNY"
    return cookies, headers


def _is_auth_required_message(msg: str) -> bool:
    text = (msg or "").lower()
    return any(token in text for token in ("登录", "登陆", "login", "auth", "unauthorized", "token", "风控", "验证"))

def _safe_snippet(value: Any, limit: int = 1200) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception as exc:
        text = f"<unserializable:{exc}>"
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text[:limit] + ("...<truncated>" if len(text) > limit else "")


def _diagnose_uuyp_empty_quote(template_id: str, data: dict[str, Any], response) -> None:
    global _empty_quote_debug_count
    if _empty_quote_debug_count >= EMPTY_QUOTE_DEBUG_LIMIT:
        return
    _empty_quote_debug_count += 1
    try:
        response_headers = dict(getattr(response, "headers", {}) or {})
    except Exception:
        response_headers = {}
    debug_headers = {
        key: value
        for key, value in response_headers.items()
        if key.lower() in {"content-type", "x-request-id", "x-trace-id", "server", "date", "set-cookie"}
    }
    logger.debug(
        "[uuyp-app] empty quote raw response | template_id=%s status=%s headers=%s json=%s text=%s",
        template_id,
        getattr(response, "status_code", None),
        _safe_snippet(debug_headers, 600),
        _safe_snippet(data),
        _safe_snippet(getattr(response, "text", ""), 1200),
    )


def _canonicalize_uuyp_headers(headers: dict[str, str]) -> dict[str, str]:
    aliases = {
        "accept": "Accept",
        "accept-language": "Accept-Language",
        "accept-encoding": "Accept-Encoding",
        "cache-control": "Cache-Control",
        "connection": "Connection",
        "content-type": "Content-Type",
        "origin": "Origin",
        "referer": "Referer",
        "user-agent": "User-Agent",
        "app-version": "App-Version",
        "appversion": "App-Version",
        "apptype": "appType",
        "platform": "platform",
        "secret-v": "secret-v",
        "secret_v": "secret-v",
        "deviceid": "deviceId",
        "deviceuk": "deviceUk",
        "uk": "uk",
    }
    out: dict[str, str] = {}
    for raw_key, raw_value in (headers or {}).items():
        key = str(raw_key or "").strip()
        value = str(raw_value or "").strip()
        if not key or not value:
            continue
        out[aliases.get(key.lower(), key)] = value
    if out.get("App-Version") and "AppVersion" not in out:
        out["AppVersion"] = out["App-Version"]
    return out


def _current_uuyp_uk(headers: dict[str, str]) -> str:
    global _uuyp_uk_cache_value, _uuyp_uk_cache_time
    now = time.time()
    if _uuyp_uk_cache_value and now - _uuyp_uk_cache_time <= UUYP_UK_TTL_SECONDS:
        return _uuyp_uk_cache_value
    try:
        from uuyp.buyer import _fetch_uuyp_uk

        fresh = _fetch_uuyp_uk(headers=headers)
    except Exception as exc:
        logger.debug("[uuyp-app] dynamic uk refresh failed: %s", exc)
        fresh = ""
    if fresh:
        _uuyp_uk_cache_value = fresh
        _uuyp_uk_cache_time = now
        return fresh
    return str(headers.get("uk") or headers.get("deviceUk") or "").strip()


def _build_uuyp_request_context(template_id: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    credential_cookies, credential_headers = _load_uuyp_credentials()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
        "Origin": "https://www.youpin898.com",
        "Pragma": "no-cache",
        "Referer": "https://www.youpin898.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "platform": "pc",
        "appType": "1",
        "App-Version": "5.26.0",
        "AppVersion": "5.26.0",
        "secret-v": "h5_v1",
    }
    if template_id:
        headers["Referer"] = (
            f"https://www.youpin898.com/market/goods-list?listType=10&templateId={template_id}&gameId=730"
        )
    headers.update(_canonicalize_uuyp_headers(credential_headers))
    current_uk = _current_uuyp_uk(headers)
    if current_uk:
        headers["uk"] = current_uk
    cookies = dict(UUYP_CNY_COOKIES)
    cookies.update(credential_cookies)
    return headers, cookies


async def _post_uuyp_json(
    request_name: str,
    url: str,
    payload: dict[str, Any],
    template_id: str,
    session: AsyncSession,
) -> tuple[Optional[dict[str, Any]], Any]:
    headers, cookies = _build_uuyp_request_context(template_id)
    for attempt in range(1, MAX_RETRIES + 2):
        if is_uuyp_auth_circuit_open():
            return None, None
        proxies = None
        try:
            proxies = get_request_proxies(failed=attempt > 1, force=True, platform="uuyp")
            logger.info(
                "[uuyp-app] %s request %s template_id=%s attempt=%s/%s",
                request_name,
                proxy_log_tag(proxies),
                template_id,
                attempt,
                MAX_RETRIES + 1,
            )
            response = await session.post(
                url,
                headers=headers,
                cookies=cookies,
                json=payload,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT,
                impersonate="chrome120",
                default_headers=True,
            )
            raise_if_stop_requested()

            if response.status_code == 429 and attempt <= MAX_RETRIES:
                sleep_for = 2 ** (attempt - 1)
                logger.info("[%s] %s 429, retrying sleep=%ss", template_id, request_name, sleep_for)
                await asyncio.sleep(sleep_for)
                continue

            if response.status_code != 200:
                reason = classify_request_failure(status_code=response.status_code)
                if proxies:
                    mark_proxy_failure(
                        proxies,
                        reason=f"uuyp_{reason}",
                        cooldown_seconds=proxy_cooldown_for_reason(reason),
                    )
                if response.status_code == 401:
                    _open_uuyp_auth_circuit(f"{request_name}_http_status={response.status_code}")
                logger.error(
                    "[%s] %s refused: %s reason=%s text=%s",
                    template_id,
                    request_name,
                    response.status_code,
                    reason,
                    _safe_snippet(getattr(response, "text", ""), 240),
                )
                return None, response

            data = response.json()
            if data.get("Code") != 0:
                msg = str(data.get("Msg") or data.get("msg") or "")
                if _is_auth_required_message(msg):
                    _open_uuyp_auth_circuit(f"{request_name}:{msg}")
                    raise UuypAuthRequired(msg)
                logger.warning("[%s] %s business error: %s", template_id, request_name, msg)
                return None, response

            mark_proxy_success(proxies)
            return data, response
        except asyncio.CancelledError:
            raise
        except UuypAuthRequired as exc:
            logger.warning("[%s] UUYP_AUTH_REQUIRED during %s: %s", template_id, request_name, exc)
            return None, None
        except Exception as exc:
            reason = classify_request_failure(exc)
            mark_proxy_failure(proxies, reason=reason, cooldown_seconds=proxy_cooldown_for_reason(reason))
            if attempt <= MAX_RETRIES:
                sleep_for = 2 ** (attempt - 1)
                logger.info(
                    "[%s] %s jitter, retrying sleep=%ss reason=%s err=%s",
                    template_id,
                    request_name,
                    sleep_for,
                    reason,
                    exc,
                )
                await asyncio.sleep(sleep_for)
                continue
            logger.error("[%s] %s network error reason=%s: %s", template_id, request_name, reason, exc)
            return None, None


async def fetch_uuyp_item_app(template_id: str, session: AsyncSession) -> Optional[List[Dict[str, Any]]]:
    raise_if_stop_requested()
    if is_uuyp_auth_circuit_open():
        return None

    url = "https://api.youpin898.com/api/homepage/pc/goods/market/queryOnSaleCommodityList"
    payload = {
        "templateId": str(template_id),
        "gameId": "730",
        "listSortType": 1,
        "listType": "10",
        "pageIndex": 1,
        "pageSize": 10,
        "sortType": 0,
    }

    data, response = await _post_uuyp_json("sale list", url, payload, str(template_id), session)
    if data is None:
        return None

    data_payload = data.get("Data", [])
    payload_list = data_payload if isinstance(data_payload, list) else data_payload.get("list", [])
    if not payload_list:
        _diagnose_uuyp_empty_quote(str(template_id), data, response)
    return payload_list


def _extract_uuyp_template_detail_identity(data: dict[str, Any]) -> str:
    payload = data.get("Data") if isinstance(data, dict) else {}
    template_info = payload.get("templateInfo", {}) if isinstance(payload, dict) else {}
    if not isinstance(template_info, dict):
        return ""
    for key in ("commodityHashName", "CommodityHashName", "commodityName", "CommodityName", "name", "Name"):
        value = template_info.get(key)
        if value:
            return str(value).strip()
    return ""


async def fetch_uuyp_template_detail_name(template_id: str, session: AsyncSession) -> Optional[str]:
    raise_if_stop_requested()
    if is_uuyp_auth_circuit_open():
        return None

    url = "https://api.youpin898.com/api/homepage/pc/goods/market/queryTemplateDetail"
    payload = {
        "templateId": str(template_id),
        "gameId": 730,
        "listType": 10,
    }
    data, _ = await _post_uuyp_json("template detail", url, payload, str(template_id), session)
    if data is None:
        return None
    identity = _extract_uuyp_template_detail_identity(data)
    if not identity:
        logger.warning("[uuyp-app] template detail missing identity | template_id=%s", template_id)
        return None
    return identity


def _clean_uuyp_payload(items: List[Dict[str, Any]]) -> tuple[float, float, int]:
    if not items:
        return 0.0, 0.0, 0
    item_list = items if isinstance(items, list) else items.get("list", [])
    if not item_list:
        return 0.0, 0.0, 0

    prices = []
    for item in item_list[:5]:
        try:
            raw_price = (
                item.get("price")
                or item.get("sellPrice")
                or item.get("sellingPrice")
                or item.get("discountPrice")
                or item.get("Price")
                or item.get("SellPrice")
                or item.get("commodityPrice")
                or 0
            )
            price = float(raw_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            prices.append(price)
    if not prices:
        return 0.0, 0.0, 0
    prices.sort()
    volume = 0
    for item in item_list:
        try:
            volume += int(item.get("OnSaleCount") or item.get("onSaleCount") or 0)
        except (TypeError, ValueError):
            continue
    return prices[0], round(sum(prices) / len(prices), 2), volume or len(item_list)


def _extract_uuyp_identity(items: List[Dict[str, Any]]) -> str:
    item_list = items if isinstance(items, list) else items.get("list", [])
    for item in item_list:
        for key in ("CommodityHashName", "commodityHashName", "CommodityName", "commodityName", "Name", "name"):
            value = item.get(key)
            if value:
                return str(value).strip()
    return ""


def _persist_uuyp_template_mapping(item_id: int, market_hash_name: str, resolved_template_id: str) -> None:
    session: Session = SessionLocal()
    try:
        item = session.query(ItemBase).filter(ItemBase.id == int(item_id)).one_or_none()
        if item is None:
            return
        item.uuyp_template_id = int(resolved_template_id)
        session.add(item)
        mapping = (
            session.query(PlatformMapping)
            .filter(PlatformMapping.item_id == int(item_id), PlatformMapping.platform_name == PLATFORM_NAME)
            .one_or_none()
        )
        if mapping is None:
            mapping = PlatformMapping(
                item_id=int(item_id),
                platform_name=PLATFORM_NAME,
                platform_item_id=str(resolved_template_id),
            )
        else:
            mapping.platform_item_id = str(resolved_template_id)
        session.add(mapping)
        session.commit()
        logger.info(
            "[uuyp-app] template mapping refreshed | item_id=%s template_id=%s market_hash_name=%s",
            item_id,
            resolved_template_id,
            market_hash_name,
        )
    except Exception as exc:
        session.rollback()
        logger.warning(
            "[uuyp-app] persist template mapping failed | item_id=%s template_id=%s err=%s",
            item_id,
            resolved_template_id,
            exc,
        )
    finally:
        session.close()


def _lookup_local_uuyp_template_id(item_id: int, current_template_id: str) -> Optional[str]:
    session: Session = SessionLocal()
    try:
        item = session.query(ItemBase).filter(ItemBase.id == int(item_id)).one_or_none()
        if item is None:
            return None

        candidates: list[str] = []
        hot_id = getattr(item, "uuyp_template_id", None)
        if hot_id not in (None, ""):
            candidates.append(str(hot_id).strip())

        mapping = (
            session.query(PlatformMapping)
            .filter(PlatformMapping.item_id == int(item_id), PlatformMapping.platform_name == PLATFORM_NAME)
            .one_or_none()
        )
        if mapping and mapping.platform_item_id:
            candidates.append(str(mapping.platform_item_id).strip())

        current = str(current_template_id or "").strip()
        for candidate in candidates:
            if candidate and candidate != current:
                return candidate
        return None
    except Exception as exc:
        logger.warning("[uuyp-app] local template lookup failed | item_id=%s err=%s", item_id, exc)
        return None
    finally:
        session.close()


async def _resolve_verified_uuyp_template(
    item_id: int,
    expected_name: str,
    template_id: str,
    session: AsyncSession,
) -> tuple[Optional[str], str]:
    resolved_template_id = str(template_id)
    if not expected_name:
        return resolved_template_id, ""

    detail_name = await fetch_uuyp_template_detail_name(resolved_template_id, session)
    if detail_name and _normalize_name(detail_name) == _normalize_name(expected_name):
        return resolved_template_id, detail_name

    if detail_name:
        logger.warning(
            "[uuyp-app] template detail mismatch detected | item_id=%s expected=%s actual=%s template_id=%s",
            item_id,
            expected_name,
            detail_name,
            resolved_template_id,
        )
    else:
        logger.warning(
            "[uuyp-app] template detail unavailable | item_id=%s expected=%s template_id=%s",
            item_id,
            expected_name,
            resolved_template_id,
        )

    refreshed_template_id = _lookup_local_uuyp_template_id(int(item_id), resolved_template_id)
    if not refreshed_template_id:
        logger.warning(
            "[uuyp-app] local template lookup miss | item_id=%s expected=%s current_template_id=%s",
            item_id,
            expected_name,
            resolved_template_id,
        )
        return None, detail_name or ""

    logger.info(
        "[uuyp-app] template retry start | item_id=%s expected=%s old_template_id=%s candidate_template_id=%s",
        item_id,
        expected_name,
        resolved_template_id,
        refreshed_template_id,
    )
    refreshed_detail_name = await fetch_uuyp_template_detail_name(str(refreshed_template_id), session)
    if not refreshed_detail_name:
        logger.warning(
            "[uuyp-app] template retry detail unavailable | item_id=%s expected=%s template_id=%s",
            item_id,
            expected_name,
            refreshed_template_id,
        )
        return None, detail_name or ""
    if _normalize_name(refreshed_detail_name) != _normalize_name(expected_name):
        logger.warning(
            "[uuyp-app] template retry still mismatched | item_id=%s expected=%s actual=%s template_id=%s",
            item_id,
            expected_name,
            refreshed_detail_name,
            refreshed_template_id,
        )
        return None, detail_name or ""

    _persist_uuyp_template_mapping(int(item_id), expected_name, str(refreshed_template_id))
    logger.info(
        "[uuyp-app] template retry success | item_id=%s expected=%s template_id=%s actual=%s",
        item_id,
        expected_name,
        refreshed_template_id,
        refreshed_detail_name,
    )
    return str(refreshed_template_id), refreshed_detail_name


async def _fetch_single_uuyp_item(item: Dict[str, Any], session: AsyncSession) -> Optional[Dict[str, Any]]:
    if is_uuyp_auth_circuit_open():
        return None
    template_id = item.get("platform_id")
    item_id = item.get("item_id")
    expected_name = str(item.get("market_hash_name") or "").strip()
    if not template_id or item_id is None:
        return None

    resolved_template_id = str(template_id)
    verified_name = expected_name
    if expected_name:
        resolved_template_id, verified_name = await _resolve_verified_uuyp_template(
            int(item_id),
            expected_name,
            resolved_template_id,
            session,
        )
        if not resolved_template_id:
            return None

    payload = await fetch_uuyp_item_app(resolved_template_id, session)
    if payload is None:
        return None

    actual_name = _extract_uuyp_identity(payload)
    expected_identity = verified_name or expected_name
    if expected_identity and (
        not actual_name or _normalize_name(expected_identity) != _normalize_name(actual_name)
    ):
        logger.warning(
            "[uuyp-app] quote skipped by identity mismatch | item_id=%s expected=%s actual=%s template_id=%s",
            item_id,
            expected_identity,
            actual_name,
            resolved_template_id,
        )
        return None

    sell_min, sell_top5_avg, volume = _clean_uuyp_payload(payload)
    if sell_min <= 0 and volume <= 0:
        logger.info("[uuyp-app] invalid empty quote skipped | template_id=%s item_id=%s", resolved_template_id, item_id)
        return None

    result = {
        "item_id": item_id,
        "platform_name": PLATFORM_NAME,
        "sell_min": sell_min,
        "sell_top5_avg": sell_top5_avg,
        "buy_max": 0,
        "buy_top5_avg": 0,
        "volume": volume,
        "currency": "CNY",
    }
    logger.info("[uuyp-app] %s %s | %s", proxy_tag(None), resolved_template_id, result)
    return result


async def fetch_uuyp_prices(items: List[Dict[str, Any]], session: AsyncSession) -> List[Dict[str, Any]]:
    if not items:
        return []
    if is_uuyp_auth_circuit_open():
        logger.warning(
            "[uuyp-app] batch skipped by auth circuit | items=%s cooldown=%ss",
            len(items),
            uuyp_auth_circuit_remaining_seconds(),
        )
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _safe_fetch(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            raise_if_stop_requested()
            if is_uuyp_auth_circuit_open():
                return None
            await asyncio.sleep(0.3)
            raise_if_stop_requested()
            if is_uuyp_auth_circuit_open():
                return None
            return await _fetch_single_uuyp_item(item, session)

    completed = await asyncio.gather(*(_safe_fetch(item) for item in items), return_exceptions=True)
    results: list[dict] = []
    for res in completed:
        if isinstance(res, dict):
            results.append(res)
        elif isinstance(res, asyncio.CancelledError):
            raise res
        elif isinstance(res, Exception):
            logger.warning("[uuyp-app] item task failed: %s", res)
    return results
