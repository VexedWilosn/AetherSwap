from app.services.trading.platform_adapters import PlatformClientAdapter
from app.services.trading.states import PlatformActionType
from app.database import PlatformAction


class FakePreflight:
    ok = True
    reason = ""
    message = ""


class FakeProvider:
    def __init__(self):
        self.classified = []

    def classify_result(self, result):
        self.classified.append(result)


class FakeFactory:
    def __init__(self, client):
        self.client_obj = client
        self.provider = FakeProvider()
        self.purposes = []

    def client(self, platform, purpose="buy"):
        self.purposes.append((platform, purpose))
        return self.client_obj, FakePreflight(), self.provider


class FakeBuffClient:
    def __init__(self):
        self.calls = []
        self.wait_pay_calls = []
        self.cancel_calls = []
        self.reprice_calls = []

    def create_buy_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "msg": "ok", "order_id": "buff-order-1"}

    def check_wait_pay_orders(self, game="csgo"):
        self.wait_pay_calls.append(game)
        return True

    def cancel_sale(self, sell_orders, **kwargs):
        self.cancel_calls.append((sell_orders, kwargs))
        return {
            "success": True,
            "msg": "cancelled",
            "data": {"cancelled": list(sell_orders), "order_status": "cancelled"},
        }

    def change_price(self, sell_orders, **kwargs):
        self.reprice_calls.append((sell_orders, kwargs))
        return {
            "success": True,
            "msg": "repriced",
            "data": {"changed": [row["sell_order_id"] for row in sell_orders], "order_status": "reprice_submitted"},
        }


class FakeBuffStatusClient(FakeBuffClient):
    def __init__(self):
        super().__init__()
        self.status_calls = []

    def query_order_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"id": "buff-order-1", "state": "success"}}


class FakeUuypStatusClient:
    def __init__(self):
        self.status_calls = []
        self.reprice_calls = []
        self.off_shelf_calls = []

    def query_order_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"OrderNo": "uuyp-order-1", "orderStatus": "pending"}}

    def change_price(self, assets):
        self.reprice_calls.append(assets)
        return {"success": True, "msg": "repriced", "data": {"changed": list(assets), "order_status": "reprice_submitted"}}

    def off_shelf(self, commodity_ids):
        self.off_shelf_calls.append(commodity_ids)
        return {"success": True, "msg": "off shelf", "data": {"cancelled": list(commodity_ids), "order_status": "cancelled"}}


class FakeSteamClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.cancel_calls = []
        self.listing_calls = []

    def fetch_active_buy_orders(self):
        self.calls += 1
        return self.rows

    def cancel_buy_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {"success": True, "msg": "cancelled", "raw": {"success": 1}}

    def create_listing(self, **kwargs):
        self.listing_calls.append(kwargs)
        return {"success": True, "msg": "listed", "listing_id": "listing-1", "assetid": kwargs.get("assetid")}


class FakeSteamStatusClient(FakeSteamClient):
    def __init__(self):
        super().__init__([])
        self.status_calls = []

    def query_order_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"order_id": "steam-order-1", "order_status": "open"}}


class FakeEcoClient:
    def __init__(self):
        self.calls = []
        self.status_calls = []
        self.listing_calls = []
        self.reprice_calls = []
        self.off_shelf_calls = []

    def create_purchase_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"PurchaseId": "eco-purchase-1"}}

    def query_order_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"OrderStatus": "completed", "OrderNo": "eco-order-1"}}

    def create_listing(self, **kwargs):
        self.listing_calls.append(kwargs)
        return {"success": True, "msg": "listed", "platform_listing_id": "eco-goods-1", "assetid": kwargs.get("assetid")}

    def change_price(self, **kwargs):
        self.reprice_calls.append(kwargs)
        return {"success": True, "msg": "repriced", "platform_listing_id": kwargs.get("assetid"), "assetid": kwargs.get("assetid")}

    def off_shelf(self, goods_nums=None, **kwargs):
        self.off_shelf_calls.append((goods_nums, kwargs))
        return {"success": True, "msg": "off shelf", "platform_listing_id": (goods_nums or [""])[0] if goods_nums else None}


