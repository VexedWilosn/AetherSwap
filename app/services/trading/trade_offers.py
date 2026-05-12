from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.database import PlatformAction

from .adapters import (
    RESULT_AUTH_REQUIRED,
    RESULT_FATAL,
    RESULT_MOBILE_CONFIRM_REQUIRED,
    RESULT_OFFER_DECLINED,
    RESULT_OFFER_EXPIRED,
    RESULT_NOT_FOUND,
    RESULT_TRADE_OFFER_ACCEPTED,
    RESULT_TRANSIENT,
    RESULT_UNSAFE_OFFER,
    RESULT_VALIDATION_ERROR,
    NormalizedResult,
)
from .steam_trade_offer_client import SteamTradeOfferClient, steam_web_api_key_from_sources


@dataclass(frozen=True)
class TradeOfferValidation:
    allowed: bool
    reason: str = ""
    trade_offer_id: str = ""
    category: str = ""


def _loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_set(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {str(value).strip()} if str(value).strip() else set()


def _action_payload(action: PlatformAction) -> dict[str, Any]:
    payload = _loads_dict(action.raw_context)
    payload.update(_loads_dict(action.request_payload))
    return payload


def _offer_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("offer", "trade_offer", "tradeOffer", "trade_offer_snapshot", "offer_snapshot"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _trade_offer_id(action: PlatformAction, payload: dict[str, Any]) -> str:
    return str(
        action.trade_offer_id
        or _first(payload, "tradeofferid", "trade_offer_id", "tradeOfferId", "TradeOfferId", "offer_id", "offerId")
        or _first(_offer_snapshot(payload), "tradeofferid", "trade_offer_id", "tradeOfferId", "TradeOfferId", "offer_id", "offerId")
        or ""
    ).strip()


def _expected_assetids(action: PlatformAction, payload: dict[str, Any]) -> set[str]:
    values = set()
    for key in ("expected_assetids", "expected_asset_ids", "assetids", "asset_ids"):
        values.update(_as_set(payload.get(key)))
    if action.assetid:
        values.add(str(action.assetid).strip())
    return {value for value in values if value}


def _expected_names(action: PlatformAction, payload: dict[str, Any]) -> set[str]:
    values = set()
    for key in ("expected_names", "expected_market_hash_names", "market_hash_names"):
        values.update(_as_set(payload.get(key)))
    if action.market_hash_name:
        values.add(str(action.market_hash_name).strip())
    return {value for value in values if value}


AcceptCallback = Callable[[PlatformAction], NormalizedResult]


STRUCTURED_ACCEPT_FAILURE_CATEGORIES = {
    "steam_auth_required": RESULT_AUTH_REQUIRED,
    "offer_expired": RESULT_OFFER_EXPIRED,
    "trade_offer_not_found": RESULT_OFFER_EXPIRED,
    "offer_declined": RESULT_OFFER_DECLINED,
    "mobile_confirm_required": RESULT_MOBILE_CONFIRM_REQUIRED,
    "transient_error": RESULT_TRANSIENT,
}


class TradeOfferService:
    def __init__(
        self,
        *,
        steam_offer_client: SteamTradeOfferClient | None = None,
        credentials: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ):
        self._lock = threading.RLock()
        self._ignored_offer_ids: set[str] = set()
        self.steam_offer_client = steam_offer_client
        self.credentials = credentials or {}
        self.config = config or {}

    def validate_receive_offer(
        self,
        offer: dict[str, Any],
        *,
        expected_assetids: set[str] | None = None,
        expected_names: set[str] | None = None,
        max_items_from_me: int = 0,
    ) -> TradeOfferValidation:
        offer_id = str(offer.get("tradeofferid") or offer.get("trade_offer_id") or "")
        items_from_me = offer.get("items_to_give") or offer.get("items_from_me") or []
        items_to_receive = offer.get("items_to_receive") or offer.get("items") or []

        if len(items_from_me) > max_items_from_me:
            return TradeOfferValidation(False, "offer_requires_our_items", offer_id)
        if not items_to_receive:
            return TradeOfferValidation(False, "empty_receive_offer", offer_id)

        expected_assetids = {str(x) for x in (expected_assetids or set()) if str(x)}
        if expected_assetids:
            received_assetids = {str(x.get("assetid") or x.get("asset_id") or "") for x in items_to_receive}
            if not expected_assetids.issubset(received_assetids):
                return TradeOfferValidation(False, "assetid_mismatch", offer_id)

        expected_names = {str(x).strip() for x in (expected_names or set()) if str(x).strip()}
        if expected_names:
            received_names = {
                str(x.get("market_hash_name") or x.get("name") or "").strip()
                for x in items_to_receive
            }
            if not expected_names.issubset(received_names):
                return TradeOfferValidation(False, "item_name_mismatch", offer_id)

        return TradeOfferValidation(True, "", offer_id)

    def validate_action_offer(self, action: PlatformAction) -> TradeOfferValidation:
        payload = _action_payload(action)
        offer_id = _trade_offer_id(action, payload)
        if not offer_id:
            return TradeOfferValidation(False, "trade_offer_id_required", "")
        if offer_id in self._ignored_offer_ids:
            return TradeOfferValidation(False, "ignored_trade_offer", offer_id)

        offer = _offer_snapshot(payload)
        if not offer:
            offer, fetch_error = self._fetch_offer_snapshot(offer_id)
            if fetch_error is not None:
                return fetch_error
        if not offer:
            return TradeOfferValidation(True, "", offer_id)
        offer.setdefault("tradeofferid", offer_id)
        max_items_from_me = int(payload.get("max_items_from_me") or payload.get("max_items_to_give") or 0)
        return self.validate_receive_offer(
            offer,
            expected_assetids=_expected_assetids(action, payload),
            expected_names=_expected_names(action, payload),
            max_items_from_me=max_items_from_me,
        )

    def _fetch_offer_snapshot(self, offer_id: str) -> tuple[dict[str, Any], TradeOfferValidation | None]:
        client = self.steam_offer_client
        if client is None:
            api_key = steam_web_api_key_from_sources(self.credentials, self.config)
            if not api_key:
                return {}, None
            client = SteamTradeOfferClient(api_key)
        result = client.fetch_offer(offer_id)
        if not result.get("success"):
            reason = str(result.get("reason") or "offer_detail_unavailable")
            category = RESULT_OFFER_EXPIRED if reason == "trade_offer_not_found" else RESULT_TRANSIENT
            return {}, TradeOfferValidation(False, reason, offer_id, category=category)
        offer = result.get("offer")
        return offer if isinstance(offer, dict) else {}, None

    def accept_for_action(self, action: PlatformAction, accept_callback: AcceptCallback) -> NormalizedResult:
        with self._lock:
            validation = self.validate_action_offer(action)
            if not validation.allowed:
                if validation.trade_offer_id and not validation.category:
                    self._ignored_offer_ids.add(validation.trade_offer_id)
                category = validation.category
                if not category:
                    category = RESULT_VALIDATION_ERROR if validation.reason == "trade_offer_id_required" else RESULT_UNSAFE_OFFER
                return NormalizedResult(
                    False,
                    category,
                    validation.reason,
                    trade_offer_id=validation.trade_offer_id or action.trade_offer_id,
                    request_payload={"trade_offer_id": validation.trade_offer_id or action.trade_offer_id},
                    response_payload={"success": False, "reason": validation.reason},
                )
            result = accept_callback(action)
            result.trade_offer_id = result.trade_offer_id or validation.trade_offer_id
            if result.success and not result.category:
                result.category = RESULT_TRADE_OFFER_ACCEPTED
            self._enrich_accept_result(result, trade_offer_id=validation.trade_offer_id)
            return result

    @staticmethod
    def _failure_reason(result: NormalizedResult) -> str:
        payload = result.response_payload if isinstance(result.response_payload, dict) else {}
        return str(
            payload.get("reason")
            or payload.get("error_code")
            or payload.get("code")
            or result.message
            or result.category
            or ""
        ).strip()

    @classmethod
    def _enrich_accept_result(cls, result: NormalizedResult, *, trade_offer_id: str = "") -> None:
        offer_id = str(result.trade_offer_id or trade_offer_id or "").strip()
        reason = cls._failure_reason(result) if not result.success else ""
        if not result.success:
            structured_category = STRUCTURED_ACCEPT_FAILURE_CATEGORIES.get(reason)
            if structured_category and result.category in {"", RESULT_FATAL, RESULT_VALIDATION_ERROR, RESULT_AUTH_REQUIRED}:
                result.category = structured_category
        if offer_id:
            result.trade_offer_id = offer_id

        request_payload = result.request_payload if isinstance(result.request_payload, dict) else {}
        request_payload = dict(request_payload)
        if offer_id:
            request_payload.setdefault("trade_offer_id", offer_id)
        result.request_payload = request_payload

        response_payload = result.response_payload if isinstance(result.response_payload, dict) else {}
        response_payload = dict(response_payload)
        response_payload.setdefault("success", bool(result.success))
        response_payload.setdefault("category", result.category)
        if offer_id:
            response_payload.setdefault("trade_offer_id", offer_id)
        if reason:
            response_payload.setdefault("reason", reason)
        if result.message:
            response_payload.setdefault("message", result.message)
        result.response_payload = response_payload
