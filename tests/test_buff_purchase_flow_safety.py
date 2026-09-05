import time

import pytest

from app import pipeline_steps as steps
from app import pipeline as pipeline_module
from buff import BuffAuthExpired, BuffWriteResultUnknown
from buff.request_policy import BuffRateLimited


@pytest.fixture(autouse=True)
def _isolated_checkout_guard(monkeypatch, tmp_path):
    from app.services import buff_checkout_guard

    monkeypatch.setattr(
        buff_checkout_guard,
        "_GUARD_PATH",
        tmp_path / "buff_checkout_guard.json",
    )


def _config(pay_method="alipay"):
    return {
        "buff": {
            "game": "csgo",
            "pay_method": pay_method,
            "price_tolerance": 0.5,
        },
        "pipeline": {"buff_sell_orders_cache_ttl_seconds": 3},
        "_strategy_runtime": {"buy": {"enabled_modules": []}},
    }


def _item(orders, fetched_at=None):
    return {
        "name": "Test Item",
        "steam_market_name": "Test Item",
        "goods_id": 123,
        "min_price": 10.0,
        "daily_volume": 100,
        "_buff_lowest_price": 10.0,
        "_buff_sell_orders": orders,
        "_buff_sell_orders_fetched_at": time.time() if fetched_at is None else fetched_at,
    }


def _checkout_args(wait_result=False):
    pending = []
    purchases = []
    return {
        "target_balance": 100.0,
        "acc": 0.0,
        "set_pending_payment": pending.append,
        "wait_payment_confirm": lambda **_kwargs: wait_result,
        "confirm_payment": lambda _ok: None,
        "is_stop_requested": lambda: False,
        "append_purchase": purchases.append,
    }, pending, purchases


class _FakeState:
    def __init__(self, *, payment_confirmed=False):
        self.payment_confirmed = payment_confirmed
        self.pending = None
        self.purchases = []
        self.status = ("idle", "")
        self.logs = []

    def clear_stop(self):
        return None

    def is_stop_requested(self):
        return False

    def set_buff_auth_expired(self, _value):
        return None

    def set_buff_verification_required(self, _value, _reason=""):
        return None

    def set_status(self, status, step="", **_kwargs):
        self.status = (status, step)

    def log(self, msg, level="info", category="", flow_id=""):
        self.logs.append((msg, level, category, flow_id))

    def set_pending_payment(self, value):
        self.pending = value

    def wait_payment_confirm(self, **_kwargs):
        return self.payment_confirmed

    def confirm_payment(self, value):
        self.payment_confirmed = bool(value)

    def append_purchase(self, value):
        self.purchases.append(value)


class _FakeContext:
    def __init__(self, state):
        self.state = state
        self.verbose = False

    def is_stop_requested(self):
        return self.state.is_stop_requested()

    def log(self, msg, level="info", category="pipeline"):
        self.state.log(msg, level, category=category)

    def set_status(self, status, step="", **kwargs):
        self.state.set_status(status, step, **kwargs)

    def debug(self, _msg, category="pipeline"):
        return None


def test_realtime_buff_check_runs_only_after_other_candidate_guards(monkeypatch):
    items = [
        {"name": "Rejected", "goods_id": 1, "min_price": 10.0, "daily_volume": 10},
        {"name": "Selected", "goods_id": 2, "min_price": 10.0, "daily_volume": 10},
    ]
    buff_calls = []

    class BuffClient:
        def get_sell_orders(self, goods_id, _game):
            buff_calls.append(goods_id)
            return [{"id": f"order-{goods_id}", "price": "10.0"}]

    config = {
        "buff": {"game": "csgo", "price_tolerance": 0.5},
        "pipeline": {},
        "stability": {"request_interval_seconds": 0},
        "_strategy_runtime": {
            "buy": {"enabled_modules": ["buy.buff_realtime_price"]}
        },
    }
    monkeypatch.setattr(steps, "set_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(steps, "_fetch_steam_sell_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        steps,
        "_passes_custom_buy_modules",
        lambda item, *_args, **_kwargs: item["goods_id"] == 2,
    )

    selected, failed = steps.pick_stable_item(
        items,
        config,
        steam_client=object(),
        analyzer=object(),
        is_stop_requested=lambda: False,
        buff_client=BuffClient(),
    )

    assert selected is items[1]
    assert failed == {1}
    assert buff_calls == [2]
    assert isinstance(selected["_buff_sell_orders_fetched_at"], float)


