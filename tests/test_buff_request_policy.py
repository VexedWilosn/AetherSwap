import json
import threading

import pytest
import requests
from requests.cookies import RequestsCookieJar, create_cookie

from buff.buyer import (
    API_BUY,
    API_BUY_PREVIEW,
    API_HISTORY,
    API_PAGE_PAY,
    API_SELL_ORDER,
    API_STEAM_TRADE,
    API_USER_INFO,
    API_WX_PAY_QRCODE,
    DEFAULT_USER_AGENT,
    PAY_METHOD_ALIPAY,
    PAY_METHOD_WECHAT,
    BuffBuyer,
)
from buff.request_policy import (
    BuffAuthExpired,
    BuffRateLimited,
    BuffRequestBlocked,
    BuffRequestPolicy,
    BuffRiskControlTriggered,
    BuffVerificationRequired,
    BuffWriteResultUnknown,
    DEFAULT_MIN_REQUEST_INTERVAL,
    DEFAULT_RATE_LIMIT_SECONDS,
    account_fingerprint,
    parse_retry_after,
)


class FakeResponse:
    def __init__(
        self,
        data,
        *,
        status_code=200,
        headers=None,
        cookies=None,
        raw_text=None,
        url="",
        history=None,
    ):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.text = (
            raw_text
            if raw_text is not None
            else (json.dumps(data, ensure_ascii=False) if data is not None else "")
        )
        self.url = url
        self.history = history or []
        self.cookies = RequestsCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value)

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.close_calls = 0
        self.cookies = RequestsCookieJar()
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.close_calls += 1


def no_wait_policy(**kwargs):
    return BuffRequestPolicy(min_interval=0, state_path=None, persist=False, **kwargs)


STEAM_ID = "76561198000000000"


def user_info_response(*, steam_id=STEAM_ID):
    return FakeResponse(
        {
            "code": "OK",
            "data": {
                "user_info": {
                    "id": "buff-user",
                    "steamid": steam_id,
                }
            },
        }
    )


def buy_preview_response(*, pay_method=PAY_METHOD_ALIPAY, btn_clickable=True):
    return FakeResponse(
        {
            "code": "OK",
            "data": {
                "pay_methods": [
                    {
                        "value": pay_method,
                        "btn_clickable": btn_clickable,
                    }
                ]
            },
        }
    )


def checkout_responses(*after_preview, pay_method=PAY_METHOD_ALIPAY):
    """Responses required before and after a single-item checkout POST."""

    return (
        user_info_response(),
        buy_preview_response(pay_method=pay_method),
        *after_preview,
    )


def test_default_user_agent_is_stable_and_can_be_overridden():
    first = BuffBuyer("session=one", request_policy=no_wait_policy())
    second = BuffBuyer("session=two", request_policy=no_wait_policy())
    custom = BuffBuyer(
        "session=three", user_agent="Browser/7", request_policy=no_wait_policy()
    )

    assert first.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert second.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert custom.headers["User-Agent"] == "Browser/7"
    assert custom.user_agent == "Browser/7"


def test_generic_alipay_uses_wallet_channel_not_credit_card_huabei():
    # BUFF currently exposes 49 as "Alipay" and 51 as the distinct
    # "Alipay - credit card/Huabei" channel.
    assert PAY_METHOD_ALIPAY == 49


def test_close_only_releases_an_internally_owned_session(monkeypatch):
    owned = FakeSession()
    monkeypatch.setattr("buff.buyer.requests.Session", lambda: owned)
    buyer = BuffBuyer("session=owned", request_policy=no_wait_policy())
    buyer.close()
    buyer.close()
    assert owned.close_calls == 1

    external = FakeSession()
    injected = BuffBuyer(
        "session=external", session=external, request_policy=no_wait_policy()
    )
    injected.close()
    assert external.close_calls == 0


