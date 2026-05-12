from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import select

from app.database import PlatformAction

from .adapters import NormalizedResult, PlatformAdapter, RESULT_FATAL
from .capabilities import normalize_platform
from .settlement import materialize_purchase_for_action
from .states import PlatformActionState
from .trade_offers import TradeOfferService
from .worker import PlatformActionWorker


RECONCILE_STATES = {
    PlatformActionState.SUBMITTED,
    PlatformActionState.PROCESSING,
    PlatformActionState.WAITING_PLATFORM,
    PlatformActionState.WAITING_TRADE_OFFER,
    PlatformActionState.WAITING_STEAM_CONFIRM,
    PlatformActionState.WAITING_SETTLEMENT,
    PlatformActionState.RETRY_WAIT,
}

FAILED_RECOVERY_STATES = {PlatformActionState.FAILED}


@dataclass
class ReconciliationRunResult:
    checked: int = 0
    updated: int = 0
    succeeded: int = 0
    waiting: int = 0
    cancelled: int = 0
    failed: int = 0
    materialized: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)


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


def _payload(action: PlatformAction) -> dict[str, Any]:
    payload = _loads_dict(action.raw_context)
    payload.update(_loads_dict(action.request_payload))
    return payload


def _external_order_id(action: PlatformAction) -> str:
    payload = _payload(action)
    return str(
        action.platform_order_id
        or _first(payload, "order_id", "orderId", "OrderId", "OrderNo", "orderNo", "buy_order_id", "PurchaseId")
        or ""
    ).strip()


def _trade_offer_id(action: PlatformAction) -> str:
    payload = _payload(action)
    return str(
        action.trade_offer_id
        or _first(payload, "tradeofferid", "trade_offer_id", "tradeOfferId", "TradeOfferId", "offer_id", "offerId")
        or ""
    ).strip()


def _claimed_state_for_reconcile(action: PlatformAction) -> str:
    state = str(action.state or "")
    if state == PlatformActionState.RETRY_WAIT:
        return PlatformActionState.WAITING_TRADE_OFFER if _trade_offer_id(action) else PlatformActionState.WAITING_PLATFORM
    return state


def _should_accept_offer(action: PlatformAction, claimed_from_state: str) -> bool:
    if claimed_from_state == PlatformActionState.WAITING_TRADE_OFFER:
        return True
    if not _trade_offer_id(action):
        return False
    return claimed_from_state in {
        PlatformActionState.SUBMITTED,
        PlatformActionState.PROCESSING,
        PlatformActionState.RETRY_WAIT,
    }


def _fingerprint(action: PlatformAction) -> tuple[Any, ...]:
    return (
        action.state,
        action.platform_order_id,
        action.platform_listing_id,
        action.trade_offer_id,
        action.assetid,
        action.filled_quantity,
        action.remaining_quantity,
        action.filled_amount_cny,
        action.locked_budget_cny,
        action.released_budget_cny,
        action.error_code,
        action.error_message,
        action.response_payload,
    )


