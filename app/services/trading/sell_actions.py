from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.database import PlatformAction

from .actions import create_platform_action, make_idempotency_key
from .capabilities import normalize_platform
from .risk_budget import RiskBudgetService, RiskDecision
from .states import PlatformActionState, PlatformActionType


SELL_SIDE_ACTION_TYPES = {
    PlatformActionType.STEAM_LISTING,
    PlatformActionType.PLATFORM_LISTING,
    PlatformActionType.REPRICE_LISTING,
    PlatformActionType.CANCEL_ORDER,
    PlatformActionType.DELIVER_ORDER,
    PlatformActionType.ACCEPT_TRADE_OFFER,
    PlatformActionType.POLL_ORDER,
}

PROFIT_GATED_SELL_ACTION_TYPES = {
    PlatformActionType.STEAM_LISTING,
    PlatformActionType.PLATFORM_LISTING,
    PlatformActionType.REPRICE_LISTING,
}


@dataclass(frozen=True)
class SellerActionCreateResult:
    action: PlatformAction
    created: bool
    risk: RiskDecision


@dataclass(frozen=True)
class SellerActionPlan:
    actions: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


@dataclass(frozen=True)
class SellerActionPlanResult:
    plan: SellerActionPlan
    created: list[SellerActionCreateResult]


def is_sell_side_action_type(action_type: str) -> bool:
    return str(action_type or "").lower().strip() in SELL_SIDE_ACTION_TYPES


def sell_side_risk_decision(
    action_type: str,
    *,
    expected_profit_rate: float | None = None,
    risk_budget: RiskBudgetService | None = None,
) -> RiskDecision:
    risk_budget = risk_budget or RiskBudgetService()
    action_type = str(action_type or "").lower().strip()
    if (
        action_type in PROFIT_GATED_SELL_ACTION_TYPES
        and expected_profit_rate is not None
        and float(expected_profit_rate) < risk_budget.config.min_expected_profit_rate
    ):
        return RiskDecision(False, "profit_floor_lock", 0.0)
    return RiskDecision(True, "", 0.0)


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int_or_default(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _order_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "0": "pending_payment",
        "1": "wait_send",
        "2": "delivering",
        "3": "waiting_receive",
        "10": "completed",
        "11": "cancelled",
    }
    return mapping.get(text, text)


def _trade_offer_id_from_order(order: dict[str, Any]) -> str:
    info = order.get("orderConfirmInfoDTO") if isinstance(order.get("orderConfirmInfoDTO"), dict) else {}
    return str(
        _first(order, "trade_offer_id", "tradeOfferId", "TradeOfferId", "offer_id", "offerId")
        or _first(info, "trade_offer_id", "tradeOfferId", "TradeOfferId", "offer_id", "offerId")
        or ""
    ).strip()


def _request_payload(raw: dict[str, Any]) -> dict[str, Any]:
    nested = raw.get("request_payload") if isinstance(raw.get("request_payload"), dict) else {}
    payload = dict(nested)
    for key in (
        "assetid",
        "asset_id",
        "AssetId",
        "platform_listing_id",
        "listing_id",
        "sell_order_id",
        "commodity_id",
        "platform_order_id",
        "order_id",
        "orderId",
        "trade_offer_id",
        "tradeOfferId",
        "steam_id",
        "SteamId",
        "game_id",
        "gameId",
        "price_cents",
        "desc",
        "description",
        "status",
        "page",
    ):
        if raw.get(key) not in (None, "") and key not in payload:
            payload[key] = raw[key]
    return payload


