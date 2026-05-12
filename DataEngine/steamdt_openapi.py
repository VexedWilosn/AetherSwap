from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from sqlalchemy.orm import Session

from DataEngine.database import ItemBase, PlatformMapping, SessionLocal
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.proxy_pool import (
    classify_request_failure,
    get_request_proxies,
    mark_proxy_failure,
    mark_proxy_success,
    proxy_cooldown_for_reason,
    proxy_log_tag,
)
from DataEngine.stop_signal import raise_if_stop_requested

setup_dataengine_logging()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"
OPENAPI_SYNC_STATE_PATH = BASE_DIR / "config" / "steamdt_openapi_state.json"
RUNTIME_STATE_PATH = BASE_DIR / "config" / "platform_runtime_state.json"

DEFAULT_BASE_URL = "https://open.steamdt.com"
DEFAULT_BASE_PATH = "/open/cs2/v1/base"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RETRIES = 2
DEFAULT_SYNC_INTERVAL_SECONDS = 24 * 3600

TRACKED_PLATFORMS_DEFAULT = ("steam", "buff", "uuyp", "eco")
HOT_FIELD_BY_PLATFORM = {
    "buff": "buff_goods_id",
    "uuyp": "uuyp_template_id",
    "eco": "eco_goods_id",
}

PLATFORM_NAME_ALIASES = {
    "steam": "steam",
    "buff": "buff",
    "buff163": "buff",
    "youpin": "uuyp",
    "uuyp": "uuyp",
    "悠悠有品": "uuyp",
    "悠有品": "uuyp",
    "ecosteam": "eco",
    "eco": "eco",
    "c5": "c5",
    "c5game": "c5",
}

SessionFactory = Callable[[], Session]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("[steamdt-openapi] load json failed | path=%s err=%s", path, exc)
    return {}


