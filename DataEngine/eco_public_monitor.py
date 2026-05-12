from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from DataEngine.proxy_observer import proxy_tag
from eco.openapi_client import EcoOpenAPIClient, EcoOpenAPIConfig, RSAPrvCrypt, load_eco_openapi_config, normalize_eco_response_payload

logger = logging.getLogger(__name__)

PLATFORM_NAME = "eco"
ECO_BATCH_PRICE_PATH = "/Api/Market/BatchSearchSellingPrice"
MAX_BATCH_SIZE = 100


def _chunked(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _nested_price(row: dict[str, Any], *keys: str) -> Any:
    value = _first_value(row, *keys)
    if isinstance(value, dict):
        return _first_value(value, "CNY", "cny", "RMB", "rmb", "Price", "price", "Value", "value", "Amount", "amount")
    return value


def _extract_sell_min(row: dict[str, Any]) -> float:
    return _safe_float(
        _nested_price(
            row,
            "Price",
            "price",
            "SellMin",
            "sellMin",
            "SellPrice",
            "sellPrice",
            "SellingPrice",
            "sellingPrice",
            "LowestPrice",
            "lowestPrice",
            "LowestSellPrice",
            "lowestSellPrice",
            "MinSellPrice",
            "minSellPrice",
            "MinPrice",
            "minPrice",
            "SalePrice",
            "salePrice",
            "SaleMinPrice",
            "saleMinPrice",
            "StartingAt",
            "starting_at",
            "startingAt",
        )
    )


def _extract_buy_max(row: dict[str, Any]) -> float:
    return _safe_float(
        _nested_price(
            row,
            "QGMaxPrice",
            "qgMaxPrice",
            "BuyMax",
            "buyMax",
            "BuyPrice",
            "buyPrice",
            "PurchasePrice",
            "purchasePrice",
            "HighestOrder",
            "highest_order",
            "HighestOrderPrice",
            "highestOrderPrice",
            "MaxBuyPrice",
            "maxBuyPrice",
        )
    )


def _extract_volume(row: dict[str, Any]) -> int:
    return _safe_int(
        _first_value(
            row,
            "SellingTotal",
            "sellingTotal",
            "SellNum",
            "sellNum",
            "SellCount",
            "sellCount",
            "Volume",
            "volume",
            "Count",
            "count",
        )
    )


def _normalize_result_rows(result_data: Any) -> list[dict[str, Any]]:
    if isinstance(result_data, list):
        return [row for row in result_data if isinstance(row, dict)]
    if isinstance(result_data, dict):
        for key in ("List", "list", "Rows", "rows", "Data", "data", "Items", "items", "PageResult", "pageResult"):
            rows = result_data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        current_goods = result_data.get("CurrentGoods") or result_data.get("currentGoods")
        if isinstance(current_goods, dict):
            return [current_goods]
        rows: list[dict[str, Any]] = []
        for key, value in result_data.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("HashName", key)
                rows.append(row)
        if rows:
            return rows
    return []


def _build_item_index(eco_items: list[dict[str, Any]]) -> dict[str, int]:
    item_map: dict[str, int] = {}
    for item in eco_items:
        item_id = item.get("item_id")
        hash_name = item.get("hash_name")
        if item_id is None or not hash_name:
            continue
        item_map[str(hash_name).strip()] = int(item_id)
    return item_map


def _row_hash_name(row: dict[str, Any], fallback: str = "") -> str:
    return str(
        row.get("HashName")
        or row.get("hashName")
        or row.get("MarketHashName")
        or row.get("marketHashName")
        or fallback
        or ""
    ).strip()


def _to_market_price_row(row: dict[str, Any], item_id: int) -> dict[str, Any]:
    sell_min = _extract_sell_min(row)
    buy_max = _extract_buy_max(row)
    volume = _extract_volume(row)
    return {
        "item_id": item_id,
        "platform_name": PLATFORM_NAME,
        "sell_min": sell_min,
        "sell_top5_avg": sell_min,
        "buy_max": buy_max,
        "buy_top5_avg": buy_max,
        "volume": volume,
        "currency": "CNY",
    }


async def fetch_eco_prices(eco_items: list[dict], session: AsyncSession) -> list[dict]:
    """Fetch ECO prices via official OpenAPI with RSA signing."""

    cfg = load_eco_openapi_config()
    if cfg is None:
        return []

    item_map = _build_item_index(eco_items)
    if not item_map:
        return []

    client = EcoOpenAPIClient(cfg)
    results: list[dict[str, Any]] = []

    for batch in _chunked(list(item_map.keys()), MAX_BATCH_SIZE):
        logger.info("[ECO] request %s batch_size=%s", proxy_tag(None), len(batch))
        try:
            data = await client.async_post(
                session,
                ECO_BATCH_PRICE_PATH,
                {"GameID": "730", "HashName": batch},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("[ECO] request failed | batch_size=%s err=%s", len(batch), exc)
            continue

        if not client.is_success(data):
            logger.warning("[ECO] business failure | batch_size=%s msg=%s", len(batch), client.result_message(data))
            continue

        rows = _normalize_result_rows(data.get("ResultData") or data.get("resultData") or data.get("data"))
        if not rows:
            logger.debug("[ECO] empty quote | batch_size=%s", len(batch))
            continue

        for row in rows:
            row_hash = _row_hash_name(row)
            item_id = item_map.get(row_hash)
            if item_id is None:
                continue
            results.append(_to_market_price_row(row, item_id))

    return results


async def fetch_eco_price(hash_name: str, session: AsyncSession) -> Optional[dict[str, Any]]:
    eco_items = [{"item_id": 1, "hash_name": hash_name}]
    results = await fetch_eco_prices(eco_items, session)
    if not results:
        return None
    result = dict(results[0])
    result.pop("item_id", None)
    return result


async def _fetch_single_eco_item(item: dict[str, Any], session: AsyncSession) -> Optional[dict[str, Any]]:
    item_id = item.get("item_id")
    hash_name = item.get("hash_name")
    if item_id is None or not hash_name:
        return None
    result = await fetch_eco_price(hash_name, session)
    if not result:
        return None
    out = {"item_id": item_id, "platform_name": PLATFORM_NAME}
    out.update(result)
    return out


async def fetch_eco_prices_legacy(items: list[dict], session: AsyncSession) -> list[dict]:
    results = await asyncio.gather(*[_fetch_single_eco_item(item, session) for item in items], return_exceptions=True)
    out: list[dict] = []
    for result in results:
        if isinstance(result, dict):
            out.append(result)
    return out


__all__ = [
    "EcoOpenAPIConfig",
    "RSAPrvCrypt",
    "fetch_eco_price",
    "fetch_eco_prices",
    "fetch_eco_prices_legacy",
    "load_eco_openapi_config",
    "normalize_eco_response_payload",
    "_extract_buy_max",
    "_extract_sell_min",
    "_extract_volume",
    "_normalize_result_rows",
]
