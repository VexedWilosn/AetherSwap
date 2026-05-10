from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_STEAM_BALANCE_COST_RATIO = 0.85
STEAM_PLATFORM_FEE_RATE = 0.05
STEAM_PUBLISHER_FEE_RATE = 0.10
STEAM_MIN_FEE = 0.01


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp_ratio(value: Any, default: float = DEFAULT_STEAM_BALANCE_COST_RATIO) -> float:
    ratio = safe_float(value, default)
    if ratio <= 0:
        return default
    return max(0.01, min(ratio, 1.0))


def steam_balance_cost_ratio(config: dict[str, Any] | None = None) -> float:
    pipeline = (config or {}).get("pipeline") if isinstance(config, dict) else {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    return clamp_ratio(
        pipeline.get("steam_balance_cost_ratio", pipeline.get("resell_ratio")),
        DEFAULT_STEAM_BALANCE_COST_RATIO,
    )


def steam_sale_net_price(price: float) -> float:
    gross = safe_float(price)
    if gross <= 0:
        return 0.0
    steam_fee = max(gross * STEAM_PLATFORM_FEE_RATE, STEAM_MIN_FEE)
    publisher_fee = max(gross * STEAM_PUBLISHER_FEE_RATE, STEAM_MIN_FEE)
    return max(0.0, gross - steam_fee - publisher_fee)


def steam_sale_gross_price_from_net(net_price: float) -> float:
    net = safe_float(net_price)
    if net <= 0:
        return 0.0
    candidates = [
        net + (STEAM_MIN_FEE * 2),
        (net + STEAM_MIN_FEE) / (1.0 - STEAM_PUBLISHER_FEE_RATE),
        net / (1.0 - STEAM_PLATFORM_FEE_RATE - STEAM_PUBLISHER_FEE_RATE),
    ]
    return min(candidates, key=lambda gross: abs(steam_sale_net_price(gross) - net))


def get_after_tax_price(price: float, platform: str) -> float:
    key = (platform or "").lower().strip()
    if key == "steam":
        return steam_sale_net_price(price)
    if key == "buff":
        return float(price) * 0.975
    if key in {"uuyp", "eco", "uusell", "ecosteam"}:
        return float(price) * 0.98
    return float(price) * 0.95


@dataclass(frozen=True)
class ProfitMath:
    profit_cny: float
    profit_rate: float
    cost_cny: float
    revenue_cny: float


@dataclass(frozen=True)
class ProfitSignal:
    best_rate: float
    best_direction: str
    cash_to_steam_rate: float
    steam_to_cash_rate: float
    cash_to_steam_profit_cny: float
    steam_to_cash_profit_cny: float
    cash_to_steam_platform: str | None = None
    steam_to_cash_platform: str | None = None
    cash_to_steam_price: float = 0.0
    steam_to_cash_price: float = 0.0


def cash_to_steam_profit(platform_cash_price: float, steam_sell_price: float) -> ProfitMath:
    """Cash platform buy -> Steam sale, measured as Steam balance growth."""

    cost = safe_float(platform_cash_price)
    steam_revenue = get_after_tax_price(safe_float(steam_sell_price), "steam")
    profit = steam_revenue - cost
    rate = profit / cost if cost > 0 else 0.0
    return ProfitMath(profit_cny=profit, profit_rate=rate, cost_cny=cost, revenue_cny=steam_revenue)


def steam_to_cash_profit(steam_buy_price: float, platform_cash_buy_price: float, sell_platform: str, balance_cost_ratio: float) -> ProfitMath:
    """Steam buy -> cash platform sale, measured against discounted Steam balance cost."""

    ratio = clamp_ratio(balance_cost_ratio)
    cost = safe_float(steam_buy_price) * ratio
    cash_revenue = get_after_tax_price(safe_float(platform_cash_buy_price), sell_platform)
    profit = cash_revenue - cost
    rate = profit / cost if cost > 0 else 0.0
    return ProfitMath(profit_cny=profit, profit_rate=rate, cost_cny=cost, revenue_cny=cash_revenue)


def opportunity_profit(
    *,
    buy_platform: str,
    sell_platform: str,
    buy_price: float,
    sell_price: float,
    balance_cost_ratio: float,
) -> ProfitMath:
    buy_key = (buy_platform or "").lower().strip()
    sell_key = (sell_platform or "").lower().strip()
    if buy_key == "steam":
        return steam_to_cash_profit(buy_price, sell_price, sell_key, balance_cost_ratio)
    if sell_key == "steam":
        return cash_to_steam_profit(buy_price, sell_price)
    cost = safe_float(buy_price)
    revenue = get_after_tax_price(safe_float(sell_price), sell_key)
    profit = revenue - cost
    return ProfitMath(profit_cny=profit, profit_rate=(profit / cost if cost > 0 else 0.0), cost_cny=cost, revenue_cny=revenue)


def lowest_nonzero_cashout_bid(candidates: list[tuple[str, float, ProfitMath]]) -> tuple[str, float, ProfitMath] | None:
    nonzero = [item for item in candidates if safe_float(item[1]) > 0]
    if not nonzero:
        return None
    return min(nonzero, key=lambda item: item[1])


def best_profit_signal_from_prices(prices: list[Any], config: dict[str, Any] | None = None) -> ProfitSignal:
    steam = next((p for p in prices if str(getattr(p, "platform_name", "") or "").lower().strip() == "steam"), None)
    steam_sell = safe_float(getattr(steam, "sell_min", None)) if steam is not None else 0.0
    balance_ratio = steam_balance_cost_ratio(config)

    best_cash_math: ProfitMath | None = None
    best_cash_platform: str | None = None
    best_cash_price = 0.0
    best_steam_math: ProfitMath | None = None
    best_steam_platform: str | None = None
    best_steam_price = 0.0
    cashout_candidates: list[tuple[str, float, ProfitMath]] = []

    for row in prices:
        platform = str(getattr(row, "platform_name", "") or "").lower().strip()
        if not platform or platform == "steam":
            continue
        sell_min = safe_float(getattr(row, "sell_min", None))
        buy_max = safe_float(getattr(row, "buy_max", None))
        if sell_min > 0 and steam_sell > 0:
            math = cash_to_steam_profit(sell_min, steam_sell)
            if best_cash_math is None or math.profit_rate > best_cash_math.profit_rate:
                best_cash_math = math
                best_cash_platform = platform
                best_cash_price = sell_min
        if buy_max > 0 and steam_sell > 0:
            math = steam_to_cash_profit(steam_sell, buy_max, platform, balance_ratio)
            cashout_candidates.append((platform, buy_max, math))

    selected_cashout = lowest_nonzero_cashout_bid(cashout_candidates)
    if selected_cashout:
        best_steam_platform, best_steam_price, best_steam_math = selected_cashout

    cash_rate = best_cash_math.profit_rate if best_cash_math else 0.0
    steam_rate = best_steam_math.profit_rate if best_steam_math else 0.0
    best_direction = "steam_to_cash" if steam_rate > cash_rate else "cash_to_steam"
    return ProfitSignal(
        best_rate=max(cash_rate, steam_rate),
        best_direction=best_direction,
        cash_to_steam_rate=cash_rate,
        steam_to_cash_rate=steam_rate,
        cash_to_steam_profit_cny=best_cash_math.profit_cny if best_cash_math else 0.0,
        steam_to_cash_profit_cny=best_steam_math.profit_cny if best_steam_math else 0.0,
        cash_to_steam_platform=best_cash_platform,
        steam_to_cash_platform=best_steam_platform,
        cash_to_steam_price=best_cash_price,
        steam_to_cash_price=best_steam_price,
    )
