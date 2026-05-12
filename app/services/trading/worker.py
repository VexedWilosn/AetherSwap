from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from .actions import claim_due_actions, create_platform_action, schedule_retry, transition_action
from .adapters import (
    RESULT_CANCELLED,
    RESULT_LISTING_SUBMITTED,
    RESULT_ORDER_COMPLETED,
    RESULT_ORDER_PENDING,
    RESULT_MOBILE_CONFIRM_REQUIRED,
    RESULT_OFFER_DECLINED,
    RESULT_OFFER_EXPIRED,
    RESULT_REPRICE_SUBMITTED,
    RESULT_SAFE_MODE,
    RESULT_TRADE_OFFER_ACCEPTED,
    RESULT_VALIDATION_ERROR,
    NormalizedResult,
    PlatformAdapter,
    SafeModeAdapter,
)
from .exposure_guard import LowPriceExposureGuard
from .risk_budget import RiskBudgetService, RiskDecision
from .states import PlatformActionState
from .states import PlatformActionType
from .states import TERMINAL_STATES
from .settlement import materialize_purchase_for_action
from .trade_offers import TradeOfferService


def _json_payload(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_or_none(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


BUY_ACTION_TYPES = {
    PlatformActionType.DIRECT_BUY,
    PlatformActionType.PURCHASE_ORDER,
    PlatformActionType.STEAM_BUY_ORDER,
}


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
        app_config: dict | None = None,
        safe_mode: bool = True,
        lease_seconds: int = 60,
    ):
        self.session_factory = session_factory
        self.adapters = adapters or {}
        self.risk_budget = risk_budget or RiskBudgetService()
        self.trade_offer_service = trade_offer_service or TradeOfferService()
        self.app_config = app_config or {}
        self.exposure_guard = LowPriceExposureGuard(self.app_config)
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
                if str(action.state or "") in TERMINAL_STATES:
                    continue
                if self._defer_purchase_order_for_target(
                    session,
                    action,
                    claimed_from_state=claimed_from_state,
                    now=ts,
                ):
                    session.add(action)
                    counters["waiting"] += 1
                    continue
                if self._needs_manual_reconciliation(action, claimed_from_state):
                    transition_action(
                        action,
                        PlatformActionState.RISK_BLOCKED,
                        now=ts,
                        error_code="manual_reconcile_required",
                        error_message=(
                            "Recovered a processing action without an external order, listing, "
                            "or trade offer id; refusing to resubmit automatically."
                        ),
                    )
                    session.add(action)
                    counters["risk_blocked"] += 1
                    continue
                if self._needs_risk_check(action, claimed_from_state):
                    exposure_decision = self._exposure_decision(session, action, claimed_from_state=claimed_from_state)
                    if not exposure_decision.allowed:
                        transition_action(
                            action,
                            PlatformActionState.RISK_BLOCKED,
                            now=ts,
                            error_code=exposure_decision.reason,
                            error_message=exposure_decision.message or exposure_decision.reason,
                        )
                        session.add(action)
                        counters["risk_blocked"] += 1
                        continue
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
                self._materialize_purchase_if_settled(session, action)
                self._settle_parent_after_cancel(session, action, result, now=ts)
                session.add(action)
                self._coordinate_purchase_target(session, action, result, now=ts)

            if actions:
                session.commit()
            return WorkerRunResult(
                claimed=len(actions),
                succeeded=counters["succeeded"],
                waiting=counters["waiting"],
                failed=counters["failed"],
                risk_blocked=counters["risk_blocked"],
            )

    def _defer_purchase_order_for_target(self, session, action, *, claimed_from_state: str, now: float) -> bool:
        if str(action.action_type or "") != PlatformActionType.PURCHASE_ORDER:
            return False
        if claimed_from_state not in {
            PlatformActionState.QUEUED,
            PlatformActionState.RETRY_WAIT,
            PlatformActionState.PROCESSING,
        }:
            return False
        target_id = str(_json_payload(action.raw_context).get("purchase_target_id") or "").strip()
        if not target_id:
            return False
        active_direct_states = {
            PlatformActionState.QUEUED,
            PlatformActionState.SUBMITTED,
            PlatformActionState.WAITING_PLATFORM,
            PlatformActionState.WAITING_TRADE_OFFER,
            PlatformActionState.WAITING_STEAM_CONFIRM,
            PlatformActionState.WAITING_SETTLEMENT,
            PlatformActionState.RETRY_WAIT,
            PlatformActionState.PROCESSING,
        }
        try:
            from sqlmodel import select
            from app.database import PlatformAction
        except Exception:
            return False
        rows = session.execute(select(PlatformAction).where(PlatformAction.raw_context.contains(target_id))).scalars().all()
        for row in rows:
            if row.id == action.id:
                continue
            if str(row.action_type or "") != PlatformActionType.DIRECT_BUY:
                continue
            row_target_id = str(_json_payload(row.raw_context).get("purchase_target_id") or "").strip()
            if row_target_id != target_id:
                continue
            if str(row.state or "") not in active_direct_states:
                continue
            transition_action(
                action,
                PlatformActionState.RETRY_WAIT,
                now=now,
                next_check_at=now + 30,
                error_code="waiting_direct_buy",
                error_message=f"purchase target waiting direct_buy action {row.id}",
            )
            return True
        return False

    def _coordinate_purchase_target(self, session, action, result: NormalizedResult, *, now: float) -> None:
        if not result.success or result.filled_quantity is None or int(result.filled_quantity or 0) <= 0:
            return
        target_id = str(_json_payload(action.raw_context).get("purchase_target_id") or "").strip()
        if not target_id:
            return
        try:
            from sqlmodel import select
            from app.database import PlatformAction
        except Exception:
            return
        rows = session.execute(select(PlatformAction).where(PlatformAction.raw_context.contains(target_id))).scalars().all()
        active_order_rows = [
            row
            for row in rows
            if row.id != action.id
            and str(row.action_type or "") == PlatformActionType.PURCHASE_ORDER
            and str(row.state or "") in {
                PlatformActionState.QUEUED,
                PlatformActionState.PROCESSING,
                PlatformActionState.SUBMITTED,
                PlatformActionState.WAITING_PLATFORM,
                PlatformActionState.RETRY_WAIT,
            }
        ]
        if not active_order_rows:
            return
        target_quantity = max(
            int(_json_payload(row.raw_context).get("target_quantity") or 0)
            for row in rows
        )
        if target_quantity <= 0:
            return
        filled = sum(max(0, int(row.filled_quantity or 0)) for row in rows)
        if action.id and action not in rows:
            filled += max(0, int(action.filled_quantity or result.filled_quantity or 0))
        if filled < target_quantity:
            return
        for row in active_order_rows:
            updates = {
                "error_code": "purchase_target_filled",
                "error_message": "purchase target filled; cancel remote purchase order if already submitted",
            }
            next_state = str(row.state or "")
            has_remote_order = bool(row.platform_order_id or row.platform_listing_id or row.trade_offer_id)
            if next_state in {
                PlatformActionState.QUEUED,
                PlatformActionState.PROCESSING,
                PlatformActionState.RETRY_WAIT,
            } and not has_remote_order:
                updates.update(
                    {
                        "locked_budget_cny": 0.0,
                        "released_budget_cny": round(float(row.released_budget_cny or 0) + float(row.locked_budget_cny or 0), 2),
                    }
                )
                next_state = PlatformActionState.CANCELLED
            elif has_remote_order:
                updates.update(
                    {
                        "error_code": "purchase_target_remote_cancel_requested",
                        "error_message": "purchase target filled; remote cancel action has been queued",
                        "next_check_at": now + 60,
                    }
                )
                self._ensure_purchase_target_cancel_action(session, row, target_id=target_id, now=now)
            transition_action(row, next_state, now=now, **updates)
            session.add(row)

    @staticmethod
    def _ensure_purchase_target_cancel_action(session, target_action, *, target_id: str, now: float) -> None:
        try:
            from sqlmodel import select
            from app.database import PlatformAction
        except Exception:
            return
        existing_rows = session.execute(
            select(PlatformAction)
            .where(PlatformAction.action_type == PlatformActionType.CANCEL_ORDER)
            .where(PlatformAction.raw_context.contains(str(target_action.id)))
        ).scalars().all()
        for row in existing_rows:
            context = _json_payload(row.raw_context)
            if int(context.get("target_action_id") or 0) == int(target_action.id or 0):
                return
        request_payload = {
            "target_action_id": target_action.id,
            "target_action_type": target_action.action_type,
            "platform_order_id": target_action.platform_order_id,
            "platform_listing_id": target_action.platform_listing_id,
            "trade_offer_id": target_action.trade_offer_id,
            "order_id": target_action.platform_order_id,
            "buy_order_id": target_action.platform_order_id,
        }
        create_platform_action(
            session,
            action_type=PlatformActionType.CANCEL_ORDER,
            platform=target_action.platform,
            item_id=int(target_action.item_id or 0),
            market_hash_name=str(target_action.market_hash_name or ""),
            quantity=1,
            locked_budget_cny=0.0,
            channel=str(target_action.channel or "purchase_target_cancel"),
            next_check_at=now,
            idempotency_key=f"purchase_target_cancel:{target_id}:{target_action.id}",
            request_payload=request_payload,
            raw_context={
                "purchase_target_id": target_id,
                "target_action_id": target_action.id,
                "target_action_type": target_action.action_type,
                "reason": "purchase_target_filled",
            },
            commit=False,
        )

    @staticmethod
    def _settle_parent_after_cancel(session, action, result: NormalizedResult, *, now: float) -> None:
        if str(action.action_type or "") != PlatformActionType.CANCEL_ORDER:
            return
        if not result.success or result.category != RESULT_CANCELLED:
            return
        payload = _json_payload(action.raw_context)
        payload.update(_json_payload(action.request_payload))
        target_action_id = int(payload.get("target_action_id") or payload.get("cancels_action_id") or 0)
        if target_action_id <= 0:
            return
        try:
            from app.database import PlatformAction
        except Exception:
            return
        target = session.get(PlatformAction, target_action_id)
        if target is None or str(target.state or "") in TERMINAL_STATES:
            return
        locked_budget = float(target.locked_budget_cny or 0)
        transition_action(
            target,
            PlatformActionState.CANCELLED,
            now=now,
            locked_budget_cny=0.0,
            released_budget_cny=round(float(target.released_budget_cny or 0) + locked_budget, 2),
            error_code="remote_cancelled_by_action",
            error_message=f"remote cancel action {action.id} succeeded",
        )
        session.add(target)
        try:
            materialize_purchase_for_action(session, target)
        except Exception:
            return

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

    def _exposure_decision(self, session, action, *, claimed_from_state: str):
        if not self._needs_exposure_check(action, claimed_from_state):
            return self.exposure_guard.check(
                session,
                item_id=int(action.item_id or 0),
                market_hash_name=action.market_hash_name,
                unit_price=0,
                proposed_quantity=0,
            )
        return self.exposure_guard.check(
            session,
            item_id=int(action.item_id or 0),
            market_hash_name=action.market_hash_name,
            unit_price=float(action.target_price or 0),
            proposed_quantity=max(1, int(action.quantity or 1)),
            exclude_action_id=action.id,
            fail_closed=True,
        )

    @staticmethod
    def _needs_exposure_check(action, claimed_from_state: str) -> bool:
        if str(action.action_type or "") not in {
            PlatformActionType.DIRECT_BUY,
            PlatformActionType.PURCHASE_ORDER,
            PlatformActionType.STEAM_BUY_ORDER,
        }:
            return False
        if bool(action.platform_order_id or action.trade_offer_id or action.platform_listing_id):
            return False
        return claimed_from_state in {
            PlatformActionState.QUEUED,
            PlatformActionState.RETRY_WAIT,
            PlatformActionState.PROCESSING,
        }

    def _submit_for_state(self, adapter: PlatformAdapter, action, claimed_from_state: str) -> NormalizedResult:
        if claimed_from_state == PlatformActionState.WAITING_PLATFORM:
            result = adapter.poll_order(action)
            if self._should_accept_discovered_trade_offer(action, result):
                self._apply_discovered_trade_offer(action, result)
                accept_result = self.trade_offer_service.accept_for_action(action, adapter.accept_trade_offer)
                return self._carry_discovered_trade_offer_result(action, result, accept_result)
            return result
        if claimed_from_state == PlatformActionState.WAITING_TRADE_OFFER:
            return self._accept_or_discover_trade_offer(adapter, action)
        if claimed_from_state in {
            PlatformActionState.SUBMITTED,
            PlatformActionState.WAITING_STEAM_CONFIRM,
            PlatformActionState.WAITING_SETTLEMENT,
        }:
            return adapter.poll_order(action)
        if claimed_from_state == PlatformActionState.PROCESSING:
            if action.trade_offer_id:
                return self._accept_or_discover_trade_offer(adapter, action)
            if action.platform_order_id or action.platform_listing_id:
                return adapter.poll_order(action)
            return NormalizedResult(
                False,
                RESULT_VALIDATION_ERROR,
                "processing action requires manual reconciliation before retry",
            )
        return adapter.submit(action)

    def _accept_or_discover_trade_offer(self, adapter: PlatformAdapter, action) -> NormalizedResult:
        if action.trade_offer_id:
            return self.trade_offer_service.accept_for_action(action, adapter.accept_trade_offer)
        return adapter.accept_trade_offer(action)

    @staticmethod
    def _should_accept_discovered_trade_offer(action, result: NormalizedResult) -> bool:
        if str(action.action_type or "") not in BUY_ACTION_TYPES:
            return False
        if not result.success or not result.trade_offer_id:
            return False
        if result.category not in {"", RESULT_ORDER_PENDING, RESULT_TRADE_OFFER_ACCEPTED, "success"}:
            return False
        return True

    @staticmethod
    def _apply_discovered_trade_offer(action, result: NormalizedResult) -> None:
        action.trade_offer_id = result.trade_offer_id or action.trade_offer_id
        action.assetid = result.assetid or action.assetid
        action.platform_order_id = result.platform_order_id or action.platform_order_id
        action.platform_listing_id = result.platform_listing_id or action.platform_listing_id
        if result.request_payload is not None:
            action.request_payload = _json_or_none(result.request_payload)
        if result.response_payload is not None:
            action.response_payload = _json_or_none(result.response_payload)

    @staticmethod
    def _carry_discovered_trade_offer_result(
        action,
        poll_result: NormalizedResult,
        accept_result: NormalizedResult,
    ) -> NormalizedResult:
        accept_result.trade_offer_id = accept_result.trade_offer_id or poll_result.trade_offer_id or action.trade_offer_id
        accept_result.assetid = accept_result.assetid or poll_result.assetid or action.assetid
        accept_result.platform_order_id = accept_result.platform_order_id or poll_result.platform_order_id or action.platform_order_id
        accept_result.platform_listing_id = (
            accept_result.platform_listing_id
            or poll_result.platform_listing_id
            or action.platform_listing_id
        )
        return accept_result

    @staticmethod
    def _needs_manual_reconciliation(action, claimed_from_state: str) -> bool:
        if claimed_from_state != PlatformActionState.PROCESSING:
            return False
        return not bool(action.platform_order_id or action.platform_listing_id or action.trade_offer_id)

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
            terminal_success = (
                result.category in {
                RESULT_SAFE_MODE,
                RESULT_ORDER_COMPLETED,
                RESULT_TRADE_OFFER_ACCEPTED,
                RESULT_LISTING_SUBMITTED,
                RESULT_REPRICE_SUBMITTED,
                }
                or claimed_from_state
                in {
                    PlatformActionState.WAITING_TRADE_OFFER,
                    PlatformActionState.WAITING_STEAM_CONFIRM,
                    PlatformActionState.WAITING_SETTLEMENT,
                }
            ) and result.category not in {RESULT_CANCELLED, RESULT_ORDER_PENDING}
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
            if category in {RESULT_ORDER_COMPLETED, RESULT_TRADE_OFFER_ACCEPTED} or claimed_from_state in {
                PlatformActionState.WAITING_TRADE_OFFER,
                PlatformActionState.WAITING_STEAM_CONFIRM,
                PlatformActionState.WAITING_SETTLEMENT,
            }:
                transition_action(action, PlatformActionState.SUCCEEDED, now=now, **updates)
                counters["succeeded"] += 1
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
            self._apply_failure_metadata(action, result)
            schedule_retry(
                action,
                error_code=self._result_error_code(result),
                error_message=self._result_error_message(result),
                now=now,
            )
            if str(action.state or "") == PlatformActionState.FAILED:
                self._apply_terminal_failure_budget_release(action, result)
            counters["failed"] += 1
            return

        self._apply_failure_metadata(action, result)
        failure_updates = self._terminal_failure_budget_release_updates(action, result)
        transition_action(
            action,
            PlatformActionState.FAILED,
            now=now,
            error_code=self._result_error_code(result),
            error_message=self._result_error_message(result),
            response_payload=result.response_payload,
            **failure_updates,
        )
        counters["failed"] += 1

    @staticmethod
    def _apply_failure_metadata(action, result: NormalizedResult) -> None:
        action.platform_order_id = result.platform_order_id or action.platform_order_id
        action.platform_listing_id = result.platform_listing_id or action.platform_listing_id
        action.trade_offer_id = result.trade_offer_id or action.trade_offer_id
        action.assetid = result.assetid or action.assetid
        if result.request_payload is not None:
            action.request_payload = _json_or_none(result.request_payload)
        if result.response_payload is not None:
            action.response_payload = _json_or_none(result.response_payload)

    @staticmethod
    def _result_error_code(result: NormalizedResult) -> str:
        payload = result.response_payload if isinstance(result.response_payload, dict) else {}
        reason = str(payload.get("reason") or payload.get("error_code") or "").strip()
        if reason in {
            "steam_auth_required",
            RESULT_OFFER_EXPIRED,
            RESULT_OFFER_DECLINED,
            RESULT_MOBILE_CONFIRM_REQUIRED,
        }:
            return reason
        return str(result.category or reason or "platform_action_failed")

    @staticmethod
    def _result_error_message(result: NormalizedResult) -> str:
        payload = result.response_payload if isinstance(result.response_payload, dict) else {}
        return str(
            payload.get("msg")
            or payload.get("message")
            or result.message
            or payload.get("reason")
            or result.category
            or ""
        )

    @staticmethod
    def _materialize_purchase_if_settled(session, action) -> None:
        try:
            materialize_purchase_for_action(session, action)
        except Exception:
            return

    @staticmethod
    def _terminal_failure_budget_release_updates(action, result: NormalizedResult) -> dict[str, float | int | None]:
        action_type = str(action.action_type or "")
        if action_type not in {
            PlatformActionType.DIRECT_BUY,
            PlatformActionType.PURCHASE_ORDER,
            PlatformActionType.STEAM_BUY_ORDER,
        }:
            return {}
        has_remote_claim = bool(
            action.platform_order_id
            or action.platform_listing_id
            or action.trade_offer_id
            or result.platform_order_id
            or result.platform_listing_id
            or result.trade_offer_id
        )
        if has_remote_claim:
            return {}
        locked_budget = round(float(action.locked_budget_cny or 0), 2)
        if locked_budget <= 0:
            return {}
        released_budget = round(float(action.released_budget_cny or 0) + locked_budget, 2)
        remaining_quantity = action.remaining_quantity
        if remaining_quantity is None:
            remaining_quantity = max(0, int(action.quantity or 0) - int(action.filled_quantity or 0))
        return {
            "locked_budget_cny": 0.0,
            "released_budget_cny": released_budget,
            "remaining_quantity": remaining_quantity,
        }

    def _apply_terminal_failure_budget_release(self, action, result: NormalizedResult) -> None:
        for key, value in self._terminal_failure_budget_release_updates(action, result).items():
            setattr(action, key, value)

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

        if cancelled:
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
