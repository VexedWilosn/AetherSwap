import pytest

from app.database import PlatformAction
import app.services.trading.platform_adapters as platform_adapter_module
from app.services.trading.adapters import (
    RESULT_AUTH_REQUIRED,
    RESULT_FATAL,
    RESULT_MOBILE_CONFIRM_REQUIRED,
    RESULT_OFFER_DECLINED,
    RESULT_OFFER_EXPIRED,
    RESULT_TRANSIENT,
    NormalizedResult,
)
from app.services.trading.platform_adapters import PlatformClientAdapter
from app.services.trading.states import PlatformActionType
from app.services.trading.trade_offers import TradeOfferService


def test_trade_offer_service_rejects_offer_that_requires_our_items():
    service = TradeOfferService()
    validation = service.validate_receive_offer(
        {
            "tradeofferid": "offer-unsafe",
            "items_to_give": [{"assetid": "our-asset"}],
            "items_to_receive": [{"assetid": "their-asset", "market_hash_name": "AK-47 | Redline (Field-Tested)"}],
        },
        expected_names={"AK-47 | Redline (Field-Tested)"},
    )

    assert validation.allowed is False
    assert validation.reason == "offer_requires_our_items"
    assert validation.trade_offer_id == "offer-unsafe"


def test_trade_offer_service_accept_for_action_blocks_unsafe_snapshot_without_callback():
    service = TradeOfferService()
    called = False
    action = PlatformAction(
        id=1,
        action_type="accept_trade_offer",
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-unsafe",
        request_payload=(
            '{"offer":{"tradeofferid":"offer-unsafe",'
            '"items_to_give":[{"assetid":"our-asset"}],'
            '"items_to_receive":[{"assetid":"their-asset","market_hash_name":"AK-47 | Redline (Field-Tested)"}]}}'
        ),
    )

    def accept(_action):
        nonlocal called
        called = True
        return NormalizedResult(True, trade_offer_id="offer-unsafe")

    result = service.accept_for_action(action, accept)

    assert result.success is False
    assert result.category == "unsafe_offer"
    assert result.message == "offer_requires_our_items"
    assert called is False


def test_trade_offer_service_fetches_offer_snapshot_before_accepting():
    class FakeSteamOfferClient:
        def __init__(self):
            self.offer_ids = []

        def fetch_offer(self, offer_id):
            self.offer_ids.append(offer_id)
            return {
                "success": True,
                "offer": {
                    "tradeofferid": offer_id,
                    "items_to_give": [],
                    "items_to_receive": [{"assetid": "asset-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"}],
                },
            }

    client = FakeSteamOfferClient()
    service = TradeOfferService(steam_offer_client=client)
    action = PlatformAction(
        action_type="accept_trade_offer",
        platform="steam",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-safe",
    )

    def accept_callback(received_action):
        return NormalizedResult(True, trade_offer_id=received_action.trade_offer_id)

    result = service.accept_for_action(action, accept_callback)

    assert result.success is True
    assert client.offer_ids == ["offer-safe"]
    assert result.trade_offer_id == "offer-safe"


def test_trade_offer_service_blocks_fetched_unsafe_offer():
    class FakeSteamOfferClient:
        def fetch_offer(self, offer_id):
            return {
                "success": True,
                "offer": {
                    "tradeofferid": offer_id,
                    "items_to_give": [{"assetid": "our-asset"}],
                    "items_to_receive": [{"assetid": "their-asset", "market_hash_name": "AK-47 | Redline (Field-Tested)"}],
                },
            }

    called = False
    service = TradeOfferService(steam_offer_client=FakeSteamOfferClient())
    action = PlatformAction(
        action_type="accept_trade_offer",
        platform="steam",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-unsafe-fetched",
    )

    def accept_callback(received_action):
        nonlocal called
        called = True
        return NormalizedResult(True, trade_offer_id=received_action.trade_offer_id)

    result = service.accept_for_action(action, accept_callback)

    assert called is False
    assert result.success is False
    assert result.category == "unsafe_offer"
    assert result.message == "offer_requires_our_items"


