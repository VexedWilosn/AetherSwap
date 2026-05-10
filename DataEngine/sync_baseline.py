from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import (
    Base,
    ItemBase,
    MarketPrice,
    PlatformMapping,
    SessionLocal,
    engine,
    is_fresh_enough,
    normalize_data_timestamp,
)
from DataEngine.eco_public_monitor import (
    _extract_buy_max,
    _extract_sell_min,
    _extract_volume,
    _normalize_result_rows,
    load_eco_openapi_config,
    normalize_eco_response_payload,
)
from eco.openapi_client import EcoOpenAPIClient
from DataEngine.stop_signal import raise_if_stop_requested

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"
DB_PATH = BASE_DIR / "config" / "market_data.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CACHE_PATH = BASE_DIR / "config" / "http_cache" / "csgotrader_baseline.json"
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
BUFF_MAPPER_PATH = BASE_DIR / "DataEngine" / "SteamTradingSite-ID-Mapper-main" / "buff" / "730.json"
UUYP_MAPPER_PATH = BASE_DIR / "DataEngine" / "SteamTradingSite-ID-Mapper-main" / "uuyp" / "730.json"


BUFF_URL = "https://prices.csgotrader.app/latest/buff163.json"
UUYP_URL = "https://prices.csgotrader.app/latest/youpin.json"
ECO_BASELINE_URL = "https://openapi.ecosteam.cn/Api/Market/GetHashNameAndPriceList"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SEC = 2.0

