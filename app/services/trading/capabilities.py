from __future__ import annotations

from dataclasses import dataclass, field


CAP_PRICE = "price_snapshot"
CAP_ORDERBOOK = "orderbook"
CAP_DIRECT_BUY = "direct_buy"
CAP_PURCHASE_ORDER = "purchase_order"
CAP_ORDER_STATUS = "order_status"
CAP_INVENTORY = "inventory"
CAP_PLATFORM_LISTING = "platform_listing"
CAP_REPRICE = "reprice_listing"
CAP_CANCEL = "cancel_order"
CAP_DELIVER_ORDER = "deliver_order"
CAP_TRADE_OFFER_POLL = "trade_offer_poll"
CAP_TRADE_OFFER_ACCEPT = "accept_trade_offer"
CAP_STEAM_LISTING = "steam_listing"
CAP_MOBILE_CONFIRM = "mobile_confirm"

STATUS_READY = "ready"
STATUS_PARTIAL = "partial"
STATUS_PLANNED = "planned"
STATUS_MISSING = "missing"


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    status: str
    local_method: str = ""
    reference: str = ""
    required_credentials: tuple[str, ...] = ()
    request_ids: tuple[str, ...] = ()
    response_ids: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class PlatformCapability:
    platform: str
    display_name: str
    capabilities: dict[str, CapabilitySpec] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        spec = self.capabilities.get(capability)
        return bool(spec and spec.status in {STATUS_READY, STATUS_PARTIAL})

    def missing(self) -> dict[str, CapabilitySpec]:
        return {
            key: spec
            for key, spec in self.capabilities.items()
            if spec.status in {STATUS_MISSING, STATUS_PLANNED}
        }


