import json
import threading
import types

import pytest


@pytest.fixture(autouse=True)
def _isolated_global_buff_policy(monkeypatch):
    import buff

    policy = buff.BuffRequestPolicy(
        min_interval=0.0,
        state_path=None,
        persist=False,
    )
    monkeypatch.setattr(buff, "get_global_policy", lambda: policy)
    return policy


def test_buff_credentials_keep_identity_metadata_and_advance_generation(monkeypatch, tmp_path):
    import config as credential_config

    credentials_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credential_config, "_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(credential_config, "_cache", {})

    credential_config.update_buff_credentials(
        "session=one",
        user_agent="Browser/1.0",
        source="manual",
    )
    first = json.loads(credentials_file.read_text(encoding="utf-8"))["buff"]

    assert first == {
        "cookies": "session=one",
        "user_agent": "Browser/1.0",
        "source": "manual",
        "generation": 1,
    }

    # The legacy one-argument API remains valid and does not change identity.
    credential_config.update_buff_credentials("session=two")
    second = json.loads(credentials_file.read_text(encoding="utf-8"))["buff"]

    assert second["cookies"] == "session=two"
    assert second["user_agent"] == "Browser/1.0"
    assert second["source"] == "manual"
    assert second["generation"] == 2


def test_manual_buff_cookie_saves_stable_user_agent_and_source(monkeypatch):
    from app.routes import auth

    saved = []
    monkeypatch.setattr(auth, "get_buff_credentials", lambda: {})
    monkeypatch.setattr(auth, "manual_buff_probe_allowed", lambda *args: (True, "ok", ""))
    monkeypatch.setattr(auth, "verify_manual_buff_credentials", lambda *args: (True, "ok", "verified"))
    monkeypatch.setattr(auth, "clear_buff_request_policy_after_verification", lambda *args: True)
    monkeypatch.setattr(auth, "update_buff_creds", lambda cookie, **kwargs: saved.append((cookie, kwargs)))
    monkeypatch.setattr(auth, "_maybe_resume_after_buff_cookie_update", lambda: None)

    result = auth.api_auth_manual_cookie(
        "buff",
        auth.ManualCookieBody(
            cookies="session=abc; csrf_token=def",
            user_agent="Browser/1.0\r\nInjected: no",
        ),
    )

    assert result["ok"] is True
    assert saved == [
        (
            "session=abc; csrf_token=def",
            {"user_agent": "Browser/1.0 Injected: no", "source": "manual"},
        )
    ]


def test_manual_buff_cookie_requires_csrf_token(monkeypatch):
    from app.routes import auth

    monkeypatch.setattr(
        auth,
        "verify_manual_buff_credentials",
        lambda *args: (_ for _ in ()).throw(AssertionError("must reject before probing")),
    )

    result = auth.api_auth_manual_cookie(
        "buff",
        auth.ManualCookieBody(cookies="session=abc"),
    )

    assert result["ok"] is False
    assert "csrf_token" in result["error"]


def test_failed_manual_cookie_probe_never_saves_clears_or_resumes(monkeypatch):
    from app.routes import auth

    monkeypatch.setattr(auth, "get_buff_credentials", lambda: {"cookies": "session=old"})
    monkeypatch.setattr(auth, "manual_buff_probe_allowed", lambda *args: (True, "ok", ""))
    monkeypatch.setattr(
        auth,
        "verify_manual_buff_credentials",
        lambda *args: (False, "verification", "安全验证未完成"),
    )
    monkeypatch.setattr(
        auth,
        "update_buff_creds",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not save")),
    )
    monkeypatch.setattr(
        auth,
        "clear_buff_request_policy_after_verification",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not clear")),
    )
    monkeypatch.setattr(
        auth,
        "_maybe_resume_after_buff_cookie_update",
        lambda: (_ for _ in ()).throw(AssertionError("must not resume")),
    )

    result = auth.api_auth_manual_cookie(
        "buff",
        auth.ManualCookieBody(cookies="session=new; csrf_token=csrf"),
    )

    assert result["ok"] is False
    assert result["code"] == "buff_cookie_verification"


