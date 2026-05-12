from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from curl_cffi import requests as cffi_requests
from sqlalchemy import delete

from DataEngine.database import ItemBase, SessionLocal, engine, init_db
from DataEngine.cn_name_mapper import load_steam_cn_name_map

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

CSGOTRADER_URL = "https://prices.csgotrader.app/latest/buff163.json"


async def fetch_csgotrader_json() -> dict[str, Any]:
    """异步拉取 CSGOTrader 的最新全网饰品 JSON。"""
    async with cffi_requests.AsyncSession(impersonate="chrome110") as session:
        resp = await session.get(CSGOTRADER_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("CSGOTrader 返回的数据格式不是字典")
        return data


def rebuild_itembase(items: list[str]) -> int:
    """清空旧数据并批量写入 ItemBase。"""
    session = SessionLocal()
    try:
        init_db()
        cn_name_map = load_steam_cn_name_map()
        # 先清空现有残留
        session.execute(delete(ItemBase))
        session.commit()

        # bulk_insert_mappings 极速入库
        mappings = [
            {
                "market_hash_name": name,
                "cn_name": cn_name_map.get(name),
                "game": "csgo",
                "crawl_priority": 3,
                "base_profit_margin": 0.0,
                "is_active": True,
            }
            for name in items
        ]

        if mappings:
            session.bulk_insert_mappings(ItemBase, mappings)
            session.commit()

        return len(mappings)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def main() -> None:
    logger.info("开始从 CSGOTrader 拉取饰品字典数据...")
    data = await fetch_csgotrader_json()

    # 顶级 Key 即 market_hash_name
    market_hash_names = list(data.keys())
    logger.info("解析完成，共提取到 %s 个饰品名，准备写入数据库", len(market_hash_names))

    inserted = rebuild_itembase(market_hash_names)
    logger.info("✅ ItemBase 重建完成，成功入库 %s 个饰品", inserted)


if __name__ == "__main__":
    asyncio.run(main())