def test_persistent_session_accepts_cookie_and_syncs_csrf_before_write():
    session = FakeSession(
        FakeResponse({"code": "OK"}, cookies={"csrf_token": "rotated"}),
        FakeResponse({"code": "OK"}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=initial",
        session=session,
        request_policy=no_wait_policy(),
    )

    buyer._make_request("GET", "https://buff.163.com/api/first")
    buyer._make_request("POST", "https://buff.163.com/api/second", data="{}")

    assert len(session.calls) == 2
    assert session.calls[1][2]["headers"]["X-Csrftoken"] == "rotated"
    assert buyer.cookies_dict["csrf_token"] == "rotated"
    exported = buyer.export_cookie_string()
    assert "session=s" in exported
    assert "csrf_token=rotated" in exported
    assert "csrf_token=initial" not in exported


def test_429_retry_after_opens_and_persists_account_circuit(tmp_path):
    state_path = tmp_path / "buff-policy.json"
    session = FakeSession(
        FakeResponse(
            {"code": "TOO_MANY_REQUESTS", "msg": "slow down"},
            status_code=429,
            headers={"Retry-After": "120"},
        )
    )
    policy = BuffRequestPolicy(min_interval=0, state_path=state_path)
    buyer = BuffBuyer("session=rate-secret", session=session, request_policy=policy)

    with pytest.raises(BuffRateLimited) as first:
        buyer._make_request("GET", "https://buff.163.com/api/one")
    assert first.value.retry_after == pytest.approx(120, abs=1)

    with pytest.raises(BuffRateLimited):
        buyer._make_request("GET", "https://buff.163.com/api/two")
    assert len(session.calls) == 1

    reloaded = BuffRequestPolicy(min_interval=0, state_path=state_path)
    another_session = FakeSession(FakeResponse({"code": "OK"}))
    another = BuffBuyer(
        "session=rate-secret", session=another_session, request_policy=reloaded
    )
    with pytest.raises(BuffRateLimited):
        another._make_request("GET", "https://buff.163.com/api/three")
    assert another_session.calls == []
    assert "rate-secret" not in state_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        "{}",
        '{"version": 2, "accounts": {}}',
        '{"version": 1, "accounts": {"default": {}}}',
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "verification", "updated_at": NaN}}}'
        ),
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "rate_limited", "updated_at": 1, '
            '"blocked_until": Infinity, "status_code": 429}}}'
        ),
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "rate_limited", "updated_at": 1, '
            '"blocked_until": 2, "status_code": 1e999}}}'
        ),
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "rate_limited", "updated_at": true, '
            '"blocked_until": true, "status_code": 429}}}'
        ),
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "verification", "updated_at": "1"}}}'
        ),
        (
            '{"version": 1, "accounts": {"default": '
            '{"reason": "rate_limited", "updated_at": 1, '
            '"blocked_until": 9999999999, "status_code": "429"}}}'
        ),
        '{"version": 1, "version": 1, "accounts": {}}',
    ],
)
def test_damaged_policy_state_fails_closed_until_explicit_clear(tmp_path, raw):
    state_path = tmp_path / "buff-policy.json"
    state_path.write_text(raw, encoding="utf-8")
    policy = BuffRequestPolicy(min_interval=0, state_path=state_path)

    with pytest.raises(BuffVerificationRequired, match="状态文件"):
        with policy.request_slot("default"):
            raise AssertionError("damaged policy must not yield a request slot")

    policy.clear("default")
    with policy.request_slot("default"):
        pass


def test_rate_limit_record_without_deadline_is_invalid_and_blocks(tmp_path):
    state_path = tmp_path / "buff-policy.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "default": {
                        "reason": "rate_limited",
                        "updated_at": 1.0,
                        "blocked_until": None,
                        "message": "missing deadline",
                        "status_code": 429,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    policy = BuffRequestPolicy(min_interval=0, state_path=state_path)

    with pytest.raises(BuffVerificationRequired, match="状态文件"):
        policy.raise_if_blocked("default")


def test_failed_policy_replace_leaves_restart_fail_closed_marker(
    monkeypatch,
    tmp_path,
):
    import buff.request_policy as policy_module

    state_path = tmp_path / "buff-policy.json"
    policy = BuffRequestPolicy(min_interval=0, state_path=state_path)
    session = FakeSession(FakeResponse({"code": "FAIL"}, status_code=403))
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=policy,
    )
    real_replace = policy_module.os.replace
    monkeypatch.setattr(
        policy_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk replace failed")),
    )

    # Persistence failure must never replace the stronger post-send write
    # classification with a pre-send verification exception.
    with pytest.raises(BuffWriteResultUnknown):
        buyer._make_request(
            "POST",
            "https://buff.163.com/api/write",
            data="{}",
        )
    with pytest.raises(BuffVerificationRequired):
        policy.raise_if_blocked(buyer.account_key)

    monkeypatch.setattr(policy_module.os, "replace", real_replace)
    reloaded = BuffRequestPolicy(min_interval=0, state_path=state_path)
    with pytest.raises(BuffVerificationRequired, match="状态文件"):
        reloaded.raise_if_blocked(buyer.account_key)

    reloaded.clear(buyer.account_key)
    reloaded.raise_if_blocked(buyer.account_key)