def test_manual_probe_uses_one_global_paced_default_account_request(
    monkeypatch,
    _isolated_global_buff_policy,
):
    import buff
    from app.services import buff_auth

    captured = {}

    class Buyer:
        def __init__(self, cookies, **kwargs):
            captured["cookies"] = cookies
            captured.update(kwargs)

        def verify_session(self):
            captured["verify_calls"] = captured.get("verify_calls", 0) + 1
            return True

        def export_cookie_string(self):
            return "session=rotated; csrf_token=rotated"

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(buff, "BuffBuyer", Buyer)

    validated_cookies = []
    result = buff_auth.verify_manual_buff_credentials(
        "session=new; csrf_token=csrf",
        "Browser/1.0",
        validated_cookies.append,
    )

    assert result[0] is True
    assert captured["verify_calls"] == 1
    assert captured["account_id"] == "default"
    assert captured["user_agent"] == "Browser/1.0"
    assert captured["request_policy"] is _isolated_global_buff_policy
    assert validated_cookies == ["session=rotated; csrf_token=rotated"]
    assert captured["closed"] is True


def test_manual_probe_persists_new_rate_limit_to_global_policy(monkeypatch):
    import buff
    from app.services import buff_auth
    from buff.request_policy import account_fingerprint

    calls = []

    class Buyer:
        def __init__(self, *args, **kwargs):
            pass

        def verify_session(self):
            raise buff.BuffRateLimited(90, "limited")

        def close(self):
            pass

    class Policy:
        def trip_rate_limit(self, account_key, retry_after, message):
            calls.append((account_key, retry_after, message))

    monkeypatch.setattr(buff, "BuffBuyer", Buyer)
    monkeypatch.setattr(buff, "get_global_policy", lambda: Policy())

    result = buff_auth.verify_manual_buff_credentials("session=new; csrf_token=csrf")

    assert result[0] is False
    assert result[1] == "rate_limited"
    assert calls[0][0] == account_fingerprint({}, account_id="default")
    assert calls[0][1] == 90


def test_same_manual_cookie_does_not_probe_an_open_circuit(monkeypatch):
    import buff
    from app.services import buff_auth

    class BlockedPolicy:
        def raise_if_blocked(self, _account_key):
            raise buff.BuffVerificationRequired("still blocked")

    monkeypatch.setattr(buff, "get_global_policy", lambda: BlockedPolicy())

    allowed, status, _message = buff_auth.manual_buff_probe_allowed(
        "csrf_token=changed; session=one; irrelevant=new",
        "session=one; csrf_token=two",
    )

    assert allowed is False
    assert status == "browser_verification_required"


def test_changed_manual_cookie_cannot_bypass_verification_circuit(monkeypatch):
    import buff
    from app.services import buff_auth

    class BlockedPolicy:
        def raise_if_blocked(self, _account_key):
            raise buff.BuffVerificationRequired("browser verification required")

    monkeypatch.setattr(buff, "get_global_policy", lambda: BlockedPolicy())

    allowed, status, message = buff_auth.manual_buff_probe_allowed(
        "session=new; csrf_token=new; device-id=new-device",
        "session=old; csrf_token=old; device-id=old-device",
    )

    assert allowed is False
    assert status == "browser_verification_required"
    assert "browser verification required" in message


def test_changed_cookie_still_cannot_bypass_active_rate_limit(monkeypatch):
    import buff
    from app.services import buff_auth

    class RateLimitedPolicy:
        def raise_if_blocked(self, _account_key):
            raise buff.BuffRateLimited(120, "cooldown")

    monkeypatch.setattr(buff, "get_global_policy", lambda: RateLimitedPolicy())

    allowed, status, _message = buff_auth.manual_buff_probe_allowed(
        "session=new; csrf_token=new",
        "session=old; csrf_token=old",
    )

    assert allowed is False
    assert status == "rate_limited"


def test_verified_clear_targets_default_account_only(monkeypatch):
    import buff
    from app.services import buff_auth
    from buff.request_policy import account_fingerprint

    calls = []

    class Policy:
        def raise_if_blocked(self, account_key):
            calls.append(("check", account_key))

        def clear(self, account_key):
            calls.append(("clear", account_key))

    monkeypatch.setattr(buff, "get_global_policy", lambda: Policy())

    assert buff_auth.clear_buff_request_policy_after_verification() is True
    expected = account_fingerprint({}, account_id="default")
    assert calls == [("check", expected), ("clear", expected)]