def test_closed_time_window_blocks_checkout_post_after_validation():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            raise AssertionError("checkout POST must not be called")

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()
    window_checks = iter([True, False])

    result = steps.lock_and_confirm_payment(
        client,
        _item([{"id": "sell-1", "price": "10.0"}]),
        _config(),
        is_time_allowed=lambda: next(window_checks),
        **kwargs,
    )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert result is steps.TIME_WINDOW_CLOSED
    assert client.lock_calls == 0
    assert get_unresolved_checkout() is None


def test_time_window_closing_during_candidate_analysis_skips_checkout(monkeypatch):
    item = _item([{"id": "sell-1", "price": "10.0"}])
    state = _FakeState()
    ctx = _FakeContext(state)
    checkout_calls = []
    window_checks = iter([True, False])

    monkeypatch.setattr(
        pipeline_module,
        "pick_stable_item",
        lambda *_args, **_kwargs: (item, set()),
    )
    monkeypatch.setattr(
        pipeline_module,
        "lock_and_confirm_payment",
        lambda *_args, **_kwargs: checkout_calls.append(True),
    )

    acc, bought, stopped = pipeline_module._process_deals_for_target(
        ctx,
        [item],
        _config(),
        20.0,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
        is_time_allowed=lambda: next(window_checks),
    )

    assert (acc, bought) == (0.0, 0)
    assert stopped is steps.TIME_WINDOW_CLOSED
    assert checkout_calls == []


def test_time_window_closing_after_first_purchase_prevents_second(monkeypatch):
    item = _item([{"id": "sell-1", "price": "10.0"}])
    state = _FakeState()
    ctx = _FakeContext(state)
    checkout_calls = []
    window_checks = iter([True, True, False])

    monkeypatch.setattr(
        pipeline_module,
        "pick_stable_item",
        lambda *_args, **_kwargs: (item, set()),
    )

    def checkout(*_args, **_kwargs):
        checkout_calls.append(True)
        return 10.0

    monkeypatch.setattr(
        pipeline_module,
        "lock_and_confirm_payment",
        checkout,
    )

    acc, bought, stopped = pipeline_module._process_deals_for_target(
        ctx,
        [item],
        _config(),
        30.0,
        0.0,
        0,
        object(),
        object(),
        object(),
        set(),
        set(),
        set(),
        is_time_allowed=lambda: next(window_checks),
    )

    assert (acc, bought) == (10.0, 1)
    assert stopped is steps.TIME_WINDOW_CLOSED
    assert checkout_calls == [True]


def test_stale_buff_cache_is_refreshed_before_locking():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.get_calls = 0
            self.locked_order_ids = []

        def get_sell_orders(self, _goods_id, _game):
            self.get_calls += 1
            return [{"id": "new-order", "price": "10.0"}]

        def lock_and_get_pay_url(self, _game, _goods_id, order_id, _price):
            self.locked_order_ids.append(order_id)
            return {
                "success": False,
                "code": "FAIL",
                "msg": "sold",
                "created": False,
            }

    client = BuffClient()
    item = _item(
        [{"id": "stale-order", "price": "10.0"}],
        fetched_at=time.time() - 10,
    )
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(client, item, _config(), **kwargs)

    assert result is None
    assert client.get_calls == 1
    assert client.locked_order_ids == ["new-order"]
    assert item["_buff_sell_orders"][0]["id"] == "new-order"


