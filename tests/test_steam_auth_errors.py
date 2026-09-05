import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _JsonResponse:
    status_code = 200

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_steam_login_connection_error_message_is_explicit():
    from app.services import steam_auth

    exc = steam_auth._req.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='steamcommunity.com', port=443): "
        "Max retries exceeded with url: / "
        "(Caused by NewConnectionError: failed to establish a new connection)"
    )

    msg = steam_auth._classify_steam_login_exception(exc)

    assert msg.startswith("network_error:")
    assert "443 是 HTTPS 端口" in msg
    assert "不是账号密码" in msg
    assert "Steam Guard" in msg
    assert "原始错误" in msg


def test_nonstandard_http_443_is_classified_as_proxy_network_error():
    from app.services import steam_auth

    msg = steam_auth._classify_steam_login_exception(
        RuntimeError("Could not obtain rsa-key. Status code: 443")
    )

    assert msg.startswith("network_error:")
    assert "非标准 HTTP 443" in msg
    assert "代理、加速器或安全网关" in msg


def test_steam_login_error_redacts_proxy_credentials():
    from app.services import steam_auth

    msg = steam_auth._classify_steam_login_exception(
        steam_auth._req.exceptions.ProxyError(
            "HTTPSConnectionPool(proxy='http://alice:secret@proxy.example:8080')"
        )
    )

    assert "alice" not in msg
    assert "secret" not in msg
    assert "http://***@proxy.example:8080" in msg


def test_steam_refresh_token_poll_retries_pending_response_and_tracks_new_client_id():
    from app.services import steam_auth

    calls = []
    responses = iter(
        [
            _JsonResponse({"response": {"new_client_id": "client-2"}}),
            _JsonResponse({"response": {}}),
            _JsonResponse({"response": {"refresh_token": "refresh-ok"}}),
        ]
    )

    class FakeExecutor:
        refresh_token = ""

        def _api_call(self, method, service, endpoint, params):
            calls.append((method, service, endpoint, params))
            return next(responses)

    executor = FakeExecutor()
    sleeps = []

    token = steam_auth._poll_steam_refresh_token(
        executor,
        "client-1",
        "request-1",
        max_attempts=3,
        interval_seconds=0.25,
        sleeper=sleeps.append,
    )

    assert token == "refresh-ok"
    assert executor.refresh_token == "refresh-ok"
    assert [call[3]["client_id"] for call in calls] == [
        "client-1",
        "client-2",
        "client-2",
    ]
    assert sleeps == [0.25, 0.25]


def test_steam_refresh_token_poll_timeout_has_actionable_error():
    from app.services import steam_auth

    class FakeExecutor:
        refresh_token = ""

        def _api_call(self, method, service, endpoint, params):
            return _JsonResponse({"response": {}})

    with pytest.raises(steam_auth.SteamAuthTokenPending) as exc_info:
        steam_auth._poll_steam_refresh_token(
            FakeExecutor(),
            "client-1",
            "request-1",
            max_attempts=2,
            interval_seconds=0,
            sleeper=lambda _seconds: None,
        )

    message = str(exc_info.value)
    assert "refresh_token" not in message
    assert "停止等待" in message
    assert "shared_secret" not in message
    assert "系统时间" not in message


def test_do_steampy_login_repairs_upstream_single_poll_and_restores_patch(monkeypatch):
    import requests
    from steampy import client as steampy_client
    from steampy.login import LoginExecutor
    from app.services import steam_auth

    original_pool = LoginExecutor._pool_sessions_steam
    poll_count = {"value": 0}

    class FakeSteamClient:
        def __init__(self, **kwargs):
            self._session = requests.Session()
            self._session.cookies.set(
                "steamLoginSecure",
                "76561198000000000%7C%7Caccess-token",
                domain="steamcommunity.com",
            )
            self._session.cookies.set(
                "sessionid",
                "session-id",
                domain="steamcommunity.com",
            )

        def login(self):
            executor = LoginExecutor("user", "password", "secret", self._session)

            def fake_api_call(method, service, endpoint, params):
                poll_count["value"] += 1
                if poll_count["value"] == 1:
                    return _JsonResponse({"response": {}})
                return _JsonResponse(
                    {"response": {"refresh_token": "refresh-ok"}}
                )

            executor._api_call = fake_api_call
            executor._pool_sessions_steam("client-1", "request-1")

        def is_session_alive(self):
            return True

    monkeypatch.setattr(steampy_client, "SteamClient", FakeSteamClient)
    monkeypatch.setattr(steam_auth, "_STEAM_AUTH_POLL_INTERVAL_SECONDS", 0)

    ok, error, cookies = steam_auth._do_steampy_login(
        "user",
        "password",
        {"shared_secret": "secret"},
    )

    assert ok is True
    assert error == ""
    assert cookies["steamLoginSecure"].startswith("76561198000000000")
    assert poll_count["value"] == 2
    assert LoginExecutor._pool_sessions_steam is original_pool