@pytest.mark.parametrize("status_code", [403, 412])
def test_risk_http_status_opens_persistent_circuit_until_explicit_reset(
    status_code, tmp_path
):
    session = FakeSession(
        FakeResponse({"code": "FAIL"}, status_code=status_code),
        FakeResponse({"code": "OK"}),
    )
    state_path = tmp_path / f"risk-{status_code}.json"
    policy = BuffRequestPolicy(min_interval=0, state_path=state_path)
    buyer = BuffBuyer("session=risk", session=session, request_policy=policy)

    with pytest.raises(BuffRiskControlTriggered) as first:
        buyer._make_request("GET", "https://buff.163.com/api/one")
    assert first.value.status_code == status_code

    with pytest.raises(BuffRiskControlTriggered):
        buyer._make_request("GET", "https://buff.163.com/api/two")
    assert len(session.calls) == 1

    reloaded_session = FakeSession(FakeResponse({"code": "OK"}))
    reloaded = BuffBuyer(
        "session=risk",
        session=reloaded_session,
        request_policy=BuffRequestPolicy(min_interval=0, state_path=state_path),
    )
    with pytest.raises(BuffRiskControlTriggered):
        reloaded._make_request("GET", "https://buff.163.com/api/reloaded")
    assert reloaded_session.calls == []

    buyer.reset_request_circuit()
    assert buyer._make_request("GET", "https://buff.163.com/api/three") == {
        "code": "OK"
    }
    assert len(session.calls) == 2


def test_get_sell_orders_non_ok_does_not_retry():
    session = FakeSession(FakeResponse({"code": "FAIL", "msg": "bad argument"}))
    buyer = BuffBuyer(
        "session=sell-orders", session=session, request_policy=no_wait_policy()
    )

    assert buyer.get_sell_orders(123) is None
    assert len(session.calls) == 1


def test_legacy_get_and_buy_never_falls_through_to_second_sell_order():
    session = FakeSession(
        FakeResponse(
            {
                "code": "OK",
                "data": {
                    "items": [
                        {"id": "sell-1", "price": "10.00", "user_id": "one"},
                        {"id": "sell-2", "price": "10.00", "user_id": "two"},
                    ]
                },
            }
        ),
        user_info_response(),
        buy_preview_response(),
        FakeResponse({"code": "FAIL", "msg": "rejected"}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    buyer.get_and_buy(123, price_tolerance=1.0)

    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_SELL_ORDER),
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]
    payload = json.loads(session.calls[3][2]["data"])
    assert payload["sell_order_id"] == "sell-1"


def test_configured_request_timeout_is_used_by_default():
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=s",
        session=session,
        request_policy=no_wait_policy(),
        request_timeout=17,
    )

    buyer._make_request("GET", "https://buff.163.com/api/read")

    assert session.calls[0][2]["timeout"] == 17