def test_single_order_payment_cancel_does_not_lock_again():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def get_sell_orders(self, _goods_id, _game):
            raise AssertionError("fresh cache should be reused")

        def lock_and_get_pay_url(self, _game, _goods_id, _order_id, _price):
            self.lock_calls += 1
            return {
                "success": True,
                "order_id": "bill-1",
                "pay_url": "https://pay.invalid/1",
                "pay_type": "alipay",
            }

        def ask_seller_to_send(self, *_args):
            raise AssertionError("unconfirmed payment must not prompt shipping")

    client = BuffClient()
    kwargs, pending, purchases = _checkout_args(wait_result=False)

    with pytest.raises(steps.PurchaseOrderCreatedPending) as exc_info:
        steps.lock_and_confirm_payment(
            client,
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    assert exc_info.value.order_id == "bill-1"
    assert client.lock_calls == 1
    assert pending[-1] is None
    assert purchases == []


def test_rate_limit_is_not_swallowed_or_retried():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def get_sell_orders(self, _goods_id, _game):
            raise AssertionError("fresh cache should be reused")

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            raise BuffRateLimited(30)

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(BuffRateLimited):
        steps.lock_and_confirm_payment(
            client,
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    assert client.lock_calls == 1


def test_failed_checkout_session_preflight_sends_no_post_or_guard():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.verify_calls = []
            self.lock_calls = 0

        def verify_session(self, game):
            self.verify_calls.append(game)
            return False

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            raise AssertionError("failed session preflight must suppress checkout POST")

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(BuffAuthExpired, match="未发送锁单请求"):
        steps.lock_and_confirm_payment(
            client,
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert client.verify_calls == ["csgo"]
    assert client.lock_calls == 0
    assert get_unresolved_checkout() is None


def test_failed_read_only_buy_preview_sends_no_post_or_guard():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.preview_calls = []
            self.lock_calls = 0

        def verify_session(self, _game):
            return True

        def prepare_single_buy(self, game, goods_id, sell_order_id, price):
            self.preview_calls.append((game, goods_id, sell_order_id, price))
            return {
                "success": False,
                "created": False,
                "code": "PAY_METHOD_UNAVAILABLE",
                "msg": "payment method disabled",
            }

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            raise AssertionError("failed preview must suppress checkout POST")

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item([{"id": "sell-1", "price": "10.0"}]),
        _config(),
        **kwargs,
    )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert result is None
    assert client.preview_calls == [("csgo", 123, "sell-1", "10.0")]
    assert client.lock_calls == 0
    assert get_unresolved_checkout() is None


def test_successful_preview_is_reused_by_the_immediate_checkout_post():
    preview = {"code": "OK", "data": {"pay_methods": []}}

    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.received_preview = None

        def verify_session(self, _game):
            return True

        def prepare_single_buy(self, *_args):
            return {"success": True, "created": False, "preview": preview}

        def lock_and_get_pay_url(self, *_args, preview=None):
            self.received_preview = preview
            return {"success": False, "code": "FAIL", "created": False}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item([{"id": "sell-1", "price": "10.0"}]),
        _config(),
        **kwargs,
    )

    assert result is None
    assert client.received_preview is preview


def test_definitive_login_rejection_resolves_guard_and_sends_one_post():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.verify_calls = 0
            self.locked_order_ids = []

        def verify_session(self, _game):
            self.verify_calls += 1
            return True

        def lock_and_get_pay_url(self, _game, _goods_id, order_id, _price):
            self.locked_order_ids.append(order_id)
            raise BuffAuthExpired(
                "BUFF 明确拒绝请求：登录状态已失效，订单未创建"
            )

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(BuffAuthExpired, match="订单未创建"):
        steps.lock_and_confirm_payment(
            client,
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            _config(),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert client.verify_calls == 1
    assert client.locked_order_ids == ["sell-1"]
    assert get_unresolved_checkout() is None


def test_transport_unknown_write_is_converted_to_terminal_purchase_state():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            raise BuffWriteResultUnknown(
                "socket closed after POST",
                method="POST",
                url="https://buff.invalid/buy",
            )

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(steps.PurchaseWriteResultUnknown, match="socket closed"):
        steps.lock_and_confirm_payment(
            client,
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    assert client.lock_calls == 1


def test_batch_payment_cancel_does_not_fallback_to_single_order():
    class BuffClient:
        _pay_method = "wechat"

        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0
            self.finalize_calls = 0

        def get_sell_orders(self, _goods_id, _game):
            raise AssertionError("fresh cache should be reused")

        def try_batch_buy(self, *_args):
            self.batch_calls += 1
            return {
                "success": True,
                "batch_id": "batch-1",
                "pay_url": "https://pay.invalid/batch-1",
                "total_price": 20.0,
            }

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL", "created": False}

        def batch_buy_find_and_finalize(self, *_args):
            self.finalize_calls += 1
            return []

        def ask_seller_to_send(self, *_args):
            raise AssertionError("unconfirmed payment must not prompt shipping")

    client = BuffClient()
    kwargs, _pending, purchases = _checkout_args(wait_result=False)

    with pytest.raises(steps.PurchaseOrderCreatedPending) as exc_info:
        steps.lock_and_confirm_payment(
            client,
            _item([
                {"id": "sell-1", "price": "10.0"},
                {"id": "sell-2", "price": "10.0"},
            ]),
            _config("wechat"),
            **kwargs,
        )

    assert exc_info.value.batch_id == "batch-1"
    assert client.batch_calls == 1
    assert client.single_calls == 0
    assert client.finalize_calls == 0
    assert purchases == []


def test_unknown_batch_result_does_not_fallback_to_single_order():
    class BuffClient:
        _pay_method = "wechat"

        def __init__(self):
            self.single_calls = 0

        def get_sell_orders(self, _goods_id, _game):
            raise AssertionError("fresh cache should be reused")

        def try_batch_buy(self, *_args):
            return None

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL", "created": False}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(steps.PurchaseWriteResultUnknown) as exc_info:
        steps.lock_and_confirm_payment(
            client,
            _item([
                {"id": "sell-1", "price": "10.0"},
                {"id": "sell-2", "price": "10.0"},
            ]),
            _config("wechat"),
            **kwargs,
        )

    assert client.single_calls == 0


def test_explicitly_unsupported_batch_can_fallback_once():
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def get_sell_orders(self, _goods_id, _game):
            raise AssertionError("fresh cache should be reused")

        def try_batch_buy(self, *_args):
            self.batch_calls += 1
            raise AssertionError("alipay is known not to support batch checkout")

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL", "created": False}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item([
            {"id": "sell-1", "price": "10.0"},
            {"id": "sell-2", "price": "10.0"},
        ]),
        _config("alipay"),
        **kwargs,
    )

    assert result is None
    assert client.batch_calls == 0
    assert client.single_calls == 1


