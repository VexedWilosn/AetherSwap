import json

import pytest

from app.pipeline_steps import (
    _affordable_quantity,
    _validate_unique_batch_matches,
)
from app.services.buff_client import BuffClient
from buff import BuffRequestBlocked, BuffWriteResultUnknown
from buff.buyer import BuffBuyer, PAY_METHOD_WECHAT


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("batch_buy_create", (42, 0.29, 3, "csgo")),
        (
            "batch_buy_finalize",
            ("csgo", 42, "sell-1", "0.29", "batch-1"),
        ),
    ],
)
def test_batch_writes_without_preview_or_paid_context_are_blocked(monkeypatch, method_name, args):
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = PAY_METHOD_WECHAT
    calls = []

    def make_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        raise AssertionError("unprepared batch flow must not issue HTTP")

    monkeypatch.setattr(buyer, "_make_request", make_request)

    with pytest.raises(BuffRequestBlocked):
        getattr(buyer, method_name)(*args)

    assert calls == []


def _batch_buyer(monkeypatch, *, pay_method=6, advertised_method=6):
    buyer = object.__new__(BuffBuyer)
    buyer.pay_method = pay_method
    buyer.steam_id = "bound-steam-id"
    buyer._batch_quote = None
    buyer._batch_context = None
    calls = []
    responses = {
        "preview": {"code": "OK", "data": {
            "batch_id": "preview-1", "price": "0.58",
            "pay_methods": [{"value": advertised_method, "btn_clickable": True,
                             "free_password": True, "pay_fee_rate": 0,
                             "passback_params": "server-passback"}],
        }},
        "create": {"code": "OK", "data": {"batch_buy_id": "funding-1"}},
        "wx_pay_qrcode": {"code": "OK", "data": {"url": "weixin://test"}},
        "page_pay": {"code": "OK", "data": {"elements_v2": {
            "alipay": {"url": "https://example.test/payment"}}}},
        "check_state": {"code": "OK", "data": {"state": 2}},
    }

    def request(method, url, **kwargs):
        payload = json.loads(kwargs["data"]) if "data" in kwargs else kwargs.get("params", {})
        name = url.rsplit("/", 1)[-1]
        calls.append((method, name, payload))
        if name == "buy":
            return {"code": "OK", "data": {"id": "bill-" + payload["sell_order_id"]}}
        response = responses[name]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(buyer, "_make_request", request)
    return buyer, calls, responses


def _start_batch(buyer, **kwargs):
    return _client_using(buyer).try_batch_buy(
        42, "csgo", [{"id": "sell-1", "price": "0.29"},
                     {"id": "sell-2", "price": "0.29"}], 0.29, 2, **kwargs,
    )


@pytest.mark.parametrize(("configured", "advertised", "pay_type"),
                         [(6, 6, "wechat"), (51, 49, "alipay"), (51, 10, "alipay")])
def test_client_batch_checkout_uses_preview_funding_and_paid_state(
    monkeypatch, configured, advertised, pay_type,
):
    buyer, calls, _ = _batch_buyer(monkeypatch, pay_method=configured,
                                  advertised_method=advertised)
    created = []
    result = _start_batch(buyer, on_created=lambda batch_id: created.append(
        (batch_id, calls[-1][1])))

    assert result["success"] is True
    assert result["batch_id"] == "funding-1"
    assert result["pay_type"] == pay_type
    assert result["total_price"] == 0.58
    assert created == [("funding-1", "create")]
    assert calls[0][2] == {"game": "csgo", "goods_id": 42,
                           "sell_orders": ["sell-1", "sell-2"],
                           "select_epay": 1, "steamid": "bound-steam-id"}
    assert calls[1][2] == {"game": "csgo", "goods_id": 42,
                           "pay_method": advertised, "frozen_amount": "0.58",
                           "max_price": "0.29", "num": 2,
                           "steamid": "bound-steam-id"}
    persisted = []
    matches = _client_using(buyer).batch_buy_find_and_finalize(
        42, "csgo", 0.29, 2, result["batch_id"],
        on_match=lambda row, rows: persisted.append((row["id"], len(calls))),
    )
    assert len(matches) == 2
    assert [c[1] for c in calls][-3:] == ["check_state", "buy", "buy"]
    assert persisted == [("sell-1", 5), ("sell-2", 6)]
    for _, _, data in calls[-2:]:
        assert data["batch_id"] == "preview-1"
        assert data["batch_buy_id"] == "funding-1"
        assert data["batch"] == 1
        assert data["pay_method"] == advertised
        assert data["passback_params"] == "server-passback"
        assert data["steamid"] == "bound-steam-id"
        assert "password_token" not in data


@pytest.mark.parametrize("state", [1, 3, None, "unknown"])
def test_unpaid_batch_never_finalizes_even_after_user_confirmation(monkeypatch, state):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    _start_batch(buyer)
    responses["check_state"]["data"]["state"] = state
    with pytest.raises(BuffRequestBlocked):
        _client_using(buyer).batch_buy_find_and_finalize(42, "csgo", 0.29, 2, "funding-1")
    assert not any(c[1] == "buy" for c in calls)