def test_policy_enforces_hard_minimum_interval_for_same_account():
    now = [10.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    policy = no_wait_policy(clock=clock, sleeper=sleep)
    policy.min_interval = 1.25

    with policy.request_slot("account"):
        pass
    with policy.request_slot("account"):
        pass

    assert sleeps == [pytest.approx(1.25)]


def test_production_defaults_use_two_second_interval_and_five_minute_429():
    assert DEFAULT_MIN_REQUEST_INTERVAL == 2.0
    assert DEFAULT_RATE_LIMIT_SECONDS == 300.0

    session = FakeSession(
        FakeResponse({"code": "TOO_MANY_REQUESTS"}, status_code=429)
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )
    with pytest.raises(BuffRateLimited) as exc_info:
        buyer._make_request("GET", "https://buff.163.com/api/rate")
    assert exc_info.value.retry_after == pytest.approx(300.0)


@pytest.mark.parametrize(
    "raw",
    ["NaN", "Infinity", "-Infinity", "-1", "-0.01"],
)
def test_invalid_numeric_retry_after_uses_safe_default(raw):
    assert parse_retry_after(raw) == pytest.approx(DEFAULT_RATE_LIMIT_SECONDS)


def test_non_finite_or_negative_retry_defaults_are_also_rejected():
    assert parse_retry_after(None, default=float("nan")) == pytest.approx(
        DEFAULT_RATE_LIMIT_SECONDS
    )
    assert parse_retry_after(None, default=-1) == pytest.approx(
        DEFAULT_RATE_LIMIT_SECONDS
    )


def test_cooling_down_response_opens_default_rate_limit_circuit():
    session = FakeSession(
        FakeResponse({"code": "Cooling Down", "msg": "operation too fast"})
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffWriteResultUnknown):
        buyer._make_request("POST", API_BUY, data="{}")

    with pytest.raises(BuffRateLimited) as exc_info:
        buyer._make_request("GET", API_HISTORY)
    assert exc_info.value.retry_after == pytest.approx(DEFAULT_RATE_LIMIT_SECONDS)
    assert len(session.calls) == 1


def test_owned_session_ignores_environment_proxy_settings():
    buyer = BuffBuyer(
        "session=s; csrf_token=c", request_policy=no_wait_policy()
    )
    try:
        assert buyer.session.trust_env is False
    finally:
        buyer.close()


def test_buff_client_uses_stable_default_account_key_across_session_rotation():
    from app.services.buff_client import BuffClient

    credentials = {
        "cookies": "session=rotated; csrf_token=next",
        "generation": 2,
    }
    client = BuffClient(
        "session=initial; csrf_token=old",
        credential_generation=1,
        credentials_provider=lambda: credentials,
    )
    try:
        original_key = client._buyer.account_key
        rebuilt_key = client._run(lambda buyer: buyer.account_key)
        assert original_key == rebuilt_key == account_fingerprint({}, "default")
    finally:
        client.close()


def test_buff_client_reloads_changed_credentials_without_generation_bump():
    from app.services.buff_client import BuffClient

    credentials = {
        "cookies": "session=initial; csrf_token=old",
        "user_agent": "Browser/1",
        "generation": 1,
    }
    client = BuffClient(
        credentials["cookies"],
        user_agent=credentials["user_agent"],
        credential_generation=1,
        credentials_provider=lambda: credentials,
    )
    try:
        credentials["cookies"] = "session=rotated; csrf_token=new"
        credentials["user_agent"] = "Browser/2"

        current = client._run(
            lambda buyer: (buyer.cookies_dict["session"], buyer.user_agent)
        )

        assert current == ("rotated", "Browser/2")
    finally:
        client.close()


def test_missing_csrf_blocks_write_before_http_call():
    session = FakeSession(FakeResponse({"code": "OK"}))
    buyer = BuffBuyer(
        "session=s", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffAuthExpired, match="csrf_token"):
        buyer._make_request("POST", "https://buff.163.com/api/write", data="{}")
    assert session.calls == []


def test_waiting_writer_reads_csrf_only_after_preceding_response_rotates_it():
    first_entered = threading.Event()
    release_first = threading.Event()

    class ConcurrentSession(FakeSession):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if len(self.calls) == 1:
                first_entered.set()
                assert release_first.wait(timeout=2)
                return FakeResponse(
                    {"code": "OK"}, cookies={"csrf_token": "rotated"}
                )
            return FakeResponse({"code": "OK"})

    session = ConcurrentSession()
    buyer = BuffBuyer(
        "session=s; csrf_token=initial",
        session=session,
        request_policy=no_wait_policy(),
        account_id="default",
    )
    failures = []

    def run(method, url):
        try:
            buyer._make_request(method, url, data="{}" if method == "POST" else None)
        except Exception as exc:  # pragma: no cover - assertion reports details
            failures.append(exc)

    first = threading.Thread(target=run, args=("GET", "https://buff.163.com/api/one"))
    second = threading.Thread(target=run, args=("POST", "https://buff.163.com/api/two"))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert failures == []
    assert not first.is_alive() and not second.is_alive()
    assert session.calls[1][2]["headers"]["X-Csrftoken"] == "rotated"


def test_write_network_timeout_is_explicit_unknown_result():
    session = FakeSession(requests.Timeout("timed out"))
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        buyer._make_request("POST", "https://buff.163.com/api/write", data="{}")
    assert exc_info.value.method == "POST"
    assert exc_info.value.url.endswith("/api/write")
    assert exc_info.value.status_code is None
    assert isinstance(exc_info.value.__cause__, requests.Timeout)


def test_public_lock_method_does_not_swallow_unknown_write_result():
    session = FakeSession(
        *checkout_responses(requests.ConnectionError("connection dropped"))
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffWriteResultUnknown):
        buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]


