import threading
import time

import pytest

from app import pipeline
from app.services import buff_checkout_guard as guard


class _State:
    def enable_sell_only(self):
        self.status["sell_only_enabled"] = True

    def is_stop_requested(self):
        return False

    def __init__(self, **overrides):
        self.status = {
            "status": "idle",
            "step": "",
            "buff_auth_expired": False,
            "buff_verification_required": False,
            "buff_verification_reason": "",
            **overrides,
        }
        self.pending = None
        self.logs = []

    def get_status(self):
        return dict(self.status)

    def set_status(self, status, step="", **_kwargs):
        self.status.update({"status": status, "step": step})

    def set_pending_payment(self, value):
        self.pending = value

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


@pytest.fixture(autouse=True)
def _isolated_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_GUARD_PATH", tmp_path / "checkout.json")
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None
        pipeline._pipeline_maintenance_reason = ""
        pipeline._shutdown_pending = False
    yield
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)
    with pipeline._pipeline_start_lock:
        pipeline._pipeline_thread = None
        pipeline._pipeline_maintenance_reason = ""
        pipeline._shutdown_pending = False


def test_unresolved_checkout_blocks_restart_until_explicit_ack(monkeypatch):
    state = _State()
    ran = threading.Event()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "_run_pipeline", lambda _config: ran.set())
    intent = guard.begin_checkout("single", 123, sell_order_id="sell-1")
    guard.update_checkout(
        stage="order_created_pending",
        order_id="bill-1",
        reason="payment link unavailable",
    )

    assert pipeline.start_pipeline({}) is False
    assert ran.is_set() is False
    blocker = pipeline.get_pipeline_start_blocker()
    assert blocker["code"] == "BUFF_RECONCILIATION_REQUIRED"
    assert blocker["checkout"]["order_id"] == "bill-1"

    assert pipeline.start_pipeline(
        {},
        acknowledge_buff_reconciliation=True,
        buff_reconciliation_intent_id=intent["intent_id"],
    ) is True
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread is not None:
        thread.join(timeout=2)
    assert ran.wait(timeout=2) is True
    assert guard.get_unresolved_checkout() is None


def test_restart_ack_requires_the_exact_displayed_intent_id(monkeypatch):
    state = _State()
    ran = threading.Event()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "_run_pipeline", lambda _config: ran.set())
    intent = guard.begin_checkout("single", 123)

    assert pipeline.start_pipeline(
        {},
        acknowledge_buff_reconciliation=True,
    ) is False
    assert pipeline.start_pipeline(
        {},
        acknowledge_buff_reconciliation=True,
        buff_reconciliation_intent_id="stale-intent",
    ) is False
    assert guard.get_unresolved_checkout()["intent_id"] == intent["intent_id"]
    assert ran.is_set() is False


@pytest.mark.parametrize(
    "status",
    [
        {"buff_auth_expired": True},
        {
            "buff_verification_required": True,
            "buff_verification_reason": "captcha",
        },
    ],
)
def test_pipeline_start_does_not_clear_unverified_auth_flags(monkeypatch, status):
    state = _State(**status)
    ran = threading.Event()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(pipeline, "_run_pipeline", lambda _config: ran.set())

    assert pipeline.start_pipeline({}) is False
    assert ran.is_set() is False
    assert state.status["buff_auth_expired"] is status.get(
        "buff_auth_expired", False
    )
    assert state.status["buff_verification_required"] is status.get(
        "buff_verification_required", False
    )


def test_durable_guard_freezes_credentials_and_background_reads(monkeypatch):
    from app.services import buff_auth, workers

    guard.begin_checkout("batch", 123, quantity=2)
    guard.update_checkout(stage="batch_created_pending", batch_id="batch-1")
    monkeypatch.setattr(
        workers,
        "get_status",
        lambda: {
            "status": "idle",
            "step": "",
            "buff_auth_expired": False,
            "buff_verification_required": False,
        },
    )

    assert "凭证已冻结" in buff_auth.buff_credential_replacement_block_reason()
    assert workers._buff_background_request_is_safe() is False
    assert workers._session_keepalive_is_safe() is False


