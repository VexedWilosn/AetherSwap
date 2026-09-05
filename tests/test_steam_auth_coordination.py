import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import steam_auth


@pytest.fixture
def auth_env(monkeypatch):
    account = {"id": "account-1", "username": "user", "password": "password",
               "steam_id": "76561198000000000"}
    saved = {"cookies": "steamLoginSecure=76561198000000000%7C%7Ctest-token; sessionid=test"}
    login = Mock(return_value=(False, "auth_pending: pending", {}))
    writes = Mock()
    monkeypatch.setattr(steam_auth, "get_account", lambda _id: dict(account, id=_id))
    monkeypatch.setattr(steam_auth, "get_current_account", lambda: account)
    monkeypatch.setattr(steam_auth, "set_current", lambda _id: True)
    monkeypatch.setattr(steam_auth, "get_steam_credentials", lambda: saved)
    monkeypatch.setattr(steam_auth, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: False)
    monkeypatch.setattr(steam_auth, "_do_steampy_login", login)
    monkeypatch.setattr(steam_auth, "update_steam_creds", writes)
    monkeypatch.setattr(steam_auth, "fetch_steam_profile_via_api", lambda *_args: ("", ""))
    monkeypatch.setattr(steam_auth, "update_account", lambda *_args, **_kw: None)
    monkeypatch.setattr(steam_auth, "log", lambda *_args, **_kw: None)
    return SimpleNamespace(account=account, saved=saved, login=login, writes=writes)


def test_verify_reuses_valid_account_session_without_new_password_login(monkeypatch, auth_env):
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: True)

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result["ok"] is True
    assert result["status"] == "session_valid"
    auth_env.login.assert_not_called()
    auth_env.writes.assert_not_called()


def test_uncertain_cookie_check_does_not_report_success_or_start_another_login(monkeypatch, auth_env):
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", lambda *_args: None)

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result["ok"] is False
    assert result["status"] == "network_error"
    auth_env.login.assert_not_called()


@pytest.mark.parametrize("manual_first", [True, False])
@pytest.mark.parametrize("login_ok", [True, False])
def test_manual_verification_and_inventory_relogin_share_one_result(monkeypatch, auth_env, manual_first, login_ok):
    entered, joined, release = threading.Event(), threading.Event(), threading.Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(steam_auth, "Future", ObservedFuture, raising=False)

    def login(*_args):
        entered.set()
        assert release.wait(3)
        if login_ok:
            return True, "", {"steamLoginSecure": "76561198000000000%7C%7Cnew-token", "sessionid": "new"}
        return False, "auth_pending: pending", {}

    auth_env.login.side_effect = login
    manual = lambda: steam_auth.verify_steam_auto_login("account-1")
    auto = steam_auth.try_steam_auto_relogin
    first, second = (manual, auto) if manual_first else (auto, manual)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first)
        try:
            assert entered.wait(2)
            waiter = pool.submit(second)
            assert joined.wait(2), "The second entry point did not join the existing login"
            assert not waiter.done()
        finally:
            release.set()
        owner_result, waiter_result = owner.result(2), waiter.result(2)

    manual_result, auto_result = (owner_result, waiter_result) if manual_first else (waiter_result, owner_result)
    assert (manual_result["ok"], manual_result["status"], manual_result["message"]) == auto_result
    assert manual_result["ok"] is login_ok
    assert auth_env.login.call_count == 1
    assert auth_env.writes.call_count == int(login_ok)


def test_cookie_subject_not_saved_metadata_controls_account_reuse(monkeypatch, auth_env):
    auth_env.saved.update(steam_id=auth_env.account["steam_id"], cookies="steamLoginSecure=76561198000000001%7C%7Cother")
    check = Mock(return_value=True)
    monkeypatch.setattr(steam_auth, "_verify_steam_cookies_valid", check)

    result = steam_auth.verify_steam_auto_login("account-1")

    check.assert_not_called()
    auth_env.login.assert_called_once()
    assert result["ok"] is False


def test_changed_account_does_not_receive_inflight_credentials(auth_env):
    def login(*_args):
        auth_env.account["id"] = "account-2"
        return True, "", {"steamLoginSecure": "76561198000000000%7C%7Cnew"}

    auth_env.login.side_effect = login

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result["status"] == "account_changed"
    assert result["ok"] is False
    auth_env.writes.assert_not_called()