def test_success_without_order_id_is_unknown_and_skips_payment_lookup():
    session = FakeSession(
        *checkout_responses(FakeResponse({"code": "OK", "data": {}}))
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffWriteResultUnknown, match="订单号"):
        buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("batch_buy_create", (1, 10.0, 2, "csgo")),
        (
            "batch_buy_finalize",
            ("csgo", 1, "sell-order", "10.00", "batch"),
        ),
    ],
)
def test_legacy_batch_writes_are_locally_blocked_without_http(method_name, args):
    session = FakeSession()
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        pay_method=PAY_METHOD_WECHAT,
        session=session,
        request_policy=no_wait_policy(),
    )

    with pytest.raises(BuffRequestBlocked):
        getattr(buyer, method_name)(*args)
    assert session.calls == []


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("_execute_post_buy", ("csgo", 1, "sell-order", "10.00")),
        ("lock_and_get_pay_url", ("csgo", 1, "sell-order", "10.00")),
    ],
)
@pytest.mark.parametrize(
    "success_data",
    [None, [], "bad-data", {}, {"id": ""}, {"id": {"bad": "type"}}],
)
def test_every_order_creating_write_rejects_malformed_success_data(
    method_name, args, success_data
):
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "OK", "data": success_data})
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    with pytest.raises(BuffWriteResultUnknown):
        getattr(buyer, method_name)(*args)
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]


@pytest.mark.parametrize("pay_method", [PAY_METHOD_ALIPAY, PAY_METHOD_WECHAT])
def test_execute_preflights_user_info_and_clickable_payment_method_before_post(
    pay_method,
):
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "FAIL", "msg": "rejected"}),
            pay_method=pay_method,
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        pay_method=pay_method,
        session=session,
        request_policy=no_wait_policy(),
    )

    assert buyer._execute_post_buy("csgo", 1, "sell-order", "10.00") == "FAIL"
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]
    preview_params = session.calls[1][2]["params"]
    assert preview_params["steamid"] == STEAM_ID
    assert preview_params["sell_order_id"] == "sell-order"
    payload = json.loads(session.calls[2][2]["data"])
    assert payload["steamid"] == STEAM_ID
    assert payload["pay_method"] == pay_method


def test_known_steam_id_skips_user_info_but_never_skips_buy_preview():
    session = FakeSession(
        buy_preview_response(),
        FakeResponse({"code": "FAIL", "msg": "rejected"}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        steam_id=STEAM_ID,
        session=session,
        request_policy=no_wait_policy(),
    )

    assert buyer._execute_post_buy("csgo", 1, "sell-order", "10.00") == "FAIL"
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]


