import asyncio
import json
import logging
import os
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

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
from DataEngine.logging_setup import setup_dataengine_logging

logger = logging.getLogger(__name__)
setup_dataengine_logging()

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
STEAM_DICT_PATH = os.path.join(CURRENT_DIR, "SteamTradingSite-ID-Mapper-main", "steam", "730.json")
PLATFORM_NAME = "steam"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
CONCURRENCY = 2
JIT_REQUEST_TIMEOUT = 8
JIT_MAX_RETRIES = 0
CIRCUIT_FAILURE_THRESHOLD = 2
CIRCUIT_COOLDOWN_SECONDS = 180
_circuit_failures = 0
_circuit_open_until = 0.0

steam_dict: dict = {}
if os.path.exists(STEAM_DICT_PATH):
    with open(STEAM_DICT_PATH, "r", encoding="utf-8") as f:
        steam_dict = json.load(f)
    logger.info("[steam] loaded mapper records=%s", len(steam_dict))
else:
    logger.error("[steam] mapper file not found: %s", STEAM_DICT_PATH)


def get_proxy() -> str | None:
    proxy = os.getenv("AETHERSWAP_STEAM_PROXY", "").strip()
    return proxy or None


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        try:
            return asyncio.get_event_loop().time()
        except RuntimeError:
            import time

            return time.monotonic()


async def _request_steam_histogram(
    session: AsyncSession,
    url: str,
    headers: dict[str, str],
    timeout: int = REQUEST_TIMEOUT,
    proxies: dict | None = None,
) -> object:
    kwargs = {
        "headers": headers,
        "cookies": {"steamCurrencyId": "23"},
        "timeout": timeout,
        "verify": False,
    }
    proxy = get_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    elif proxies:
        kwargs["proxies"] = proxies
    return await session.get(url, **kwargs)


def _steam_circuit_is_open() -> bool:
    return _circuit_open_until > _loop_time()


def is_steam_circuit_open() -> bool:
    return _steam_circuit_is_open()


def steam_circuit_remaining_seconds() -> int:
    return max(0, int(_circuit_open_until - _loop_time()))


def _record_steam_success() -> None:
    global _circuit_failures, _circuit_open_until
    _circuit_failures = 0
    _circuit_open_until = 0.0


def _record_steam_failure() -> None:
    global _circuit_failures, _circuit_open_until
    _circuit_failures += 1
    if _circuit_failures >= CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = _loop_time() + CIRCUIT_COOLDOWN_SECONDS
        logger.warning(
            "[steam] circuit breaker opened | failures=%s cooldown=%ss proxy_env=%s",
            _circuit_failures,
            CIRCUIT_COOLDOWN_SECONDS,
            proxy_tag(get_proxy()) if get_proxy() else "proxy_pool/direct",
        )


