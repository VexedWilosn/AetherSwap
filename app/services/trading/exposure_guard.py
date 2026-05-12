from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from sqlmodel import select

from app.database import PlatformAction, Purchase

from .states import PlatformActionState, PlatformActionType


LOW_PRICE_EXPOSURE_REASON = "low_price_exposure_quota"
LOW_PRICE_EXPOSURE_RULE_INVALID = "low_price_exposure_rule_invalid"

BUY_ACTION_TYPES = {
    PlatformActionType.DIRECT_BUY,
    PlatformActionType.PURCHASE_ORDER,
    PlatformActionType.STEAM_BUY_ORDER,
}

ACTIVE_BUY_STATES = {
    PlatformActionState.QUEUED,
    PlatformActionState.PROCESSING,
    PlatformActionState.SUBMITTED,
    PlatformActionState.WAITING_PLATFORM,
    PlatformActionState.WAITING_TRADE_OFFER,
    PlatformActionState.WAITING_STEAM_CONFIRM,
    PlatformActionState.WAITING_SETTLEMENT,
    PlatformActionState.RETRY_WAIT,
}

DEFAULT_LOW_PRICE_EXPOSURE_GUARD: dict[str, Any] = {
    "enabled": True,
    "rule": "0-0-0.02-2-0.05-4-0.10-8-0.30",
    "price_basis": "buy_price",
    "hide_signals": True,
    "block_execution": True,
    "cache_ttl_seconds": 30,
    "include_inventory": True,
    "include_purchases": True,
    "include_active_orders": True,
    "include_pending_receipt": True,
}

_EXPOSURE_CACHE: dict[tuple[Any, ...], tuple[float, ExposureBreakdown]] = {}


@dataclass(frozen=True)
class ExposureRuleInterval:
    min_price: float
    max_price: float
    max_quantity: int

    def contains(self, price: float, *, is_last: bool = False) -> bool:
        if price < self.min_price:
            return False
        if is_last:
            return price <= self.max_price
        return price < self.max_price

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExposureBreakdown:
    purchases: int = 0
    pending_receipt: int = 0
    active_orders: int = 0
    inventory: int = 0

    @property
    def held_total(self) -> int:
        return max(max(0, int(self.purchases or 0)), max(0, int(self.inventory or 0)))

    @property
    def total(self) -> int:
        return (
            self.held_total
            + max(0, int(self.pending_receipt or 0))
            + max(0, int(self.active_orders or 0))
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["held_total"] = self.held_total
        data["total"] = self.total
        return data


@dataclass(frozen=True)
class ExposureDecision:
    allowed: bool
    reason: str = ""
    item_id: int = 0
    market_hash_name: str = ""
    unit_price: float = 0.0
    proposed_quantity: int = 0
    current_quantity: int = 0
    projected_quantity: int = 0
    max_quantity: int | None = None
    interval: ExposureRuleInterval | None = None
    breakdown: ExposureBreakdown | None = None
    rule: str = ""
    message: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked": self.blocked,
            "reason": self.reason,
            "item_id": self.item_id,
            "market_hash_name": self.market_hash_name,
            "unit_price": self.unit_price,
            "proposed_quantity": self.proposed_quantity,
            "current_quantity": self.current_quantity,
            "projected_quantity": self.projected_quantity,
            "max_quantity": self.max_quantity,
            "interval": self.interval.to_dict() if self.interval else None,
            "breakdown": self.breakdown.to_dict() if self.breakdown else None,
            "rule": self.rule,
            "message": self.message,
        }


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def low_price_exposure_guard_config(app_config: dict[str, Any] | None) -> dict[str, Any]:
    raw = app_config if isinstance(app_config, dict) else {}
    section = raw.get("low_price_exposure_guard")
    if not isinstance(section, dict):
        flat_keys = set(DEFAULT_LOW_PRICE_EXPOSURE_GUARD) - {"enabled"}
        if any(key in raw for key in flat_keys):
            section = raw
        else:
            section = {}
    cfg = dict(DEFAULT_LOW_PRICE_EXPOSURE_GUARD)
    cfg.update(section)
    cfg["enabled"] = _to_bool(cfg.get("enabled"), True)
    cfg["hide_signals"] = _to_bool(cfg.get("hide_signals"), True)
    cfg["block_execution"] = _to_bool(cfg.get("block_execution"), True)
    cfg["include_inventory"] = _to_bool(cfg.get("include_inventory"), True)
    cfg["include_purchases"] = _to_bool(cfg.get("include_purchases"), True)
    cfg["include_active_orders"] = _to_bool(cfg.get("include_active_orders"), True)
    cfg["include_pending_receipt"] = _to_bool(cfg.get("include_pending_receipt"), True)
    cfg["cache_ttl_seconds"] = max(0, _to_int(cfg.get("cache_ttl_seconds"), 30))
    cfg["rule"] = str(cfg.get("rule") or DEFAULT_LOW_PRICE_EXPOSURE_GUARD["rule"]).strip()
    cfg["price_basis"] = str(cfg.get("price_basis") or "buy_price").strip().lower()
    return cfg