def test_running_pipeline_freezes_manual_credential_replacement(monkeypatch):
    from app.services import buff_auth

    monkeypatch.setattr(
        buff_auth,
        "get_status",
        lambda: {"status": "running", "step": "CHECKING_STABILITY"},
    )
    monkeypatch.setattr(buff_auth, "get_pending_payment", lambda: None)

    assert "流水线正在运行" in buff_auth.buff_credential_replacement_block_reason()


def test_full_import_rejects_during_atomic_checkout_then_succeeds(monkeypatch):
    from app import pipeline_steps as steps
    from app.routes import config as config_routes
    from app.services import buff_auth

    state = _State()
    monkeypatch.setattr(pipeline, "get_state", lambda: state)
    monkeypatch.setattr(buff_auth, "get_status", state.get_status)
    monkeypatch.setattr(buff_auth, "get_pending_payment", lambda: None)

    identity_read = threading.Event()
    release_identity = threading.Event()
    import_committed = threading.Event()
    checkout_finished = threading.Event()
    events = []
    errors = []

    class BuffClient:
        _pay_method = "alipay"

        def get_credential_identity(self):
            events.append("identity")
            identity_read.set()
            release_identity.wait(timeout=2)
            return {
                "credential_generation": 1,
                "credential_fingerprint": "old-account",
            }

        def lock_and_get_pay_url(self, *_args):
            events.append("post")
            return {
                "success": False,
                "code": "SOLD",
                "created": False,
            }

    kwargs = {
        "target_balance": 100.0,
        "acc": 0.0,
        "set_pending_payment": lambda _value: None,
        "wait_payment_confirm": lambda **_kwargs: False,
        "confirm_payment": lambda _ok: None,
        "is_stop_requested": lambda: False,
        "append_purchase": lambda _record: None,
    }
    item = {
        "name": "Test",
        "steam_market_name": "Test",
        "goods_id": 123,
        "min_price": 10.0,
        "daily_volume": 100,
        "_buff_lowest_price": 10.0,
        "_buff_sell_orders": [{"id": "sell-1", "price": "10.0"}],
        "_buff_sell_orders_fetched_at": time.time(),
    }
    cfg = {
        "buff": {
            "game": "csgo",
            "pay_method": "alipay",
            "price_tolerance": 0.5,
        },
        "pipeline": {"buff_sell_orders_cache_ttl_seconds": 3},
        "_strategy_runtime": {"buy": {"enabled_modules": []}},
    }

    monkeypatch.setattr(config_routes, "load_app_config", lambda: {})
    monkeypatch.setattr(config_routes, "get_all_credentials", lambda: {})
    monkeypatch.setattr(config_routes, "get_purchases", lambda: [])
    monkeypatch.setattr(config_routes, "get_sales", lambda: [])
    monkeypatch.setattr(config_routes, "list_accounts", lambda: [])
    monkeypatch.setattr(config_routes, "get_log", lambda _since=0: [])
    monkeypatch.setattr(
        config_routes,
        "replace_transactions",
        lambda *_args: (
            events.append("import"),
            import_committed.set(),
        ),
    )
    monkeypatch.setattr(config_routes, "replace_log", lambda _data: None)

    def run_checkout():
        try:
            steps.lock_and_confirm_payment(
                BuffClient(),
                item,
                cfg,
                **kwargs,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            checkout_finished.set()

    checkout_thread = threading.Thread(target=run_checkout)
    checkout_thread.start()
    assert identity_read.wait(timeout=2)

    import_result = {}

    def run_import():
        import_result.update(
            config_routes.api_import_full(
                config_routes.ImportFullBody(
                    transactions={"purchases": [], "sales": []}
                )
            )
        )

    import_thread = threading.Thread(target=run_import)
    import_thread.start()
    import_thread.join(timeout=1)
    assert import_thread.is_alive() is False
    assert import_result["ok"] is False
    assert import_result["reconciliation_required"] is True
    assert "BUFF" in import_result["error"]
    assert import_committed.is_set() is False

    release_identity.set()
    checkout_thread.join(timeout=2)
    retry_result = config_routes.api_import_full(
        config_routes.ImportFullBody(
            transactions={"purchases": [], "sales": []}
        )
    )

    assert errors == []
    assert checkout_finished.is_set() is True
    assert retry_result["ok"] is True
    assert import_committed.is_set() is True
    assert events.index("post") < events.index("import")
