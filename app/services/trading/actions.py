from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import update
from sqlmodel import select

from app.database import PlatformAction
from .risk_categories import risk_category_from_market_hash_name
from .states import CLAIMABLE_STATES, TERMINAL_STATES, PlatformActionState, can_transition


class InvalidPlatformActionTransition(ValueError):
    pass


def _now() -> float:
    return time.time()


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def make_idempotency_key(
    *,
    action_type: str,
    platform: str,
    item_id: int,
    target_price: float | None = None,
    quantity: int = 1,
    external_ref: str = "",
) -> str:
    price_token = "" if target_price is None else f"{float(target_price):.4f}"
    raw = "|".join(
        [
            str(action_type or "").lower().strip(),
            str(platform or "").lower().strip(),
            str(int(item_id or 0)),
            price_token,
            str(int(quantity or 1)),
            str(external_ref or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_platform_action(
    session,
    *,
    action_type: str,
    platform: str,
    item_id: int,
    market_hash_name: str,
    risk_category: str | None = None,
    quantity: int = 1,
    target_price: float | None = None,
    reference_price: float | None = None,
    cost_basis_cny: float | None = None,
    expected_profit_rate: float | None = None,
    locked_budget_cny: float | None = None,
    channel: str = "auto",
    next_check_at: float | None = None,
    idempotency_key: str | None = None,
    request_payload: Any = None,
    raw_context: Any = None,
    commit: bool = True,
) -> tuple[PlatformAction, bool]:
    quantity = max(1, int(quantity or 1))
    platform = str(platform or "").lower().strip()
    action_type = str(action_type or "").lower().strip()
    market_hash_name = str(market_hash_name or "")
    category = str(risk_category or "").strip() or risk_category_from_market_hash_name(market_hash_name)
    idempotency_key = idempotency_key or make_idempotency_key(
        action_type=action_type,
        platform=platform,
        item_id=int(item_id or 0),
        target_price=target_price,
        quantity=quantity,
        external_ref=str(market_hash_name or ""),
    )
    existing = (
        session.execute(
            select(PlatformAction).where(PlatformAction.idempotency_key == idempotency_key)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing, False

    now = _now()
    budget = locked_budget_cny
    if budget is None and target_price is not None:
        budget = round(float(target_price) * quantity, 2)
    action = PlatformAction(
        created_at=now,
        updated_at=now,
        next_check_at=next_check_at if next_check_at is not None else now,
        action_type=action_type,
        platform=platform,
        state=PlatformActionState.QUEUED,
        channel=str(channel or "auto"),
        item_id=int(item_id or 0),
        market_hash_name=market_hash_name,
        risk_category=category,
        quantity=quantity,
        target_price=target_price,
        reference_price=reference_price,
        cost_basis_cny=cost_basis_cny,
        expected_profit_rate=expected_profit_rate,
        locked_budget_cny=float(budget or 0),
        idempotency_key=idempotency_key,
        request_payload=_json_or_none(request_payload),
        raw_context=_json_or_none(raw_context),
    )
    session.add(action)
    if commit:
        session.commit()
        session.refresh(action)
    return action, True


def transition_action(
    action: PlatformAction,
    next_state: str,
    *,
    now: float | None = None,
    clear_lease: bool = True,
    **updates: Any,
) -> PlatformAction:
    current_state = str(action.state or "")
    next_state = str(next_state or "")
    if not can_transition(current_state, next_state):
        raise InvalidPlatformActionTransition(f"{current_state} -> {next_state} is not allowed")

    ts = _now() if now is None else float(now)
    action.state = next_state
    action.updated_at = ts
    if next_state in TERMINAL_STATES:
        action.finished_at = ts
    if clear_lease:
        action.lease_until = None

    for key, value in updates.items():
        if key in {"request_payload", "response_payload", "raw_context"}:
            value = _json_or_none(value)
        if hasattr(action, key):
            setattr(action, key, value)
    return action


def schedule_retry(
    action: PlatformAction,
    *,
    error_code: str = "",
    error_message: str = "",
    now: float | None = None,
    base_delay_seconds: int = 30,
    max_delay_seconds: int = 900,
) -> PlatformAction:
    ts = _now() if now is None else float(now)
    action.retry_count = int(action.retry_count or 0) + 1
    action.error_code = error_code or action.error_code
    action.error_message = error_message or action.error_message
    if action.retry_count > int(action.max_retries or 0):
        return transition_action(action, PlatformActionState.FAILED, now=ts)
    delay = min(max_delay_seconds, base_delay_seconds * (2 ** max(0, action.retry_count - 1)))
    retry_state = PlatformActionState.RETRY_WAIT
    if action.state == PlatformActionState.PROCESSING:
        claimed_from_state = str(getattr(action, "_claimed_from_state", "") or "")
        if claimed_from_state in {
            PlatformActionState.SUBMITTED,
            PlatformActionState.WAITING_PLATFORM,
            PlatformActionState.WAITING_TRADE_OFFER,
            PlatformActionState.WAITING_STEAM_CONFIRM,
            PlatformActionState.WAITING_SETTLEMENT,
        }:
            retry_state = claimed_from_state
    return transition_action(
        action,
        retry_state,
        now=ts,
        next_check_at=ts + delay,
    )


def claim_due_actions(
    session,
    *,
    now: float | None = None,
    limit: int = 10,
    lease_seconds: int = 60,
) -> list[PlatformAction]:
    ts = _now() if now is None else float(now)
    limit = max(1, int(limit or 1))
    claimable = list(CLAIMABLE_STATES - TERMINAL_STATES)
    candidate_rows = (
        session.execute(
            select(PlatformAction.id, PlatformAction.state)
            .where(PlatformAction.state.in_(claimable))
            .where(PlatformAction.archived_at.is_(None))
            .where(PlatformAction.next_check_at <= ts)
            .where((PlatformAction.lease_until.is_(None)) | (PlatformAction.lease_until <= ts))
            .order_by(PlatformAction.next_check_at.asc(), PlatformAction.id.asc())
            .limit(limit)
        )
        .all()
    )
    claimed_pairs: list[tuple[PlatformAction, str]] = []
    lease_until = ts + max(1, int(lease_seconds or 1))
    for action_id, claimed_from_state in candidate_rows:
        claimed_from_state = str(claimed_from_state or "")
        if not can_transition(claimed_from_state, PlatformActionState.PROCESSING):
            continue
        result = session.execute(
            update(PlatformAction)
            .where(PlatformAction.id == int(action_id))
            .where(PlatformAction.state == claimed_from_state)
            .where(PlatformAction.next_check_at <= ts)
            .where((PlatformAction.lease_until.is_(None)) | (PlatformAction.lease_until <= ts))
            .values(
                state=PlatformActionState.PROCESSING,
                updated_at=ts,
                lease_until=lease_until,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        action = session.get(PlatformAction, int(action_id))
        if action is None:
            continue
        claimed_pairs.append((action, claimed_from_state))
    if claimed_pairs:
        session.commit()
        for action, claimed_from_state in claimed_pairs:
            session.refresh(action)
            setattr(action, "_claimed_from_state", claimed_from_state)
    return [action for action, _ in claimed_pairs]


def active_states() -> set[str]:
    return set(CLAIMABLE_STATES) | {PlatformActionState.RISK_BLOCKED}
