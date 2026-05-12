from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capabilities import CAP_DIRECT_BUY, CAP_PURCHASE_ORDER, supports


@dataclass(frozen=True)
class PlatformMarketQuote:
    platform: str
    sell_orders: tuple[tuple[float, int], ...] = ()
    buy_max: float = 0.0
    target_order_price: float | None = None
    min_order_quantity: int = 0
    weight: float = 1.0


@dataclass(frozen=True)
class PurchasePlanAction:
    action_type: str
    platform: str
    price: float
    quantity: int
    reason: str


@dataclass(frozen=True)
class PurchasePlan:
    item_id: int
    market_hash_name: str
    target_quantity: int
    max_unit_price: float
    direct_quantity: int
    order_quantity: int
    actions: tuple[PurchasePlanAction, ...]
    remaining_quantity: int


def _bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _quote_from_mapping(data: dict[str, Any]) -> PlatformMarketQuote:
    orders = []
    for row in data.get("sell_orders") or data.get("asks") or []:
        if isinstance(row, dict):
            price = _positive_float(row.get("price") or row.get("sell_min"))
            quantity = _positive_int(row.get("quantity") or row.get("num") or row.get("count"), 1)
        elif isinstance(row, (list, tuple)) and row:
            price = _positive_float(row[0])
            quantity = _positive_int(row[1] if len(row) > 1 else 1, 1)
        else:
            continue
        if price > 0 and quantity > 0:
            orders.append((round(price, 2), quantity))
    return PlatformMarketQuote(
        platform=str(data.get("platform") or "").lower().strip(),
        sell_orders=tuple(orders),
        buy_max=_positive_float(data.get("buy_max") or data.get("highest_order")),
        target_order_price=_positive_float(data.get("target_order_price"), 0.0) or None,
        min_order_quantity=_positive_int(data.get("min_order_quantity")),
        weight=_positive_float(data.get("weight"), 1.0),
    )


def coerce_market_quotes(rows: list[PlatformMarketQuote | dict[str, Any]] | tuple[PlatformMarketQuote | dict[str, Any], ...]) -> tuple[PlatformMarketQuote, ...]:
    out: list[PlatformMarketQuote] = []
    for row in rows or []:
        quote = row if isinstance(row, PlatformMarketQuote) else _quote_from_mapping(row) if isinstance(row, dict) else None
        if quote is None or not quote.platform:
            continue
        out.append(quote)
    return tuple(out)


def build_quotes_from_config(
    market_rows: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    default_order_price: float | None = None,
) -> tuple[PlatformMarketQuote, ...]:
    trading = (config or {}).get("cash_platform_trading") if isinstance(config, dict) else {}
    trading = trading if isinstance(trading, dict) else {}
    platform_cfgs = trading.get("platforms") if isinstance(trading.get("platforms"), dict) else {}
    quotes: list[PlatformMarketQuote] = []
    for platform, row in (market_rows or {}).items():
        key = str(platform or "").lower().strip()
        if not key or not isinstance(row, dict):
            continue
        pcfg = platform_cfgs.get(key) if isinstance(platform_cfgs.get(key), dict) else {}
        if not _bool_value(pcfg.get("enabled"), True):
            continue
        sell_min = _positive_float(row.get("sell_min") or row.get("sellMin"))
        sell_volume = _positive_int(row.get("sell_volume") or row.get("sellVolume") or row.get("volume"), 1)
        allow_direct_buy = _bool_value(pcfg.get("allow_direct_buy"), True) and supports(key, CAP_DIRECT_BUY)
        sell_orders = ((round(sell_min, 2), sell_volume),) if sell_min > 0 and allow_direct_buy else ()
        target_order_price = (
            _positive_float(pcfg.get("target_order_price"), 0.0)
            or _positive_float(row.get("target_order_price"), 0.0)
            or _positive_float(default_order_price, 0.0)
            or None
        )
        if not (_bool_value(pcfg.get("allow_purchase_order"), True) and supports(key, CAP_PURCHASE_ORDER)):
            target_order_price = None
        quotes.append(
            PlatformMarketQuote(
                platform=key,
                sell_orders=sell_orders,
                buy_max=_positive_float(row.get("buy_max") or row.get("buyMax")),
                target_order_price=target_order_price,
                min_order_quantity=_positive_int(pcfg.get("min_order_quantity")),
                weight=_positive_float(pcfg.get("purchase_order_weight"), 1.0),
            )
        )
    primary = str(trading.get("primary_platform") or "").lower().strip()
    if primary:
        quotes.sort(key=lambda quote: (quote.platform != primary, quote.platform))
    return tuple(quotes)