CAPABILITY_REGISTRY: dict[str, PlatformCapability] = {
    "buff": PlatformCapability(
        platform="buff",
        display_name="BUFF",
        capabilities={
            CAP_PRICE: CapabilitySpec(CAP_PRICE, STATUS_READY, "buff.buyer.BuffBuyer.get_price"),
            CAP_ORDERBOOK: CapabilitySpec(CAP_ORDERBOOK, STATUS_READY, "buff.buyer.BuffBuyer.get_sell_orders"),
            CAP_DIRECT_BUY: CapabilitySpec(
                CAP_DIRECT_BUY,
                STATUS_READY,
                "buff.buyer.BuffBuyer.create_buy_order",
                required_credentials=("buff.cookies",),
                request_ids=("goods_id", "price", "num"),
                response_ids=("order_id", "bill_id"),
            ),
            CAP_PURCHASE_ORDER: CapabilitySpec(
                CAP_PURCHASE_ORDER,
                STATUS_PARTIAL,
                "buff.buyer.BuffBuyer.create_buy_order",
                reference="Steamauto BuffApi buy/order flow",
                notes="BUFF buy order can request seller/buyer offer handoff after bill creation.",
            ),
            CAP_TRADE_OFFER_POLL: CapabilitySpec(
                CAP_TRADE_OFFER_POLL,
                STATUS_READY,
                "app.receive_flow.fetch_buff_steam_trade",
                reference="Steamauto plugins/BuffAutoAcceptOffer.py",
                response_ids=("tradeofferid", "assetid", "goods_id"),
            ),
            CAP_TRADE_OFFER_ACCEPT: CapabilitySpec(
                CAP_TRADE_OFFER_ACCEPT,
                STATUS_READY,
                "app.receive_flow.accept_steam_trade_offer",
                required_credentials=("steam.cookies", "steam.session_id"),
                request_ids=("tradeofferid",),
            ),
            CAP_PLATFORM_LISTING: CapabilitySpec(
                CAP_PLATFORM_LISTING,
                STATUS_PLANNED,
                reference="Steamauto BuffApi.create_sell_order/manual_plus",
                request_ids=("assetid", "price", "goods_id"),
            ),
            CAP_REPRICE: CapabilitySpec(
                CAP_REPRICE,
                STATUS_PARTIAL,
                "buff.buyer.BuffBuyer.change_price",
                reference="Steamauto BuffApi.change_price",
                request_ids=("sell_order_id", "price"),
            ),
            CAP_CANCEL: CapabilitySpec(
                CAP_CANCEL,
                STATUS_PARTIAL,
                "buff.buyer.BuffBuyer.cancel_sale",
                reference="Steamauto BuffApi.cancel_sale",
                request_ids=("sell_order_id",),
                notes="Adapter supports BUFF seller-side sell order cancellation; purchase-order cancellation is still separate.",
            ),
            CAP_ORDER_STATUS: CapabilitySpec(CAP_ORDER_STATUS, STATUS_PARTIAL, "buff.buyer.BuffBuyer.query_order_status"),
        },
    ),
    "uuyp": PlatformCapability(
        platform="uuyp",
        display_name="UUYP",
        capabilities={
            CAP_PRICE: CapabilitySpec(CAP_PRICE, STATUS_READY, "uuyp.buyer.UuypBuyer.get_price"),
            CAP_ORDERBOOK: CapabilitySpec(CAP_ORDERBOOK, STATUS_READY, "uuyp.buyer.UuypBuyer.get_template_purchase_order_pc"),
            CAP_DIRECT_BUY: CapabilitySpec(
                CAP_DIRECT_BUY,
                STATUS_READY,
                "uuyp.buyer.UuypBuyer.buy_listing",
                required_credentials=("uuyp.cookies",),
                request_ids=("commodity_no", "price"),
                response_ids=("order_id",),
            ),
            CAP_PURCHASE_ORDER: CapabilitySpec(
                CAP_PURCHASE_ORDER,
                STATUS_READY,
                "uuyp.buyer.UuypBuyer.create_buy_order",
                required_credentials=("uuyp.cookies", "uuyp.deviceId", "uuyp.uk"),
                request_ids=("templateId", "purchasePrice", "purchaseNum"),
                response_ids=("OrderNo", "orderNo"),
            ),
            CAP_PLATFORM_LISTING: CapabilitySpec(
                CAP_PLATFORM_LISTING,
                STATUS_PLANNED,
                reference="Steamauto plugins/UUAutoSellItem.py",
            ),
            CAP_REPRICE: CapabilitySpec(
                CAP_REPRICE,
                STATUS_PARTIAL,
                "uuyp.buyer.UuypBuyer.change_price",
                reference="Steamauto plugins/UUAutoSellItem.py price adjustment flow",
                request_ids=("CommodityId", "Price"),
            ),
            CAP_TRADE_OFFER_ACCEPT: CapabilitySpec(
                CAP_TRADE_OFFER_ACCEPT,
                STATUS_PLANNED,
                reference="Steamauto plugins/UUAutoAcceptOffer.py",
            ),
            CAP_CANCEL: CapabilitySpec(
                CAP_CANCEL,
                STATUS_PARTIAL,
                "uuyp.buyer.UuypBuyer.off_shelf",
                reference="Steamauto uuyoupinapi.off_shelf",
                request_ids=("CommodityId",),
                notes="Adapter uses cancel_order as generic off-shelf rescue for seller listings.",
            ),
        },
    ),
    "eco": PlatformCapability(
        platform="eco",
        display_name="ECO",
        capabilities={
            CAP_PRICE: CapabilitySpec(CAP_PRICE, STATUS_READY, "eco.buyer.EcoBuyer.sell_goods_list"),
            CAP_ORDERBOOK: CapabilitySpec(CAP_ORDERBOOK, STATUS_READY, "eco.buyer.EcoBuyer.sell_goods_list"),
            CAP_DIRECT_BUY: CapabilitySpec(
                CAP_DIRECT_BUY,
                STATUS_READY,
                "eco.buyer.EcoBuyer.create_buy_order",
                required_credentials=("eco_openapi.app_key", "eco_openapi.secret"),
                request_ids=("GoodsNum", "AssetID", "TradeLink"),
                response_ids=("OrderNo", "MerchantNo"),
            ),
            CAP_PURCHASE_ORDER: CapabilitySpec(
                CAP_PURCHASE_ORDER,
                STATUS_READY,
                "eco.buyer.EcoBuyer.create_purchase_order",
                required_credentials=("eco_openapi.app_key", "eco_openapi.secret"),
                request_ids=("HashName", "UnitPrice", "Count"),
                response_ids=("PurchaseId",),
            ),
            CAP_ORDER_STATUS: CapabilitySpec(CAP_ORDER_STATUS, STATUS_READY, "eco.buyer.EcoBuyer.query_order_status"),
            CAP_PLATFORM_LISTING: CapabilitySpec(
                CAP_PLATFORM_LISTING,
                STATUS_PARTIAL,
                "eco.buyer.EcoBuyer.create_listing",
                reference="Steamauto PyECOsteam PublishRentAndSaleGoods",
                request_ids=("SteamId", "AssetId", "SellPrice"),
                response_ids=("GoodsNum", "AssetId"),
                notes="Adapter supports sale-only publish; rent-side fields and mobile confirmation policy are separate.",
            ),
            CAP_REPRICE: CapabilitySpec(
                CAP_REPRICE,
                STATUS_PARTIAL,
                "eco.buyer.EcoBuyer.change_price",
                reference="Steamauto PyECOsteam PublishRentAndSaleGoods publishType=2",
                request_ids=("SteamId", "AssetId", "SellPrice"),
            ),
            CAP_CANCEL: CapabilitySpec(
                CAP_CANCEL,
                STATUS_PARTIAL,
                "eco.buyer.EcoBuyer.off_shelf",
                reference="Steamauto PyECOsteam OffshelfGoods",
                request_ids=("GoodsNum", "AssetId"),
            ),
        },
    ),
    "c5game": PlatformCapability(
        platform="c5game",
        display_name="C5Game",
        capabilities={
            CAP_PRICE: CapabilitySpec(CAP_PRICE, STATUS_PLANNED, reference="Steamauto PyC5Game OpenAPI"),
            CAP_ORDERBOOK: CapabilitySpec(CAP_ORDERBOOK, STATUS_PLANNED, reference="Steamauto PyC5Game OpenAPI"),
            CAP_DIRECT_BUY: CapabilitySpec(
                CAP_DIRECT_BUY,
                STATUS_PLANNED,
                reference="Steamauto PyC5Game OpenAPI",
                required_credentials=("c5game.app_key", "c5game.secret"),
            ),
            CAP_PURCHASE_ORDER: CapabilitySpec(CAP_PURCHASE_ORDER, STATUS_PLANNED, reference="Steamauto PyC5Game OpenAPI"),
            CAP_ORDER_STATUS: CapabilitySpec(
                CAP_ORDER_STATUS,
                STATUS_READY,
                "c5game.client.C5GameClient.query_order_status",
                reference="Steamauto PyC5Game.orderList",
                required_credentials=("c5game.app_key",),
                request_ids=("orderId", "steamId", "status"),
            ),
            CAP_DELIVER_ORDER: CapabilitySpec(
                CAP_DELIVER_ORDER,
                STATUS_READY,
                "c5game.client.C5GameClient.deliver",
                reference="Steamauto PyC5Game.deliver",
                required_credentials=("c5game.app_key",),
                request_ids=("orderId",),
                notes="Requests C5Game to send the seller-side Steam offer for an existing sold order.",
            ),
            CAP_TRADE_OFFER_POLL: CapabilitySpec(
                CAP_TRADE_OFFER_POLL,
                STATUS_READY,
                "c5game.client.C5GameClient.find_trade_offer_id",
                reference="Steamauto plugins/C5AutoAcceptOffer.py delivering order scan",
                request_ids=("orderId", "steamId"),
                response_ids=("offerId",),
            ),
            CAP_TRADE_OFFER_ACCEPT: CapabilitySpec(
                CAP_TRADE_OFFER_ACCEPT,
                STATUS_PLANNED,
                reference="Steamauto plugins/C5AutoAcceptOffer.py",
            ),
        },
    ),
    "steam": PlatformCapability(
        platform="steam",
        display_name="Steam",
        capabilities={
            CAP_PRICE: CapabilitySpec(CAP_PRICE, STATUS_READY, "app.services.steam_client.SteamClient"),
            CAP_ORDERBOOK: CapabilitySpec(CAP_ORDERBOOK, STATUS_READY, "steam.market_orders.get_sell_orders_cny"),
            CAP_STEAM_LISTING: CapabilitySpec(
                CAP_STEAM_LISTING,
                STATUS_READY,
                "app.services.steam_buyer.SteamBuyer.create_listing",
                required_credentials=("steam.cookies", "steam.session_id"),
                request_ids=("assetid", "price_cents"),
                response_ids=("listingid", "assetid"),
            ),
            CAP_DIRECT_BUY: CapabilitySpec(
                CAP_DIRECT_BUY,
                STATUS_READY,
                "app.services.steam_buyer.SteamBuyer.create_buy_order",
                required_credentials=("steam.cookies", "steam.session_id"),
                request_ids=("market_hash_name", "price", "quantity"),
            ),
            CAP_TRADE_OFFER_ACCEPT: CapabilitySpec(
                CAP_TRADE_OFFER_ACCEPT,
                STATUS_READY,
                "app.receive_flow.accept_steam_trade_offer",
                reference="Steamauto steampy.client.accept_trade_offer",
            ),
            CAP_MOBILE_CONFIRM: CapabilitySpec(
                CAP_MOBILE_CONFIRM,
                STATUS_READY,
                "app.steam_confirm.auto_confirm_once",
                reference="Steamauto steampy.confirmation",
                required_credentials=("steam_confirm.identity_secret", "steam_confirm.device_id"),
            ),
            CAP_CANCEL: CapabilitySpec(
                CAP_CANCEL,
                STATUS_PARTIAL,
                "app.services.steam_buyer.SteamBuyer.cancel_buy_order",
                reference="Steamauto steampy.market.cancel_*",
                request_ids=("buy_order_id",),
                notes="Adapter supports Steam buy-order cancellation; Steam listing delist remains in app.steam_delist.",
            ),
        },
    ),
}


def normalize_platform(platform: str) -> str:
    value = str(platform or "").lower().strip()
    aliases = {"c5": "c5game", "youpin": "uuyp", "uuyoupin": "uuyp", "ecosteam": "eco"}
    return aliases.get(value, value)


def get_platform_capabilities(platform: str) -> PlatformCapability:
    key = normalize_platform(platform)
    if key not in CAPABILITY_REGISTRY:
        raise KeyError(f"unknown platform: {platform}")
    return CAPABILITY_REGISTRY[key]


def supports(platform: str, capability: str) -> bool:
    return get_platform_capabilities(platform).supports(capability)


def missing_capabilities(platform: str) -> dict[str, CapabilitySpec]:
    return get_platform_capabilities(platform).missing()
