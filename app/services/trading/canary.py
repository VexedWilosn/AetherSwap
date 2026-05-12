from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from app.database import PlatformAction
from .capabilities import (
    CAP_CANCEL,
    CAP_DELIVER_ORDER,
    CAP_DIRECT_BUY,
    CAP_ORDER_STATUS,
    CAP_PLATFORM_LISTING,
    CAP_PURCHASE_ORDER,
    CAP_REPRICE,
    CAP_STEAM_LISTING,
    CAP_TRADE_OFFER_ACCEPT,
    normalize_platform,
)
from .states import CLAIMABLE_STATES, TERMINAL_STATES, PlatformActionState, PlatformActionType


LIVE_CANARY_CHANNEL = "live_canary"
TEST_SIGNAL_KEYS = {
    "test_signal",
    "test_profit_boost",
    "canary_profit_boost",
    "fake_profit_rate",
    "forced_candidate_reason",
}

BUY_ACTION_TYPES = {
    PlatformActionType.DIRECT_BUY,
    PlatformActionType.PURCHASE_ORDER,
    PlatformActionType.STEAM_BUY_ORDER,
}


@dataclass(frozen=True)
class LiveCanaryConfig:
    enabled: bool = False
    kill_switch: bool = True
    require_channel: str = LIVE_CANARY_CHANNEL
    max_action_cny: float = 1.0
    max_daily_cny: float = 10.0
    allowed_platforms: tuple[str, ...] = ()
    allowed_action_types: tuple[str, ...] = ()
    allowed_item_ids: tuple[int, ...] = ()
    allowed_market_hash_names: tuple[str, ...] = ()
    require_recent_smoke_seconds: int = 900
    require_manual_run_once: bool = True
    allow_background_worker: bool = False


@dataclass(frozen=True)
class LiveCanaryDecision:
    allowed: bool
    reason: str = ""
    message: str = ""
    action_id: int | None = None


@dataclass(frozen=True)
class LiveCanaryRunInspection:
    checked_at: float
    limit: int
    decision: LiveCanaryDecision
    action: PlatformAction | None = None
    required_capability: str = ""
    smoke_recent: bool | None = None


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


def _to_float(value: Any, default: float, *, minimum: float = 0.0, maximum: float = 1_000_000.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _to_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 86_400) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _str_tuple(value: Any, *, normalize: bool = False) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        out.append(normalize_platform(text) if normalize else text.lower())
    return tuple(dict.fromkeys(out))


