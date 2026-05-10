from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.database import PlatformAction
from .order_status import (
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_FAILED,
    ORDER_STATUS_PENDING,
    normalize_order_status,
)
from .states import PlatformActionType


RESULT_SUCCESS = "success"
RESULT_SAFE_MODE = "safe_mode"
RESULT_TRANSIENT = "transient_error"
RESULT_AUTH_REQUIRED = "auth_required"
RESULT_RISK_BLOCKED = "risk_blocked"
RESULT_VALIDATION_ERROR = "validation_error"
RESULT_NOT_FOUND = "not_found"
RESULT_FATAL = "fatal_error"
RESULT_UNSAFE_OFFER = "unsafe_offer"
RESULT_ORDER_PENDING = "order_pending"
RESULT_ORDER_COMPLETED = "order_completed"
RESULT_TRADE_OFFER_ACCEPTED = "trade_offer_accepted"
RESULT_CANCELLED = "cancelled"
RESULT_LISTING_SUBMITTED = "listing_submitted"
RESULT_REPRICE_SUBMITTED = "reprice_submitted"

RETRIABLE_CATEGORIES = {RESULT_TRANSIENT, RESULT_AUTH_REQUIRED, RESULT_RISK_BLOCKED}


@dataclass
class NormalizedResult:
    success: bool
    category: str = RESULT_SUCCESS
    message: str = ""
    platform_order_id: str | None = None
    platform_listing_id: str | None = None
    trade_offer_id: str | None = None
    assetid: str | None = None
    filled_quantity: int | None = None
    remaining_quantity: int | None = None
    filled_amount_cny: float | None = None
    remaining_amount_cny: float | None = None
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None
    raw: Any = None

    @property
    def retriable(self) -> bool:
        return (not self.success) and self.category in RETRIABLE_CATEGORIES


class PlatformAdapter(Protocol):
    platform: str

    def submit(self, action: PlatformAction) -> NormalizedResult:
        ...