def build_purchase_plan(
    *,
    item_id: int,
    market_hash_name: str,
    target_quantity: int,
    max_unit_price: float,
    quotes: list[PlatformMarketQuote | dict[str, Any]] | tuple[PlatformMarketQuote | dict[str, Any], ...],
    default_order_price: float | None = None,
) -> PurchasePlan:
    target_quantity = max(1, int(target_quantity or 1))
    max_unit_price = round(float(max_unit_price or 0), 2)
    if max_unit_price <= 0:
        raise ValueError("max_unit_price must be greater than 0")
    parsed_quotes = coerce_market_quotes(quotes)
    remaining = target_quantity
    actions: list[PurchasePlanAction] = []

    direct_candidates: list[tuple[float, str, int]] = []
    for quote in parsed_quotes:
        for price, quantity in quote.sell_orders:
            if price <= max_unit_price and quantity > 0:
                direct_candidates.append((price, quote.platform, quantity))
    direct_candidates.sort(key=lambda row: (row[0], row[1]))

    for price, platform, quantity in direct_candidates:
        if remaining <= 0:
            break
        take = min(remaining, quantity)
        actions.append(PurchasePlanAction("direct_buy", platform, round(price, 2), take, "sell_order_at_or_below_target"))
        remaining -= take

    direct_quantity = target_quantity - remaining
    if remaining <= 0:
        return PurchasePlan(
            item_id=int(item_id or 0),
            market_hash_name=str(market_hash_name or ""),
            target_quantity=target_quantity,
            max_unit_price=max_unit_price,
            direct_quantity=direct_quantity,
            order_quantity=0,
            actions=tuple(actions),
            remaining_quantity=0,
        )

    order_quotes = [
        quote
        for quote in parsed_quotes
        if (quote.target_order_price or default_order_price or quote.buy_max or 0) > 0
    ]
    order_quotes.sort(
        key=lambda quote: (
            -float(quote.weight or 1.0),
            float(quote.target_order_price or default_order_price or quote.buy_max or max_unit_price),
            quote.platform,
        )
    )
    if not order_quotes:
        return PurchasePlan(
            item_id=int(item_id or 0),
            market_hash_name=str(market_hash_name or ""),
            target_quantity=target_quantity,
            max_unit_price=max_unit_price,
            direct_quantity=direct_quantity,
            order_quantity=0,
            actions=tuple(actions),
            remaining_quantity=remaining,
        )

    total_weight = sum(max(0.01, float(quote.weight or 1.0)) for quote in order_quotes)
    allocations: list[tuple[PlatformMarketQuote, int]] = []
    allocated = 0
    for quote in order_quotes:
        share = max(1, int(round(remaining * max(0.01, float(quote.weight or 1.0)) / total_weight)))
        if quote.min_order_quantity > 0:
            share = max(share, quote.min_order_quantity)
        share = min(share, remaining - allocated) if allocated + share > remaining else share
        if share <= 0:
            continue
        allocations.append((quote, share))
        allocated += share
        if allocated >= remaining:
            break
    while allocated < remaining and allocations:
        quote, qty = allocations[0]
        allocations[0] = (quote, qty + 1)
        allocated += 1

    for quote, quantity in allocations:
        price = round(float(quote.target_order_price or default_order_price or quote.buy_max or max_unit_price), 2)
        price = min(price, max_unit_price)
        actions.append(PurchasePlanAction("purchase_order", quote.platform, price, quantity, "remaining_quantity_after_direct_buy"))

    return PurchasePlan(
        item_id=int(item_id or 0),
        market_hash_name=str(market_hash_name or ""),
        target_quantity=target_quantity,
        max_unit_price=max_unit_price,
        direct_quantity=direct_quantity,
        order_quantity=sum(qty for _, qty in allocations),
        actions=tuple(actions),
        remaining_quantity=max(0, remaining - sum(qty for _, qty in allocations)),
    )
