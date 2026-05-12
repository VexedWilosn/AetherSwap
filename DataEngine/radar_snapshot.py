from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from DataEngine.database import ItemBase, MarketPrice, PlatformMapping, RadarSnapshot, SessionLocal
from DataEngine.profit_model import cash_to_steam_profit, lowest_nonzero_cashout_bid, steam_balance_cost_ratio, steam_to_cash_profit


RADAR_PLATFORMS = ("steam", "buff", "uuyp", "eco")
TRADE_PLATFORMS = ("buff", "uuyp", "eco")
BASE_DIR = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"


def load_app_config() -> dict[str, Any]:
    try:
        if APP_CONFIG_PATH.exists():
            return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8") or "{}") or {}
    except Exception:
        return {}
    return {}


def _load_uuyp_mapper() -> dict[str, str]:
    path = __import__("pathlib").Path(__file__).resolve().parent / "SteamTradingSite-ID-Mapper-main" / "uuyp" / "730.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        return {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _price_payload(price: MarketPrice) -> dict[str, Any]:
    data_source = (getattr(price, "data_source", None) or "").lower()
    sell_volume = int(getattr(price, "sell_volume", 0) or 0)
    buy_volume = int(getattr(price, "buy_volume", 0) or 0)
    orderbook_depth = int(getattr(price, "orderbook_depth", 0) or 0)
    if data_source == "steamdt_openapi" and orderbook_depth <= 0:
        orderbook_depth = sell_volume + buy_volume
    volume_24h = 0 if data_source == "steamdt_openapi" else int(price.volume or 0)
    return {
        "sell_min": float(price.sell_min or 0),
        "buy_max": float(price.buy_max or 0),
        "sell_top5_avg": float(price.sell_top5_avg or 0),
        "buy_top5_avg": float(price.buy_top5_avg or 0),
        "volume": volume_24h,
        "sell_volume": sell_volume,
        "buy_volume": buy_volume,
        "orderbook_depth": orderbook_depth,
        "orderbook_balance": float(getattr(price, "orderbook_balance", 0.0) or 0.0),
        "liquidity_score": float(getattr(price, "liquidity_score", 0.0) or 0.0),
        "liquidity_source": getattr(price, "liquidity_source", None) or "",
        "currency": getattr(price, "currency", "CNY") or "CNY",
        "updated_at": _iso(getattr(price, "updated_at", None)),
        "data_source": getattr(price, "data_source", None) or "",
    }


def _is_non_decision_price(price: dict[str, Any]) -> bool:
    data_source = str(price.get("data_source") or "").lower().strip()
    return data_source == "baseline"


