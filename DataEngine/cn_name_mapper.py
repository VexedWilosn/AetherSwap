from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from DataEngine.database import ItemBase, SessionLocal


BASE_DIR = Path(__file__).resolve().parent.parent
STEAM_MAPPER_PATH = BASE_DIR / "DataEngine" / "SteamTradingSite-ID-Mapper-main" / "steam" / "730.json"

_STEAM_CN_NAME_MAP: dict[str, str] | None = None


def _normalize_search_text(value: object) -> str:
    return "".join(ch for ch in str(value or "").casefold() if not ch.isspace())


def load_steam_cn_name_map() -> dict[str, str]:
    global _STEAM_CN_NAME_MAP
    if _STEAM_CN_NAME_MAP is not None:
        return _STEAM_CN_NAME_MAP
    if not STEAM_MAPPER_PATH.exists():
        _STEAM_CN_NAME_MAP = {}
        return _STEAM_CN_NAME_MAP
    try:
        raw = json.loads(STEAM_MAPPER_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        _STEAM_CN_NAME_MAP = {}
        return _STEAM_CN_NAME_MAP
    result: dict[str, str] = {}
    if isinstance(raw, dict):
        for market_hash_name, details in raw.items():
            if not isinstance(market_hash_name, str) or not isinstance(details, dict):
                continue
            cn_name = str(details.get("cn_name") or "").strip()
            if cn_name:
                result[market_hash_name] = cn_name
    _STEAM_CN_NAME_MAP = result
    return result


def search_steam_cn_names(keyword: str, limit: int = 30) -> list[dict[str, str]]:
    query = str(keyword or "").strip()
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        return []
    matches: list[dict[str, str]] = []
    for market_hash_name, cn_name in load_steam_cn_name_map().items():
        if normalized_query in _normalize_search_text(cn_name) or normalized_query in _normalize_search_text(market_hash_name):
            matches.append({"market_hash_name": market_hash_name, "cn_name": cn_name})
            if len(matches) >= limit:
                break
    return matches


def backfill_item_cn_names(session_factory: Callable = SessionLocal, chunk_size: int = 1000) -> int:
    cn_map = load_steam_cn_name_map()
    if not cn_map:
        return 0
    updated = 0
    with session_factory() as session:
        rows = (
            session.query(ItemBase)
            .filter((ItemBase.cn_name.is_(None)) | (ItemBase.cn_name == ""))
            .order_by(ItemBase.id.asc())
            .all()
        )
        for item in rows:
            cn_name = cn_map.get(item.market_hash_name)
            if not cn_name:
                continue
            item.cn_name = cn_name
            session.add(item)
            updated += 1
            if updated % chunk_size == 0:
                session.commit()
        if updated % chunk_size:
            session.commit()
    return updated
