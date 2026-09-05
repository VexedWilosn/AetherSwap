import json

import pytest
import requests

from steam import client


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.headers.update({"Content-Type": "application/json", **(headers or {})})
    return result


def test_history_can_report_429_without_exposing_response_secrets(monkeypatch):
    raw = response(429, {"cookie": "secret-cookie"}, {"Retry-After": "120"})
    monkeypatch.setattr(client.requests, "get", lambda *_args, **_kwargs: raw)

    with pytest.raises(client.SteamHistoryError) as caught:
        client.fetch_history("Test Item", raise_on_error=True)

    assert caught.value.status_code == 429
    assert caught.value.retry_after == 120
    assert "HTTP 429" in str(caught.value)
    assert "body=object" in str(caught.value)
    assert "application/json" in str(caught.value)
    assert "secret-cookie" not in str(caught.value)
    assert client.fetch_history("Test Item") is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [(None, "unexpected_json"), ([], "unexpected_json"),
     ({"success": False}, "unsuccessful_response"),
     ({"success": True, "prices": "invalid"}, "invalid_prices")],
)
def test_history_reports_invalid_success_responses(monkeypatch, body, reason):
    monkeypatch.setattr(client.requests, "get", lambda *_args, **_kwargs: response(body=body))
    with pytest.raises(client.SteamHistoryError, match=reason):
        client.fetch_history("Test Item", raise_on_error=True)
    assert client.fetch_history("Test Item") is None


def test_history_transport_errors_do_not_leak_credentials(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.exceptions.ProxyError("http://user:secret-password@example.com")

    monkeypatch.setattr(client.requests, "get", fail)
    with pytest.raises(client.SteamHistoryError, match="ProxyError") as caught:
        client.fetch_history("Test Item", raise_on_error=True)
    assert "secret-password" not in str(caught.value)


@pytest.mark.parametrize("return_currency", [False, True])
def test_history_success_contract_is_unchanged(monkeypatch, return_currency):
    prices = [["Aug 01 2026 01: +0", 10.5, "2"]]
    raw = response(body={"success": True, "prices": prices, "price_prefix": "$"})
    sent = []

    def get(_url, **kwargs):
        sent.append(kwargs)
        return raw

    monkeypatch.setattr(client.requests, "get", get)
    result = client.fetch_history("Test Item", return_currency=return_currency)
    assert result == ({"history": prices, "currency": "USD"} if return_currency else prices)
    assert sent[0]["headers"]["Accept-Language"] == "en-US,en;q=0.9"
