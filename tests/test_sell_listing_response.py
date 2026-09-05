from unittest.mock import MagicMock

import pytest


def _entry():
    return {
        "it": {
            "appid": 730,
            "contextid": "2",
            "assetid": "52667017535",
        },
        "list_price": 10.0,
        "reason": "test",
        "price_cents": 1000,
        "name": "AK-47 | Uncharted",
        "aid": "52667017535",
    }


def _context():
    ctx = MagicMock()
    ctx.is_stop_requested.return_value = False
    return ctx


def _messages(ctx):
    return [call.args[0] for call in ctx.log.call_args_list]


@pytest.mark.parametrize(
    ("response", "expected_detail"),
    [
        (None, "无响应"),
        ({"status_code": 200, "text": "null"}, "HTTP 200; body=null"),
        ({"status_code": 200, "text": "[]"}, "HTTP 200; body=[]"),
        ({"status_code": 200, "text": '"ok"'}, 'HTTP 200; body="ok"'),
        (
            {"status_code": 502, "text": "<html>bad gateway</html>"},
            "HTTP 502; body=<html>bad gateway</html>",
        ),
        (["unexpected"], "返回类型异常: list"),
    ],
)
def test_submit_listing_handles_invalid_response_shapes(
    monkeypatch,
    response,
    expected_detail,
):
    from app import sell_pipeline

    ctx = _context()
    recorded = []
    monkeypatch.setattr(sell_pipeline, "list_item", lambda *_args: response)
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(
        sell_pipeline,
        "_record_listing_success",
        lambda *_args: recorded.append(_args),
    )

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    messages = _messages(ctx)
    assert listed == 0
    assert recorded == []
    assert any("上架响应异常" in message for message in messages)
    assert any(expected_detail in message for message in messages)
    assert not any("未捕获异常" in message for message in messages)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"success": False, "message": "pending confirmation"},
        {"success": False, "message": "already have a listing"},
    ],
)
def test_submit_listing_preserves_success_variants(monkeypatch, payload):
    from app import sell_pipeline

    ctx = _context()
    recorded = []
    monkeypatch.setattr(
        sell_pipeline,
        "list_item",
        lambda *_args: {
            "status_code": 200,
            "text": __import__("json").dumps(payload),
        },
    )
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(
        sell_pipeline,
        "_record_listing_success",
        lambda *args: recorded.append(args),
    )

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    assert listed == 1
    assert len(recorded) == 1
    assert not any("未捕获异常" in message for message in _messages(ctx))


def test_submit_listing_retry_null_is_diagnostic_and_not_success(
    monkeypatch,
):
    from app import sell_pipeline

    ctx = _context()
    responses = iter(
        [
            {
                "status_code": 200,
                "text": (
                    '{"success": false, '
                    '"message": "Please wait until your previous action completes"}'
                ),
            },
            {"status_code": 200, "text": "null"},
        ]
    )
    recorded = []
    monkeypatch.setattr(
        sell_pipeline,
        "list_item",
        lambda *_args: next(responses),
    )
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(
        sell_pipeline,
        "_record_listing_success",
        lambda *args: recorded.append(args),
    )

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    messages = _messages(ctx)
    assert listed == 0
    assert recorded == []
    assert any(
        "上架重试响应异常" in message and "HTTP 200; body=null" in message
        for message in messages
    )
    assert not any("未捕获异常" in message for message in messages)


def test_submit_listing_retry_success_is_recorded_once(monkeypatch):
    from app import sell_pipeline

    ctx = _context()
    responses = iter(
        [
            {
                "status_code": 200,
                "text": (
                    '{"success": false, '
                    '"message": "Please wait until your previous action completes"}'
                ),
            },
            {"status_code": 200, "text": '{"success": true}'},
        ]
    )
    recorded = []
    monkeypatch.setattr(
        sell_pipeline,
        "list_item",
        lambda *_args: next(responses),
    )
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(
        sell_pipeline,
        "_record_listing_success",
        lambda *args: recorded.append(args),
    )

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    assert listed == 1
    assert len(recorded) == 1
    assert any("[重试成功]" in message for message in _messages(ctx))


def test_submit_listing_non_string_message_is_safe(monkeypatch):
    from app import sell_pipeline

    ctx = _context()
    monkeypatch.setattr(
        sell_pipeline,
        "list_item",
        lambda *_args: {
            "status_code": 400,
            "text": '{"success": false, "message": {"detail": "bad"}}',
        },
    )
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    messages = _messages(ctx)
    assert listed == 0
    assert any("{'detail': 'bad'}" in message for message in messages)
    assert not any("未捕获异常" in message for message in messages)


@pytest.mark.parametrize(
    "response",
    [
        {"status_code": 200, "text": '{"success": "false"}'},
        {"status_code": 200, "text": '{"success": 1}'},
        {"status_code": 500, "text": '{"success": true}'},
        {
            "status_code": 503,
            "text": '{"message": "pending confirmation"}',
        },
    ],
)
def test_submit_listing_rejects_untrusted_success_shapes(
    monkeypatch,
    response,
):
    from app import sell_pipeline

    ctx = _context()
    recorded = []
    monkeypatch.setattr(sell_pipeline, "list_item", lambda *_args: response)
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    monkeypatch.setattr(
        sell_pipeline,
        "_record_listing_success",
        lambda *args: recorded.append(args),
    )

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    assert listed == 0
    assert recorded == []
    assert any("上架失败" in message for message in _messages(ctx))