@pytest.mark.parametrize("patch", [
    {"btn_clickable": False, "error": "wallet upgrade required"},
    {"free_password": False}, {"free_password": "false"},
    {"free_password": None}, {"pay_fee_rate": 0.01},
    {"sub_pay": {"value": 59}}, {"ejzb_auth": {"required": True}},
])
def test_preview_requirements_block_funding_write(monkeypatch, patch):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["preview"]["data"]["pay_methods"][0].update(patch)
    result = _start_batch(buyer)
    assert result["success"] is False
    assert result["created"] is False
    assert [c[1] for c in calls] == ["preview"]


def test_live_wallet_only_preview_reports_reason_without_spending_balance(monkeypatch):
    buyer, calls, responses = _batch_buyer(monkeypatch, advertised_method=59)
    responses["preview"]["data"]["pay_methods"][0].update(
        name="BUFF funds", btn_clickable=False, error="wallet upgrade required")
    result = _start_batch(buyer)
    assert result["code"] == "NOT_SUPPORTED"
    assert result["created"] is False
    assert "wallet upgrade required" in result["msg"]
    assert len(calls) == 1


@pytest.mark.parametrize("price", ["0.59", "NaN", "-0.58", None])
def test_invalid_or_changed_preview_total_never_creates_batch(monkeypatch, price):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["preview"]["data"]["price"] = price
    result = _start_batch(buyer)
    assert result["success"] is False
    assert result["created"] is False
    assert len(calls) == 1


def test_unknown_create_is_never_safe_to_fallback_or_retried(monkeypatch):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["create"] = BuffWriteResultUnknown("timeout", method="POST")
    with pytest.raises(BuffWriteResultUnknown):
        _start_batch(buyer)
    with pytest.raises(BuffRequestBlocked):
        _start_batch(buyer)
    assert [c[1] for c in calls] == ["preview", "create"]


def test_created_batch_survives_payment_link_failure(monkeypatch):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["wx_pay_qrcode"] = BuffRequestBlocked("blocked")
    created = []
    result = _start_batch(buyer, on_created=created.append)
    assert result["created"] is True
    assert result["success"] is False
    assert result["batch_id"] == "funding-1"
    assert created == ["funding-1"]


def test_missing_funding_id_and_failed_persistence_stop_before_payment(monkeypatch):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["create"]["data"] = {"batch_id": "wrong-id"}
    with pytest.raises(BuffWriteResultUnknown):
        _start_batch(buyer)
    assert len(calls) == 2
    buyer, calls, _ = _batch_buyer(monkeypatch)
    with pytest.raises(BuffWriteResultUnknown) as exc:
        _start_batch(buyer, on_created=lambda _: (_ for _ in ()).throw(OSError()))
    assert exc.value.batch_id == "funding-1"
    assert len(calls) == 2


def test_finalize_binds_quote_and_does_not_retry_same_sell_order(monkeypatch):
    buyer, calls, _ = _batch_buyer(monkeypatch)
    _start_batch(buyer)
    buyer.batch_buy_orders_for_finalize(42, "csgo", 0.29, 2, "funding-1")
    for game, goods, sell, price, batch in [
        ("csgo", 43, "sell-1", "0.29", "funding-1"),
        ("csgo", 42, "other-sell", "0.29", "funding-1"),
        ("csgo", 42, "sell-1", "0.30", "funding-1"),
        ("csgo", 42, "sell-1", "0.29", "preview-1"),
    ]:
        with pytest.raises(BuffRequestBlocked):
            buyer.batch_buy_finalize(game, goods, sell, price, batch)
    assert not any(c[1] == "buy" for c in calls)
    assert buyer.batch_buy_finalize("csgo", 42, "sell-1", "0.29", "funding-1")
    with pytest.raises(BuffRequestBlocked):
        buyer.batch_buy_finalize("csgo", 42, "sell-1", "0.29", "funding-1")
    assert sum(c[1] == "buy" for c in calls) == 1


@pytest.mark.parametrize("change", ["expired", "account", "goods", "price", "quantity"])
def test_prepared_quote_cannot_fund_after_parameters_change(monkeypatch, change):
    buyer, calls, _ = _batch_buyer(monkeypatch)
    assert buyer.prepare_batch_buy(42, "csgo", [
        {"id": "sell-1", "price": "0.29"}, {"id": "sell-2", "price": "0.29"},
    ], 0.29, 2)["success"]
    if change == "expired":
        buyer._batch_quote["prepared_at"] -= 31
    if change == "account":
        buyer.steam_id = "another-account"
    with pytest.raises(BuffRequestBlocked):
        buyer.batch_buy_create(43 if change == "goods" else 42,
                               0.30 if change == "price" else 0.29,
                               3 if change == "quantity" else 2)
    assert len(calls) == 1


