from app.notify import compute_holdings_stats


def test_total_market_change_only_counts_items_with_both_market_prices():
    holdings = [
        {"price": 70, "market_price": 100, "current_market_price": 90},
        {"price": 120, "market_price": 200},
        {"price": 30, "current_market_price": 45},
    ]

    total_price, total_mp, total_cmp, pl, pl_pct, ratio = compute_holdings_stats(holdings)

    assert total_price == 220
    assert total_mp == 300
    assert total_cmp == 135
    assert pl == -10
    assert pl_pct == -10
    assert ratio == 0.85