def test_client_without_batch_support_falls_back_to_one_single_post():
    class BuffClient:
        _pay_method = "wechat"
        supports_batch_buy = False

        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def verify_session(self, _game):
            return True

        def try_batch_buy(self, *_args):
            self.batch_calls += 1
            raise AssertionError("disabled batch protocol must never send a POST")

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL", "created": False}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item(
            [
                {"id": "sell-1", "price": "10.0"},
                {"id": "sell-2", "price": "10.0"},
            ]
        ),
        _config("wechat"),
        **kwargs,
    )

    assert result is None
    assert client.batch_calls == 0
    assert client.single_calls == 1


def test_trusted_not_created_batch_response_can_fallback_once():
    class BuffClient:
        _pay_method = "wechat"

        def __init__(self):
            self.batch_calls = 0
            self.single_calls = 0

        def try_batch_buy(self, *_args):
            self.batch_calls += 1
            return {
                "success": False,
                "code": "NOT_SUPPORTED",
                "created": False,
                "safe_to_fallback": True,
            }

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL", "created": False}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item([
            {"id": "sell-1", "price": "10.0"},
            {"id": "sell-2", "price": "10.0"},
        ]),
        _config("wechat"),
        **kwargs,
    )

    assert result is None
    assert client.batch_calls == 1
    assert client.single_calls == 1


def test_unknown_write_result_stops_entire_pipeline_with_explicit_status(monkeypatch):
    item = _item([{"id": "sell-1", "price": "10.0"}])
    state = _FakeState()

    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            return {
                "success": False,
                "code": "UNKNOWN_AFTER_SEND",
                "created": None,
            }

    class ProxyManager:
        def is_proxy_enabled(self):
            return False

    class NetworkChecker:
        def report_success(self):
            return None

    client = BuffClient()
    monkeypatch.setattr(pipeline_module, "get_state", lambda: state)
    monkeypatch.setattr(
        pipeline_module,
        "apply_strategy_to_config",
        lambda cfg, _kind: {
            **cfg,
            "_strategy_runtime": {"buy": {"enabled_modules": []}},
        },
    )
    monkeypatch.setattr(
        pipeline_module,
        "get_buff_credentials",
        lambda: {"cookies": "session=test"},
    )
    monkeypatch.setattr(
        pipeline_module,
        "create_buff_client_from_config",
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(pipeline_module, "get_proxy_manager", ProxyManager)
    monkeypatch.setattr(pipeline_module, "get_network_checker", NetworkChecker)
    monkeypatch.setattr(
        pipeline_module,
        "_fetch_and_filter_deals",
        lambda *_args, **_kwargs: ([item], False),
    )
    monkeypatch.setattr(
        pipeline_module,
        "pick_stable_item",
        lambda *_args, **_kwargs: (item, set()),
    )
    monkeypatch.setattr(pipeline_module, "SteamClient", lambda: object())
    monkeypatch.setattr(pipeline_module, "StabilityAnalyzer", lambda **_kwargs: object())

    pipeline_module._run_pipeline({"pipeline": {"target_balance": 100}})

    assert client.lock_calls == 1
    assert state.status == ("error", "BUFF_WRITE_RESULT_UNKNOWN")
    assert any("停止全部后续购买请求" in entry[0] for entry in state.logs)


def test_purchase_amount_is_counted_when_post_commit_shipping_prompt_is_blocked(monkeypatch):
    item = _item([{"id": "sell-1", "price": "10.0"}])
    state = _FakeState(payment_confirmed=True)
    ctx = _FakeContext(state)

    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            return {
                "success": True,
                "order_id": "bill-committed",
                "pay_url": "https://pay.invalid/committed",
                "pay_type": "alipay",
            }

        def ask_seller_to_send(self, *_args):
            raise BuffRateLimited(30)

    monkeypatch.setattr(
        pipeline_module,
        "pick_stable_item",
        lambda *_args, **_kwargs: (item, set()),
    )
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)
    config = _config()

    acc, bought, stopped = pipeline_module._process_deals_for_target(
        ctx,
        [item],
        config,
        10.0,
        0.0,
        0,
        object(),
        object(),
        BuffClient(),
        set(),
        set(),
        set(),
    )

    assert (acc, bought, stopped) == (10.0, 1, True)
    assert state.status == ("error", "BUFF_RATE_LIMITED")
    assert len(state.purchases) == 1
    assert state.purchases[0]["buff_order_id"] == "bill-committed"
    assert state.pending is None


