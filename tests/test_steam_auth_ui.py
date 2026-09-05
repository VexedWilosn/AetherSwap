import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest


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


@pytest.fixture
def auth_page(browser):
    web = Path(__file__).resolve().parent.parent / "web"
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        url = urlparse(route.request.url)
        if url.hostname != "aetherswap.test":
            route.fulfill(body="", content_type="application/javascript")
            return
        path = (web / url.path.lstrip("/")).resolve() if url.path != "/" else web / "index.html"
        assert web in path.parents
        assert not url.path.startswith("/api/"), "UI tests must not reach a real API"
        if path.suffix == ".js":
            # Exercise the real views without bootstrapping unrelated workers/pollers.
            if path.name not in ("utils.js", "accounts.js", "main.js"):
                route.fulfill(body="", content_type="application/javascript")
                return
            source = path.read_text(encoding="utf-8")
            if path.name == "main.js":
                assert source.rstrip().endswith("init();")
                source = source.rstrip().removesuffix("init();")
            route.fulfill(body=source, content_type="application/javascript")
        elif path.is_file():
            route.fulfill(path=str(path), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
        else:
            route.fulfill(status=404, body="")

    page.route("**/*", local_only)
    page.goto("http://aetherswap.test/")
    page.evaluate("""() => {
      window.testAccount = {id: 'test-account', username: 'Test account', steam_id: '76561198000000000'};
      window.verifyCalls = 0;
      window.inventoryResponse = {items: [{name: 'Test item'}], source: 'cache', auth_status: 'auth_pending'};
      fetchJson = async (url) => {
        if (url.endsWith('/verify')) {
          window.verifyCalls++;
          return new Promise(resolve => { window.finishVerification = resolve; });
        }
        if (url.includes('/inventory')) return window.inventoryResponse;
        throw new Error('Unexpected mock request');
      };
      refreshAccounts = async () => {};
      _hasAnyAccount = true;
      window.showTestPanel = (name) => {
        document.querySelectorAll('.panel').forEach(panel => panel.classList.remove('active'));
        el('panel-' + name).classList.add('active');
        document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.tab === name));
        el('page-title-display').textContent = name === 'inventory' ? '库存管理' : '账号管理';
      };
      showTestPanel('accounts');
      renderAccountDetail(testAccount, testAccount.id);
    }""")
    yield page
    page.close()
    assert errors == []


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_pending_verification_and_inventory_sources_are_unambiguous(auth_page, tmp_path, width, height):
    page = auth_page
    page.set_viewport_size({"width": width, "height": height})
    page.locator("#btn-acc-verify").click()
    assert page.locator("#btn-acc-verify").is_disabled()
    page.evaluate("renderAccountDetail(testAccount, testAccount.id)")
    assert page.locator("#btn-acc-verify").is_disabled()
    assert page.evaluate("verifyCalls") == 1
    page.evaluate("finishVerification({ok: false, status: 'auth_pending', message: '本次已停止等待，请稍后重试'})")
    page.wait_for_function("!document.getElementById('btn-acc-verify').disabled")
    assert page.locator(".toast .t").last.inner_text() == "等待登录凭证超时"
    assert "验证未通过" not in page.locator("#toast-host").inner_text()
    assert "验证通过" not in page.locator("#toast-host").inner_text()
    page.evaluate("showTestPanel('inventory')")
    page.evaluate("refreshInventory(true)")
    assert page.locator("#inv-data-status").inner_text() == "缓存库存 · 登录凭证等待超时"
    assert page.locator("#inv-table tbody tr").count() == 1
    assert page.locator("#relogin-overlay").is_hidden()
    toolbar = page.locator("#panel-inventory .panel-toolbar")
    assert toolbar.evaluate("node => node.scrollWidth <= node.clientWidth")
    status_box = page.locator("#inv-data-status").bounding_box()
    refresh_box = page.locator("#btn-refresh-inventory").bounding_box()
    assert status_box["y"] >= refresh_box["y"] + refresh_box["height"]
    page.screenshot(path=str(tmp_path / f"inventory-auth-{width}.png"), full_page=True, animations="disabled")
    page.evaluate("inventoryResponse = {items: [{name: 'Fresh item'}], source: 'live'}")
    page.evaluate("refreshInventory(true)")
    assert page.locator("#inv-data-status").inner_text() == "实时库存"
    assert "Fresh item" in page.locator("#inv-table").inner_text()
    assert "验证通过" not in page.locator("#toast-host").inner_text()


def test_reused_session_has_distinct_verification_message(auth_page):
    page = auth_page
    page.locator("#btn-acc-verify").click()
    page.evaluate("finishVerification({ok: true, status: 'session_valid', message: '当前 Steam 会话有效'})")
    page.wait_for_function("!document.getElementById('btn-acc-verify').disabled")
    assert page.locator(".toast .t").last.inner_text() == "当前登录有效"
