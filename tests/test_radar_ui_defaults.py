from __future__ import annotations

from pathlib import Path


def test_radar_defaults_to_cashout_mode_and_cashout_sort():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert '<option value="profit_rate|desc" selected>' in html
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
    assert "if (balanceDiscountInput) balanceDiscountInput.value = '85';" not in clear_block
    assert "syncRadarMethodControls();" in clear_block
    assert "if (onlyProfitableInput) onlyProfitableInput.checked = true;" in clear_block


def test_radar_has_visible_profit_goal_selector():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert '<span class="radar-choice-label">目标</span>' in html
    assert 'data-radar-purpose-mode="steam_to_cash">套现收益</button>' in html
    assert 'data-radar-purpose-mode="cash_to_steam">余额增益</button>' in html
    assert "const purposeModeButtons = Array.from(document.querySelectorAll('[data-radar-purpose-mode]'));" in html
    assert "setRadarPurposeMode(btn.dataset.radarPurposeMode || 'steam_to_cash');" in html
    assert "if (sortSelect) sortSelect.value = radarModeCopy(targetMode).sort;" in html
    assert "if (mode === 'steam_to_cash') return { label: '套现收益', action: 'Steam买入', sort: 'profit_rate|desc' };" in html
    assert "setRadarPurposeMode(modeFilter ? (modeFilter.value || 'steam_to_cash') : 'steam_to_cash', false);" in html


def test_radar_no_visible_only_profitable_filter():
    html = Path("web/index.html").read_text(encoding="utf-8")

    assert "只看有效套利" not in html
    assert 'id="radar-only-profitable" type="checkbox" checked class="hidden"' in html


def test_main_js_supports_hash_deep_link_tabs():
    js = Path("web/js/main.js").read_text(encoding="utf-8")

    assert "function tabFromHash()" in js
    assert 'document.getElementById("panel-" + hash)' in js
    assert "const hashTab = tabFromHash();" in js
    assert "if (hashTab) tabSwitch(hashTab);" in js
