import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buff.buyer import BuffBuyer


def test_wait_order_leave_wait_pay_returns_true_after_order_disappears(monkeypatch):
    buyer = BuffBuyer("csrf_token=test")
    calls = []
    responses = [
        {"code": "OK", "data": {"items": [{"id": "order-1", "state": "WAIT_PAY", "state_text": "待付款"}]}},
        {"code": "OK", "data": {"items": [{"id": "order-1", "state": "WAIT_SEND_OFFER", "state_text": "等待发起报价"}]}},
    ]

    def fake_request(method, url, **kwargs):
        calls.append(kwargs.get("params", {}))
        return responses.pop(0)

    monkeypatch.setattr(buyer, "_make_request", fake_request)
    monkeypatch.setattr("buff.buyer.jittered_sleep", lambda *args, **kwargs: None)

    assert buyer.wait_order_leave_wait_pay("order-1", timeout_seconds=5, interval_seconds=1)
    assert len(calls) == 2
    assert all("state" not in params for params in calls)


def test_wait_order_leave_wait_pay_times_out_while_still_waiting(monkeypatch):
    buyer = BuffBuyer("csrf_token=test")
    monkeypatch.setattr(
        buyer,
        "_make_request",
        lambda method, url, **kwargs: {
            "code": "OK",
            "data": {"items": [{"id": "order-1", "state": "WAIT_PAY", "state_text": "待付款"}]},
        },
    )
    monkeypatch.setattr("buff.buyer.jittered_sleep", lambda *args, **kwargs: None)

    assert not buyer.wait_order_leave_wait_pay("order-1", timeout_seconds=1, interval_seconds=1)


def test_wait_order_leave_wait_pay_returns_false_for_failed_order(monkeypatch):
    buyer = BuffBuyer("csrf_token=test")
    monkeypatch.setattr(
        buyer,
        "_make_request",
        lambda method, url, **kwargs: {
            "code": "OK",
            "data": {"items": [{"id": "order-1", "state": "FAIL", "state_text": "购买失败-已退款"}]},
        },
    )

    assert not buyer.wait_order_leave_wait_pay("order-1", timeout_seconds=5, interval_seconds=1)
