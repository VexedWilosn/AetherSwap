from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from app.database import PlatformAction, Purchase

from .adapters import RESULT_SAFE_MODE
from .states import PlatformActionState, PlatformActionType


BUY_ACTION_TYPES = {
    PlatformActionType.DIRECT_BUY,
    PlatformActionType.PURCHASE_ORDER,
    PlatformActionType.STEAM_BUY_ORDER,
}


@dataclass(frozen=True)
class PurchaseMaterializeResult:
    created: int
    existing: int
    target_quantity: int


def _loads_dict(value: str | dict | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _unit_price(action: PlatformAction, quantity: int, context: dict[str, Any]) -> float:
    filled_amount = float(action.filled_amount_cny or 0)
    if quantity > 0 and filled_amount > 0:
        return round(filled_amount / quantity, 2)
    for value in (
        _first(context, "unit_cost_cny", "unit_price", "buy_price", "target_price"),
        action.target_price,
        action.cost_basis_cny,
    ):
        try:
            price = float(value or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return round(price, 2)
    return 0.0


def _market_price(action: PlatformAction, context: dict[str, Any]) -> float | None:
    for value in (_first(context, "market_price", "steam_market_price", "reference_price"), action.reference_price):
        try:
            price = float(value or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return round(price, 2)
    return None


def _assetids(action: PlatformAction, context: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("assetids", "asset_ids", "received_assetids", "received_asset_ids"):
        raw = context.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        elif raw not in (None, ""):
            values.append(raw)
    if action.assetid:
        values.insert(0, action.assetid)
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _response_category(action: PlatformAction) -> str:
    payload = _loads_dict(action.response_payload)
    if bool(payload.get("safe_mode")):
        return RESULT_SAFE_MODE
    return str(payload.get("category") or "").strip()


def materialize_purchase_for_action(session, action: PlatformAction) -> PurchaseMaterializeResult:
    action_type = str(action.action_type or "")
    if action_type not in BUY_ACTION_TYPES:
        return PurchaseMaterializeResult(created=0, existing=0, target_quantity=0)
    state = str(action.state or "")
    if state not in {PlatformActionState.SUCCEEDED, PlatformActionState.CANCELLED}:
        return PurchaseMaterializeResult(created=0, existing=0, target_quantity=0)
    if _response_category(action) == RESULT_SAFE_MODE:
        return PurchaseMaterializeResult(created=0, existing=0, target_quantity=0)
    if not action.id:
        return PurchaseMaterializeResult(created=0, existing=0, target_quantity=0)

    filled_quantity = max(0, int(action.filled_quantity or 0))
    if filled_quantity <= 0 and state == PlatformActionState.SUCCEEDED:
        filled_quantity = max(0, int(action.quantity or 0))
    if filled_quantity <= 0:
        return PurchaseMaterializeResult(created=0, existing=0, target_quantity=0)

    existing_rows = session.execute(
        select(Purchase).where(Purchase.source_action_id == int(action.id))
    ).scalars().all()
    existing_indexes = {
        int(row.source_fill_index)
        for row in existing_rows
        if row.source_fill_index is not None
    }
    context = _loads_dict(action.raw_context)
    context.update(_loads_dict(action.request_payload))
    context.update(_loads_dict(action.response_payload))
    unit_price = _unit_price(action, filled_quantity, context)
    if unit_price <= 0:
        return PurchaseMaterializeResult(created=0, existing=len(existing_rows), target_quantity=filled_quantity)

    assets = _assetids(action, context)
    market_price = _market_price(action, context)
    created = 0
    for fill_index in range(1, filled_quantity + 1):
        if fill_index in existing_indexes:
            continue
        purchase = Purchase(
            name=action.market_hash_name or str(context.get("market_hash_name") or ""),
            goods_id=int(action.item_id or 0),
            price=unit_price,
            at=float(action.finished_at or action.updated_at or action.created_at or 0),
            market_price=market_price,
            pending_receipt=fill_index - 1 >= len(assets),
            assetid=assets[fill_index - 1] if fill_index - 1 < len(assets) else None,
            listing=False,
            listing_status=None,
            source_platform=action.platform,
            source_action_id=int(action.id),
            source_order_id=action.platform_order_id,
            source_trade_offer_id=action.trade_offer_id,
            source_fill_index=fill_index,
        )
        session.add(purchase)
        created += 1
    return PurchaseMaterializeResult(created=created, existing=len(existing_rows), target_quantity=filled_quantity)
