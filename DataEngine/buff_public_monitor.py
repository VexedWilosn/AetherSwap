import asyncio
import logging
from typing import Any, Dict, List, Optional

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
from DataEngine.stop_signal import StopRequested, raise_if_stop_requested
from DataEngine.logging_setup import setup_dataengine_logging

setup_dataengine_logging()
logger = logging.getLogger(__name__)

PROXY_URL = ""
TIMEOUT = 10
JIT_TIMEOUT = 6
PLATFORM_NAME = "buff"
CONCURRENCY = 2
JIT_CONCURRENCY = 1
MAX_RETRIES = 1
JIT_MAX_RETRIES = 0
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 300
BUFF_BLOCK_PROXY_COOLDOWN_SECONDS = 600
_circuit_failures = 0
_circuit_open_until = 0.0
BUFF_CNY_COOKIES = {
    "locale": "zh-Hans",
    "Locale-Supported": "zh-Hans",
    "currency": "CNY",
}
BUFF_CNY_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        try:
            return asyncio.get_event_loop().time()
        except RuntimeError:
            import time

            return time.monotonic()


def _buff_headers(goods_id: int, order_type: str) -> dict[str, str]:
    headers = dict(BUFF_CNY_HEADERS)
    headers["Referer"] = f"https://buff.163.com/goods/{goods_id}?from=market"
    return headers


def _buff_circuit_is_open() -> bool:
    return _circuit_open_until > _loop_time()


def is_buff_circuit_open() -> bool:
    return _buff_circuit_is_open()


def buff_circuit_remaining_seconds() -> int:
    return max(0, int(_circuit_open_until - _loop_time()))


def _record_buff_success() -> None:
    global _circuit_failures, _circuit_open_until
    _circuit_failures = 0
    _circuit_open_until = 0.0


def _record_buff_failure() -> None:
    global _circuit_failures, _circuit_open_until
    _circuit_failures += 1
    if _circuit_failures >= CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = _loop_time() + CIRCUIT_COOLDOWN_SECONDS
        logger.warning(
            "[buff] circuit breaker opened | failures=%s cooldown=%ss",
            _circuit_failures,
            CIRCUIT_COOLDOWN_SECONDS,
        )