def build_radar_entries(
    session,
    item_ids: list[int] | None = None,
    search: str = "",
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(ItemBase, MarketPrice)
        .join(MarketPrice, MarketPrice.item_id == ItemBase.id)
        .where(ItemBase.crawl_priority > 0)
        .where(MarketPrice.platform_name.in_(RADAR_PLATFORMS))
    )
    if item_ids is not None:
        if not item_ids:
            return []
        stmt = stmt.where(ItemBase.id.in_(item_ids))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where((ItemBase.market_hash_name.like(like)) | (ItemBase.cn_name.like(like)))
    rows = session.execute(stmt).all()
    if not rows:
        return []

    ids = list({int(item.id) for item, _ in rows})
    mapping_rows = (
        session.query(PlatformMapping.item_id, PlatformMapping.platform_name, PlatformMapping.platform_item_id)
        .filter(PlatformMapping.item_id.in_(ids))
        .all()
    )
    mapping_by_item: dict[int, dict[str, str]] = {}
    for item_id, platform_name, platform_item_id in mapping_rows:
        mapping_by_item.setdefault(int(item_id), {})[str(platform_name).lower().strip()] = str(platform_item_id)

    uuyp_mapper = _load_uuyp_mapper()
    balance_ratio = steam_balance_cost_ratio(config)
    grouped: dict[int, dict[str, Any]] = {}
    for item, price in rows:
        mappings = mapping_by_item.get(item.id, {})
        entry = grouped.setdefault(
            item.id,
            {
                "item_id": int(item.id),
                "item_name": item.cn_name or item.market_hash_name,
                "market_hash_name": item.market_hash_name,
                "buff_goods_id": item.buff_goods_id or mappings.get("buff"),
                "uuyp_template_id": item.uuyp_template_id or mappings.get("uuyp") or uuyp_mapper.get(item.market_hash_name),
                "eco_goods_id": item.eco_goods_id or mappings.get("eco"),
                "crawl_priority": int(item.crawl_priority or 0),
                "priority_score": float(getattr(item, "priority_score", 0.0) or 0.0),
                "priority_reason": getattr(item, "priority_reason", None),
                "priority_source": getattr(item, "priority_source", None),
                "radar_last_matched_at": _iso(getattr(item, "radar_last_matched_at", None)),
                "steam": {"buy_max": 0.0, "sell_min": 0.0, "updated_at": None},
                "platforms": {},
                "currency": "CNY",
            },
        )
        platform = (price.platform_name or "").lower().strip()
        payload = _price_payload(price)
        if platform == "steam":
            existing = entry.get("steam") or {}
            existing_ts = existing.get("updated_at")
            new_ts = payload.get("updated_at")
            existing_src = str(existing.get("data_source") or "").lower()
            new_src = str(payload.get("data_source") or "").lower()
            if (
                not existing
                or (new_src == "steam" and existing_src != "steam")
                or (new_ts and existing_ts and new_ts > existing_ts)
                or (new_ts and not existing_ts)
            ):
                entry["steam"] = payload
        elif platform in TRADE_PLATFORMS:
            existing = entry["platforms"].get(platform)
            existing_ts = existing.get("updated_at") if isinstance(existing, dict) else None
            new_ts = payload.get("updated_at")
            if existing is None or (new_ts and existing_ts and new_ts > existing_ts) or (new_ts and not existing_ts):
                entry["platforms"][platform] = payload

    entries: list[dict[str, Any]] = []
    for entry in grouped.values():
        steam_buy_raw = float(entry["steam"].get("buy_max") or 0)
        steam_sell = float(entry["steam"].get("sell_min") or 0)
        best_platform = None
        best_price = 0.0
        best_platform_buy = 0.0
        best_cash_to_steam_platform = None
        best_cash_to_steam_price = 0.0
        best_cash_to_steam = None
        steam_to_cash_candidates: list[tuple[str, float, Any]] = []
        best_volume = 0
        total_volume = 0
        best_orderbook_depth = 0
        total_orderbook_depth = 0
        best_liquidity_score = 0.0
        liquidity_sell_volume = 0
        liquidity_buy_volume = 0
        liquidity_orderbook_depth = 0
        liquidity_orderbook_balance = 0.0
        liquidity_platform = None
        for price in entry["platforms"].values():
            price["ignored_for_profit"] = _is_non_decision_price(price)

        decision_prices = {
            platform: price
            for platform, price in entry["platforms"].items()
            if not bool(price.get("ignored_for_profit"))
        }
        cash_bids = [
            float(price.get("buy_max") or 0)
            for price in decision_prices.values()
            if float(price.get("buy_max") or 0) > 0
        ]
        cash_bid_floor = min(cash_bids) if cash_bids else 0.0
        for platform, price in decision_prices.items():
            sell_min = float(price.get("sell_min") or 0)
            buy_max = float(price.get("buy_max") or 0)
            peer_floor = min((bid for bid in cash_bids if bid != buy_max), default=cash_bid_floor)
            if buy_max > 0 and peer_floor > 0 and buy_max >= max(peer_floor * 1.35, peer_floor + 100.0):
                buy_max = 0.0
                price["buy_max"] = 0.0
                price["buy_top5_avg"] = 0.0
            volume = int(price.get("volume") or 0)
            orderbook_depth = int(price.get("orderbook_depth") or 0)
            liquidity_score = float(price.get("liquidity_score") or 0)
            total_volume += volume
            total_orderbook_depth += orderbook_depth
            if sell_min > 0 and (best_price <= 0 or sell_min < best_price):
                best_price = sell_min
                best_platform = platform
                best_volume = volume
                best_orderbook_depth = orderbook_depth
            if buy_max > 0 and buy_max > best_platform_buy:
                best_platform_buy = buy_max
            if sell_min > 0 and steam_sell > 0:
                candidate = cash_to_steam_profit(sell_min, steam_sell)
                if best_cash_to_steam is None or candidate.profit_rate > best_cash_to_steam.profit_rate:
                    best_cash_to_steam = candidate
                    best_cash_to_steam_platform = platform
                    best_cash_to_steam_price = sell_min
            if buy_max > 0 and steam_sell > 0:
                candidate = steam_to_cash_profit(steam_sell, buy_max, platform, balance_ratio)
                steam_to_cash_candidates.append((platform, buy_max, candidate))
            if liquidity_score > best_liquidity_score:
                best_liquidity_score = liquidity_score
                liquidity_sell_volume = int(price.get("sell_volume") or 0)
                liquidity_buy_volume = int(price.get("buy_volume") or 0)
                liquidity_orderbook_depth = int(price.get("orderbook_depth") or 0)
                liquidity_orderbook_balance = float(price.get("orderbook_balance") or 0)
                liquidity_platform = platform

        selected_cashout = lowest_nonzero_cashout_bid(steam_to_cash_candidates)
        if selected_cashout:
            best_steam_to_cash_platform, best_steam_to_cash_price, best_steam_to_cash = selected_cashout
        else:
            best_steam_to_cash_platform = None
            best_steam_to_cash_price = 0.0
            best_steam_to_cash = None

        cash_to_steam_rate = (best_cash_to_steam.profit_rate * 100.0) if best_cash_to_steam else 0.0
        cash_to_steam_profit_cny = best_cash_to_steam.profit_cny if best_cash_to_steam else 0.0
        steam_to_cash_rate = (best_steam_to_cash.profit_rate * 100.0) if best_steam_to_cash else 0.0
        steam_to_cash_profit_cny = best_steam_to_cash.profit_cny if best_steam_to_cash else 0.0
        best_direction = "steam_to_cash" if steam_to_cash_rate > cash_to_steam_rate else "cash_to_steam"
        best_rate = max(cash_to_steam_rate, steam_to_cash_rate)
        best_profit_cny = steam_to_cash_profit_cny if best_direction == "steam_to_cash" else cash_to_steam_profit_cny
        mode_platform = best_steam_to_cash_platform if best_direction == "steam_to_cash" else best_cash_to_steam_platform
        display_platform = best_cash_to_steam_platform or best_platform or mode_platform
        display_price = best_cash_to_steam_price or best_price
        mode_price = best_steam_to_cash_price if best_direction == "steam_to_cash" else display_price
        entry.update(
            {
                "best_platform": display_platform,
                "best_platform_price": display_price,
                "platform_name": mode_platform or display_platform,
                "sell_min": display_price,
                "steam_buy_max": steam_buy_raw,
                "steam_crossed_book": bool(steam_sell > 0 and steam_buy_raw > steam_sell),
                "steam_data_source": str(entry["steam"].get("data_source") or ""),
                "volume": best_volume or total_volume,
                "volume_24h": best_volume or total_volume,
                "depth": best_orderbook_depth or total_orderbook_depth,
                "sell_volume": liquidity_sell_volume,
                "buy_volume": liquidity_buy_volume,
                "orderbook_depth": liquidity_orderbook_depth,
                "orderbook_balance": liquidity_orderbook_balance,
                "liquidity_platform": liquidity_platform,
                "liquidity_score": round(best_liquidity_score, 4),
                "profit_rate": round(best_rate, 2),
                "reverse_profit_rate": round(steam_to_cash_rate, 2),
                "best_profit_rate": round(best_rate, 2),
                "best_direction": best_direction,
                "cash_to_steam_profit_rate": round(cash_to_steam_rate, 2),
                "cash_to_steam_profit_cny": round(cash_to_steam_profit_cny, 4),
                "cash_to_steam_platform": best_cash_to_steam_platform,
                "cash_to_steam_price": best_cash_to_steam_price,
                "steam_to_cash_profit_rate": round(steam_to_cash_rate, 2),
                "steam_to_cash_profit_cny": round(steam_to_cash_profit_cny, 4),
                "steam_to_cash_platform": best_steam_to_cash_platform,
                "steam_to_cash_price": best_steam_to_cash_price,
                "best_profit_cny": round(best_profit_cny, 4),
                "steam_balance_cost_ratio": balance_ratio,
                "steam_sell_min": steam_sell,
                "best_platform_buy_max": best_platform_buy,
            }
        )
        for _, price in entry["platforms"].items():
            if bool(price.get("ignored_for_profit")):
                price["steam_diff"] = 0.0
                price["profit_rate"] = 0.0
                price["cash_to_steam_profit_rate"] = 0.0
                price["cash_to_steam_profit_cny"] = 0.0
                price["reverse_profit_rate"] = 0.0
                price["steam_to_cash_profit_rate"] = 0.0
                price["steam_to_cash_profit_cny"] = 0.0
                continue
            sell_min = float(price.get("sell_min") or 0)
            buy_max = float(price.get("buy_max") or 0)
            price["steam_diff"] = round(steam_buy_raw - sell_min, 2) if sell_min > 0 and steam_buy_raw > 0 else 0.0
            cash_math = cash_to_steam_profit(sell_min, steam_sell) if sell_min > 0 and steam_sell > 0 else None
            steam_math = steam_to_cash_profit(steam_sell, buy_max, _, balance_ratio) if steam_sell > 0 and buy_max > 0 else None
            price["profit_rate"] = round(cash_math.profit_rate * 100.0, 2) if cash_math else 0.0
            price["cash_to_steam_profit_rate"] = price["profit_rate"]
            price["cash_to_steam_profit_cny"] = round(cash_math.profit_cny, 4) if cash_math else 0.0
            price["reverse_profit_rate"] = round(steam_math.profit_rate * 100.0, 2) if steam_math else 0.0
            price["steam_to_cash_profit_rate"] = price["reverse_profit_rate"]
            price["steam_to_cash_profit_cny"] = round(steam_math.profit_cny, 4) if steam_math else 0.0
        entries.append(entry)
    return entries


