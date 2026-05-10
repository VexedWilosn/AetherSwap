import asyncio
import argparse
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from curl_cffi.requests import AsyncSession
from sqlalchemy.orm import Session, joinedload

from DataEngine.buff_public_monitor import fetch_buff_prices, is_buff_circuit_open, buff_circuit_remaining_seconds
from DataEngine.database import (
    Base,
    DB_PATH,
    ItemBase,
    SessionLocal,
    engine,
    normalize_data_timestamp,
    upsert_market_price_if_fresh,
)
from DataEngine.eco_public_monitor import fetch_eco_prices
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.proxy_pool import warmup_proxy_pool
from DataEngine.stop_signal import StopRequested, raise_if_stop_requested
from DataEngine.uuyp_public_monitor import fetch_uuyp_prices, is_uuyp_auth_circuit_open, uuyp_auth_circuit_remaining_seconds

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"
RUNTIME_STATE_PATH = BASE_DIR / "config" / "platform_runtime_state.json"

setup_dataengine_logging()
logger = logging.getLogger(__name__)

PLATFORM_TIMEOUTS = {
    "buff": 25,
    "buff_jit": 25,
    "uuyp": 25,
    "eco": 25,
    "steam": 55,
    "steam_jit": 12,
}

PRIORITY_PAUSED = 0
PRIORITY_LOW_FREQ = 1
PRIORITY_STEAMDT_CANDIDATE = 2
PRIORITY_HIGH_FREQ = 3
PRIORITY_JIT = 4


@dataclass(frozen=True)
class PlatformFetchTask:
    name: str
    items: list[dict]
    fetch_coro: Awaitable[list[dict]] | None
    timeout: int
    proxy_policy: str = "proxy_pool"

    @property
    def item_count(self) -> int:
        return len(self.items)


def _timer_start() -> float:
    return time.perf_counter()


def _timer_cost(start: float) -> float:
    return round(time.perf_counter() - start, 2)