def parse_low_price_exposure_rule(rule: str) -> list[ExposureRuleInterval]:
    tokens = [token.strip() for token in re.split(r"\s*-\s*", str(rule or "").strip()) if token.strip()]
    if len(tokens) < 3 or len(tokens) % 2 == 0:
        raise ValueError("rule must use price-quantity-price-quantity-price format")

    prices: list[float] = []
    quantities: list[int] = []
    for index, token in enumerate(tokens):
        if index % 2 == 0:
            price = _to_float(token, -1.0)
            if price < 0:
                raise ValueError(f"invalid price boundary: {token}")
            prices.append(price)
        else:
            quantity = _to_int(token, -1)
            if quantity < 0:
                raise ValueError(f"invalid quantity limit: {token}")
            quantities.append(quantity)

    for left, right in zip(prices, prices[1:]):
        if right <= left:
            raise ValueError("price boundaries must be strictly increasing")

    return [
        ExposureRuleInterval(
            min_price=round(prices[index], 8),
            max_price=round(prices[index + 1], 8),
            max_quantity=int(quantities[index]),
        )
        for index in range(len(quantities))
    ]


def _loads_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _matches_inventory_record(record: dict[str, Any], *, item_id: int, market_hash_name: str) -> bool:
    raw_item_id = record.get("item_id") or record.get("goods_id")
    if raw_item_id not in (None, "") and _to_int(raw_item_id, 0) == int(item_id or 0):
        return True
    raw_name = str(record.get("market_hash_name") or record.get("name") or record.get("item_name") or "").strip()
    return bool(raw_name and raw_name == str(market_hash_name or "").strip())


def _record_quantity(record: dict[str, Any]) -> int:
    return max(1, _to_int(record.get("quantity") or record.get("num") or record.get("count") or 1, 1))


