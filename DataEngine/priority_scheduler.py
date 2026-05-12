from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any

from config import load_app_config
from DataEngine.database import ItemBase, MarketPrice, SessionLocal, SteamDTOpportunity
from DataEngine.profit_model import best_profit_signal_from_prices, get_after_tax_price, safe_float, steam_balance_cost_ratio

logger = logging.getLogger(__name__)

PRIORITY_PAUSED = 0
PRIORITY_LOW_FREQ = 1
PRIORITY_STEAMDT_CANDIDATE = 2
PRIORITY_HIGH_FREQ = 3
PRIORITY_JIT = 4

DEFAULT_SCHEDULER_CONFIG = {
    "enabled": True,
    "min_volume_24h": 10,
    "min_liquidity_score": 0.6,
    "min_net_profit_rate": 0.03,
    "p1_to_p2_score": 25,
    "p2_to_p3_score": 50,
    "p2_to_p1_score": 12,
    "p3_to_p2_score": 18,
    "p3_to_p2_no_profit_rounds": 3,
    "p2_to_p1_no_hit_rounds": 3,
    "p2_to_p3_hit_rounds": 2,
    "max_high_priority_items": 100,
    "steamdt_fresh_minutes": 60,
    "jit_ttl_minutes": 10,
    "respect_manual_watch": True,
    "manual_watch_min_priority": 3,
    "respect_ttl": True,
    "respect_cooldown": True,
    "cashout_excellent_balance_cost_ratio": 0.55,
    "cashout_good_balance_cost_ratio": 0.63,
    "cashout_pass_balance_cost_ratio": 0.70,
    "cashout_excellent_score_floor": 75,
    "cashout_good_score_floor": 60,
    "cashout_pass_score_floor": 50,
}


@dataclass(frozen=True)
class PriorityDecision:
    item_id: int
    priority: int
    score: float
    reason: str
    source: str = "priority_scheduler"
    ttl_until: datetime | None = None
    up_hits: int = 0
    down_hits: int = 0


def scheduler_config(app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_SCHEDULER_CONFIG)
    if isinstance(app_config, dict):
        raw = app_config.get("priority_scheduler")
        if isinstance(raw, dict):
            cfg.update(raw)
        elif any(key in app_config for key in DEFAULT_SCHEDULER_CONFIG):
            cfg.update({key: value for key, value in app_config.items() if key in DEFAULT_SCHEDULER_CONFIG})
    if "promote_p2_score" in cfg:
        cfg["p1_to_p2_score"] = cfg["promote_p2_score"]
    if "promote_p3_score" in cfg:
        cfg["p2_to_p3_score"] = cfg["promote_p3_score"]
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


def _score_liquidity(volume: int) -> float:
    return min(100.0, math.log(max(0, volume) + 1, 2) * 12.0)


def _score_spread(profit_rate_pct: float) -> float:
    # SteamDT stores percent; MarketPrice calculations may pass ratio. Normalize lightly.
    rate = profit_rate_pct / 100.0 if profit_rate_pct > 1 else profit_rate_pct
    return max(0.0, min(100.0, rate * 1000.0))


def _cashout_tier_signal(
    prices: list[MarketPrice],
    cfg: dict[str, Any],
    app_config: dict[str, Any] | None = None,
) -> tuple[str | None, float, float]:
    steam_sell = 0.0
    cashout_revenue = 0.0
    for price in prices:
        platform = (getattr(price, "platform_name", "") or "").lower().strip()
        if platform == "steam":
            steam_sell = max(steam_sell, safe_float(getattr(price, "sell_min", None)))
            continue
        buy_max = safe_float(getattr(price, "buy_max", None))
        if buy_max <= 0:
            continue
        cashout_revenue = max(cashout_revenue, get_after_tax_price(buy_max, platform))
    if steam_sell <= 0 or cashout_revenue <= 0:
        return None, 0.0, 0.0

    active_ratio = steam_balance_cost_ratio(app_config)
    cost = steam_sell * active_ratio
    if cost <= 0 or cashout_revenue <= cost:
        return None, 0.0, 0.0

    excellent_ratio = float(cfg.get("cashout_excellent_balance_cost_ratio", 0.55) or 0.55)
    good_ratio = float(cfg.get("cashout_good_balance_cost_ratio", 0.63) or 0.63)
    pass_ratio = float(cfg.get("cashout_pass_balance_cost_ratio", 0.70) or 0.70)
    spread = (cashout_revenue - cost) / cost
    if active_ratio <= excellent_ratio:
        return "cashout_55_discount_profit", float(cfg.get("cashout_excellent_score_floor", 75) or 75), spread
    if active_ratio <= good_ratio:
        return "cashout_63_discount_profit", float(cfg.get("cashout_good_score_floor", 60) or 60), spread
    if active_ratio <= pass_ratio:
        return "cashout_70_discount_profit", float(cfg.get("cashout_pass_score_floor", 50) or 50), spread
    return "cashout_over_70_discount_profit", float(cfg.get("cashout_pass_score_floor", 50) or 50), spread