def test_verified_clear_preserves_unexpired_rate_limit(monkeypatch):
    import buff
    from app.services import buff_auth

    class Policy:
        def raise_if_blocked(self, _account_key):
            raise buff.BuffRateLimited(60, "cooldown")

        def clear(self, _account_key):
            raise AssertionError("active cooldown must not be cleared")

    monkeypatch.setattr(buff, "get_global_policy", lambda: Policy())

    assert buff_auth.clear_buff_request_policy_after_verification() is False


def test_verified_clear_preserves_global_pacing_timestamp(
    _isolated_global_buff_policy,
):
    import buff
    from app.services import buff_auth
    from buff.request_policy import account_fingerprint

    policy = _isolated_global_buff_policy
    account_key = account_fingerprint({}, account_id="default")
    policy._last_started[account_key] = 123.5
    policy.trip_verification(account_key, "verify")

    assert buff_auth.clear_buff_request_policy_after_verification() is True
    assert policy._last_started[account_key] == 123.5
    policy.raise_if_blocked(account_key)


def test_verified_clear_recovers_fail_closed_invalid_policy_state(
    _isolated_global_buff_policy,
):
    from app.services import buff_auth

    policy = _isolated_global_buff_policy
    policy._state_invalid = True

    assert buff_auth.clear_buff_request_policy_after_verification() is True
    assert policy._state_invalid is False


def test_browser_verification_script_has_hard_timeout_and_global_pacing(
    _isolated_global_buff_policy,
):
    from app.services.buff_auth import verify_buff_browser_session
    from buff.request_policy import account_fingerprint

    captured = []

    class Page:
        url = "https://buff.163.com/"

        def title(self):
            return "BUFF"

        def content(self):
            return "market"

        def evaluate(self, script):
            captured.append(script)
            body = (
                '{"code":"OK","data":{"user_info":{"id":"masked"},"meta_list":{}}}'
                if "/account/api/user/info/v2?meta_list=buy_order_state" in script
                else '{"code":"Invalid Argument","error":"Not a valid choice"}'
            )
            return {
                "status": 200,
                "url": "https://buff.163.com/account/api/user/info/v2",
                "body": body,
            }

    assert verify_buff_browser_session(Page())[0] is True
    assert "/account/api/user/info/v2?meta_list=buy_order_state" in captured[0]
    assert "/api/market/buy_order/history" not in captured[0]
    assert "AbortController" in captured[0]
    assert "15000" in captured[0]
    assert "signal: controller.signal" in captured[0]
    assert "retry-after" in captured[0]
    account_key = account_fingerprint({}, account_id="default")
    assert account_key in _isolated_global_buff_policy._last_started


def test_browser_verification_rejects_ok_without_user_info(
    _isolated_global_buff_policy,
):
    from app.services.buff_auth import verify_buff_browser_session

    class Page:
        url = "https://buff.163.com/"

        def title(self):
            return "BUFF"

        def content(self):
            return "market"

        def evaluate(self, _script):
            return {
                "status": 200,
                "url": "https://buff.163.com/account/api/user/info/v2",
                "body": '{"code":"OK","data":{"meta_list":{}}}',
            }

    verified, status, message = verify_buff_browser_session(Page())

    assert verified is False
    assert status == "error"
    assert "用户信息格式异常" in message


@pytest.mark.parametrize("status_code", [403, 412])
def test_browser_verification_trips_global_risk_circuit(
    status_code,
    _isolated_global_buff_policy,
):
    import buff
    from app.services.buff_auth import verify_buff_browser_session
    from buff.request_policy import account_fingerprint

    class Page:
        url = "https://buff.163.com/"

        def evaluate(self, _script):
            return {
                "status": status_code,
                "url": "https://buff.163.com/api/market/buy_order/history",
                "body": '{"code":"RISK"}',
            }

    verified, status, _message = verify_buff_browser_session(Page())

    assert verified is False
    assert status == "verification"
    account_key = account_fingerprint({}, account_id="default")
    with pytest.raises(buff.BuffRiskControlTriggered) as exc_info:
        _isolated_global_buff_policy.raise_if_blocked(account_key)
    assert exc_info.value.status_code == status_code