def _int_tuple(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for item in values:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            out.append(parsed)
    return tuple(dict.fromkeys(out))


def live_canary_config_from_app_config(config: dict[str, Any] | None) -> LiveCanaryConfig:
    raw = config if isinstance(config, dict) else {}
    section = raw.get("trading_live_canary") if isinstance(raw.get("trading_live_canary"), dict) else {}
    return LiveCanaryConfig(
        enabled=_to_bool(section.get("enabled"), False),
        kill_switch=_to_bool(section.get("kill_switch"), True),
        require_channel=str(section.get("require_channel") or LIVE_CANARY_CHANNEL).strip() or LIVE_CANARY_CHANNEL,
        max_action_cny=_to_float(section.get("max_action_cny"), 1.0, minimum=0.01),
        max_daily_cny=_to_float(section.get("max_daily_cny"), 10.0, minimum=0.01),
        allowed_platforms=_str_tuple(section.get("allowed_platforms"), normalize=True),
        allowed_action_types=_str_tuple(section.get("allowed_action_types")),
        allowed_item_ids=_int_tuple(section.get("allowed_item_ids")),
        allowed_market_hash_names=_str_tuple(section.get("allowed_market_hash_names")),
        require_recent_smoke_seconds=_to_int(section.get("require_recent_smoke_seconds"), 900),
        require_manual_run_once=_to_bool(section.get("require_manual_run_once"), True),
        allow_background_worker=_to_bool(section.get("allow_background_worker"), False),
    )


class LiveCanarySmokeRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seen: dict[tuple[str, str], float] = {}

    def record_results(self, results: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, now: float | None = None) -> None:
        ts = time.time() if now is None else float(now)
        with self._lock:
            for row in results or []:
                if not bool(row.get("ok")) or not bool(row.get("live_preflight")):
                    continue
                platform = normalize_platform(str(row.get("platform") or ""))
                if not platform:
                    continue
                for capability in row.get("ready_capabilities") or row.get("checked_capabilities") or []:
                    name = str(capability or "").strip()
                    if name:
                        self._seen[(platform, name)] = ts
                self._seen[(platform, "*")] = ts

    def has_recent(
        self,
        platform: str,
        capability: str,
        *,
        max_age_seconds: int,
        now: float | None = None,
    ) -> bool:
        if max_age_seconds <= 0:
            return True
        ts = time.time() if now is None else float(now)
        platform_key = normalize_platform(platform)
        capability_key = str(capability or "").strip()
        with self._lock:
            seen_at = self._seen.get((platform_key, capability_key)) or self._seen.get((platform_key, "*"))
        return bool(seen_at and ts - seen_at <= max_age_seconds)

    def snapshot(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = sorted(self._seen.items(), key=lambda item: (item[0][0], item[0][1]))
        return [
            {
                "platform": platform,
                "capability": capability,
                "seen_at": seen_at,
                "age_seconds": max(0.0, ts - seen_at),
            }
            for (platform, capability), seen_at in rows
        ]

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


def raw_context_with_test_signal(payload: dict[str, Any]) -> Any:
    raw = payload.get("raw_context")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {"raw_context": raw}
    if not isinstance(raw, dict):
        raw = {}

    signal: dict[str, Any] = {}
    existing_signal = raw.get("test_signal")
    if isinstance(existing_signal, dict):
        signal.update(existing_signal)
    for key in TEST_SIGNAL_KEYS:
        if key in payload:
            value = payload.get(key)
            if key == "test_signal" and isinstance(value, dict):
                signal.update(value)
            else:
                signal[key] = value
    if signal:
        raw["test_signal"] = signal
    return raw or payload.get("raw_context")


def required_capability_for_action(action: PlatformAction) -> str:
    action_type = str(action.action_type or "")
    if action.state == PlatformActionState.WAITING_PLATFORM:
        return CAP_ORDER_STATUS
    if action.state == PlatformActionState.WAITING_TRADE_OFFER:
        return CAP_TRADE_OFFER_ACCEPT
    if action_type == PlatformActionType.DIRECT_BUY:
        return CAP_DIRECT_BUY
    if action_type in {PlatformActionType.PURCHASE_ORDER, PlatformActionType.STEAM_BUY_ORDER}:
        return CAP_PURCHASE_ORDER if action.platform != "steam" else CAP_DIRECT_BUY
    if action_type == PlatformActionType.STEAM_LISTING:
        return CAP_STEAM_LISTING
    if action_type == PlatformActionType.PLATFORM_LISTING:
        return CAP_PLATFORM_LISTING
    if action_type == PlatformActionType.REPRICE_LISTING:
        return CAP_REPRICE
    if action_type == PlatformActionType.CANCEL_ORDER:
        return CAP_CANCEL
    if action_type == PlatformActionType.DELIVER_ORDER:
        return CAP_DELIVER_ORDER
    if action_type == PlatformActionType.ACCEPT_TRADE_OFFER:
        return CAP_TRADE_OFFER_ACCEPT
    if action_type == PlatformActionType.POLL_ORDER:
        return CAP_ORDER_STATUS
    return action_type


def _buy_budget(action: PlatformAction) -> float:
    if str(action.action_type or "") not in BUY_ACTION_TYPES:
        return 0.0
    if float(action.locked_budget_cny or 0) > 0:
        return round(float(action.locked_budget_cny or 0), 2)
    return round(float(action.target_price or 0) * max(1, int(action.quantity or 1)), 2)


def _platform_daily_canary_budget(session, config: LiveCanaryConfig, *, now: float) -> float:
    local = time.localtime(now)
    try:
        start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, local.tm_wday, local.tm_yday, local.tm_isdst))
    except (OverflowError, OSError, ValueError):
        start = max(0.0, now - 86400)
    rows = (
        session.execute(
            select(PlatformAction.locked_budget_cny, PlatformAction.filled_amount_cny)
            .where(PlatformAction.channel == config.require_channel)
            .where(PlatformAction.created_at >= start)
            .where(PlatformAction.state.notin_([PlatformActionState.CANCELLED, PlatformActionState.FAILED, PlatformActionState.EXPIRED]))
        )
        .all()
    )
    return sum(float(locked or 0) + float(filled or 0) for locked, filled in rows)


def _first_due_action(session, *, now: float, limit: int) -> PlatformAction | None:
    claimable = list(CLAIMABLE_STATES - TERMINAL_STATES)
    return (
        session.execute(
            select(PlatformAction)
            .where(PlatformAction.state.in_(claimable))
            .where(PlatformAction.archived_at.is_(None))
            .where(PlatformAction.next_check_at <= now)
            .where((PlatformAction.lease_until.is_(None)) | (PlatformAction.lease_until <= now))
            .order_by(PlatformAction.next_check_at.asc(), PlatformAction.id.asc())
            .limit(max(1, int(limit or 1)))
        )
        .scalars()
        .first()
    )