def _load_openapi_config() -> dict[str, Any]:
    root_cfg = _read_json(CONFIG_PATH)
    steamdt_cfg = root_cfg.get("steamdt") if isinstance(root_cfg.get("steamdt"), dict) else {}
    openapi_cfg = steamdt_cfg.get("openapi") if isinstance(steamdt_cfg.get("openapi"), dict) else {}
    if not openapi_cfg and isinstance(steamdt_cfg.get("open_api"), dict):
        openapi_cfg = steamdt_cfg.get("open_api")

    credentials = _read_json(CREDENTIALS_PATH)
    cred_openapi = (
        credentials.get("steamdt_openapi")
        if isinstance(credentials.get("steamdt_openapi"), dict)
        else {}
    )
    if not cred_openapi and isinstance(credentials.get("steamdt"), dict):
        steamdt_cred = credentials.get("steamdt")
        if isinstance(steamdt_cred.get("openapi"), dict):
            cred_openapi = steamdt_cred.get("openapi")

    api_key = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_API_KEY"),
        openapi_cfg.get("api_key"),
        steamdt_cfg.get("openapi_api_key"),
        cred_openapi.get("api_key"),
        cred_openapi.get("token"),
        cred_openapi.get("authorization"),
    )

    enabled_default = bool(openapi_cfg.get("enabled", steamdt_cfg.get("openapi_enabled", bool(api_key))))
    enabled = _env_bool(os.getenv("STEAMDT_OPENAPI_ENABLED"), enabled_default)
    use_proxy_default = bool(openapi_cfg.get("use_proxy", steamdt_cfg.get("openapi_use_proxy", True)))
    use_proxy = _env_bool(os.getenv("STEAMDT_OPENAPI_USE_PROXY"), use_proxy_default)

    endpoint = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_ENDPOINT"),
        openapi_cfg.get("endpoint"),
        steamdt_cfg.get("openapi_endpoint"),
    )
    base_url = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_BASE_URL"),
        openapi_cfg.get("base_url"),
        steamdt_cfg.get("openapi_base_url"),
        DEFAULT_BASE_URL,
    )
    base_path = _first_non_empty(
        os.getenv("STEAMDT_OPENAPI_BASE_PATH"),
        openapi_cfg.get("base_path"),
        steamdt_cfg.get("openapi_base_path"),
        DEFAULT_BASE_PATH,
    )

    timeout = _safe_int(
        os.getenv(
            "STEAMDT_OPENAPI_TIMEOUT",
            openapi_cfg.get("timeout_seconds", steamdt_cfg.get("openapi_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        ),
        DEFAULT_TIMEOUT_SECONDS,
    )
    timeout = max(5, timeout)

    max_retries = _safe_int(
        os.getenv("STEAMDT_OPENAPI_MAX_RETRIES", openapi_cfg.get("max_retries", DEFAULT_MAX_RETRIES)),
        DEFAULT_MAX_RETRIES,
    )
    max_retries = max(1, max_retries)

    interval_seconds = _safe_int(
        os.getenv(
            "STEAMDT_OPENAPI_SYNC_INTERVAL_SECONDS",
            openapi_cfg.get(
                "sync_interval_seconds",
                steamdt_cfg.get("openapi_sync_interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS),
            ),
        ),
        DEFAULT_SYNC_INTERVAL_SECONDS,
    )
    interval_seconds = max(3600, interval_seconds)

    tracked_platforms = openapi_cfg.get("tracked_platforms", TRACKED_PLATFORMS_DEFAULT)
    if not isinstance(tracked_platforms, list) or not tracked_platforms:
        tracked_platforms = list(TRACKED_PLATFORMS_DEFAULT)
    tracked_platforms = [str(name).strip().lower() for name in tracked_platforms if str(name).strip()]
    if not tracked_platforms:
        tracked_platforms = list(TRACKED_PLATFORMS_DEFAULT)

    return {
        "enabled": enabled,
        "api_key": api_key,
        "endpoint": endpoint,
        "base_url": base_url,
        "base_path": base_path,
        "timeout_seconds": timeout,
        "max_retries": max_retries,
        "sync_interval_seconds": interval_seconds,
        "use_proxy": use_proxy,
        "tracked_platforms": tracked_platforms,
    }


def _resolve_endpoint(cfg: dict[str, Any]) -> str:
    endpoint = str(cfg.get("endpoint") or "").strip()
    if endpoint:
        return endpoint
    base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    base_path = str(cfg.get("base_path") or DEFAULT_BASE_PATH).strip()
    if not base_path.startswith("/"):
        base_path = f"/{base_path}"
    return f"{base_url}{base_path}"


def _load_sync_state() -> dict[str, Any]:
    return _read_json(OPENAPI_SYNC_STATE_PATH)


def _save_sync_state(payload: dict[str, Any]) -> None:
    try:
        OPENAPI_SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPENAPI_SYNC_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[steamdt-openapi] save sync state failed | err=%s", exc)


def _write_runtime_state(*, status: str, rows: int = 0, saved: int = 0, reason: str = "", cost_seconds: float | None = None) -> None:
    try:
        payload = _read_json(RUNTIME_STATE_PATH)
        if not isinstance(payload, dict):
            payload = {}
        node = payload.get("steamdt_openapi") if isinstance(payload.get("steamdt_openapi"), dict) else {}
        node = dict(node or {})
        node["platform"] = "steamdt_openapi"
        node["status"] = status
        node["rows"] = int(rows)
        node["saved"] = int(saved)
        node["reason"] = str(reason or "")
        node["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if cost_seconds is not None:
            node["cost_seconds"] = float(cost_seconds)
        payload["steamdt_openapi"] = node
        RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[steamdt-openapi] write runtime state failed | err=%s", exc)


def normalize_market_hash_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_platform_name(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    direct = PLATFORM_NAME_ALIASES.get(raw)
    if direct:
        return direct
    key = raw.lower().replace(" ", "").replace("-", "").replace("_", "")
    return PLATFORM_NAME_ALIASES.get(key)


def extract_platform_ids_from_base_row(row: dict[str, Any]) -> dict[str, str]:
    platform_list = row.get("platformList") or row.get("platform_list") or row.get("platforms") or []
    if not isinstance(platform_list, list):
        return {}
    result: dict[str, str] = {}
    for platform_row in platform_list:
        if not isinstance(platform_row, dict):
            continue
        platform_name = normalize_platform_name(
            platform_row.get("name")
            or platform_row.get("platformName")
            or platform_row.get("platform")
            or platform_row.get("platformEnum")
            or platform_row.get("source")
        )
        if not platform_name:
            continue
        item_id = _first_non_empty(
            platform_row.get("itemId"),
            platform_row.get("item_id"),
            platform_row.get("platformItemId"),
            platform_row.get("platform_item_id"),
            platform_row.get("templateId"),
            platform_row.get("template_id"),
            platform_row.get("goodsId"),
            platform_row.get("goods_id"),
            platform_row.get("id"),
        )
        if not item_id or item_id == "-1":
            continue
        if platform_name not in result:
            result[platform_name] = item_id
    return result


def extract_base_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if data is None:
        data = payload.get("Data", payload)

    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _request_openapi_base_rows(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    endpoint = _resolve_endpoint(cfg)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
        "User-Agent": "AetherSwap/1.0 (+https://open.steamdt.com)",
    }
    max_retries = int(cfg.get("max_retries", DEFAULT_MAX_RETRIES))
    timeout = int(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))

    for attempt in range(1, max_retries + 1):
        raise_if_stop_requested()
        proxies = get_request_proxies(force=True, platform="global") if cfg.get("use_proxy") else None
        try:
            logger.info(
                "[steamdt-openapi] request base rows | attempt=%s/%s %s endpoint=%s",
                attempt,
                max_retries,
                proxy_log_tag(proxies),
                endpoint,
            )
            response = requests.get(endpoint, headers=headers, timeout=timeout, proxies=proxies)
            if response.status_code != 200:
                reason = classify_request_failure(status_code=response.status_code)
                mark_proxy_failure(
                    proxies,
                    reason=f"steamdt_openapi_{reason}",
                    cooldown_seconds=proxy_cooldown_for_reason(reason),
                )
                logger.warning(
                    "[steamdt-openapi] non-200 response | status=%s reason=%s text=%s",
                    response.status_code,
                    reason,
                    str(getattr(response, "text", "") or "")[:240],
                )
                continue

            payload = response.json()
            if isinstance(payload, dict) and payload.get("success") is False:
                code = payload.get("code")
                msg = payload.get("msg") or payload.get("message") or payload.get("errorMsg") or ""
                mark_proxy_failure(
                    proxies,
                    reason="steamdt_openapi_business_error",
                    cooldown_seconds=proxy_cooldown_for_reason("blocked", default=300),
                )
                logger.warning("[steamdt-openapi] business error | code=%s msg=%s", code, msg)
                return [], False

            rows = extract_base_rows(payload)
            mark_proxy_success(proxies)
            logger.info("[steamdt-openapi] base rows loaded | rows=%s", len(rows))
            return rows, True
        except Exception as exc:
            reason = classify_request_failure(exc)
            mark_proxy_failure(
                proxies,
                reason=f"steamdt_openapi_{reason}",
                cooldown_seconds=proxy_cooldown_for_reason(reason),
            )
            logger.warning(
                "[steamdt-openapi] request failed | attempt=%s/%s reason=%s err=%s",
                attempt,
                max_retries,
                reason,
                exc,
            )
            continue

    return [], False


def _parse_int_id(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def sync_base_rows(
    rows: list[dict[str, Any]],
    *,
    session_factory: SessionFactory = SessionLocal,
    tracked_platforms: set[str] | None = None,
) -> dict[str, int]:
    tracked = tracked_platforms or set(TRACKED_PLATFORMS_DEFAULT)
    tracked = {str(name).strip().lower() for name in tracked if str(name).strip()}
    if not tracked:
        tracked = set(TRACKED_PLATFORMS_DEFAULT)

    stats = {
        "rows_total": len(rows),
        "rows_matched_item": 0,
        "rows_missing_item": 0,
        "rows_without_platform": 0,
        "mapping_created": 0,
        "mapping_updated": 0,
        "hot_field_updated": 0,
    }
    if not rows:
        return stats

    db = session_factory()
    try:
        items = db.query(ItemBase).all()
        item_by_name = {
            normalize_market_hash_name(item.market_hash_name): item
            for item in items
            if item.market_hash_name
        }
        mapping_rows = (
            db.query(PlatformMapping)
            .filter(PlatformMapping.platform_name.in_(list(tracked)))
            .all()
        )
        mapping_index: dict[tuple[int, str], PlatformMapping] = {}
        for mapping in mapping_rows:
            platform_name = str(mapping.platform_name or "").strip().lower()
            if not platform_name:
                continue
            mapping_index[(int(mapping.item_id), platform_name)] = mapping

        for idx, row in enumerate(rows, start=1):
            if idx % 500 == 0:
                raise_if_stop_requested()
            market_hash_name = _first_non_empty(
                row.get("marketHashName"),
                row.get("market_hash_name"),
                row.get("hashName"),
                row.get("hash_name"),
                row.get("name"),
            )
            normalized_name = normalize_market_hash_name(market_hash_name)
            item = item_by_name.get(normalized_name)
            if item is None:
                stats["rows_missing_item"] += 1
                continue
            stats["rows_matched_item"] += 1

            platform_ids = extract_platform_ids_from_base_row(row)
            if not platform_ids:
                stats["rows_without_platform"] += 1
                continue

            for platform_name, platform_item_id in platform_ids.items():
                if platform_name not in tracked:
                    continue
                key = (int(item.id), platform_name)
                mapping = mapping_index.get(key)
                if mapping is None:
                    mapping = PlatformMapping(
                        item_id=int(item.id),
                        platform_name=platform_name,
                        platform_item_id=str(platform_item_id),
                    )
                    db.add(mapping)
                    mapping_index[key] = mapping
                    stats["mapping_created"] += 1
                elif str(mapping.platform_item_id) != str(platform_item_id):
                    mapping.platform_item_id = str(platform_item_id)
                    stats["mapping_updated"] += 1

                hot_field = HOT_FIELD_BY_PLATFORM.get(platform_name)
                hot_value = _parse_int_id(platform_item_id)
                if not hot_field or hot_value is None:
                    continue
                if getattr(item, hot_field, None) != hot_value:
                    setattr(item, hot_field, hot_value)
                    stats["hot_field_updated"] += 1

        db.commit()
        return stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sync_steamdt_openapi_base(
    *,
    force: bool = False,
    session_factory: SessionFactory = SessionLocal,
) -> dict[str, Any]:
    start = datetime.now()
    cfg = _load_openapi_config()
    if not cfg.get("enabled"):
        logger.info("[steamdt-openapi] sync skipped | reason=disabled")
        _write_runtime_state(status="disabled", reason="disabled")
        return {"ok": False, "skipped": "disabled"}
    if not cfg.get("api_key"):
        logger.warning("[steamdt-openapi] sync skipped | reason=missing_api_key")
        _write_runtime_state(status="missing_key", reason="missing_api_key")
        return {"ok": False, "skipped": "missing_api_key"}

    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_sync_state()
    if not force and str(state.get("last_success_date") or "") == today:
        logger.info("[steamdt-openapi] sync skipped | reason=already_synced_today date=%s", today)
        _write_runtime_state(status="idle", reason="already_synced_today")
        return {"ok": True, "skipped": "already_synced_today"}

    rows, success = _request_openapi_base_rows(cfg)
    if not success:
        _write_runtime_state(status="error", reason="request_failed")
        return {"ok": False, "skipped": "request_failed"}

    tracked = set(cfg.get("tracked_platforms") or TRACKED_PLATFORMS_DEFAULT)
    stats = sync_base_rows(rows, session_factory=session_factory, tracked_platforms=tracked)
    payload = {
        "last_success_date": today,
        "last_success_at": datetime.now().isoformat(timespec="seconds"),
        "last_row_count": int(stats.get("rows_total", 0)),
        "last_mapping_created": int(stats.get("mapping_created", 0)),
        "last_mapping_updated": int(stats.get("mapping_updated", 0)),
        "last_hot_field_updated": int(stats.get("hot_field_updated", 0)),
        "sync_interval_seconds": int(cfg.get("sync_interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS)),
    }
    _save_sync_state(payload)
    cost = round((datetime.now() - start).total_seconds(), 2)
    saved = int(stats.get("mapping_created", 0)) + int(stats.get("mapping_updated", 0)) + int(stats.get("hot_field_updated", 0))
    _write_runtime_state(status="ok", rows=int(stats.get("rows_total", 0)), saved=saved, cost_seconds=cost)
    logger.info("[steamdt-openapi] sync done | stats=%s", stats)
    return {"ok": True, "stats": stats}


def run_once(*, force: bool = False) -> int:
    result = sync_steamdt_openapi_base(force=force)
    stats = result.get("stats") if isinstance(result, dict) else {}
    if not isinstance(stats, dict):
        return 0
    return int(stats.get("mapping_created", 0)) + int(stats.get("mapping_updated", 0)) + int(
        stats.get("hot_field_updated", 0)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SteamDT OpenAPI base mapping sync.")
    parser.add_argument("--force", action="store_true", help="Ignore daily sync state and force one request.")
    args = parser.parse_args()
    run_once(force=bool(args.force))
