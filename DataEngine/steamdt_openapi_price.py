from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import func

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import ItemBase, MarketPrice, SessionLocal, normalize_data_timestamp, upsert_market_price_if_fresh
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.proxy_pool import (
    classify_request_failure,
    get_request_proxies,
    mark_proxy_failure,
    mark_proxy_success,
    proxy_cooldown_for_reason,
    proxy_log_tag,
)
from DataEngine.stop_signal import clear_stop, raise_if_stop_requested

setup_dataengine_logging()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"
STATE_PATH = BASE_DIR / "config" / "steamdt_openapi_price_state.json"
QUOTA_STATE_PATH = BASE_DIR / "config" / "steamdt_openapi_price_quota.json"
CHECKPOINT_STATE_PATH = BASE_DIR / "config" / "steamdt_openapi_price_checkpoints.json"

DEFAULT_BASE_URL = "https://open.steamdt.com"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_BATCH_RPM = 1
DEFAULT_SINGLE_RPM = 60
DEFAULT_BATCH_SIZE = 100
DEFAULT_P2_TARGET_MINUTES = 60
DEFAULT_P3_TARGET_MINUTES = 30
DEFAULT_MODE = "stable"
DEFAULT_TRACKED_PLATFORMS = ("steam", "buff", "uuyp", "eco")
DEFAULT_USE_PROXY = True