@pytest.mark.parametrize(
    "pay_methods",
    [
        [],
        [{"value": 6, "btn_clickable": True}],
        [{"value": PAY_METHOD_ALIPAY, "btn_clickable": False}],
        [{"value": PAY_METHOD_ALIPAY}],
    ],
)
@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("_execute_post_buy", "FAIL"),
        ("lock_and_get_pay_url", "PAY_METHOD_UNAVAILABLE"),
    ],
)
def test_single_checkout_never_posts_without_explicit_clickable_payment_method(
    method_name, expected, pay_methods
):
    session = FakeSession(
        user_info_response(),
        FakeResponse({"code": "OK", "data": {"pay_methods": pay_methods}}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    result = getattr(buyer, method_name)("csgo", 1, "sell-order", "10.00")

    if method_name == "lock_and_get_pay_url":
        assert result["code"] == expected
        assert result["created"] is False
    else:
        assert result == expected
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
    ]


def test_exact_http_200_login_required_after_post_is_known_auth_rejection():
    session = FakeSession(
        *checkout_responses(
            FakeResponse(
                {"code": "Login Required", "msg": "please login"},
                status_code=200,
            )
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    with pytest.raises(BuffAuthExpired) as exc_info:
        buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert not isinstance(exc_info.value, BuffWriteResultUnknown)
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
    ]


def test_buy_reuses_server_cashier_trace_and_current_browser_write_headers():
    preview = FakeResponse(
        {
            "code": "OK",
            "data": {
                "pay_methods": [
                    {"value": PAY_METHOD_ALIPAY, "btn_clickable": True}
                ]
            },
        },
        headers={"Buff-Cashier-Trace-ID": "server-issued-trace"},
    )
    session = FakeSession(
        user_info_response(),
        preview,
        FakeResponse({"code": "FAIL", "msg": "rejected"}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    result = buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert result["success"] is False
    preview_headers = session.calls[1][2]["headers"]
    write_headers = session.calls[2][2]["headers"]
    assert "Buff-Cashier-Trace-ID" not in preview_headers
    assert write_headers["Buff-Cashier-Trace-ID"] == "server-issued-trace"
    assert write_headers["Origin"] == "https://buff.163.com"
    assert int(write_headers["Timezone-Offset-DST"]) % 60000 == 0


def test_buy_never_invents_cashier_trace_when_server_did_not_issue_one():
    session = FakeSession(
        *checkout_responses(FakeResponse({"code": "FAIL", "msg": "rejected"}))
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert "Buff-Cashier-Trace-ID" not in session.calls[2][2]["headers"]


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse({"code": "ERROR"}, status_code=500),
        FakeResponse(
            None,
            status_code=200,
            headers={"Content-Type": "text/plain"},
            raw_text="upstream returned no JSON",
        ),
    ],
)
def test_non_idempotent_5xx_or_non_json_is_unknown_result(response):
    session = FakeSession(response)
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        buyer._make_request("POST", "https://buff.163.com/api/write", data="{}")
    assert exc_info.value.status_code == response.status_code


@pytest.mark.parametrize(
    ("response", "circuit_exception"),
    [
        (
            FakeResponse({"code": "FAIL"}, status_code=403),
            BuffRiskControlTriggered,
        ),
        (
            FakeResponse({"code": "FAIL"}, status_code=412),
            BuffRiskControlTriggered,
        ),
        (
            FakeResponse(
                {"code": "TOO_MANY_REQUESTS"},
                status_code=429,
                headers={"Retry-After": "60"},
            ),
            BuffRateLimited,
        ),
        (
            FakeResponse(
                {"code": "OK"},
                url="https://buff.163.com/account/login",
                history=[
                    FakeResponse(
                        None,
                        status_code=302,
                        headers={"Location": "/account/login"},
                        url="https://buff.163.com/api/write",
                    )
                ],
            ),
            None,
        ),
        (
            FakeResponse(
                {"code": "OK"},
                url="https://buff.163.com/verify",
                history=[
                    FakeResponse(
                        None,
                        status_code=302,
                        headers={"Location": "/verify"},
                        url="https://buff.163.com/api/write",
                    )
                ],
            ),
            BuffVerificationRequired,
        ),
        (
            FakeResponse(
                {"code": "OK"},
                url="https://buff.163.com/maintenance",
                history=[
                    FakeResponse(
                        None,
                        status_code=302,
                        headers={"Location": "/maintenance"},
                        url="https://buff.163.com/api/write",
                    )
                ],
            ),
            BuffVerificationRequired,
        ),
        (
            FakeResponse({"code": "LOGIN_REQUIRED"}, status_code=401),
            None,
        ),
        (
            FakeResponse({"code": "RISK", "msg": "captcha required"}),
            BuffVerificationRequired,
        ),
        (
            FakeResponse(
                {"code": "LOGIN_REQUIRED", "msg": "login"},
                status_code=500,
            ),
            None,
        ),
        (
            FakeResponse(
                {"code": "RATE_LIMIT", "msg": "slow down"},
                status_code=500,
            ),
            BuffRateLimited,
        ),
        (
            FakeResponse(
                None,
                headers={"Content-Type": "text/html"},
                raw_text="<html><body>please login</body></html>",
            ),
            None,
        ),
        (
            FakeResponse(
                None,
                headers={"Content-Type": "text/html"},
                raw_text="<html><body>unexpected gateway page</body></html>",
            ),
            BuffVerificationRequired,
        ),
    ],
)
def test_every_issued_abnormal_write_is_unknown_and_keeps_relevant_circuit(
    response, circuit_exception
):
    session = FakeSession(response)
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
        account_id="default",
    )

    with pytest.raises(BuffWriteResultUnknown):
        buyer._make_request("POST", "https://buff.163.com/api/write", data="{}")

    if circuit_exception is not None:
        with pytest.raises(circuit_exception):
            buyer._make_request("GET", "https://buff.163.com/api/read")
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "pay_response",
    [
        requests.Timeout("payment lookup timed out"),
        FakeResponse({"code": "LOGIN_REQUIRED"}, status_code=401),
        FakeResponse({"code": "FAIL"}, status_code=403),
        FakeResponse({"code": "OK", "data": []}),
        FakeResponse({"code": "OK", "data": {"url": {"bad": "type"}}}),
    ],
)
def test_order_created_payment_lookup_failure_returns_created_pending(pay_response):
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "OK", "data": {"id": "order-created"}}),
            pay_response,
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
        account_id="default",
    )

    result = buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert result == {
        "success": False,
        "code": "CREATED_WITHOUT_PAY_URL",
        "created": True,
        "pay_url": None,
        "pay_type": "alipay",
        "order_id": "order-created",
    }
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
        ("GET", API_PAGE_PAY),
    ]