class PlatformAdapterBase:
    platform = ""

    def submit(self, action: PlatformAction) -> NormalizedResult:
        action_type = str(action.action_type or "")
        if action_type == PlatformActionType.DIRECT_BUY:
            return self.create_direct_buy(action)
        if action_type == PlatformActionType.PURCHASE_ORDER:
            return self.create_purchase_order(action)
        if action_type == PlatformActionType.STEAM_BUY_ORDER:
            return self.create_steam_buy_order(action)
        if action_type in {PlatformActionType.STEAM_LISTING, PlatformActionType.PLATFORM_LISTING}:
            return self.create_listing(action)
        if action_type == PlatformActionType.REPRICE_LISTING:
            return self.change_price(action)
        if action_type == PlatformActionType.DELIVER_ORDER:
            return self.deliver_order(action)
        if action_type == PlatformActionType.CANCEL_ORDER:
            return self.cancel_order(action)
        if action_type == PlatformActionType.ACCEPT_TRADE_OFFER:
            return self.accept_trade_offer(action)
        if action_type == PlatformActionType.POLL_ORDER:
            return self.poll_order(action)
        return NormalizedResult(
            success=False,
            category=RESULT_VALIDATION_ERROR,
            message=f"unsupported action_type: {action_type}",
        )

    def create_direct_buy(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("direct_buy")

    def create_purchase_order(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("purchase_order")

    def create_steam_buy_order(self, action: PlatformAction) -> NormalizedResult:
        return self.create_purchase_order(action)

    def create_listing(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("listing")

    def change_price(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("change_price")

    def deliver_order(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("deliver_order")

    def cancel_order(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("cancel_order")

    def accept_trade_offer(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("accept_trade_offer")

    def poll_order(self, action: PlatformAction) -> NormalizedResult:
        return self._not_implemented("poll_order")

    @staticmethod
    def _not_implemented(capability: str) -> NormalizedResult:
        return NormalizedResult(
            success=False,
            category=RESULT_VALIDATION_ERROR,
            message=f"adapter capability not implemented: {capability}",
        )


@dataclass
class SafeModeAdapter(PlatformAdapterBase):
    platform: str = "safe_mode"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, action: PlatformAction) -> NormalizedResult:
        payload = {
            "action_id": action.id,
            "action_type": action.action_type,
            "platform": action.platform,
            "item_id": action.item_id,
            "market_hash_name": action.market_hash_name,
            "quantity": action.quantity,
            "target_price": action.target_price,
        }
        self.calls.append(payload)
        is_cancel = str(action.action_type or "") == PlatformActionType.CANCEL_ORDER
        is_delivery = str(action.action_type or "") == PlatformActionType.DELIVER_ORDER
        is_listing = str(action.action_type or "") in {PlatformActionType.STEAM_LISTING, PlatformActionType.PLATFORM_LISTING}
        is_reprice = str(action.action_type or "") == PlatformActionType.REPRICE_LISTING
        category = RESULT_CANCELLED if is_cancel else RESULT_SAFE_MODE
        if is_listing:
            category = RESULT_LISTING_SUBMITTED
        if is_reprice:
            category = RESULT_REPRICE_SUBMITTED
        if is_delivery:
            category = RESULT_ORDER_PENDING
        return NormalizedResult(
            success=True,
            category=category,
            message="SAFE_MODE simulated cancel action" if is_cancel else "SAFE_MODE simulated platform action",
            platform_order_id=action.platform_order_id if is_cancel or is_delivery else f"safe-{action.id or len(self.calls)}",
            platform_listing_id=action.platform_listing_id if is_cancel else (f"safe-listing-{action.id or len(self.calls)}" if is_listing else None),
            assetid=action.assetid,
            request_payload=payload,
            response_payload={"success": True, "safe_mode": True},
            raw={"safe_mode": True},
        )


def normalize_platform_result(
    result: dict[str, Any] | NormalizedResult | None,
    *,
    platform: str = "",
    expected_order_id: str = "",
) -> NormalizedResult:
    if isinstance(result, NormalizedResult):
        return result
    if not isinstance(result, dict):
        return NormalizedResult(success=False, category=RESULT_FATAL, message=str(result or "empty result"))

    success = bool(result.get("success"))
    message = str(result.get("msg") or result.get("message") or result.get("error") or "")
    category = RESULT_SUCCESS if success else RESULT_FATAL
    reason = str(result.get("reason") or result.get("code") or "").lower()
    text = f"{reason} {message}".lower()
    if not success:
        if result.get("auth_required") or any(token in text for token in ("login", "auth", "token", "expired")):
            category = RESULT_AUTH_REQUIRED
        elif "risk" in text or "cooldown" in text:
            category = RESULT_RISK_BLOCKED
        elif "not found" in text or "missing" in text:
            category = RESULT_NOT_FOUND
        elif "timeout" in text or "rate" in text or "tempor" in text:
            category = RESULT_TRANSIENT
        elif "validation" in text or "invalid" in text:
            category = RESULT_VALIDATION_ERROR

    order_snapshot = normalize_order_status(result, platform=platform, expected_order_id=expected_order_id)
    order_status = _order_status_category(order_snapshot.status)
    if success and order_status:
        category = order_status
        if order_status == RESULT_FATAL:
            success = False

    return NormalizedResult(
        success=success,
        category=category,
        message=message,
        platform_order_id=order_snapshot.platform_order_id,
        platform_listing_id=order_snapshot.platform_listing_id,
        trade_offer_id=order_snapshot.trade_offer_id,
        assetid=order_snapshot.assetid,
        filled_quantity=order_snapshot.filled_quantity,
        remaining_quantity=order_snapshot.remaining_quantity,
        filled_amount_cny=order_snapshot.filled_amount_cny,
        remaining_amount_cny=order_snapshot.remaining_amount_cny,
        response_payload=result,
        raw=result,
    )


def _order_status_category(status: str) -> str:
    if status == ORDER_STATUS_PENDING:
        return RESULT_ORDER_PENDING
    if status == ORDER_STATUS_COMPLETED:
        return RESULT_ORDER_COMPLETED
    if status == ORDER_STATUS_FAILED:
        return RESULT_FATAL
    return ""