def validate_live_canary_action(
    session,
    action: PlatformAction,
    *,
    config: LiveCanaryConfig,
    smoke_registry: LiveCanarySmokeRegistry | None = None,
    now: float | None = None,
) -> LiveCanaryDecision:
    ts = time.time() if now is None else float(now)
    action_id = int(action.id or 0) or None
    if not config.enabled:
        return LiveCanaryDecision(False, "live_canary_disabled", "Enable trading_live_canary before any live action.", action_id)
    if config.kill_switch:
        return LiveCanaryDecision(False, "live_canary_kill_switch_enabled", "Disable the live canary kill switch for this single test.", action_id)
    if str(action.channel or "") != config.require_channel:
        return LiveCanaryDecision(False, "live_canary_channel_required", f"Live action must use channel={config.require_channel}.", action_id)

    platform = normalize_platform(str(action.platform or ""))
    if not config.allowed_platforms or platform not in config.allowed_platforms:
        return LiveCanaryDecision(False, "live_canary_platform_not_allowed", f"Platform {platform} is not in the canary allowlist.", action_id)
    action_type = str(action.action_type or "").lower().strip()
    if not config.allowed_action_types or action_type not in config.allowed_action_types:
        return LiveCanaryDecision(False, "live_canary_action_not_allowed", f"Action {action_type} is not in the canary allowlist.", action_id)

    name = str(action.market_hash_name or "").lower().strip()
    item_allowed = bool(config.allowed_item_ids and int(action.item_id or 0) in config.allowed_item_ids)
    name_allowed = bool(config.allowed_market_hash_names and name in config.allowed_market_hash_names)
    if not item_allowed and not name_allowed:
        return LiveCanaryDecision(False, "live_canary_item_not_allowed", "Item is not in the canary allowlist.", action_id)

    budget = _buy_budget(action)
    if budget > config.max_action_cny:
        return LiveCanaryDecision(False, "live_canary_action_cap_exceeded", "Action budget exceeds live canary max_action_cny.", action_id)
    daily = _platform_daily_canary_budget(session, config, now=ts)
    if daily > config.max_daily_cny:
        return LiveCanaryDecision(False, "live_canary_daily_cap_exceeded", "Daily live canary budget exceeds max_daily_cny.", action_id)

    capability = required_capability_for_action(action)
    if smoke_registry and not smoke_registry.has_recent(
        platform,
        capability,
        max_age_seconds=config.require_recent_smoke_seconds,
        now=ts,
    ):
        return LiveCanaryDecision(False, "live_canary_smoke_required", f"Recent live smoke is required for {platform}:{capability}.", action_id)
    return LiveCanaryDecision(True, action_id=action_id)


def validate_live_canary_run(
    session,
    app_config: dict[str, Any] | None,
    *,
    limit: int,
    smoke_registry: LiveCanarySmokeRegistry | None = None,
    now: float | None = None,
) -> LiveCanaryDecision:
    return inspect_live_canary_run(
        session,
        app_config,
        limit=limit,
        smoke_registry=smoke_registry,
        now=now,
    ).decision


def inspect_live_canary_run(
    session,
    app_config: dict[str, Any] | None,
    *,
    limit: int,
    smoke_registry: LiveCanarySmokeRegistry | None = None,
    now: float | None = None,
) -> LiveCanaryRunInspection:
    ts = time.time() if now is None else float(now)
    try:
        limit_value = max(1, int(limit or 1))
    except (TypeError, ValueError):
        limit_value = 1
    config = live_canary_config_from_app_config(app_config)
    action = _first_due_action(session, now=ts, limit=limit_value)
    capability = required_capability_for_action(action) if action is not None else ""
    smoke_recent: bool | None = None
    if action is not None and smoke_registry is not None:
        smoke_recent = smoke_registry.has_recent(
            action.platform,
            capability,
            max_age_seconds=config.require_recent_smoke_seconds,
            now=ts,
        )
    if config.require_manual_run_once and limit_value != 1:
        decision = LiveCanaryDecision(
            False,
            "live_canary_limit_must_be_one",
            "First live canary run must use limit=1.",
            int(action.id or 0) or None if action is not None else None,
        )
        return LiveCanaryRunInspection(ts, limit_value, decision, action, capability, smoke_recent)
    if action is None:
        return LiveCanaryRunInspection(ts, limit_value, LiveCanaryDecision(True), None, "", None)
    decision = validate_live_canary_action(session, action, config=config, smoke_registry=smoke_registry, now=ts)
    return LiveCanaryRunInspection(ts, limit_value, decision, action, capability, smoke_recent)
