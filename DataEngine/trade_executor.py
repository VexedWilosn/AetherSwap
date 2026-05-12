"""Trade executor for verified arbitrage opportunities.

The executor reads open opportunities from market_data.db, resolves the target
platform item id from local mappings, optionally refreshes stale quotes, places
orders through the platform buyer classes, and records success/failure state.
"""

from __future__ import annotations

import json
import logging
import os
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import ActionDecision, ArbitrageOpportunity, ItemBase, MarketPrice, PlatformMapping, SessionLocal as DBSessionLocal
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.main_engine import refresh_items_prices, refresh_single_item_prices
from DataEngine.profit_model import opportunity_profit, steam_balance_cost_ratio
from DataEngine.uuyp_public_monitor import is_uuyp_auth_circuit_open, uuyp_auth_circuit_remaining_seconds
from app.services.notifier import notify_trade_success
from app.services.platform_sessions import PlatformClientFactory
from app.services.trading.exposure_guard import LowPriceExposureGuard

# =============================================================================
# Section
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
MARKET_DB_PATH = CONFIG_DIR / "market_data.db"
APP_CONFIG_PATH = CONFIG_DIR / "app_config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"

setup_dataengine_logging()

logger = logging.getLogger(__name__)


def _notify_trade_success_safe(opportunity: "OpportunityView", result: dict[str, Any]) -> None:
    try:
        notify_trade_success(
            item_name=opportunity.market_hash_name,
            action="auto_buy",
            price=float(opportunity.buy_price or 0),
            platform=opportunity.buy_platform,
            quantity=1,
            extra={"opportunity_id": opportunity.id, "result": result},
        )
    except Exception as exc:
        logger.warning("trade notifier failed | opportunity_id=%s err=%s", opportunity.id, exc)

# =============================================================================
# Section
# =============================================================================

DEFAULT_GAME = "csgo"
SUPPORTED_BUY_PLATFORMS = {"buff", "uuyp", "eco", "steam"}
ID_RESOLVE_CACHE: dict[tuple[str, str], str] = {}
JIT_REFRESHED_ITEMS: set[int] = set()
DEFAULT_JIT_BYPASS_MINUTES = 5
DEFAULT_PURCHASE_ORDER_JIT_BYPASS_MINUTES = 10
DEFAULT_STEAMDT_BYPASS_MINUTES = 5
DEFAULT_STEAMDT_MIN_VOLUME = 20
ORDER_ACTION_TOKENS = ("buy_order", "purchase_order", "create_buy_order")
DIRECT_BUY_ACTION_TOKENS = ("direct_buy", "direct_trade", "auto_buy", "auto_sell", "buy_listing", "instant_buy")