def _cashout_tier_priority(tier_label: str | None) -> int | None:
    if tier_label == "cashout_55_discount_profit":
        return PRIORITY_HIGH_FREQ
    if tier_label == "cashout_63_discount_profit":
        return PRIORITY_STEAMDT_CANDIDATE
    if tier_label in {"cashout_70_discount_profit", "cashout_over_70_discount_profit"}:
        return PRIORITY_LOW_FREQ
    return None


def _latest_price_signal(prices: list[MarketPrice], config: dict[str, Any] | None = None, scheduler_cfg: dict[str, Any] | None = None) -> tuple[int, float, float, bool, str | None]:
    volume = 0
    liquidity_score = 0.0
    has_steam = False
    has_cash_quote = False
    for price in prices:
        platform = (getattr(price, "platform_name", "") or "").lower().strip()
        source = (getattr(price, "data_source", "") or "").lower()
        if source != "steamdt_openapi":
            volume = max(volume, _safe_int(price.volume))
        liquidity_score = max(liquidity_score, _safe_float(getattr(price, "liquidity_score", None)))
        if platform == "steam" and _safe_float(getattr(price, "sell_min", None)) > 0:
            has_steam = True
        elif platform and platform != "steam" and (
            _safe_float(getattr(price, "sell_min", None)) > 0 or _safe_float(getattr(price, "buy_max", None)) > 0
        ):
            has_cash_quote = True
    signal = best_profit_signal_from_prices(prices, config)
    tier_label, tier_score_floor, tier_spread = _cashout_tier_signal(prices, scheduler_cfg or DEFAULT_SCHEDULER_CONFIG, config)
    spread = max(signal.best_rate, tier_spread) if tier_label else signal.best_rate
    return volume, spread, liquidity_score, bool(has_steam and has_cash_quote), tier_label


def _steamdt_signal(opps: list[SteamDTOpportunity], fresh_minutes: int) -> tuple[int, float, int]:
    cutoff = datetime.now() - timedelta(minutes=fresh_minutes)
    fresh = [opp for opp in opps if opp.steamdt_updated_at and opp.steamdt_updated_at >= cutoff]
    if not fresh:
        return 0, 0.0, 0
    volume = max(_safe_int(opp.transaction_count_24h) for opp in fresh)
    spread = max(_safe_float(opp.profit_rate) / 100.0 for opp in fresh)
    return len(fresh), spread, volume