@pytest.mark.parametrize("legacy_setting", [None, False])
def test_successful_batch_records_identifiers_then_prompts_shipping(monkeypatch, legacy_setting):
    shipping_calls = []

    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-recorded",
                "pay_url": "https://pay.invalid/batch-recorded",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": 10.0, "bill_order_id": "bill-1"},
                {"id": "sell-2", "price": 10.0, "bill_order_id": "bill-2"},
            ]

        def ask_seller_to_send(self, bill_order_ids, game):
            assert len(purchases) == 2
            shipping_calls.append((bill_order_ids, game))
            return True

    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)
    config = _config("wechat")
    if legacy_setting is not None:
        config["buff"]["auto_ask_seller_to_send"] = legacy_setting

    paid = steps.lock_and_confirm_payment(
        BuffClient(),
        _item([
            {"id": "sell-1", "price": "10.0"},
            {"id": "sell-2", "price": "10.0"},
        ]),
        config,
        **kwargs,
    )

    assert paid == 20.0
    assert [record["bill_order_id"] for record in purchases] == ["bill-1", "bill-2"]
    assert all(record["batch_id"] == "batch-recorded" for record in purchases)
    assert [record["buff_sell_order_id"] for record in purchases] == ["sell-1", "sell-2"]
    assert shipping_calls == [(["bill-1", "bill-2"], "csgo")]


def test_partial_batch_finalize_records_committed_items_and_halts(monkeypatch):
    class BuffClient:
        _pay_method = "wechat"

        def __init__(self):
            self.shipping_calls = 0

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-partial",
                "pay_url": "https://pay.invalid/batch-partial",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": 10.0, "bill_order_id": "bill-1"},
            ]

        def ask_seller_to_send(self, *_args):
            self.shipping_calls += 1
            return True

    client = BuffClient()
    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)

    with pytest.raises(steps.PurchaseOrderCreatedPending) as exc_info:
        steps.lock_and_confirm_payment(
            client,
            _item([
                {"id": "sell-1", "price": "10.0"},
                {"id": "sell-2", "price": "10.0"},
            ]),
            _config("wechat"),
            **kwargs,
        )

    assert exc_info.value.batch_id == "batch-partial"
    assert exc_info.value.committed_amount == 10.0
    assert [record["buff_order_id"] for record in purchases] == ["bill-1"]
    assert client.shipping_calls == 0


def test_batch_finalize_unknown_persists_prior_successes_before_halting(monkeypatch):
    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-unknown",
                "pay_url": "https://pay.invalid/batch-unknown",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args):
            exc = BuffWriteResultUnknown(
                "second finalize timed out",
                method="POST",
                url="https://buff.invalid/buy",
            )
            exc.partial_results = [
                {"id": "sell-1", "price": 10.0, "bill_order_id": "bill-1"}
            ]
            raise exc

    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)

    with pytest.raises(steps.PurchaseWriteResultUnknown) as exc_info:
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            _config("wechat"),
            **kwargs,
        )

    assert exc_info.value.batch_id == "batch-unknown"
    assert exc_info.value.committed_amount == 10.0
    assert [record["buff_order_id"] for record in purchases] == ["bill-1"]
    from app.services.buff_checkout_guard import get_unresolved_checkout

    unresolved = get_unresolved_checkout()
    assert unresolved["stage"] == "batch_finalize_unknown"
    assert unresolved["completed_order_ids"] == ["bill-1"]