def test_do_steampy_login_once_injects_proxy_and_bounded_timeout(monkeypatch):
    import requests
    from steampy import client as steampy_client
    from app.services import steam_auth

    request_calls = []

    def fake_request(_session, method, url, **kwargs):
        request_calls.append((method, url, kwargs))
        return _JsonResponse({})

    class FakeSteamClient:
        def __init__(self, **_kwargs):
            self._session = requests.Session()
            self._session.cookies.set(
                "steamLoginSecure",
                "76561198000000000%7C%7Caccess-token",
                domain="steamcommunity.com",
            )

        def login(self):
            self._session.get("https://steamcommunity.com/login")

        def is_session_alive(self):
            return True

    proxy = {
        "http": "http://proxy.example:8080/",
        "https": "http://proxy.example:8080/",
    }
    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(steampy_client, "SteamClient", FakeSteamClient)

    ok, error, _cookies = steam_auth._do_steampy_login_once(
        "user",
        "password",
        {"shared_secret": "secret"},
        proxy,
    )

    assert ok is True
    assert error == ""
    assert len(request_calls) == 1
    assert request_calls[0][2]["proxies"] == proxy
    assert request_calls[0][2]["timeout"] == (10, 25)
    assert request_calls[0][2]["verify"] is False


def test_steam_login_strategy_one_retries_network_error_through_proxy(monkeypatch):
    from app.services import steam_auth

    proxy = {
        "http": "http://proxy.example:8080/",
        "https": "http://proxy.example:8080/",
    }
    manager_calls = []
    login_calls = []

    class FakeProxyManager:
        def get_proxies_for_request(self, failed=False):
            manager_calls.append(failed)
            return proxy if failed else None

    def fake_login_once(_username, _password, _guard, request_proxies=None):
        login_calls.append(request_proxies)
        if request_proxies is None:
            return False, "network_error: direct failed", {}
        return True, "", {"steamLoginSecure": "ok"}

    monkeypatch.setattr(
        steam_auth,
        "load_app_config_validated",
        lambda: {"proxy_pool": {"enabled": True, "strategy": 1}},
    )
    monkeypatch.setattr(
        steam_auth,
        "_get_steam_login_proxy_manager",
        lambda: FakeProxyManager(),
    )
    monkeypatch.setattr(steam_auth, "_do_steampy_login_once", fake_login_once)

    result = steam_auth._do_steampy_login("user", "password", None)

    assert result[0] is True
    assert manager_calls == [False, True]
    assert login_calls == [None, proxy]


def test_steam_login_does_not_retry_authentication_error(monkeypatch):
    from app.services import steam_auth

    manager_calls = []
    login_calls = []

    class FakeProxyManager:
        def get_proxies_for_request(self, failed=False):
            manager_calls.append(failed)
            return None

    def fake_login_once(_username, _password, _guard, request_proxies=None):
        login_calls.append(request_proxies)
        return False, "wrong_creds", {}

    monkeypatch.setattr(
        steam_auth,
        "load_app_config_validated",
        lambda: {"proxy_pool": {"enabled": True, "strategy": 1}},
    )
    monkeypatch.setattr(
        steam_auth,
        "_get_steam_login_proxy_manager",
        lambda: FakeProxyManager(),
    )
    monkeypatch.setattr(steam_auth, "_do_steampy_login_once", fake_login_once)

    result = steam_auth._do_steampy_login("user", "password", None)

    assert result == (False, "wrong_creds", {})
    assert manager_calls == [False]
    assert login_calls == [None]