def test_browser_verification_429_uses_retry_after_and_trips_global_rate_limit(
    _isolated_global_buff_policy,
):
    import buff
    from app.services.buff_auth import verify_buff_browser_session
    from buff.request_policy import account_fingerprint

    class Page:
        url = "https://buff.163.com/"

        def evaluate(self, _script):
            return {
                "status": 429,
                "url": "https://buff.163.com/api/market/buy_order/history",
                "retryAfter": "47",
                "body": '{"code":"TOO_MANY_REQUESTS"}',
            }

    verified, status, _message = verify_buff_browser_session(Page())

    assert verified is False
    assert status == "rate_limited"
    account_key = account_fingerprint({}, account_id="default")
    with pytest.raises(buff.BuffRateLimited) as exc_info:
        _isolated_global_buff_policy.raise_if_blocked(account_key)
    assert 45 <= exc_info.value.retry_after <= 47


def test_duplicate_relogin_start_is_rejected_without_closing_active_context(monkeypatch):
    from app.routes import auth

    class AliveThread:
        def is_alive(self):
            return True

    class Context:
        def close(self):
            raise AssertionError("active context must not be closed cross-thread")

    monkeypatch.setattr(auth, "get_runtime_profile", lambda: types.SimpleNamespace(can_launch_headful_browser=True))
    monkeypatch.setattr(auth, "_relogin_thread", AliveThread())
    monkeypatch.setattr(auth, "_relogin_context", Context())

    result = auth._relogin_start("buff")

    assert result["ok"] is False
    assert result["code"] == "relogin_busy"


def test_active_rate_limit_rejects_browser_relogin_before_thread_start(
    monkeypatch,
    _isolated_global_buff_policy,
):
    from app.routes import auth
    from buff.request_policy import account_fingerprint

    account_key = account_fingerprint({}, account_id="default")
    _isolated_global_buff_policy.trip_rate_limit(account_key, 60, "cooldown")
    monkeypatch.setattr(
        auth,
        "get_runtime_profile",
        lambda: types.SimpleNamespace(can_launch_headful_browser=True),
    )
    monkeypatch.setattr(auth, "_relogin_thread", None)
    monkeypatch.setattr(auth, "_relogin_context", None)
    monkeypatch.setattr(auth, "_relogin_browser", None)
    monkeypatch.setattr(
        auth,
        "buff_credential_replacement_block_reason",
        lambda: "",
    )
    monkeypatch.setattr(
        auth.threading,
        "Thread",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("429 must block before starting Playwright worker")
        ),
    )

    result = auth._relogin_start("buff")

    assert result["ok"] is False
    assert result["code"] == "buff_rate_limited"


def test_checkout_pending_rejects_manual_and_browser_credential_replacement(
    monkeypatch,
):
    from app.routes import auth

    monkeypatch.setattr(
        auth,
        "buff_credential_replacement_block_reason",
        lambda: "checkout pending",
    )
    monkeypatch.setattr(
        auth,
        "verify_manual_buff_credentials",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("manual probe must not run during checkout")
        ),
    )
    manual_result = auth.api_auth_manual_cookie(
        "buff",
        auth.ManualCookieBody(cookies="session=new; csrf_token=new"),
    )

    monkeypatch.setattr(
        auth,
        "get_runtime_profile",
        lambda: types.SimpleNamespace(can_launch_headful_browser=True),
    )
    monkeypatch.setattr(auth, "_relogin_thread", None)
    monkeypatch.setattr(auth, "_relogin_context", None)
    monkeypatch.setattr(auth, "_relogin_browser", None)
    monkeypatch.setattr(
        auth,
        "browser_buff_verification_allowed",
        lambda: (_ for _ in ()).throw(
            AssertionError("checkout must block before browser policy probing")
        ),
    )
    browser_result = auth._relogin_start("buff")

    assert manual_result["code"] == "buff_checkout_pending"
    assert browser_result["code"] == "buff_checkout_pending"


@pytest.mark.parametrize(
    ("step", "pending_payment"),
    [
        ("CHECKOUT_PENDING", None),
        ("BUFF_WRITE_RESULT_UNKNOWN", None),
        ("BUFF_ORDER_CREATED_PENDING", None),
        ("", {"order_id": "bill-1"}),
    ],
)
def test_checkout_state_freezes_buff_credentials(
    monkeypatch,
    step,
    pending_payment,
):
    from app.services import buff_auth

    monkeypatch.setattr(
        buff_auth,
        "get_status",
        lambda: {"status": "error", "step": step},
    )
    monkeypatch.setattr(
        buff_auth,
        "get_pending_payment",
        lambda: pending_payment,
    )

    assert buff_auth.buff_credential_replacement_block_reason()