def test_batch_created_without_pay_url_is_terminal_and_never_falls_back():
    class BuffClient:
        _pay_method = "wechat"

        def __init__(self):
            self.single_calls = 0

        def try_batch_buy(self, *_args):
            return {
                "success": False,
                "code": "CREATED_WITHOUT_PAY_URL",
                "created": True,
                "batch_id": "batch-created",
            }

        def lock_and_get_pay_url(self, *_args):
            self.single_calls += 1
            return {"success": False, "code": "FAIL"}

    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(steps.PurchaseOrderCreatedPending) as exc_info:
        steps.lock_and_confirm_payment(
            client,
            _item([
                {"id": "sell-1", "price": "10.0"},
                {"id": "sell-2", "price": "10.0"},
            ]),
            _config("wechat"),
            **kwargs,
        )

    assert exc_info.value.batch_id == "batch-created"
    assert client.single_calls == 0


def test_single_created_without_pay_url_keeps_order_id_and_never_auto_resumes():
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            return {
                "success": False,
                "code": "CREATED_WITHOUT_PAY_URL",
                "created": True,
                "order_id": "bill-created",
                "msg": "verification required while reading payment link",
            }

    kwargs, _pending, purchases = _checkout_args()

    with pytest.raises(steps.PurchaseOrderCreatedPending) as exc_info:
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    assert exc_info.value.order_id == "bill-created"
    assert purchases == []


def test_cooling_down_has_dedicated_terminal_state():
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            return {"success": False, "code": "COOLING_DOWN"}

    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(steps.PurchaseCoolingDown):
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )


def test_expired_snapshot_immediately_before_write_sends_no_post(monkeypatch):
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.lock_calls = 0

        def lock_and_get_pay_url(self, *_args):
            self.lock_calls += 1
            return {"success": False, "code": "FAIL"}

    freshness = iter((True, True, False))
    monkeypatch.setattr(
        steps,
        "_buff_orders_cache_is_fresh",
        lambda *_args, **_kwargs: next(freshness),
    )
    client = BuffClient()
    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        client,
        _item([{"id": "sell-1", "price": "10.0"}]),
        _config(),
        **kwargs,
    )

    assert result is None
    assert client.lock_calls == 0


def test_invalid_zero_lowest_price_is_rejected_before_quantity_math():
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            raise AssertionError("invalid prices must not reach the write endpoint")

    kwargs, _pending, _purchases = _checkout_args()

    result = steps.lock_and_confirm_payment(
        BuffClient(),
        _item([{"id": "sell-1", "price": "0"}]),
        _config(),
        **kwargs,
    )

    assert result is None


def test_pending_payment_is_cleared_when_wait_callback_raises():
    pending = []

    def fail_wait(**_kwargs):
        raise RuntimeError("wait failed")

    with pytest.raises(RuntimeError, match="wait failed"):
        steps._do_payment_notify_and_wait(
            {"name": "Test Item"},
            {},
            10.0,
            1,
            "https://pay.invalid",
            "alipay",
            "bill-1",
            0.0,
            pending.append,
            fail_wait,
            lambda _ok: None,
            lambda: False,
            None,
        )

    assert pending[-1] is None


def test_unrecognised_non_ok_write_result_stays_unresolved():
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            return {"success": False, "code": "NEW_SERVER_STATE", "msg": "unknown"}

    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(steps.PurchaseWriteResultUnknown):
        exc_info = steps.lock_and_confirm_payment(
            BuffClient(),
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert get_unresolved_checkout()["stage"] == "write_result_unknown"


def test_single_order_id_is_durable_before_payment_link_fetch_can_crash():
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(
            self,
            *_args,
            on_created=None,
        ):
            on_created("bill-before-crash")
            raise SystemExit("simulated hard stop during pay-link GET")

    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(SystemExit):
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item([{"id": "sell-1", "price": "10.0"}]),
            _config(),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    unresolved = get_unresolved_checkout()
    assert unresolved["stage"] == "order_created"
    assert unresolved["order_id"] == "bill-before-crash"


def test_batch_id_is_durable_before_qr_fetch_can_crash():
    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args, on_created=None):
            on_created("batch-before-crash")
            raise SystemExit("simulated hard stop during QR GET")

    kwargs, _pending, _purchases = _checkout_args()

    with pytest.raises(SystemExit):
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            _config("wechat"),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    unresolved = get_unresolved_checkout()
    assert unresolved["stage"] == "batch_created"
    assert unresolved["batch_id"] == "batch-before-crash"


