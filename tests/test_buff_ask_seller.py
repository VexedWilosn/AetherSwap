import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buff import BuffWriteResultUnknown
from buff.buyer import BuffBuyer


def _buyer_with_responses(monkeypatch, responses):
    buyer = object.__new__(BuffBuyer)
    calls = []
    response_iter = iter(responses)

    def make_request(method, url, **kwargs):
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": json.loads(kwargs["data"]),
                "best_effort_write": kwargs.get("best_effort_write"),
            }
        )
        response = next(response_iter)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(buyer, "_make_request", make_request)
    return buyer, calls


def test_all_bill_orders_are_sent_once_and_all_ok_is_success(monkeypatch):
    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [
            {
                "code": "OK",
                "data": {
                    "bill-1": "OK",
                    "bill-2": "OK",
                    "bill-3": "OK",
                },
            }
        ],
    )

    result = buyer.ask_seller_to_send(["bill-1", "bill-2", "bill-3"])

    assert result is True
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["best_effort_write"] is True
    assert calls[0]["payload"] == {
        "bill_orders": ["bill-1", "bill-2", "bill-3"],
        "game": "csgo",
        "steamid": None,
    }


def test_order_ids_are_normalized_and_deduplicated_in_one_request(monkeypatch):
    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [{"code": "OK", "data": {"bill-1": "OK", "bill-2": "OK"}}],
    )

    result = buyer.ask_seller_to_send(
        [" bill-1 ", "", None, "bill-1", 0, "bill-2"],
    )

    assert result is True
    assert len(calls) == 1
    assert calls[0]["payload"]["bill_orders"] == ["bill-1", "bill-2"]


def test_partial_per_order_failure_is_not_complete_success(monkeypatch):
    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [
            {
                "code": "OK",
                "data": {
                    "bill-1": "OK",
                    "bill-2": "Rejected",
                    "bill-3": "OK",
                },
            }
        ],
    )

    result = buyer.ask_seller_to_send(["bill-1", "bill-2", "bill-3"])

    assert result is False
    assert len(calls) == 1
    assert calls[0]["payload"]["bill_orders"] == [
        "bill-1",
        "bill-2",
        "bill-3",
    ]


def test_missing_per_order_status_is_not_complete_success(monkeypatch):
    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [{"code": "OK", "data": {"bill-1": "OK", "bill-3": "OK"}}],
    )

    result = buyer.ask_seller_to_send(["bill-1", "bill-2", "bill-3"])

    assert result is False
    assert len(calls) == 1


def test_top_level_failure_is_not_retried(monkeypatch):
    buyer, calls = _buyer_with_responses(
        monkeypatch,
        [{"code": "Rejected", "error": "request rejected", "data": {}}],
    )

    result = buyer.ask_seller_to_send(["bill-1", "bill-2"])

    assert result is False
    assert len(calls) == 1
    assert calls[0]["payload"]["bill_orders"] == ["bill-1", "bill-2"]


def test_unknown_write_result_is_never_retried(monkeypatch):
    unknown = BuffWriteResultUnknown(
        "timeout after send",
        method="POST",
        url="https://buff.invalid/ask",
    )
    buyer, calls = _buyer_with_responses(monkeypatch, [unknown])

    with pytest.raises(BuffWriteResultUnknown):
        buyer.ask_seller_to_send(["bill-1", "bill-2"])

    assert len(calls) == 1
    assert calls[0]["payload"]["bill_orders"] == ["bill-1", "bill-2"]