class FakeC5Client:
    def __init__(self):
        self.deliver_calls = []
        self.status_calls = []
        self.offer_calls = []

    def deliver(self, order_ids):
        self.deliver_calls.append(order_ids)
        return {"success": True, "msg": "deliver", "data": {"order_id": order_ids[0], "order_status": "delivering"}}

    def query_order_status(self, **kwargs):
        self.status_calls.append(kwargs)
        return {"success": True, "msg": "ok", "data": {"orderId": "c5-order-1", "status": 2}}

    def find_trade_offer_id(self, **kwargs):
        self.offer_calls.append(kwargs)
        return {
            "success": True,
            "msg": "offer ready",
            "data": {"order_id": kwargs["order_id"], "trade_offer_id": "offer-c5-1", "order_status": "pending"},
        }


def test_buff_adapter_submits_direct_buy_with_payload_mapping():
    client = FakeBuffClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("buff", factory=factory)
    action = PlatformAction(
        id=7,
        action_type=PlatformActionType.DIRECT_BUY,
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=100,
        quantity=2,
        request_payload='{"goods_id": 123}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.platform_order_id == "buff-order-1"
    assert client.calls == [{"goods_id": 123, "price": 100.0, "num": 2, "game": "csgo"}]
    assert factory.purposes == [("buff", "direct_buy")]


def test_eco_adapter_submits_purchase_order_with_default_trade_identity():
    client = FakeEcoClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter(
        "eco",
        credentials={"steam": {"trade_link": "https://trade", "steam_id": "7656"}},
        factory=factory,
    )
    action = PlatformAction(
        id=8,
        action_type=PlatformActionType.PURCHASE_ORDER,
        platform="eco",
        item_id=1,
        market_hash_name="M4A1-S | Printstream (Field-Tested)",
        target_price=200,
        quantity=1,
    )

    result = adapter.submit(action)

    assert result.success is True
    assert client.calls[0]["trade_link"] == "https://trade"
    assert client.calls[0]["steam_id"] == "7656"
    assert client.calls[0]["market_hash_name"] == "M4A1-S | Printstream (Field-Tested)"


def test_eco_adapter_polls_order_status_generically():
    client = FakeEcoClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("eco", factory=factory)
    action = PlatformAction(
        id=9,
        action_type=PlatformActionType.POLL_ORDER,
        platform="eco",
        item_id=1,
        market_hash_name="M4A1-S | Printstream (Field-Tested)",
        platform_order_id="eco-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.platform_order_id == "eco-order-1"
    assert result.category == "order_completed"
    assert client.status_calls == [{"order_nums": ["eco-order-1"], "game_id": "730"}]
    assert factory.purposes == [("eco", "order_status")]


def test_buff_adapter_polls_wait_pay_as_pending():
    client = FakeBuffClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("buff", factory=factory)
    action = PlatformAction(
        id=10,
        action_type=PlatformActionType.POLL_ORDER,
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="buff-order-1",
        request_payload='{"game": "csgo"}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_pending"
    assert result.platform_order_id == "buff-order-1"
    assert client.wait_pay_calls == ["csgo"]


def test_buff_adapter_uses_native_status_query_when_available():
    client = FakeBuffStatusClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("buff", factory=factory)
    action = PlatformAction(
        id=12,
        action_type=PlatformActionType.POLL_ORDER,
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="buff-order-1",
        request_payload='{"game": "csgo"}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_completed"
    assert result.platform_order_id == "buff-order-1"
    assert client.status_calls == [{"order_nums": ["buff-order-1"], "game": "csgo"}]
    assert client.wait_pay_calls == []


def test_uuyp_adapter_polls_with_template_id():
    client = FakeUuypStatusClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("uuyp", factory=factory)
    action = PlatformAction(
        id=13,
        action_type=PlatformActionType.POLL_ORDER,
        platform="uuyp",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="uuyp-order-1",
        request_payload='{"template_id": "tpl-1", "game_id": "730"}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_pending"
    assert result.platform_order_id == "uuyp-order-1"
    assert client.status_calls == [{"order_nums": ["uuyp-order-1"], "template_id": "tpl-1", "game_id": "730"}]


def test_steam_adapter_uses_native_status_query_when_available():
    client = FakeSteamStatusClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("steam", factory=factory)
    action = PlatformAction(
        id=14,
        action_type=PlatformActionType.POLL_ORDER,
        platform="steam",
        item_id=1,
        market_hash_name="item",
        platform_order_id="steam-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_pending"
    assert result.platform_order_id == "steam-order-1"
    assert client.status_calls == [{"order_nums": ["steam-order-1"]}]
    assert client.calls == 0


def test_steam_adapter_polls_active_buy_order_as_pending():
    client = FakeSteamClient([{"order_id": "steam-order-1", "market_hash_name": "item", "my_price": 1.23}])
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("steam", factory=factory)
    action = PlatformAction(
        id=11,
        action_type=PlatformActionType.POLL_ORDER,
        platform="steam",
        item_id=1,
        market_hash_name="item",
        platform_order_id="steam-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_pending"
    assert result.platform_order_id == "steam-order-1"
    assert client.calls == 1


def test_steam_adapter_cancels_buy_order():
    client = FakeSteamClient([])
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("steam", factory=factory)
    action = PlatformAction(
        id=15,
        action_type=PlatformActionType.CANCEL_ORDER,
        platform="steam",
        item_id=1,
        market_hash_name="item",
        platform_order_id="steam-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "cancelled"
    assert result.platform_order_id == "steam-order-1"
    assert client.cancel_calls == ["steam-order-1"]
    assert factory.purposes == [("steam", "cancel_order")]


def test_buff_adapter_cancels_sell_order():
    client = FakeBuffClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("buff", factory=factory)
    action = PlatformAction(
        id=16,
        action_type=PlatformActionType.CANCEL_ORDER,
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_listing_id="sell-order-1",
        request_payload='{"game": "csgo"}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "cancelled"
    assert result.platform_listing_id == "sell-order-1"
    assert client.cancel_calls == [(["sell-order-1"], {"game": "csgo"})]
    assert factory.purposes == [("buff", "cancel_order")]


def test_steam_adapter_creates_listing():
    client = FakeSteamClient([])
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("steam", factory=factory)
    action = PlatformAction(
        id=17,
        action_type=PlatformActionType.STEAM_LISTING,
        platform="steam",
        item_id=1,
        market_hash_name="item",
        target_price=123.45,
        assetid="asset-1",
        request_payload='{"price_cents": 12345, "appid": 730, "contextid": "2"}',
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "listing_submitted"
    assert result.assetid == "asset-1"
    assert result.platform_listing_id == "listing-1"
    assert client.listing_calls == [
        {
            "assetid": "asset-1",
            "price": 123.45,
            "price_cents": 12345,
            "appid": 730,
            "contextid": "2",
            "amount": 1,
            "account_currency": "CNY",
        }
    ]
    assert factory.purposes == [("steam", "listing")]


def test_buff_adapter_reprices_listing():
    client = FakeBuffClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("buff", factory=factory)
    action = PlatformAction(
        id=18,
        action_type=PlatformActionType.REPRICE_LISTING,
        platform="buff",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=88.8,
        platform_listing_id="sell-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "reprice_submitted"
    assert result.platform_listing_id == "sell-order-1"
    assert client.reprice_calls == [([{"sell_order_id": "sell-order-1", "price": 88.8, "desc": ""}], {"game": "csgo"})]
    assert factory.purposes == [("buff", "reprice_listing")]


def test_uuyp_adapter_reprices_and_off_shelves_listing():
    client = FakeUuypStatusClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("uuyp", factory=factory)
    reprice = PlatformAction(
        id=19,
        action_type=PlatformActionType.REPRICE_LISTING,
        platform="uuyp",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=77.7,
        platform_listing_id="commodity-1",
    )
    cancel = PlatformAction(
        id=20,
        action_type=PlatformActionType.CANCEL_ORDER,
        platform="uuyp",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_listing_id="commodity-1",
    )

    reprice_result = adapter.submit(reprice)
    cancel_result = adapter.submit(cancel)

    assert reprice_result.success is True
    assert reprice_result.category == "reprice_submitted"
    assert cancel_result.success is True
    assert cancel_result.category == "cancelled"
    assert client.reprice_calls == [{"commodity-1": 77.7}]
    assert client.off_shelf_calls == [["commodity-1"]]
    assert factory.purposes == [("uuyp", "reprice_listing"), ("uuyp", "cancel_order")]


def test_eco_adapter_creates_listing_reprices_and_off_shelves():
    client = FakeEcoClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter(
        "eco",
        credentials={"steam": {"steam_id": "7656"}},
        factory=factory,
    )
    listing = PlatformAction(
        id=21,
        action_type=PlatformActionType.PLATFORM_LISTING,
        platform="eco",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=99.9,
        assetid="asset-eco-1",
        request_payload='{"game_id": "730", "desc": "fast sale"}',
    )
    reprice = PlatformAction(
        id=22,
        action_type=PlatformActionType.REPRICE_LISTING,
        platform="eco",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=88.8,
        platform_listing_id="eco-goods-1",
        assetid="asset-eco-1",
    )
    cancel = PlatformAction(
        id=23,
        action_type=PlatformActionType.CANCEL_ORDER,
        platform="eco",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_listing_id="eco-goods-1",
        assetid="asset-eco-1",
        request_payload='{"game_id": "730"}',
    )

    listing_result = adapter.submit(listing)
    reprice_result = adapter.submit(reprice)
    cancel_result = adapter.submit(cancel)

    assert listing_result.success is True
    assert listing_result.category == "listing_submitted"
    assert listing_result.platform_listing_id == "eco-goods-1"
    assert reprice_result.success is True
    assert reprice_result.category == "reprice_submitted"
    assert reprice_result.platform_listing_id == "eco-goods-1"
    assert cancel_result.success is True
    assert cancel_result.category == "cancelled"
    assert client.listing_calls == [
        {
            "assetid": "asset-eco-1",
            "price": 99.9,
            "steam_id": "7656",
            "game_id": "730",
            "desc": "fast sale",
        }
    ]
    assert client.reprice_calls == [
        {
            "assetid": "asset-eco-1",
            "price": 88.8,
            "steam_id": "7656",
            "game_id": "730",
            "desc": "",
        }
    ]
    assert client.off_shelf_calls == [(["eco-goods-1"], {"assetids": ["asset-eco-1"], "game_id": "730"})]
    assert factory.purposes == [("eco", "listing"), ("eco", "reprice_listing"), ("eco", "cancel_order")]


def test_eco_adapter_reprice_can_use_assetid_without_listing_id():
    client = FakeEcoClient()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("eco", credentials={"steam": {"steam_id": "7656"}}, factory=factory)
    action = PlatformAction(
        id=24,
        action_type=PlatformActionType.REPRICE_LISTING,
        platform="eco",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        target_price=87.6,
        assetid="asset-eco-2",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "reprice_submitted"
    assert result.platform_listing_id == "asset-eco-2"
    assert client.reprice_calls[0]["assetid"] == "asset-eco-2"


def test_c5_adapter_delivers_order_and_polls_order_status():
    client = FakeC5Client()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("c5", credentials={"steam": {"steam_id": "7656"}}, factory=factory)
    deliver = PlatformAction(
        id=25,
        action_type=PlatformActionType.DELIVER_ORDER,
        platform="c5game",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="c5-order-1",
    )
    poll = PlatformAction(
        id=26,
        action_type=PlatformActionType.POLL_ORDER,
        platform="c5game",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="c5-order-1",
        request_payload='{"status": 2}',
    )

    deliver_result = adapter.submit(deliver)
    poll_result = adapter.submit(poll)

    assert deliver_result.success is True
    assert deliver_result.category == "order_pending"
    assert deliver_result.platform_order_id == "c5-order-1"
    assert poll_result.success is True
    assert poll_result.category == "order_pending"
    assert client.deliver_calls == [["c5-order-1"]]
    assert client.status_calls == [{"order_nums": ["c5-order-1"], "steam_id": "7656", "page": 1, "status": 2}]
    assert factory.purposes == [("c5game", "deliver_order"), ("c5game", "order_status")]


def test_c5_adapter_finds_trade_offer_before_accept_step():
    client = FakeC5Client()
    factory = FakeFactory(client)
    adapter = PlatformClientAdapter("c5game", credentials={"steam": {"steam_id": "7656"}}, factory=factory)
    action = PlatformAction(
        id=27,
        action_type=PlatformActionType.ACCEPT_TRADE_OFFER,
        platform="c5game",
        item_id=1,
        market_hash_name="AK-47 | Redline (Field-Tested)",
        platform_order_id="c5-order-1",
    )

    result = adapter.submit(action)

    assert result.success is True
    assert result.category == "order_pending"
    assert result.platform_order_id == "c5-order-1"
    assert result.trade_offer_id == "offer-c5-1"
    assert client.offer_calls == [{"order_id": "c5-order-1", "steam_id": "7656", "page": 1}]
    assert factory.purposes == [("c5game", "trade_offer_poll")]
