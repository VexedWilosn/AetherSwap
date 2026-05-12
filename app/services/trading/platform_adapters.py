from __future__ import annotations

import json
from typing import Any

from app.database import PlatformAction
from app.receive_flow import accept_steam_trade_offer
from app.services.platform_sessions import PlatformClientFactory
from .adapters import (
    RESULT_CANCELLED,
    RESULT_AUTH_REQUIRED,
    RESULT_FATAL,
    RESULT_LISTING_SUBMITTED,
    RESULT_REPRICE_SUBMITTED,
    RESULT_RISK_BLOCKED,
    RESULT_VALIDATION_ERROR,
    NormalizedResult,
    PlatformAdapterBase,
    normalize_platform_result,
)
from .capabilities import normalize_platform


def _loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _action_payload(action: PlatformAction) -> dict[str, Any]:
    payload = _loads_dict(action.raw_context)
    payload.update(_loads_dict(action.request_payload))
    return payload


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _extract_trade_offer_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("trade_offer_id", "tradeOfferId", "TradeOfferId", "tradeofferid", "offer_id", "offerId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        for key in ("data", "Data", "ResultData", "resultData", "orderConfirmInfoDTO", "raw"):
            nested = payload.get(key)
            if nested is payload:
                continue
            found = _extract_trade_offer_id(nested)
            if found:
                return found
        for key in ("items", "Items", "rows", "Rows", "list", "List"):
            found = _extract_trade_offer_id(payload.get(key))
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_trade_offer_id(item)
            if found:
                return found
    return ""


def _steam_creds(credentials: dict[str, Any]) -> dict[str, Any]:
    data = credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {}
    return data if isinstance(data, dict) else {}


def _steam_cookie_dict(credentials: dict[str, Any]) -> dict[str, str]:
    steam = _steam_creds(credentials)
    raw = str(steam.get("cookies") or steam.get("cookie") or "")
    out: dict[str, str] = {}
    for part in raw.split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key:
            out[key] = val
    session_id = str(steam.get("session_id") or steam.get("sessionid") or out.get("sessionid") or "")
    if session_id:
        out["sessionid"] = session_id
    return out


def _default_trade_link(credentials: dict[str, Any]) -> str:
    for section in ("eco", "eco_openapi", "steam"):
        data = credentials.get(section) if isinstance(credentials.get(section), dict) else {}
        value = data.get("trade_link") or data.get("TradeLink") or data.get("tradeLink")
        if value:
            return str(value).strip()
    return ""


def _default_steam_id(credentials: dict[str, Any]) -> str:
    for section in ("eco", "eco_openapi", "steam"):
        data = credentials.get(section) if isinstance(credentials.get(section), dict) else {}
        value = data.get("steam_id") or data.get("SteamId") or data.get("steamId")
        if value:
            return str(value).strip()
    return ""


def _platform_item_id(action: PlatformAction, payload: dict[str, Any]) -> Any:
    platform = normalize_platform(action.platform)
    if platform == "buff":
        return _first(payload, "goods_id", "buff_goods_id", "platform_item_id", "platform_id")
    if platform == "uuyp":
        return _first(payload, "template_id", "templateId", "uuyp_template_id", "goods_id", "platform_item_id")
    if platform == "eco":
        return _first(payload, "goods_id", "goods_num", "GoodsNum", "eco_goods_id", "platform_item_id")
    return _first(payload, "platform_item_id", "goods_id")


def _listing_id(action: PlatformAction, payload: dict[str, Any]) -> str:
    return str(
        action.platform_listing_id
        or _first(payload, "platform_listing_id", "listing_id", "sell_order_id", "sellOrderId", "commodity_id", "commodityId")
        or ""
    ).strip()


def _steam_result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    success = bool(getattr(result, "success", False))
    return {
        "success": success,
        "msg": str(getattr(result, "msg", "") or ""),
        "raw": getattr(result, "raw", None),
    }


class PlatformClientAdapter(PlatformAdapterBase):
    def __init__(
        self,
        platform: str,
        *,
        credentials: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        factory: PlatformClientFactory | None = None,
    ):
        self.platform = normalize_platform(platform)
        self.credentials = credentials or {}
        self.config = config or {}
        self.factory = factory or PlatformClientFactory(credentials=self.credentials, config=self.config)

    def _client_or_result(self, purpose: str):
        try:
            client, preflight, provider = self.factory.client(self.platform, purpose=purpose)
        except Exception as exc:
            return None, None, NormalizedResult(False, RESULT_FATAL, str(exc))
        if not preflight.ok or client is None:
            category = RESULT_RISK_BLOCKED if preflight.reason == "risk_cooldown" else RESULT_AUTH_REQUIRED
            return None, provider, NormalizedResult(False, category, preflight.message or preflight.reason)
        return client, provider, None

    def _normalize(
        self,
        provider: Any,
        result: dict[str, Any] | NormalizedResult | None,
        *,
        expected_order_id: str = "",
    ) -> NormalizedResult:
        if isinstance(result, dict) and provider is not None:
            try:
                provider.classify_result(result)
            except Exception:
                pass
        normalized = normalize_platform_result(
            result,
            platform=self.platform,
            expected_order_id=expected_order_id,
        )
        if normalized.request_payload is None:
            normalized.request_payload = {}
        return normalized

    def create_direct_buy(self, action: PlatformAction) -> NormalizedResult:
        return self._create_buy_like(action, purchase_order=False)

    def create_purchase_order(self, action: PlatformAction) -> NormalizedResult:
        return self._create_buy_like(action, purchase_order=True)

    def create_steam_buy_order(self, action: PlatformAction) -> NormalizedResult:
        return self._create_buy_like(action, purchase_order=True)

    def create_listing(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        price = float(action.target_price or _first(payload, "price", "list_price", "listing_price") or 0)
        client, provider, preflight_error = self._client_or_result("listing")
        if preflight_error is not None:
            return preflight_error

        try:
            if self.platform == "steam" and hasattr(client, "create_listing"):
                assetid = str(action.assetid or _first(payload, "assetid", "asset_id", "AssetId") or "")
                if not assetid:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "steam assetid is required")
                result = client.create_listing(
                    assetid=assetid,
                    price=price if price > 0 else None,
                    price_cents=_first(payload, "price_cents", "priceCents"),
                    appid=int(payload.get("appid") or payload.get("app_id") or 730),
                    contextid=str(payload.get("contextid") or payload.get("context_id") or "2"),
                    amount=max(1, int(action.quantity or payload.get("amount") or 1)),
                    account_currency=str(payload.get("account_currency") or payload.get("currency") or "CNY"),
                )
                return self._lifecycle_result(
                    result,
                    provider,
                    category=RESULT_LISTING_SUBMITTED,
                    platform_listing_id=str(result.get("listing_id") or "") or None if isinstance(result, dict) else None,
                    assetid=assetid,
                    request_payload={"assetid": assetid, "price": price, "platform": self.platform},
                )

            if self.platform == "eco" and hasattr(client, "create_listing"):
                assetid = str(action.assetid or _first(payload, "assetid", "asset_id", "AssetId") or "")
                steam_id = str(_first(payload, "steam_id", "SteamId", "steamId") or _default_steam_id(self.credentials))
                if not assetid:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "eco assetid is required")
                if not steam_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "eco steam_id is required")
                result = client.create_listing(
                    assetid=assetid,
                    price=price,
                    steam_id=steam_id,
                    game_id=str(payload.get("game_id") or payload.get("gameId") or "730"),
                    desc=str(payload.get("desc") or payload.get("description") or ""),
                )
                return self._lifecycle_result(
                    result,
                    provider,
                    category=RESULT_LISTING_SUBMITTED,
                    platform_listing_id=str(result.get("platform_listing_id") or "") or None if isinstance(result, dict) else None,
                    assetid=assetid,
                    request_payload={
                        "assetid": assetid,
                        "price": price,
                        "steam_id": steam_id,
                        "platform": self.platform,
                    },
                )

            return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} listing is not implemented")
        except Exception as exc:
            return NormalizedResult(False, RESULT_FATAL, str(exc))

    def change_price(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        listing_id = _listing_id(action, payload)
        eco_assetid = str(action.assetid or _first(payload, "assetid", "asset_id", "AssetId") or "")
        if self.platform == "eco" and not listing_id and eco_assetid:
            listing_id = eco_assetid
        price = float(action.target_price or _first(payload, "price", "new_price", "target_price") or 0)
        if not listing_id:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "platform_listing_id is required")
        if price <= 0:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "target_price must be greater than 0")
        client, provider, preflight_error = self._client_or_result("reprice_listing")
        if preflight_error is not None:
            return preflight_error

        try:
            if self.platform == "buff" and hasattr(client, "change_price"):
                result = client.change_price(
                    [{"sell_order_id": listing_id, "price": price, "desc": str(payload.get("desc") or "")}],
                    game=str(payload.get("game") or "csgo"),
                )
                return self._lifecycle_result(
                    result,
                    provider,
                    category=RESULT_REPRICE_SUBMITTED,
                    platform_listing_id=listing_id,
                    request_payload={"platform_listing_id": listing_id, "price": price, "platform": self.platform},
                )
            if self.platform == "uuyp" and hasattr(client, "change_price"):
                result = client.change_price({listing_id: price})
                return self._lifecycle_result(
                    result,
                    provider,
                    category=RESULT_REPRICE_SUBMITTED,
                    platform_listing_id=listing_id,
                    request_payload={"platform_listing_id": listing_id, "price": price, "platform": self.platform},
                )
            if self.platform == "eco" and hasattr(client, "change_price"):
                assetid = eco_assetid
                steam_id = str(_first(payload, "steam_id", "SteamId", "steamId") or _default_steam_id(self.credentials))
                if not assetid:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "eco assetid is required")
                if not steam_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "eco steam_id is required")
                result = client.change_price(
                    assetid=assetid,
                    price=price,
                    steam_id=steam_id,
                    game_id=str(payload.get("game_id") or payload.get("gameId") or "730"),
                    desc=str(payload.get("desc") or payload.get("description") or ""),
                )
                return self._lifecycle_result(
                    result,
                    provider,
                    category=RESULT_REPRICE_SUBMITTED,
                    platform_listing_id=listing_id,
                    assetid=assetid,
                    request_payload={
                        "platform_listing_id": listing_id,
                        "assetid": assetid,
                        "price": price,
                        "steam_id": steam_id,
                        "platform": self.platform,
                    },
                )
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} reprice_listing is not implemented")
        except Exception as exc:
            return NormalizedResult(False, RESULT_FATAL, str(exc))

    def _create_buy_like(self, action: PlatformAction, *, purchase_order: bool) -> NormalizedResult:
        payload = _action_payload(action)
        price = float(action.target_price or 0)
        quantity = max(1, int(action.quantity or 1))
        if price <= 0:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "target_price must be greater than 0")

        client, provider, preflight_error = self._client_or_result("purchase_order" if purchase_order else "direct_buy")
        if preflight_error is not None:
            return preflight_error

        platform_id = _platform_item_id(action, payload)
        try:
            if self.platform == "buff":
                if not platform_id and hasattr(client, "search_goods_id"):
                    platform_id = client.search_goods_id(action.market_hash_name)
                if not platform_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "buff goods_id is required")
                result = client.create_buy_order(
                    goods_id=platform_id,
                    price=price,
                    num=quantity,
                    game=str(payload.get("game") or "csgo"),
                )
            elif self.platform == "uuyp":
                commodity_no = _first(payload, "commodity_no", "commodityNo", "listing_id", "platform_listing_id")
                if commodity_no and not purchase_order and hasattr(client, "buy_listing"):
                    result = client.buy_listing(commodity_no=commodity_no, price=price, game_id=payload.get("game_id", "730"))
                else:
                    if not platform_id and hasattr(client, "search_item_id_by_name"):
                        platform_id = client.search_item_id_by_name(action.market_hash_name)
                    if not platform_id:
                        return NormalizedResult(False, RESULT_VALIDATION_ERROR, "uuyp template_id is required")
                    result = client.create_buy_order(
                        goods_id=platform_id,
                        template_id=platform_id,
                        market_hash_name=action.market_hash_name,
                        commodity_name=action.market_hash_name,
                        template_hash_name=action.market_hash_name,
                        price=price,
                        num=quantity,
                        game_id=payload.get("game_id", "730"),
                    )
            elif self.platform == "eco":
                trade_link = str(_first(payload, "trade_link", "TradeLink", "tradeLink") or _default_trade_link(self.credentials))
                steam_id = str(_first(payload, "steam_id", "SteamId", "steamId") or _default_steam_id(self.credentials))
                if purchase_order:
                    result = client.create_purchase_order(
                        market_hash_name=action.market_hash_name,
                        price=price,
                        num=quantity,
                        trade_link=trade_link,
                        steam_id=steam_id,
                        game_id=str(payload.get("game_id") or "730"),
                    )
                else:
                    result = client.create_buy_order(
                        goods_id=platform_id,
                        market_hash_name=action.market_hash_name,
                        commodity_name=action.market_hash_name,
                        price=price,
                        num=quantity,
                        trade_link=trade_link,
                        steam_id=steam_id,
                        game_id=str(payload.get("game_id") or "730"),
                    )
            elif self.platform == "steam":
                result = _steam_result_to_dict(
                    client.create_buy_order(
                        market_hash_name=action.market_hash_name,
                        price=price,
                        quantity=quantity,
                    )
                )
            else:
                return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} adapter is not implemented")
        except Exception as exc:
            return NormalizedResult(False, RESULT_FATAL, str(exc))

        normalized = self._normalize(provider, result)
        normalized.request_payload = {
            "action_id": action.id,
            "action_type": action.action_type,
            "platform": self.platform,
            "platform_item_id": platform_id,
            "market_hash_name": action.market_hash_name,
            "price": price,
            "quantity": quantity,
        }
        return normalized

    def deliver_order(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        order_id = str(action.platform_order_id or _first(payload, "order_id", "orderId", "OrderId") or "")
        if not order_id:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "platform_order_id is required")
        client, provider, preflight_error = self._client_or_result("deliver_order")
        if preflight_error is not None:
            return preflight_error
        if self.platform != "c5game" or not hasattr(client, "deliver"):
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} deliver_order is not implemented")
        try:
            result = client.deliver([order_id])
        except Exception as exc:
            return NormalizedResult(False, RESULT_FATAL, str(exc))
        normalized = self._normalize(provider, result, expected_order_id=order_id)
        normalized.platform_order_id = normalized.platform_order_id or order_id
        normalized.request_payload = {
            "action_id": action.id,
            "action_type": action.action_type,
            "platform": self.platform,
            "platform_order_id": order_id,
        }
        return normalized

    def accept_trade_offer(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        offer_id = str(action.trade_offer_id or _first(payload, "tradeofferid", "trade_offer_id") or "")
        if self.platform == "c5game" and not offer_id:
            order_id = str(action.platform_order_id or _first(payload, "order_id", "orderId", "OrderId") or "")
            if not order_id:
                return NormalizedResult(False, RESULT_VALIDATION_ERROR, "c5game order_id is required")
            client, provider, preflight_error = self._client_or_result("trade_offer_poll")
            if preflight_error is not None:
                return preflight_error
            if not hasattr(client, "find_trade_offer_id"):
                return NormalizedResult(False, RESULT_VALIDATION_ERROR, "c5game trade offer polling is not implemented")
            result = client.find_trade_offer_id(
                order_id=order_id,
                steam_id=str(_first(payload, "steam_id", "SteamId", "steamId") or _default_steam_id(self.credentials)),
                page=int(payload.get("page") or 1),
            )
            normalized = self._normalize(provider, result, expected_order_id=order_id)
            normalized.trade_offer_id = normalized.trade_offer_id or _extract_trade_offer_id(result)
            normalized.platform_order_id = normalized.platform_order_id or order_id
            normalized.request_payload = {
                "action_id": action.id,
                "action_type": action.action_type,
                "platform": self.platform,
                "platform_order_id": order_id,
            }
            return normalized
        if not offer_id:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "trade_offer_id is required")
        cookies = _steam_cookie_dict(self.credentials)
        if not cookies.get("sessionid") or not cookies.get("steamLoginSecure"):
            return NormalizedResult(False, RESULT_AUTH_REQUIRED, "steam cookies/sessionid are required")
        ok = accept_steam_trade_offer(offer_id, cookies)
        return NormalizedResult(
            success=bool(ok),
            category="trade_offer_accepted" if ok else RESULT_FATAL,
            message="trade offer accepted" if ok else "trade offer accept failed",
            trade_offer_id=offer_id,
            request_payload={"trade_offer_id": offer_id},
            response_payload={"success": bool(ok)},
        )

    def cancel_order(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        client, provider, preflight_error = self._client_or_result("cancel_order")
        if preflight_error is not None:
            return preflight_error

        try:
            if self.platform == "steam" and hasattr(client, "cancel_buy_order"):
                order_id = str(action.platform_order_id or _first(payload, "buy_order_id", "buy_orderid", "order_id") or "")
                if not order_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "steam buy_order_id is required")
                result = _steam_result_to_dict(client.cancel_buy_order(order_id))
                return self._cancel_result(result, provider, platform_order_id=order_id)

            if self.platform == "buff" and hasattr(client, "cancel_sale"):
                sell_order_id = str(action.platform_listing_id or _first(payload, "sell_order_id", "sellOrderId", "listing_id", "platform_listing_id") or "")
                if not sell_order_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "buff sell_order_id is required")
                result = client.cancel_sale([sell_order_id], game=str(payload.get("game") or "csgo"))
                return self._cancel_result(result, provider, platform_listing_id=sell_order_id)

            if self.platform == "uuyp" and hasattr(client, "off_shelf"):
                listing_id = _listing_id(action, payload)
                if not listing_id:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "uuyp commodity_id is required")
                result = client.off_shelf([listing_id])
                return self._cancel_result(result, provider, platform_listing_id=listing_id)

            if self.platform == "eco" and hasattr(client, "off_shelf"):
                listing_id = _listing_id(action, payload)
                assetid = str(action.assetid or _first(payload, "assetid", "asset_id", "AssetId") or "")
                if not listing_id and not assetid:
                    return NormalizedResult(False, RESULT_VALIDATION_ERROR, "eco GoodsNum or AssetId is required")
                result = client.off_shelf(
                    [listing_id] if listing_id else None,
                    assetids=[assetid] if assetid else None,
                    game_id=str(payload.get("game_id") or payload.get("gameId") or "730"),
                )
                return self._cancel_result(result, provider, platform_listing_id=listing_id or None)

            return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} cancel_order is not implemented")
        except Exception as exc:
            return NormalizedResult(False, RESULT_FATAL, str(exc))

    def _lifecycle_result(
        self,
        result: dict[str, Any],
        provider: Any,
        *,
        category: str,
        platform_order_id: str | None = None,
        platform_listing_id: str | None = None,
        assetid: str | None = None,
        request_payload: dict[str, Any] | None = None,
    ) -> NormalizedResult:
        if provider is not None:
            try:
                provider.classify_result(result)
            except Exception:
                pass
        success = bool(result.get("success")) if isinstance(result, dict) else False
        message = str(result.get("msg") or result.get("message") or result.get("error") or "") if isinstance(result, dict) else str(result or "")
        reason = str(result.get("reason") or result.get("code") or "") if isinstance(result, dict) else ""
        text = f"{reason} {message}".lower()
        out_category = category if success else RESULT_FATAL
        if not success:
            if (isinstance(result, dict) and result.get("auth_required")) or any(token in text for token in ("login", "auth", "token", "expired")):
                out_category = RESULT_AUTH_REQUIRED
            elif "risk" in text or "cooldown" in text:
                out_category = RESULT_RISK_BLOCKED
            elif "validation" in text or "invalid" in text or "required" in text:
                out_category = RESULT_VALIDATION_ERROR
        return NormalizedResult(
            success=success,
            category=out_category,
            message=message,
            platform_order_id=platform_order_id,
            platform_listing_id=platform_listing_id,
            assetid=assetid,
            request_payload=request_payload or {},
            response_payload=result if isinstance(result, dict) else {"raw": result},
            raw=result,
        )

    def _cancel_result(
        self,
        result: dict[str, Any],
        provider: Any,
        *,
        platform_order_id: str | None = None,
        platform_listing_id: str | None = None,
    ) -> NormalizedResult:
        if provider is not None:
            try:
                provider.classify_result(result)
            except Exception:
                pass
        success = bool(result.get("success"))
        message = str(result.get("msg") or result.get("message") or result.get("error") or "")
        reason = str(result.get("reason") or result.get("code") or "").lower()
        text = f"{reason} {message}".lower()
        category = RESULT_CANCELLED if success else RESULT_FATAL
        if not success:
            if result.get("auth_required") or any(token in text for token in ("login", "auth", "token", "expired")):
                category = RESULT_AUTH_REQUIRED
            elif "risk" in text or "cooldown" in text:
                category = RESULT_RISK_BLOCKED
            elif "validation" in text or "invalid" in text or "required" in text:
                category = RESULT_VALIDATION_ERROR
        return NormalizedResult(
            success=success,
            category=category,
            message=message,
            platform_order_id=platform_order_id,
            platform_listing_id=platform_listing_id,
            request_payload={
                "platform": self.platform,
                "platform_order_id": platform_order_id,
                "platform_listing_id": platform_listing_id,
            },
            response_payload=result,
            raw=result,
        )

    def poll_order(self, action: PlatformAction) -> NormalizedResult:
        payload = _action_payload(action)
        order_id = str(action.platform_order_id or _first(payload, "order_id", "OrderNo", "orderNo") or "")
        if not order_id:
            return NormalizedResult(False, RESULT_VALIDATION_ERROR, "platform_order_id is required")
        client, provider, preflight_error = self._client_or_result("order_status")
        if preflight_error is not None:
            return preflight_error
        if hasattr(client, "query_order_status"):
            result = client.query_order_status(**self._order_status_query_kwargs(payload, order_id))
            return self._normalize(provider, result, expected_order_id=order_id)
        if self.platform == "steam" and hasattr(client, "fetch_active_buy_orders"):
            result = self._poll_steam_buy_order(client, action, order_id)
            return self._normalize(provider, result, expected_order_id=order_id)
        if self.platform == "buff" and hasattr(client, "check_wait_pay_orders"):
            result = self._poll_buff_order(client, payload, order_id)
            return self._normalize(provider, result, expected_order_id=order_id)
        return NormalizedResult(False, RESULT_VALIDATION_ERROR, f"{self.platform} order polling is not implemented")

    def _order_status_query_kwargs(self, payload: dict[str, Any], order_id: str) -> dict[str, Any]:
        if self.platform == "buff":
            return {
                "order_nums": [order_id],
                "game": str(payload.get("game") or "csgo"),
            }
        if self.platform == "uuyp":
            return {
                "order_nums": [order_id],
                "template_id": _first(payload, "template_id", "templateId", "uuyp_template_id", "goods_id", "platform_item_id"),
                "game_id": str(payload.get("game_id") or payload.get("gameId") or "730"),
            }
        if self.platform == "eco":
            kwargs: dict[str, Any] = {
                "order_nums": [order_id],
                "game_id": str(payload.get("game_id") or payload.get("gameId") or "730"),
            }
            merchant_nos = _as_str_list(
                _first(payload, "merchant_nos", "MerchantNos", "merchant_no", "MerchantNo", "merchantNo")
            )
            if merchant_nos:
                kwargs["merchant_nos"] = merchant_nos
            return kwargs
        if self.platform == "c5game":
            kwargs: dict[str, Any] = {
                "order_nums": [order_id],
                "steam_id": str(_first(payload, "steam_id", "SteamId", "steamId") or _default_steam_id(self.credentials)),
                "page": int(payload.get("page") or 1),
            }
            if payload.get("status") not in (None, ""):
                kwargs["status"] = int(payload.get("status"))
            return kwargs
        if self.platform == "steam":
            return {"order_nums": [order_id]}
        return {"order_nums": [order_id]}

    @staticmethod
    def _poll_steam_buy_order(client: Any, action: PlatformAction, order_id: str) -> dict[str, Any]:
        rows = client.fetch_active_buy_orders()
        matched = [row for row in rows if str(row.get("order_id") or "").strip() == str(order_id)]
        if matched:
            row = dict(matched[0])
            row.setdefault("order_status", "open")
            return {"success": True, "msg": "Steam buy order is still active", "data": row}
        return {
            "success": True,
            "msg": "Steam buy order not found in active orders; waiting for settlement evidence",
            "data": {
                "order_id": str(order_id),
                "market_hash_name": action.market_hash_name,
                "order_status": "pending",
                "missing_from_active_orders": True,
            },
        }

    @staticmethod
    def _poll_buff_order(client: Any, payload: dict[str, Any], order_id: str) -> dict[str, Any]:
        game = str(payload.get("game") or "csgo")
        has_wait_pay = bool(client.check_wait_pay_orders(game=game))
        status = "wait_pay" if has_wait_pay else "pending"
        return {
            "success": True,
            "msg": "BUFF wait-pay order check completed",
            "data": {
                "order_id": str(order_id),
                "order_status": status,
                "wait_pay": has_wait_pay,
            },
        }


def build_platform_adapters(
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    platforms: tuple[str, ...] = ("buff", "uuyp", "eco", "steam", "c5game"),
) -> dict[str, PlatformClientAdapter]:
    credentials = credentials or {}
    config = config or {}
    factory = PlatformClientFactory(credentials=credentials, config=config)
    return {
        normalize_platform(platform): PlatformClientAdapter(
            platform,
            credentials=credentials,
            config=config,
            factory=factory,
        )
        for platform in platforms
    }
