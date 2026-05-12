from app.services.steam_buyer import SteamBuyer


def test_query_order_status_matches_active_buy_order(monkeypatch):
    buyer = SteamBuyer(cookie_str="sessionid=sid", session_id="sid")

    monkeypatch.setattr(
        buyer,
        "fetch_active_buy_orders",
        lambda: [{"order_id": "steam-order-1", "market_hash_name": "item", "my_price": 1.23}],
    )

    result = buyer.query_order_status(order_nums=["steam-order-1"])

    assert result["success"] is True
    assert result["data"]["order_id"] == "steam-order-1"
    assert result["data"]["order_status"] == "open"


def test_query_order_status_missing_active_order_stays_pending(monkeypatch):
    buyer = SteamBuyer(cookie_str="sessionid=sid", session_id="sid")
    monkeypatch.setattr(buyer, "fetch_active_buy_orders", lambda: [])

    result = buyer.query_order_status(order_nums=["steam-order-1"])

    assert result["success"] is True
    assert result["data"][0]["order_id"] == "steam-order-1"
    assert result["data"][0]["order_status"] == "pending"
    assert result["data"][0]["missing_from_active_orders"] is True


def test_query_order_status_surfaces_fetch_failure(monkeypatch):
    buyer = SteamBuyer(cookie_str="sessionid=sid", session_id="sid")

    def raise_fetch_error():
        raise RuntimeError("steam unavailable")

    monkeypatch.setattr(buyer, "fetch_active_buy_orders", raise_fetch_error)

    result = buyer.query_order_status(order_nums=["steam-order-1"])

    assert result["success"] is False
    assert "steam unavailable" in result["msg"]


def test_create_listing_posts_sell_item(monkeypatch):
    buyer = SteamBuyer(cookie_str="sessionid=sid; steamLoginSecure=token", session_id="sid")
    calls = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_list_item(session, session_id, appid, contextid, assetid, price, amount=1):
        calls.append(
            {
                "session": session,
                "session_id": session_id,
                "appid": appid,
                "contextid": contextid,
                "assetid": assetid,
                "price": price,
                "amount": amount,
            }
        )
        return {"status_code": 200, "text": '{"success": true, "listingid": "listing-1"}'}

    monkeypatch.setattr(buyer, "_build_requests_session", lambda: FakeSession())
    monkeypatch.setattr("app.services.steam_buyer.list_item", fake_list_item)

    result = buyer.create_listing(assetid="asset-1", price_cents=1234, appid=730, contextid="2", amount=1)

    assert result["success"] is True
    assert result["listing_id"] == "listing-1"
    assert calls == [
        {
            "session": calls[0]["session"],
            "session_id": "sid",
            "appid": 730,
            "contextid": "2",
            "assetid": "asset-1",
            "price": 1234,
            "amount": 1,
        }
    ]


def test_create_listing_requires_sessionid():
    buyer = SteamBuyer(cookie_str="steamLoginSecure=token", session_id="")

    result = buyer.create_listing(assetid="asset-1", price_cents=1234)

    assert result["success"] is False
    assert result["auth_required"] is True
