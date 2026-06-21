import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_detect_currency_supports_steam_symbol_prefixes():
    from steam.client import detect_currency

    assert detect_currency("\u20b9 100") == "INR"
    assert detect_currency("\u20bd 100") == "RUB"
    assert detect_currency("\u20ac 100") == "EUR"
    assert detect_currency("\u20ba 100") == "TRY"
    assert detect_currency("HK$ 100") == "HKD"
    assert detect_currency("R$ 100") == "BRL"
    assert detect_currency("CL$ 100") == "CLP"
    assert detect_currency("US$ 100") == "USD"
    assert detect_currency("CNY 100") == "CNY"
    assert detect_currency("\uffe5 100") == "CNY"


def test_apply_currency_uses_rate_map_for_non_usd_prices():
    from utils.money import apply_currency

    converted, currency = apply_currency([100.0, 125.5], "INR", rate_map={"INR": 0.07})

    assert currency == "CNY"
    assert converted == pytest.approx([7.0, 8.785])


def test_stability_currency_conversion_uses_exchange_rate_map(monkeypatch):
    import utils.money
    from analysis import stability

    monkeypatch.setattr(utils.money, "_load_exchange_rates", lambda: {"JPY": 0.05})

    converted, currency = stability._apply_currency([1000.0], "JPY", 7.2)

    assert currency == "CNY"
    assert converted == [50.0]