def _update_platform_runtime_state(
    platform: str,
    *,
    status: str,
    rows: int | None = None,
    saved: int | None = None,
    reason: str = "",
    cost_seconds: float | None = None,
) -> None:
    try:
        payload: dict = {}
        if RUNTIME_STATE_PATH.exists():
            payload = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        node = payload.get(platform) if isinstance(payload.get(platform), dict) else {}
        node = dict(node or {})
        node["platform"] = platform
        node["status"] = status
        if rows is not None:
            node["rows"] = int(rows)
        if saved is not None:
            node["saved"] = int(saved)
        node["reason"] = str(reason or "")
        if cost_seconds is not None:
            node["cost_seconds"] = float(cost_seconds)
        node["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload[platform] = node
        RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("runtime state write failed | platform=%s", platform)


def ensure_db_ready() -> None:
    Base.metadata.create_all(engine)
    logger.info("database ready | path=%s", DB_PATH)
    warmup_proxy_pool()


def _platform_circuit_open(name: str) -> tuple[bool, int]:
    if name == "steam":
        from DataEngine.steam_public_monitor import is_steam_circuit_open, steam_circuit_remaining_seconds

        if is_steam_circuit_open():
            return True, steam_circuit_remaining_seconds()
    if name == "buff" and is_buff_circuit_open():
        return True, buff_circuit_remaining_seconds()
    if name == "uuyp" and is_uuyp_auth_circuit_open():
        return True, uuyp_auth_circuit_remaining_seconds()
    return False, 0


def _mapping_by_platform(item: ItemBase) -> dict[str, str]:
    return {m.platform_name.lower().strip(): m.platform_item_id for m in item.mappings if m.platform_name}


def _targets_from_item(item: ItemBase, wanted: set[str]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    mapping = _mapping_by_platform(item)
    buff_items: list[dict] = []
    uuyp_items: list[dict] = []
    eco_items: list[dict] = []
    steam_items: list[dict] = []

    buff_goods_id = getattr(item, "buff_goods_id", None) or mapping.get("buff")
    if "buff" in wanted and buff_goods_id:
        buff_items.append({"item_id": item.id, "platform_id": str(buff_goods_id)})

    uuyp_template_id = getattr(item, "uuyp_template_id", None) or mapping.get("uuyp")
    if "uuyp" in wanted and uuyp_template_id:
        uuyp_items.append(
            {
                "item_id": item.id,
                "platform_id": str(uuyp_template_id),
                "market_hash_name": item.market_hash_name,
            }
        )

    eco_goods_id = getattr(item, "eco_goods_id", None) or mapping.get("eco")
    if "eco" in wanted and item.market_hash_name:
        eco_items.append({"item_id": item.id, "platform_id": str(eco_goods_id or ""), "hash_name": item.market_hash_name})

    if "steam" in wanted and item.market_hash_name:
        steam_items.append({"item_id": item.id, "hash_name": item.market_hash_name})

    return buff_items, uuyp_items, eco_items, steam_items


def load_target_items(
    db: Session,
    *,
    min_priority: int = PRIORITY_HIGH_FREQ,
    limit: int | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    raise_if_stop_requested()
    start = _timer_start()
    query = (
        db.query(ItemBase)
        .options(joinedload(ItemBase.mappings))
        .filter(ItemBase.is_active.is_(True), ItemBase.crawl_priority >= int(min_priority))
        .order_by(
            getattr(ItemBase, "priority_score", ItemBase.crawl_priority).desc(),
            ItemBase.crawl_priority.desc(),
            ItemBase.id.asc(),
        )
    )
    if limit is not None:
        query = query.limit(max(1, int(limit)))
    rows = query.all()

    buff_items: list[dict] = []
    uuyp_items: list[dict] = []
    eco_items: list[dict] = []
    steam_items: list[dict] = []
    wanted = {"buff", "uuyp", "eco", "steam"}
    for item in rows:
        b, u, e, s = _targets_from_item(item, wanted)
        buff_items.extend(b)
        uuyp_items.extend(u)
        eco_items.extend(e)
        steam_items.extend(s)

    logger.info(
        "loaded target items | total=%s min_priority=%s buff=%s uuyp=%s eco=%s steam=%s cost=%.2fs",
        len(rows),
        min_priority,
        len(buff_items),
        len(uuyp_items),
        len(eco_items),
        len(steam_items),
        _timer_cost(start),
    )
    return buff_items, uuyp_items, eco_items, steam_items


def load_single_item_targets(db: Session, item_id: int, platforms: set[str]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    item = (
        db.query(ItemBase)
        .options(joinedload(ItemBase.mappings))
        .filter(ItemBase.id == item_id)
        .one_or_none()
    )
    if item is None:
        return [], [], [], []
    wanted = {p.lower().strip() for p in platforms if p}
    return _targets_from_item(item, wanted)


def _invalid_quote(row: dict) -> bool:
    if (row.get("platform_name") or "").lower() != "uuyp":
        return False
    try:
        sell_min = float(row.get("sell_min") or 0)
        buy_max = float(row.get("buy_max") or 0)
        volume = int(row.get("volume") or 0)
    except (TypeError, ValueError):
        return True
    return sell_min <= 0 and buy_max <= 0 and volume <= 0


def save_market_prices(db: Session, all_results: list[dict]) -> int:
    start = _timer_start()
    updated_count = 0
    invalid_skipped = 0
    touched_item_ids: set[int] = set()
    for row in all_results:
        raise_if_stop_requested()
        if _invalid_quote(row):
            invalid_skipped += 1
            logger.debug("invalid quote skipped | row=%s", row)
            continue

        item_id = row.get("item_id")
        platform_name = row.get("platform_name") or row.get("data_source") or "steam"
        if item_id is None:
            logger.warning("market row skipped: missing item_id | row=%s", row)
            continue

        data_source = row.get("data_source") or platform_name or "steam"
        new_timestamp = normalize_data_timestamp(
            row.get("new_timestamp") or row.get("data_timestamp") or row.get("updated_at") or datetime.now()
        )

        try:
            if upsert_market_price_if_fresh(
                db,
                item_id=int(item_id),
                platform_name=str(platform_name),
                data_source=str(data_source),
                sell_min=row.get("sell_min", row.get("Price", 0)),
                sell_top5_avg=row.get("sell_top5_avg", row.get("sell_min", row.get("Price", 0))),
                buy_max=row.get("buy_max", row.get("QGMaxPrice", 0)),
                buy_top5_avg=row.get("buy_top5_avg", row.get("buy_max", row.get("QGMaxPrice", 0))),
                volume=row.get("volume", row.get("SellingTotal", 0)),
                currency=row.get("currency") or "CNY",
                new_timestamp=new_timestamp,
                log=logger,
            ):
                updated_count += 1
                touched_item_ids.add(int(item_id))
        except Exception as exc:
            logger.exception("market price upsert failed | item_id=%s platform=%s row=%s err=%s", item_id, platform_name, row, exc)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("market price commit failed | count=%s err=%s", updated_count, exc)
        raise

    if invalid_skipped:
        logger.info("invalid quotes skipped | count=%s", invalid_skipped)
    if touched_item_ids:
        try:
            from DataEngine.radar_snapshot import refresh_radar_snapshots

            refresh_radar_snapshots(list(touched_item_ids))
        except Exception as exc:
            logger.warning("radar snapshot update failed | items=%s err=%s", len(touched_item_ids), exc)
    logger.info("market prices saved | count=%s cost=%.2fs", updated_count, _timer_cost(start))
    return updated_count


async def _run_platform_fetch(name: str, coro: Awaitable[list[dict]], item_count: int, timeout: int) -> tuple[list[dict], str, str, float]:
    if item_count <= 0:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        logger.info("platform fetch skipped | platform=%s items=0", name)
        return [], "idle", "items=0", 0.0

    circuit_open, cooldown = _platform_circuit_open(name)
    if circuit_open:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        logger.warning("platform fetch skipped by circuit | platform=%s items=%s cooldown=%ss", name, item_count, cooldown)
        return [], "degraded", f"circuit_open cooldown={cooldown}s", 0.0

    logger.info("platform fetch start | platform=%s items=%s timeout=%ss", name, item_count, timeout)
    start = _timer_start()
    task = asyncio.create_task(coro)
    try:
        result = await asyncio.wait_for(task, timeout=timeout)
        raise_if_stop_requested()
    except StopRequested:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("platform fetch stopped by request | platform=%s items=%s", name, item_count)
        raise
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.warning("platform fetch timeout, skipped | platform=%s items=%s timeout=%ss", name, item_count, timeout)
        return [], "timeout", f"timeout {timeout}s", _timer_cost(start)
    except Exception as exc:
        logger.exception("platform fetch failed, skipped | platform=%s err=%s", name, exc)
        return [], "error", str(exc), _timer_cost(start)

    if not isinstance(result, list):
        logger.warning("platform fetch returned invalid type | platform=%s type=%s", name, type(result).__name__)
        return [], "error", f"invalid result type {type(result).__name__}", _timer_cost(start)

    cost = _timer_cost(start)
    logger.info("platform fetch done | platform=%s results=%s cost=%.2fs", name, len(result), cost)
    return result, "running", "", cost


async def _run_platform_task(task: PlatformFetchTask) -> list[dict]:
    start = _timer_start()
    _update_platform_runtime_state(task.name, status="running", rows=0, reason="scheduled")
    logger.info(
        "platform task scheduled | platform=%s items=%s timeout=%ss proxy_policy=%s",
        task.name,
        task.item_count,
        task.timeout,
        task.proxy_policy,
    )
    result, status, reason, cost = await _run_platform_fetch(task.name, task.fetch_coro, task.item_count, task.timeout)
    _update_platform_runtime_state(task.name, status=status, rows=len(result), reason=reason, cost_seconds=cost)
    logger.info(
        "platform task finished | platform=%s rows=%s cost=%.2fs",
        task.name,
        len(result),
        _timer_cost(start),
    )
    return result


def _save_platform_results(name: str, results: list[dict]) -> int:
    if not results:
        _update_platform_runtime_state(name, status="no_data", rows=0, saved=0, reason="empty_result")
        return 0
    db = SessionLocal()
    try:
        saved = save_market_prices(db, results)
        logger.info("platform results committed | platform=%s rows=%s saved=%s", name, len(results), saved)
        _update_platform_runtime_state(name, status="ok", rows=len(results), saved=saved, reason="")
        return saved
    finally:
        db.close()


async def _fetch_all_platforms(
    buff_items: list[dict],
    uuyp_items: list[dict],
    eco_items: list[dict],
    steam_items: list[dict],
    *,
    fast: bool,
    on_platform_result: Callable[[str, list[dict]], None] | None = None,
) -> list[dict]:
    async def _named_fetch(task: PlatformFetchTask) -> tuple[str, list[dict]]:
        return task.name, await _run_platform_task(task)

    async with AsyncSession(impersonate="chrome110") as session:
        steam_timeout = PLATFORM_TIMEOUTS["steam_jit"] if fast else PLATFORM_TIMEOUTS["steam"]
        steam_coro = None
        if steam_items:
            from DataEngine.steam_public_monitor import fetch_steam_prices

            steam_coro = fetch_steam_prices(steam_items, session, fast=fast)
        platform_tasks = [
            PlatformFetchTask(
                name="buff",
                items=buff_items,
                fetch_coro=fetch_buff_prices(buff_items, session, fast=fast),
                timeout=PLATFORM_TIMEOUTS["buff_jit"] if fast else PLATFORM_TIMEOUTS["buff"],
                proxy_policy="proxy_pool:weighted_health",
            ),
            PlatformFetchTask(
                name="uuyp",
                items=uuyp_items,
                fetch_coro=fetch_uuyp_prices(uuyp_items, session),
                timeout=PLATFORM_TIMEOUTS["uuyp"],
                proxy_policy="proxy_pool:weighted_health",
            ),
            PlatformFetchTask(
                name="eco",
                items=eco_items,
                fetch_coro=fetch_eco_prices(eco_items, session),
                timeout=PLATFORM_TIMEOUTS["eco"],
                proxy_policy="direct",
            ),
            PlatformFetchTask(
                name="steam",
                items=steam_items,
                fetch_coro=steam_coro,
                timeout=steam_timeout,
                proxy_policy="env_proxy_or_proxy_pool",
            ),
        ]
        tasks = [asyncio.create_task(_named_fetch(task)) for task in platform_tasks]

        results = []
        for task in asyncio.as_completed(tasks):
            try:
                name, platform_result = await task
            except StopRequested:
                for pending in tasks:
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("crawl round stopped by request")
                raise
            except Exception as exc:
                logger.exception("platform task failed", exc_info=exc)
                results.append(exc)
                continue
            if isinstance(platform_result, list) and on_platform_result:
                try:
                    on_platform_result(name, platform_result)
                except Exception:
                    logger.exception("platform result callback failed | platform=%s", name)
            results.append(platform_result)

    all_results: list[dict] = []
    for platform_result in results:
        if isinstance(platform_result, list):
            all_results.extend(platform_result)
        elif isinstance(platform_result, Exception):
            logger.exception("platform task failed", exc_info=platform_result)
    return all_results


async def run_engine(*, min_priority: int = PRIORITY_HIGH_FREQ, limit: int | None = None, fast: bool = False) -> None:
    raise_if_stop_requested()
    ensure_db_ready()
    db = SessionLocal()
    try:
        buff_items, uuyp_items, eco_items, steam_items = load_target_items(db, min_priority=min_priority, limit=limit)
    finally:
        db.close()

    if not any([buff_items, uuyp_items, eco_items, steam_items]):
        logger.info("no crawl targets found")
        return

    start = _timer_start()
    all_results = await _fetch_all_platforms(
        buff_items,
        uuyp_items,
        eco_items,
        steam_items,
        fast=fast,
        on_platform_result=_save_platform_results,
    )
    logger.info("crawl round done | results=%s cost=%.2fs", len(all_results), _timer_cost(start))


async def refresh_single_item_prices(item_id: int, platforms: set[str]) -> list[dict]:
    raise_if_stop_requested()
    wanted = {"steam", *(p.lower().strip() for p in platforms if p)}
    db = SessionLocal()
    try:
        buff_items, uuyp_items, eco_items, steam_items = load_single_item_targets(db, item_id, wanted)
    finally:
        db.close()

    if not any([buff_items, uuyp_items, eco_items, steam_items]):
        logger.warning("single item refresh has no targets | item_id=%s platforms=%s", item_id, sorted(wanted))
        return []

    all_results = await _fetch_all_platforms(buff_items, uuyp_items, eco_items, steam_items, fast=True)
    if all_results:
        db = SessionLocal()
        try:
            save_market_prices(db, all_results)
        finally:
            db.close()
    return all_results


async def refresh_items_prices(item_ids: set[int], platforms: set[str], fast: bool = True) -> list[dict]:
    raise_if_stop_requested()
    if not item_ids:
        return []

    wanted = {p.lower().strip() for p in platforms if p}
    buff_items: list[dict] = []
    uuyp_items: list[dict] = []
    eco_items: list[dict] = []
    steam_items: list[dict] = []

    db = SessionLocal()
    try:
        rows = (
            db.query(ItemBase)
            .options(joinedload(ItemBase.mappings))
            .filter(ItemBase.id.in_(list(item_ids)))
            .all()
        )
        for item in rows:
            b, u, e, s = _targets_from_item(item, wanted)
            buff_items.extend(b)
            uuyp_items.extend(u)
            eco_items.extend(e)
            steam_items.extend(s)
    finally:
        db.close()

    all_results = await _fetch_all_platforms(buff_items, uuyp_items, eco_items, steam_items, fast=fast)
    if all_results:
        db = SessionLocal()
        try:
            save_market_prices(db, all_results)
        finally:
            db.close()
    return all_results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-priority", type=int, default=PRIORITY_HIGH_FREQ)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()
    logger.info("main_engine start | min_priority=%s limit=%s fast=%s", args.min_priority, args.limit, args.fast)
    asyncio.run(run_engine(min_priority=args.min_priority, limit=args.limit, fast=args.fast))


if __name__ == "__main__":
    main()