def test_payment_lookup_block_is_preserved_after_created_pending_result():
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "OK", "data": {"id": "order-created"}}),
            FakeResponse({"code": "FAIL"}, status_code=412),
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
        account_id="default",
    )

    result = buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert result["code"] == "CREATED_WITHOUT_PAY_URL"
    assert result["created"] is True
    assert result["order_id"] == "order-created"
    with pytest.raises(BuffRiskControlTriggered):
        buyer._make_request("GET", API_HISTORY)
    assert len(session.calls) == 4


def test_wechat_payment_lookup_auth_failure_is_created_pending(monkeypatch):
    monkeypatch.setattr("buff.buyer.jittered_sleep", lambda *_args, **_kwargs: None)
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "OK", "data": {"id": "wechat-order"}}),
            FakeResponse({"code": "LOGIN_REQUIRED"}, status_code=401),
            pay_method=PAY_METHOD_WECHAT,
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        pay_method=PAY_METHOD_WECHAT,
        session=session,
        request_policy=no_wait_policy(),
    )

    result = buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00")

    assert result["code"] == "CREATED_WITHOUT_PAY_URL"
    assert result["created"] is True
    assert result["pay_type"] == "wechat"
    assert result["order_id"] == "wechat-order"
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
        ("GET", API_WX_PAY_QRCODE),
    ]


def test_created_order_with_valid_payment_url_keeps_success_contract():
    session = FakeSession(
        *checkout_responses(
            FakeResponse({"code": "OK", "data": {"id": "paid-order"}}),
            FakeResponse(
                {"code": "OK", "data": {"url": " https://pay.example/1 "}}
            ),
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    assert buyer.lock_and_get_pay_url("csgo", 1, "sell-order", "10.00") == {
        "success": True,
        "pay_url": "https://pay.example/1",
        "pay_type": "alipay",
        "order_id": "paid-order",
    }
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", API_USER_INFO),
        ("GET", API_BUY_PREVIEW),
        ("POST", API_BUY),
        ("GET", API_PAGE_PAY),
    ]


def test_login_redirect_is_auth_expired_and_verification_redirect_is_fused():
    login = FakeResponse(
        {"code": "OK"},
        url="https://buff.163.com/account/login",
        history=[
            FakeResponse(
                None,
                status_code=302,
                headers={"Location": "/account/login"},
                url="https://buff.163.com/api/history",
            )
        ],
    )
    login_session = FakeSession(login)
    login_buyer = BuffBuyer(
        "session=login", session=login_session, request_policy=no_wait_policy()
    )
    with pytest.raises(BuffAuthExpired):
        login_buyer._make_request("GET", "https://buff.163.com/api/history")

    verify = FakeResponse(
        {"code": "OK"},
        url="https://buff.163.com/verify",
        history=[
            FakeResponse(
                None,
                status_code=302,
                headers={"Location": "/verify"},
                url="https://buff.163.com/api/history",
            )
        ],
    )
    verify_session = FakeSession(verify)
    verify_buyer = BuffBuyer(
        "session=verify", session=verify_session, request_policy=no_wait_policy()
    )
    with pytest.raises(BuffVerificationRequired):
        verify_buyer._make_request("GET", "https://buff.163.com/api/history")
    with pytest.raises(BuffVerificationRequired):
        verify_buyer._make_request("GET", "https://buff.163.com/api/again")
    assert len(verify_session.calls) == 1


def test_unknown_html_read_fails_closed():
    session = FakeSession(
        FakeResponse(
            None,
            headers={"Content-Type": "text/html; charset=utf-8"},
            raw_text="<!doctype html><html><body>gateway page</body></html>",
        )
    )
    buyer = BuffBuyer(
        "session=html", session=session, request_policy=no_wait_policy()
    )
    with pytest.raises(BuffVerificationRequired):
        buyer._make_request("GET", "https://buff.163.com/api/read")


def test_initial_cookie_is_domain_scoped_and_expiration_removes_it():
    buyer = BuffBuyer(
        "session=old; csrf_token=c", request_policy=no_wait_policy()
    )
    try:
        session_cookie = next(
            cookie for cookie in buyer.session.cookies if cookie.name == "session"
        )
        assert session_cookie.domain == "buff.163.com"
        assert session_cookie.path == "/"
        assert session_cookie.secure is True

        expired = RequestsCookieJar()
        expired.set_cookie(
            create_cookie(
                "session",
                "",
                domain="buff.163.com",
                path="/",
                expires=1,
            )
        )
        buyer._merge_response_cookies(expired)
        assert "session=" not in buyer.export_cookie_string()
    finally:
        buyer.close()


def test_verify_session_is_one_lightweight_get_and_trade_poll_uses_same_session():
    session = FakeSession(
        FakeResponse(
            {
                "code": "OK",
                "data": {
                    "user_info": {"id": "buff-user", "steamid": "masked"},
                    "meta_list": {},
                },
            }
        ),
        FakeResponse({"code": "OK", "data": [{"tradeofferid": "1"}]}),
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c", session=session, request_policy=no_wait_policy()
    )

    assert buyer.verify_session() is True
    assert buyer.steam_id == "masked"
    assert buyer.get_steam_trades() == [{"tradeofferid": "1"}]
    assert [call[0] for call in session.calls] == ["GET", "GET"]
    assert session.calls[0][1] == API_USER_INFO
    assert session.calls[0][2]["params"] == {
        "meta_list": "buy_order_state"
    }
    assert session.calls[0][2]["headers"]["Referer"] == "https://buff.163.com/"
    assert session.calls[1][1] == API_STEAM_TRADE
    assert "to_receive" in session.calls[1][2]["headers"]["Referer"]


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "OK", "data": {"meta_list": {}}},
        {"code": "OK", "data": {"user_info": {}}},
        {"code": "Invalid Argument", "error": "Not a valid choice"},
    ],
)
def test_verify_session_rejects_responses_without_a_real_user(payload):
    session = FakeSession(FakeResponse(payload))
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )

    assert buyer.verify_session() is False
    assert len(session.calls) == 1
    assert session.calls[0][1] == API_USER_INFO


