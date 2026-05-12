import threading
from unittest.mock import patch
import requests


def _build_manager(entries: list):
    from utils.proxy_manager import ProxyManager

    with patch.object(ProxyManager, "_reload", return_value=None):
        mgr = ProxyManager.__new__(ProxyManager)
        mgr._lock = threading.Lock()
        mgr._warming_up = False
        mgr._proxies = entries
        mgr._cached_strategy = 2
        mgr._cached_enabled = True
        mgr._proxy_configs = []
        mgr._proxy_weights = []
        mgr._proxy_group_configs = {}
        mgr._proxy_group_weights = {}
        mgr._sync_cycle()
    return mgr


def test_platform_proxy_prefers_dedicated_pool_and_falls_back_to_global():
    mgr = _build_manager(
        [
            {"config": {"host": "global", "port": 80}, "pool": "global", "score": 10, "cooldown_until": 0, "health": {}},
            {"config": {"host": "steam", "port": 81}, "pool": "steam", "score": 10, "cooldown_until": 0, "health": {}},
        ]
    )

    assert "steam:81" in mgr.get_next_proxy_dict(platform="steam")["http"]

    mgr.mark_proxy_failure("http://steam:81/", reason="blocked", cooldown_seconds=60)

    assert "global:80" in mgr.get_next_proxy_dict(platform="steam")["http"]


def test_proxy_health_keeps_same_gateway_different_credentials_independent():
    mgr = _build_manager(
        [
            {
                "config": {"host": "gateway.example", "port": 22225, "username": "zone-steam", "password": "pw1"},
                "pool": "steam",
                "score": 10,
                "cooldown_until": 0,
                "health": {},
            },
            {
                "config": {"host": "gateway.example", "port": 22225, "username": "zone-buff", "password": "pw2"},
                "pool": "buff",
                "score": 10,
                "cooldown_until": 0,
                "health": {},
            },
            {"config": {"host": "global", "port": 80}, "pool": "global", "score": 10, "cooldown_until": 0, "health": {}},
        ]
    )

    mgr.mark_proxy_failure("http://zone-steam:pw1@gateway.example:22225/", reason="blocked", cooldown_seconds=60)

    assert "zone-buff:pw2@gateway.example:22225" in mgr.get_next_proxy_dict(platform="buff")["http"]
    assert "global:80" in mgr.get_next_proxy_dict(platform="steam")["http"]


def test_proxy_reload_accepts_global_and_platform_string_pools(monkeypatch):
    from utils import proxy_manager
    from utils.proxy_manager import ProxyManager

    monkeypatch.setattr(
        proxy_manager,
        "_load_proxy_pool_cfg",
        lambda: {
            "enabled": True,
            "strategy": 2,
            "global_proxies": ["global:80:user:pass"],
            "steam_proxies": ["http://steam-user:steam-pass@steam:81/"],
            "buff_proxies": [],
            "uuyp_proxies": [],
        },
    )
    monkeypatch.setattr(ProxyManager, "_load_health_state", lambda self: {})

    mgr = ProxyManager()

    assert len(mgr._proxies) == 2
    assert mgr.get_next_proxy_dict(platform="steam")["http"].startswith("http://steam-user:steam-pass@steam:81")
    assert mgr.get_next_proxy_dict(platform="buff")["http"].startswith("http://user:pass@global:80")


def test_clear_proxy_config_removes_dedicated_pools(monkeypatch):
    from app.routes import proxy as proxy_route

    saved = {}
    config = {
        "proxy_pool": {
            "proxies": [{"host": "global", "port": 80, "username": "", "password": ""}],
            "global_proxies": ["global:80"],
            "steam_proxies": ["steam:81"],
            "buff_proxies": ["buff:82"],
            "uuyp_proxies": ["uuyp:83"],
        }
    }

    monkeypatch.setattr(proxy_route, "load_app_config_validated", lambda: config)
    monkeypatch.setattr(proxy_route, "save_app_config_validated", lambda cfg: saved.update(cfg))
    monkeypatch.setattr("utils.proxy_manager.get_proxy_manager", lambda: None)

    response = proxy_route.api_clear_proxies()

    assert response["ok"] is True
    pool = saved["proxy_pool"]
    assert pool["proxies"] == []
    assert pool["global_proxies"] == []
    assert pool["steam_proxies"] == []
    assert pool["buff_proxies"] == []
    assert pool["uuyp_proxies"] == []


def test_bright_data_proxy_reports_target_failure_separately(monkeypatch):
    from utils import proxy_manager

    class Response:
        def __init__(self, status_code=200, text="ok"):
            self.status_code = status_code
            self.text = text

    def fake_get(url, **kwargs):
        if url == proxy_manager.BRIGHT_DATA_TEST_URL:
            return Response(200, "bright ok")
        if url == "https://steamcommunity.com/market/":
            raise requests.exceptions.ConnectionError("connection reset")
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(proxy_manager.requests, "get", fake_get)

    result = proxy_manager.test_one_proxy(
        {
            "host": "brd.superproxy.io",
            "port": 33335,
            "username": "zone-steam",
            "password": "secret",
        },
        "https://steamcommunity.com/market/",
        3,
    )

    assert result["proxy_status"] == "ok"
    assert result["target_status"] == "failed"
    assert result["status"] == "target_failed"
    assert "connection reset" in result["target_error"]
