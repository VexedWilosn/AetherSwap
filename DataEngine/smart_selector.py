from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import ItemBase, MarketPrice, SessionLocal
from DataEngine.priority_scheduler import PRIORITY_STEAMDT_CANDIDATE, recalculate_priorities

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"

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

SMART_SELECTOR_LIMIT = int(os.getenv("AETHERSWAP_SMART_SELECTOR_LIMIT", "50"))


@dataclass(frozen=True)
class SmartSelectorItem:
    market_hash_name: str
    rank: int
    volume_24h: int
    price_cny: float
    profit_rate: float


def get_selector_config() -> dict[str, Any]:
    return {
        "limit": SMART_SELECTOR_LIMIT,
    }


def _apply_hot_targets(db, market_hash_names: list[str]) -> set[int]:
    if not market_hash_names:
        return set()
    now = datetime.now()
    rows = db.query(ItemBase).filter(ItemBase.market_hash_name.in_(market_hash_names)).all()
    touched: set[int] = set()
    for item in rows:
        touched.add(int(item.id))
        if not bool(getattr(item, "manual_watch", False)) and int(item.crawl_priority or 0) < PRIORITY_STEAMDT_CANDIDATE:
            item.crawl_priority = PRIORITY_STEAMDT_CANDIDATE
        item.radar_last_matched_at = now
        item.priority_source = "smart_selector"
        item.priority_reason = "hot_volume_candidate"
        item.priority_updated_at = now
        db.add(item)
    db.commit()
    return touched


def fetch_hot_items(limit: int | None = None) -> list[SmartSelectorItem]:
    cap = limit or SMART_SELECTOR_LIMIT
    with SessionLocal() as db:
        rows = (
            db.query(MarketPrice, ItemBase.market_hash_name)
            .join(ItemBase, MarketPrice.item_id == ItemBase.id)
            .filter(MarketPrice.volume > 10, MarketPrice.sell_min > 5)
            .order_by(MarketPrice.volume.desc())
            .limit(50)
            .all()
        )
        items: list[SmartSelectorItem] = []
        for idx, row in enumerate(rows, start=1):
            market_price = row[0]
            market_hash_name = str(row[1] or "").strip()
            if not market_hash_name:
                continue
            items.append(
                SmartSelectorItem(
                    market_hash_name=market_hash_name,
                    rank=idx,
                    volume_24h=int(getattr(market_price, "volume", 0) or 0),
                    price_cny=float(getattr(market_price, "sell_min", 0) or 0),
                    profit_rate=float(getattr(market_price, "profit_rate", 0) or 0),
                )
            )
            if len(items) >= cap:
                break
        return items


def run_smart_selector() -> int:
    start = time.perf_counter()
    db = SessionLocal()
    try:
        hot_items = fetch_hot_items()
        market_hash_names = [item.market_hash_name for item in hot_items if item.market_hash_name]
        touched_item_ids = _apply_hot_targets(db, market_hash_names)
        if touched_item_ids:
            recalculate_priorities(item_ids=touched_item_ids)
        logger.info("本轮识别到 %s 个热门饰品，已更新雷达目标", len(market_hash_names))
        logger.info("smart_selector 完成 | 耗时 %.2fs", round(time.perf_counter() - start, 2))
        return len(market_hash_names)
    except Exception as exc:
        db.rollback()
        logger.exception("smart_selector 执行失败 | err=%s", exc)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_smart_selector()
