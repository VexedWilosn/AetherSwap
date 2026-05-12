from __future__ import annotations

from dataclasses import dataclass

from DataEngine.profit_model import (
    best_profit_signal_from_prices,
    get_after_tax_price,
    steam_sale_gross_price_from_net,
    steam_sale_net_price,
)


@dataclass
class PriceRow:
    platform_name: str
    sell_min: float
    buy_max: float


def test_steam_sale_net_price_applies_minimum_fee_components():
    assert steam_sale_net_price(0.02) == 0.0
    assert round(steam_sale_net_price(0.03), 2) == 0.01
    assert round(get_after_tax_price(0.10, "steam"), 2) == 0.08


def test_steam_sale_net_price_keeps_percent_fee_for_normal_prices():
    assert round(get_after_tax_price(100.0, "steam"), 2) == 85.0


def test_steam_sale_gross_price_from_net_handles_minimum_fee_breakpoints():
    assert round(steam_sale_gross_price_from_net(0.01), 2) == 0.03
    assert round(steam_sale_net_price(steam_sale_gross_price_from_net(85.0)), 2) == 85.0


def test_best_profit_signal_cashout_uses_lowest_valid_cash_platform_bid():
    prices = [
        PriceRow("steam", 100.0, 95.0),
        PriceRow("buff", 92.0, 82.0),
        PriceRow("uuyp", 91.0, 95.0),
    ]

    signal = best_profit_signal_from_prices(prices, {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    assert signal.steam_to_cash_platform == "buff"
    assert signal.steam_to_cash_price == 82.0
    assert round(signal.steam_to_cash_rate, 4) == round(((82.0 * 0.975 - 100.0 * 0.75) / (100.0 * 0.75)), 4)


def test_best_profit_signal_cashout_ignores_zero_bid_then_uses_lowest_nonzero_bid():
    prices = [
        PriceRow("steam", 100.0, 95.0),
        PriceRow("buff", 92.0, 0.0),
        PriceRow("uuyp", 91.0, 95.0),
        PriceRow("eco", 93.0, 82.0),
    ]

    signal = best_profit_signal_from_prices(prices, {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    assert signal.steam_to_cash_platform == "eco"
    assert signal.steam_to_cash_price == 82.0