class PlatformActionReconciliationService:
    """Repair PlatformAction state from external platform facts without resubmitting orders."""

    def __init__(
        self,
        *,
        adapters: dict[str, PlatformAdapter] | None = None,
        trade_offer_service: TradeOfferService | None = None,
    ):
        self.adapters = adapters or {}
        self.trade_offer_service = trade_offer_service or TradeOfferService()
        self._worker = PlatformActionWorker(
            lambda: None,
            adapters=self.adapters,
            trade_offer_service=self.trade_offer_service,
            safe_mode=False,
        )

    def run(
        self,
        session,
        *,
        limit: int = 50,
        platform: str = "",
        item_id: int | None = None,
        dry_run: bool = False,
        accept_trade_offers: bool = True,
        recover_failed: bool = False,
        now: float | None = None,
    ) -> ReconciliationRunResult:
        ts = time.time() if now is None else float(now)
        limit = max(1, min(int(limit or 50), 500))
        result = ReconciliationRunResult()
        rows = self._candidate_actions(
            session,
            limit=limit,
            platform=platform,
            item_id=item_id,
            now=ts,
            recover_failed=recover_failed,
        )

        for action in rows:
            result.checked += 1
            skip_reason = self._skip_reason(action, accept_trade_offers=accept_trade_offers)
            claimed_from_state = _claimed_state_for_reconcile(action)
            if dry_run and _should_accept_offer(action, claimed_from_state):
                skip_reason = skip_reason or "dry_run_trade_offer_accept_skipped"
            if skip_reason:
                result.skipped.append(self._skip_item(action, skip_reason))
                continue

            adapter = self.adapters.get(str(action.platform or ""))
            if adapter is None:
                result.skipped.append(self._skip_item(action, "adapter_missing"))
                continue

            before = _fingerprint(action)
            if str(action.state or "") == PlatformActionState.FAILED and recover_failed:
                claimed_from_state = (
                    PlatformActionState.WAITING_TRADE_OFFER
                    if _trade_offer_id(action)
                    else PlatformActionState.WAITING_PLATFORM
                )
                action.state = PlatformActionState.PROCESSING
                action.finished_at = None
                action.lease_until = None
                setattr(action, "_claimed_from_state", claimed_from_state)
            try:
                normalized = self._fetch_result(
                    adapter,
                    action,
                    claimed_from_state,
                    dry_run=dry_run,
                    accept_trade_offers=accept_trade_offers,
                )
            except Exception as exc:
                normalized = NormalizedResult(False, RESULT_FATAL, str(exc))

            if str(action.state or "") == PlatformActionState.RETRY_WAIT:
                action.state = PlatformActionState.PROCESSING
                action.lease_until = None
                setattr(action, "_claimed_from_state", claimed_from_state)

            counters = {"succeeded": 0, "waiting": 0, "failed": 0, "risk_blocked": 0}
            self._worker._apply_result(action, normalized, ts, counters, claimed_from_state=claimed_from_state)
            materialized = materialize_purchase_for_action(session, action)
            result.materialized += int(materialized.created or 0)

            if _fingerprint(action) != before:
                result.updated += 1
            self._count_final_state(result, str(action.state or ""))
            session.add(action)

        if dry_run:
            session.rollback()
        else:
            session.commit()
        return result

    @staticmethod
    def _candidate_actions(
        session,
        *,
        limit: int,
        platform: str = "",
        item_id: int | None = None,
        now: float,
        recover_failed: bool = False,
    ) -> list[PlatformAction]:
        states = set(RECONCILE_STATES)
        if recover_failed:
            states.update(FAILED_RECOVERY_STATES)
        stmt = select(PlatformAction).where(PlatformAction.state.in_(list(states)))
        stmt = stmt.where(PlatformAction.archived_at.is_(None))
        stmt = stmt.where((PlatformAction.lease_until.is_(None)) | (PlatformAction.lease_until <= float(now)))
        if recover_failed:
            stmt = stmt.where(
                (PlatformAction.state != PlatformActionState.FAILED)
                | (PlatformAction.platform_order_id.is_not(None))
                | (PlatformAction.trade_offer_id.is_not(None))
            )
        normalized_platform = normalize_platform(platform) if platform else ""
        if normalized_platform:
            stmt = stmt.where(PlatformAction.platform == normalized_platform)
        if item_id is not None:
            stmt = stmt.where(PlatformAction.item_id == int(item_id))
        return session.execute(
            stmt.order_by(PlatformAction.updated_at.asc(), PlatformAction.id.asc()).limit(limit)
        ).scalars().all()

    @staticmethod
    def _skip_item(action: PlatformAction, reason: str) -> dict[str, Any]:
        return {
            "id": action.id,
            "platform": action.platform,
            "state": action.state,
            "action_type": action.action_type,
            "item_id": action.item_id,
            "market_hash_name": action.market_hash_name,
            "reason": reason,
        }

    @staticmethod
    def _skip_reason(action: PlatformAction, *, accept_trade_offers: bool) -> str:
        claimed_from_state = _claimed_state_for_reconcile(action)
        if _should_accept_offer(action, claimed_from_state):
            if not accept_trade_offers:
                return "trade_offer_accept_disabled"
            if not (_trade_offer_id(action) or _external_order_id(action)):
                return "external_id_missing"
            return ""
        if not _external_order_id(action):
            return "external_id_missing"
        return ""

    def _fetch_result(
        self,
        adapter: PlatformAdapter,
        action: PlatformAction,
        claimed_from_state: str,
        *,
        dry_run: bool = False,
        accept_trade_offers: bool = True,
    ) -> NormalizedResult:
        if _should_accept_offer(action, claimed_from_state):
            if _trade_offer_id(action):
                return self.trade_offer_service.accept_for_action(action, adapter.accept_trade_offer)
            return adapter.accept_trade_offer(action)
        result = adapter.poll_order(action)
        if self._worker._should_accept_discovered_trade_offer(action, result):
            if dry_run or not accept_trade_offers:
                return result
            self._worker._apply_discovered_trade_offer(action, result)
            accept_result = self.trade_offer_service.accept_for_action(action, adapter.accept_trade_offer)
            return self._worker._carry_discovered_trade_offer_result(action, result, accept_result)
        return result

    @staticmethod
    def _count_final_state(result: ReconciliationRunResult, state: str) -> None:
        if state == PlatformActionState.SUCCEEDED:
            result.succeeded += 1
        elif state == PlatformActionState.CANCELLED:
            result.cancelled += 1
        elif state == PlatformActionState.FAILED:
            result.failed += 1
        elif state in RECONCILE_STATES:
            result.waiting += 1