def upsert_radar_snapshots(session, entries: list[dict[str, Any]]) -> int:
    saved = 0
    now = datetime.now()
    for entry in entries:
        item_id = int(entry.get("item_id") or 0)
        if item_id <= 0:
            continue
        row = session.get(RadarSnapshot, item_id)
        values = {
            "item_name": entry.get("item_name"),
            "market_hash_name": entry.get("market_hash_name") or "",
            "buff_goods_id": entry.get("buff_goods_id"),
            "uuyp_template_id": entry.get("uuyp_template_id"),
            "eco_goods_id": entry.get("eco_goods_id"),
            "crawl_priority": int(entry.get("crawl_priority") or 0),
            "priority_score": float(entry.get("priority_score") or 0),
            "priority_reason": entry.get("priority_reason"),
            "priority_source": entry.get("priority_source"),
            "radar_last_matched_at": None,
            "best_platform": entry.get("best_platform"),
            "best_platform_price": float(entry.get("best_platform_price") or 0),
            "best_platform_buy_max": float(entry.get("best_platform_buy_max") or 0),
            "steam_buy_max": float(entry.get("steam_buy_max") or 0),
            "steam_sell_min": float(entry.get("steam_sell_min") or 0),
            "best_profit_rate": float(entry.get("best_profit_rate") or 0),
            "profit_rate": float(entry.get("profit_rate") or 0),
            "reverse_profit_rate": float(entry.get("reverse_profit_rate") or 0),
            "best_direction": entry.get("best_direction"),
            "cash_to_steam_profit_rate": float(entry.get("cash_to_steam_profit_rate") or 0),
            "cash_to_steam_profit_cny": float(entry.get("cash_to_steam_profit_cny") or 0),
            "cash_to_steam_platform": entry.get("cash_to_steam_platform"),
            "cash_to_steam_price": float(entry.get("cash_to_steam_price") or 0),
            "steam_to_cash_profit_rate": float(entry.get("steam_to_cash_profit_rate") or 0),
            "steam_to_cash_profit_cny": float(entry.get("steam_to_cash_profit_cny") or 0),
            "steam_to_cash_platform": entry.get("steam_to_cash_platform"),
            "steam_to_cash_price": float(entry.get("steam_to_cash_price") or 0),
            "best_profit_cny": float(entry.get("best_profit_cny") or 0),
            "steam_balance_cost_ratio": float(entry.get("steam_balance_cost_ratio") or 0),
            "steam_crossed_book": bool(entry.get("steam_crossed_book")),
            "steam_data_source": entry.get("steam_data_source"),
            "volume": int(entry.get("volume") or 0),
            "volume_24h": int(entry.get("volume_24h") or 0),
            "depth": int(entry.get("depth") or 0),
            "sell_volume": int(entry.get("sell_volume") or 0),
            "buy_volume": int(entry.get("buy_volume") or 0),
            "orderbook_depth": int(entry.get("orderbook_depth") or 0),
            "orderbook_balance": float(entry.get("orderbook_balance") or 0),
            "liquidity_platform": entry.get("liquidity_platform"),
            "liquidity_score": float(entry.get("liquidity_score") or 0),
            "currency": entry.get("currency") or "CNY",
            "platform_payload_json": json.dumps({"steam": entry.get("steam") or {}, "platforms": entry.get("platforms") or {}}, ensure_ascii=False),
            "snapshot_updated_at": now,
        }
        matched_at = entry.get("radar_last_matched_at")
        if matched_at:
            try:
                values["radar_last_matched_at"] = datetime.fromisoformat(str(matched_at))
            except Exception:
                values["radar_last_matched_at"] = None
        if row is None:
            session.add(RadarSnapshot(item_id=item_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        saved += 1
    return saved


def refresh_radar_snapshots(item_ids: list[int] | None = None, config: dict[str, Any] | None = None) -> int:
    cfg = config if config is not None else load_app_config()
    with SessionLocal() as session:
        entries = build_radar_entries(session, item_ids=item_ids, config=cfg)
        if item_ids is not None:
            built_ids = {int(entry.get("item_id") or 0) for entry in entries}
            stale_ids = [int(item_id) for item_id in item_ids if int(item_id) not in built_ids]
            if stale_ids:
                session.query(RadarSnapshot).filter(RadarSnapshot.item_id.in_(stale_ids)).delete(synchronize_session=False)
        saved = upsert_radar_snapshots(session, entries)
        session.commit()
        return saved