async def fetch_order_book(
    session: AsyncSession,
    goods_id: int,
    order_type: str,
    timeout: int = TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> Optional[Dict[str, Any]]:
    raise_if_stop_requested()
    if _buff_circuit_is_open():
        logger.warning("[buff] circuit open, fast-skip | goods_id=%s type=%s", goods_id, order_type)
        return None

    url = f"https://buff.163.com/api/market/goods/{order_type}?game=csgo&goods_id={goods_id}&page_num=1"
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else get_request_proxies(failed=attempt > 1, force=True, platform="buff")
        proxy_label = proxy_tag(PROXY_URL) if PROXY_URL else proxy_log_tag(proxies)
        logger.info("[buff] request %s goods_id=%s type=%s attempt=%s/%s", proxy_label, goods_id, order_type, attempt, total_attempts)
        try:
            response = await session.get(
                url,
                headers=_buff_headers(goods_id, order_type),
                cookies=BUFF_CNY_COOKIES,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome120",
                default_headers=True,
            )
            raise_if_stop_requested()
            if response.status_code in {403, 429}:
                reason = classify_request_failure(status_code=response.status_code)
                _record_buff_failure()
                if proxies:
                    mark_proxy_failure(
                        proxies,
                        reason=f"buff_{reason}",
                        cooldown_seconds=proxy_cooldown_for_reason(reason, default=BUFF_BLOCK_PROXY_COOLDOWN_SECONDS),
                    )
                if attempt < total_attempts and response.status_code == 429:
                    sleep_for = 2 ** (attempt - 1)
                    logger.info("[%s] %s status=%s, retrying sleep=%ss", goods_id, order_type, response.status_code, sleep_for)
                    await asyncio.sleep(sleep_for)
                    continue
                logger.warning("[%s] %s blocked by BUFF status=%s", goods_id, order_type, response.status_code)
                return None
            if response.status_code != 200:
                reason = classify_request_failure(status_code=response.status_code)
                logger.warning("[%s] %s request failed status=%s reason=%s", goods_id, order_type, response.status_code, reason)
                return None

            data = response.json()
            if data.get("code") != "OK":
                msg = data.get("msg")
                if "??????" in str(msg) or "rate" in str(msg).lower():
                    _record_buff_failure()
                logger.warning("[%s] %s API business error: %s", goods_id, order_type, msg)
                return None

            _record_buff_success()
            mark_proxy_success(proxies)
            return data.get("data")
        except asyncio.CancelledError:
            raise
        except StopRequested:
            logger.info("[%s] %s fetch stopped by request", goods_id, order_type)
            raise
        except Exception as e:
            reason = classify_request_failure(e)
            if reason != "stop_requested":
                mark_proxy_failure(proxies, reason=reason, cooldown_seconds=proxy_cooldown_for_reason(reason))
            if attempt < total_attempts:
                sleep_for = 2 ** (attempt - 1)
                logger.info("[%s] %s request jitter, retrying sleep=%ss reason=%s err=%s", goods_id, order_type, sleep_for, reason, e)
                await asyncio.sleep(sleep_for)
                continue
            logger.error("[%s] %s network/parse error reason=%s: %s", goods_id, order_type, reason, e)
            return None


def calculate_stable_price(items: List[Dict[str, Any]]) -> tuple[float, float]:
    if not items:
        return 0.0, 0.0
    prices = [float(item.get('price', 0)) for item in items[:5] if item.get('price') is not None]
    if not prices:
        return 0.0, 0.0
    return prices[0], round(sum(prices) / len(prices), 2)


async def _fetch_single_buff_item(item: Dict[str, Any], session: AsyncSession, fast: bool = False) -> Optional[Dict[str, Any]]:
    raise_if_stop_requested()
    if _buff_circuit_is_open():
        return None
    goods_id = item.get("platform_id")
    item_id = item.get("item_id")
    if not goods_id or item_id is None:
        return None

    timeout = JIT_TIMEOUT if fast else TIMEOUT
    max_retries = JIT_MAX_RETRIES if fast else MAX_RETRIES
    sell_data, buy_data = await asyncio.gather(
        fetch_order_book(session, int(goods_id), "sell_order", timeout=timeout, max_retries=max_retries),
        fetch_order_book(session, int(goods_id), "buy_order", timeout=timeout, max_retries=max_retries),
    )
    if not sell_data or not buy_data:
        return None

    goods_infos = sell_data.get("goods_infos", {})
    info = goods_infos.get(str(goods_id), {})
    market_hash_name = info.get("market_hash_name", "Unknown")

    sell_min, sell_top5_avg = calculate_stable_price(sell_data.get("items", []))
    buy_max, buy_top5_avg = calculate_stable_price(buy_data.get("items", []))
    volume = int(sell_data.get("total_count", 0) or 0)

    result = {
        "item_id": item_id,
        "platform_name": PLATFORM_NAME,
        "sell_min": sell_min,
        "sell_top5_avg": sell_top5_avg,
        "buy_max": buy_max,
        "buy_top5_avg": buy_top5_avg,
        "volume": volume,
        "currency": "CNY",
    }
    logger.info("[buff] %s %s(%s) | %s", proxy_tag(PROXY_URL) if PROXY_URL else "(ProxyPool)", market_hash_name, goods_id, result)
    return result


async def fetch_buff_prices(items: list[dict], session: AsyncSession, fast: bool = False) -> list[dict]:
    if not items:
        return []
    if _buff_circuit_is_open():
        logger.warning(
            "[buff] batch skipped by circuit | items=%s cooldown=%ss",
            len(items),
            buff_circuit_remaining_seconds(),
        )
        return []

    semaphore = asyncio.Semaphore(JIT_CONCURRENCY if fast else CONCURRENCY)
    item_timeout = JIT_TIMEOUT * 2 if fast else TIMEOUT * 2

    async def guarded_fetch(item: dict) -> Optional[Dict[str, Any]]:
        async with semaphore:
            raise_if_stop_requested()
            if _buff_circuit_is_open():
                return None
            await asyncio.sleep(0.2 if fast else 0.35)
            if _buff_circuit_is_open():
                return None
            goods_id = item.get("platform_id")
            try:
                return await asyncio.wait_for(
                    _fetch_single_buff_item(item, session, fast=fast),
                    timeout=item_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("[buff] item fetch timeout skipped | goods_id=%s timeout=%ss", goods_id, item_timeout)
                return None
            except StopRequested:
                raise
            except Exception as exc:
                logger.warning("[buff] item fetch failed skipped | goods_id=%s err=%s", goods_id, exc)
                return None

    tasks = [guarded_fetch(item) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: list[dict] = []
    for result in results:
        if isinstance(result, dict):
            rows.append(result)
        elif isinstance(result, asyncio.CancelledError):
            raise result
        elif isinstance(result, StopRequested):
            raise result
    return rows