async def _fetch_single_steam_item(
    item: dict,
    session: AsyncSession,
    retry_count: int = MAX_RETRIES,
    request_timeout: int = REQUEST_TIMEOUT,
) -> dict | None:
    raise_if_stop_requested()
    item_id = item.get("item_id")
    hash_name = item.get("hash_name")
    if not item_id or not hash_name:
        return None

    if _steam_circuit_is_open():
        logger.warning("[steam] circuit open, fast-skip | item=%s", hash_name)
        return None

    item_data = steam_dict.get(hash_name, {})
    name_id = item_data.get("name_id") if isinstance(item_data, dict) else None
    if not name_id:
        logger.warning("[steam] missing name_id: %s", hash_name)
        return None

    encoded_name = quote(hash_name, safe="")
    url = (
        "https://steamcommunity.com/market/itemordershistogram"
        f"?country=CN&language=schinese&currency=23&item_nameid={name_id}&two_factor=0"
    )
    headers = {
        "Accept": "*/*",
        "Origin": "https://steamcommunity.com",
        "Referer": f"https://steamcommunity.com/market/listings/730/{encoded_name}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    }

    response = None
    total_attempts = retry_count + 1
    for attempt in range(1, total_attempts + 1):
        pool_proxies = None if get_proxy() else get_request_proxies(failed=attempt > 1, force=True, platform="steam")
        proxy_label = proxy_tag(get_proxy()) if get_proxy() else proxy_log_tag(pool_proxies)
        proxy_mode = "env_proxy" if get_proxy() else ("pool_proxy" if pool_proxies else "direct")
        try:
            logger.info("[steam] request %s mode=%s item=%s attempt=%s/%s", proxy_label, proxy_mode, hash_name, attempt, total_attempts)
            response = await _request_steam_histogram(session, url, headers, timeout=request_timeout, proxies=pool_proxies)
            raise_if_stop_requested()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = classify_request_failure(exc)
            mark_proxy_failure(
                pool_proxies or get_proxy(),
                reason=reason,
                cooldown_seconds=proxy_cooldown_for_reason(reason),
            )
            if attempt < total_attempts:
                backoff = 2 ** (attempt - 1)
                logger.info("[steam] transient request failure, retrying | item=%s attempt=%s sleep=%ss reason=%s err=%s", hash_name, attempt, backoff, reason, exc)
                await asyncio.sleep(backoff)
                continue
            logger.warning(
                "[steam] request failed | item=%s attempts=%s mode=%s proxy=%s reason=%s circuit_failures=%s err=%s",
                hash_name,
                total_attempts,
                proxy_mode,
                proxy_label,
                reason,
                _circuit_failures + 1,
                exc,
            )
            _record_steam_failure()
            return None

        if response.status_code == 429 and attempt < total_attempts:
            backoff = 2 ** (attempt - 1)
            logger.info("[steam] rate limited, retrying | item=%s attempt=%s sleep=%ss", hash_name, attempt, backoff)
            await asyncio.sleep(backoff)
            continue
        break

    if response is None:
        return None

    if response.status_code != 200:
        reason = classify_request_failure(status_code=response.status_code)
        logger.warning("[steam] http error | item=%s status=%s reason=%s mode=%s proxy=%s", hash_name, response.status_code, reason, proxy_mode, proxy_label)
        mark_proxy_failure(
            pool_proxies or get_proxy(),
            reason=f"steam_{reason}",
            cooldown_seconds=proxy_cooldown_for_reason(reason),
        )
        if response.status_code == 429:
            _record_steam_failure()
        return None

    try:
        data = response.json()
    except Exception as exc:
        logger.warning("[steam] json parse failed | item=%s err=%s", hash_name, exc)
        return None

    if data.get("success") != 1:
        logger.warning("[steam] business failed | item=%s", hash_name)
        return None

    sell_orders = data.get("sell_order_graph", [])
    buy_orders = data.get("buy_order_graph", [])
    sell_min = sell_orders[0][0] if sell_orders else 0
    buy_max = buy_orders[0][0] if buy_orders else 0
    sell_top5 = [order[0] for order in sell_orders[:5]]
    buy_top5 = [order[0] for order in buy_orders[:5]]

    result = {
        "item_id": item_id,
        "platform_name": PLATFORM_NAME,
        "sell_min": sell_min,
        "sell_top5_avg": round(sum(sell_top5) / len(sell_top5), 2) if sell_top5 else 0,
        "buy_max": buy_max,
        "buy_top5_avg": round(sum(buy_top5) / len(buy_top5), 2) if buy_top5 else 0,
        "volume": 0,
        "currency": "CNY",
    }
    _record_steam_success()
    mark_proxy_success(pool_proxies or get_proxy())
    logger.info("[steam] %s %s | sell_min=%s buy_max=%s", proxy_tag(get_proxy()) if get_proxy() else "(ProxyPool)", hash_name, sell_min, buy_max)
    return result


async def fetch_steam_prices(items: list[dict], session: AsyncSession, fast: bool = False) -> list[dict]:
    if not items:
        return []
    if _steam_circuit_is_open():
        logger.warning(
            "[steam] batch skipped by circuit | items=%s cooldown=%ss",
            len(items),
            steam_circuit_remaining_seconds(),
        )
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY)
    retry_count = JIT_MAX_RETRIES if fast else MAX_RETRIES
    request_timeout = JIT_REQUEST_TIMEOUT if fast else REQUEST_TIMEOUT

    async def guarded_fetch(item: dict) -> dict | None:
        async with semaphore:
            raise_if_stop_requested()
            if _steam_circuit_is_open():
                return None
            await asyncio.sleep(0.3)
            if _steam_circuit_is_open():
                return None
            return await _fetch_single_steam_item(
                item,
                session,
                retry_count=retry_count,
                request_timeout=request_timeout,
            )

    completed_results = await asyncio.gather(
        *(guarded_fetch(item) for item in items),
        return_exceptions=True,
    )
    results: list[dict] = []
    for res in completed_results:
        if isinstance(res, dict):
            results.append(res)
        elif isinstance(res, asyncio.CancelledError):
            raise res
        elif isinstance(res, Exception):
            logger.warning("[steam] item task failed: %s", res)
    return results
