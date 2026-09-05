import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes import inventory


@pytest.fixture
def inventory_env(monkeypatch):
    cached = [{"name": "cached item"}]
    scan = Mock(return_value=(False, [], "Steam 登录已过期"))
    sell = Mock()
    monkeypatch.setattr(inventory, "get_inventory", lambda: cached)
    monkeypatch.setattr(inventory, "set_inventory", lambda items: cached.__setitem__(slice(None), items))
    monkeypatch.setattr(inventory, "is_steam_background_allowed", lambda: True)
    monkeypatch.setattr(inventory, "scan_cs2_inventory", scan)
    monkeypatch.setattr(inventory, "_enrich_inventory_with_steam_prices", lambda *_args: None)
    monkeypatch.setattr(inventory, "run_sell_phase_on_inventory_update", sell)
    monkeypatch.setattr(inventory, "log", lambda *_args, **_kw: None)
    return cached, scan, sell


@pytest.mark.parametrize("status", ["auth_pending", "busy", "network_error"])
def test_pending_login_returns_explicit_cache_without_expired_login_popup(monkeypatch, inventory_env, status):
    cached, scan, sell = inventory_env
    monkeypatch.setattr(inventory, "_try_steam_auto_relogin", lambda: (False, status, "pending"))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = inventory.api_inventory(refresh=True)

    assert result["items"] == cached
    assert result["source"] == "cache"
    assert result["auth_status"] == status
    assert not result.get("auth_expired")
    assert scan.call_count == 1
    sell.assert_not_called()


def test_cache_read_is_not_reported_as_a_successful_refresh(inventory_env):
    _, scan, sell = inventory_env

    result = inventory.api_inventory(refresh=False)

    assert result["source"] == "cache"
    scan.assert_not_called()
    sell.assert_not_called()


def test_successful_inventory_read_is_live(inventory_env):
    _, scan, sell = inventory_env
    scan.return_value = (True, [{"name": "fresh item"}], None)

    result = inventory.api_inventory(refresh=True)

    assert result["source"] == "live"
    assert result["items"] == [{"name": "fresh item"}]
    sell.assert_called_once()


def test_inventory_after_shared_login_success_is_live(monkeypatch, inventory_env):
    _, scan, sell = inventory_env
    scan.side_effect = [(False, [], "Steam 登录已过期"), (True, [{"name": "fresh item"}], None)]
    monkeypatch.setattr(inventory, "_try_steam_auto_relogin", lambda: (True, "auto_ok", "ready"))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = inventory.api_inventory(refresh=True)

    assert result["source"] == "live"
    assert scan.call_count == 2
    sell.assert_called_once()


def test_real_credential_error_still_requests_manual_intervention(monkeypatch, inventory_env):
    _, _, sell = inventory_env
    monkeypatch.setattr(inventory, "_try_steam_auto_relogin", lambda: (False, "need_2fa", "guard required"))

    result = inventory.api_inventory(refresh=True)

    assert result["auth_expired"] is True
    assert result["auth_expired_reason"] == "need_2fa"
    assert result["source"] == "cache"
    sell.assert_not_called()