PLATFORM_NAME_ALIASES = {
    "steam": "steam",
    "buff": "buff",
    "buff163": "buff",
    "youpin": "uuyp",
    "uuyp": "uuyp",
    "ecosteam": "eco",
    "eco": "eco",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("[steamdt-openapi-price] load json failed | path=%s err=%s", path, exc)
    return {}


def _env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int, min_value: int = 0) -> int:
    try:
        return max(min_value, int(value))
    except Exception:
        return default


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _load_config() -> dict[str, Any]:
    root_cfg = _read_json(CONFIG_PATH)
    steamdt_cfg = root_cfg.get("steamdt") if isinstance(root_cfg.get("steamdt"), dict) else {}
    openapi_cfg = steamdt_cfg.get("openapi") if isinstance(steamdt_cfg.get("openapi"), dict) else {}
    price_cfg = steamdt_cfg.get("openapi_price") if isinstance(steamdt_cfg.get("openapi_price"), dict) else {}

    credentials = _read_json(CREDENTIALS_PATH)
    cred_openapi = credentials.get("steamdt_openapi") if isinstance(credentials.get("steamdt_openapi"), dict) else {}

    api_key = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_API_KEY"),
        price_cfg.get("api_key"),
        openapi_cfg.get("api_key"),
        steamdt_cfg.get("openapi_api_key"),
        cred_openapi.get("api_key"),
    )

    enabled_default = bool(price_cfg.get("enabled", bool(api_key)))
    enabled = _env_bool(os.getenv("STEAMDT_OPENAPI_PRICE_ENABLED"), enabled_default)

    base_url = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_BASE_URL"),
        price_cfg.get("base_url"),
        openapi_cfg.get("base_url"),
        steamdt_cfg.get("openapi_base_url"),
        DEFAULT_BASE_URL,
    ).rstrip("/")

    timeout_seconds = _safe_int(
        os.getenv("STEAMDT_OPENAPI_PRICE_TIMEOUT", price_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        DEFAULT_TIMEOUT_SECONDS,
        min_value=5,
    )
    batch_rpm = _safe_int(
        os.getenv("STEAMDT_OPENAPI_PRICE_BATCH_RPM", price_cfg.get("batch_requests_per_minute", DEFAULT_BATCH_RPM)),
        DEFAULT_BATCH_RPM,
        min_value=0,
    )
    single_rpm = _safe_int(
        os.getenv("STEAMDT_OPENAPI_PRICE_SINGLE_RPM", price_cfg.get("single_requests_per_minute", DEFAULT_SINGLE_RPM)),
        DEFAULT_SINGLE_RPM,
        min_value=0,
    )
    batch_size = _safe_int(
        os.getenv("STEAMDT_OPENAPI_PRICE_BATCH_SIZE", price_cfg.get("batch_size", DEFAULT_BATCH_SIZE)),
        DEFAULT_BATCH_SIZE,
        min_value=1,
    )
    batch_size = min(batch_size, 100)
    p2_target_minutes = _safe_int(price_cfg.get("p2_target_minutes", DEFAULT_P2_TARGET_MINUTES), DEFAULT_P2_TARGET_MINUTES, min_value=5)
    p3_target_minutes = _safe_int(price_cfg.get("p3_target_minutes", DEFAULT_P3_TARGET_MINUTES), DEFAULT_P3_TARGET_MINUTES, min_value=5)
    jitter_single_reserved = _safe_int(price_cfg.get("single_reserved_for_jit", 15), 15, min_value=0)
    mode = str(price_cfg.get("mode") or DEFAULT_MODE).strip().lower()
    if mode not in {"init", "stable", "idle", "custom"}:
        mode = DEFAULT_MODE
    stable_pool_cycle_minutes = _safe_int(price_cfg.get("stable_pool_cycle_minutes", 60), 60, min_value=10)
    discovery_target_minutes = _safe_int(price_cfg.get("discovery_target_minutes", 720), 720, min_value=30)
    custom_pool_share_pct = _safe_int(price_cfg.get("custom_pool_share_pct", 70), 70, min_value=0)
    custom_pool_share_pct = min(100, custom_pool_share_pct)
    auto_switch_to_stable_on_idle_complete = bool(price_cfg.get("auto_switch_to_stable_on_idle_complete", True))
    use_proxy = _env_bool(os.getenv("STEAMDT_OPENAPI_PRICE_USE_PROXY"), bool(price_cfg.get("use_proxy", DEFAULT_USE_PROXY)))
    tracked = price_cfg.get("tracked_platforms", list(DEFAULT_TRACKED_PLATFORMS))
    if not isinstance(tracked, list) or not tracked:
        tracked = list(DEFAULT_TRACKED_PLATFORMS)
    tracked = [str(p).strip().lower() for p in tracked if str(p).strip()]
    tracked = [PLATFORM_NAME_ALIASES.get(p, p) for p in tracked]

    return {
        "enabled": enabled,
        "api_key": api_key,
        "base_url": base_url,
        "timeout_seconds": timeout_seconds,
        "batch_rpm": batch_rpm,
        "single_rpm": single_rpm,
        "batch_size": batch_size,
        "p2_target_minutes": p2_target_minutes,
        "p3_target_minutes": p3_target_minutes,
        "single_reserved_for_jit": jitter_single_reserved,
        "mode": mode,
        "stable_pool_cycle_minutes": stable_pool_cycle_minutes,
        "discovery_target_minutes": discovery_target_minutes,
        "custom_pool_share_pct": custom_pool_share_pct,
        "auto_switch_to_stable_on_idle_complete": auto_switch_to_stable_on_idle_complete,
        "use_proxy": use_proxy,
        "tracked_platforms": tracked or list(DEFAULT_TRACKED_PLATFORMS),
    }


def _normalize_platform(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if not raw:
        return None
    return PLATFORM_NAME_ALIASES.get(raw, raw)


def _write_state(payload: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[steamdt-openapi-price] write state failed | err=%s", exc)


def _read_quota_state() -> dict[str, Any]:
    raw = _read_json(QUOTA_STATE_PATH)
    if not isinstance(raw, dict):
        return {}
    return raw


def _write_quota_state(payload: dict[str, Any]) -> None:
    try:
        QUOTA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        QUOTA_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[steamdt-openapi-price] write quota state failed | err=%s", exc)


def _read_checkpoint_state() -> dict[str, Any]:
    raw = _read_json(CHECKPOINT_STATE_PATH)
    return raw if isinstance(raw, dict) else {}


def _write_checkpoint_state(payload: dict[str, Any]) -> None:
    try:
        CHECKPOINT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[steamdt-openapi-price] write checkpoint state failed | err=%s", exc)


def _checkpoint_key(item_id: int, platform: str) -> str:
    return f"{int(item_id)}:{str(platform).lower().strip()}"


def _parse_checkpoint_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return normalize_data_timestamp(str(value))
    except Exception:
        return None


def _checkpoint_times_for_item(state: dict[str, Any], item_id: int, tracked: set[str]) -> dict[str, datetime]:
    times: dict[str, datetime] = {}
    for platform in tracked:
        ts = _parse_checkpoint_time(state.get(_checkpoint_key(item_id, platform)))
        if ts is not None:
            times[str(platform).lower().strip()] = ts
    return times


def _merge_checkpoint_times(
    times: dict[str, datetime],
    state: dict[str, Any],
    item_id: int,
    tracked: set[str],
) -> dict[str, datetime]:
    merged = dict(times)
    for platform, checked_at in _checkpoint_times_for_item(state, item_id, tracked).items():
        current = merged.get(platform)
        if current is None or checked_at > normalize_data_timestamp(current):
            merged[platform] = checked_at
    return merged


def _mark_items_checked(item_ids: list[int], platforms: set[str], *, checked_at: datetime | None = None) -> None:
    if not item_ids or not platforms:
        return
    state = _read_checkpoint_state()
    ts = (checked_at or datetime.now()).isoformat(timespec="seconds")
    for item_id in item_ids:
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            continue
        if iid <= 0:
            continue
        for platform in platforms:
            normalized = str(platform).lower().strip()
            if normalized:
                state[_checkpoint_key(iid, normalized)] = ts
    _write_checkpoint_state(state)


def _acquire_quota(*, key: str, limit_per_minute: int) -> int:
    if limit_per_minute <= 0:
        return 0
    now_ts = int(datetime.now().timestamp())
    state = _read_quota_state()
    node = state.get(key) if isinstance(state.get(key), dict) else {}
    window_start = int(node.get("window_start") or now_ts)
    used = int(node.get("used") or 0)
    if now_ts - window_start >= 60:
        window_start = now_ts
        used = 0
    available = max(0, limit_per_minute - used)
    state[key] = {"window_start": window_start, "used": used}
    _write_quota_state(state)
    return available


def _consume_quota(*, key: str, consume: int) -> None:
    if consume <= 0:
        return
    now_ts = int(datetime.now().timestamp())
    state = _read_quota_state()
    node = state.get(key) if isinstance(state.get(key), dict) else {}
    window_start = int(node.get("window_start") or now_ts)
    used = int(node.get("used") or 0)
    if now_ts - window_start >= 60:
        window_start = now_ts
        used = 0
    used += int(consume)
    state[key] = {"window_start": window_start, "used": used}
    _write_quota_state(state)


def _platform_freshness(
    times: dict[str, datetime],
    tracked: set[str],
) -> tuple[int, datetime, datetime | None]:
    normalized = {
        str(platform).lower().strip(): normalize_data_timestamp(updated_at)
        for platform, updated_at in times.items()
        if str(platform).lower().strip() in tracked and updated_at
    }
    missing = len(tracked - set(normalized.keys()))
    if not normalized:
        oldest = datetime.fromtimestamp(0)
    else:
        oldest = min(normalized.values())
    newest = max(normalized.values()) if normalized else None
    return missing, oldest, newest


def _due_item_sort_key(item: dict[str, Any]) -> tuple[datetime, int, int]:
    return (
        item.get("oldest_updated_at") or item.get("last_updated_at") or datetime.fromtimestamp(0),
        -int(item.get("missing_platforms") or 0),
        int(item.get("item_id") or 0),
    )


def _select_due_items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    tracked = set(cfg["tracked_platforms"])
    now = datetime.now()
    p2_cutoff = now - timedelta(minutes=int(cfg["p2_target_minutes"]))
    p3_cutoff = now - timedelta(minutes=int(cfg["p3_target_minutes"]))
    checkpoint_state = _read_checkpoint_state()
    with SessionLocal() as db:
        items = (
            db.query(ItemBase.id, ItemBase.market_hash_name, ItemBase.crawl_priority)
            .filter(ItemBase.is_active.is_(True), ItemBase.crawl_priority.in_([2, 3]))
            .all()
        )
        if not items:
            return []
        item_ids = [int(row.id) for row in items]
        last_price_rows = (
            db.query(
                MarketPrice.item_id.label("item_id"),
                MarketPrice.platform_name.label("platform_name"),
                func.max(MarketPrice.updated_at).label("updated_at"),
            )
            .filter(
                MarketPrice.item_id.in_(item_ids),
                MarketPrice.platform_name.in_(list(tracked)),
            )
            .group_by(MarketPrice.item_id, MarketPrice.platform_name)
            .all()
        )

    platform_times_by_item: dict[int, dict[str, datetime]] = defaultdict(dict)
    for row in last_price_rows:
        if row.updated_at:
            platform_times_by_item[int(row.item_id)][str(row.platform_name).lower().strip()] = normalize_data_timestamp(row.updated_at)

    due: list[dict[str, Any]] = []
    for row in items:
        item_id = int(row.id)
        name = str(row.market_hash_name or "").strip()
        if not name:
            continue
        priority = int(row.crawl_priority or 0)
        cutoff = p3_cutoff if priority >= 3 else p2_cutoff
        times = _merge_checkpoint_times(platform_times_by_item.get(item_id, {}), checkpoint_state, item_id, tracked)
        missing, oldest, newest = _platform_freshness(times, tracked)
        stale = oldest < cutoff
        if stale:
            due.append(
                {
                    "item_id": item_id,
                    "market_hash_name": name,
                    "crawl_priority": priority,
                    "missing_platforms": missing,
                    "oldest_updated_at": oldest,
                    "newest_updated_at": newest,
                    "last_updated_at": oldest,
                }
            )

    due.sort(key=_due_item_sort_key)
    return due


def _select_discovery_items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    tracked = set(cfg["tracked_platforms"])
    now = datetime.now()
    cutoff = now - timedelta(minutes=int(cfg["discovery_target_minutes"]))
    checkpoint_state = _read_checkpoint_state()
    with SessionLocal() as db:
        items = (
            db.query(ItemBase.id, ItemBase.market_hash_name, ItemBase.crawl_priority)
            .filter(ItemBase.is_active.is_(True), ItemBase.crawl_priority.in_([0, 1]))
            .all()
        )
        if not items:
            return []
        item_ids = [int(row.id) for row in items]
        last_price_rows = (
            db.query(
                MarketPrice.item_id.label("item_id"),
                MarketPrice.platform_name.label("platform_name"),
                func.max(MarketPrice.updated_at).label("updated_at"),
            )
            .filter(
                MarketPrice.item_id.in_(item_ids),
                MarketPrice.platform_name.in_(list(tracked)),
            )
            .group_by(MarketPrice.item_id, MarketPrice.platform_name)
            .all()
        )
    platform_times_by_item: dict[int, dict[str, datetime]] = defaultdict(dict)
    for row in last_price_rows:
        if row.updated_at:
            platform_times_by_item[int(row.item_id)][str(row.platform_name).lower().strip()] = normalize_data_timestamp(row.updated_at)
    due: list[dict[str, Any]] = []
    for row in items:
        item_id = int(row.id)
        name = str(row.market_hash_name or "").strip()
        if not name:
            continue
        times = _merge_checkpoint_times(platform_times_by_item.get(item_id, {}), checkpoint_state, item_id, tracked)
        missing, oldest, newest = _platform_freshness(times, tracked)
        stale = oldest < cutoff
        if stale:
            due.append(
                {
                    "item_id": item_id,
                    "market_hash_name": name,
                    "crawl_priority": int(row.crawl_priority or 0),
                    "missing_platforms": missing,
                    "oldest_updated_at": oldest,
                    "newest_updated_at": newest,
                    "last_updated_at": oldest,
                }
            )
    due.sort(key=_due_item_sort_key)
    return due


def _auto_switch_mode_to_stable() -> None:
    try:
        cfg = _read_json(CONFIG_PATH)
        steamdt_cfg = cfg.get("steamdt") if isinstance(cfg.get("steamdt"), dict) else {}
        price_cfg = steamdt_cfg.get("openapi_price") if isinstance(steamdt_cfg.get("openapi_price"), dict) else {}
        if not price_cfg:
            return
        if str(price_cfg.get("mode") or "").strip().lower() == "stable":
            return
        price_cfg["mode"] = "stable"
        steamdt_cfg["openapi_price"] = price_cfg
        cfg["steamdt"] = steamdt_cfg
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[steamdt-openapi-price] mode auto switched | idle -> stable")
    except Exception as exc:
        logger.warning("[steamdt-openapi-price] mode auto switch failed | err=%s", exc)


def _split_mode_budgets(*, cfg: dict[str, Any], pool_due: int, discovery_due: int, batch_requests: int, single_requests: int) -> tuple[int, int]:
    capacity = max(0, int(batch_requests) * int(cfg["batch_size"]) + int(single_requests))
    if capacity <= 0:
        return 0, 0
    mode = str(cfg.get("mode") or DEFAULT_MODE).lower()
    if mode == "init":
        pool_items = capacity
    elif mode == "idle":
        pool_items = 0
    elif mode == "custom":
        pool_items = round(capacity * (float(cfg.get("custom_pool_share_pct", 70)) / 100.0))
    else:
        target_mins = max(10, int(cfg.get("stable_pool_cycle_minutes", 60)))
        pool_required_per_round = max(0, (pool_due + target_mins - 1) // target_mins)
        # Stable mode should only reserve the minimum needed to keep pool cycle SLA.
        # Remaining capacity is intentionally released to discovery.
        pool_items = min(capacity, pool_required_per_round)
    pool_items = max(0, min(pool_items, capacity))
    discovery_items = max(0, capacity - pool_items)
    if mode != "idle" and discovery_due <= 0:
        discovery_items = 0
        pool_items = capacity
    if mode == "idle" and pool_due > 0 and discovery_due <= 0:
        pool_items = capacity
        discovery_items = 0
    return pool_items, discovery_items


def _select_round_items(
    pool_due_items: list[dict[str, Any]],
    discovery_due_items: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    capacity: int,
) -> list[dict[str, Any]]:
    if capacity <= 0:
        return []
    selected = list(pool_due_items) + list(discovery_due_items)
    selected.sort(key=_due_item_sort_key)
    return selected[:capacity]


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int,
    use_proxy: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    proxies = get_request_proxies(force=True, platform="global") if use_proxy else None
    try:
        logger.info("[steamdt-openapi-price] request | method=%s url=%s %s", method, url, proxy_log_tag(proxies))
        if method == "GET":
            resp = requests.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies)
        else:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout, proxies=proxies)
        if resp.status_code != 200:
            reason = classify_request_failure(status_code=resp.status_code)
            mark_proxy_failure(proxies, reason=f"steamdt_openapi_price_{reason}", cooldown_seconds=proxy_cooldown_for_reason(reason))
            return None, f"http_{resp.status_code}"
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            code = data.get("errorCode")
            msg = data.get("errorMsg") or data.get("msg") or "business_error"
            mark_proxy_failure(proxies, reason="steamdt_openapi_price_business_error", cooldown_seconds=proxy_cooldown_for_reason("blocked", default=300))
            return None, f"business_{code}_{msg}"
        mark_proxy_success(proxies)
        return data if isinstance(data, dict) else None, None
    except Exception as exc:
        reason = classify_request_failure(exc)
        mark_proxy_failure(proxies, reason=f"steamdt_openapi_price_{reason}", cooldown_seconds=proxy_cooldown_for_reason(reason))
        return None, f"{reason}:{exc}"


def _parse_update_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        val = float(value)
        if val <= 0:
            return None
        if val > 10_000_000_000:
            val = val / 1000.0
        parsed = datetime.fromtimestamp(val)
        if parsed.year < 2020:
            return None
        return parsed
    except Exception:
        return None


def compute_orderbook_liquidity_score(
    *,
    sell_volume: int,
    buy_volume: int,
    reference_price: float,
    updated_at: datetime | None = None,
) -> tuple[int, float, float]:
    sell_v = max(0, int(sell_volume or 0))
    buy_v = max(0, int(buy_volume or 0))
    depth = sell_v + buy_v
    if depth <= 0 or reference_price <= 0:
        return depth, 0.0, 0.0
    balance = min(sell_v, buy_v) / max(sell_v, buy_v) if max(sell_v, buy_v) > 0 else 0.0
    if sell_v > 0 and buy_v == 0:
        balance = 0.25
    elif buy_v > 0 and sell_v == 0:
        balance = 0.15
    freshness = 1.0
    if updated_at is not None:
        age_minutes = max(0.0, (datetime.now() - normalize_data_timestamp(updated_at)).total_seconds() / 60.0)
        freshness = max(0.35, min(1.0, 1.0 - (age_minutes / 1440.0)))
    score = math.log(depth + 1.0) * balance * math.log(reference_price + 1.0) * freshness
    return depth, round(balance, 4), round(score, 4)


def cash_platform_bid_is_outlier(platform: str, buy_price: float, peer_bid_floor: float | None) -> bool:
    """Detect condition-limited cash platform bids using other cash platforms only."""

    platform_key = _normalize_platform(platform)
    if platform_key == "steam" or platform_key not in {"buff", "uuyp", "eco"}:
        return False
    buy = float(buy_price or 0)
    floor = float(peer_bid_floor or 0)
    if buy <= 0 or floor <= 0:
        return False
    return buy >= max(floor * 1.35, floor + 100.0)


def _cash_platform_bid_floor(rows: list[dict[str, Any]], exclude_platform: str | None = None) -> float:
    exclude = _normalize_platform(exclude_platform)
    bids: list[float] = []
    for row in rows:
        platform = _normalize_platform(row.get("platform"))
        if platform == "steam" or platform not in {"buff", "uuyp", "eco"} or platform == exclude:
            continue
        quote = normalize_openapi_quote_row(row)
        bid = quote["buy"]
        if bid > 0:
            bids.append(bid)
    return min(bids) if bids else 0.0


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def normalize_openapi_quote_row(row: dict[str, Any]) -> dict[str, Any]:
    """SteamDT can keep a price while the matching order count is zero."""

    sell_volume = _safe_int(row.get("sellCount"), 0, min_value=0)
    buy_volume = _safe_int(row.get("biddingCount"), 0, min_value=0)
    sell = _positive_float(row.get("sellPrice")) if sell_volume > 0 else 0.0
    buy = _positive_float(row.get("biddingPrice")) if buy_volume > 0 else 0.0
    return {
        "sell": sell,
        "buy": buy,
        "sell_volume": sell_volume,
        "buy_volume": buy_volume,
    }


def _save_openapi_prices(items: list[dict[str, Any]], tracked: set[str]) -> tuple[int, int]:
    if not items:
        return 0, 0
    saved = 0
    rows = 0
    touched_item_ids: set[int] = set()
    with SessionLocal() as db:
        for item in items:
            item_id = int(item.get("item_id") or 0)
            data_list = item.get("data_list") if isinstance(item.get("data_list"), list) else []
            if item_id <= 0:
                continue
            for row in data_list:
                platform = _normalize_platform(row.get("platform"))
                if platform not in tracked:
                    continue
                currency_raw = str(row.get("currency") or row.get("currencyType") or "CNY").upper().strip()
                currency = "CNY" if currency_raw in {"CNY", "RMB", "CNY¥", "¥"} else currency_raw
                if currency != "CNY":
                    continue
                quote = normalize_openapi_quote_row(row)
                sell = quote["sell"]
                buy = quote["buy"]
                peer_floor = _cash_platform_bid_floor(data_list, exclude_platform=platform)
                if cash_platform_bid_is_outlier(platform, buy, peer_floor):
                    logger.warning(
                        "[steamdt-openapi-price] dropped conditional bid outlier | platform=%s buy=%.4f peer_floor=%.4f",
                        platform,
                        buy,
                        peer_floor,
                    )
                    buy = 0.0
                if sell <= 0 and buy <= 0:
                    continue
                sell_volume = quote["sell_volume"]
                buy_volume = quote["buy_volume"]
                ts = _parse_update_time(row.get("updateTime"))
                if ts is None:
                    continue
                reference_price = sell if sell > 0 else buy
                orderbook_depth, orderbook_balance, liquidity_score = compute_orderbook_liquidity_score(
                    sell_volume=sell_volume,
                    buy_volume=buy_volume,
                    reference_price=reference_price,
                    updated_at=ts,
                )
                rows += 1
                changed = upsert_market_price_if_fresh(
                    db,
                    item_id=item_id,
                    platform_name=platform,
                    data_source="steamdt_openapi",
                    sell_min=sell if sell > 0 else 0.0,
                    sell_top5_avg=sell if sell > 0 else 0.0,
                    buy_max=buy if buy > 0 else 0.0,
                    buy_top5_avg=buy if buy > 0 else 0.0,
                    volume=0,
                    sell_volume=sell_volume,
                    buy_volume=buy_volume,
                    orderbook_depth=orderbook_depth,
                    orderbook_balance=orderbook_balance,
                    liquidity_score=liquidity_score,
                    liquidity_source="orderbook_depth",
                    currency=currency,
                    new_timestamp=ts,
                    log=logger,
                )
                if changed:
                    saved += 1
                touched_item_ids.add(item_id)
        db.commit()

    if touched_item_ids:
        try:
            from DataEngine.priority_scheduler import recalculate_priorities

            recalculate_priorities(item_ids=touched_item_ids)
        except Exception as exc:
            logger.warning("[steamdt-openapi-price] priority scheduler update failed | err=%s", exc)
        try:
            from DataEngine.radar_snapshot import refresh_radar_snapshots

            refresh_radar_snapshots(list(touched_item_ids))
        except Exception as exc:
            logger.warning("[steamdt-openapi-price] radar snapshot update failed | err=%s", exc)
    return rows, saved


def _parse_batch_payload(payload: dict[str, Any], item_id_by_name: dict[str, int]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("marketHashName") or "").strip()
        item_id = item_id_by_name.get(name)
        if not item_id:
            continue
        data_list = row.get("dataList")
        if not isinstance(data_list, list):
            continue
        out.append({"item_id": item_id, "data_list": data_list})
    return out


def refresh_selected_items(item_ids: list[int], platforms: set[str] | None = None, urgent: bool = False) -> dict[str, Any]:
    start = datetime.now()
    cfg = _load_config()
    if not cfg["api_key"]:
        return {"ok": False, "status": "missing_key", "reason": "missing_api_key", "rows": 0, "saved": 0, "platforms_by_item": {}}
    if not item_ids:
        return {"ok": True, "status": "no_data", "reason": "empty_item_ids", "rows": 0, "saved": 0, "platforms_by_item": {}}

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "AetherSwap/1.0 (+https://open.steamdt.com)",
    }
    batch_url = f"{cfg['base_url']}/open/cs2/v1/price/batch"
    single_url = f"{cfg['base_url']}/open/cs2/v1/price/single"
    tracked = set(platforms or cfg["tracked_platforms"] or DEFAULT_TRACKED_PLATFORMS)

    with SessionLocal() as db:
        rows = db.query(ItemBase.id, ItemBase.market_hash_name).filter(ItemBase.id.in_(item_ids), ItemBase.is_active.is_(True)).all()
    item_id_by_name = {str(r.market_hash_name): int(r.id) for r in rows if str(r.market_hash_name or "").strip()}
    names = list(item_id_by_name.keys())
    if not names:
        return {"ok": True, "status": "no_data", "reason": "no_valid_items", "rows": 0, "saved": 0, "platforms_by_item": {}}

    batch_budget = max(1, int(cfg["batch_rpm"])) if urgent else _acquire_quota(key="batch", limit_per_minute=max(0, int(cfg["batch_rpm"])))
    single_budget = max(0, int(cfg["single_rpm"])) if urgent else _acquire_quota(key="single", limit_per_minute=max(0, int(cfg["single_rpm"])))
    batch_size = int(cfg["batch_size"])

    rows_total = 0
    saved_total = 0
    batch_used = 0
    single_used = 0
    failed_reason = ""
    fetched_names: set[str] = set()
    checked_item_ids: set[int] = set()
    platforms_by_item: dict[int, set[str]] = defaultdict(set)

    for idx in range(max(0, batch_budget)):
        chunk = names[idx * batch_size : (idx + 1) * batch_size]
        if not chunk:
            break
        payload, err = _request_json(
            "POST",
            batch_url,
            headers=headers,
            payload={"marketHashNames": chunk},
            timeout=int(cfg["timeout_seconds"]),
            use_proxy=bool(cfg["use_proxy"]),
        )
        batch_used += 1
        if payload is None:
            failed_reason = err or "batch_failed"
            if "business_4005" in failed_reason:
                break
            continue
        checked_item_ids.update(
            int(item_id_by_name[name])
            for name in chunk
            if name in item_id_by_name and int(item_id_by_name[name]) > 0
        )
        parsed = _parse_batch_payload(payload, item_id_by_name)
        for node in parsed:
            item_id = int(node.get("item_id") or 0)
            for p in node.get("data_list") or []:
                pp = _normalize_platform(p.get("platform"))
                if pp in tracked:
                    platforms_by_item[item_id].add(pp)
        rows, saved = _save_openapi_prices(parsed, tracked)
        rows_total += rows
        saved_total += saved
        if parsed:
            parsed_item_ids = {int(node.get("item_id") or 0) for node in parsed}
            fetched_names.update(
                name
                for name, item_id in item_id_by_name.items()
                if int(item_id) in parsed_item_ids
            )

    remaining = [n for n in names if n not in fetched_names]
    for name in remaining[: max(0, single_budget)]:
        payload, err = _request_json(
            "GET",
            single_url,
            headers=headers,
            params={"marketHashName": name},
            timeout=int(cfg["timeout_seconds"]),
            use_proxy=bool(cfg["use_proxy"]),
        )
        single_used += 1
        if payload is None:
            failed_reason = err or failed_reason or "single_failed"
            if "business_4005" in failed_reason:
                break
            continue
        data_list = payload.get("data") if isinstance(payload.get("data"), list) else []
        item_id = item_id_by_name[name]
        for p in data_list:
            pp = _normalize_platform(p.get("platform"))
            if pp in tracked:
                platforms_by_item[item_id].add(pp)
        rows, saved = _save_openapi_prices([{"item_id": item_id, "data_list": data_list}], tracked)
        rows_total += rows
        saved_total += saved
        if item_id > 0:
            checked_item_ids.add(int(item_id))

    if not urgent:
        _consume_quota(key="batch", consume=batch_used)
        _consume_quota(key="single", consume=single_used)
    if checked_item_ids:
        _mark_items_checked(sorted(checked_item_ids), tracked)

    status = "ok" if saved_total > 0 else ("degraded" if rows_total > 0 else "no_data")
    return {
        "ok": status in {"ok", "degraded", "no_data"},
        "status": status,
        "reason": failed_reason,
        "rows": rows_total,
        "saved": saved_total,
        "batch_requests_used": batch_used,
        "single_requests_used": single_used,
        "cost_seconds": round((datetime.now() - start).total_seconds(), 2),
        "platforms_by_item": {str(k): sorted(v) for k, v in platforms_by_item.items()},
    }


def run_once() -> dict[str, Any]:
    start = datetime.now()
    cfg = _load_config()
    if not cfg["enabled"]:
        result = {"ok": False, "status": "disabled", "reason": "disabled", "updated_at": start.isoformat(timespec="seconds")}
        _write_state(result)
        logger.info("[steamdt-openapi-price] skipped | reason=disabled")
        return result
    if not cfg["api_key"]:
        result = {"ok": False, "status": "missing_key", "reason": "missing_api_key", "updated_at": start.isoformat(timespec="seconds")}
        _write_state(result)
        logger.warning("[steamdt-openapi-price] skipped | reason=missing_api_key")
        return result

    pool_due_items = _select_due_items(cfg)
    discovery_due_items = _select_discovery_items(cfg)
    if not pool_due_items and not discovery_due_items:
        result = {
            "ok": True,
            "status": "no_data",
            "reason": "no_due_items",
            "rows": 0,
            "saved": 0,
            "due_items": 0,
            "pool_due_items": 0,
            "discovery_due_items": 0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_state(result)
        logger.info("[steamdt-openapi-price] no due items")
        return result

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "AetherSwap/1.0 (+https://open.steamdt.com)",
    }
    batch_url = f"{cfg['base_url']}/open/cs2/v1/price/batch"
    single_url = f"{cfg['base_url']}/open/cs2/v1/price/single"

    batch_requests = _acquire_quota(key="batch", limit_per_minute=max(0, int(cfg["batch_rpm"])))
    single_requests = _acquire_quota(
        key="single",
        limit_per_minute=max(0, int(cfg["single_rpm"]) - int(cfg["single_reserved_for_jit"])),
    )
    batch_size = int(cfg["batch_size"])

    pool_due = len(pool_due_items)
    discovery_due = len(discovery_due_items)
    capacity = max(0, int(batch_requests) * batch_size + int(single_requests))
    pool_budget_items, discovery_budget_items = _split_mode_budgets(
        cfg=cfg,
        pool_due=pool_due,
        discovery_due=discovery_due,
        batch_requests=batch_requests,
        single_requests=single_requests,
    )
    selected_items = _select_round_items(
        pool_due_items,
        discovery_due_items,
        cfg=cfg,
        capacity=capacity,
    )
    selected_names = [str(row["market_hash_name"]) for row in selected_items]
    if cfg.get("mode") == "idle" and discovery_due <= 0 and bool(cfg.get("auto_switch_to_stable_on_idle_complete", True)):
        _auto_switch_mode_to_stable()

    name_to_id: dict[str, int] = {}
    for row in pool_due_items + discovery_due_items:
        name = str(row["market_hash_name"])
        if name and name not in name_to_id:
            name_to_id[name] = int(row["item_id"])
    item_id_by_name = name_to_id
    names = selected_names

    rows_total = 0
    saved_total = 0
    batch_used = 0
    single_used = 0
    failed_reason = ""
    fetched_names: set[str] = set()
    checked_item_ids: set[int] = set()

    tracked = set(cfg["tracked_platforms"])

    try:
        for idx in range(batch_requests):
            raise_if_stop_requested()
            chunk = names[idx * batch_size : (idx + 1) * batch_size]
            if not chunk:
                break
            payload, err = _request_json(
                "POST",
                batch_url,
                headers=headers,
                payload={"marketHashNames": chunk},
                timeout=int(cfg["timeout_seconds"]),
                use_proxy=bool(cfg["use_proxy"]),
            )
            batch_used += 1
            if payload is None:
                failed_reason = err or "batch_failed"
                if "business_4005" in failed_reason:
                    logger.warning("[steamdt-openapi-price] quota hit on batch | err=%s", failed_reason)
                    break
                logger.warning("[steamdt-openapi-price] batch failed | idx=%s err=%s", idx, failed_reason)
                continue
            parsed = _parse_batch_payload(payload, item_id_by_name)
            rows, saved = _save_openapi_prices(parsed, tracked)
            rows_total += rows
            saved_total += saved
            for n in chunk:
                fetched_names.add(n)
            checked_item_ids.update(
                int(item_id_by_name[n])
                for n in chunk
                if n in item_id_by_name and int(item_id_by_name[n]) > 0
            )

        remaining = [n for n in names if n not in fetched_names]
        for n in remaining[:single_requests]:
            raise_if_stop_requested()
            payload, err = _request_json(
                "GET",
                single_url,
                headers=headers,
                params={"marketHashName": n},
                timeout=int(cfg["timeout_seconds"]),
                use_proxy=bool(cfg["use_proxy"]),
            )
            single_used += 1
            if payload is None:
                failed_reason = err or failed_reason or "single_failed"
                if "business_4005" in failed_reason:
                    logger.warning("[steamdt-openapi-price] quota hit on single | err=%s", failed_reason)
                    break
                logger.warning("[steamdt-openapi-price] single failed | name=%s err=%s", n, failed_reason)
                continue
            data_list = payload.get("data") if isinstance(payload.get("data"), list) else []
            parsed = [{"item_id": item_id_by_name[n], "data_list": data_list}]
            rows, saved = _save_openapi_prices(parsed, tracked)
            rows_total += rows
            saved_total += saved
            fetched_names.add(n)
            if n in item_id_by_name and int(item_id_by_name[n]) > 0:
                checked_item_ids.add(int(item_id_by_name[n]))

        if checked_item_ids:
            _mark_items_checked(sorted(checked_item_ids), tracked)

        status = "ok" if saved_total > 0 else ("degraded" if rows_total > 0 else "no_data")
        result = {
            "ok": status in {"ok", "degraded", "no_data"},
            "status": status,
            "reason": failed_reason,
            "rows": rows_total,
            "saved": saved_total,
            "due_items": len(names),
            "pool_due_items": pool_due,
            "discovery_due_items": discovery_due,
            "pool_budget_items": pool_budget_items,
            "discovery_budget_items": discovery_budget_items,
            "mode": cfg.get("mode"),
            "batch_requests_used": batch_used,
            "single_requests_used": single_used,
            "batch_requests_available": batch_requests,
            "single_requests_available": single_requests,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "cost_seconds": round((datetime.now() - start).total_seconds(), 2),
        }
        _consume_quota(key="batch", consume=batch_used)
        _consume_quota(key="single", consume=single_used)
        _write_state(result)
        logger.info(
            "[steamdt-openapi-price] round done | mode=%s status=%s rows=%s saved=%s due=%s pool_due=%s discovery_due=%s batch=%s single=%s cost=%.2fs",
            cfg.get("mode"),
            status,
            rows_total,
            saved_total,
            len(names),
            pool_due,
            discovery_due,
            batch_used,
            single_used,
            result["cost_seconds"],
        )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "error",
            "reason": str(exc),
            "rows": rows_total,
            "saved": saved_total,
            "due_items": len(names),
            "batch_requests_used": batch_used,
            "single_requests_used": single_used,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_state(result)
        logger.exception("[steamdt-openapi-price] round failed")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="SteamDT OpenAPI mid-frequency price sync")
    parser.add_argument("--once", action="store_true", help="run one sync round")
    parser.parse_args()
    clear_stop()
    run_once()


if __name__ == "__main__":
    main()
