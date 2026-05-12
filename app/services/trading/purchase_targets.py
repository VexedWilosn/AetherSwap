from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.database import PlatformAction

from .actions import create_platform_action
from .purchase_planner import PurchasePlan, PurchasePlanAction, build_purchase_plan
from .states import PlatformActionType


@dataclass(frozen=True)
class PurchaseTargetCreateResult:
    target_id: str
    plan: PurchasePlan
    actions: tuple[PlatformAction, ...]
    created_count: int
    existing_count: int


def _request_payload_for_action(action: PurchasePlanAction, base_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(base_payload)
    payload["planned_action_type"] = action.action_type
    payload["planned_platform"] = action.platform
    payload["planned_quantity"] = action.quantity
    payload["planned_price"] = action.price
    platform_ids = payload.get("platform_ids") if isinstance(payload.get("platform_ids"), dict) else {}
    platform_payloads = payload.get("platform_payloads") if isinstance(payload.get("platform_payloads"), dict) else {}
    platform_payload = platform_payloads.get(action.platform) if isinstance(platform_payloads.get(action.platform), dict) else {}
    payload.update(platform_payload)
    for key in ("goods_id", "template_id", "commodity_no", "platform_item_id", "sell_order_id", "listing_id"):
        if key not in payload and key in platform_payload:
            payload[key] = platform_payload[key]
    if "platform_item_id" not in payload and action.platform in platform_ids:
        payload["platform_item_id"] = platform_ids[action.platform]
    return payload


def create_purchase_target_actions(
    session,
    *,
    item_id: int,
    market_hash_name: str,
    target_quantity: int,
    max_unit_price: float,
    quotes: list[Any] | tuple[Any, ...],
    default_order_price: float | None = None,
    channel: str = "purchase_target",
    request_payload: dict[str, Any] | None = None,
    raw_context: dict[str, Any] | None = None,
    target_id: str | None = None,
    next_check_at: float | None = None,
) -> PurchaseTargetCreateResult:
    target_id = str(target_id or uuid.uuid4().hex)
    plan = build_purchase_plan(
        item_id=item_id,
        market_hash_name=market_hash_name,
        target_quantity=target_quantity,
        max_unit_price=max_unit_price,
        quotes=quotes,
        default_order_price=default_order_price,
    )
    base_payload = dict(request_payload or {})
    base_context = dict(raw_context or {})
    base_context.update(
        {
            "purchase_target_id": target_id,
            "target_quantity": plan.target_quantity,
            "target_remaining_quantity": plan.remaining_quantity,
            "direct_quantity": plan.direct_quantity,
            "order_quantity": plan.order_quantity,
            "cost_batch_id": target_id,
        }
    )
    created: list[PlatformAction] = []
    created_count = 0
    existing_count = 0
    now = time.time()
    for index, planned in enumerate(plan.actions):
        action_type = (
            PlatformActionType.DIRECT_BUY
            if planned.action_type == "direct_buy"
            else PlatformActionType.PURCHASE_ORDER
        )
        payload = _request_payload_for_action(planned, base_payload)
        context = dict(base_context)
        context.update(
            {
                "purchase_target_action_index": index,
                "planned_reason": planned.reason,
                "planned_action_type": planned.action_type,
            }
        )
        action, was_created = create_platform_action(
            session,
            action_type=action_type,
            platform=planned.platform,
            item_id=int(item_id or 0),
            market_hash_name=market_hash_name,
            quantity=planned.quantity,
            target_price=planned.price,
            channel=channel,
            next_check_at=next_check_at if next_check_at is not None else now,
            idempotency_key=f"purchase_target:{target_id}:{index}:{planned.platform}:{planned.action_type}",
            request_payload=payload,
            raw_context=context,
            commit=False,
        )
        created.append(action)
        created_count += 1 if was_created else 0
        existing_count += 0 if was_created else 1
    session.commit()
    for action in created:
        session.refresh(action)
    return PurchaseTargetCreateResult(
        target_id=target_id,
        plan=plan,
        actions=tuple(created),
        created_count=created_count,
        existing_count=existing_count,
    )
