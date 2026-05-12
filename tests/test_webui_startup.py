from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


def test_lifespan_does_not_autostart_workers_by_default(monkeypatch):
    import app.api as api

    calls: list[str] = []

    class FakeRuntime:
        def __init__(self, name: str):
            self.name = name

        def start_from_config(self):
            calls.append(f"{self.name}:start")
            return True

        def stop(self, timeout_seconds: float = 5.0):
            calls.append(f"{self.name}:stop")
            return True

    monkeypatch.setattr(api, "load_app_config", lambda: {"automation_modules": {"auto_trading_enabled": True}})
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeRuntime("worker"))
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeRuntime("scanner"))

    async def run_lifespan():
        async with api._lifespan(api.app):
            pass

    import asyncio

    asyncio.run(run_lifespan())

    assert "worker:start" not in calls
    assert "scanner:start" not in calls
    assert "worker:stop" in calls
    assert "scanner:stop" in calls


def test_lifespan_can_autostart_workers_when_explicitly_enabled(monkeypatch):
    import app.api as api

    calls: list[str] = []

    class FakeRuntime:
        def __init__(self, name: str):
            self.name = name

        def start_from_config(self):
            calls.append(f"{self.name}:start")
            return True

        def stop(self, timeout_seconds: float = 5.0):
            calls.append(f"{self.name}:stop")
            return True

    monkeypatch.setattr(
        api,
        "load_app_config",
        lambda: {"automation_modules": {"auto_trading_enabled": True, "autostart_on_webui_boot": True}},
    )
    monkeypatch.setattr(api, "PLATFORM_ACTION_WORKER_RUNTIME", FakeRuntime("worker"))
    monkeypatch.setattr(api, "SELLER_SNAPSHOT_SCANNER_RUNTIME", FakeRuntime("scanner"))

    async def run_lifespan():
        async with api._lifespan(api.app):
            pass

    import asyncio

    asyncio.run(run_lifespan())

    assert "worker:start" in calls
    assert "scanner:start" in calls


def test_inventory_cached_only_does_not_scan_steam(monkeypatch):
    import app.routes.inventory as inventory
    import app.api as api

    calls: list[str] = []
    monkeypatch.setattr(inventory, "get_inventory", lambda: [{"name": "cached item"}])
    monkeypatch.setattr(inventory, "scan_cs2_inventory", lambda: calls.append("scan") or (True, [], ""))

    client = TestClient(api.app)
    resp = client.get("/api/inventory?cached_only=1")

    assert resp.status_code == 200
    assert resp.json() == {"items": [{"name": "cached item"}]}
    assert calls == []


def test_engine_log_api_tails_large_file_without_full_read(monkeypatch, tmp_path):
    import app.api as api

    log_path = tmp_path / "aetherswap_engine.log"
    with log_path.open("wb") as fh:
        for i in range(12000):
            fh.write(f"line {i}\n".encode("utf-8"))
    monkeypatch.setattr(api, "LOG_PATH", log_path)

    rows = api._read_engine_log_lines(0, 5)

    assert [row["msg"] for row in rows] == ["line 11995", "line 11996", "line 11997", "line 11998", "line 11999"]
    assert api._read_engine_log_lines(rows[-1]["id"], 5) == []


def test_realtime_signals_has_condition_selection_controls():
    html = Path("web/index.html").read_text(encoding="utf-8-sig")
    soup = BeautifulSoup(html, "html.parser")

    for element_id in [
        "signals-cond-price-op",
        "signals-cond-price-value",
        "signals-cond-rate-op",
        "signals-cond-rate-value",
        "signals-cond-profit-op",
        "signals-cond-profit-value",
        "btn-signals-select-by-condition",
        "btn-signals-clear-conditions",
        "signals-condition-result",
    ]:
        assert soup.find(id=element_id) is not None

    sort_values = {option.get("value") for option in soup.select("#signals-sort option")}
    assert "profit_cny_desc" in sort_values
    assert "profit_cny_asc" in sort_values
    assert "净利润" in soup.get_text()
