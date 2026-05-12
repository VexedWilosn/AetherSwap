from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from DataEngine.database import ActionDecision, ArbitrageOpportunity, ItemBase, MarketPrice, SessionLocal
from DataEngine.profit_model import opportunity_profit, steam_balance_cost_ratio

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"

ACTION_OBSERVE_ONLY = "observe_only"
ACTION_CREATE_BUY_ORDER = "create_buy_order"
ACTION_DIRECT_BUY = "direct_buy"
ACTION_LIST_SELL = "list_sell"
ACTION_ADJUST_BUY_ORDER = "adjust_buy_order"
ACTION_CANCEL_BUY_ORDER = "cancel_buy_order"
ACTION_BLOCKED = "blocked"

DEFAULT_ACTION_POLICY_CONFIG: dict[str, Any] = {
    "enabled": True,
    "decision_ttl_minutes": 15,
    "direct_buy_min_profit_rate": 0.08,
    "buy_order_min_profit_rate": 0.12,
    "sell_min_profit_rate": 0.03,
    "min_24h_volume": 20,
    "direct_buy_requires_jit": True,
    "sell_requires_jit": True,
    "allow_direct_buy": True,
    "allow_buy_order": True,
    "allow_auto_sell": False,
    "direct_buy_score_weight": 1.0,
    "buy_order_score_weight": 1.15,
    "jit_cost_penalty": 0.015,
    "capital_usage_penalty": 0.02,
    "buy_order_wait_penalty": 0.01,
    "decision_reopen_price_change_rate": 0.02,
    "risk_segments": [
        {"min_price": 0, "max_price": 10, "max_capital_per_item": 80, "max_inventory_per_item": 8},
        {"min_price": 10, "max_price": 100, "max_capital_per_item": 300, "max_inventory_per_item": 3},
        {"min_price": 100, "max_price": None, "max_capital_per_item": 800, "max_inventory_per_item": 1},
    ],
}


@dataclass(frozen=True)
class HoldingState:
    quantity: int = 0
    capital_cny: float = 0.0


@dataclass(frozen=True)
class ActionCandidate:
    action: str
    target_price: float
    expected_profit_cny: float
    expected_profit_rate: float
    requires_jit: bool
    score: float
    reason: str


@dataclass(frozen=True)
class RiskSegment:
    min_price: float = 0.0
    max_price: float | None = None
    max_capital_per_item: float = 0.0
    max_inventory_per_item: int = 0


@dataclass(frozen=True)
class ActionPolicyDecision:
    opportunity_id: int | None
    item_id: int
    action: str
    target_platform: str
    sell_platform: str | None
    target_price: float
    reference_price: float | None
    quantity: int
    score: float
    expected_profit_cny: float
    expected_profit_rate: float
    requires_jit: bool
    status: str
    reason: str
    risk_flags: str = ""
    expires_at: datetime | None = None


def load_app_config() -> dict[str, Any]:
    try:
        if APP_CONFIG_PATH.exists():
            return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8") or "{}") or {}
    except Exception as exc:
        logger.warning("failed to load app config for action policy | err=%s", exc)
    return {}


