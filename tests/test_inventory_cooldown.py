from pathlib import Path

import pytest

from app.inventory_cs2 import _parse_cooldown, _safe_iso


@pytest.mark.parametrize("descriptions,expected", [
    ([{"value": "This item is trade-protected until [date]1789106400[/date]."}], 1789106400),
    ([{"value": "This item is trade-protected until Sep 11, 2026 (6:00:00) GMT."}], 1789106400),
    ([], 0),
    (None, 0),
    ([{"value": "This item is trade-protected."}], 0),
    ([{"value": "This item is trade-protected until [date]bad[/date]."}], 0),
])
def test_backend_cooldown_formats(descriptions, expected):
    text, timestamp = _parse_cooldown(descriptions)
    assert timestamp == expected
    assert text == (descriptions[0]["value"] if descriptions else "")
    assert _safe_iso(timestamp) == ("2026-09-11T06:00:00Z" if expected else None)


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium is not installed")
            raise
        yield browser
        browser.close()


@pytest.mark.parametrize("timezone,expected,early", [
    ("Asia/Shanghai", "2026/09/11 14:00:00", "2026/01/02 03:04:05"),
    ("UTC", "2026/09/11 06:00:00", "2026/01/01 19:04:05"),
    ("America/Los_Angeles", "2026/09/10 23:00:00", "2026/01/01 11:04:05"),
])
@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_inventory_render_uses_local_time_and_preserves_description(
    browser, timezone, expected, early, width, height,
):
    web = Path(__file__).resolve().parent.parent / "web"
    source = (web / "js" / "main.js").read_text(encoding="utf-8").rstrip()
    assert source.endswith("init();")
    source = source.removesuffix("init();")
    description = "This item is trade-protected until [date]1789106400[/date]."
    cases = [
        {"cooldown_at_iso": "2026-09-11T06:00:00Z"},
        {"cooldown_at": 1789106400},
        {"cooldown_at": "1789106400"},
        {"cooldown_at": None, "cooldown_at_iso": None, "cooldown_text": description},
        {"cooldown_at_iso": "2026-01-01T19:04:05Z"},
        {},
        {"cooldown_at_iso": "not-a-date"},
        {"cooldown_at": -1},
        {"cooldown_text": "This item is trade-protected."},
        {"cooldown_text": "This item is trade-protected until [date]bad[/date]."},
        {"cooldown_at": 0},
        {"cooldown_at": "NaN"},
        {"cooldown_text": "<script>throw new Error('unsafe')</script>"},
    ]
    context = browser.new_context(
        timezone_id=timezone, viewport={"width": width, "height": height},
    )
    context.route("**/*", lambda route: route.abort())
    try:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content('<div id="inv-data-status"></div><table id="inv-table"><tbody></tbody></table>')
        page.add_script_tag(content=(web / "js" / "utils.js").read_text(encoding="utf-8"))
        page.add_script_tag(content=source)
        page.evaluate("""items => {
          fetchJson = async () => ({items: items.map((item, i) => ({...item, name: `Item ${i}`})), source: 'cache'});
          return refreshInventory(false);
        }""", cases)
        rows = page.locator("#inv-table tbody tr")
        assert rows.count() == len(cases)
        displayed = rows.evaluate_all("rows => rows.map(row => row.cells[4].textContent)")
        assert displayed == [expected] * 4 + [early] + ["\u2014"] * 8
        assert rows.nth(3).locator("td").nth(6).inner_text() == description
        assert rows.nth(12).locator("td").nth(6).inner_text() == cases[12]["cooldown_text"]
        assert errors == []
    finally:
        context.close()
