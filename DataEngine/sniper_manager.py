from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from curl_cffi import requests as cffi_requests
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import ArbitrageOpportunity, ItemBase, MarketPrice, SessionLocal, engine, upsert_market_price_if_fresh

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# =========================
# 通用配置
# =========================
DEFAULT_TIMEOUT_SEC = 20
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SEC = 1.5
DEFAULT_CONCURRENCY = 5
DEFAULT_RANDOM_SLEEP_MIN = 0.3
DEFAULT_RANDOM_SLEEP_MAX = 1.2

BUFF_SEARCH_URL = "https://buff.163.com/api/market/goods?game=csgo&search={template_id}"
STEAM_SEARCH_URL = "https://steamcommunity.com/market/search/render/?appid=730&query={template_id}&count=10&norender=1"
UUYP_SEARCH_URL = "https://api-app.uuyp.cc/api/v1/market/search?template_id={template_id}"
ECO_SEARCH_URL = "https://www.eco.com/api/market/search?template_id={template_id}"


@dataclass(frozen=True)
class NormalizedPrice:
    sell_min: float
    buy_max: float
    sell_volume: int
    buy_volume: int


class BaseSniper(ABC):
    """统一的异步抓价接口基类。"""

    platform_name: str

    def __init__(self, session: cffi_requests.AsyncSession, semaphore: asyncio.Semaphore):
        self.session = session
        self.semaphore = semaphore

    @abstractmethod
    async def fetch_price(self, template_id: str) -> Optional[dict[str, Any]]:
        """返回标准化价格字典。"""

    async def _safe_get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
            try:
                async with self.semaphore:
                    await asyncio.sleep(random.uniform(DEFAULT_RANDOM_SLEEP_MIN, DEFAULT_RANDOM_SLEEP_MAX))
                    resp = await self.session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_SEC)

                if resp.status_code != 200:
                    preview = getattr(resp, "text", "")[:300]
                    raise RuntimeError(f"HTTP {resp.status_code}: {preview}")

                return resp.json()

            except Exception as exc:
                last_error = exc
                if attempt >= DEFAULT_MAX_RETRIES:
                    break
                await asyncio.sleep(DEFAULT_BACKOFF_SEC * attempt)

        logger.warning("%s 抓取失败 | url=%s | err=%s", self.platform_name, url, last_error)
        return None