class LowPriceExposureGuard:
    def __init__(self, app_config: dict[str, Any] | None = None):
        self.config = low_price_exposure_guard_config(app_config)
        self._inventory_cache: list[dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def hide_signals(self) -> bool:
        return bool(self.config.get("hide_signals"))

    @property
    def block_execution(self) -> bool:
        return bool(self.config.get("block_execution"))

    def intervals(self) -> list[ExposureRuleInterval]:
        return parse_low_price_exposure_rule(str(self.config.get("rule") or ""))

    def preview(self, rule: str | None = None) -> dict[str, Any]:
        selected_rule = str(rule if rule is not None else self.config.get("rule") or "").strip()
        intervals = parse_low_price_exposure_rule(selected_rule)
        return {"rule": selected_rule, "intervals": [interval.to_dict() for interval in intervals]}

    def check(
        self,
        session,
        *,
        item_id: int,
        market_hash_name: str = "",
        unit_price: float,
        proposed_quantity: int = 1,
        exclude_action_id: int | None = None,
        fail_closed: bool = False,
        use_cache: bool = False,
    ) -> ExposureDecision:
        item_id = int(item_id or 0)
        market_hash_name = str(market_hash_name or "")
        unit_price = round(_to_float(unit_price, 0.0), 8)
        proposed_quantity = max(0, _to_int(proposed_quantity, 0))
        rule = str(self.config.get("rule") or "")

        if not self.enabled:
            return ExposureDecision(True, "disabled", item_id, market_hash_name, unit_price, proposed_quantity, rule=rule)
        if proposed_quantity > 0 and not self.block_execution:
            return ExposureDecision(True, "block_disabled", item_id, market_hash_name, unit_price, proposed_quantity, rule=rule)
        if unit_price <= 0:
            return ExposureDecision(True, "price_not_applicable", item_id, market_hash_name, unit_price, proposed_quantity, rule=rule)

        try:
            intervals = self.intervals()
        except ValueError as exc:
            allowed = not fail_closed
            return ExposureDecision(
                allowed,
                LOW_PRICE_EXPOSURE_RULE_INVALID if fail_closed else "rule_invalid_ignored",
                item_id,
                market_hash_name,
                unit_price,
                proposed_quantity,
                rule=rule,
                message=str(exc),
            )

        matched = None
        for index, interval in enumerate(intervals):
            if interval.contains(unit_price, is_last=index == len(intervals) - 1):
                matched = interval
                break
        if matched is None:
            return ExposureDecision(True, "price_not_in_guard_range", item_id, market_hash_name, unit_price, proposed_quantity, rule=rule)

        breakdown = self.exposure_breakdown(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            exclude_action_id=exclude_action_id,
            use_cache=use_cache,
        )
        current = breakdown.total
        projected = current + proposed_quantity
        if proposed_quantity <= 0:
            blocked = current >= matched.max_quantity
        else:
            blocked = projected > matched.max_quantity
        return ExposureDecision(
            allowed=not blocked,
            reason=LOW_PRICE_EXPOSURE_REASON if blocked else "",
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=unit_price,
            proposed_quantity=proposed_quantity,
            current_quantity=current,
            projected_quantity=projected,
            max_quantity=matched.max_quantity,
            interval=matched,
            breakdown=breakdown,
            rule=rule,
            message=(
                f"low price exposure {current}+{proposed_quantity}>{matched.max_quantity}"
                if blocked and proposed_quantity > 0
                else f"low price exposure {current}>={matched.max_quantity}"
                if blocked
                else ""
            ),
        )

    def should_hide_signal(
        self,
        session,
        *,
        item_id: int,
        market_hash_name: str = "",
        unit_price: float,
    ) -> ExposureDecision:
        if not self.hide_signals:
            return ExposureDecision(True, "hide_disabled", int(item_id or 0), market_hash_name, _to_float(unit_price, 0.0), 0)
        return self.check(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=unit_price,
            proposed_quantity=0,
            fail_closed=False,
            use_cache=True,
        )

    def exposure_breakdown(
        self,
        session,
        *,
        item_id: int,
        market_hash_name: str = "",
        exclude_action_id: int | None = None,
        use_cache: bool = False,
    ) -> ExposureBreakdown:
        cache_key = self._cache_key(session, item_id, market_hash_name, exclude_action_id)
        ttl = max(0, _to_int(self.config.get("cache_ttl_seconds"), 0))
        now = time.monotonic()
        if use_cache and ttl > 0:
            cached = _EXPOSURE_CACHE.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]
        purchases = pending_receipt = active_orders = inventory = 0
        if self.config.get("include_purchases"):
            purchases, pending_receipt = self._purchase_exposure(session, item_id, market_hash_name)
        if self.config.get("include_active_orders"):
            active_orders = self._active_order_exposure(session, item_id, exclude_action_id=exclude_action_id)
        if self.config.get("include_inventory"):
            inventory = self._inventory_exposure(item_id, market_hash_name)
        breakdown = ExposureBreakdown(
            purchases=purchases,
            pending_receipt=pending_receipt,
            active_orders=active_orders,
            inventory=inventory,
        )
        if use_cache and ttl > 0:
            _EXPOSURE_CACHE[cache_key] = (now + ttl, breakdown)
        return breakdown

    def _cache_key(self, session, item_id: int, market_hash_name: str, exclude_action_id: int | None) -> tuple[Any, ...]:
        try:
            bind_key = str(session.get_bind().url)
        except Exception:
            bind_key = "unknown"
        return (
            bind_key,
            int(item_id or 0),
            str(market_hash_name or ""),
            int(exclude_action_id or 0),
            bool(self.config.get("include_purchases")),
            bool(self.config.get("include_pending_receipt")),
            bool(self.config.get("include_active_orders")),
            bool(self.config.get("include_inventory")),
        )

    def _purchase_exposure(self, session, item_id: int, market_hash_name: str) -> tuple[int, int]:
        try:
            rows = session.execute(
                select(Purchase).where(
                    (Purchase.goods_id == int(item_id or 0)) | (Purchase.name == str(market_hash_name or ""))
                )
            ).scalars().all()
        except Exception:
            return 0, 0
        purchases = 0
        pending_receipt = 0
        include_pending = bool(self.config.get("include_pending_receipt"))
        for row in rows:
            if row.sold_at is not None:
                continue
            if bool(row.pending_receipt):
                if include_pending:
                    pending_receipt += 1
                continue
            purchases += 1
        return purchases, pending_receipt

    def _active_order_exposure(self, session, item_id: int, *, exclude_action_id: int | None = None) -> int:
        stmt = (
            select(PlatformAction)
            .where(PlatformAction.item_id == int(item_id or 0))
            .where(PlatformAction.action_type.in_(list(BUY_ACTION_TYPES)))
            .where(PlatformAction.state.in_(list(ACTIVE_BUY_STATES)))
            .where(PlatformAction.archived_at.is_(None))
        )
        if exclude_action_id is not None:
            stmt = stmt.where(PlatformAction.id != int(exclude_action_id))
        try:
            rows = session.execute(stmt).scalars().all()
        except Exception:
            return 0
        total = 0
        for row in rows:
            if row.remaining_quantity is not None:
                qty = max(0, int(row.remaining_quantity or 0))
            else:
                qty = max(0, int(row.quantity or 0) - int(row.filled_quantity or 0))
            if qty <= 0:
                context = _loads_dict(row.raw_context)
                if context.get("target_remaining_quantity") is not None:
                    qty = max(0, _to_int(context.get("target_remaining_quantity"), 0))
            total += qty
        return total

    def _inventory_exposure(self, item_id: int, market_hash_name: str) -> int:
        try:
            if self._inventory_cache is None:
                from app.state import get_inventory

                self._inventory_cache = [row for row in get_inventory() if isinstance(row, dict)]
            return sum(
                _record_quantity(row)
                for row in self._inventory_cache
                if _matches_inventory_record(row, item_id=item_id, market_hash_name=market_hash_name)
            )
        except Exception:
            return 0