def test_finalize_failure_preserves_prior_bill_and_stops_further_posts(monkeypatch):
    buyer, calls, _ = _batch_buyer(monkeypatch)
    _start_batch(buyer)
    request = buyer._make_request

    def fail_second(method, url, **kwargs):
        if url.endswith("/buy") and json.loads(kwargs["data"])["sell_order_id"] == "sell-2":
            raise BuffWriteResultUnknown("finalize timeout", method="POST")
        return request(method, url, **kwargs)

    monkeypatch.setattr(buyer, "_make_request", fail_second)
    saved = []
    with pytest.raises(BuffWriteResultUnknown) as exc:
        _client_using(buyer).batch_buy_find_and_finalize(
            42, "csgo", 0.29, 2, "funding-1", on_match=lambda row, _: saved.append(row))
    assert exc.value.partial_results == saved
    assert saved[0]["bill_order_id"] == "bill-sell-1"
    assert buyer._batch_context["attempted"] == {"sell-1", "sell-2"}
    with pytest.raises(BuffRequestBlocked):
        buyer.batch_buy_finalize("csgo", 42, "sell-2", "0.29", "funding-1")


def test_failed_batch_preview_does_not_claim_a_purchase_was_sent(monkeypatch):
    buyer, calls, responses = _batch_buyer(monkeypatch)
    responses["preview"] = BuffWriteResultUnknown("preview timeout", method="POST")
    with pytest.raises(BuffRequestBlocked, match="未发送创建或购买请求"):
        _start_batch(buyer)
    assert [c[1] for c in calls] == ["preview"]
    assert buyer._batch_context is None


def test_batch_stopped_during_preview_never_creates_funding(monkeypatch):
    buyer, calls, _ = _batch_buyer(monkeypatch)
    result = _start_batch(buyer, can_create=lambda: False)
    assert result["created"] is False
    assert result["code"] == "BATCH_CANCELLED"
    assert [c[1] for c in calls] == ["preview"]


def test_exact_budget_multiple_does_not_plan_one_item_too_few():
    # Binary float division produces 2.9999999999999996 here.
    assert 0.15 / 0.05 < 3
    assert _affordable_quantity(0.15, 0, 0.05) == 3
    assert _affordable_quantity(0.30, 0.10, 0.20) == 1


def _client_using(buyer):
    client = object.__new__(BuffClient)
    client._run = lambda operation: operation(buyer)
    return client


def test_finalize_skips_duplicate_sell_rows_without_sending_duplicate_post():
    class Buyer:
        def __init__(self):
            self.calls = []

        def batch_buy_orders_for_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-2", "price": "0.29"},
            ]

        def batch_buy_finalize(
            self,
            _game,
            _goods_id,
            sell_order_id,
            _price,
            _batch_id,
        ):
            self.calls.append(sell_order_id)
            return f"bill-{sell_order_id}"

    buyer = Buyer()

    matched = _client_using(buyer).batch_buy_find_and_finalize(
        goods_id=42,
        game="csgo",
        max_price=0.29,
        num=2,
        batch_id="batch-1",
    )

    assert buyer.calls == ["sell-1", "sell-2"]
    assert [row["id"] for row in matched] == ["sell-1", "sell-2"]


def test_duplicate_bill_id_halts_with_only_prior_unique_results():
    class Buyer:
        def __init__(self):
            self.calls = []

        def batch_buy_orders_for_finalize(self, *_args):
            return [
                {"id": "sell-1", "price": "0.29"},
                {"id": "sell-2", "price": "0.29"},
                {"id": "sell-3", "price": "0.29"},
            ]

        def batch_buy_finalize(
            self,
            _game,
            _goods_id,
            sell_order_id,
            _price,
            _batch_id,
        ):
            self.calls.append(sell_order_id)
            return "bill-1"

    buyer = Buyer()

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        _client_using(buyer).batch_buy_find_and_finalize(
            goods_id=42,
            game="csgo",
            max_price=0.29,
            num=3,
            batch_id="batch-1",
        )

    assert buyer.calls == ["sell-1", "sell-2"]
    assert exc_info.value.partial_results == [
        {
            "id": "sell-1",
            "price": 0.29,
            "bill_order_id": "bill-1",
        }
    ]


def test_pipeline_rejects_duplicate_ids_before_persisting_complete_batch():
    matches = [
        {"id": "sell-1", "price": 0.29, "bill_order_id": "bill-1"},
        {"id": "sell-2", "price": 0.29, "bill_order_id": "bill-1"},
        {"id": "sell-3", "price": 0.29, "bill_order_id": "bill-3"},
    ]

    with pytest.raises(BuffWriteResultUnknown) as exc_info:
        _validate_unique_batch_matches(matches, "batch-1")

    assert exc_info.value.batch_id == "batch-1"
    assert exc_info.value.partial_results == [matches[0], matches[2]]