def test_default_polling_accepts_token_after_old_ten_poll_limit(monkeypatch):
    calls = []

    def api_call(*_args, **_kwargs):
        calls.append(1)
        payload = {"refresh_token": "ready"} if len(calls) == 11 else {}
        return SimpleNamespace(status_code=200, json=lambda: {"response": payload})

    monkeypatch.setattr(steam_auth.time, "sleep", lambda _seconds: None)
    executor = SimpleNamespace(_api_call=api_call, refresh_token="")

    steam_auth._steampy_pool_sessions_with_retry(executor, "client", "request")

    assert executor.refresh_token == "ready"
    assert len(calls) == 11


@pytest.mark.parametrize("status", [429, 500, 503])
def test_cookie_http_errors_are_unknown_not_valid(monkeypatch, status):
    response = SimpleNamespace(status_code=status, url="https://steamcommunity.com/my/", text="null", json=lambda: None)
    monkeypatch.setattr(steam_auth._req.Session, "get", lambda *_args, **_kw: response)

    assert steam_auth._verify_steam_cookies_valid("steamLoginSecure=76561198000000000%7C%7Ctest") is None


def test_cookie_network_error_is_unknown_not_valid(monkeypatch):
    def fail(*_args, **_kwargs):
        raise steam_auth._req.exceptions.Timeout("test timeout")

    monkeypatch.setattr(steam_auth._req.Session, "get", fail)

    assert steam_auth._verify_steam_cookies_valid("steamLoginSecure=76561198000000000%7C%7Ctest") is None


@pytest.mark.parametrize("path, status, expected", [
    ("/profiles/76561198000000000", 200, True),
    ("/profiles/76561198000000001", 200, False),
    ("/id/test-user/", 200, True),
    ("/login/home/", 200, False),
    ("/my/profile", 200, None),
    ("/my/profile", 401, False),
])
def test_cookie_verification_requires_authenticated_profile(monkeypatch, path, status, expected):
    response = SimpleNamespace(status_code=status, url="https://steamcommunity.com" + path, history=[object()])
    get = Mock(return_value=response)
    monkeypatch.setattr(steam_auth._req.Session, "get", get)

    assert steam_auth._verify_steam_cookies_valid("steamLoginSecure=76561198000000000%7c%7ctest") is expected
    get.assert_called_once()


def test_different_account_cannot_start_login_while_another_is_running(monkeypatch, auth_env):
    active = Future()
    monkeypatch.setattr(steam_auth, "_steam_auth_inflight", ("account-2", active))
    select = Mock()
    monkeypatch.setattr(steam_auth, "set_current", select)

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result["status"] == "busy"
    select.assert_not_called()
    auth_env.login.assert_not_called()
    assert not active.done()


def test_waiter_timeout_does_not_cancel_or_restart_owner_login(monkeypatch, auth_env):
    active = Future()
    monkeypatch.setattr(steam_auth, "_steam_auth_inflight", ("account-1", active))
    monkeypatch.setattr(steam_auth, "_STEAM_AUTH_JOIN_TIMEOUT_SECONDS", 0)

    assert steam_auth.verify_steam_auto_login("account-1")["status"] == "busy"
    assert not active.done()
    auth_env.login.assert_not_called()
    active.set_result((True, "auto_ok", "ready"))
    assert steam_auth.try_steam_auto_relogin() == (True, "auto_ok", "ready")


def test_unexpected_login_error_releases_shared_task_without_leaking_details(auth_env):
    auth_env.login.side_effect = RuntimeError("private credential")

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result["status"] == "error"
    assert "private credential" not in result["message"]
    assert steam_auth._steam_auth_inflight is None
    auth_env.login.side_effect = None
    assert steam_auth.try_steam_auto_relogin()[1] == "auth_pending"


def test_polling_stops_scheduling_requests_after_time_budget():
    now, calls = [0.0], []

    def api_call(*_args, **_kwargs):
        calls.append(1)
        now[0] += 4
        return SimpleNamespace(status_code=200, json=lambda: {"response": {}})

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(steam_auth.SteamAuthTokenPending):
        steam_auth._poll_steam_refresh_token(
            SimpleNamespace(_api_call=api_call), "client", "request",
            clock=lambda: now[0], sleeper=sleep, timeout_seconds=5,
        )

    assert len(calls) == 1
    assert now[0] == 5


@pytest.mark.parametrize("status, payload", [(429, {"response": {}}), (200, {}), (200, None)])
def test_protocol_errors_are_not_reported_as_pending(status, payload):
    response = SimpleNamespace(status_code=status, json=lambda: payload)
    executor = SimpleNamespace(_api_call=lambda *_args, **_kw: response)

    with pytest.raises(RuntimeError) as exc:
        steam_auth._poll_steam_refresh_token(executor, "client", "request")

    assert not isinstance(exc.value, steam_auth.SteamAuthTokenPending)