def compute_priority_decision(
    item: ItemBase,
    prices: list[MarketPrice],
    steamdt_opps: list[SteamDTOpportunity],
    *,
    config: dict[str, Any] | None = None,
) -> PriorityDecision:
    cfg = scheduler_config(config)
    now = datetime.now()
    current_priority = int(getattr(item, "crawl_priority", PRIORITY_LOW_FREQ) or PRIORITY_LOW_FREQ)
    manual_watch = bool(getattr(item, "manual_watch", False))
    ttl_until = getattr(item, "priority_ttl_until", None)
    cooldown_until = getattr(item, "priority_cooldown_until", None)
    if cfg.get("respect_cooldown", True) and cooldown_until and cooldown_until > now:
        return PriorityDecision(
            item_id=item.id,
            priority=PRIORITY_PAUSED,
            score=0.0,
            reason=f"cooldown_until={cooldown_until.isoformat()}",
            up_hits=0,
            down_hits=0,
        )
    if cfg.get("respect_ttl", True) and ttl_until and ttl_until > now and current_priority >= PRIORITY_HIGH_FREQ:
        return PriorityDecision(
            item_id=item.id,
            priority=current_priority,
            score=float(getattr(item, "priority_score", 0.0) or 0.0),
            reason=f"ttl_hold_until={ttl_until.isoformat()}",
            ttl_until=ttl_until,
            up_hits=int(getattr(item, "priority_up_hits", 0) or 0),
            down_hits=0,
        )

    fresh_minutes = int(cfg["steamdt_fresh_minutes"])
    market_volume, market_spread, market_liquidity_score, has_market_signal, cashout_tier = _latest_price_signal(prices, config, cfg)
    hit_count, steamdt_spread, steamdt_volume = _steamdt_signal(steamdt_opps, fresh_minutes)
    volume = max(market_volume, steamdt_volume)
    spread = market_spread if has_market_signal else steamdt_spread

    liquidity = _score_liquidity(volume)
    if market_liquidity_score > 0:
        liquidity = max(liquidity, min(100.0, market_liquidity_score * 12.0))
    spread_score = _score_spread(spread)
    hit_score = min(100.0, hit_count * 25.0)
    score = round((0.45 * liquidity) + (0.35 * spread_score) + (0.20 * hit_score), 2)
    if cashout_tier:
        _, score_floor, _ = _cashout_tier_signal(prices, cfg, config)
        score = max(score, score_floor)

    min_volume = int(cfg["min_volume_24h"])
    min_liquidity_score = float(cfg.get("min_liquidity_score", 1.0) or 1.0)
    min_profit_rate = float(cfg["min_net_profit_rate"])
    has_volume = volume >= min_volume or market_liquidity_score >= min_liquidity_score
    has_spread = spread >= min_profit_rate
    p1_to_p2 = float(cfg["p1_to_p2_score"])
    p2_to_p3 = float(cfg["p2_to_p3_score"])
    p2_to_p1 = float(cfg["p2_to_p1_score"])
    p3_to_p2 = float(cfg["p3_to_p2_score"])
    up_hits = int(getattr(item, "priority_up_hits", 0) or 0)
    down_hits = int(getattr(item, "priority_down_hits", 0) or 0)
    priority = max(PRIORITY_LOW_FREQ, min(PRIORITY_JIT, current_priority))
    cashout_reason = f" cashout_tier={cashout_tier}" if cashout_tier else ""
    reason = f"hold score={score} volume={volume} liquidity={market_liquidity_score:.4f} spread={spread:.4f} hits={hit_count}{cashout_reason}"

    if priority == PRIORITY_JIT:
        priority = PRIORITY_HIGH_FREQ
        reason = "jit_ttl_expired_return_high"
        up_hits = 0
        down_hits = 0
    elif not has_volume:
        down_hits += 1
        up_hits = 0
        if priority > PRIORITY_LOW_FREQ and down_hits >= int(cfg["p2_to_p1_no_hit_rounds"]):
            priority = PRIORITY_LOW_FREQ
            reason = f"demote_low_liquidity volume={volume} min_volume={min_volume} liquidity={market_liquidity_score:.4f} min_liquidity={min_liquidity_score} down_hits={down_hits}"
        else:
            reason = f"hold_low_liquidity volume={volume} min_volume={min_volume} liquidity={market_liquidity_score:.4f} min_liquidity={min_liquidity_score} down_hits={down_hits}"
    elif priority <= PRIORITY_LOW_FREQ:
        if has_spread or score >= p1_to_p2 or hit_count > 0:
            priority = PRIORITY_STEAMDT_CANDIDATE
            up_hits = 1
            down_hits = 0
            reason = f"p1_to_p2 score={score} volume={volume} liquidity={market_liquidity_score:.4f} spread={spread:.4f} hits={hit_count}{cashout_reason}"
    elif priority == PRIORITY_STEAMDT_CANDIDATE:
        if hit_count > 0 and score >= p2_to_p3 and has_spread:
            up_hits += 1
            down_hits = 0
            if up_hits >= int(cfg["p2_to_p3_hit_rounds"]):
                priority = PRIORITY_HIGH_FREQ
                reason = f"p2_to_p3 score={score} hits={up_hits} spread={spread:.4f}{cashout_reason}"
            else:
                reason = f"p2_pending_p3 score={score} hits={up_hits} spread={spread:.4f}{cashout_reason}"
        elif hit_count <= 0 and score <= p2_to_p1:
            down_hits += 1
            up_hits = 0
            if down_hits >= int(cfg["p2_to_p1_no_hit_rounds"]):
                priority = PRIORITY_LOW_FREQ
                reason = f"p2_to_p1 score={score} down_hits={down_hits}"
            else:
                reason = f"p2_pending_p1 score={score} down_hits={down_hits}"
        else:
            up_hits = 0
            down_hits = 0
    elif priority >= PRIORITY_HIGH_FREQ:
        if score <= p3_to_p2 or not has_spread:
            down_hits += 1
            up_hits = 0
            if down_hits >= int(cfg["p3_to_p2_no_profit_rounds"]):
                priority = PRIORITY_STEAMDT_CANDIDATE
                reason = f"p3_to_p2 score={score} spread={spread:.4f} down_hits={down_hits}"
            else:
                reason = f"p3_pending_p2 score={score} spread={spread:.4f} down_hits={down_hits}"
        else:
            down_hits = 0
            up_hits = min(up_hits + 1, int(cfg["p2_to_p3_hit_rounds"]))
            reason = f"p3_hold score={score} volume={volume} liquidity={market_liquidity_score:.4f} spread={spread:.4f} hits={hit_count}{cashout_reason}"

    cashout_target_priority = _cashout_tier_priority(cashout_tier)
    if cashout_target_priority is not None and priority != cashout_target_priority:
        previous_priority = priority
        priority = cashout_target_priority
        if priority > previous_priority:
            up_hits = max(1, up_hits)
            down_hits = 0
        elif priority < previous_priority:
            up_hits = 0
            down_hits = max(1, down_hits)
        reason = f"{cashout_tier}_target_priority={priority} score={score} volume={volume} liquidity={market_liquidity_score:.4f} spread={spread:.4f}"

    if cfg.get("respect_manual_watch", True) and manual_watch:
        min_manual = int(cfg["manual_watch_min_priority"])
        if priority < min_manual:
            priority = min_manual
            reason = f"manual_watch_floor {reason}"
            down_hits = 0

    if priority == PRIORITY_HIGH_FREQ and current_priority != PRIORITY_HIGH_FREQ:
        ttl_minutes = int(cfg.get("jit_ttl_minutes", 10) or 10)
        ttl_until = now + timedelta(minutes=ttl_minutes)
    else:
        ttl_until = ttl_until if priority == current_priority else None

    return PriorityDecision(item_id=item.id, priority=priority, score=score, reason=reason, ttl_until=ttl_until, up_hits=up_hits, down_hits=down_hits)


