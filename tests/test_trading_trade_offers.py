from app.database import PlatformAction
from app.services.trading.adapters import NormalizedResult
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
