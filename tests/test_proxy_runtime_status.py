import threading
from unittest.mock import patch


def test_proxy_manager_status_snapshot_reports_runtime_nodes():
    from utils.proxy_manager import ProxyManager

    with patch.object(ProxyManager, "_reload", return_value=None):
        mgr = ProxyManager.__new__(ProxyManager)
        mgr._lock = threading.Lock()
        mgr._warming_up = False
        mgr._cached_strategy = 2
        mgr._cached_enabled = True
        mgr._proxy_configs = []
        mgr._proxy_weights = []
        mgr._proxies = [
            {"config": {"host": "active", "port": 80}, "score": 10, "cooldown_until": 0, "failures": 0, "successes": 2, "health": {}},
            {"config": {"host": "down", "port": 81}, "score": 0, "cooldown_until": 0, "failures": 1, "successes": 0, "health": {"last_failure_reason": "proxy_auth"}},
        ]

    snapshot = mgr.status_snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["total"] == 2
    assert snapshot["active"] == 1
    assert snapshot["unavailable"] == 1
    assert snapshot["nodes"][0]["state"] == "active"
    assert snapshot["nodes"][1]["state"] == "unavailable"