def test_waiting_payment_history_uses_current_state_contract_without_state_param(
    monkeypatch,
):
    session = FakeSession(
        FakeResponse(
            {
                "code": "OK",
                "data": {
                    "items": [
                        {
                            "id": "bill-paying",
                            "state": "PAYING",
                            "pay_expire_timeout": 120,
                        },
                        {
                            "id": "bill-expired",
                            "state": "PAYING",
                            "pay_expire_timeout": -1,
                        },
                        {
                            "id": "bill-success",
                            "state": "SUCCESS",
                            "pay_expire_timeout": 120,
                        },
                    ]
                },
            }
        )
    )
    buyer = BuffBuyer(
        "session=s; csrf_token=c",
        session=session,
        request_policy=no_wait_policy(),
    )
    fetched = []
    monkeypatch.setattr(
        buyer,
        "_fetch_pay_url",
        lambda game, order_id: fetched.append((game, order_id)),
    )

    assert buyer.check_wait_pay_orders() is True

    assert fetched == [("csgo", "bill-paying")]
    assert session.calls[0][1] == API_HISTORY
    assert "state" not in session.calls[0][2]["params"]


def test_cookie_persistence_failure_never_masks_operation_result_or_exception():
    from app.services.buff_client import BuffClient

    def persistence_failure(_cookies, _user_agent):
        raise OSError("disk full")

    class Buyer:
        user_agent = "Browser/1"

        def __init__(self, failure=None):
            self.failure = failure

        def get_sell_orders(self, _goods_id, _game):
            if self.failure:
                raise self.failure
            return [{"id": "ok"}]

        def export_cookie_string(self):
            return "session=rotated"

        def close(self):
            pass

    client = BuffClient(
        "session=old", credentials_update_callback=persistence_failure
    )
    client._buyer.close()
    client._buyer = Buyer()
    assert client.get_sell_orders(1) == [{"id": "ok"}]

    original = BuffRateLimited(30)
    client._buyer = Buyer(original)
    with pytest.raises(BuffRateLimited) as exc_info:
        client.get_sell_orders(1)
    assert exc_info.value is original