def apply_priority_decision(db, decision: PriorityDecision) -> bool:
    item = db.get(ItemBase, decision.item_id)
    if item is None:
        return False
    changed = (
        int(item.crawl_priority or 0) != decision.priority
        or round(float(item.priority_score or 0), 2) != round(float(decision.score or 0), 2)
        or (item.priority_reason or "") != decision.reason
        or int(item.priority_up_hits or 0) != int(decision.up_hits or 0)
        or int(item.priority_down_hits or 0) != int(decision.down_hits or 0)
    )
    item.crawl_priority = decision.priority
    item.priority_score = decision.score
    item.priority_reason = decision.reason
    item.priority_source = decision.source
    item.priority_updated_at = datetime.now()
    item.priority_ttl_until = decision.ttl_until
    item.priority_up_hits = int(decision.up_hits or 0)
    item.priority_down_hits = int(decision.down_hits or 0)
    db.add(item)
    return changed


def recalculate_priorities(config: dict[str, Any] | None = None, *, item_ids: set[int] | None = None) -> int:
    app_config = config if isinstance(config, dict) else load_app_config()
    cfg = scheduler_config(app_config)
    if not cfg.get("enabled", True):
        logger.info("priority scheduler disabled")
        return 0
    updated = 0
    changed_item_ids: set[int] = set()
    with SessionLocal() as db:
        item_query = db.query(ItemBase).filter(ItemBase.is_active.is_(True))
        if item_ids:
            item_query = item_query.filter(ItemBase.id.in_(list(item_ids)))
        items = item_query.all()
        ids = [item.id for item in items]
        prices_by_item: dict[int, list[MarketPrice]] = defaultdict(list)
        opps_by_item: dict[int, list[SteamDTOpportunity]] = defaultdict(list)
        if ids:
            for price in db.query(MarketPrice).filter(MarketPrice.item_id.in_(ids)).all():
                prices_by_item[price.item_id].append(price)
            for opp in db.query(SteamDTOpportunity).filter(SteamDTOpportunity.item_id.in_(ids)).all():
                opps_by_item[opp.item_id].append(opp)
        for item in items:
            prices = prices_by_item.get(item.id, [])
            opps = opps_by_item.get(item.id, [])
            decision = compute_priority_decision(item, prices, opps, config=app_config)
            if apply_priority_decision(db, decision):
                updated += 1
                changed_item_ids.add(int(item.id))
        db.commit()
    if changed_item_ids:
        try:
            from DataEngine.radar_snapshot import refresh_radar_snapshots

            refresh_radar_snapshots(list(changed_item_ids))
        except Exception as exc:
            logger.warning("radar snapshot priority sync failed | items=%s err=%s", len(changed_item_ids), exc)
    logger.info("priority scheduler completed | items=%s updated=%s", len(items), updated)
    return updated


def main() -> None:
    recalculate_priorities()


if __name__ == "__main__":
    main()