def action_policy_config(app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_ACTION_POLICY_CONFIG)
    if isinstance(app_config, dict):
        raw = app_config.get("action_policy")
        if isinstance(raw, dict):
            cfg.update(raw)
        else:
            known_keys = set(DEFAULT_ACTION_POLICY_CONFIG)
            if any(key in app_config for key in known_keys):
                cfg.update({key: value for key, value in app_config.items() if key in known_keys})
    return cfg


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_risk_segments(config: dict[str, Any]) -> list[RiskSegment]:
    raw_segments = config.get("risk_segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raw_segments = DEFAULT_ACTION_POLICY_CONFIG["risk_segments"]
    segments: list[RiskSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        max_price_raw = raw.get("max_price")
        max_price = None if max_price_raw in (None, "", "null") else max(0.0, _safe_float(max_price_raw))
        segments.append(
            RiskSegment(
                min_price=max(0.0, _safe_float(raw.get("min_price"))),
                max_price=max_price,
                max_capital_per_item=max(0.0, _safe_float(raw.get("max_capital_per_item"))),
                max_inventory_per_item=max(0, _safe_int(raw.get("max_inventory_per_item"))),
            )
        )
    return sorted(segments, key=lambda s: (s.min_price, float("inf") if s.max_price is None else s.max_price))


def select_risk_segment(price: float, config: dict[str, Any]) -> RiskSegment:
    segments = normalize_risk_segments(config)
    for segment in segments:
        if price >= segment.min_price and (segment.max_price is None or price < segment.max_price):
            return segment
    return segments[-1]


def _load_prices_by_item(db: Session, item_ids: list[int]) -> dict[int, dict[str, MarketPrice]]:
    if not item_ids:
        return {}
    rows = db.query(MarketPrice).filter(MarketPrice.item_id.in_(item_ids)).all()
    grouped: dict[int, dict[str, MarketPrice]] = {}
    for row in rows:
        grouped.setdefault(int(row.item_id), {})[(row.platform_name or "").lower().strip()] = row
    return grouped


def _matches_item_record(record: dict[str, Any], *, item_id: int, market_hash_name: str) -> bool:
    raw_item_id = record.get("item_id")
    if raw_item_id is not None and _safe_int(raw_item_id) == int(item_id):
        return True
    raw_name = str(record.get("market_hash_name") or record.get("name") or record.get("item_name") or "").strip()
    return bool(raw_name and raw_name == market_hash_name)


def _record_quantity(record: dict[str, Any]) -> int:
    return max(1, _safe_int(record.get("quantity") or record.get("num") or record.get("count") or 1))


def _record_price(record: dict[str, Any]) -> float:
    return _safe_float(
        record.get("price")
        or record.get("target_price")
        or record.get("buy_price")
        or record.get("my_price")
        or record.get("market_price")
    )


def _estimate_local_purchase_exposure(item_id: int, market_hash_name: str) -> HoldingState:
    try:
        from app.state import get_purchases

        quantity = 0
        capital = 0.0
        for purchase in get_purchases():
            if not isinstance(purchase, dict) or not _matches_item_record(purchase, item_id=item_id, market_hash_name=market_hash_name):
                continue
            if purchase.get("sold_at"):
                continue
            qty = _record_quantity(purchase)
            quantity += qty
            capital += _record_price(purchase) * qty
        return HoldingState(quantity=quantity, capital_cny=capital)
    except Exception as exc:
        logger.debug("purchase exposure unavailable | item_id=%s item=%s err=%s", item_id, market_hash_name, exc)
        return HoldingState()


def _estimate_inventory_exposure(item_id: int, market_hash_name: str) -> HoldingState:
    try:
        from app.state import get_inventory

        quantity = 0
        capital = 0.0
        for row in get_inventory():
            if not isinstance(row, dict) or not _matches_item_record(row, item_id=item_id, market_hash_name=market_hash_name):
                continue
            qty = _record_quantity(row)
            quantity += qty
            capital += _record_price(row) * qty
        return HoldingState(quantity=quantity, capital_cny=capital)
    except Exception as exc:
        logger.debug("inventory exposure unavailable | item_id=%s item=%s err=%s", item_id, market_hash_name, exc)
        return HoldingState()


def _estimate_active_order_exposure(item_id: int, market_hash_name: str) -> HoldingState:
    try:
        from app.state import get_plan

        quantity = 0
        capital = 0.0
        for row in get_plan():
            if not isinstance(row, dict) or not _matches_item_record(row, item_id=item_id, market_hash_name=market_hash_name):
                continue
            action = str(row.get("action") or row.get("type") or "").lower()
            status = str(row.get("status") or "open").lower()
            if action and "buy" not in action and "order" not in action:
                continue
            if status in {"cancelled", "canceled", "closed", "failed", "sold"}:
                continue
            qty = _record_quantity(row)
            quantity += qty
            capital += _record_price(row) * qty
        return HoldingState(quantity=quantity, capital_cny=capital)
    except Exception as exc:
        logger.debug("active order exposure unavailable | item_id=%s item=%s err=%s", item_id, market_hash_name, exc)
        return HoldingState()


def _estimate_decision_exposure(db: Session | None, item_id: int, exclude_opportunity_id: int | None = None) -> HoldingState:
    if db is None:
        return HoldingState()
    try:
        query = (
            db.query(ActionDecision)
            .filter(ActionDecision.item_id == int(item_id))
            .filter(ActionDecision.action.in_([ACTION_CREATE_BUY_ORDER, ACTION_DIRECT_BUY]))
            .filter(ActionDecision.status.in_(["open", "success"]))
        )
        if exclude_opportunity_id is not None:
            query = query.filter(
                (ActionDecision.opportunity_id.is_(None)) | (ActionDecision.opportunity_id != int(exclude_opportunity_id))
            )
        rows = query.all()
        quantity = 0
        capital = 0.0
        for row in rows:
            qty = max(1, int(row.quantity or 1))
            quantity += qty
            capital += _safe_float(row.target_price) * qty
        return HoldingState(quantity=quantity, capital_cny=capital)
    except Exception as exc:
        logger.debug("decision exposure unavailable | item_id=%s err=%s", item_id, exc)
        return HoldingState()


def _merge_holdings(*states: HoldingState) -> HoldingState:
    return HoldingState(
        quantity=sum(max(0, int(state.quantity or 0)) for state in states),
        capital_cny=sum(max(0.0, float(state.capital_cny or 0.0)) for state in states),
    )


def estimate_holding_state(
    item_id: int,
    market_hash_name: str,
    db: Session | None = None,
    *,
    exclude_opportunity_id: int | None = None,
) -> HoldingState:
    return _merge_holdings(
        _estimate_local_purchase_exposure(item_id, market_hash_name),
        _estimate_inventory_exposure(item_id, market_hash_name),
        _estimate_active_order_exposure(item_id, market_hash_name),
        _estimate_decision_exposure(db, item_id, exclude_opportunity_id=exclude_opportunity_id),
    )


def _volume_for_item(price_map: dict[str, MarketPrice]) -> int:
    return max(
        (
            _safe_int(getattr(price, "volume", 0))
            or _safe_int(getattr(price, "orderbook_depth", 0))
            or (_safe_int(getattr(price, "sell_volume", 0)) + _safe_int(getattr(price, "buy_volume", 0)))
            or int(max(0.0, _safe_float(getattr(price, "liquidity_score", 0))) * 20)
            for price in price_map.values()
        ),
        default=0,
    )


def _risk_adjusted_score(
    *,
    action: str,
    expected_profit: float,
    expected_rate: float,
    target_price: float,
    volume: int,
    requires_jit: bool,
    holding: HoldingState,
    segment: RiskSegment,
    config: dict[str, Any],
) -> float:
    action_weight_key = "direct_buy_score_weight" if action == ACTION_DIRECT_BUY else "buy_order_score_weight"
    action_weight = _safe_float(config.get(action_weight_key)) or 1.0
    volume_bonus = min(volume, 500) / 1000.0
    capital_limit = segment.max_capital_per_item if segment.max_capital_per_item > 0 else max(target_price, 1.0) * 10
    projected_capital_ratio = min(1.0, (holding.capital_cny + target_price) / max(capital_limit, 1.0))
    score = (expected_profit * 0.02) + (expected_rate * action_weight) + volume_bonus
    score -= projected_capital_ratio * (_safe_float(config.get("capital_usage_penalty")) or 0.0)
    if requires_jit:
        score -= _safe_float(config.get("jit_cost_penalty")) or 0.0
    if action == ACTION_CREATE_BUY_ORDER:
        score -= _safe_float(config.get("buy_order_wait_penalty")) or 0.0
    return round(score, 6)


def _decision_for_blocked(opportunity: ArbitrageOpportunity, reason: str, risk_flags: str = "") -> ActionPolicyDecision:
    return ActionPolicyDecision(
        opportunity_id=opportunity.id,
        item_id=opportunity.item_id,
        action=ACTION_BLOCKED,
        target_platform=opportunity.buy_platform,
        sell_platform=opportunity.sell_platform,
        target_price=float(opportunity.buy_price or 0),
        reference_price=float(opportunity.sell_price or 0),
        quantity=0,
        score=0.0,
        expected_profit_cny=0.0,
        expected_profit_rate=0.0,
        requires_jit=True,
        status="blocked",
        reason=reason,
        risk_flags=risk_flags,
    )


def compute_action_decision(
    opportunity: ArbitrageOpportunity,
    item: ItemBase,
    price_map: dict[str, MarketPrice],
    *,
    config: dict[str, Any] | None = None,
    holding_state: HoldingState | None = None,
    db: Session | None = None,
) -> ActionPolicyDecision:
    cfg = action_policy_config(config)
    if not cfg.get("enabled", True):
        return _decision_for_blocked(opportunity, "action_policy_disabled")

    buy_price = _safe_float(opportunity.buy_price)
    sell_price = _safe_float(opportunity.sell_price)
    buy_platform = (opportunity.buy_platform or "").lower().strip()
    sell_platform = (opportunity.sell_platform or "steam").lower().strip()
    if buy_price <= 0 or sell_price <= 0:
        return _decision_for_blocked(opportunity, "invalid_price")

    volume = _volume_for_item(price_map)
    min_volume = _safe_int(cfg.get("min_24h_volume"))
    if volume < min_volume:
        return _decision_for_blocked(opportunity, f"low_volume volume={volume} min={min_volume}", "low_volume")

    math = opportunity_profit(
        buy_platform=buy_platform,
        sell_platform=sell_platform,
        buy_price=buy_price,
        sell_price=sell_price,
        balance_cost_ratio=steam_balance_cost_ratio(config),
    )
    after_tax = math.revenue_cny
    expected_profit = math.profit_cny
    expected_rate = math.profit_rate
    if expected_profit <= 0:
        return _decision_for_blocked(opportunity, "unprofitable_after_tax", "negative_profit")

    holding = holding_state or estimate_holding_state(
        int(item.id),
        item.market_hash_name,
        db,
        exclude_opportunity_id=int(opportunity.id) if getattr(opportunity, "id", None) is not None else None,
    )
    segment = select_risk_segment(buy_price, cfg)
    projected_capital = holding.capital_cny + buy_price
    projected_quantity = holding.quantity + 1
    risk_flags: list[str] = []
    if segment.max_capital_per_item > 0 and projected_capital > segment.max_capital_per_item:
        risk_flags.append(f"capital_limit projected={projected_capital:.2f} max={segment.max_capital_per_item:.2f}")
    if segment.max_inventory_per_item > 0 and projected_quantity > segment.max_inventory_per_item:
        risk_flags.append(f"inventory_limit projected={projected_quantity} max={segment.max_inventory_per_item}")
    if risk_flags:
        return _decision_for_blocked(opportunity, "risk_segment_limit", "; ".join(risk_flags))

    ttl_minutes = max(1, _safe_int(cfg.get("decision_ttl_minutes")) or 15)
    expires_at = datetime.now() + timedelta(minutes=ttl_minutes)
    direct_min_rate = _safe_float(cfg.get("direct_buy_min_profit_rate"))
    order_min_rate = _safe_float(cfg.get("buy_order_min_profit_rate"))

    candidates: list[ActionCandidate] = []
    if cfg.get("allow_direct_buy", True) and expected_rate >= direct_min_rate:
        requires_jit = bool(cfg.get("direct_buy_requires_jit", True))
        score = _risk_adjusted_score(
            action=ACTION_DIRECT_BUY,
            expected_profit=expected_profit,
            expected_rate=expected_rate,
            target_price=buy_price,
            volume=volume,
            requires_jit=requires_jit,
            holding=holding,
            segment=segment,
            config=cfg,
        )
        candidates.append(
            ActionCandidate(
                action=ACTION_DIRECT_BUY,
                target_price=buy_price,
                expected_profit_cny=round(expected_profit, 4),
                expected_profit_rate=round(expected_rate, 6),
                requires_jit=requires_jit,
                score=score,
                reason=f"direct_buy score={score} rate={expected_rate:.4f} volume={volume}",
            )
        )

    if cfg.get("allow_buy_order", True) and expected_rate >= order_min_rate:
        order_price = round(min(buy_price, after_tax * (1.0 - order_min_rate)), 2)
        order_profit = after_tax - order_price
        order_rate = (order_profit / order_price) if order_price > 0 else 0.0
        score = _risk_adjusted_score(
            action=ACTION_CREATE_BUY_ORDER,
            expected_profit=order_profit,
            expected_rate=order_rate,
            target_price=order_price,
            volume=volume,
            requires_jit=False,
            holding=holding,
            segment=segment,
            config=cfg,
        )
        candidates.append(
            ActionCandidate(
                action=ACTION_CREATE_BUY_ORDER,
                target_price=order_price,
                expected_profit_cny=round(order_profit, 4),
                expected_profit_rate=round(order_rate, 6),
                requires_jit=False,
                score=score,
                reason=f"create_buy_order score={score} rate={order_rate:.4f} volume={volume}",
            )
        )

    if candidates:
        best = max(candidates, key=lambda c: (c.score, c.expected_profit_cny, c.expected_profit_rate))
        return ActionPolicyDecision(
            opportunity_id=opportunity.id,
            item_id=opportunity.item_id,
            action=best.action,
            target_platform=buy_platform,
            sell_platform=sell_platform,
            target_price=best.target_price,
            reference_price=sell_price,
            quantity=1,
            score=best.score,
            expected_profit_cny=best.expected_profit_cny,
            expected_profit_rate=best.expected_profit_rate,
            requires_jit=best.requires_jit,
            status="open",
            reason=best.reason,
            expires_at=expires_at,
        )

    return ActionPolicyDecision(
        opportunity_id=opportunity.id,
        item_id=opportunity.item_id,
        action=ACTION_OBSERVE_ONLY,
        target_platform=buy_platform,
        sell_platform=sell_platform,
        target_price=buy_price,
        reference_price=sell_price,
        quantity=0,
        score=round(expected_rate * 100.0, 2),
        expected_profit_cny=round(expected_profit, 4),
        expected_profit_rate=round(expected_rate, 6),
        requires_jit=False,
        status="open",
        reason=f"below_action_threshold rate={expected_rate:.4f}",
        expires_at=expires_at,
    )


def _decision_payload(decision: ActionPolicyDecision) -> dict[str, Any]:
    return {
        "opportunity_id": decision.opportunity_id,
        "item_id": decision.item_id,
        "action": decision.action,
        "target_platform": decision.target_platform,
        "sell_platform": decision.sell_platform,
        "target_price": decision.target_price,
        "reference_price": decision.reference_price,
        "quantity": decision.quantity,
        "score": decision.score,
        "expected_profit_cny": decision.expected_profit_cny,
        "expected_profit_rate": decision.expected_profit_rate,
        "requires_jit": decision.requires_jit,
        "status": decision.status,
        "reason": decision.reason,
        "risk_flags": decision.risk_flags,
        "updated_at": datetime.now(),
        "expires_at": decision.expires_at,
    }


def _terminal_decision_can_reopen(existing: ActionDecision, payload: dict[str, Any], config: dict[str, Any]) -> bool:
    if existing.status not in {"success", "failed"}:
        return True
    now = datetime.now()
    if existing.expires_at is not None and existing.expires_at < now:
        return True
    old_price = _safe_float(existing.target_price)
    new_price = _safe_float(payload.get("target_price"))
    if old_price > 0 and abs(new_price - old_price) / old_price >= (_safe_float(config.get("decision_reopen_price_change_rate")) or 0.02):
        return True
    return False


def _apply_decision_payload(existing: ActionDecision, payload: dict[str, Any], *, reopen: bool) -> None:
    existing.item_id = int(payload["item_id"])
    existing.sell_platform = payload.get("sell_platform")
    existing.target_price = float(payload.get("target_price") or 0)
    existing.reference_price = payload.get("reference_price")
    existing.quantity = int(payload.get("quantity") or 1)
    existing.score = float(payload.get("score") or 0)
    existing.expected_profit_cny = float(payload.get("expected_profit_cny") or 0)
    existing.expected_profit_rate = float(payload.get("expected_profit_rate") or 0)
    existing.requires_jit = bool(payload.get("requires_jit", True))
    existing.reason = payload.get("reason")
    existing.risk_flags = payload.get("risk_flags")
    existing.updated_at = payload.get("updated_at") or datetime.now()
    if reopen:
        existing.status = str(payload.get("status") or "open")
        existing.expires_at = payload.get("expires_at")


def save_action_decisions(
    db: Session,
    decisions: list[ActionPolicyDecision],
    config: dict[str, Any] | None = None,
) -> int:
    if not decisions:
        return 0
    cfg = action_policy_config(config or load_app_config())
    saved = 0
    for decision in decisions:
        payload = _decision_payload(decision)
        existing = (
            db.query(ActionDecision)
            .filter(ActionDecision.opportunity_id == decision.opportunity_id)
            .filter(ActionDecision.action == decision.action)
            .filter(ActionDecision.target_platform == decision.target_platform)
            .one_or_none()
        )
        if existing is None:
            terminal_peer = (
                db.query(ActionDecision)
                .filter(ActionDecision.opportunity_id == decision.opportunity_id)
                .filter(ActionDecision.target_platform == decision.target_platform)
                .filter(ActionDecision.status.in_(["success", "failed"]))
                .first()
            )
            if terminal_peer is not None and not _terminal_decision_can_reopen(terminal_peer, payload, cfg):
                continue
            db.add(ActionDecision(**payload))
            saved += 1
            continue
        reopen = _terminal_decision_can_reopen(existing, payload, cfg)
        _apply_decision_payload(existing, payload, reopen=reopen)
        db.add(existing)
        saved += 1
    db.commit()
    return saved


def generate_action_decisions(config: dict[str, Any] | None = None) -> int:
    cfg = action_policy_config(config or load_app_config())
    if not cfg.get("enabled", True):
        logger.info("action policy disabled")
        return 0
    with SessionLocal() as db:
        rows = (
            db.query(ArbitrageOpportunity, ItemBase)
            .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
            .filter(ArbitrageOpportunity.status == "open")
            .all()
        )
        item_ids = list({opp.item_id for opp, _ in rows})
        prices_by_item = _load_prices_by_item(db, item_ids)
        decisions = [
            compute_action_decision(
                opp,
                item,
                prices_by_item.get(opp.item_id, {}),
                config=cfg,
                db=db,
            )
            for opp, item in rows
        ]
        saved = save_action_decisions(db, decisions, config=cfg)
    logger.info("action policy completed | opportunities=%s decisions=%s", len(rows), saved)
    return saved


def main() -> None:
    generate_action_decisions()


if __name__ == "__main__":
    main()
