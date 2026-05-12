from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .actions import claim_due_actions, schedule_retry, transition_action
from .adapters import (
    RESULT_CANCELLED,
    RESULT_LISTING_SUBMITTED,
    RESULT_ORDER_COMPLETED,
    RESULT_ORDER_PENDING,
    RESULT_REPRICE_SUBMITTED,
    RESULT_SAFE_MODE,
    RESULT_TRADE_OFFER_ACCEPTED,
    NormalizedResult,
    PlatformAdapter,
    SafeModeAdapter,
)
from .risk_budget import RiskBudgetService, RiskDecision
from .states import PlatformActionState
from .states import PlatformActionType
from .trade_offers import TradeOfferService


@dataclass(frozen=True)
class WorkerRunResult:
    claimed: int
    succeeded: int
    waiting: int
    failed: int
    risk_blocked: int


class PlatformActionWorker:
    def __init__(
        self,
        session_factory: Callable,
        *,
        adapters: dict[str, PlatformAdapter] | None = None,
        risk_budget: RiskBudgetService | None = None,
        trade_offer_service: TradeOfferService | None = None,
        safe_mode: bool = True,
        lease_seconds: int = 60,
    ):
        self.session_factory = session_factory
        self.adapters = adapters or {}
        self.risk_budget = risk_budget or RiskBudgetService()
        self.trade_offer_service = trade_offer_service or TradeOfferService()
        self.safe_mode = bool(safe_mode)
        self.lease_seconds = max(1, int(lease_seconds or 1))
        self.safe_adapter = SafeModeAdapter()

    def run_once(self, *, now: float | None = None, limit: int = 10) -> WorkerRunResult:
        ts = time.time() if now is None else float(now)
        with self.session_factory() as session:
            actions = claim_due_actions(session, now=ts, limit=limit, lease_seconds=self.lease_seconds)
            counters = {"succeeded": 0, "waiting": 0, "failed": 0, "risk_blocked": 0}

            for action in actions:
                claimed_from_state = str(getattr(action, "_claimed_from_state", "") or action.state or "")
                if self._needs_risk_check(action, claimed_from_state):
                    decision = self._risk_decision(session, action, now=ts)
                    if not decision.allowed:
                        transition_action(
                            action,
                            PlatformActionState.RISK_BLOCKED,
                            now=ts,
                            error_code=decision.reason,
                            error_message=decision.reason,
                        )
                        session.add(action)
                        counters["risk_blocked"] += 1
                        continue

                adapter = self.safe_adapter if self.safe_mode else self.adapters.get(action.platform)
                if adapter is None:
                    schedule_retry(
                        action,
                        error_code="adapter_missing",
                        error_message=f"adapter missing for {action.platform}",
                        now=ts,
                    )
                    session.add(action)
                    counters["failed"] += 1
                    continue

                result = self._submit_for_state(adapter, action, claimed_from_state)
                self._apply_result(action, result, ts, counters, claimed_from_state=claimed_from_state)
                session.add(action)

            if actions:
                session.commit()
            return WorkerRunResult(
                claimed=len(actions),
                succeeded=counters["succeeded"],
                waiting=counters["waiting"],
                failed=counters["failed"],
                risk_blocked=counters["risk_blocked"],
            )

    @staticmethod
    def _needs_risk_check(action, claimed_from_state: str) -> bool:
        if str(action.action_type or "") in {
            PlatformActionType.CANCEL_ORDER,
            PlatformActionType.DELIVER_ORDER,
            PlatformActionType.ACCEPT_TRADE_OFFER,
            PlatformActionType.POLL_ORDER,
        }:
            return False
        return claimed_from_state in {
            PlatformActionState.QUEUED,
            PlatformActionState.RETRY_WAIT,
            PlatformActionState.PROCESSING,
        }

    def _risk_decision(self, session, action, *, now: float):
        if str(action.action_type or "") in {
            PlatformActionType.STEAM_LISTING,
            PlatformActionType.PLATFORM_LISTING,
            PlatformActionType.REPRICE_LISTING,
        }:
            if action.expected_profit_rate is not None and float(action.expected_profit_rate) < self.risk_budget.config.min_expected_profit_rate:
                return RiskDecision(False, "profit_floor_lock", 0)
            return RiskDecision(True, "", 0)
        return self.risk_budget.check_action(session, action, now=now)

    def _submit_for_state(self, adapter: PlatformAdapter, action, claimed_from_state: str) -> NormalizedResult:
        if claimed_from_state == PlatformActionState.WAITING_PLATFORM:
            return adapter.poll_order(action)
        if claimed_from_state == PlatformActionState.WAITING_TRADE_OFFER:
            return self.trade_offer_service.accept_for_action(action, adapter.accept_trade_offer)
        return adapter.submit(action)

    def _apply_result(
        self,
        action,
        result: NormalizedResult,
        now: float,
        counters: dict[str, int],
        *,
        claimed_from_state: str,
    ) -> None:
        if result.success:
            terminal_success = result.category in {
                RESULT_SAFE_MODE,
                RESULT_ORDER_COMPLETED,
                RESULT_TRADE_OFFER_ACCEPTED,
                RESULT_LISTING_SUBMITTED,
                RESULT_REPRICE_SUBMITTED,
            } or claimed_from_state in {
                PlatformActionState.WAITING_TRADE_OFFER,
                PlatformActionState.WAITING_STEAM_CONFIRM,
                PlatformActionState.WAITING_SETTLEMENT,
            }
            updates = {
                "platform_order_id": result.platform_order_id or action.platform_order_id,
                "platform_listing_id": result.platform_listing_id or action.platform_listing_id,
                "trade_offer_id": result.trade_offer_id or action.trade_offer_id,
                "assetid": result.assetid or action.assetid,
                "request_payload": result.request_payload,
                "response_payload": result.response_payload,
                "error_code": "",
                "error_message": "",
            }
            updates.update(
                self._fill_progress_updates(
                    action,
                    result,
                    terminal_success=terminal_success,
                    cancelled=result.category == RESULT_CANCELLED,
                )
            )
            category = result.category
            if (
                category == RESULT_ORDER_PENDING
                and int(updates.get("filled_quantity") or action.filled_quantity or 0) > 0
                and int(updates.get("remaining_quantity") or action.remaining_quantity or 0) <= 0
            ):
                category = RESULT_ORDER_COMPLETED
            if category == RESULT_SAFE_MODE:
                transition_action(action, PlatformActionState.SUCCEEDED, now=now, **updates)
                counters["succeeded"] += 1
                return
            if category in {RESULT_ORDER_COMPLETED, RESULT_TRADE_OFFER_ACCEPTED} or claimed_from_state in {
                PlatformActionState.WAITING_TRADE_OFFER,
                PlatformActionState.WAITING_STEAM_CONFIRM,
                PlatformActionState.WAITING_SETTLEMENT,
            }:
                transition_action(action, PlatformActionState.SUCCEEDED, now=now, **updates)
                counters["succeeded"] += 1
                return
            if category == RESULT_CANCELLED:
                transition_action(action, PlatformActionState.CANCELLED, now=now, **updates)
                counters["succeeded"] += 1
                return
            if category in {RESULT_LISTING_SUBMITTED, RESULT_REPRICE_SUBMITTED}:
                transition_action(action, PlatformActionState.SUCCEEDED, now=now, **updates)
                counters["succeeded"] += 1
                return
            if category == RESULT_ORDER_PENDING:
                if result.trade_offer_id:
                    transition_action(
                        action,
                        PlatformActionState.WAITING_TRADE_OFFER,
                        now=now,
                        next_check_at=now + 30,
                        **updates,
                    )
                    counters["waiting"] += 1
                    return
                transition_action(
                    action,
                    PlatformActionState.WAITING_PLATFORM,
                    now=now,
                    next_check_at=now + 60,
                    **updates,
                )
                counters["waiting"] += 1
                return
            if result.trade_offer_id:
                transition_action(
                    action,
                    PlatformActionState.WAITING_TRADE_OFFER,
                    now=now,
                    next_check_at=now + 30,
                    **updates,
                )
            else:
                transition_action(
                    action,
                    PlatformActionState.WAITING_PLATFORM,
                    now=now,
                    next_check_at=now + 60,
                    **updates,
                )
            counters["waiting"] += 1
            return

        if result.retriable:
            schedule_retry(action, error_code=result.category, error_message=result.message, now=now)
            counters["failed"] += 1
            return

        transition_action(
            action,
            PlatformActionState.FAILED,
            now=now,
            error_code=result.category,
            error_message=result.message,
            response_payload=result.response_payload,
        )
        counters["failed"] += 1

    @staticmethod
    def _fill_progress_updates(
        action,
        result: NormalizedResult,
        *,
        terminal_success: bool = False,
        cancelled: bool = False,
    ) -> dict[str, float | int | None]:
        action_type = str(action.action_type or "")
        buy_like = action_type in {
            PlatformActionType.DIRECT_BUY,
            PlatformActionType.PURCHASE_ORDER,
            PlatformActionType.STEAM_BUY_ORDER,
            PlatformActionType.POLL_ORDER,
        }
        has_fill_signal = any(
            value is not None
            for value in (
                result.filled_quantity,
                result.remaining_quantity,
                result.filled_amount_cny,
                result.remaining_amount_cny,
            )
        )
        should_track_fill = buy_like or has_fill_signal
        quantity = max(0, int(action.quantity or 0))
        previous_locked = round(float(action.locked_budget_cny or 0), 2)
        previous_filled = max(0, int(action.filled_quantity or 0))
        previous_amount = round(float(action.filled_amount_cny or 0), 2)
        previous_released = round(float(action.released_budget_cny or 0), 2)
        target_price = float(action.target_price or 0)

        filled_quantity: int | None = None
        if result.filled_quantity is not None:
            filled_quantity = max(previous_filled, max(0, int(result.filled_quantity or 0)))
        elif terminal_success and should_track_fill and quantity:
            filled_quantity = max(previous_filled, quantity)
        elif should_track_fill:
            filled_quantity = previous_filled

        remaining_quantity: int | None = None
        if result.remaining_quantity is not None:
            remaining_quantity = max(0, int(result.remaining_quantity or 0))
        elif filled_quantity is not None and quantity:
            remaining_quantity = max(0, quantity - filled_quantity)
        elif cancelled and should_track_fill and quantity:
            remaining_quantity = max(0, quantity - previous_filled)

        if terminal_success and should_track_fill:
            remaining_quantity = 0
        if cancelled and remaining_quantity is None and should_track_fill and quantity:
            remaining_quantity = max(0, quantity - (filled_quantity or previous_filled))

        filled_amount: float | None = None
        if result.filled_amount_cny is not None:
            filled_amount = max(previous_amount, round(float(result.filled_amount_cny or 0), 2))
        elif filled_quantity is not None and target_price > 0:
            filled_amount = max(previous_amount, round(target_price * filled_quantity, 2))

        remaining_budget: float | None = None
        if result.remaining_amount_cny is not None:
            remaining_budget = round(max(0.0, float(result.remaining_amount_cny or 0)), 2)
        elif remaining_quantity is not None and target_price > 0:
            remaining_budget = round(max(0, remaining_quantity) * target_price, 2)
        elif terminal_success or cancelled:
            remaining_budget = 0.0

        if remaining_budget is not None:
            if not terminal_success and not cancelled and previous_locked > 0:
                remaining_budget = min(previous_locked, remaining_budget)
            release_delta = max(0.0, previous_locked - remaining_budget)
            released_budget = round(previous_released + release_delta, 2)
        else:
            released_budget = previous_released

        updates: dict[str, float | int | None] = {}
        if should_track_fill:
            updates["filled_quantity"] = filled_quantity if filled_quantity is not None else previous_filled
            updates["remaining_quantity"] = remaining_quantity
            updates["filled_amount_cny"] = filled_amount if filled_amount is not None else previous_amount
        if remaining_budget is not None:
            updates["locked_budget_cny"] = remaining_budget
            updates["released_budget_cny"] = released_budget
        return updates