def test_listing_response_logs_are_single_line_bounded_and_redacted(
    monkeypatch,
):
    from app import sell_pipeline

    ctx = _context()
    sensitive_body = (
        "bad\r\n"
        "sessionid=secret-session; "
        'access_token="secret-token"; '
        "Authorization: Bearer secret-bearer "
        + ("x" * 500)
    )
    monkeypatch.setattr(
        sell_pipeline,
        "list_item",
        lambda *_args: {"status_code": 502, "text": sensitive_body},
    )
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)

    listed = sell_pipeline._submit_listings(
        ctx,
        [_entry()],
        object(),
        "session-id",
        0,
    )

    response_log = next(
        message for message in _messages(ctx) if "上架响应异常" in message
    )
    assert listed == 0
    assert "\r" not in response_log
    assert "\n" not in response_log
    assert "secret-session" not in response_log
    assert "secret-token" not in response_log
    assert "secret-bearer" not in response_log
    assert "sessionid=***" in response_log
    assert 'access_token="***' in response_log
    assert "Bearer ***" in response_log
    assert len(response_log) < 300


@pytest.mark.parametrize(
    ("raw", "ok", "parsed"),
    [
        ('{"success": true}', True, {"success": True}),
        ('{"success": false}', False, {"success": False}),
        ("null", False, None),
        ("[]", False, None),
        ('"text"', False, None),
        ("<html>bad gateway</html>", False, None),
    ],
)
def test_parse_sell_response_contract(raw, ok, parsed):
    from steam import parse_sell_response

    assert parse_sell_response(raw) == (ok, parsed)


@pytest.mark.parametrize(
    ("status_code", "text", "expected_ok", "expected_response"),
    [
        (200, '{"success": true}', True, {"success": True}),
        (500, '{"success": true}', False, {"success": True}),
        (200, "null", False, None),
    ],
)
def test_list_item_by_name_preserves_safe_response_contract(
    monkeypatch,
    status_code,
    text,
    expected_ok,
    expected_response,
):
    from steam import inventory, market

    asset = {
        "appid": 753,
        "contextid": 6,
        "assetid": "asset-1",
    }
    monkeypatch.setattr(inventory, "fetch_inventory", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        inventory,
        "find_asset_by_name",
        lambda *_args, **_kwargs: asset,
    )
    monkeypatch.setattr(
        market,
        "list_item",
        lambda *_args, **_kwargs: {
            "status_code": status_code,
            "text": text,
        },
    )

    result = market.list_item_by_name(
        object(),
        "steam-id",
        "session-id",
        "Test Item",
        100,
    )

    assert result["ok"] is expected_ok
    assert result["response"] == expected_response
    if text == "null":
        assert result["error"] == "null"


@pytest.mark.parametrize("during_retry", [False, True])
def test_listing_429_stops_batch_and_respects_cooldown(monkeypatch, during_retry):
    from app import sell_pipeline
    from steam.request_policy import MarketCooldown

    now = [1000.0]
    monkeypatch.setattr(sell_pipeline, "_listing_cooldown", MarketCooldown(lambda: now[0]), raising=False)
    limited = {"status_code": 429, "text": "null", "retry_after": "120"}
    pending = {"status_code": 200, "text": '{"success": false, "message": "until your previous action completes"}'}
    send = MagicMock(side_effect=([pending, limited] if during_retry else [limited]))
    recorded = MagicMock()
    monkeypatch.setattr(sell_pipeline, "list_item", send)
    monkeypatch.setattr(sell_pipeline, "_record_listing_success", recorded)
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)
    ctx = _context()
    entries = [_entry(), _entry(), _entry()]

    assert sell_pipeline._submit_listings(ctx, entries, object(), "session-id", 0) == 0
    assert send.call_count == (2 if during_retry else 1)
    recorded.assert_not_called()
    assert any("429" in message and "120" in message for message in _messages(ctx))

    send.reset_mock(side_effect=True)
    send.return_value = {"status_code": 200, "text": '{"success": true}'}
    assert sell_pipeline._submit_listings(ctx, entries, object(), "session-id", 0) == 0
    send.assert_not_called()
    now[0] += 121
    assert sell_pipeline._submit_listings(ctx, [_entry()], object(), "session-id", 0) == 1
    send.assert_called_once()


def test_listing_429_preserves_prior_success_count(monkeypatch):
    from app import sell_pipeline
    from steam.request_policy import MarketCooldown

    monkeypatch.setattr(sell_pipeline, "_listing_cooldown", MarketCooldown(), raising=False)
    send = MagicMock(side_effect=[
        {"status_code": 200, "text": '{"success": true}'},
        {"status_code": 429, "text": '{"success": true}'},
    ])
    recorded = MagicMock()
    monkeypatch.setattr(sell_pipeline, "list_item", send)
    monkeypatch.setattr(sell_pipeline, "_record_listing_success", recorded)
    monkeypatch.setattr(sell_pipeline, "jittered_sleep", lambda *_args: None)

    assert sell_pipeline._submit_listings(_context(), [_entry()] * 3, object(), "session-id", 0) == 1
    assert send.call_count == 2
    recorded.assert_called_once()