def test_steam_login_strategy_two_uses_proxy_and_rotates_once(monkeypatch):
    from app.services import steam_auth

    proxies = [
        {"https": "http://proxy-one.example:8080/"},
        {"https": "http://proxy-two.example:8080/"},
    ]
    manager_calls = []
    login_calls = []

    class FakeProxyManager:
        def get_proxies_for_request(self, failed=False):
            manager_calls.append(failed)
            return proxies[len(manager_calls) - 1]

    def fake_login_once(_username, _password, _guard, request_proxies=None):
        login_calls.append(request_proxies)
        if len(login_calls) == 1:
            return False, "network_error: proxy failed", {}
        return True, "", {"steamLoginSecure": "ok"}

    monkeypatch.setattr(
        steam_auth,
        "load_app_config_validated",
        lambda: {"proxy_pool": {"enabled": True, "strategy": 2}},
    )
    monkeypatch.setattr(
        steam_auth,
        "_get_steam_login_proxy_manager",
        lambda: FakeProxyManager(),
    )
    monkeypatch.setattr(steam_auth, "_do_steampy_login_once", fake_login_once)

    result = steam_auth._do_steampy_login("user", "password", None)

    assert result[0] is True
    assert manager_calls == [False, True]
    assert login_calls == proxies


def test_steam_login_strategy_two_never_falls_back_to_direct(monkeypatch):
    from app.services import steam_auth

    class EmptyProxyManager:
        def get_proxies_for_request(self, failed=False):
            return None

    login_calls = []
    monkeypatch.setattr(
        steam_auth,
        "load_app_config_validated",
        lambda: {"proxy_pool": {"enabled": True, "strategy": 2}},
    )
    monkeypatch.setattr(
        steam_auth,
        "_get_steam_login_proxy_manager",
        lambda: EmptyProxyManager(),
    )
    monkeypatch.setattr(
        steam_auth,
        "_do_steampy_login_once",
        lambda *_args: login_calls.append(_args),
    )

    result = steam_auth._do_steampy_login("user", "password", None)

    assert result[0] is False
    assert result[1].startswith("network_error:")
    assert "没有可用节点" in result[1]
    assert login_calls == []


def test_do_steampy_login_converts_refresh_token_key_error_to_auth_pending(monkeypatch):
    import requests
    from steampy import client as steampy_client
    from steampy.login import LoginExecutor
    from app.services import steam_auth

    original_request = requests.Session.request
    original_pool = LoginExecutor._pool_sessions_steam

    class FakeSteamClient:
        def __init__(self, **kwargs):
            self._session = requests.Session()

        def login(self):
            raise KeyError("refresh_token")

    monkeypatch.setattr(steampy_client, "SteamClient", FakeSteamClient)

    ok, error, cookies = steam_auth._do_steampy_login(
        "user",
        "password",
        {"shared_secret": "secret"},
    )

    assert ok is False
    assert error.startswith("auth_pending:")
    assert "refresh_token" not in error
    assert cookies == {}
    assert requests.Session.request is original_request
    assert LoginExecutor._pool_sessions_steam is original_pool
    assert steam_auth._steampy_login_patch_lock.acquire(blocking=False)
    steam_auth._steampy_login_patch_lock.release()


def test_do_steampy_login_releases_lock_when_login_executor_import_fails(monkeypatch):
    import requests
    from steampy import client as steampy_client
    from app.services import steam_auth

    # Ensure steampy.client is already loaded, then make only the subsequent
    # LoginExecutor import fail inside _do_steampy_login.
    assert steampy_client.SteamClient
    original_request = requests.Session.request
    monkeypatch.setitem(sys.modules, "steampy.login", types.ModuleType("steampy.login"))

    ok, _error, cookies = steam_auth._do_steampy_login(
        "user",
        "password",
        {"shared_secret": "secret"},
    )

    assert ok is False
    assert cookies == {}
    assert requests.Session.request is original_request
    assert steam_auth._steampy_login_patch_lock.acquire(blocking=False)
    steam_auth._steampy_login_patch_lock.release()


def test_verify_account_surfaces_auth_pending_instead_of_refresh_token(monkeypatch):
    from app.services import steam_auth

    monkeypatch.setattr(
        steam_auth,
        "get_account",
        lambda account_id: {
            "id": account_id,
            "username": "user",
            "password": "password",
        },
    )
    monkeypatch.setattr(steam_auth, "set_current", lambda _account_id: True)
    monkeypatch.setattr(steam_auth, "get_steam_credentials", lambda: {})
    monkeypatch.setattr(
        steam_auth,
        "load_app_config_validated",
        lambda: {"steam_guard": {"shared_secret": "secret"}},
    )
    monkeypatch.setattr(
        steam_auth,
        "_do_steampy_login",
        lambda *_args: (
            False,
            "auth_pending: Steam 登录确认尚未返回凭证，请稍后重试",
            {},
        ),
    )

    result = steam_auth.verify_steam_auto_login("account-1")

    assert result == {
        "ok": False,
        "status": "auth_pending",
        "message": "Steam 登录确认尚未返回凭证，请稍后重试",
    }