_root_logger = logging.getLogger()
if not _root_logger.handlers:
    _root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    _root_logger.addHandler(console_handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    _root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BaselinePriceRow:
    item_id: int
    platform_name: str
    sell_min: float = 0.0
    sell_top5_avg: float = 0.0
    buy_max: float = 0.0
    buy_top5_avg: float = 0.0
    volume: int = 0
    data_timestamp: datetime | None = None


@dataclass(frozen=True)
class ConditionalJsonResult:
    key: str
    status_code: int
    payload: Any | None = None
    data_timestamp: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    cache_metadata: dict[str, str] | None = None


def _timer_start() -> float:
    return time.perf_counter()


def _timer_cost(start: float) -> float:
    return round(time.perf_counter() - start, 2)


def ensure_db_ready() -> None:
    Base.metadata.create_all(engine)
    logger.info("数据库检查完成 | path=%s", DB_PATH)


def _load_http_cache() -> dict[str, dict[str, str]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning("读取 HTTP 协商缓存失败，将重新拉取 | path=%s err=%s", CACHE_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_http_cache(cache: dict[str, dict[str, str]]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_last_modified(value: str | None) -> datetime:
    if not value:
        logger.warning("CSGOTrader 响应缺少 Last-Modified，回退到当前时间")
        return datetime.now()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        logger.warning("CSGOTrader Last-Modified 解析失败，回退到当前时间 | value=%s err=%s", value, exc)
        return datetime.now()
    return normalize_data_timestamp(parsed)


def _conditional_headers(key: str, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    metadata = cache.get(key) or {}
    headers: dict[str, str] = {}
    if metadata.get("etag"):
        headers["If-None-Match"] = metadata["etag"]
    if metadata.get("last_modified"):
        headers["If-Modified-Since"] = metadata["last_modified"]
    return headers


async def _fetch_json(session: cffi_requests.AsyncSession, url: str) -> Any:
    # 融入你提供的 cURL 原生 Header，提升绕过 Cloudflare 的成功率
    # 注意：故意不加 User-Agent 和 sec-ch-ua，让 impersonate="chrome110" 自动在底层完美生成，防止指纹冲突
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'zh-CN,zh;q=0.9',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
    }

    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        raise_if_stop_requested()
        try:
            response = await session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_SEC)
            raise_if_stop_requested()
            if response.status_code != 200:
                # 👇 修复点1：curl_cffi 中，text 是属性而不是异步方法，把 await 删掉
                preview = response.text[:500]
                raise RuntimeError(f"GET {url} failed with status {response.status_code}: {preview}")
            
            # 👇 修复点2：curl_cffi 中，json() 是同步方法直接返回 dict，绝对不能加 await！
            return response.json()
            
        except Exception as exc:
            last_error = exc
            if attempt >= DEFAULT_MAX_RETRIES:
                raise
            sleep_for = DEFAULT_BACKOFF_SEC * attempt
            logger.warning("下载失败，重试中 | url=%s attempt=%s sleep=%.1fs err=%s", url, attempt, sleep_for, exc)
            await asyncio.sleep(sleep_for)
            
    raise last_error or RuntimeError(f"Failed to fetch {url}")


async def _fetch_json_conditional(
    session: cffi_requests.AsyncSession,
    key: str,
    url: str,
    cache: dict[str, dict[str, str]],
) -> ConditionalJsonResult:
    headers = {
        "accept": "application/json,text/plain,*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }
    headers.update(_conditional_headers(key, cache))

    last_error: Exception | None = None
    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        raise_if_stop_requested()
        try:
            response = await session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_SEC)
            raise_if_stop_requested()
            status_code = int(response.status_code)
            response_headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
            etag = response_headers.get("etag")
            last_modified = response_headers.get("last-modified")

            if status_code == 304:
                cached_ts = cache.get(key, {}).get("data_timestamp")
                logger.info(
                    "CSGOTrader 未更新，跳过入库 | key=%s last_modified=%s",
                    key,
                    last_modified or cache.get(key, {}).get("last_modified"),
                )
                return ConditionalJsonResult(
                    key=key,
                    status_code=304,
                    data_timestamp=normalize_data_timestamp(cached_ts) if cached_ts else None,
                    etag=etag or cache.get(key, {}).get("etag"),
                    last_modified=last_modified or cache.get(key, {}).get("last_modified"),
                )

            if status_code != 200:
                preview = response.text[:500]
                raise RuntimeError(f"GET {url} failed with status {status_code}: {preview}")

            data_timestamp = _parse_last_modified(last_modified)
            payload = response.json()
            cache_metadata = {
                "etag": etag or "",
                "last_modified": last_modified or "",
                "data_timestamp": data_timestamp.isoformat(timespec="seconds"),
            }
            logger.info(
                "CSGOTrader 已更新 | key=%s status=200 last_modified=%s data_timestamp=%s",
                key,
                last_modified,
                data_timestamp,
            )
            return ConditionalJsonResult(
                key=key,
                status_code=200,
                payload=payload,
                data_timestamp=data_timestamp,
                etag=etag,
                last_modified=last_modified,
                cache_metadata=cache_metadata,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= DEFAULT_MAX_RETRIES:
                raise
            sleep_for = DEFAULT_BACKOFF_SEC * attempt
            logger.warning("协商缓存下载失败，重试中 | key=%s attempt=%s sleep=%.1fs err=%s", key, attempt, sleep_for, exc)
            await asyncio.sleep(sleep_for)

    raise last_error or RuntimeError(f"Failed to fetch {url}")


async def _fetch_eco_payload(session: cffi_requests.AsyncSession) -> dict[str, Any]:
    raise_if_stop_requested()
    cfg = load_eco_openapi_config()
    if cfg is None:
        logger.error("ECO OpenAPI ??????? ECO ????")
        return {}

    try:
        client = EcoOpenAPIClient(cfg, timeout=DEFAULT_TIMEOUT_SEC)
        data = await client.async_post(
            session,
            "/Api/Market/GetHashNameAndPriceList",
            {"GameID": "730"},
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        raise_if_stop_requested()
    except Exception as exc:
        logger.error("ECO ????????: %s", exc)
        return {}

    if not isinstance(data, dict):
        logger.error("ECO API ????????: %s", type(data))
        return {}

    data = normalize_eco_response_payload(data)
    result_code = str(data.get("ResultCode", data.get("StatusCode", data.get("code", data.get("Code", ""))))).strip()
    result_msg = data.get("ResultMsg") or data.get("StatusMsg") or data.get("msg") or ""
    if result_code not in {"0", "200", "OK", "ok"}:
        logger.warning("ECO ???? | ResultCode=%s | ResultMsg=%s", result_code, result_msg)
        return data

    return data

def _load_active_item_map() -> dict[str, int]:
    db = SessionLocal()
    start = _timer_start()
    try:
        rows = db.query(ItemBase.market_hash_name, ItemBase.id).filter(ItemBase.is_active.is_(True)).all()
        result = {name: item_id for name, item_id in rows if name}
        logger.info("加载活跃饰品映射完成 | count=%s | 耗时 %.2fs", len(result), _timer_cost(start))
        return result
    finally:
        db.close()


def _load_uuyp_template_map() -> dict[str, int]:
    if not UUYP_MAPPER_PATH.exists():
        logger.warning("UUYP mapper file not found | path=%s", UUYP_MAPPER_PATH)
        return {}
    try:
        raw = json.loads(UUYP_MAPPER_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("UUYP mapper 读取失败 | path=%s err=%s", UUYP_MAPPER_PATH, exc)
        return {}
    result: dict[str, int] = {}
    if isinstance(raw, dict):
        for market_hash_name, template_id in raw.items():
            try:
                result[str(market_hash_name)] = int(template_id)
            except (TypeError, ValueError):
                continue
    logger.info("UUYP mapper 加载完成 | count=%s", len(result))
    return result


def _sync_uuyp_template_ids(db, template_map: dict[str, int]) -> int:
    if not template_map:
        return 0

    rows = (
        db.query(ItemBase)
        .filter(ItemBase.market_hash_name.in_(list(template_map.keys())))
        .all()
    )
    changed = 0
    for item in rows:
        template_id = template_map.get(item.market_hash_name)
        if not template_id:
            continue
        if getattr(item, "uuyp_template_id", None) != template_id:
            item.uuyp_template_id = template_id
            changed += 1

        mapping = (
            db.query(PlatformMapping)
            .filter(PlatformMapping.item_id == item.id, PlatformMapping.platform_name == "uuyp")
            .one_or_none()
        )
        if mapping is None:
            db.add(PlatformMapping(item_id=item.id, platform_name="uuyp", platform_item_id=str(template_id)))
            changed += 1
        elif mapping.platform_item_id != str(template_id):
            mapping.platform_item_id = str(template_id)
            changed += 1

    logger.info("UUYP templateId 回填完成 | matched=%s changed=%s", len(rows), changed)
    return changed


def _load_buff_goods_map() -> dict[str, int]:
    if not BUFF_MAPPER_PATH.exists():
        logger.warning("Buff mapper file not found | path=%s", BUFF_MAPPER_PATH)
        return {}
    try:
        raw = json.loads(BUFF_MAPPER_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Buff mapper load failed | path=%s err=%s", BUFF_MAPPER_PATH, exc)
        return {}
    result: dict[str, int] = {}
    if isinstance(raw, dict):
        for market_hash_name, goods_id in raw.items():
            try:
                result[str(market_hash_name)] = int(goods_id)
            except (TypeError, ValueError):
                continue
    logger.info("Buff mapper loaded | count=%s", len(result))
    return result


def _sync_buff_goods_ids(db, goods_map: dict[str, int]) -> int:
    if not goods_map:
        return 0

    rows = (
        db.query(ItemBase)
        .filter(ItemBase.market_hash_name.in_(list(goods_map.keys())))
        .all()
    )
    changed = 0
    for item in rows:
        goods_id = goods_map.get(item.market_hash_name)
        if not goods_id:
            continue
        if getattr(item, "buff_goods_id", None) != goods_id:
            item.buff_goods_id = goods_id
            changed += 1

        mapping = (
            db.query(PlatformMapping)
            .filter(PlatformMapping.item_id == item.id, PlatformMapping.platform_name == "buff")
            .one_or_none()
        )
        if mapping is None:
            db.add(PlatformMapping(item_id=item.id, platform_name="buff", platform_item_id=str(goods_id)))
            changed += 1
        elif mapping.platform_item_id != str(goods_id):
            mapping.platform_item_id = str(goods_id)
            changed += 1

    logger.info("Buff goodsId backfill done | matched=%s changed=%s", len(rows), changed)
    return changed


def _parse_buff_payload(payload: dict[str, Any], item_map: dict[str, int], data_timestamp: datetime) -> list[BaselinePriceRow]:
    rows: list[BaselinePriceRow] = []
    matched = 0
    for market_hash_name, record in payload.items():
        item_id = item_map.get(market_hash_name)
        if not item_id or not isinstance(record, dict):
            continue
        starting_at = record.get("starting_at") or {}
        highest_order = record.get("highest_order") or {}
        sell_min = float(starting_at.get("price") or 0) if isinstance(starting_at, dict) else 0.0
        buy_max = float(highest_order.get("price") or 0) if isinstance(highest_order, dict) else 0.0
        rows.append(
            BaselinePriceRow(
                item_id=item_id,
                platform_name="buff",
                sell_min=sell_min,
                buy_max=buy_max,
                data_timestamp=data_timestamp,
            )
        )
        matched += 1
    logger.info("buff 匹配完成 | matched=%s", matched)
    return rows


def _parse_uuyp_payload(payload: dict[str, Any], item_map: dict[str, int], data_timestamp: datetime) -> list[BaselinePriceRow]:
    rows: list[BaselinePriceRow] = []
    matched = 0
    for market_hash_name, value in payload.items():
        item_id = item_map.get(market_hash_name)
        if not item_id:
            continue
        if value is None:
            sell_min = 0.0
        else:
            try:
                sell_min = float(value)
            except (TypeError, ValueError):
                sell_min = 0.0
        rows.append(
            BaselinePriceRow(
                item_id=item_id,
                platform_name="uuyp",
                sell_min=sell_min,
                data_timestamp=data_timestamp,
            )
        )
        matched += 1
    logger.info("uuyp 匹配完成 | matched=%s", matched)
    return rows


def _parse_eco_payload(payload: dict[str, Any], item_map: dict[str, int], data_timestamp: datetime | None = None) -> list[BaselinePriceRow]:
    rows: list[BaselinePriceRow] = []
    matched = 0
    payload = normalize_eco_response_payload(payload)
    data_timestamp = normalize_data_timestamp(data_timestamp)
    result_data = _normalize_result_rows(payload.get("ResultData") or payload.get("resultData") or payload.get("data"))

    if not result_data:
        logger.warning("ECO 返回的 ResultData 格式异常")
        return rows

    for item in result_data:
        if not isinstance(item, dict):
            continue

        hash_name = item.get("HashName") or item.get("hashName") or item.get("MarketHashName") or item.get("marketHashName")
        if not hash_name:
            continue

        item_id = item_map.get(hash_name)
        if not item_id:
            continue

        sell_min = _extract_sell_min(item)
        buy_max = _extract_buy_max(item)
        volume = _extract_volume(item)

        rows.append(
            BaselinePriceRow(
                item_id=item_id,
                platform_name="eco",  # 统一存入 eco 平台盘口
                sell_min=sell_min,
                buy_max=buy_max,
                volume=volume,
                data_timestamp=data_timestamp,
            )
        )
        matched += 1

    logger.info("eco 匹配完成 | matched=%s", matched)
    return rows


def _upsert_market_prices(db, rows: list[BaselinePriceRow], chunk_size: int = 2000) -> int:
    """
    使用纯 SQLAlchemy 内存比对 + 批量写入机制。
    完美绕过 SQLite 对 ON CONFLICT 的严格约束限制，且性能极高。
    """
    if not rows:
        return 0

    total_processed = 0
    stale_skipped = 0

    # 将海量数据切分为每块 2000 条的小批次，防止撑爆内存
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]

        # 1. 提取当前批次的所有 item_id 和 platform_name
        chunk_item_ids = list({r.item_id for r in chunk})
        chunk_platforms = list({r.platform_name for r in chunk})

        # 2. 从数据库中极速查出这些记录的现有 ID
        existing_records = (
            db.query(MarketPrice.id, MarketPrice.item_id, MarketPrice.platform_name, MarketPrice.updated_at)
            .filter(
                MarketPrice.item_id.in_(chunk_item_ids),
                MarketPrice.platform_name.in_(chunk_platforms)
            )
            .all()
        )

        # 构建快速查找字典 {(item_id, platform_name): record_id}
        existing_map = {
            (rec.item_id, rec.platform_name): {"id": rec.id, "updated_at": rec.updated_at}
            for rec in existing_records
        }

        updates = []
        inserts = []

        # 3. 分流：存在则放入更新队列，不存在则放入插入队列
        for r in chunk:
            key = (r.item_id, r.platform_name)
            data_timestamp = normalize_data_timestamp(r.data_timestamp)
            if key in existing_map:
                existing = existing_map[key]
                if not is_fresh_enough(existing["updated_at"], data_timestamp):
                    stale_skipped += 1
                    logger.debug(
                        "旧数据拦截 | item_id=%s platform=%s source=baseline incoming=%s current=%s",
                        r.item_id,
                        r.platform_name,
                        data_timestamp,
                        existing["updated_at"],
                    )
                    continue
                updates.append({
                    "id": existing["id"],
                    "data_source": "baseline",  # 👈 新增这一行
                    "sell_min": r.sell_min,
                    "sell_top5_avg": r.sell_top5_avg,
                    "buy_max": r.buy_max,
                    "buy_top5_avg": r.buy_top5_avg,
                    "volume": r.volume,
                    "updated_at": data_timestamp,
                })
            else:
                inserts.append({
                    "item_id": r.item_id,
                    "platform_name": r.platform_name,
                    "data_source": "baseline",  # 👈 新增这一行
                    "sell_min": r.sell_min,
                    "sell_top5_avg": r.sell_top5_avg,
                    "buy_max": r.buy_max,
                    "buy_top5_avg": r.buy_top5_avg,
                    "volume": r.volume,
                    "updated_at": data_timestamp,
                })
        # 4. 执行极速批量操作
        if updates:
            db.bulk_update_mappings(MarketPrice, updates)
        if inserts:
            db.bulk_insert_mappings(MarketPrice, inserts)

        total_processed += len(updates) + len(inserts)

    if stale_skipped:
        logger.info("基线旧数据拦截完成 | skipped=%s", stale_skipped)
    return total_processed


async def sync_baseline() -> None:
    raise_if_stop_requested()
    ensure_db_ready()
    item_map = _load_active_item_map()
    buff_goods_map = _load_buff_goods_map()
    uuyp_template_map = _load_uuyp_template_map()
    if not item_map:
        logger.info("没有可同步的活跃饰品，任务结束。")
        return

    download_start = _timer_start()
    cache = _load_http_cache()
    async with cffi_requests.AsyncSession(impersonate="chrome110") as session:
        buff_task = _fetch_json_conditional(session, "buff", BUFF_URL, cache)
        uuyp_task = _fetch_json_conditional(session, "uuyp", UUYP_URL, cache)
        eco_task = _fetch_eco_payload(session)
        buff_result, uuyp_result, eco_payload = await asyncio.gather(buff_task, uuyp_task, eco_task)
        raise_if_stop_requested()
    logger.info("下载完成 | 耗时 %.2fs", _timer_cost(download_start))

    if buff_result.status_code == 200 and not isinstance(buff_result.payload, dict):
        raise TypeError("buff163 payload is not a dict")
    if uuyp_result.status_code == 200 and not isinstance(uuyp_result.payload, dict):
        raise TypeError("youpin payload is not a dict")
    if not isinstance(eco_payload, dict):
        raise TypeError("eco payload is not a dict")

    parse_start = _timer_start()
    buff_rows = (
        _parse_buff_payload(buff_result.payload, item_map, normalize_data_timestamp(buff_result.data_timestamp))
        if buff_result.status_code == 200 and isinstance(buff_result.payload, dict)
        else []
    )
    uuyp_rows = (
        _parse_uuyp_payload(uuyp_result.payload, item_map, normalize_data_timestamp(uuyp_result.data_timestamp))
        if uuyp_result.status_code == 200 and isinstance(uuyp_result.payload, dict)
        else []
    )
    eco_rows = _parse_eco_payload(eco_payload, item_map, datetime.now())
    logger.info("解析完成 | buff=%s uuyp=%s eco=%s | 耗时 %.2fs", len(buff_rows), len(uuyp_rows), len(eco_rows), _timer_cost(parse_start))

    write_start = _timer_start()
    db = SessionLocal()
    try:
        buff_mapping_updates = _sync_buff_goods_ids(db, buff_goods_map)
        template_updates = _sync_uuyp_template_ids(db, uuyp_template_map)
        total = _upsert_market_prices(db, buff_rows + uuyp_rows + eco_rows)
        db.commit()
        if buff_result.status_code == 200 and buff_result.cache_metadata:
            cache["buff"] = buff_result.cache_metadata
        if uuyp_result.status_code == 200 and uuyp_result.cache_metadata:
            cache["uuyp"] = uuyp_result.cache_metadata
        _save_http_cache(cache)
        logger.info(
            "baseline mapper backfill | buff_mapping_updates=%s uuyp_template_updates=%s",
            buff_mapping_updates,
            template_updates,
        )
        logger.info("入库完成 | total=%s template_updates=%s | 耗时 %.2fs", total, template_updates, _timer_cost(write_start))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    logger.info("sync_baseline 启动")
    asyncio.run(sync_baseline())


if __name__ == "__main__":
    main()
