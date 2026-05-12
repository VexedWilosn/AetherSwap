from __future__ import annotations

from pathlib import Path


def test_radar_defaults_to_cashout_mode_and_cashout_sort():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert '<option value="profit_rate|desc" selected>' in html
    assert '<option value="cash_to_steam|desc">' in html
    assert '<option value="steam_to_cash|desc">' in html
    assert '<option value="steam_to_cash" selected>' in html
    assert 'data-radar-purpose-mode="steam_to_cash"' in html
    assert 'data-radar-purpose-mode="cash_to_steam"' in html
    assert 'data-radar-buy-mode="direct"' in html
    assert 'data-radar-buy-mode="order"' in html
    assert 'data-radar-sell-mode="bid"' in html
    assert 'data-radar-sell-mode="listing"' in html
    assert 'data-radar-mode-target="best"' not in html
    assert "const sortVal = sortSelect ? sortSelect.value : 'profit_rate|desc';" in html


def test_radar_has_cashout_price_mode_selector():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert 'id="radar-cashout-price-mode"' in html
    assert '<option value="bid" selected>' in html
    assert '<option value="listing">' in html
    assert "q.set('cashout_price_mode', cashoutPriceModeSelect.value || 'bid')" in html
    assert "q.set('buy_price_mode', radarBuyMode)" in html


def test_radar_platform_button_sends_direct_or_purchase_action():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert "const action = radarBuyMode === 'order' ? 'purchase_order' : 'direct_buy';" in html
    assert "JSON.stringify({ item_id: Number(itemId), platform, buy_price: buyPrice, quantity: 1, action })" in html
    assert "action: 'platform_order'" not in html


def test_radar_uuyp_open_platform_button_is_explicit_not_order_execution():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert "打开平台" in html
    assert "不会在 AetherSwap 内自动下单" in html
    assert "if (platform === 'uuyp')" in html


def test_signal_batch_direct_excludes_uuyp():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert "const DIRECT_BUY_PLATFORMS = new Set(['buff', 'eco']);" in html
    assert "return DIRECT_BUY_PLATFORMS.has(platform);" in html


def test_radar_add_monitor_has_multi_select_picker():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert 'id="radar-monitor-picker-modal"' in html
    assert 'class="radar-monitor-picker-check"' in html
    assert "openMonitorPicker(data.matches || [])" in html
    assert "JSON.stringify({ item_ids: itemIds })" in html


def test_radar_clear_filters_preserves_trade_mode_and_balance_discount():
    html = Path("web/index.html").read_text(encoding="utf-8")
    clear_block = html.split("if (clearFiltersBtn) clearFiltersBtn.addEventListener('click', () => {", 1)[1].split("if (addMonitorBtn)", 1)[0]

    assert "if (modeFilter) modeFilter.value = 'best';" not in clear_block
    assert "if (balanceDiscountInput) balanceDiscountInput.value = '70';" not in clear_block
    assert "syncRadarMethodControls();" in clear_block
    assert "if (onlyProfitableInput) onlyProfitableInput.checked = true;" in clear_block


def test_radar_has_visible_profit_goal_selector():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert 'data-radar-purpose-mode="steam_to_cash"' in html
    assert 'data-radar-purpose-mode="cash_to_steam"' in html
    assert "const purposeModeButtons = Array.from(document.querySelectorAll('[data-radar-purpose-mode]'));" in html
    assert "setRadarPurposeMode(btn.dataset.radarPurposeMode || 'steam_to_cash');" in html
    assert "if (sortSelect) sortSelect.value = radarModeCopy(targetMode).sort;" in html
    assert "sort: 'cash_to_steam|desc'" in html
    assert "sort: 'steam_to_cash|desc'" in html
    assert "sort: 'profit_rate|desc'" in html
    assert "setRadarPurposeMode(modeFilter ? (modeFilter.value || 'steam_to_cash') : 'steam_to_cash', false);" in html


def test_radar_balance_discount_is_shared_with_calculator():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert 'id="radar-balance-discount-rate"' in html
    assert 'value="70"' in html
    assert 'id="calc-balance-discount-rate"' not in html
    assert "window.AetherSteamBalanceCostRatio = getRadarSteamBalanceCostRatio();" in html
    assert "q.set('steam_balance_cost_ratio', getRadarSteamBalanceCostRatio().toFixed(4));" in html
    assert "if (window.AetherProfitCalculator) window.AetherProfitCalculator.update();" in html
    assert "saveRadarBalanceDiscountToConfig();" in html
    assert "steam_balance_cost_ratio: getRadarSteamBalanceCostRatio()" in html


def test_radar_no_visible_only_profitable_filter():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert "鍙湅鏈夋晥濂楀埄" not in html
    assert 'id="radar-only-profitable" type="checkbox" checked class="hidden"' in html


def test_main_js_supports_hash_deep_link_tabs():
    js = Path("web/js/main.js").read_text(encoding="utf-8")

    assert "function tabFromHash()" in js
    assert 'document.getElementById("panel-" + hash)' in js
    assert "const hashTab = tabFromHash();" in js
    assert "if (hashTab) tabSwitch(hashTab);" in js


def test_settings_has_cash_platform_matrix():
    html = Path("web/index.html").read_text(encoding="utf-8")
    js = Path("web/js/settings.js").read_text(encoding="utf-8")

    assert 'id="cfg-cash-platforms-body"' in html
    assert "function renderCashPlatformRows(platforms)" in js
    assert "function readCashPlatformRows()" in js
    assert "allow_purchase_order" in js
    assert "purchase_order_weight" in js


def test_settings_has_low_price_exposure_guard_controls():
    html = Path("web/index.html").read_text(encoding="utf-8")
    js = Path("web/js/settings.js").read_text(encoding="utf-8")

    for element_id in [
        "cfg-low-price-exposure-enabled",
        "cfg-low-price-exposure-rule",
        "cfg-low-price-exposure-hide-signals",
        "cfg-low-price-exposure-block-execution",
        "cfg-low-price-exposure-include-active-orders",
    ]:
        assert f'id="{element_id}"' in html

    assert "low_price_exposure_guard" in js
    assert "cfg-low-price-exposure-rule" in js