def test_trade_offer_service_blocks_accept_when_configured_offer_fetch_fails():
    class FlakySteamOfferClient:
        def __init__(self):
            self.calls = 0

        def fetch_offer(self, offer_id):
            self.calls += 1
            if self.calls == 1:
                return {"success": False, "reason": "steam_offer_fetch_failed", "msg": "timeout"}
            return {
                "success": True,
                "offer": {
                    "tradeofferid": offer_id,
                    "items_to_give": [],
                    "items_to_receive": [{"assetid": "asset-1", "market_hash_name": "AK-47 | Redline (Field-Tested)"}],
                },
            }

    called = False
    client = FlakySteamOfferClient()
    service = TradeOfferService(steam_offer_client=client)
    action = PlatformAction(
        action_type="accept_trade_offer",
        platform="steam",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-fetch-error",
    )

    def accept_callback(received_action):
        nonlocal called
        called = True
        return NormalizedResult(True, trade_offer_id=received_action.trade_offer_id)

    result = service.accept_for_action(action, accept_callback)

    assert called is False
    assert result.success is False
    assert result.category == "transient_error"
    assert result.message == "steam_offer_fetch_failed"

    result = service.accept_for_action(action, accept_callback)

    assert called is True
    assert result.success is True
    assert client.calls == 2


def test_trade_offer_service_classifies_missing_offer_snapshot_as_expired():
    class MissingSteamOfferClient:
        def fetch_offer(self, offer_id):
            return {"success": False, "reason": "trade_offer_not_found", "msg": "missing"}

    called = False
    service = TradeOfferService(steam_offer_client=MissingSteamOfferClient())
    action = PlatformAction(
        action_type="accept_trade_offer",
        platform="steam",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-missing",
    )

    def accept_callback(received_action):
        nonlocal called
        called = True
        return NormalizedResult(True, trade_offer_id=received_action.trade_offer_id)

    result = service.accept_for_action(action, accept_callback)

    assert called is False
    assert result.success is False
    assert result.category == RESULT_OFFER_EXPIRED
    assert result.message == "trade_offer_not_found"
    assert result.response_payload["reason"] == "trade_offer_not_found"


def test_trade_offer_service_enriches_callback_failure_with_structured_payload():
    service = TradeOfferService()
    action = PlatformAction(
        action_type="accept_trade_offer",
        platform="steam",
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-declined",
    )

    def accept_callback(received_action):
        return NormalizedResult(False, RESULT_FATAL, "offer_declined", trade_offer_id=received_action.trade_offer_id)

    result = service.accept_for_action(action, accept_callback)

    assert result.success is False
    assert result.category == RESULT_OFFER_DECLINED
    assert result.trade_offer_id == "offer-declined"
    assert result.request_payload["trade_offer_id"] == "offer-declined"
    assert result.response_payload["success"] is False
    assert result.response_payload["category"] == RESULT_OFFER_DECLINED
    assert result.response_payload["reason"] == "offer_declined"
    assert result.response_payload["trade_offer_id"] == "offer-declined"


@pytest.mark.parametrize(
    ("reason", "expected_category"),
    [
        ("steam_auth_required", RESULT_AUTH_REQUIRED),
        ("offer_expired", RESULT_OFFER_EXPIRED),
        ("offer_declined", RESULT_OFFER_DECLINED),
        ("mobile_confirm_required", RESULT_MOBILE_CONFIRM_REQUIRED),
        ("transient_error", RESULT_TRANSIENT),
        ("trade_offer_accept_failed", RESULT_FATAL),
    ],
)
def test_platform_adapter_preserves_structured_steam_offer_accept_failure(monkeypatch, reason, expected_category):
    calls = []

    def fake_accept_trade_offer_result(offer_id, cookies):
        calls.append((offer_id, dict(cookies)))
        return {"success": False, "reason": reason, "status_code": 200}

    monkeypatch.setattr(platform_adapter_module, "accept_steam_trade_offer_result", fake_accept_trade_offer_result)
    adapter = PlatformClientAdapter(
        "steam",
        credentials={"steam": {"cookies": "sessionid=sid; steamLoginSecure=secure-token"}},
    )
    action = PlatformAction(
        action_type=PlatformActionType.ACCEPT_TRADE_OFFER,
        platform="steam",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        trade_offer_id="offer-structured",
    )

    result = adapter.accept_trade_offer(action)

    assert result.success is False
    assert result.category == expected_category
    assert result.message == reason
    assert result.trade_offer_id == "offer-structured"
    assert result.response_payload["reason"] == reason
    assert calls == [("offer-structured", {"sessionid": "sid", "steamLoginSecure": "secure-token"})]