class BuffSniper(BaseSniper):
    platform_name = "buff"

    async def fetch_price(self, template_id: str) -> Optional[dict[str, Any]]:
        url = BUFF_SEARCH_URL.format(template_id=template_id)
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": "https://buff.163.com/",
        }
        payload = await self._safe_get_json(url, headers=headers)
        if not isinstance(payload, dict):
            return None

        # 兼容不同版本字段：尽量从常见结构中提取
        data = payload.get("data") or payload
        goods = data.get("goods") if isinstance(data, dict) else None
        if isinstance(goods, list) and goods:
            first = goods[0] or {}
            return self._normalize_from_market_item(first)

        return self._normalize_from_market_item(data)

    def _normalize_from_market_item(self, raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        sell_min = self._to_float(
            raw.get("sell_min")
            or raw.get("min_price")
            or raw.get("lowest_price")
            or raw.get("starting_at", {}).get("price")
        )
        buy_max = self._to_float(
            raw.get("buy_max")
            or raw.get("highest_order", {}).get("price")
            or raw.get("buy_order_max_price")
        )
        sell_volume = self._to_int(raw.get("sell_volume") or raw.get("sell_count") or raw.get("sales"))
        buy_volume = self._to_int(raw.get("buy_volume") or raw.get("buy_count") or raw.get("orders"))
        return self._pack(sell_min, buy_max, sell_volume, buy_volume)

    @staticmethod
    def _pack(sell_min: float, buy_max: float, sell_volume: int, buy_volume: int) -> dict[str, Any] | None:
        if sell_min <= 0 and buy_max <= 0:
            return None
        return {
            "sell_min": sell_min,
            "buy_max": buy_max,
            "sell_volume": sell_volume,
            "buy_volume": buy_volume,
        }

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0


class SteamSniper(BaseSniper):
    platform_name = "steam"

    async def fetch_price(self, template_id: str) -> Optional[dict[str, Any]]:
        url = STEAM_SEARCH_URL.format(template_id=template_id)
        headers = {
            "accept": "application/json,text/javascript,*/*;q=0.01",
            "referer": "https://steamcommunity.com/market/",
            "x-requested-with": "XMLHttpRequest",
        }
        payload = await self._safe_get_json(url, headers=headers)
        if not isinstance(payload, dict):
            return None

        # Steam 市场搜索返回通常在 results_html / results 中，需要按实际页面做二次解析
        return self._normalize_from_payload(payload)

    def _normalize_from_payload(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        sell_min = self._to_float(payload.get("sell_min") or payload.get("lowest_price"))
        buy_max = self._to_float(payload.get("buy_max") or payload.get("highest_buy_order"))
        sell_volume = self._to_int(payload.get("sell_volume") or payload.get("sell_count"))
        buy_volume = self._to_int(payload.get("buy_volume") or payload.get("buy_count"))
        if sell_min <= 0 and buy_max <= 0:
            return None
        return {
            "sell_min": sell_min,
            "buy_max": buy_max,
            "sell_volume": sell_volume,
            "buy_volume": buy_volume,
        }

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0


class UuypSniper(BaseSniper):
    platform_name = "uuyp"

    async def fetch_price(self, template_id: str) -> Optional[dict[str, Any]]:
        url = UUYP_SEARCH_URL.format(template_id=template_id)
        # 带 Android 请求头，贴近 App API 行为
        headers = {
            "accept": "application/json",
            "content-type": "application/json;charset=UTF-8",
            "user-agent": "okhttp/4.10.0 Android",
            "x-requested-with": "com.uuyp.app",
            "platform": "android",
            "app-version": "6.0.0",
            "device-model": "Pixel 7",
            "device-brand": "Google",
        }
        payload = await self._safe_get_json(url, headers=headers)
        if not isinstance(payload, dict):
            return None
        return self._normalize(payload)

    def _normalize(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        data = payload.get("data") or payload
        if isinstance(data, dict):
            sell_min = self._to_float(data.get("sell_min") or data.get("min_sell_price") or data.get("price"))
            buy_max = self._to_float(data.get("buy_max") or data.get("max_buy_price"))
            sell_volume = self._to_int(data.get("sell_volume") or data.get("sell_num"))
            buy_volume = self._to_int(data.get("buy_volume") or data.get("buy_num"))
            if sell_min <= 0 and buy_max <= 0:
                return None
            return {
                "sell_min": sell_min,
                "buy_max": buy_max,
                "sell_volume": sell_volume,
                "buy_volume": buy_volume,
            }
        return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0


class EcoSniper(BaseSniper):
    platform_name = "eco"

    async def fetch_price(self, template_id: str) -> Optional[dict[str, Any]]:
        url = ECO_SEARCH_URL.format(template_id=template_id)
        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": "https://www.eco.com/",
            "user-agent": "Mozilla/5.0",
        }
        payload = await self._safe_get_json(url, headers=headers)
        if not isinstance(payload, dict):
            return None
        return self._normalize(payload)

    def _normalize(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        data = payload.get("data") or payload
        if isinstance(data, dict):
            sell_min = self._to_float(data.get("sell_min") or data.get("lowest_price") or data.get("price"))
            buy_max = self._to_float(data.get("buy_max") or data.get("highest_buy_order"))
            sell_volume = self._to_int(data.get("sell_volume") or data.get("listing_count"))
            buy_volume = self._to_int(data.get("buy_volume") or data.get("buy_order_count"))
            if sell_min <= 0 and buy_max <= 0:
                return None
            return {
                "sell_min": sell_min,
                "buy_max": buy_max,
                "sell_volume": sell_volume,
                "buy_volume": buy_volume,
            }
        return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0


class SniperManager:
    """统一的 Sniper 调度器：抓价、落盘、并发控制全部收口。"""

    def __init__(
        self,
        concurrency: int = DEFAULT_CONCURRENCY,
        random_sleep_range: tuple[float, float] = (DEFAULT_RANDOM_SLEEP_MIN, DEFAULT_RANDOM_SLEEP_MAX),
    ):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.random_sleep_range = random_sleep_range
        self._session: cffi_requests.AsyncSession | None = None
        self._snipers: dict[str, BaseSniper] = {}

    async def __aenter__(self) -> "SniperManager":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_session(self) -> cffi_requests.AsyncSession:
        if self._session is None:
            self._session = cffi_requests.AsyncSession(impersonate="chrome110")
        return self._session

    def _get_sniper(self, platform: str) -> BaseSniper:
        platform = platform.lower().strip()
        if platform in self._snipers:
            return self._snipers[platform]

        session = self._session
        if session is None:
            raise RuntimeError("SniperManager 尚未初始化，请先 await manager.__aenter__() 或使用 async with")

        mapping: dict[str, type[BaseSniper]] = {
            "buff": BuffSniper,
            "steam": SteamSniper,
            "uuyp": UuypSniper,
            "eco": EcoSniper,
            "igxe": EcoSniper,
        }
        if platform not in mapping:
            raise ValueError(f"不支持的平台: {platform}")

        sniper = mapping[platform](session=session, semaphore=self.semaphore)
        self._snipers[platform] = sniper
        return sniper

    async def get_price(self, platform: str, template_id: str) -> Optional[dict[str, Any]]:
        await self._ensure_session()
        sniper = self._get_sniper(platform)
        result = await sniper.fetch_price(template_id)
        if result:
            await self._upsert_market_price(platform, template_id, result)
        return result

    async def _upsert_market_price(self, platform: str, template_id: str, result: dict[str, Any]) -> None:
        """抓取成功后自动落盘到 `MarketPrice`。"""
        platform = platform.lower().strip()
        db = SessionLocal()
        try:
            item = db.query(ItemBase).filter(ItemBase.market_hash_name == template_id).one_or_none()
            if item is None:
                logger.info("未找到饰品映射，跳过落盘 | template_id=%s platform=%s", template_id, platform)
                return

            data_timestamp = result.get("new_timestamp") or result.get("data_timestamp") or result.get("updated_at") or datetime.now()
            changed = upsert_market_price_if_fresh(
                db,
                item_id=item.id,
                platform_name=platform,
                data_source="sniper",
                sell_min=float(result.get("sell_min") or 0),
                buy_max=float(result.get("buy_max") or 0),
                sell_volume=int(result.get("sell_volume") or 0),
                buy_volume=int(result.get("buy_volume") or 0),
                new_timestamp=data_timestamp,
                log=logger,
            )
            db.commit()
            if changed:
                logger.info("MarketPrice saved | item_id=%s platform=%s", item.id, platform)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def upsert_opportunity(
        self,
        item_id: int,
        buy_platform: str,
        buy_price: float,
        sell_platform: str,
        sell_price: float,
        profit_cny: float,
        profit_rate: float,
        status: str = "open",
    ) -> None:
        """可选：把机会同步到套利雷达表，方便前端直接读。"""
        db = SessionLocal()
        try:
            stmt = sqlite_insert(ArbitrageOpportunity).values(
                item_id=item_id,
                buy_platform=buy_platform,
                buy_price=buy_price,
                sell_platform=sell_platform,
                sell_price=sell_price,
                profit_cny=profit_cny,
                profit_rate=profit_rate,
                status=status,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[ArbitrageOpportunity.item_id, ArbitrageOpportunity.buy_platform, ArbitrageOpportunity.sell_platform],
                set_={
                    "buy_price": stmt.excluded.buy_price,
                    "sell_price": stmt.excluded.sell_price,
                    "profit_cny": stmt.excluded.profit_cny,
                    "profit_rate": stmt.excluded.profit_rate,
                    "status": stmt.excluded.status,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            db.execute(stmt)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


async def get_price(platform: str, template_id: str) -> Optional[dict[str, Any]]:
    """便捷函数：外部直接调用。"""
    async with SniperManager() as manager:
        return await manager.get_price(platform, template_id)


if __name__ == "__main__":
    async def _demo() -> None:
        async with SniperManager() as manager:
            print(await manager.get_price("buff", "AK-47 | Redline (Field-Tested)"))

    asyncio.run(_demo())