def test_each_batch_bill_id_is_durable_before_the_next_finalize_post():
    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-finalize-crash",
                "pay_url": "https://pay.invalid/batch-finalize-crash",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args, on_match=None):
            first = {
                "id": "sell-1",
                "price": 10.0,
                "bill_order_id": "bill-before-next-post",
            }
            on_match(first, [first])
            raise SystemExit("simulated hard stop before next finalize")

    kwargs, _pending, _purchases = _checkout_args(wait_result=True)

    with pytest.raises(SystemExit):
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            _config("wechat"),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    unresolved = get_unresolved_checkout()
    assert unresolved["completed_order_ids"] == ["bill-before-next-post"]
    assert unresolved["partial_results"][0]["bill_order_id"] == (
        "bill-before-next-post"
    )


def test_all_batch_ids_are_durable_before_local_db_append_failure(monkeypatch):
    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-db-failure",
                "pay_url": "https://pay.invalid/batch-db-failure",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": 10.0, "bill_order_id": "bill-1"},
                {"id": "sell-2", "price": 10.0, "bill_order_id": "bill-2"},
            ]

    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    original_append = kwargs["append_purchase"]

    def fail_second_append(record):
        if purchases:
            raise OSError("simulated SQLite failure")
        original_append(record)

    kwargs["append_purchase"] = fail_second_append
    monkeypatch.setattr(
        steps,
        "_fetch_smart_market_price",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(OSError, match="SQLite"):
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            _config("wechat"),
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    unresolved = get_unresolved_checkout()
    assert unresolved["stage"] == "batch_matches_received"
    assert unresolved["completed_order_ids"] == ["bill-1", "bill-2"]


@pytest.mark.parametrize("legacy_setting", [None, False])
def test_single_purchase_automatically_prompts_shipping_after_recording(monkeypatch, legacy_setting):
    class BuffClient:
        _pay_method = "alipay"

        def __init__(self):
            self.shipping_calls = []

        def lock_and_get_pay_url(self, *_args):
            return {
                "success": True,
                "order_id": "bill-auto-prompt",
                "pay_url": "https://pay.invalid/auto-prompt",
                "pay_type": "alipay",
            }

        def ask_seller_to_send(self, order_id, game):
            assert len(purchases) == 1
            self.shipping_calls.append((order_id, game))
            return True

    client = BuffClient()
    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    config = _config()
    if legacy_setting is not None:
        config["buff"]["auto_ask_seller_to_send"] = legacy_setting
    monkeypatch.setattr(
        steps,
        "_fetch_smart_market_price",
        lambda *_args, **_kwargs: None,
    )

    paid = steps.lock_and_confirm_payment(
        client,
        _item([{"id": "sell-1", "price": "10.0"}]),
        config,
        **kwargs,
    )

    assert paid == 10.0
    assert client.shipping_calls == [("bill-auto-prompt", "csgo")]
    assert len(purchases) == 1


def test_shipping_prompt_unknown_halts_after_recording(monkeypatch):
    class BuffClient:
        _pay_method = "alipay"

        def lock_and_get_pay_url(self, *_args):
            return {
                "success": True,
                "order_id": "bill-shipping-unknown",
                "pay_url": "https://pay.invalid/shipping-unknown",
                "pay_type": "alipay",
            }

        def ask_seller_to_send(self, *_args):
            raise BuffWriteResultUnknown(
                "seller prompt timed out",
                method="POST",
                url="https://buff.invalid/ask",
            )

    config = _config()
    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    monkeypatch.setattr(
        steps,
        "_fetch_smart_market_price",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(steps.PurchaseWriteResultUnknown) as exc_info:
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item([{"id": "sell-1", "price": "10.0"}]),
            config,
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert len(purchases) == 1
    assert exc_info.value.committed_amount == 10.0
    assert get_unresolved_checkout()["stage"] == "shipping_reminder_unknown"


def test_batch_shipping_prompt_unknown_preserves_all_committed_items(monkeypatch):
    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {
                "success": True,
                "batch_id": "batch-shipping-unknown",
                "pay_url": "https://pay.invalid/batch-shipping-unknown",
                "total_price": 20.0,
            }

        def batch_buy_find_and_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": 10.0, "bill_order_id": "bill-1"},
                {"id": "sell-2", "price": 10.0, "bill_order_id": "bill-2"},
            ]

        def ask_seller_to_send(self, *_args):
            raise BuffWriteResultUnknown(
                "batch seller prompt timed out",
                method="POST",
                url="https://buff.invalid/ask",
            )

    config = _config("wechat")
    kwargs, _pending, purchases = _checkout_args(wait_result=True)
    monkeypatch.setattr(
        steps,
        "_fetch_smart_market_price",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(steps.PurchaseWriteResultUnknown) as exc_info:
        steps.lock_and_confirm_payment(
            BuffClient(),
            _item(
                [
                    {"id": "sell-1", "price": "10.0"},
                    {"id": "sell-2", "price": "10.0"},
                ]
            ),
            config,
            **kwargs,
        )

    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert len(purchases) == 2
    assert exc_info.value.committed_amount == 20.0
    assert exc_info.value.committed_orders == 2
    unresolved = get_unresolved_checkout()
    assert unresolved["stage"] == "shipping_reminder_unknown"
    assert unresolved["completed_order_ids"] == ["bill-1", "bill-2"]


def test_alipay_batch_uses_advertised_payment_type_and_never_single_fallback(monkeypatch):
    class BuffClient:
        _pay_method = "alipay"
        batch_pay_methods = ("wechat", "alipay")

        def try_batch_buy(self, *_args, on_created=None):
            on_created("funding-alipay")
            return {"success": True, "batch_id": "funding-alipay",
                    "pay_url": "https://pay.invalid/batch", "pay_type": "alipay",
                    "total_price": 20.0}

        def batch_buy_find_and_finalize(self, *_args, on_match=None):
            from app.services.buff_checkout_guard import get_unresolved_checkout

            rows = []
            for i in (1, 2):
                row = {"id": f"sell-{i}", "price": 10.0, "bill_order_id": f"bill-{i}"}
                rows.append(row)
                on_match(row, rows)
                assert get_unresolved_checkout()["completed_order_ids"] == [
                    match["bill_order_id"] for match in rows]
            return rows

        def ask_seller_to_send(self, order_ids, _game):
            assert len(purchases) == 2
            assert order_ids == ["bill-1", "bill-2"]
            return True

        def lock_and_get_pay_url(self, *_args):
            raise AssertionError("supported Alipay batch must not fall back to single")

    monkeypatch.setattr(steps, "_fetch_smart_market_price", lambda *_args, **_kwargs: None)
    kwargs, pending, purchases = _checkout_args(wait_result=True)
    result = steps.lock_and_confirm_payment(
        BuffClient(), _item([{"id": "sell-1", "price": "10.0"},
                             {"id": "sell-2", "price": "10.0"}]), _config(), **kwargs)
    from app.services.buff_checkout_guard import get_unresolved_checkout

    assert result == 20.0
    assert pending[0]["pay_type"] == "alipay"
    assert get_unresolved_checkout() is None


def test_batch_fallback_log_preserves_actual_preview_reason():
    reason = "wallet upgrade required"

    class BuffClient:
        _pay_method = "wechat"

        def try_batch_buy(self, *_args):
            return {"success": False, "created": False, "code": "NOT_SUPPORTED", "msg": reason}

        def lock_and_get_pay_url(self, *_args):
            return {"success": False, "created": False, "code": "FAIL"}

    kwargs, _pending, _purchases = _checkout_args()
    logs = []
    steps.lock_and_confirm_payment(
        BuffClient(), _item([{"id": "sell-1", "price": "10.0"},
                             {"id": "sell-2", "price": "10.0"}]), _config("wechat"),
        log_fn=lambda msg, _level: logs.append(msg), **kwargs)
    assert any("降级" in msg and reason in msg for msg in logs)


def test_guarded_runner_sets_error_for_unhandled_exception(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr(pipeline_module, "get_state", lambda: state)
    monkeypatch.setattr(
        pipeline_module,
        "_run_pipeline",
        lambda _config: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    pipeline_module._run_pipeline_guarded({})

    assert state.status == ("error", "PIPELINE_UNEXPECTED_ERROR")
    assert state.pending is None
