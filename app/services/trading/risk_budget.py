from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import or_
from sqlmodel import select

from app.database import PlatformAction
from .actions import active_states
from .risk_categories import risk_category_from_market_hash_name
from .states import PlatformActionState


@dataclass(frozen=True)
class RiskBudgetConfig:
    max_drawdown_rate: float = 0.20
    max_single_item_budget_cny: float = 3000.0
    max_single_category_budget_cny: float = 5000.0
    max_platform_daily_auto_cny: float = 5000.0
    max_steam_balance_lock_days: int = 5
    min_expected_profit_rate: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    locked_budget_cny: float = 0.0
    current_item_budget_cny: float = 0.0
    current_category_budget_cny: float = 0.0
    current_platform_daily_cny: float = 0.0


class RiskBudgetService:
    def __init__(self, config: RiskBudgetConfig | None = None):
        self.config = config or RiskBudgetConfig()

    def check_new_action(
        self,
        session,
        *,
        platform: str,
        item_id: int,
        market_hash_name: str = "",
        risk_category: str = "",
        target_price: float | None = None,
        quantity: int = 1,
        locked_budget_cny: float | None = None,
        expected_profit_rate: float | None = None,
        steam_balance_lock_days: int | None = None,
        exclude_action_id: int | None = None,
        now: float | None = None,
    ) -> RiskDecision:
        quantity = max(1, int(quantity or 1))
        proposed_budget = locked_budget_cny
        if proposed_budget is None:
            proposed_budget = float(target_price or 0) * quantity
        proposed_budget = round(float(proposed_budget or 0), 2)

        if expected_profit_rate is not None and float(expected_profit_rate) < self.config.min_expected_profit_rate:
            return RiskDecision(False, "profit_floor_lock", proposed_budget)
        if steam_balance_lock_days is not None and int(steam_balance_lock_days) > self.config.max_steam_balance_lock_days:
            return RiskDecision(False, "steam_balance_lock_too_long", proposed_budget)
        if proposed_budget > self.config.max_single_item_budget_cny:
            return RiskDecision(False, "single_item_budget_exceeded", proposed_budget)

        item_budget = self._active_budget_for_item(session, int(item_id or 0), exclude_action_id=exclude_action_id)
        if item_budget + proposed_budget > self.config.max_single_item_budget_cny:
            return RiskDecision(False, "single_item_active_budget_exceeded", proposed_budget, item_budget)

        category = str(risk_category or "").strip() or risk_category_from_market_hash_name(market_hash_name)
        category_budget = self._active_budget_for_category(
            session,
            category,
            market_hash_name=market_hash_name,
            exclude_action_id=exclude_action_id,
        )
        if category_budget + proposed_budget > self.config.max_single_category_budget_cny:
            return RiskDecision(
                False,
                "single_category_budget_exceeded",
                proposed_budget,
                item_budget,
                category_budget,
            )

        platform_daily = self._platform_daily_budget(session, platform, now=now, exclude_action_id=exclude_action_id)
        if platform_daily + proposed_budget > self.config.max_platform_daily_auto_cny:
            return RiskDecision(
                False,
                "platform_daily_budget_exceeded",
                proposed_budget,
                item_budget,
                category_budget,
                platform_daily,
            )

        return RiskDecision(
            True,
            "",
            proposed_budget,
            item_budget,
            category_budget,
            platform_daily,
        )

    def check_action(self, session, action: PlatformAction, *, now: float | None = None) -> RiskDecision:
        return self.check_new_action(
            session,
            platform=action.platform,
            item_id=action.item_id,
            market_hash_name=action.market_hash_name,
            risk_category=action.risk_category,
            target_price=action.target_price,
            quantity=action.quantity,
            locked_budget_cny=action.locked_budget_cny,
            expected_profit_rate=action.expected_profit_rate,
            exclude_action_id=action.id,
            now=now,
        )

    def _active_budget_for_item(self, session, item_id: int, *, exclude_action_id: int | None = None) -> float:
        if not item_id:
            return 0.0
        stmt = (
            select(PlatformAction.locked_budget_cny)
            .where(PlatformAction.item_id == item_id)
            .where(PlatformAction.state.in_(list(active_states())))
        )
        if exclude_action_id is not None:
            stmt = stmt.where(PlatformAction.id != int(exclude_action_id))
        rows = session.execute(stmt).scalars().all()
        return sum(float(x or 0) for x in rows)

    def _active_budget_for_category(
        self,
        session,
        risk_category: str,
        *,
        market_hash_name: str = "",
        exclude_action_id: int | None = None,
    ) -> float:
        category = str(risk_category or "").strip()
        name = str(market_hash_name or "").strip()
        if not category:
            return 0.0
        stmt = (
            select(PlatformAction.locked_budget_cny)
            .where(
                or_(
                    PlatformAction.risk_category == category,
                    (
                        (PlatformAction.risk_category.is_(None) | (PlatformAction.risk_category == ""))
                        & (PlatformAction.market_hash_name == name)
                    ),
                )
            )
            .where(PlatformAction.state.in_(list(active_states())))
        )
        if exclude_action_id is not None:
            stmt = stmt.where(PlatformAction.id != int(exclude_action_id))
        rows = session.execute(stmt).scalars().all()
        return sum(float(x or 0) for x in rows)

    def _platform_daily_budget(self, session, platform: str, *, now: float | None = None, exclude_action_id: int | None = None) -> float:
        ts = time.time() if now is None else float(now)
        local = time.localtime(ts)
        try:
            start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst))
        except (OverflowError, OSError, ValueError):
            start = max(0.0, ts - 86400)
        stmt = (
            select(PlatformAction.locked_budget_cny, PlatformAction.filled_amount_cny)
            .where(PlatformAction.platform == str(platform or "").lower().strip())
            .where(PlatformAction.created_at >= start)
            .where(PlatformAction.state != PlatformActionState.CANCELLED)
            .where(PlatformAction.state != PlatformActionState.FAILED)
            .where(PlatformAction.state != PlatformActionState.EXPIRED)
        )
        if exclude_action_id is not None:
            stmt = stmt.where(PlatformAction.id != int(exclude_action_id))
        rows = session.execute(stmt).all()
        return sum(float(locked or 0) + float(filled or 0) for locked, filled in rows)
