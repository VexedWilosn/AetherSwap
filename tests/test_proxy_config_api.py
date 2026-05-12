from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.proxy import router


def test_proxy_clear_api_removes_all_proxy_pools(monkeypatch):
    import app.routes.proxy as proxy_route

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    config = {
        "proxy_pool": {
            "enabled": True,
            "strategy": 2,
            "test_url": "https://ipv4.webshare.io/",
            "timeout_seconds": 10,
            "webshare_api_key": "",
            "proxies": [{"host": "global", "port": 80, "username": "", "password": ""}],
            "global_proxies": ["global:80"],
            "steam_proxies": ["steam:81"],
            "buff_proxies": ["buff:82"],
            "uuyp_proxies": ["uuyp:83"],
        }
    }
    saved = {}

    monkeypatch.setattr(proxy_route, "load_app_config_validated", lambda: config)
    monkeypatch.setattr(proxy_route, "save_app_config_validated", lambda cfg: saved.update(cfg))
    monkeypatch.setattr("utils.proxy_manager.get_proxy_manager", lambda: None)

    response = client.post("/api/proxy/clear")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    pool = saved["proxy_pool"]
    assert pool["proxies"] == []
    assert pool["global_proxies"] == []
    assert pool["steam_proxies"] == []
    assert pool["buff_proxies"] == []
    assert pool["uuyp_proxies"] == []


def test_proxy_config_api_backfills_global_proxies_for_legacy_config(monkeypatch):
    import app.routes.proxy as proxy_route

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(
        proxy_route,
        "load_app_config_validated",
        lambda: {
            "proxy_pool": {
                "enabled": True,
                "strategy": 2,
                "proxies": [{"host": "legacy", "port": 80, "username": "u", "password": "p"}],
                "global_proxies": [],
            }
        },
    )

    response = client.get("/api/proxy/config")

    assert response.status_code == 200
    assert response.json()["proxy_pool"]["global_proxies"] == ["legacy:80:u:p"]


def test_proxy_test_api_includes_platform_specific_pools(monkeypatch):
    import app.routes.proxy as proxy_route

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    monkeypatch.setattr(
        proxy_route,
        "load_app_config_validated",
        lambda: {
            "proxy_pool": {
                "test_url": "https://example.test/",
                "timeout_seconds": 10,
                "proxies": [{"host": "global", "port": 80, "username": "", "password": ""}],
                "steam_proxies": ["steam:81:u:p"],
                "buff_proxies": ["buff:82"],
                "uuyp_proxies": ["uuyp:83"],
            }
        },
    )

    def fake_test_one_proxy(proxy_cfg, test_url, timeout):
        return {
            "host": proxy_cfg["host"],
            "port": proxy_cfg["port"],
            "username": proxy_cfg.get("username", ""),
            "status": "ok",
            "ip_detected": "127.0.0.1",
            "latency_ms": 1,
            "error": None,
        }

    monkeypatch.setattr("utils.proxy_manager.test_one_proxy", fake_test_one_proxy)

    response = client.post("/api/proxy/test")

    assert response.status_code == 200
    results = response.json()["results"]
    by_pool = {row["pool"]: row for row in results}
    assert set(by_pool) == {"global", "steam", "buff", "uuyp"}
    assert by_pool["global"]["test_url"] == "https://example.test/"
    assert by_pool["steam"]["test_url"] == "https://steamcommunity.com/market/"
    assert by_pool["buff"]["test_url"] == "https://buff.163.com/"
    assert by_pool["uuyp"]["test_url"] == "https://www.youpin898.com/"