def test_abandoned_relogin_times_out_and_releases_buff_lock(monkeypatch, tmp_path):
    from app.routes import auth

    class Page:
        def goto(self, *args, **kwargs):
            return None

    class Context:
        pages = [Page()]

        def close(self):
            return None

    class Playwright:
        def stop(self):
            return None

    playwright_pkg = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: Playwright())
    monkeypatch.setitem(__import__("sys").modules, "playwright", playwright_pkg)
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(auth, "_launch_relogin_context", lambda *args: (Context(), None))
    monkeypatch.setattr(auth, "_RELOGIN_TOTAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(auth, "_relogin_ready", threading.Event())
    monkeypatch.setattr(auth, "_relogin_wake", threading.Event())
    monkeypatch.setattr(auth, "_relogin_done", threading.Event())
    monkeypatch.setattr(auth, "_relogin_success", False)
    monkeypatch.setattr(auth, "_relogin_error", None)
    monkeypatch.setattr(auth, "_relogin_context", None)
    monkeypatch.setattr(auth, "_relogin_browser", None)
    monkeypatch.setattr(auth, "_relogin_playwright", None)

    worker = threading.Thread(target=auth._relogin_worker, args=("buff",))
    monkeypatch.setattr(auth, "_relogin_thread", worker)
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False
    assert "超时" in auth._relogin_error
    lock = auth.get_buff_auth_lock()
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_bulk_credentials_import_rebases_generation_and_is_atomic(monkeypatch, tmp_path):
    import config as credential_config

    credentials_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credential_config, "_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(credential_config, "_cache", {})
    credential_config.update_buff_credentials("session=old", source="manual")

    credential_config.save_credentials(
        {"buff": {"cookies": "session=imported", "generation": 1, "source": "manual"}}
    )

    saved = json.loads(credentials_file.read_text(encoding="utf-8"))["buff"]
    assert saved["cookies"] == "session=imported"
    assert saved["generation"] == 2
    assert list(tmp_path.glob(".credentials.json.*.tmp")) == []

    snapshot = credential_config.get_buff()
    snapshot["cookies"] = "mutated-outside-lock"
    assert credential_config.get_buff()["cookies"] == "session=imported"


def test_auto_keepalive_never_opens_profile_for_manual_credentials(monkeypatch):
    from app.services import buff_auth

    monkeypatch.setattr(
        buff_auth,
        "get_buff_credentials",
        lambda: {"cookies": "session=manual", "source": "manual"},
    )

    result = buff_auth._try_buff_auto_relogin_impl()

    assert result[0] is False
    assert result[1] == "external_credentials"


def test_auto_keepalive_does_not_replace_credentials_during_checkout(monkeypatch):
    from app.services import buff_auth

    monkeypatch.setattr(
        buff_auth,
        "get_buff_credentials",
        lambda: {"cookies": "session=old", "source": "playwright"},
    )
    monkeypatch.setattr(
        buff_auth,
        "buff_credential_replacement_block_reason",
        lambda: "checkout pending",
    )
    monkeypatch.setattr(
        buff_auth,
        "manual_buff_probe_allowed",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("checkout must block before circuit probing")
        ),
    )

    result = buff_auth._try_buff_auto_relogin_impl()

    assert result == (False, "checkout_pending", "checkout pending")


def test_auto_keepalive_does_not_probe_an_open_circuit(monkeypatch):
    from app.services import buff_auth

    monkeypatch.setattr(
        buff_auth,
        "get_buff_credentials",
        lambda: {"cookies": "session=old", "source": "playwright"},
    )
    monkeypatch.setattr(
        buff_auth,
        "manual_buff_probe_allowed",
        lambda *args: (False, "rate_limited", "wait"),
    )

    result = buff_auth._try_buff_auto_relogin_impl()

    assert result == (False, "rate_limited", "wait")


def test_challenge_page_is_not_accepted_and_trips_global_verification(
    _isolated_global_buff_policy,
):
    import buff
    from app.services.buff_auth import verify_buff_browser_session
    from buff.request_policy import account_fingerprint

    class ChallengePage:
        url = "https://buff.163.com/verify"

        def title(self):
            return "安全验证"

        def content(self):
            return "<div>请完成验证</div>"

        def evaluate(self, _script):
            raise AssertionError("challenge must be detected before an API request")

    verified, status, message = verify_buff_browser_session(ChallengePage())

    assert verified is False
    assert status == "verification"
    assert "验证" in message
    account_key = account_fingerprint({}, account_id="default")
    with pytest.raises(buff.BuffVerificationRequired):
        _isolated_global_buff_policy.raise_if_blocked(account_key)