_engine = create_engine(
    f"sqlite:///{MARKET_DB_PATH}",
    future=True,
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionLocal = DBSessionLocal


# =============================================================================
# Section
# =============================================================================

@dataclass
class OpportunityView:
    id: int
    item_id: int
    market_hash_name: str
    buy_price: float
    buy_platform: str
    sell_price: float
    sell_platform: str
    profit_rate: float
    status: str
    action: str = "direct_trade"
    decision_id: int | None = None
    quantity: int = 1
    requires_jit: bool = True


# =============================================================================
# Section
# =============================================================================

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return bool(default)
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_app_config() -> dict:
    cfg: dict = {}
    if APP_CONFIG_PATH.exists():
        try:
            with open(APP_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception as exc:
            logger.warning("failed to load app_config.json; using defaults | err=%s", exc)
            cfg = {}

    cfg["SAFE_MODE_ENABLED"] = _env_bool("SAFE_MODE_ENABLED", cfg.get("SAFE_MODE_ENABLED", True))
    cfg["BUFF_PAY_METHOD"] = int(os.getenv("BUFF_PAY_METHOD", cfg.get("BUFF_PAY_METHOD", 51)))
    cfg["BUFF_COOKIE"] = os.getenv("BUFF_COOKIE", cfg.get("BUFF_COOKIE", ""))
    cfg["BUFF_COOKIE_FILE"] = os.getenv("BUFF_COOKIE_FILE", cfg.get("BUFF_COOKIE_FILE", ""))
    cfg["UUYP_COOKIE"] = os.getenv("UUYP_COOKIE", cfg.get("UUYP_COOKIE", ""))
    cfg["UUYP_COOKIE_FILE"] = os.getenv("UUYP_COOKIE_FILE", cfg.get("UUYP_COOKIE_FILE", ""))
    cfg["BUFF_GAMES"] = cfg.get("BUFF_GAMES", {"default": DEFAULT_GAME})
    cfg["JIT_BYPASS_MINUTES"] = int(os.getenv("JIT_BYPASS_MINUTES", cfg.get("JIT_BYPASS_MINUTES", DEFAULT_JIT_BYPASS_MINUTES)) or DEFAULT_JIT_BYPASS_MINUTES)
    return cfg


def load_credentials() -> dict:
    cred_path = BASE_DIR / "config" / "credentials.json"
    if not cred_path.exists():
        return {}
    try:
        return json.loads(cred_path.read_text(encoding="utf-8") or "{}") or {}
    except Exception as exc:
        logger.warning("failed to load credentials.json | err=%s", exc)
        return {}


def normalize_platform_credentials(raw: dict) -> dict:
    normalized: dict[str, dict[str, str]] = {}
    for platform in ("steam", "buff", "uuyp", "eco"):
        data = raw.get(platform) or {}
        if isinstance(data, dict):
            normalized[platform] = {str(k): str(v) for k, v in data.items() if v is not None}
        else:
            normalized[platform] = {}
    eco_openapi = raw.get("eco_openapi") or {}
    if isinstance(eco_openapi, dict):
        normalized["eco_openapi"] = {str(k): str(v) for k, v in eco_openapi.items() if v is not None}
    return normalized


def get_platform_cookie(config: dict, credentials: dict, platform: str, env_key: str) -> str:
    platform_data = credentials.get(platform) or {}
    if isinstance(platform_data, dict):
        for key in ("cookies", "cookie", "token", "auth", "session", "value"):
            val = str(platform_data.get(key, "")).strip()
            if val:
                return val

    cookie = str(config.get(env_key, "")).strip()
    if cookie:
        return cookie

    file_key = f"{env_key}_FILE"
    cookie_file = str(config.get(file_key, "")).strip()
    if cookie_file:
        path = Path(cookie_file)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("failed to read cookie file %s | err=%s", file_key, exc)
    return ""


def normalize_goods_name(name: str) -> str:
    return (name or "").strip()


def _has_uuyp_login_state(credentials: dict, config: dict) -> bool:
    data = credentials.get("uuyp") or {}
    cookie_blob = ""
    if isinstance(data, dict):
        cookie_blob = " ".join(str(v) for v in data.values() if v)
    cookie_blob += " " + str(config.get("UUYP_COOKIE", ""))
    return any(token in cookie_blob for token in ("uu_token", "Authorization", "Bearer", "deviceUk", "deviceId"))


def _default_trade_link(credentials: dict, config: dict) -> str:
    for source in (
        config,
        credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {},
        credentials.get("eco") if isinstance(credentials.get("eco"), dict) else {},
        credentials.get("eco_openapi") if isinstance(credentials.get("eco_openapi"), dict) else {},
    ):
        if not isinstance(source, dict):
            continue
        for key in ("ECO_TRADE_LINK", "STEAM_TRADE_LINK", "trade_link", "TradeLink", "tradeLink"):
            value = str(source.get(key, "")).strip()
            if value:
                return value
    return os.getenv("ECO_TRADE_LINK", os.getenv("STEAM_TRADE_LINK", "")).strip()


def _default_steam_id(credentials: dict, config: dict) -> str:
    for source in (
        config,
        credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {},
        credentials.get("eco") if isinstance(credentials.get("eco"), dict) else {},
        credentials.get("eco_openapi") if isinstance(credentials.get("eco_openapi"), dict) else {},
    ):
        if not isinstance(source, dict):
            continue
        for key in ("ECO_STEAM_ID", "steam_id", "SteamId", "steamId"):
            value = str(source.get(key, "")).strip()
            if value:
                return value
    return os.getenv("ECO_STEAM_ID", "").strip()


def _preflight_platform_or_verify(factory: PlatformClientFactory, session: Session, opportunity: OpportunityView, platform: str):
    try:
        preflight = factory.preflight(platform, purpose="auto_buy")
    except Exception as exc:
        logger.warning("platform preflight failed | platform=%s opportunity_id=%s err=%s", platform, opportunity.id, exc)
        mark_plan_blocked(session, opportunity, "platform_preflight_failed")
        session.commit()
        return None
    if not preflight.ok:
        logger.warning(
            "platform preflight blocked | platform=%s opportunity_id=%s reason=%s status=%s cooldown=%ss",
            platform,
            opportunity.id,
            preflight.reason,
            preflight.status,
            preflight.cooldown_remaining,
        )
        mark_plan_blocked(session, opportunity, f"platform_preflight_blocked:{preflight.reason}")
        session.commit()
        return None
    return preflight


# =============================================================================
# Section
# =============================================================================

def _get_cached_platform_id(platform: str, market_hash_name: str) -> Optional[str]:
    return ID_RESOLVE_CACHE.get((platform, market_hash_name))


def _set_cached_platform_id(platform: str, market_hash_name: str, resolved_id: str) -> None:
    ID_RESOLVE_CACHE[(platform, market_hash_name)] = str(resolved_id)


def _persist_platform_mapping(session: Session, item: ItemBase, platform: str, resolved_id: str) -> None:
    try:
        existing = (
            session.query(PlatformMapping)
            .filter(PlatformMapping.item_id == item.id, PlatformMapping.platform_name == platform)
            .one_or_none()
        )
        if existing is None:
            existing = PlatformMapping(item_id=item.id, platform_name=platform, platform_item_id=str(resolved_id))
            session.add(existing)
        else:
            existing.platform_item_id = str(resolved_id)
            session.add(existing)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("failed to persist PlatformMapping | err=%s", exc)


def resolve_platform_id(
    platform: str,
    market_hash_name: str,
    buyer=None,
    session: Session | None = None,
    allow_online_lookup: bool = False,
) -> Optional[str]:
    platform = (platform or "").lower().strip()
    normalized = normalize_goods_name(market_hash_name)
    if not platform or not normalized:
        return None

    cached = _get_cached_platform_id(platform, normalized)
    if cached:
        return cached

    if session is not None:
        try:
            item = session.query(ItemBase).filter(ItemBase.market_hash_name == normalized).one_or_none()
            if item is not None:
                if platform == "buff" and getattr(item, "buff_goods_id", None):
                    resolved = str(item.buff_goods_id)
                    _set_cached_platform_id(platform, normalized, resolved)
                    return resolved
                if platform == "uuyp" and getattr(item, "uuyp_template_id", None):
                    resolved = str(item.uuyp_template_id)
                    _set_cached_platform_id(platform, normalized, resolved)
                    return resolved
                if platform == "eco" and getattr(item, "eco_goods_id", None):
                    resolved = str(item.eco_goods_id)
                    _set_cached_platform_id(platform, normalized, resolved)
                    return resolved
                mapping = (
                    session.query(PlatformMapping)
                    .filter(PlatformMapping.item_id == item.id, PlatformMapping.platform_name == platform)
                    .one_or_none()
                )
                if mapping is not None and mapping.platform_item_id:
                    resolved = str(mapping.platform_item_id)
                    _set_cached_platform_id(platform, normalized, resolved)
                    return resolved
        except Exception as exc:
            logger.warning("failed to resolve platform id from ItemBase/PlatformMapping | err=%s", exc)

    if not allow_online_lookup:
        logger.info("platform id missing in local DB, online lookup disabled | platform=%s item=%s", platform, normalized)
        return None

    if buyer is None:
        return None

    try:
        if platform == "buff" and hasattr(buyer, "search_goods_id"):
            resolved = buyer.search_goods_id(normalized, game=DEFAULT_GAME)
        elif platform in {"uuyp", "eco"} and hasattr(buyer, "search_item_id_by_name"):
            resolved = buyer.search_item_id_by_name(normalized)
        else:
            resolved = None
    except Exception as exc:
        logger.warning("platform id online lookup failed | platform=%s item=%s err=%s", platform, normalized, exc)
        resolved = None

    if resolved is not None:
        resolved = str(resolved)
        _set_cached_platform_id(platform, normalized, resolved)
        if session is not None:
            try:
                item = session.query(ItemBase).filter(ItemBase.market_hash_name == normalized).one_or_none()
                if item is not None:
                    resolved_int = int(resolved)
                    if platform == "buff" and getattr(item, "buff_goods_id", None) != resolved_int:
                        item.buff_goods_id = resolved_int
                    elif platform == "uuyp" and getattr(item, "uuyp_template_id", None) != resolved_int:
                        item.uuyp_template_id = resolved_int
                    elif platform == "eco" and getattr(item, "eco_goods_id", None) != resolved_int:
                        item.eco_goods_id = resolved_int
                    session.add(item)
                    session.commit()
                    _persist_platform_mapping(session, item, platform, resolved)
            except Exception as exc:
                logger.warning("failed to save resolved platform id | err=%s", exc)
    return resolved


def resolve_goods_id(buyer, market_hash_name: str, game: str = DEFAULT_GAME, session=None) -> Optional[int]:
    normalized = normalize_goods_name(market_hash_name)
    if not normalized:
        return None
    resolved = resolve_platform_id("buff", normalized, buyer=buyer, session=session, allow_online_lookup=False)
    return int(resolved) if resolved is not None else None


def get_risk_limits(config: dict) -> tuple[Optional[float], int]:
    pipeline = config.get("pipeline") or {}
    max_purchase_price = pipeline.get("max_purchase_price")
    hard_qty_cap = int(pipeline.get("safe_purchase_hard_qty_cap", 50) or 50)
    try:
        max_purchase_price = float(max_purchase_price) if max_purchase_price is not None else None
    except Exception:
        max_purchase_price = None
    return max_purchase_price, hard_qty_cap


def resolve_platform_payload(opportunity: OpportunityView) -> dict[str, Any]:
    market_hash_name = normalize_goods_name(opportunity.market_hash_name)
    return {
        "goods_id": None,
        "market_hash_name": market_hash_name,
        "template_id": opportunity.item_id,
        "commodity_name": market_hash_name,
        "template_hash_name": market_hash_name,
    }


# =============================================================================
# Section
# =============================================================================

def fetch_open_opportunities(session: Session) -> Sequence[OpportunityView]:
    now = datetime.now()
    decision_stmt = (
        select(
            ActionDecision.id,
            ActionDecision.item_id,
            ItemBase.market_hash_name,
            ActionDecision.target_price,
            ActionDecision.target_platform,
            ActionDecision.reference_price,
            ActionDecision.sell_platform,
            ActionDecision.expected_profit_rate,
            ActionDecision.status,
            ActionDecision.action,
            ActionDecision.quantity,
            ActionDecision.requires_jit,
        )
        .join(ItemBase, ActionDecision.item_id == ItemBase.id)
        .where(ActionDecision.status == "open")
        .where(ActionDecision.action.in_(["create_buy_order", "direct_buy"]))
        .where((ActionDecision.expires_at.is_(None)) | (ActionDecision.expires_at >= now))
        .order_by(ActionDecision.score.desc(), ActionDecision.id.asc())
    )
    decision_rows = session.execute(decision_stmt).all()
    if decision_rows:
        return [
            OpportunityView(
                id=int(row.id),
                item_id=int(row.item_id),
                market_hash_name=row.market_hash_name,
                buy_price=float(row.target_price or 0),
                buy_platform=row.target_platform,
                sell_price=float(row.reference_price or 0),
                sell_platform=row.sell_platform or "steam",
                profit_rate=float(row.expected_profit_rate or 0),
                status=row.status,
                action=row.action,
                decision_id=int(row.id),
                quantity=max(1, int(row.quantity or 1)),
                requires_jit=bool(row.requires_jit),
            )
            for row in decision_rows
        ]

    stmt = (
        select(
            ArbitrageOpportunity.id,
            ArbitrageOpportunity.item_id,
            ItemBase.market_hash_name,
            ArbitrageOpportunity.buy_price,
            ArbitrageOpportunity.buy_platform,
            ArbitrageOpportunity.sell_price,
            ArbitrageOpportunity.sell_platform,
            ArbitrageOpportunity.profit_rate,
            ArbitrageOpportunity.status,
            ArbitrageOpportunity.action,
        )
        .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
        .where(ArbitrageOpportunity.status == "open")
        .order_by(ArbitrageOpportunity.id.asc())
    )
    rows = session.execute(stmt).all()
    return [OpportunityView(*row) for row in rows]


def pre_refresh_stale_opportunities(session: Session, opportunities: Sequence[OpportunityView], config: dict) -> None:
    cutoff = datetime.now() - timedelta(minutes=_max_staleness_minutes(config))
    refresh_map: dict[int, set[str]] = {}
    skipped = 0

    for opportunity in opportunities:
        buy_platform = (opportunity.buy_platform or "").lower().strip()
        sell_platform = (opportunity.sell_platform or "steam").lower().strip()
        platforms = {buy_platform, sell_platform, "steam"}
        skip_jit, reason = should_skip_jit_refresh(session, opportunity, config, platforms)
        if skip_jit:
            skipped += 1
            logger.info(
                "JIT pre-refresh skipped | opportunity_id=%s item_id=%s reason=%s",
                opportunity.id,
                opportunity.item_id,
                reason,
            )
            continue
        price_map = _load_price_map(session, opportunity.item_id, platforms)
        if any(_price_is_stale(price_map.get(platform), cutoff) for platform in platforms):
            refresh_map.setdefault(opportunity.item_id, set()).update(platforms)

    if not refresh_map:
        logger.info("JIT pre-refresh skipped | stale_items=0 exempted=%s", skipped)
        return

    item_ids = set(refresh_map)
    all_platforms = {platform for platforms in refresh_map.values() for platform in platforms}
    logger.info(
        "JIT pre-refresh started | stale_items=%s platforms=%s exempted=%s",
        len(item_ids),
        sorted(all_platforms),
        skipped,
    )
    asyncio.run(refresh_items_prices(item_ids, all_platforms, fast=True))
    JIT_REFRESHED_ITEMS.update(item_ids)
    session.expire_all()
    logger.info("JIT pre-refresh completed | stale_items=%s exempted=%s", len(item_ids), skipped)


def mark_opportunity_verifying(session: Session, opportunity_id: int) -> None:
    obj = session.get(ArbitrageOpportunity, opportunity_id)
    if obj is None:
        logger.warning("opportunity not found while marking verifying | opportunity_id=%s", opportunity_id)
        return
    obj.status = "verifying"
    session.add(obj)


def _max_staleness_minutes(config: dict) -> int:
    pipeline = config.get("pipeline") or {}
    raw = pipeline.get("max_staleness_minutes", pipeline.get("current_price_refresh_minutes", 10))
    try:
        return max(1, int(raw or 10))
    except Exception:
        return 10


def _price_is_stale(price: MarketPrice | None, cutoff: datetime) -> bool:
    return price is None or price.updated_at is None or price.updated_at < cutoff


def _jit_bypass_minutes(config: dict) -> int:
    pipeline = config.get("pipeline") or {}
    raw = config.get("JIT_BYPASS_MINUTES", pipeline.get("JIT_BYPASS_MINUTES", DEFAULT_JIT_BYPASS_MINUTES))
    try:
        return max(0, int(raw or DEFAULT_JIT_BYPASS_MINUTES))
    except Exception:
        return DEFAULT_JIT_BYPASS_MINUTES


def _purchase_order_jit_bypass_minutes(config: dict) -> int:
    pipeline = config.get("pipeline") or {}
    raw = pipeline.get(
        "purchase_order_jit_bypass_minutes",
        config.get("PURCHASE_ORDER_JIT_BYPASS_MINUTES", DEFAULT_PURCHASE_ORDER_JIT_BYPASS_MINUTES),
    )
    try:
        return max(0, int(raw or DEFAULT_PURCHASE_ORDER_JIT_BYPASS_MINUTES))
    except Exception:
        return DEFAULT_PURCHASE_ORDER_JIT_BYPASS_MINUTES


def _steamdt_bypass_minutes(config: dict) -> int:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    raw = steamdt.get("jit_bypass_minutes", config.get("STEAMDT_JIT_BYPASS_MINUTES", DEFAULT_STEAMDT_BYPASS_MINUTES))
    try:
        return max(0, int(raw or DEFAULT_STEAMDT_BYPASS_MINUTES))
    except Exception:
        return DEFAULT_STEAMDT_BYPASS_MINUTES


def _steamdt_min_volume(config: dict) -> int:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    raw = steamdt.get("jit_min_volume", config.get("STEAMDT_JIT_MIN_VOLUME", DEFAULT_STEAMDT_MIN_VOLUME))
    try:
        return max(0, int(raw or DEFAULT_STEAMDT_MIN_VOLUME))
    except Exception:
        return DEFAULT_STEAMDT_MIN_VOLUME


def _steamdt_jit_bypass_allowed(config: dict) -> bool:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    raw = steamdt.get("allow_jit_bypass", True)
    allowed = raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return allowed and bool(config.get("SAFE_MODE_ENABLED", True))


def _opportunity_action(opportunity: OpportunityView) -> str:
    return str(getattr(opportunity, "action", "") or "").strip().lower()


def _is_direct_buy_action(opportunity: OpportunityView) -> bool:
    action = _opportunity_action(opportunity)
    return any(token.lower() in action for token in DIRECT_BUY_ACTION_TOKENS)


def _is_purchase_order_action(opportunity: OpportunityView) -> bool:
    action = _opportunity_action(opportunity)
    return any(token.lower() in action for token in ORDER_ACTION_TOKENS)


def _latest_price_updated_at(price_map: dict[str, MarketPrice], platforms: set[str]) -> datetime | None:
    timestamps = [
        row.updated_at
        for platform, row in price_map.items()
        if platform in platforms and row is not None and row.updated_at is not None
    ]
    return max(timestamps) if timestamps else None


def _all_prices_fresh_within(price_map: dict[str, MarketPrice], platforms: set[str], minutes: int) -> bool:
    if minutes <= 0:
        return False
    cutoff = datetime.now() - timedelta(minutes=minutes)
    for platform in {p for p in platforms if p}:
        row = price_map.get(platform)
        if row is None or row.updated_at is None or row.updated_at < cutoff:
            return False
        if _safe_float(getattr(row, "sell_min", None)) <= 0 and _safe_float(getattr(row, "buy_max", None)) <= 0:
            return False
    return True


def _has_fresh_steamdt_buffer(
    price_map: dict[str, MarketPrice],
    platforms: set[str],
    *,
    max_age_minutes: int,
    min_volume: int,
) -> bool:
    if max_age_minutes <= 0:
        return False
    cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
    required = {p for p in platforms if p}
    for platform in required:
        price = price_map.get(platform)
        if price is None:
            return False
        if (price.data_source or "").lower() != "steamdt":
            return False
        if price.updated_at is None or price.updated_at < cutoff:
            return False
        if _safe_float(getattr(price, "sell_min", None)) <= 0 and _safe_float(getattr(price, "buy_max", None)) <= 0:
            return False
        if int(price.volume or 0) < min_volume:
            return False
    return True


def should_skip_jit_refresh(
    session: Session,
    opportunity: OpportunityView,
    config: dict,
    platforms: set[str] | None = None,
) -> tuple[bool, str]:
    """Decide whether execution can use existing DB quotes without JIT probing."""
    buy_platform = (opportunity.buy_platform or "").lower().strip()
    sell_platform = (opportunity.sell_platform or "steam").lower().strip()
    platforms = platforms or {buy_platform, sell_platform, "steam"}

    action = _opportunity_action(opportunity)
    if opportunity.decision_id is not None and bool(getattr(opportunity, "requires_jit", True)):
        return False, "action_decision_requires_jit"
    if _is_purchase_order_action(opportunity):
        minutes = _purchase_order_jit_bypass_minutes(config)
        price_map = _load_price_map(session, opportunity.item_id, platforms)
        if _all_prices_fresh_within(price_map, platforms, minutes):
            return True, f"fresh_purchase_order_within_{minutes}m"
        return False, "purchase_order_prices_stale"

    if action and not _is_direct_buy_action(opportunity):
        return False, "unknown_action_requires_jit"
    return False, "trade_action_requires_jit"


def _load_price_map(session: Session, item_id: int, platforms: set[str]) -> dict[str, MarketPrice]:
    stmt = select(MarketPrice).where(
        MarketPrice.item_id == item_id,
        MarketPrice.platform_name.in_([p.lower().strip() for p in platforms if p]),
    )
    rows = session.execute(stmt).scalars().all()
    return {row.platform_name.lower().strip(): row for row in rows}


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _simulated_result(platform: str, platform_id: str | int | None) -> dict[str, Any]:
    return {
        "success": True,
        "simulated": True,
        "msg": "SAFE_MODE_SIMULATED",
        "platform": platform,
        "platform_id": str(platform_id) if platform_id is not None else None,
    }


def _buy_result_order_ids(result: dict[str, Any] | None) -> list[str]:
    if not isinstance(result, dict):
        return []
    order_ids: list[str] = []

    def add(value: Any) -> None:
        if value in (None, ""):
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        token = str(value).strip()
        if token and token not in order_ids:
            order_ids.append(token)

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for source in (result, data):
        for key in ("order_id", "bill_order_id", "buy_order_id", "OrderNo", "orderNo", "PurchaseId"):
            add(source.get(key))
        for key in ("order_ids", "bill_order_ids", "buy_order_ids", "OrderNos", "orderNos", "PurchaseIds"):
            add(source.get(key))
    return order_ids


def _buy_result_order_id(result: dict[str, Any] | None) -> str:
    order_ids = _buy_result_order_ids(result)
    return order_ids[0] if order_ids else ""


def _is_success_result(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    return bool(result.get("success") or result.get("simulated"))


def _request_buff_seller_offer(buyer, result: dict[str, Any], *, game: str = DEFAULT_GAME) -> None:
    if not isinstance(result, dict) or not result.get("success") or not hasattr(buyer, "ask_seller_to_send"):
        return
    order_ids = _buy_result_order_ids(result)
    if not order_ids:
        return
    try:
        ask_result = buyer.ask_seller_to_send(order_ids, game=game)
        result["seller_offer_request"] = ask_result
        if isinstance(ask_result, dict) and ask_result.get("success"):
            logger.info("Buff seller offer requested | order_ids=%s", order_ids)
        else:
            logger.warning("Buff seller offer request failed | order_ids=%s result=%s", order_ids, ask_result)
    except Exception as exc:
        result["seller_offer_request"] = {"success": False, "msg": str(exc)}
        logger.warning("Buff seller offer request exception | order_ids=%s err=%s", order_ids, exc)


def verify_opportunity_freshness(session: Session, opportunity: OpportunityView, config: dict) -> bool:
    buy_platform = (opportunity.buy_platform or "").lower().strip()
    sell_platform = (opportunity.sell_platform or "steam").lower().strip()
    platforms = {buy_platform, sell_platform, "steam"}
    cutoff = datetime.now() - timedelta(minutes=_max_staleness_minutes(config))
    price_map = _load_price_map(session, opportunity.item_id, platforms)

    skip_jit, skip_reason = should_skip_jit_refresh(session, opportunity, config, platforms)
    if skip_jit:
        logger.info(
            "JIT refresh bypassed | opportunity_id=%s item_id=%s reason=%s",
            opportunity.id,
            opportunity.item_id,
            skip_reason,
        )
    else:
        if opportunity.item_id not in JIT_REFRESHED_ITEMS:
            logger.info("JIT single refresh started | opportunity_id=%s item_id=%s platforms=%s", opportunity.id, opportunity.item_id, sorted(platforms))
            asyncio.run(refresh_single_item_prices(opportunity.item_id, platforms))
            JIT_REFRESHED_ITEMS.add(opportunity.item_id)
            session.expire_all()
            price_map = _load_price_map(session, opportunity.item_id, platforms)
        elif any(_price_is_stale(price_map.get(platform), cutoff) for platform in platforms):
            logger.info("JIT already refreshed this round but quote is still stale | opportunity_id=%s item_id=%s", opportunity.id, opportunity.item_id)

    buy_record = price_map.get(buy_platform)
    sell_record = price_map.get(sell_platform)
    buy_price = _safe_float(getattr(buy_record, "sell_min", None))
    sell_price = _safe_float(getattr(sell_record, "buy_max", None))
    if sell_price <= 0:
        sell_price = _safe_float(getattr(sell_record, "sell_min", None))

    obj = None if opportunity.decision_id is not None else session.get(ArbitrageOpportunity, opportunity.id)

    pipeline = config.get("pipeline") or {}
    balance_ratio = steam_balance_cost_ratio(config)
    max_discount = _safe_float(pipeline.get("max_discount", 0.8)) or 0.8
    effective_buy_cost = buy_price * balance_ratio if buy_platform == "steam" else buy_price
    if buy_price <= 0 or sell_price <= 0 or (effective_buy_cost / sell_price) > max_discount:
        if obj is not None:
            obj.status = "closed"
            session.add(obj)
            session.commit()
        logger.info("JIT validation rejected stale or invalid quote | opportunity_id=%s buy=%.2f sell=%.2f", opportunity.id, buy_price, sell_price)
        return False

    math = opportunity_profit(
        buy_platform=buy_platform,
        sell_platform=sell_platform,
        buy_price=buy_price,
        sell_price=sell_price,
        balance_cost_ratio=balance_ratio,
    )
    after_tax_revenue = math.revenue_cny
    cash_profit = math.profit_cny
    self_profit = after_tax_revenue - buy_price
    if cash_profit <= 0 and self_profit <= 0:
        if obj is not None:
            obj.status = "closed"
            session.add(obj)
            session.commit()
        logger.info("JIT validation rejected unprofitable quote | opportunity_id=%s buy=%.2f sell=%.2f", opportunity.id, buy_price, sell_price)
        return False

    if obj is not None:
        obj.buy_price = buy_price
        obj.sell_price = sell_price
        obj.profit_cny = cash_profit
        obj.profit_rate = math.profit_rate
        session.add(obj)
        session.commit()
    opportunity.buy_price = buy_price
    opportunity.sell_price = sell_price
    opportunity.profit_rate = math.profit_rate
    return True


def close_underlying_opportunity_for_decision(session: Session, decision_id: int | None, status: str = "closed") -> None:
    if decision_id is None:
        return
    decision = session.get(ActionDecision, decision_id)
    if decision is None or decision.opportunity_id is None:
        return
    obj = session.get(ArbitrageOpportunity, int(decision.opportunity_id))
    if obj is None:
        return
    obj.status = status
    session.add(obj)


def mark_decision_done(session: Session, decision_id: int | None, status: str, result: dict[str, Any] | None = None) -> None:
    if decision_id is None:
        return
    decision = session.get(ActionDecision, decision_id)
    if decision is None:
        return
    decision.status = status
    detail = result.get("msg") if isinstance(result, dict) else ""
    if detail:
        decision.reason = f"{decision.reason or ''} | executor={detail}"[:1024]
    session.add(decision)


def mark_plan_blocked(session: Session, opportunity: OpportunityView, msg: str) -> None:
    if opportunity.decision_id is not None:
        mark_decision_done(session, opportunity.decision_id, "failed", {"msg": msg})
        if msg == "jit_validation_failed":
            close_underlying_opportunity_for_decision(session, opportunity.decision_id, "closed")
    else:
        mark_opportunity_verifying(session, opportunity.id)


# =============================================================================
# Section
# =============================================================================

def _init_buyers(config: dict, credentials: dict):
    from buff import BuffBuyer
    from eco import EcoBuyer
    from uuyp import UuypBuyer
    from app.services.steam_buyer import SteamBuyer

    buff_cookie = get_platform_cookie(config, credentials, "buff", "BUFF_COOKIE")
    uuyp_cookie = get_platform_cookie(config, credentials, "uuyp", "UUYP_COOKIE")
    steam_cookie = get_platform_cookie(config, credentials, "steam", "STEAM_COOKIE")
    steam_session_id = (
        str((credentials.get("steam") or {}).get("sessionid") or "").strip()
        or str((credentials.get("steam") or {}).get("session_id") or "").strip()
        or str(config.get("STEAM_SESSION_ID") or "").strip()
    )
    eco_openapi = credentials.get("eco_openapi") if isinstance(credentials.get("eco_openapi"), dict) else {}

    buff_buyer = BuffBuyer(cookie_str=buff_cookie, pay_method=int(config.get("BUFF_PAY_METHOD", 51))) if buff_cookie else None
    uuyp_buyer = UuypBuyer(cookie_str=credentials.get("uuyp", {})) if uuyp_cookie else None
    steam_buyer = SteamBuyer(cookie_str=steam_cookie, session_id=steam_session_id, currency=int(config.get("STEAM_CURRENCY", 23) or 23)) if steam_cookie and steam_session_id else None
    try:
        eco_buyer = EcoBuyer(cookie_str=eco_openapi or credentials.get("eco", {})) if eco_openapi else None
    except Exception as exc:
        logger.warning("eco openapi credentials invalid; ECO buyer disabled | err=%s", exc)
        eco_buyer = None

    if not buff_cookie:
        logger.warning("buff credentials missing; Buff buyer disabled")
    if not uuyp_cookie:
        logger.warning("uuyp credentials missing; UUYP buyer disabled")
    if not eco_openapi:
        logger.warning("eco_openapi credentials missing; ECO buyer disabled")
    if not steam_cookie or not steam_session_id:
        logger.warning("steam credentials/sessionid missing; Steam buyer disabled")

    return buff_buyer, uuyp_buyer, eco_buyer, steam_buyer


def run_trade_executor() -> None:
    from buff import BuffAuthExpired

    config = load_app_config()
    credentials = normalize_platform_credentials(load_credentials())
    safe_mode = bool(config.get("SAFE_MODE_ENABLED", True))
    _, hard_qty_cap = get_risk_limits(config)
    uuyp_auth_available = _has_uuyp_login_state(credentials, config)
    platform_factory = PlatformClientFactory(credentials=credentials, config=config)

    buff_buyer, uuyp_buyer, eco_buyer, steam_buyer = _init_buyers(config, credentials)

    logger.info("trade_executor started | SAFE_MODE_ENABLED=%s", safe_mode)

    with SessionLocal() as session:
        try:
            opportunities = fetch_open_opportunities(session)
            if not opportunities:
                logger.info("no open arbitrage opportunities")
                return

            logger.info("loaded open opportunities | count=%s", len(opportunities))
            pre_refresh_stale_opportunities(session, opportunities, config)

            for opportunity in opportunities:
                platform = (opportunity.buy_platform or "").lower().strip()
                logger.info(
                    "processing opportunity | id=%s item_id=%s name=%s platform=%s price=%.2f",
                    opportunity.id,
                    opportunity.item_id,
                    opportunity.market_hash_name,
                    platform,
                    opportunity.buy_price,
                )

                if platform not in SUPPORTED_BUY_PLATFORMS:
                    logger.warning("unsupported buy platform %s; marking verifying", platform)
                    mark_plan_blocked(session, opportunity, "unsupported_platform")
                    session.commit()
                    continue

                exposure_decision = LowPriceExposureGuard(config).check(
                    session,
                    item_id=int(opportunity.item_id or 0),
                    market_hash_name=opportunity.market_hash_name,
                    unit_price=float(opportunity.buy_price or 0),
                    proposed_quantity=max(1, int(getattr(opportunity, "quantity", 1) or 1)),
                    fail_closed=True,
                )
                if not exposure_decision.allowed:
                    logger.info(
                        "low price exposure blocked opportunity | id=%s item_id=%s reason=%s current=%s max=%s",
                        opportunity.id,
                        opportunity.item_id,
                        exposure_decision.reason,
                        exposure_decision.current_quantity,
                        exposure_decision.max_quantity,
                    )
                    mark_plan_blocked(session, opportunity, exposure_decision.reason)
                    session.commit()
                    continue

                preflight = _preflight_platform_or_verify(platform_factory, session, opportunity, platform)
                if preflight is None:
                    continue

                if not verify_opportunity_freshness(session, opportunity, config):
                    mark_plan_blocked(session, opportunity, "jit_validation_failed")
                    session.commit()
                    continue

                session.expire_on_commit = False
                platform_payload = resolve_platform_payload(opportunity)
                result: dict[str, Any] = {"success": False, "msg": "not_executed"}

                if platform == "buff":
                    if buff_buyer is None:
                        try:
                            buff_buyer, _, _ = platform_factory.client("buff", purpose="auto_buy")
                        except Exception as exc:
                            logger.warning("Buff provider init failed | opportunity_id=%s err=%s", opportunity.id, exc)
                            buff_buyer = None
                    if not buff_buyer:
                        logger.warning("Buff buyer unavailable; marking verifying | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "buff_buyer_unavailable")
                        session.commit()
                        continue
                    goods_id = resolve_goods_id(buff_buyer, opportunity.market_hash_name, game=DEFAULT_GAME, session=session)
                    if goods_id is None:
                        logger.warning("platform id missing; marking verifying | platform=%s item=%s", platform, opportunity.market_hash_name)
                        mark_plan_blocked(session, opportunity, "platform_id_missing")
                        session.commit()
                        continue
                    if hard_qty_cap < 1:
                        logger.info("hard_qty_cap < 1; skip opportunity | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "hard_qty_cap")
                        session.commit()
                        continue
                    if safe_mode:
                        logger.info("SAFE_MODE simulated Buff order | price=%.2f item=%s goods_id=%s", opportunity.buy_price, opportunity.market_hash_name, goods_id)
                        result = _simulated_result("buff", goods_id)
                    else:
                        try:
                            if _is_direct_buy_action(opportunity) and not _is_purchase_order_action(opportunity):
                                result = buff_buyer.direct_buy(
                                    goods_id=goods_id,
                                    price=float(opportunity.buy_price),
                                    num=max(1, int(opportunity.quantity or 1)),
                                    game=DEFAULT_GAME,
                                )
                                _request_buff_seller_offer(buff_buyer, result, game=DEFAULT_GAME)
                            else:
                                result = buff_buyer.create_buy_order(
                                    goods_id=goods_id,
                                    price=float(opportunity.buy_price),
                                    num=max(1, int(opportunity.quantity or 1)),
                                    game=DEFAULT_GAME,
                                )
                        except BuffAuthExpired:
                            logger.exception("Buff auth expired; stopping this executor round")
                            raise
                        except Exception as exc:
                            logger.exception("BuffBuyer.create_buy_order failed: %s", exc)
                            result = {"success": False, "msg": str(exc)}

                elif platform == "uuyp":
                    if is_uuyp_auth_circuit_open():
                        logger.warning(
                            "UUYP_AUTH_REQUIRED: auth circuit open, skip opportunity | id=%s cooldown=%ss",
                            opportunity.id,
                            uuyp_auth_circuit_remaining_seconds(),
                        )
                        mark_plan_blocked(session, opportunity, "uuyp_auth_circuit_open")
                        session.commit()
                        continue
                    if not uuyp_auth_available:
                        logger.warning("UUYP auth unavailable; marking verifying | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "uuyp_auth_unavailable")
                        session.commit()
                        continue
                    if uuyp_buyer is None:
                        try:
                            uuyp_buyer, _, _ = platform_factory.client("uuyp", purpose="auto_buy")
                        except Exception as exc:
                            logger.warning("UUYP provider init failed | opportunity_id=%s err=%s", opportunity.id, exc)
                            uuyp_buyer = None
                    if not uuyp_buyer:
                        logger.warning("UUYP buyer unavailable; marking verifying | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "uuyp_buyer_unavailable")
                        session.commit()
                        continue
                    resolved_id = resolve_platform_id("uuyp", opportunity.market_hash_name, buyer=uuyp_buyer, session=session)
                    if resolved_id is None:
                        logger.warning("UUYP templateId missing; marking verifying | item=%s", opportunity.market_hash_name)
                        mark_plan_blocked(session, opportunity, "uuyp_template_id_missing")
                        session.commit()
                        continue
                    if hard_qty_cap < 1:
                        logger.info("hard_qty_cap < 1; skip opportunity | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "hard_qty_cap")
                        session.commit()
                        continue
                    if safe_mode:
                        logger.info("SAFE_MODE simulated UUYP order | price=%.2f item=%s template_id=%s", opportunity.buy_price, opportunity.market_hash_name, resolved_id)
                        result = _simulated_result("uuyp", resolved_id)
                    else:
                        if _is_direct_buy_action(opportunity) and not _is_purchase_order_action(opportunity):
                            listing = uuyp_buyer.select_best_listing(
                                resolved_id,
                                max_price=float(opportunity.buy_price),
                                game_id="730",
                                page_size=max(10, int(opportunity.quantity or 1)),
                            ) if hasattr(uuyp_buyer, "select_best_listing") else None
                            if not listing:
                                result = {
                                    "success": False,
                                    "msg": "UUYP no direct listing within target price",
                                    "reason": "direct_listing_not_found",
                                }
                            else:
                                commodity_no = listing.get("_selected_commodity_no")
                                direct_price = float(listing.get("_selected_price") or opportunity.buy_price)
                                result = uuyp_buyer.buy_listing(
                                    commodity_no=commodity_no,
                                    price=direct_price,
                                    game_id="730",
                                )
                        else:
                            result = uuyp_buyer.create_buy_order(
                                goods_id=resolved_id,
                                price=float(opportunity.buy_price),
                                num=max(1, int(opportunity.quantity or 1)),
                                template_id=resolved_id,
                                commodity_name=platform_payload["commodity_name"],
                                template_hash_name=platform_payload["template_hash_name"],
                                market_hash_name=platform_payload["market_hash_name"],
                            )

                elif platform == "eco":
                    if eco_buyer is None:
                        try:
                            eco_buyer, _, _ = platform_factory.client("eco", purpose="auto_buy")
                        except Exception as exc:
                            logger.warning("ECO provider init failed | opportunity_id=%s err=%s", opportunity.id, exc)
                            eco_buyer = None
                    if not eco_buyer:
                        logger.warning("ECO buyer unavailable; marking verifying | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "eco_buyer_unavailable")
                        session.commit()
                        continue
                    if hard_qty_cap < 1:
                        logger.info("hard_qty_cap < 1; skip opportunity | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "hard_qty_cap")
                        session.commit()
                        continue
                    trade_link = _default_trade_link(credentials, config)
                    steam_id = _default_steam_id(credentials, config)
                    if safe_mode:
                        logger.info("SAFE_MODE simulated ECO order | price=%.2f item=%s", opportunity.buy_price, opportunity.market_hash_name)
                        result = _simulated_result("eco", None)
                    else:
                        if _is_purchase_order_action(opportunity) and not _is_direct_buy_action(opportunity):
                            result = eco_buyer.create_purchase_order(
                                market_hash_name=platform_payload["market_hash_name"],
                                price=float(opportunity.buy_price),
                                num=max(1, int(opportunity.quantity or 1)),
                                trade_link=trade_link,
                                steam_id=steam_id,
                            )
                        else:
                            result = eco_buyer.create_buy_order(
                                price=float(opportunity.buy_price),
                                num=max(1, int(opportunity.quantity or 1)),
                                trade_link=trade_link,
                                steam_id=steam_id,
                                commodity_name=platform_payload["commodity_name"],
                                market_hash_name=platform_payload["market_hash_name"],
                            )

                elif platform == "steam":
                    if steam_buyer is None:
                        try:
                            steam_buyer, _, _ = platform_factory.client("steam", purpose="auto_buy")
                        except Exception as exc:
                            logger.warning("Steam provider init failed | opportunity_id=%s err=%s", opportunity.id, exc)
                            steam_buyer = None
                    if not steam_buyer:
                        logger.warning("Steam buyer unavailable; marking verifying | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "steam_buyer_unavailable")
                        session.commit()
                        continue
                    if hard_qty_cap < 1:
                        logger.info("hard_qty_cap < 1; skip opportunity | id=%s", opportunity.id)
                        mark_plan_blocked(session, opportunity, "hard_qty_cap")
                        session.commit()
                        continue
                    if safe_mode:
                        logger.info("SAFE_MODE simulated Steam buy order | price=%.2f item=%s", opportunity.buy_price, opportunity.market_hash_name)
                        result = _simulated_result("steam", opportunity.market_hash_name)
                    else:
                        steam_result = steam_buyer.create_buy_order(
                            market_hash_name=platform_payload["market_hash_name"],
                            price=float(opportunity.buy_price),
                            quantity=max(1, int(opportunity.quantity or 1)),
                        )
                        result = {
                            "success": bool(getattr(steam_result, "success", False)),
                            "msg": getattr(steam_result, "msg", ""),
                            "raw": getattr(steam_result, "raw", None),
                        }

                logger.info("opportunity processed | id=%s result=%s", opportunity.id, result)
                if isinstance(result, dict):
                    try:
                        platform_factory.provider(platform).classify_result(result)
                    except Exception:
                        pass
                if _is_success_result(result):
                    _notify_trade_success_safe(opportunity, result)
                if opportunity.decision_id is not None:
                    mark_decision_done(session, opportunity.decision_id, "success" if _is_success_result(result) else "failed", result)
                else:
                    mark_opportunity_verifying(session, opportunity.id)
                session.commit()

            logger.info("trade_executor round completed")
        except Exception:
            session.rollback()
            logger.exception("trade_executor failed")
            raise


if __name__ == "__main__":
    run_trade_executor()