def _seller_action_external_ref(action_type: str, platform: str, raw: dict[str, Any]) -> str:
    payload = _request_payload(raw)
    assetid = str(_first(raw, "assetid", "asset_id", "AssetId") or _first(payload, "assetid", "asset_id", "AssetId") or "").strip()
    listing_id = str(
        _first(raw, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id")
        or _first(payload, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id")
        or ""
    ).strip()
    order_id = str(
        _first(raw, "platform_order_id", "order_id", "orderId", "OrderId")
        or _first(payload, "platform_order_id", "order_id", "orderId", "OrderId")
        or ""
    ).strip()
    offer_id = str(
        _first(raw, "trade_offer_id", "tradeOfferId", "TradeOfferId", "tradeofferid")
        or _first(payload, "trade_offer_id", "tradeOfferId", "TradeOfferId", "tradeofferid")
        or ""
    ).strip()
    price = raw.get("target_price")
    if price in (None, ""):
        price = _first(payload, "target_price", "price", "new_price", "listing_price")
    parts = [platform, action_type, listing_id, order_id, offer_id, assetid]
    if price not in (None, ""):
        parts.append(f"{float(price):.4f}")
    return "|".join(str(part) for part in parts if str(part or "").strip())


class SellerActionService:
    """Creates sell-side PlatformAction rows without executing platform calls."""

    def __init__(self, risk_budget: RiskBudgetService | None = None):
        self.risk_budget = risk_budget or RiskBudgetService()

    def plan_from_snapshot(self, payload: dict[str, Any]) -> SellerActionPlan:
        if not isinstance(payload, dict):
            raise ValueError("snapshot payload must be an object")
        actions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        actions.extend(self._plan_inventory_listings(payload, skipped))
        actions.extend(self._plan_reprices(payload, skipped))
        actions.extend(self._plan_cancellations(payload, skipped))
        actions.extend(self._plan_deliveries(payload, skipped))
        return SellerActionPlan(actions=actions, skipped=skipped)

    def plan_and_create(self, session, payload: dict[str, Any]) -> SellerActionPlanResult:
        plan = self.plan_from_snapshot(payload)
        created = [self.create_action(session, action) for action in plan.actions]
        return SellerActionPlanResult(plan=plan, created=created)

    def create_action(self, session, payload: dict[str, Any]) -> SellerActionCreateResult:
        if not isinstance(payload, dict):
            raise ValueError("seller action payload must be an object")
        action_type = str(payload.get("action_type") or "").lower().strip()
        if action_type not in SELL_SIDE_ACTION_TYPES:
            raise ValueError(f"unsupported seller action_type: {action_type}")
        platform = normalize_platform(str(payload.get("platform") or ""))
        item_id = int(payload.get("item_id") or 0)
        market_hash_name = str(payload.get("market_hash_name") or "").strip()
        if not platform or not item_id or not market_hash_name:
            raise ValueError("platform/item_id/market_hash_name are required")

        target_price = _float_or_none(payload.get("target_price"))
        quantity = _int_or_default(payload.get("quantity"), 1)
        expected_profit_rate = _float_or_none(payload.get("expected_profit_rate"))
        request_payload = _request_payload(payload)
        raw_context = payload.get("raw_context")
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            idempotency_key = make_idempotency_key(
                action_type=action_type,
                platform=platform,
                item_id=item_id,
                target_price=target_price,
                quantity=quantity,
                external_ref=_seller_action_external_ref(action_type, platform, payload),
            )

        risk = sell_side_risk_decision(
            action_type,
            expected_profit_rate=expected_profit_rate,
            risk_budget=self.risk_budget,
        )
        action, created = create_platform_action(
            session,
            action_type=action_type,
            platform=platform,
            item_id=item_id,
            market_hash_name=market_hash_name,
            risk_category=str(payload.get("risk_category") or ""),
            quantity=quantity,
            target_price=target_price,
            reference_price=_float_or_none(payload.get("reference_price")),
            cost_basis_cny=_float_or_none(payload.get("cost_basis_cny")),
            expected_profit_rate=expected_profit_rate,
            locked_budget_cny=0.0,
            channel=str(payload.get("channel") or "seller_automation"),
            next_check_at=payload.get("next_check_at"),
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            raw_context=raw_context,
            commit=False,
        )
        if created:
            action.platform_order_id = str(_first(payload, "platform_order_id", "order_id", "orderId", "OrderId") or _first(request_payload, "platform_order_id", "order_id", "orderId", "OrderId") or "") or None
            action.platform_listing_id = str(
                _first(payload, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id")
                or _first(request_payload, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id")
                or ""
            ) or None
            action.trade_offer_id = str(
                _first(payload, "trade_offer_id", "tradeOfferId", "TradeOfferId", "tradeofferid")
                or _first(request_payload, "trade_offer_id", "tradeOfferId", "TradeOfferId", "tradeofferid")
                or ""
            ) or None
            action.assetid = str(_first(payload, "assetid", "asset_id", "AssetId") or _first(request_payload, "assetid", "asset_id", "AssetId") or "") or None
        if not risk.allowed:
            action.state = PlatformActionState.RISK_BLOCKED
            action.error_code = risk.reason
            action.error_message = risk.reason
        session.add(action)
        session.commit()
        session.refresh(action)
        return SellerActionCreateResult(action=action, created=created, risk=risk)

    def _plan_inventory_listings(self, payload: dict[str, Any], skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = payload.get("inventory") or payload.get("items") or []
        if not isinstance(items, list):
            raise ValueError("inventory/items must be a list")
        platform = normalize_platform(str(payload.get("listing_platform") or payload.get("platform") or "steam"))
        action_type = PlatformActionType.STEAM_LISTING if platform == "steam" else PlatformActionType.PLATFORM_LISTING
        active_assetids = {str(x).strip() for x in (payload.get("active_assetids") or payload.get("active_listing_assetids") or []) if str(x).strip()}
        default_price = _float_or_none(payload.get("target_price"))
        default_expected_profit = _float_or_none(payload.get("expected_profit_rate"))
        steam_id = str(_first(payload, "steam_id", "SteamId", "steamId") or "").strip()
        channel = str(payload.get("channel") or "seller_snapshot")
        planned: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                skipped.append({"kind": "inventory", "index": index, "reason": "invalid_item"})
                continue
            assetid = str(_first(item, "assetid", "asset_id", "AssetId") or "").strip()
            name = str(_first(item, "market_hash_name", "marketHashName", "name") or "").strip()
            item_id = int(_first(item, "item_id", "itemId", "id") or payload.get("item_id") or 0)
            price = _float_or_none(_first(item, "target_price", "listing_price", "price") or default_price)
            if not _bool_value(item.get("can_sell"), default=False):
                skipped.append({"kind": "inventory", "index": index, "assetid": assetid, "reason": "not_sellable"})
                continue
            if assetid and assetid in active_assetids:
                skipped.append({"kind": "inventory", "index": index, "assetid": assetid, "reason": "already_listed"})
                continue
            if not assetid or not name or not item_id or price is None or price <= 0:
                skipped.append({"kind": "inventory", "index": index, "assetid": assetid, "reason": "missing_listing_fields"})
                continue
            row = {
                "action_type": action_type,
                "platform": platform,
                "item_id": item_id,
                "market_hash_name": name,
                "target_price": price,
                "quantity": _int_or_default(item.get("quantity"), 1),
                "assetid": assetid,
                "expected_profit_rate": _float_or_none(item.get("expected_profit_rate")) if item.get("expected_profit_rate") not in (None, "") else default_expected_profit,
                "channel": channel,
                "request_payload": {
                    "assetid": assetid,
                    "game_id": str(_first(item, "game_id", "gameId", "appid") or payload.get("game_id") or "730"),
                },
                "raw_context": {"source": "inventory_snapshot", "item": item},
            }
            if platform == "steam":
                row["request_payload"].update(
                    {
                        "appid": int(_first(item, "appid", "app_id") or 730),
                        "contextid": str(_first(item, "contextid", "context_id") or "2"),
                        "price_cents": _first(item, "price_cents", "priceCents"),
                    }
                )
            elif steam_id:
                row["request_payload"]["steam_id"] = steam_id
            planned.append(row)
        return planned

    def _plan_reprices(self, payload: dict[str, Any], skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = payload.get("reprices") or payload.get("reprice_listings") or []
        if not isinstance(rows, list):
            raise ValueError("reprices must be a list")
        channel = str(payload.get("channel") or "seller_snapshot")
        planned: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                skipped.append({"kind": "reprice", "index": index, "reason": "invalid_item"})
                continue
            platform = normalize_platform(str(row.get("platform") or payload.get("platform") or ""))
            listing_id = str(_first(row, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id") or "").strip()
            assetid = str(_first(row, "assetid", "asset_id", "AssetId") or "").strip()
            item_id = int(_first(row, "item_id", "itemId", "id") or payload.get("item_id") or 0)
            name = str(_first(row, "market_hash_name", "marketHashName", "name") or "").strip()
            price = _float_or_none(_first(row, "target_price", "new_price", "price"))
            if not platform or not item_id or not name or price is None or price <= 0 or (not listing_id and not assetid):
                skipped.append({"kind": "reprice", "index": index, "reason": "missing_reprice_fields"})
                continue
            planned.append(
                {
                    "action_type": PlatformActionType.REPRICE_LISTING,
                    "platform": platform,
                    "item_id": item_id,
                    "market_hash_name": name,
                    "target_price": price,
                    "platform_listing_id": listing_id or None,
                    "assetid": assetid or None,
                    "expected_profit_rate": _float_or_none(row.get("expected_profit_rate")),
                    "channel": channel,
                    "request_payload": _request_payload(row),
                    "raw_context": {"source": "reprice_snapshot", "item": row},
                }
            )
        return planned

    def _plan_cancellations(self, payload: dict[str, Any], skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = payload.get("cancellations") or payload.get("cancel_listings") or []
        if not isinstance(rows, list):
            raise ValueError("cancellations must be a list")
        channel = str(payload.get("channel") or "seller_snapshot")
        planned: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                skipped.append({"kind": "cancel", "index": index, "reason": "invalid_item"})
                continue
            platform = normalize_platform(str(row.get("platform") or payload.get("platform") or ""))
            listing_id = str(_first(row, "platform_listing_id", "listing_id", "sell_order_id", "commodity_id") or "").strip()
            order_id = str(_first(row, "platform_order_id", "order_id", "orderId", "OrderId") or "").strip()
            assetid = str(_first(row, "assetid", "asset_id", "AssetId") or "").strip()
            item_id = int(_first(row, "item_id", "itemId", "id") or payload.get("item_id") or 0)
            name = str(_first(row, "market_hash_name", "marketHashName", "name") or "").strip()
            if not platform or not item_id or not name or (not listing_id and not order_id and not assetid):
                skipped.append({"kind": "cancel", "index": index, "reason": "missing_cancel_fields"})
                continue
            planned.append(
                {
                    "action_type": PlatformActionType.CANCEL_ORDER,
                    "platform": platform,
                    "item_id": item_id,
                    "market_hash_name": name,
                    "platform_listing_id": listing_id or None,
                    "platform_order_id": order_id or None,
                    "assetid": assetid or None,
                    "channel": channel,
                    "request_payload": _request_payload(row),
                    "raw_context": {"source": "cancel_snapshot", "item": row},
                }
            )
        return planned

    def _plan_deliveries(self, payload: dict[str, Any], skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = payload.get("orders") or payload.get("deliveries") or []
        if not isinstance(rows, list):
            raise ValueError("orders/deliveries must be a list")
        platform = normalize_platform(str(payload.get("delivery_platform") or payload.get("platform") or "c5game"))
        channel = str(payload.get("channel") or "seller_snapshot")
        planned: list[dict[str, Any]] = []
        for index, order in enumerate(rows):
            if not isinstance(order, dict):
                skipped.append({"kind": "delivery", "index": index, "reason": "invalid_item"})
                continue
            order_id = str(_first(order, "platform_order_id", "order_id", "orderId", "OrderId", "id") or "").strip()
            item_id = int(_first(order, "item_id", "itemId", "id") or payload.get("item_id") or 0)
            name = str(_first(order, "market_hash_name", "marketHashName", "name") or "").strip()
            status = _order_status(_first(order, "order_status", "orderStatus", "status", "Status"))
            offer_id = _trade_offer_id_from_order(order)
            if not order_id or not item_id or not name:
                skipped.append({"kind": "delivery", "index": index, "reason": "missing_delivery_fields"})
                continue
            if status in {"10", "completed", "complete", "finished", "cancelled", "canceled", "11"}:
                skipped.append({"kind": "delivery", "index": index, "order_id": order_id, "reason": "terminal_order"})
                continue
            action_type = PlatformActionType.ACCEPT_TRADE_OFFER if offer_id else PlatformActionType.DELIVER_ORDER
            planned.append(
                {
                    "action_type": action_type,
                    "platform": platform,
                    "item_id": item_id,
                    "market_hash_name": name,
                    "platform_order_id": order_id,
                    "trade_offer_id": offer_id or None,
                    "channel": channel,
                    "request_payload": _request_payload(order),
                    "raw_context": {"source": "delivery_snapshot", "item": order},
                }
            )
        return planned