def test_inactive_captcha_script_does_not_look_like_active_challenge():
    from app.services.buff_auth import detect_buff_challenge

    class NormalPage:
        url = "https://buff.163.com/"

        def title(self):
            return "BUFF Market"

        def content(self):
            return '<html><script src="/static/captcha.js"></script><body>market</body></html>'

    assert detect_buff_challenge(NormalPage()) == ""


def test_session_keepalive_requires_idle_non_checkout_pipeline(monkeypatch):
    from app.services import workers

    monkeypatch.setattr(workers, "get_status", lambda: {"status": "running", "step": "FILTER"})
    assert workers._session_keepalive_is_safe() is False

    monkeypatch.setattr(workers, "get_status", lambda: {"status": "idle", "step": "CHECKOUT_PENDING"})
    assert workers._session_keepalive_is_safe() is False

    monkeypatch.setattr(workers, "get_status", lambda: {"status": "idle", "step": ""})
    assert workers._session_keepalive_is_safe() is True

    monkeypatch.setattr(
        workers,
        "get_status",
        lambda: {"status": "error", "step": "BUFF_WRITE_RESULT_UNKNOWN"},
    )
    assert workers._session_keepalive_is_safe() is False


def test_buff_session_keepalive_is_opt_in_by_default():
    from app.config_schema import DEFAULTS
    from app.services.workers import _session_keepalive_enabled

    assert DEFAULTS["system"]["buff_session_keepalive_enabled"] is False
    assert _session_keepalive_enabled(DEFAULTS) is False


def test_keepalive_first_wait_uses_full_configured_interval(monkeypatch):
    from app.services import workers

    sleeps = []
    cfg = {
        "system": {
            "buff_session_keepalive_enabled": True,
            "session_keepalive_hours": 2.0,
        }
    }
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: cfg)
    monkeypatch.setattr(workers, "log", lambda *args, **kwargs: None)

    def stop_on_sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop worker")

    monkeypatch.setattr(workers.time, "sleep", stop_on_sleep)

    try:
        workers.session_keepalive_worker()
    except RuntimeError:
        pass

    assert sleeps[0] == 2.0 * 3600
    assert sleeps[0] != 300


@pytest.mark.parametrize(
    ("pay_type", "endpoint", "data", "expected"),
    [
        (
            "alipay",
            "/api/market/bill_order/page_pay",
            {"elements_v2": {"alipay": {"url": "https://pay.example/alipay"}}},
            "https://pay.example/alipay",
        ),
        (
            "wechat",
            "/api/market/bill_order/wx_pay_qrcode",
            {"url": "https://pay.example/wechat"},
            "https://pay.example/wechat",
        ),
    ],
)
def test_browser_payment_url_lookup_uses_profile(
    monkeypatch, pay_type, endpoint, data, expected
):
    from app.services import buff_auth

    calls = []

    class Page:
        def goto(self, url, **kwargs):
            calls.append(("goto", url))

        def evaluate(self, script, payload):
            calls.append(("evaluate", payload))
            return {"status": 200, "text": json.dumps({"code": "OK", "data": data})}

    class Context:
        pages = [Page()]

        def close(self):
            calls.append(("close", ""))

    class Chromium:
        def launch_persistent_context(self, *args, **kwargs):
            calls.append(("launch", kwargs.get("user_agent")))
            return Context()

    class Playwright:
        def __enter__(self):
            return types.SimpleNamespace(chromium=Chromium())

        def __exit__(self, exc_type, exc, tb):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: Playwright()
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(
        buff_auth,
        "get_buff_credentials",
        lambda: {"user_agent": "Test Browser/1.0"},
    )

    result = buff_auth.fetch_buff_payment_url_via_browser(
        "csgo", "order-1", pay_type
    )

    assert result == expected
    assert calls[0] == ("launch", "Test Browser/1.0")
    assert calls[1] == (
        "goto",
        "https://buff.163.com/market/buy_order/history?game=csgo",
    )
    assert calls[2] == (
        "evaluate",
        {"endpoint": endpoint, "orderId": "order-1"},
    )
    assert calls[3] == ("close", "")
