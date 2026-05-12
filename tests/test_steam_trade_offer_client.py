from app.services.trading.steam_trade_offer_client import SteamTradeOfferClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)


def test_steam_trade_offer_client_normalizes_offer_descriptions():
    session = FakeSession(
        {
            "response": {
                "offer": {
                    "tradeofferid": "offer-1",
                    "trade_offer_state": 2,
                    "items_to_give": [],
                    "items_to_receive": [{"assetid": "asset-1", "classid": "class-1", "instanceid": "inst-1"}],
                },
                "descriptions": [
                    {
                        "classid": "class-1",
                        "instanceid": "inst-1",
                        "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    }
                ],
            }
        }
    )
    client = SteamTradeOfferClient("api-key", session=session)

    result = client.fetch_offer("offer-1")

    assert result["success"] is True
    assert result["offer"]["tradeofferid"] == "offer-1"
    assert result["offer"]["items_to_receive"][0]["assetid"] == "asset-1"
    assert result["offer"]["items_to_receive"][0]["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert session.calls[0]["params"]["key"] == "api-key"
    assert session.calls[0]["params"]["tradeofferid"] == "offer-1"
