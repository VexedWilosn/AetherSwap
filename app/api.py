"""
FastAPI application entry point.
All routes are registered via `app.routes`, and all background
workers are started from `app.services.workers`.  This file is
intentionally kept minimal.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from DataEngine.database import ArbitrageOpportunity, ItemBase, MarketPrice, PlatformMapping, RadarSnapshot, SessionLocal, SteamDTOpportunity
from DataEngine.cn_name_mapper import search_steam_cn_names
from DataEngine.radar_snapshot import refresh_radar_snapshots
from DataEngine.profit_model import cash_to_steam_profit, opportunity_profit, steam_balance_cost_ratio, steam_to_cash_profit
from DataEngine.stop_signal import clear_stop as clear_engine_stop, request_stop as request_engine_stop
from app.database import init_db, migrate_from_json
from app.state import get_inventory, log, request_stop, set_inventory
from app.database import PlatformAction, TradeExecutionRecord, get_session
from app.services.browser_auth import start_login_browser, finish_login_and_extract, finish_login_and_extract_capsule
from app.services.steam_buyer import SteamBuyer, build_steam_market_url
from app.services.notifier import notify_trade_success
from app.services.platform_sessions import resolve_buff_pay_method
from app.services.session_capsule_pool import SessionCapsulePool
from app.services.task_queue import get_task_queue
from app.services.trading.actions import create_platform_action, transition_action
from app.services.trading.capabilities import CAPABILITY_REGISTRY, normalize_platform
from app.services.trading.adapters import RESULT_NOT_FOUND, RESULT_ORDER_COMPLETED, RESULT_ORDER_PENDING, RESULT_TRADE_OFFER_ACCEPTED
from app.services.trading.exposure_guard import LowPriceExposureGuard
from app.services.trading.platform_adapters import PlatformClientAdapter, build_platform_adapters
from app.services.trading.canary import (
    LiveCanarySmokeRegistry,
    inspect_live_canary_run,
    live_canary_config_from_app_config,
    raw_context_with_test_signal,
    validate_live_canary_run,
)
from app.services.trading.purchase_targets import create_purchase_target_actions
from app.services.trading.risk_budget import RiskBudgetService
from app.services.trading.reconciliation import PlatformActionReconciliationService
from app.services.trading.inventory_alignment import InventoryAlignmentService
from app.services.trading.runtime import (
    PlatformActionWorkerRuntime,
    platform_action_worker_config_from_app_config,
)
from app.services.trading.settlement import materialize_purchase_for_action
from app.services.trading.sell_actions import SellerActionService, is_sell_side_action_type
from app.services.trading.sell_scanner import (
    SellerSnapshotScanner,
    SellerSnapshotScannerRuntime,
    seller_snapshot_scanner_config_from_app_config,
)
from app.services.trading.smoke import PlatformAutomationSmokeService
from app.services.trading.states import CLAIMABLE_STATES, TERMINAL_STATES, PlatformActionState, PlatformActionType
from app.services.trading.trade_offers import TradeOfferService
from app.services.trading.worker import PlatformActionWorker
from DataEngine.steamdt_fetcher import register_steamdt_capsule_from_cookie
from config import load_app_config, save_app_config

init_db()


def radar_mode_columns(opportunity_mode: str):
    mode = (opportunity_mode or "best").lower().strip()
    if mode == "cash_to_steam":
        return RadarSnapshot.cash_to_steam_profit_rate, RadarSnapshot.cash_to_steam_price
    if mode == "steam_to_cash":
        return RadarSnapshot.steam_to_cash_profit_rate, RadarSnapshot.steam_to_cash_price
    return RadarSnapshot.best_profit_rate, RadarSnapshot.best_platform_price


def radar_cashout_payload_from_platforms(
    row: RadarSnapshot,
    payload: dict | None = None,
    cashout_price_mode: str = "bid",
    steam_balance_cost_ratio_override: float | None = None,
) -> dict:
    mode = (cashout_price_mode or "bid").lower().strip()
    if mode not in {"bid", "listing"}:
        mode = "bid"
    balance_ratio = float(steam_balance_cost_ratio_override or row.steam_balance_cost_ratio or 0.70)
    if mode == "bid":
        platform = getattr(row, "steam_to_cash_platform", None) or row.best_platform
        price = float(getattr(row, "steam_to_cash_price", 0) or row.best_platform_buy_max or 0)
        if steam_balance_cost_ratio_override is not None:
            steam_sell = float(row.steam_sell_min or 0)
            if steam_sell > 0 and price > 0:
                math = steam_to_cash_profit(steam_sell, price, platform or "", balance_ratio)
                profit_rate = round(math.profit_rate * 100.0, 2)
                profit_cny = round(math.profit_cny, 4)
            else:
                profit_rate = 0.0
                profit_cny = 0.0
        else:
            profit_rate = float(getattr(row, "steam_to_cash_profit_rate", 0) or 0)
            profit_cny = float(getattr(row, "steam_to_cash_profit_cny", 0) or 0)
        return {
            "steam_to_cash_platform": platform,
            "steam_to_cash_price": price,
            "steam_to_cash_profit_rate": profit_rate,
            "steam_to_cash_profit_cny": profit_cny,
            "steam_to_cash_price_mode": "bid",
        }

    payload = payload if isinstance(payload, dict) else {}
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
    steam_payload = payload.get("steam") if isinstance(payload.get("steam"), dict) else {}
    steam_sell = float(steam_payload.get("sell_min") or row.steam_sell_min or 0)
    candidates: list[tuple[str, float, object]] = []
    for platform, price in platforms.items():
        if not isinstance(price, dict):
            continue
        if bool(price.get("ignored_for_profit")):
            continue
        sell_min = _safe_float(price.get("sell_min"))
        if sell_min <= 0 or steam_sell <= 0:
            continue
        platform_key = str(platform or "").lower().strip()
        math = steam_to_cash_profit(steam_sell, sell_min, platform_key, balance_ratio)
        candidates.append((platform_key, sell_min, math))
    if not candidates:
        return {
            "steam_to_cash_platform": None,
            "steam_to_cash_price": 0.0,
            "steam_to_cash_profit_rate": 0.0,
            "steam_to_cash_profit_cny": 0.0,
            "steam_to_cash_price_mode": "listing",
        }
    platform, price, math = min(candidates, key=lambda item: item[1])
    return {
        "steam_to_cash_platform": platform,
        "steam_to_cash_price": price,
        "steam_to_cash_profit_rate": round(math.profit_rate * 100.0, 2),
        "steam_to_cash_profit_cny": round(math.profit_cny, 4),
        "steam_to_cash_price_mode": "listing",
    }


def _platform_price_outlier(platforms: dict, platform: str, field: str) -> bool:
    platform_key = str(platform or "").lower().strip()
    values: list[tuple[str, float]] = []
    for peer_platform, price in (platforms or {}).items():
        if not isinstance(price, dict):
            continue
        peer_key = str(peer_platform or "").lower().strip()
        value = _safe_float(price.get(field))
        if peer_key and value > 0:
            values.append((peer_key, value))
    current = next((value for key, value in values if key == platform_key), 0.0)
    peers = [value for key, value in values if key != platform_key]
    if current <= 0 or not peers:
        return False
    peer_floor = min(peers)
    return current >= max(peer_floor * 5.0, peer_floor + 50.0)


def radar_method_payload_from_platforms(
    row: RadarSnapshot,
    payload: dict | None = None,
    buy_price_mode: str = "direct",
    sell_price_mode: str = "bid",
    steam_balance_cost_ratio_override: float | None = None,
) -> dict:
    buy_mode = (buy_price_mode or "direct").lower().strip()
    if buy_mode not in {"direct", "order"}:
        buy_mode = "direct"
    sell_mode = (sell_price_mode or "bid").lower().strip()
    if sell_mode not in {"bid", "listing"}:
        sell_mode = "bid"
    balance_ratio = float(steam_balance_cost_ratio_override or row.steam_balance_cost_ratio or 0.70)
    payload = payload if isinstance(payload, dict) else {}
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
    steam_payload = payload.get("steam") if isinstance(payload.get("steam"), dict) else {}
    steam_listing = float(steam_payload.get("sell_min") or row.steam_sell_min or 0)
    steam_bid = float(steam_payload.get("buy_max") or row.steam_buy_max or 0)
    steam_buy_price = steam_bid if buy_mode == "order" else steam_listing
    steam_sell_price = steam_listing

    best_cash_math = None
    best_cash_platform = None
    best_cash_price = 0.0
    best_steam_math = None
    best_steam_platform = None
    best_steam_price = 0.0
    ignored_outliers: list[str] = []

    for platform, price in platforms.items():
        if not isinstance(price, dict) or bool(price.get("ignored_for_profit")):
            continue
        platform_key = str(platform or "").lower().strip()
        if not platform_key:
            continue
        cash_buy_field = "buy_max" if buy_mode == "order" else "sell_min"
        cash_sell_field = "buy_max" if sell_mode == "bid" else "sell_min"
        cash_buy_price = _safe_float(price.get(cash_buy_field))
        cash_sell_price = _safe_float(price.get(cash_sell_field))
        if _platform_price_outlier(platforms, platform_key, cash_buy_field):
            cash_buy_price = 0.0
            ignored_outliers.append(f"{platform_key}_{cash_buy_field}")
        if _platform_price_outlier(platforms, platform_key, cash_sell_field):
            cash_sell_price = 0.0
            ignored_outliers.append(f"{platform_key}_{cash_sell_field}")
        if cash_buy_price > 0 and steam_sell_price > 0:
            math = cash_to_steam_profit(cash_buy_price, steam_sell_price)
            if best_cash_math is None or math.profit_rate > best_cash_math.profit_rate:
                best_cash_math = math
                best_cash_platform = platform_key
                best_cash_price = cash_buy_price
        if cash_sell_price > 0 and steam_buy_price > 0:
            math = steam_to_cash_profit(steam_buy_price, cash_sell_price, platform_key, balance_ratio)
            if best_steam_math is None or (
                math.profit_rate > best_steam_math.profit_rate if sell_mode == "bid" else cash_sell_price < best_steam_price
            ):
                best_steam_math = math
                best_steam_platform = platform_key
                best_steam_price = cash_sell_price

    cash_rate = round(best_cash_math.profit_rate * 100.0, 2) if best_cash_math else 0.0
    steam_rate = round(best_steam_math.profit_rate * 100.0, 2) if best_steam_math else 0.0
    use_steam = steam_rate > cash_rate
    return {
        "cash_to_steam_platform": best_cash_platform,
        "cash_to_steam_price": best_cash_price,
        "cash_to_steam_profit_rate": cash_rate,
        "cash_to_steam_profit_cny": round(best_cash_math.profit_cny, 4) if best_cash_math else 0.0,
        "steam_to_cash_platform": best_steam_platform,
        "steam_to_cash_price": best_steam_price,
        "steam_to_cash_profit_rate": steam_rate,
        "steam_to_cash_profit_cny": round(best_steam_math.profit_cny, 4) if best_steam_math else 0.0,
        "steam_to_cash_price_mode": sell_mode,
        "mode_platform": best_steam_platform if use_steam else best_cash_platform,
        "mode_price": best_steam_price if use_steam else best_cash_price,
        "mode_profit_rate": steam_rate if use_steam else cash_rate,
        "mode_profit_cny": round(best_steam_math.profit_cny, 4) if use_steam and best_steam_math else (round(best_cash_math.profit_cny, 4) if best_cash_math else 0.0),
        "mode_direction": "steam_to_cash" if use_steam else "cash_to_steam",
        "ignored_outliers": sorted(set(ignored_outliers)),
    }


def radar_row_mode_payload(
    row: RadarSnapshot,
    opportunity_mode: str = "best",
    payload: dict | None = None,
    cashout_price_mode: str = "bid",
    buy_price_mode: str = "direct",
    steam_balance_cost_ratio_override: float | None = None,
) -> dict:
    buy_mode = (buy_price_mode or "direct").lower().strip()
    payload_platforms = payload.get("platforms") if isinstance(payload, dict) else None
    if isinstance(payload_platforms, dict) and payload_platforms:
        return radar_method_payload_from_platforms(row, payload, buy_mode, cashout_price_mode, steam_balance_cost_ratio_override)
    cash_to_steam_platform = getattr(row, "cash_to_steam_platform", None) or row.best_platform
    cash_to_steam_price = float(getattr(row, "cash_to_steam_price", 0) or row.best_platform_price or 0)
    cashout_payload = radar_cashout_payload_from_platforms(row, payload, cashout_price_mode, steam_balance_cost_ratio_override)
    steam_to_cash_platform = cashout_payload["steam_to_cash_platform"] or row.best_platform
    steam_to_cash_price = float(cashout_payload["steam_to_cash_price"] or 0)
    steam_to_cash_rate = float(cashout_payload["steam_to_cash_profit_rate"] or 0)
    steam_to_cash_cny = float(cashout_payload["steam_to_cash_profit_cny"] or 0)
    mode = (opportunity_mode or "best").lower().strip()
    if mode == "cash_to_steam":
        mode_platform = cash_to_steam_platform
        mode_price = cash_to_steam_price
        mode_profit_rate = float(getattr(row, "cash_to_steam_profit_rate", 0) or 0)
        mode_profit_cny = float(getattr(row, "cash_to_steam_profit_cny", 0) or 0)
        mode_direction = "cash_to_steam"
    elif mode == "steam_to_cash":
        mode_platform = steam_to_cash_platform
        mode_price = steam_to_cash_price
        mode_profit_rate = steam_to_cash_rate
        mode_profit_cny = steam_to_cash_cny
        mode_direction = "steam_to_cash"
    elif steam_to_cash_rate > float(getattr(row, "cash_to_steam_profit_rate", 0) or 0):
        mode_platform = steam_to_cash_platform
        mode_price = steam_to_cash_price
        mode_profit_rate = steam_to_cash_rate
        mode_profit_cny = steam_to_cash_cny
        mode_direction = "steam_to_cash"
    else:
        mode_platform = cash_to_steam_platform
        mode_price = cash_to_steam_price
        mode_profit_rate = float(getattr(row, "cash_to_steam_profit_rate", row.best_profit_rate) or 0)
        mode_profit_cny = float(getattr(row, "cash_to_steam_profit_cny", row.best_profit_cny) or 0)
        mode_direction = "cash_to_steam"
    return {
        "cash_to_steam_platform": cash_to_steam_platform,
        "cash_to_steam_price": cash_to_steam_price,
        "cash_to_steam_profit_rate": float(getattr(row, "cash_to_steam_profit_rate", 0) or 0),
        "cash_to_steam_profit_cny": float(getattr(row, "cash_to_steam_profit_cny", 0) or 0),
        "steam_to_cash_platform": steam_to_cash_platform,
        "steam_to_cash_price": steam_to_cash_price,
        "steam_to_cash_profit_rate": steam_to_cash_rate,
        "steam_to_cash_profit_cny": steam_to_cash_cny,
        "steam_to_cash_price_mode": cashout_payload["steam_to_cash_price_mode"],
        "mode_platform": mode_platform,
        "mode_price": mode_price,
        "mode_profit_rate": mode_profit_rate,
        "mode_profit_cny": mode_profit_cny,
        "mode_direction": mode_direction,
    }


def radar_snapshot_has_conditional_bid_outlier(row: RadarSnapshot) -> bool:
    """Detect snapshots polluted by condition-limited cash platform bids."""

    return radar_payload_has_conditional_bid_outlier(row.platform_payload_json)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def radar_payload_has_conditional_bid_outlier(payload_or_json) -> bool:
    """Detect condition-limited cash bids from an already loaded platform payload."""

    try:
        payload = json.loads(payload_or_json or "{}") if not isinstance(payload_or_json, dict) else payload_or_json
    except Exception:
        return False
    platforms = payload.get("platforms") if isinstance(payload, dict) else {}
    if not isinstance(platforms, dict):
        return False
    cash_bids: list[tuple[str, float]] = []
    for platform, price in platforms.items():
        if not isinstance(price, dict) or str(platform).lower() == "steam":
            continue
        buy = _safe_float(price.get("buy_max"))
        if buy > 0:
            cash_bids.append((str(platform).lower(), buy))
    for platform, buy in cash_bids:
        peers = [peer_buy for peer_platform, peer_buy in cash_bids if peer_platform != platform]
        if not peers:
            continue
        peer_floor = min(peers)
        if buy >= max(peer_floor * 3.0, peer_floor + 50.0):
            return True
    return False


def radar_payload_has_baseline_profit(payload_or_json, platform: str | None = None) -> bool:
    """Detect legacy baseline prices that should not drive radar profit."""

    try:
        payload = json.loads(payload_or_json or "{}") if not isinstance(payload_or_json, dict) else payload_or_json
    except Exception:
        return False
    platforms = payload.get("platforms") if isinstance(payload, dict) else {}
    if not isinstance(platforms, dict):
        return False
    target = str(platform or "").lower().strip()
    for platform_name, price in platforms.items():
        if target and str(platform_name).lower().strip() != target:
            continue
        if not isinstance(price, dict):
            continue
        data_source = str(price.get("data_source") or "").lower().strip()
        if data_source != "baseline":
            continue
        if _safe_float(price.get("sell_min")) > 0 or _safe_float(price.get("buy_max")) > 0:
            return True
    return False


def radar_snapshot_has_baseline_profit(row: RadarSnapshot) -> bool:
    if float(getattr(row, "cash_to_steam_profit_rate", 0) or 0) < 300:
        return False
    return radar_payload_has_baseline_profit(row.platform_payload_json, getattr(row, "cash_to_steam_platform", None))


def radar_snapshot_warning_flags(row: RadarSnapshot, payload: dict | None = None) -> list[str]:
    flags: list[str] = []
    payload = payload if isinstance(payload, dict) else {}
    steam_payload = payload.get("steam") if isinstance(payload.get("steam"), dict) else {}
    steam_bid = float(steam_payload.get("buy_max") or row.steam_buy_max or 0)
    steam_sell = float(steam_payload.get("sell_min") or row.steam_sell_min or 0)
    snapshot_updated_at = getattr(row, "snapshot_updated_at", None)
    if steam_bid <= 0:
        flags.append("steam_bid_missing")
    if steam_sell <= 0:
        flags.append("steam_sell_missing")
    if radar_payload_has_conditional_bid_outlier(payload) or (
        payload.get("platforms") is None and radar_payload_has_conditional_bid_outlier(row.platform_payload_json)
    ):
        flags.append("conditional_bid_outlier")
    if radar_payload_has_baseline_profit(payload, getattr(row, "cash_to_steam_platform", None)) or (
        payload.get("platforms") is None and radar_snapshot_has_baseline_profit(row)
    ):
        flags.append("baseline_price")
    if snapshot_updated_at and datetime.now(snapshot_updated_at.tzinfo) - snapshot_updated_at > timedelta(hours=2):
        flags.append("source_delayed")
    max_profit_rate = max(
        float(row.best_profit_rate or 0),
        float(getattr(row, "cash_to_steam_profit_rate", 0) or 0),
        float(getattr(row, "steam_to_cash_profit_rate", 0) or 0),
    )
    if max_profit_rate >= 300:
        flags.append("suspicious_profit")
    if row.best_direction in {"steam_to_platform", "platform_to_steam"}:
        flags.append("stale_snapshot")
    return sorted(set(flags))


def radar_freshness_label(row: RadarSnapshot) -> str:
    ts = getattr(row, "snapshot_updated_at", None)
    if not ts:
        return "unknown"
    age_minutes = max(0, int((datetime.now() - ts).total_seconds() // 60))
    if age_minutes < 5:
        return "just_now"
    if age_minutes < 60:
        return f"{age_minutes}m_ago"
    age_hours = age_minutes // 60
    if age_hours < 24:
        return f"{age_hours}h_ago"
    return f"{age_hours // 24}d_ago"
migrate_from_json()

PLATFORM_ACTION_WORKER_RUNTIME = PlatformActionWorkerRuntime(
    get_session,
    config_loader=load_app_config,
    credentials_loader=lambda: _load_credentials(),
)
SELLER_SNAPSHOT_SCANNER_RUNTIME = SellerSnapshotScannerRuntime(
    get_session,
    config_loader=load_app_config,
)
LIVE_CANARY_SMOKE_REGISTRY = LiveCanarySmokeRegistry()


@asynccontextmanager
async def _lifespan(application: FastAPI):
    cfg = load_app_config() or {}
    modules = cfg.get("automation_modules") if isinstance(cfg.get("automation_modules"), dict) else {}
    autostart = bool(modules.get("autostart_on_webui_boot", False))
    if autostart:
        try:
            PLATFORM_ACTION_WORKER_RUNTIME.start_from_config()
        except Exception as e:
            print(f"platform action worker startup skipped: {e}")
        try:
            SELLER_SNAPSHOT_SCANNER_RUNTIME.start_from_config()
        except Exception as e:
            print(f"seller snapshot scanner startup skipped: {e}")
    try:
        yield
    finally:
        try:
            SELLER_SNAPSHOT_SCANNER_RUNTIME.stop(timeout_seconds=5)
        except Exception as e:
            print(f"seller snapshot scanner shutdown skipped: {e}")
        try:
            PLATFORM_ACTION_WORKER_RUNTIME.stop(timeout_seconds=5)
        except Exception as e:
            print(f"platform action worker shutdown skipped: {e}")
    # FastAPI 閸忔娊妫撮弮璁圭礉绾喕绻氭惔鏇炵湴瀵洘鎼哥€涙劘绻樼粙瀣潶瑜拌绨冲〒鍛倞
    try:
        api_stop()
    except Exception as e:
        print(f"濞撳懐鎮婃惔鏇炵湴瀵洘鎼告潻娑氣柤閺冭泛鍤柨? {e}")


app = FastAPI(title="aetherswap", lifespan=_lifespan)

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
LOG_PATH = BASE_DIR / "logs" / "aetherswap_engine.log"
ENGINE_PROCESS: subprocess.Popen | None = None
ENGINE_LOCK = threading.Lock()
_CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"
_SESSION_CAPSULES_PATH = BASE_DIR / "config" / "session_capsules.json"
_STEAMDT_OPENAPI_PRICE_STATE_PATH = BASE_DIR / "config" / "steamdt_openapi_price_state.json"
_PLATFORM_RUNTIME_STATE_PATH = BASE_DIR / "config" / "platform_runtime_state.json"
_UUYP_MAPPER_PATH = BASE_DIR / "DataEngine" / "SteamTradingSite-ID-Mapper-main" / "uuyp" / "730.json"
_UUYP_MAPPER_CACHE: dict[str, str] | None = None

for static_name in ("css", "js", "images", "flags"):
    static_dir = WEB_DIR / static_name
    if static_dir.is_dir():
        app.mount(f"/{static_name}", StaticFiles(directory=str(static_dir)), name=static_name)


def _engine_paths() -> tuple[str]:
    return (str(BASE_DIR / "DataEngine" / "master_loop.py"),)


def _load_uuyp_mapper() -> dict[str, str]:
    global _UUYP_MAPPER_CACHE
    if _UUYP_MAPPER_CACHE is not None:
        return _UUYP_MAPPER_CACHE
    try:
        data = json.loads(_UUYP_MAPPER_PATH.read_text(encoding="utf-8") or "{}")
        _UUYP_MAPPER_CACHE = {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        _UUYP_MAPPER_CACHE = {}
    return _UUYP_MAPPER_CACHE


def _is_process_running(proc: subprocess.Popen | None) -> bool:
    return proc is not None and proc.poll() is None


def _start_engine_process() -> subprocess.Popen:
    clear_engine_stop()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, _engine_paths()[0]],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        env=env,
    )
    return proc


def _stop_engine_process(proc: subprocess.Popen | None) -> None:
    request_engine_stop()
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                return
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    finally:
        if _is_process_running(proc):
            try:
                proc.kill()
            except Exception:
                pass


def _load_runtime_config() -> dict:
    cfg = load_app_config()
    return cfg if isinstance(cfg, dict) else {}


def _save_runtime_config(cfg: dict) -> None:
    save_app_config(cfg if isinstance(cfg, dict) else {})


def _module_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _auto_trading_enabled_from_config(cfg: dict | None = None) -> bool:
    cfg = cfg if isinstance(cfg, dict) else _load_runtime_config()
    modules = cfg.get("automation_modules") if isinstance(cfg.get("automation_modules"), dict) else {}
    if "auto_trading_enabled" in modules:
        return _module_bool(modules.get("auto_trading_enabled"), True)
    return True


def _set_auto_trading_enabled(enabled: bool) -> dict:
    cfg = _load_runtime_config()
    modules = cfg.get("automation_modules") if isinstance(cfg.get("automation_modules"), dict) else {}
    modules["auto_trading_enabled"] = bool(enabled)
    cfg["automation_modules"] = modules
    if not enabled:
        trading_worker = cfg.get("trading_worker") if isinstance(cfg.get("trading_worker"), dict) else {}
        trading_worker["enabled"] = False
        cfg["trading_worker"] = trading_worker
        seller_scanner = cfg.get("seller_snapshot_scanner") if isinstance(cfg.get("seller_snapshot_scanner"), dict) else {}
        seller_scanner["enabled"] = False
        cfg["seller_snapshot_scanner"] = seller_scanner
    _save_runtime_config(cfg)
    return cfg


def _save_trading_module_config(
    *,
    enabled: bool,
    trading_worker: dict | None = None,
    seller_snapshot_scanner: dict | None = None,
) -> dict:
    cfg = _load_runtime_config()
    modules = cfg.get("automation_modules") if isinstance(cfg.get("automation_modules"), dict) else {}
    modules["auto_trading_enabled"] = bool(enabled)
    cfg["automation_modules"] = modules
    if trading_worker is not None:
        cfg["trading_worker"] = dict(trading_worker)
    if seller_snapshot_scanner is not None:
        cfg["seller_snapshot_scanner"] = dict(seller_snapshot_scanner)
    _save_runtime_config(cfg)
    return cfg


def _scrape_status_payload() -> dict:
    with ENGINE_LOCK:
        running = _is_process_running(ENGINE_PROCESS)
        pid = ENGINE_PROCESS.pid if running and ENGINE_PROCESS is not None else None
    return {
        "running": running,
        "pid": pid,
        "log_path": str(LOG_PATH),
    }


def _trading_status_payload(cfg: dict | None = None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else _load_runtime_config()
    worker = PLATFORM_ACTION_WORKER_RUNTIME.status()
    scanner = SELLER_SNAPSHOT_SCANNER_RUNTIME.status()
    enabled = _auto_trading_enabled_from_config(cfg)
    return {
        "enabled": enabled,
        "worker": worker,
        "scanner": scanner,
        "running": bool(enabled and (worker.get("running") or scanner.get("running"))),
        "worker_running": bool(worker.get("running")),
        "scanner_running": bool(scanner.get("running")),
    }


def _system_modules_status() -> dict:
    q = get_task_queue()
    active_tasks = q.active_count()
    scrape = _scrape_status_payload()
    trading = _trading_status_payload()
    return {
        "success": True,
        "running": bool(scrape["running"] or active_tasks > 0 or trading["running"]),
        "scrape": scrape,
        "trading": trading,
        "engine_running": scrape["running"],
        "pid": scrape["pid"],
        "active_tasks": active_tasks,
        "log_path": str(LOG_PATH),
    }


@app.post("/api/start")
def api_start():
    return api_scrape_start()


@app.post("/api/stop")
def api_stop():
    scrape = api_scrape_stop()
    trading = api_trading_stop({"timeout_seconds": 5})
    return {
        "success": True,
        "running": False,
        "scrape": scrape,
        "trading": trading,
        "cancelled_tasks": scrape.get("cancelled_tasks", 0),
        "msg": "AetherSwap modules stopped",
    }


@app.get("/api/modules/status")
def api_modules_status():
    return _system_modules_status()


@app.post("/api/modules/scrape/start")
def api_scrape_start():
    global ENGINE_PROCESS
    with ENGINE_LOCK:
        if _is_process_running(ENGINE_PROCESS):
            return {"success": True, "running": True, "msg": "数据爬取已在运行", "scrape": _scrape_status_payload()}
        ENGINE_PROCESS = _start_engine_process()
    return {"success": True, "running": True, "msg": "数据爬取已启动", "scrape": _scrape_status_payload()}


@app.post("/api/modules/scrape/stop")
def api_scrape_stop():
    global ENGINE_PROCESS
    with ENGINE_LOCK:
        proc = ENGINE_PROCESS
        ENGINE_PROCESS = None
    request_stop()
    cancelled_tasks = get_task_queue().cancel_all()
    _stop_engine_process(proc)
    return {"success": True, "running": False, "cancelled_tasks": cancelled_tasks, "msg": "数据爬取已停止", "scrape": _scrape_status_payload()}


@app.post("/api/modules/trading/start")
def api_trading_start(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = _load_runtime_config()
    section = dict(cfg.get("trading_worker") or {})
    section["enabled"] = True
    for key in (
        "safe_mode",
        "poll_interval_seconds",
        "batch_size",
        "lease_seconds",
        "error_backoff_seconds",
    ):
        if key in payload:
            section[key] = payload[key]
    if not bool(section.get("safe_mode", True)):
        canary_config = live_canary_config_from_app_config(cfg)
        if not canary_config.allow_background_worker:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "reason": "live_canary_background_worker_blocked",
                    "msg": "Use manual run_once(limit=1) for first live canary actions.",
                    "trading": _trading_status_payload(cfg),
                },
            )
    started_worker = PLATFORM_ACTION_WORKER_RUNTIME.start({"trading_worker": section, "SAFE_MODE_ENABLED": cfg.get("SAFE_MODE_ENABLED")})
    worker_running = bool(started_worker or PLATFORM_ACTION_WORKER_RUNTIME.status().get("running"))
    if not worker_running:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "reason": "trading_worker_start_failed",
                "msg": "自动化交易启动失败：执行 worker 未进入运行态。",
                "trading": _trading_status_payload(cfg),
            },
        )
    scanner_section = dict(cfg.get("seller_snapshot_scanner") or {})
    scanner_started = False
    if bool(payload.get("start_scanner", False)):
        scanner_section["enabled"] = True
        scanner_section.setdefault("commit", False)
        scanner_started = bool(SELLER_SNAPSHOT_SCANNER_RUNTIME.start({"seller_snapshot_scanner": scanner_section}))
    else:
        scanner_section["enabled"] = bool(scanner_section.get("enabled", False))
    saved_cfg = _save_trading_module_config(
        enabled=True,
        trading_worker=section,
        seller_snapshot_scanner=scanner_section,
    )
    return {
        "success": True,
        "started": worker_running,
        "started_scanner": scanner_started,
        "msg": "自动化交易已启动",
        "trading": _trading_status_payload(saved_cfg),
    }


@app.post("/api/modules/trading/stop")
def api_trading_stop(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    timeout_seconds = float(payload.get("timeout_seconds") or 5)
    stopped_worker = PLATFORM_ACTION_WORKER_RUNTIME.stop(timeout_seconds=timeout_seconds)
    stopped_scanner = SELLER_SNAPSHOT_SCANNER_RUNTIME.stop(timeout_seconds=timeout_seconds)
    cfg = _set_auto_trading_enabled(False)
    return {
        "success": True,
        "running": False,
        "stopped_worker": stopped_worker,
        "stopped_scanner": stopped_scanner,
        "msg": "自动化交易已停止",
        "trading": _trading_status_payload(cfg),
    }


@app.post("/api/system/sync_items")
def api_system_sync_items():
    script_path = BASE_DIR / "DataEngine" / "init_items.py"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    if output:
        log(f"sync_items output:\n{output}", level="info")
    if result.returncode != 0:
        log(f"sync_items failed with code {result.returncode}: {output[-2000:]}", level="error")
        return JSONResponse(
            status_code=500,
            content={"success": False, "msg": "妤楁澘鎼х€涙鍚€閸氬本顒炴径杈Е", "detail": output[-2000:]},
        )
    log("sync_items completed successfully", level="info")
    return {"success": True, "msg": "item sync completed"}


@app.get("/api/platform/session_state")
def api_platform_session_state():
    from app.services.platform_sessions import PlatformSessionStateStore

    store = PlatformSessionStateStore()
    states = []
    for platform in ["steam", "buff", "uuyp", "eco", "c5game"]:
        state = store.get(platform)
        data = state.to_dict()
        data["cooldown_remaining"] = state.cooldown_remaining
        data["cooldown_open"] = state.is_cooldown_open
        states.append(data)
    return {"success": True, "states": states}


@app.get("/api/trade/exposure_guard/preview")
def api_trade_exposure_guard_preview(rule: str = ""):
    guard = LowPriceExposureGuard(load_app_config())
    try:
        preview = guard.preview(rule or None)
        return {"success": True, **preview}
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "msg": str(exc), "reason": "low_price_exposure_rule_invalid"},
        )


@app.post("/api/trade/manual_buy")
def api_trade_manual_buy(payload: dict):
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    platform = normalize_platform(str(payload.get("platform") or payload.get("buy_platform") or "").strip()) if isinstance(payload, dict) else ""
    if item_id is None or not platform:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 閸?platform 娑撳秷鍏樻稉铏光敄"})

    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 閺嶇厧绱￠柨娆掝嚖"})

    try:
        buy_price = float(payload.get("buy_price") or 0)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 閺嶇厧绱￠柨娆掝嚖"})
    if buy_price <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 韫囧懘銆忔径褌绨?0"})

    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 閺嶇厧绱￠柨娆掝嚖"})
    if quantity < 1:
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 韫囧懘銆忔径褌绨?0"})

    with SessionLocal() as session:
        item = session.get(ItemBase, item_id)
        if item is None:
            return JSONResponse(status_code=404, content={"success": False, "msg": "item not found"})
        market_hash_name = item.market_hash_name
        platform_mapping = (
            session.query(PlatformMapping)
            .filter(PlatformMapping.item_id == item_id, PlatformMapping.platform_name == platform)
            .one_or_none()
        )
        mapped_id = str(platform_mapping.platform_item_id) if platform_mapping and platform_mapping.platform_item_id else ""
        buff_goods_id = item.buff_goods_id or (mapped_id if platform == "buff" else None)
        uuyp_template_id = item.uuyp_template_id or (mapped_id if platform == "uuyp" else None)
        eco_goods_id = item.eco_goods_id or (mapped_id if platform == "eco" else None)
        platform_id = {
            "buff": buff_goods_id,
            "uuyp": uuyp_template_id,
            "eco": eco_goods_id,
            "c5game": mapped_id,
        }.get(platform)
        invalid_signal = _invalid_signal_source_response(session, payload, item_id=item_id)
        if invalid_signal is not None:
            return JSONResponse(status_code=invalid_signal["status_code"], content=invalid_signal["content"])
        exposure_decision = LowPriceExposureGuard(load_app_config()).check(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=buy_price,
            proposed_quantity=quantity,
            fail_closed=True,
        )
        if not exposure_decision.allowed:
            detail = exposure_decision.to_dict()
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "msg": "low price exposure quota blocked this buy",
                    "reason": exposure_decision.reason,
                    "exposure_guard": detail,
                },
            )

    action = _normalize_manual_buy_action(payload.get("action"))
    if action is None:
        return JSONResponse(status_code=400, content={"success": False, "msg": "unsupported manual buy action"})
    record_id = _create_trade_record(
        action=action,
        channel="ui",
        item_id=item_id,
        market_hash_name=market_hash_name,
        platform=platform,
        quantity=quantity,
        target_price=buy_price,
        status="running",
        request_payload={k: v for k, v in payload.items() if k not in {"cookies", "cookie"}},
    )
    platform_action_id = _record_platform_action_from_manual_buy(
        action=action,
        platform=platform,
        item_id=item_id,
        market_hash_name=market_hash_name,
        quantity=quantity,
        target_price=buy_price,
        payload=payload,
        record_id=record_id,
        platform_item_id=platform_id,
    )
    if platform not in {"buff", "uuyp", "eco", "c5game"}:
        result = {"success": False, "msg": f"unsupported platform: {platform}", "reason": "unsupported_platform"}
        _update_trade_record(record_id, status="failed", response_payload=result, error_message=result["msg"])
        _update_platform_action_from_trade_result(platform_action_id, status="failed", result=result, error_message=result["msg"])
        return JSONResponse(status_code=400, content={**result, "record_id": record_id, "platform_action_id": platform_action_id})

    with get_session() as session:
        platform_action = session.get(PlatformAction, int(platform_action_id)) if platform_action_id else None
        if platform_action is None:
            result = {"success": False, "msg": "platform action bridge failed", "reason": "platform_action_missing"}
            _update_trade_record(record_id, status="failed", response_payload=result, error_message=result["msg"])
            return JSONResponse(status_code=500, content={**result, "record_id": record_id})

        try:
            adapter = PlatformClientAdapter(
                platform,
                credentials=_load_credentials(),
                config=load_app_config(),
            )
            normalized = adapter.submit(platform_action)
            action_success_state = _manual_success_state(normalized) if normalized.success else PlatformActionState.FAILED
            status = "success" if normalized.success else "failed"
            record_status = "success" if action_success_state == PlatformActionState.SUCCEEDED else "submitted" if normalized.success else "failed"
            result = _normalized_result_payload(normalized)
            stale_opportunity = _close_stale_opportunity_if_needed(payload, normalized, result)
            _update_trade_record(
                record_id,
                status=record_status,
                response_payload=result,
                error_message="" if normalized.success else normalized.message,
            )
            _apply_normalized_result_to_platform_action(
                platform_action,
                normalized,
                status=status,
            )
            materialize_purchase_for_action(session, platform_action)
            session.add(platform_action)
            session.commit()
            session.refresh(platform_action)
            if normalized.success and action_success_state == PlatformActionState.SUCCEEDED:
                _notify_trade_success_safe(
                    item_name=market_hash_name,
                    action=action,
                    price=buy_price,
                    platform=platform,
                    quantity=quantity,
                    extra={"record_id": record_id, "platform_action_id": platform_action.id},
                )
            log(
                f"manual_buy finished | action={action} platform={platform} item_id={item_id} "
                f"market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} success={normalized.success}",
                level="info",
            )
            status_code = 200 if normalized.success else 400
            content = {
                "success": bool(normalized.success),
                "msg": normalized.message or ("platform action submitted" if normalized.success else normalized.category),
                "result": result,
                "market_hash_name": market_hash_name,
                "record_id": record_id,
                "platform_action_id": platform_action.id,
                "action": action,
            }
            if stale_opportunity:
                content.update(
                    {
                        "stale_opportunity": True,
                        "closed_opportunity_id": stale_opportunity.get("opportunity_id"),
                        "refresh_opportunities": True,
                    }
                )
            if status_code == 200:
                return content
            return JSONResponse(status_code=status_code, content=content)
        except Exception as exc:
            result = {"success": False, "msg": str(exc), "reason": "manual_buy_exception"}
            _update_trade_record(record_id, status="failed", response_payload=result, error_message=str(exc))
            _update_platform_action_from_trade_result(platform_action_id, status="failed", result=result, error_message=str(exc))
            log(f"manual_buy failed | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} err={exc}", level="error")
            return JSONResponse(status_code=500, content={"success": False, "msg": f"{platform} order failed: {exc}", "record_id": record_id, "platform_action_id": platform_action_id})


@app.post("/api/trade/manual_steam_order")
def api_trade_manual_steam_order(payload: dict):
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    buy_price = payload.get("buy_price") if isinstance(payload, dict) else None
    quantity = payload.get("quantity") if isinstance(payload, dict) else 1
    if item_id is None or buy_price is None:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 閸?buy_price 娑撳秷鍏樻稉铏光敄"})
    try:
        item_id = int(item_id)
        buy_price = float(buy_price)
        quantity = int(quantity)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "invalid numeric parameter"})
    if buy_price <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 韫囧懘銆忔径褌绨?0"})
    if quantity < 1:
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 韫囧懘銆忔径褌绨?0"})

    with SessionLocal() as session:
        item = session.get(ItemBase, item_id)
        if item is None:
            return JSONResponse(status_code=404, content={"success": False, "msg": "item not found"})
        market_hash_name = item.market_hash_name
        exposure_decision = LowPriceExposureGuard(load_app_config()).check(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=buy_price,
            proposed_quantity=quantity,
            fail_closed=True,
        )
        if not exposure_decision.allowed:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "msg": "low price exposure quota blocked this Steam buy order",
                    "reason": exposure_decision.reason,
                    "exposure_guard": exposure_decision.to_dict(),
                },
            )
        steam_row = (
            session.query(MarketPrice)
            .filter(MarketPrice.item_id == item_id, MarketPrice.platform_name == "steam")
            .order_by(MarketPrice.updated_at.desc())
            .first()
        )
        steam_buy_max = float(steam_row.buy_max or 0) if steam_row else 0.0
        credentials = _load_credentials()
        steam_cookie = str((credentials.get("steam") or {}).get("cookies") or (credentials.get("steam") or {}).get("cookie") or "").strip()

    record_id = _create_trade_record(
        action="steam_order",
        channel="ui",
        item_id=item_id,
        market_hash_name=market_hash_name,
        platform="steam",
        quantity=quantity,
        target_price=buy_price,
        reference_price=steam_buy_max,
        status="running",
        request_payload={"item_id": item_id, "buy_price": buy_price, "quantity": quantity},
    )

    if not steam_cookie:
        _update_trade_record(record_id, status="failed", error_message="missing Steam cookie")
        return JSONResponse(status_code=400, content={"success": False, "msg": "missing Steam cookie"})

    try:
        buyer = SteamBuyer(cookie_str=steam_cookie)
        result = buyer.create_buy_order(
            market_hash_name=market_hash_name,
            price=buy_price,
            quantity=quantity,
        )
        if (not result.success) and ("session" in (result.msg or "").lower() or "csrf" in (result.msg or "").lower()):
            try:
                refreshed_cookie = asyncio.run(finish_login_and_extract("steam"))
                if refreshed_cookie:
                    buyer = SteamBuyer(cookie_str=refreshed_cookie)
                    result = buyer.create_buy_order(
                        market_hash_name=market_hash_name,
                        price=buy_price,
                        quantity=quantity,
                    )
            except Exception as retry_exc:
                log(f"manual_steam_order retry refresh failed | item_id={item_id} err={retry_exc}", level="warning")

        if not result.success:
            _update_trade_record(record_id, status="failed", response_payload=result.raw or {}, error_message=result.msg)
            log(
                f"manual_steam_order failed | item_id={item_id} market_hash_name={market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity} err={result.msg}",
                level="error",
            )
            return JSONResponse(status_code=500, content={"success": False, "msg": result.msg, "detail": result.raw})
        _update_trade_record(record_id, status="success", response_payload=result.raw or {}, error_message="")
        log(
            f"manual_steam_order success | item_id={item_id} market_hash_name={market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity}",
            level="info",
        )
        return {
            "success": True,
            "msg": result.msg,
            "item_id": item_id,
            "market_hash_name": market_hash_name,
            "steam_buy_max": steam_buy_max,
            "buy_price": buy_price,
            "quantity": quantity,
            "detail": result.raw,
            "record_id": record_id,
        }
    except Exception as exc:
        _update_trade_record(record_id, status="failed", error_message=str(exc))
        log(
            f"manual_steam_order exception | item_id={item_id} market_hash_name={market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity} err={exc}",
            level="error",
        )
        return JSONResponse(status_code=500, content={"success": False, "msg": f"Steam 濮瑰倽鍠樻径杈Е: {exc}"})


@app.get("/api/status")
def api_status():
    payload = _system_modules_status()
    payload["status"] = "running" if payload["running"] else "idle"
    return payload


@app.get("/api/trade/steam_active_orders")
def api_trade_steam_active_orders():
    steam_cookie = _normalize_steam_cookie()
    if not steam_cookie:
        return JSONResponse(status_code=400, content={"success": False, "msg": "??? Steam Cookie????? Steam ???"})

    buyer = SteamBuyer(cookie_str=steam_cookie)
    try:
        with SessionLocal() as session:
            item_rows = {str(row.market_hash_name): row for row in session.query(ItemBase).all()}
        orders = []
        for order in buyer.fetch_active_buy_orders():
            market_hash_name = str(order.get("market_hash_name") or "").strip()
            current_highest = 0.0
            item = item_rows.get(market_hash_name)
            if item is not None:
                steam_row = (
                    SessionLocal().query(MarketPrice)
                    .filter(MarketPrice.item_id == item.id, MarketPrice.platform_name == "steam")
                    .order_by(MarketPrice.updated_at.desc())
                    .first()
                )
                current_highest = float(steam_row.buy_max or 0) if steam_row else 0.0
            my_price = float(order.get("my_price") or 0)
            delta = round(my_price - current_highest, 2)
            orders.append({
                **order,
                "current_highest_buy": current_highest,
                "delta": delta,
                "status": "overbid" if current_highest > my_price else "active",
                "item_id": item.id if item is not None else None,
                "item_name": item.cn_name if item is not None else market_hash_name,
            })
        return {"success": True, "items": orders, "total": len(orders)}
    except Exception as exc:
        try:
            refreshed_cookie = asyncio.run(finish_login_and_extract("steam"))
            if refreshed_cookie:
                buyer = SteamBuyer(cookie_str=refreshed_cookie)
                with SessionLocal() as session:
                    item_rows = {str(row.market_hash_name): row for row in session.query(ItemBase).all()}
                orders = []
                for order in buyer.fetch_active_buy_orders():
                    market_hash_name = str(order.get("market_hash_name") or "").strip()
                    current_highest = 0.0
                    item = item_rows.get(market_hash_name)
                    if item is not None:
                        steam_row = (
                            SessionLocal().query(MarketPrice)
                            .filter(MarketPrice.item_id == item.id, MarketPrice.platform_name == "steam")
                            .order_by(MarketPrice.updated_at.desc())
                            .first()
                        )
                        current_highest = float(steam_row.buy_max or 0) if steam_row else 0.0
                    my_price = float(order.get("my_price") or 0)
                    delta = round(my_price - current_highest, 2)
                    orders.append({
                        **order,
                        "current_highest_buy": current_highest,
                        "delta": delta,
                        "status": "overbid" if current_highest > my_price else "active",
                        "item_id": item.id if item is not None else None,
                        "item_name": item.cn_name if item is not None else market_hash_name,
                    })
                return {"success": True, "items": orders, "total": len(orders)}
        except Exception as retry_exc:
            log(f"steam_active_orders retry failed: {retry_exc}", level="warning")
        return JSONResponse(status_code=500, content={"success": False, "msg": f"閼惧嘲褰?Steam 濞叉槒绌Ч鍌濆枠閸楁洖銇戠拹? {exc}"})


@app.get("/api/trade/execution_records")
def api_trade_execution_records(limit: int = 50, offset: int = 0, action: str = "", platform: str = "", status: str = "", item_id: int | None = None):
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    with get_session() as session:
        stmt = select(TradeExecutionRecord)
        if action:
            stmt = stmt.where(TradeExecutionRecord.action == action)
        if platform:
            stmt = stmt.where(TradeExecutionRecord.platform == platform)
        if status:
            stmt = stmt.where(TradeExecutionRecord.status == status)
        if item_id is not None:
            stmt = stmt.where(TradeExecutionRecord.item_id == int(item_id))
        total = len(session.execute(stmt).all())
        rows = session.execute(stmt.order_by(TradeExecutionRecord.created_at.desc()).offset(offset).limit(limit)).scalars().all()
        items = []
        for r in rows:
            items.append({
                "id": r.id,
                "created_at": r.created_at,
                "action": r.action,
                "channel": r.channel,
                "item_id": r.item_id,
                "market_hash_name": r.market_hash_name,
                "platform": r.platform,
                "quantity": r.quantity,
                "target_price": r.target_price,
                "reference_price": r.reference_price,
                "status": r.status,
                "request_payload": r.request_payload,
                "response_payload": r.response_payload,
                "error_message": r.error_message,
            })
        return {"success": True, "total": total, "items": items}


def _platform_action_to_dict(action: PlatformAction) -> dict:
    return {
        "id": action.id,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
        "finished_at": action.finished_at,
        "next_check_at": action.next_check_at,
        "lease_until": action.lease_until,
        "archived_at": action.archived_at,
        "archived_reason": action.archived_reason,
        "archived_by": action.archived_by,
        "action_type": action.action_type,
        "platform": action.platform,
        "state": action.state,
        "channel": action.channel,
        "item_id": action.item_id,
        "market_hash_name": action.market_hash_name,
        "risk_category": action.risk_category,
        "quantity": action.quantity,
        "target_price": action.target_price,
        "reference_price": action.reference_price,
        "cost_basis_cny": action.cost_basis_cny,
        "expected_profit_rate": action.expected_profit_rate,
        "locked_budget_cny": action.locked_budget_cny,
        "filled_quantity": action.filled_quantity,
        "remaining_quantity": action.remaining_quantity,
        "filled_amount_cny": action.filled_amount_cny,
        "released_budget_cny": action.released_budget_cny,
        "platform_order_id": action.platform_order_id,
        "platform_listing_id": action.platform_listing_id,
        "trade_offer_id": action.trade_offer_id,
        "assetid": action.assetid,
        "retry_count": action.retry_count,
        "max_retries": action.max_retries,
        "error_code": action.error_code,
        "error_message": action.error_message,
        "request_payload": action.request_payload,
        "response_payload": action.response_payload,
        "raw_context": action.raw_context,
    }


@app.get("/api/trade/platform_capabilities")
def api_trade_platform_capabilities():
    platforms = {}
    for platform, info in CAPABILITY_REGISTRY.items():
        platforms[platform] = {
            "platform": info.platform,
            "display_name": info.display_name,
            "capabilities": {
                name: asdict(spec)
                for name, spec in info.capabilities.items()
            },
        }
    return {"success": True, "platforms": platforms}


@app.get("/api/trade/platform_actions")
def api_trade_platform_actions(
    limit: int = 50,
    offset: int = 0,
    action_type: str = "",
    platform: str = "",
    state: str = "",
    channel: str = "",
    item_id: int | None = None,
    include_archived: bool = False,
):
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    with get_session() as session:
        stmt = select(PlatformAction)
        if not include_archived:
            stmt = stmt.where(PlatformAction.archived_at.is_(None))
        if action_type:
            stmt = stmt.where(PlatformAction.action_type == action_type)
        if platform:
            stmt = stmt.where(PlatformAction.platform == normalize_platform(platform))
        if state:
            stmt = stmt.where(PlatformAction.state == state)
        if channel:
            stmt = stmt.where(PlatformAction.channel == channel)
        if item_id is not None:
            stmt = stmt.where(PlatformAction.item_id == int(item_id))
        total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = session.execute(
            stmt.order_by(PlatformAction.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all()
        return {"success": True, "total": int(total or 0), "items": [_platform_action_to_dict(row) for row in rows]}


@app.post("/api/trade/platform_actions/clear")
def api_trade_platform_actions_clear(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    scope = str(payload.get("scope") or "terminal").strip().lower()
    state_filter = str(payload.get("state") or "").strip()
    force = bool(payload.get("force", False))
    raw_ids = payload.get("ids")
    terminal_states = set(TERMINAL_STATES)
    now = time.time()

    if scope not in {"terminal", "all"}:
        return JSONResponse(status_code=400, content={"success": False, "msg": "scope must be terminal or all"})

    ids: list[int] | None = None
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            return JSONResponse(status_code=400, content={"success": False, "msg": "ids must be a list"})
        try:
            ids = sorted({int(row_id) for row_id in raw_ids if int(row_id) > 0})
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"success": False, "msg": "ids must contain positive integers"})
        if not ids:
            return {
                "success": True,
                "deleted": 0,
                "archived": 0,
                "cancelled": 0,
                "skipped": 0,
                "deleted_ids": [],
                "archived_ids": [],
                "cancelled_ids": [],
                "skipped_items": [],
                "preserved_audit_records": True,
            }

    with get_session() as session:
        stmt = select(PlatformAction)
        if ids is not None:
            stmt = stmt.where(PlatformAction.id.in_(ids))
        elif state_filter:
            stmt = stmt.where(PlatformAction.state == state_filter)
        elif scope == "terminal":
            stmt = stmt.where(PlatformAction.state.in_(list(terminal_states)))

        rows = session.execute(stmt.order_by(PlatformAction.created_at.desc())).scalars().all()
        archived_ids: list[int] = []
        cancelled_ids: list[int] = []
        skipped_items: list[dict] = []

        for action in rows:
            action_id = int(action.id or 0)
            state = str(action.state or "")
            if action.archived_at is not None:
                skipped_items.append({
                    "id": action_id,
                    "state": state,
                    "reason": "already_archived",
                })
                continue
            if state in terminal_states:
                action.archived_at = now
                action.archived_reason = action.archived_reason or "manual_clear_terminal"
                action.archived_by = action.archived_by or "operator"
                action.updated_at = now
                session.add(action)
                archived_ids.append(action_id)
                continue

            if not force:
                skipped_items.append({
                    "id": action_id,
                    "state": state,
                    "reason": "non_terminal_action_requires_force",
                })
                continue

            locked_budget = float(action.locked_budget_cny or 0)
            action.state = PlatformActionState.CANCELLED
            action.updated_at = now
            action.finished_at = now
            action.next_check_at = now
            action.lease_until = None
            action.released_budget_cny = float(action.released_budget_cny or 0) + locked_budget
            action.locked_budget_cny = 0.0
            action.error_code = action.error_code or "manual_clear_cancelled"
            action.error_message = action.error_message or "Cancelled locally by platform action clear."
            action.archived_at = now
            action.archived_reason = action.archived_reason or "manual_clear_cancelled"
            action.archived_by = action.archived_by or "operator"
            session.add(action)
            cancelled_ids.append(action_id)
            archived_ids.append(action_id)

        session.commit()
        return {
            "success": True,
            "deleted": 0,
            "archived": len(archived_ids),
            "cancelled": len(cancelled_ids),
            "skipped": len(skipped_items),
            "deleted_ids": [],
            "archived_ids": archived_ids,
            "cancelled_ids": cancelled_ids,
            "skipped_items": skipped_items,
            "preserved_audit_records": True,
        }


def _platform_action_summary_payload(session, *, now: float | None = None) -> dict:
    now = float(now or time.time())
    terminal_states = list(TERMINAL_STATES)
    active_states = [state for state in CLAIMABLE_STATES if state not in TERMINAL_STATES]
    attention_states = [
        PlatformActionState.PROCESSING,
        PlatformActionState.RISK_BLOCKED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.WAITING_PLATFORM,
        PlatformActionState.WAITING_TRADE_OFFER,
        PlatformActionState.WAITING_STEAM_CONFIRM,
        PlatformActionState.WAITING_SETTLEMENT,
    ]
    state_rows = session.execute(
        select(PlatformAction.state, func.count(), func.coalesce(func.sum(PlatformAction.locked_budget_cny), 0.0))
        .where(PlatformAction.archived_at.is_(None))
        .group_by(PlatformAction.state)
    ).all()
    platform_rows = session.execute(
        select(PlatformAction.platform, func.count(), func.coalesce(func.sum(PlatformAction.locked_budget_cny), 0.0))
        .where(PlatformAction.state.notin_(terminal_states))
        .where(PlatformAction.archived_at.is_(None))
        .group_by(PlatformAction.platform)
    ).all()
    due_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state.in_(active_states))
        .where(PlatformAction.archived_at.is_(None))
        .where(PlatformAction.next_check_at <= now)
        .order_by(PlatformAction.next_check_at.asc())
        .limit(20)
    ).scalars().all()
    stuck_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state.in_(attention_states))
        .where(PlatformAction.archived_at.is_(None))
        .order_by(PlatformAction.updated_at.asc())
        .limit(20)
    ).scalars().all()
    risk_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state == PlatformActionState.RISK_BLOCKED)
        .where(PlatformAction.archived_at.is_(None))
        .order_by(PlatformAction.updated_at.desc())
        .limit(20)
    ).scalars().all()
    active_budget = session.execute(
        select(func.coalesce(func.sum(PlatformAction.locked_budget_cny), 0.0))
        .where(PlatformAction.state.notin_(terminal_states))
        .where(PlatformAction.archived_at.is_(None))
    ).scalar_one()
    due_count = session.execute(
        select(func.count())
        .where(PlatformAction.state.in_(active_states))
        .where(PlatformAction.archived_at.is_(None))
        .where(PlatformAction.next_check_at <= now)
    ).scalar_one()
    return {
        "by_state": {
            row[0]: {"count": int(row[1] or 0), "locked_budget_cny": float(row[2] or 0)}
            for row in state_rows
        },
        "active_by_platform": {
            row[0]: {"count": int(row[1] or 0), "locked_budget_cny": float(row[2] or 0)}
            for row in platform_rows
        },
        "active_locked_budget_cny": float(active_budget or 0),
        "due_count": int(due_count or 0),
        "due_items": [_platform_action_to_dict(row) for row in due_rows],
        "attention_items": [_platform_action_to_dict(row) for row in stuck_rows],
        "risk_blocked_items": [_platform_action_to_dict(row) for row in risk_rows],
    }


def _action_payload_dict(action) -> dict:
    if isinstance(action, dict):
        return dict(action)
    raw = getattr(action, "request_payload", None)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def _normalize_manual_buy_action(raw_action) -> str | None:
    token = str(raw_action or "").strip().lower()
    if not token:
        return PlatformActionType.PURCHASE_ORDER
    if token in {"direct", "direct_buy", "direct_trade", "instant_buy", "buy_listing", "cash_direct_buy"}:
        return PlatformActionType.DIRECT_BUY
    if token in {"platform_order", "purchase", "buy_order", "purchase_order", "create_buy_order", "cash_purchase_order"}:
        return PlatformActionType.PURCHASE_ORDER
    return None


def _manual_buy_request_payload(
    *,
    payload: dict,
    platform: str,
    platform_item_id,
    market_hash_name: str,
) -> dict:
    safe_payload = {k: v for k, v in (payload or {}).items() if k not in {"cookies", "cookie"}}
    if platform_item_id not in (None, ""):
        safe_payload.setdefault("platform_item_id", platform_item_id)
        if platform == "buff":
            safe_payload.setdefault("goods_id", platform_item_id)
            safe_payload.setdefault("buff_goods_id", platform_item_id)
        elif platform == "uuyp":
            safe_payload.setdefault("template_id", platform_item_id)
            safe_payload.setdefault("uuyp_template_id", platform_item_id)
        elif platform == "eco":
            safe_payload.setdefault("goods_id", platform_item_id)
            safe_payload.setdefault("eco_goods_id", platform_item_id)
    safe_payload.setdefault("market_hash_name", market_hash_name)
    return safe_payload


def _normalized_result_payload(result) -> dict:
    payload = result.response_payload if isinstance(getattr(result, "response_payload", None), dict) else {}
    out = dict(payload)
    out.setdefault("success", bool(result.success))
    out.setdefault("msg", result.message)
    out["category"] = result.category
    if result.platform_order_id:
        out.setdefault("order_id", result.platform_order_id)
        out.setdefault("platform_order_id", result.platform_order_id)
    if result.platform_listing_id:
        out.setdefault("platform_listing_id", result.platform_listing_id)
    if result.trade_offer_id:
        out.setdefault("trade_offer_id", result.trade_offer_id)
    if result.assetid:
        out.setdefault("assetid", result.assetid)
    if result.filled_quantity is not None:
        out.setdefault("filled_quantity", result.filled_quantity)
    if result.remaining_quantity is not None:
        out.setdefault("remaining_quantity", result.remaining_quantity)
    if result.filled_amount_cny is not None:
        out.setdefault("filled_amount_cny", result.filled_amount_cny)
    if result.remaining_amount_cny is not None:
        out.setdefault("remaining_amount_cny", result.remaining_amount_cny)
    return out


def _is_not_found_like(category: str, reason: str, message: str) -> bool:
    category_token = str(category or "").strip().lower()
    reason_token = str(reason or "").strip().lower()
    if category_token == RESULT_NOT_FOUND:
        return True
    if reason_token in {"not_found", "listing_not_found", "sell_order_not_found", "order_not_found"}:
        return True
    text = f"{reason_token} {str(message or '').strip().lower()}".strip()
    if not text:
        return False
    hints = (
        "not found",
        "no listing",
        "no sell order",
        "listing not found",
        "sell order not found",
        "at or below target price",
        "target price",
        "has been removed",
        "already sold",
        "sold out",
        "已无可买",
        "无可买",
        "没有可买",
        "目标价内已无",
        "挂单不存在",
        "报价不存在",
        "买单不存在",
    )
    return any(token in text for token in hints)


def _close_stale_opportunity_if_needed(payload: dict, result, result_payload: dict | None = None) -> dict | None:
    if getattr(result, "success", False):
        return None
    category = str(getattr(result, "category", "") or "")
    response = result_payload if isinstance(result_payload, dict) else _normalized_result_payload(result)
    reason = str(response.get("reason") or response.get("code") or "").strip().lower()
    message = str(response.get("msg") or response.get("message") or getattr(result, "message", "") or "").lower()
    stale = _is_not_found_like(category, reason, message)
    if not stale:
        return None

    try:
        with SessionLocal() as session:
            opportunity_id = _payload_int(payload, "opportunity_id")
            opportunity = session.get(ArbitrageOpportunity, opportunity_id) if opportunity_id > 0 else None
            if opportunity is None:
                opportunity = _find_matching_open_opportunity(session, payload)
            if opportunity is None:
                return None
            opportunity_id = int(opportunity.id)
            opportunity.status = "closed"
            opportunity.updated_at = datetime.now()
            session.add(opportunity)
            session.commit()
        return {"opportunity_id": opportunity_id, "status": "closed", "reason": reason or category}
    except Exception as exc:
        log(f"close stale opportunity failed | opportunity_id={opportunity_id} err={exc}", level="warning")
        return None


def _payload_int(payload: dict | None, key: str, default: int = 0) -> int:
    try:
        return int((payload or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _payload_float(payload: dict | None, key: str, default: float = 0.0) -> float:
    try:
        return float((payload or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _is_non_decision_market_price(row: MarketPrice | None) -> bool:
    return str(getattr(row, "data_source", "") or "").strip().lower() == "baseline"


def _market_price_for_platform(session, item_id: int, platform: str) -> MarketPrice | None:
    platform = normalize_platform(str(platform or ""))
    if item_id <= 0 or not platform:
        return None
    return (
        session.query(MarketPrice)
        .filter(MarketPrice.item_id == int(item_id))
        .filter(func.lower(MarketPrice.platform_name) == platform)
        .first()
    )


def _opportunity_non_decision_problem(session, opportunity: ArbitrageOpportunity | None) -> dict | None:
    if opportunity is None:
        return None
    item_id = int(getattr(opportunity, "item_id", 0) or 0)
    for platform in {
        str(getattr(opportunity, "buy_platform", "") or "").lower().strip(),
        str(getattr(opportunity, "sell_platform", "") or "").lower().strip(),
    }:
        if not platform:
            continue
        row = _market_price_for_platform(session, item_id, platform)
        if _is_non_decision_market_price(row):
            return {
                "platform": platform,
                "data_source": str(getattr(row, "data_source", "") or ""),
                "reason": "baseline_price",
            }
    return None


def _invalid_signal_source_response(session, payload: dict | None, *, item_id: int) -> dict | None:
    opportunity_id = _payload_int(payload, "opportunity_id")
    if opportunity_id <= 0:
        return None
    opportunity = session.get(ArbitrageOpportunity, opportunity_id)
    if opportunity is None:
        return None
    if int(opportunity.item_id or 0) != int(item_id):
        return {
            "status_code": 400,
            "content": {
                "success": False,
                "reason": "opportunity_item_mismatch",
                "msg": "机会与请求饰品不匹配，已阻止执行",
                "opportunity_id": opportunity_id,
            },
        }
    problem = _opportunity_non_decision_problem(session, opportunity)
    if not problem:
        return None
    opportunity.status = "closed"
    opportunity.updated_at = datetime.now()
    session.add(opportunity)
    session.commit()
    return {
        "status_code": 409,
        "content": {
            "success": False,
            "reason": "non_decision_market_price",
            "msg": "该机会来自基线占位行情，不能执行真实交易，已关闭并从机会列表移除",
            "stale_opportunity": True,
            "closed_opportunity_id": opportunity_id,
            "refresh_opportunities": True,
            "source_problem": problem,
        },
    }


def _find_matching_open_opportunity(session, payload: dict | None):
    item_id = _payload_int(payload, "item_id")
    platform = normalize_platform(str((payload or {}).get("platform") or (payload or {}).get("buy_platform") or ""))
    buy_price = _payload_float(payload, "buy_price")
    if item_id <= 0 or not platform or buy_price <= 0:
        return None
    rows = (
        session.execute(
            select(ArbitrageOpportunity)
            .where(ArbitrageOpportunity.item_id == item_id)
            .where(ArbitrageOpportunity.buy_platform.in_([platform, str((payload or {}).get("platform") or "").strip()]))
            .where(ArbitrageOpportunity.status.in_(["open", "verifying"]))
            .order_by(ArbitrageOpportunity.updated_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    for row in rows:
        try:
            row_price = float(row.buy_price or 0)
        except (TypeError, ValueError):
            row_price = 0.0
        if abs(row_price - buy_price) <= 0.01:
            return row
    return rows[0] if len(rows) == 1 else None


def _result_error_code(result) -> str:
    payload = result.response_payload if isinstance(getattr(result, "response_payload", None), dict) else {}
    reason = str(payload.get("reason") or payload.get("code") or "").strip()
    text = f"{reason} {getattr(result, 'message', '')}".lower()
    if reason:
        return reason
    if result.category == "auth_required" and "missing" in text and "credential" in text:
        return "missing_credentials"
    return str(result.category or "manual_buy_failed")


def _manual_success_state(result) -> str:
    if result.category in {RESULT_ORDER_COMPLETED, RESULT_TRADE_OFFER_ACCEPTED}:
        return PlatformActionState.SUCCEEDED
    if result.trade_offer_id:
        return PlatformActionState.WAITING_TRADE_OFFER
    if result.category == RESULT_ORDER_PENDING or result.platform_order_id:
        return PlatformActionState.WAITING_PLATFORM
    return PlatformActionState.SUCCEEDED


def _apply_normalized_result_to_platform_action(action: PlatformAction, result, *, status: str) -> None:
    response_payload = _normalized_result_payload(result)
    poll_interval = _manual_action_poll_interval_seconds(action)
    updates = {
        "platform_order_id": result.platform_order_id or action.platform_order_id,
        "platform_listing_id": result.platform_listing_id or action.platform_listing_id,
        "trade_offer_id": result.trade_offer_id or action.trade_offer_id,
        "assetid": result.assetid or action.assetid,
        "response_payload": response_payload,
    }
    if result.filled_quantity is not None:
        updates["filled_quantity"] = max(int(action.filled_quantity or 0), int(result.filled_quantity or 0))
    if result.remaining_quantity is not None:
        updates["remaining_quantity"] = max(0, int(result.remaining_quantity or 0))
    if result.filled_amount_cny is not None:
        updates["filled_amount_cny"] = max(float(action.filled_amount_cny or 0), float(result.filled_amount_cny or 0))
    if result.remaining_amount_cny is not None:
        remaining = max(0.0, float(result.remaining_amount_cny or 0))
        updates["locked_budget_cny"] = remaining
        updates["released_budget_cny"] = round(max(0.0, float(action.released_budget_cny or 0)) + max(0.0, float(action.locked_budget_cny or 0) - remaining), 2)
    if status == "success":
        updates.update({"error_code": "", "error_message": ""})
        next_state = _manual_success_state(result)
        if next_state == PlatformActionState.WAITING_TRADE_OFFER:
            updates["next_check_at"] = time.time() + min(30, poll_interval)
        elif next_state == PlatformActionState.WAITING_PLATFORM:
            updates["next_check_at"] = time.time() + poll_interval
        transition_action(action, next_state, **updates)
    else:
        updates.update(
            {
                "error_code": _result_error_code(result),
                "error_message": result.message,
            }
        )
        transition_action(action, PlatformActionState.FAILED, **updates)


def _manual_action_poll_interval_seconds(action: PlatformAction) -> int:
    try:
        cfg = load_app_config()
        cash = cfg.get("cash_platform_trading") if isinstance(cfg.get("cash_platform_trading"), dict) else {}
        platforms = cash.get("platforms") if isinstance(cash.get("platforms"), dict) else {}
        platform_cfg = platforms.get(str(action.platform or "")) if isinstance(platforms.get(str(action.platform or "")), dict) else {}
        raw = platform_cfg.get("order_poll_interval_seconds", cash.get("order_poll_interval_seconds", 60))
        return max(15, min(int(raw or 60), 600))
    except Exception:
        return 60


def _append_purchase_target_summary(action_payload: dict, rows: list[PlatformAction]) -> dict:
    if not isinstance(action_payload, dict):
        return {}
    target_id = str(action_payload.get("purchase_target_id") or "").strip()
    if not target_id:
        return {}
    target_rows = []
    for row in rows:
        raw_context = row.raw_context
        if not raw_context or target_id not in str(raw_context):
            continue
        target_rows.append(row)
    if not target_rows:
        return {}
    return {
        "purchase_target_id": target_id,
        "target_quantity": max(int((json.loads(row.raw_context or "{}") if isinstance(row.raw_context, str) else {}).get("target_quantity") or 0) for row in target_rows),
        "filled_quantity": sum(int(row.filled_quantity or 0) for row in target_rows),
        "active_orders": sum(1 for row in target_rows if row.state not in TERMINAL_STATES),
    }


@app.get("/api/trade/platform_action_summary")
def api_trade_platform_action_summary():
    with get_session() as session:
        summary = _platform_action_summary_payload(session)
    return {"success": True, **summary}


def _automation_alerts(summary: dict, worker: dict, scanner: dict) -> list[dict]:
    alerts: list[dict] = []
    if worker.get("last_error"):
        alerts.append({"level": "danger", "kind": "worker_error", "message": str(worker.get("last_error"))})
    if scanner.get("last_error"):
        alerts.append({"level": "warning", "kind": "scanner_error", "message": str(scanner.get("last_error"))})
    due_count = int(summary.get("due_count") or 0)
    if due_count and not bool(worker.get("running")):
        alerts.append({
            "level": "warning",
            "kind": "due_worker_stopped",
            "count": due_count,
            "message": f"{due_count} 个动作已到期，但执行 worker 未运行",
        })
    waiting_count = sum(
        int((summary.get("by_state") or {}).get(state, {}).get("count") or 0)
        for state in (
            PlatformActionState.WAITING_PLATFORM,
            PlatformActionState.WAITING_TRADE_OFFER,
            PlatformActionState.WAITING_STEAM_CONFIRM,
            PlatformActionState.WAITING_SETTLEMENT,
            PlatformActionState.RETRY_WAIT,
        )
    )
    if waiting_count:
        alerts.append({
            "level": "info",
            "kind": "waiting_actions",
            "count": waiting_count,
            "message": f"{waiting_count} 个动作正在等待或重试",
        })
    risk_count = int((summary.get("by_state") or {}).get(PlatformActionState.RISK_BLOCKED, {}).get("count") or 0)
    if risk_count:
        alerts.append({
            "level": "warning",
            "kind": "risk_blocked",
            "count": risk_count,
            "message": f"{risk_count} 个动作被风控锁定",
        })
    locked = float(summary.get("active_locked_budget_cny") or 0)
    if locked > 0:
        alerts.append({
            "level": "info",
            "kind": "locked_budget",
            "locked_budget_cny": locked,
            "message": f"当前动作占用预算 ¥{locked:.2f}",
        })
    return alerts


@app.get("/api/trade/automation_overview")
def api_trade_automation_overview():
    worker = PLATFORM_ACTION_WORKER_RUNTIME.status()
    scanner = SELLER_SNAPSHOT_SCANNER_RUNTIME.status()
    with get_session() as session:
        summary = _platform_action_summary_payload(session)
    return {
        "success": True,
        "worker": worker,
        "scanner": scanner,
        "summary": summary,
        "alerts": _automation_alerts(summary, worker, scanner),
    }


def _live_canary_config_payload(cfg: dict) -> dict:
    canary = live_canary_config_from_app_config(cfg)
    return {
        "enabled": canary.enabled,
        "kill_switch": canary.kill_switch,
        "require_channel": canary.require_channel,
        "max_action_cny": canary.max_action_cny,
        "max_daily_cny": canary.max_daily_cny,
        "allowed_platforms": list(canary.allowed_platforms),
        "allowed_action_types": list(canary.allowed_action_types),
        "allowed_item_ids": list(canary.allowed_item_ids),
        "allowed_market_hash_names": list(canary.allowed_market_hash_names),
        "require_recent_smoke_seconds": canary.require_recent_smoke_seconds,
        "require_manual_run_once": canary.require_manual_run_once,
        "allow_background_worker": canary.allow_background_worker,
    }


def _live_canary_gate_payload(session, cfg: dict, *, limit: int = 1, now: float | None = None) -> dict:
    inspection = inspect_live_canary_run(
        session,
        cfg,
        limit=max(1, int(limit or 1)),
        smoke_registry=LIVE_CANARY_SMOKE_REGISTRY,
        now=now,
    )
    decision = inspection.decision
    return {
        "checked_at": inspection.checked_at,
        "limit": inspection.limit,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "message": decision.message,
        "action_id": decision.action_id,
        "required_capability": inspection.required_capability,
        "smoke_recent": inspection.smoke_recent,
        "next_action": _platform_action_to_dict(inspection.action) if inspection.action is not None else None,
    }


@app.get("/api/trade/live_canary/status")
def api_trade_live_canary_status():
    cfg = load_app_config()
    worker = PLATFORM_ACTION_WORKER_RUNTIME.status()
    with get_session() as session:
        summary = _platform_action_summary_payload(session)
        gate = _live_canary_gate_payload(session, cfg, limit=1)
    return {
        "success": True,
        "config": _live_canary_config_payload(cfg),
        "worker": worker,
        "summary": summary,
        "smoke": {
            "items": LIVE_CANARY_SMOKE_REGISTRY.snapshot(),
        },
        "gate": gate,
    }


@app.post("/api/trade/live_canary/precheck")
def api_trade_live_canary_precheck(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    try:
        limit = max(1, min(int(payload.get("limit") or 1), 100))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "limit must be an integer"})
    cfg = load_app_config()
    with get_session() as session:
        gate = _live_canary_gate_payload(session, cfg, limit=limit)
    return {
        "success": True,
        "safe_to_call_run_once": bool(gate.get("allowed")) and limit == 1,
        "recommended_run_once_payload": {"safe_mode": False, "limit": 1} if bool(gate.get("allowed")) and limit == 1 else None,
        "gate": gate,
    }


@app.post("/api/trade/platform_actions/run_once")
def api_trade_platform_actions_run_once(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = load_app_config()
    worker_config = platform_action_worker_config_from_app_config(cfg)
    safe_mode = bool(payload.get("safe_mode", worker_config.safe_mode))
    limit = max(1, min(int(payload.get("limit") or 10), 100))
    if not safe_mode:
        with get_session() as session:
            canary = validate_live_canary_run(
                session,
                cfg,
                limit=limit,
                smoke_registry=LIVE_CANARY_SMOKE_REGISTRY,
            )
        if not canary.allowed:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "reason": canary.reason,
                    "msg": canary.message,
                    "action_id": canary.action_id,
                },
            )
    credentials = _load_credentials()
    adapters = build_platform_adapters(credentials=credentials, config=cfg)
    worker = PlatformActionWorker(
        get_session,
        adapters=adapters,
        trade_offer_service=TradeOfferService(credentials=credentials, config=cfg),
        safe_mode=safe_mode,
        lease_seconds=worker_config.lease_seconds,
    )
    result = worker.run_once(limit=limit)
    return {
        "success": True,
        "safe_mode": safe_mode,
        "result": asdict(result),
    }


@app.post("/api/trade/reconcile")
def api_trade_reconcile(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    try:
        limit = max(1, min(int(payload.get("limit") or 50), 500))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "limit must be an integer"})
    platform = normalize_platform(str(payload.get("platform") or "")) if payload.get("platform") else ""
    item_id = payload.get("item_id")
    try:
        item_id = int(item_id) if item_id not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id must be an integer"})
    dry_run = bool(payload.get("dry_run", False))
    accept_trade_offers = bool(payload.get("accept_trade_offers", True))
    recover_failed = bool(payload.get("recover_failed", True))
    force = bool(payload.get("force", False))
    align_inventory = bool(payload.get("align_inventory", False))
    refresh_inventory = bool(payload.get("refresh_inventory", False))
    inventory = payload.get("inventory")
    if inventory is not None and not isinstance(inventory, list):
        return JSONResponse(status_code=400, content={"success": False, "msg": "inventory must be a list"})
    try:
        inventory_limit = max(1, min(int(payload.get("inventory_limit") or payload.get("align_inventory_limit") or 100), 1000))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "inventory_limit must be an integer"})

    worker_status = PLATFORM_ACTION_WORKER_RUNTIME.status()
    if bool(worker_status.get("running")) and not dry_run and not force:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "reason": "worker_running",
                "msg": "Stop the platform action worker before manual reconciliation, or pass force=true.",
            },
        )

    cfg = load_app_config()
    credentials = _load_credentials()
    service = PlatformActionReconciliationService(
        adapters=build_platform_adapters(credentials=credentials, config=cfg),
        trade_offer_service=TradeOfferService(credentials=credentials, config=cfg),
    )
    with get_session() as session:
        result = service.run(
            session,
            limit=limit,
            platform=platform,
            item_id=item_id,
            dry_run=dry_run,
            accept_trade_offers=accept_trade_offers,
            recover_failed=recover_failed,
        )
    inventory_result = None
    if align_inventory:
        inventory_rows = inventory
        if inventory_rows is None:
            if refresh_inventory:
                try:
                    from app.inventory_cs2 import scan_cs2_inventory

                    ok, rows, err = scan_cs2_inventory()
                except Exception as exc:
                    return JSONResponse(status_code=500, content={"success": False, "msg": f"inventory scan failed: {exc}"})
                if not ok:
                    return JSONResponse(status_code=502, content={"success": False, "msg": err or "inventory scan failed"})
                inventory_rows = rows or []
                set_inventory(inventory_rows)
            else:
                inventory_rows = get_inventory() or []
        with get_session() as session:
            try:
                inventory_result = InventoryAlignmentService().run(
                    session,
                    inventory=inventory_rows,
                    limit=inventory_limit,
                    dry_run=dry_run,
                )
            except Exception as exc:
                return JSONResponse(status_code=500, content={"success": False, "msg": str(exc)})
    return {
        "success": True,
        "dry_run": dry_run,
        "accept_trade_offers": accept_trade_offers,
        "recover_failed": recover_failed,
        "align_inventory": align_inventory,
        "refresh_inventory": refresh_inventory,
        "result": asdict(result),
        "inventory_alignment": asdict(inventory_result) if inventory_result is not None else None,
    }


@app.post("/api/trade/inventory_align")
def api_trade_inventory_align(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    try:
        limit = max(1, min(int(payload.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "limit must be an integer"})
    dry_run = bool(payload.get("dry_run", False))
    refresh_inventory = bool(payload.get("refresh_inventory", False))
    inventory = payload.get("inventory")
    if inventory is not None and not isinstance(inventory, list):
        return JSONResponse(status_code=400, content={"success": False, "msg": "inventory must be a list"})

    if inventory is None:
        if refresh_inventory:
            try:
                from app.inventory_cs2 import scan_cs2_inventory

                ok, rows, err = scan_cs2_inventory()
            except Exception as exc:
                return JSONResponse(status_code=500, content={"success": False, "msg": f"inventory scan failed: {exc}"})
            if not ok:
                return JSONResponse(status_code=502, content={"success": False, "msg": err or "inventory scan failed"})
            inventory = rows or []
            set_inventory(inventory)
        else:
            inventory = get_inventory() or []

    service = InventoryAlignmentService()
    with get_session() as session:
        try:
            result = service.run(
                session,
                inventory=inventory,
                limit=limit,
                dry_run=dry_run,
            )
        except Exception as exc:
            return JSONResponse(status_code=500, content={"success": False, "msg": str(exc)})
    return {
        "success": True,
        "dry_run": dry_run,
        "refresh_inventory": refresh_inventory,
        "result": asdict(result),
    }


@app.post("/api/trade/platform_actions/smoke")
def api_trade_platform_actions_smoke(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    safe_mode = bool(payload.get("safe_mode", True))
    live_preflight = bool(payload.get("live_preflight", False))
    if live_preflight and safe_mode:
        live_preflight = False
    platforms = payload.get("platforms")
    capabilities = payload.get("capabilities")
    if platforms is not None and not isinstance(platforms, list):
        return JSONResponse(status_code=400, content={"success": False, "msg": "platforms must be a list"})
    if capabilities is not None and not isinstance(capabilities, list):
        return JSONResponse(status_code=400, content={"success": False, "msg": "capabilities must be a list"})
    service = PlatformAutomationSmokeService(credentials=_load_credentials(), config=load_app_config())
    results = service.run(
        platforms=platforms,
        capabilities=capabilities,
        safe_mode=safe_mode,
        live_preflight=live_preflight,
    )
    LIVE_CANARY_SMOKE_REGISTRY.record_results(results)
    return {
        "success": True,
        "safe_mode": safe_mode,
        "live_preflight": live_preflight,
        "items": results,
        "ok": all(row.get("ok") for row in results),
    }


@app.get("/api/trade/platform_actions/worker_status")
def api_trade_platform_action_worker_status():
    return {"success": True, "worker": PLATFORM_ACTION_WORKER_RUNTIME.status()}


@app.post("/api/trade/platform_actions/worker_start")
def api_trade_platform_action_worker_start(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = _load_runtime_config()
    if not _auto_trading_enabled_from_config(cfg):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "reason": "auto_trading_disabled",
                "msg": "自动交易已暂停，请先通过模块控制启动交易。",
                "trading": _trading_status_payload(cfg),
            },
        )
    section = dict(cfg.get("trading_worker") or {})
    section["enabled"] = True
    for key in (
        "safe_mode",
        "poll_interval_seconds",
        "batch_size",
        "lease_seconds",
        "error_backoff_seconds",
    ):
        if key in payload:
            section[key] = payload[key]
    if not bool(section.get("safe_mode", True)):
        canary_config = live_canary_config_from_app_config(cfg)
        if not canary_config.allow_background_worker:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "reason": "live_canary_background_worker_blocked",
                    "msg": "Use manual run_once(limit=1) for first live canary actions.",
                },
            )
    started = PLATFORM_ACTION_WORKER_RUNTIME.start({"trading_worker": section, "SAFE_MODE_ENABLED": cfg.get("SAFE_MODE_ENABLED")})
    if started or PLATFORM_ACTION_WORKER_RUNTIME.status().get("running"):
        _save_trading_module_config(enabled=True, trading_worker=section)
    return {
        "success": True,
        "started": started,
        "worker": PLATFORM_ACTION_WORKER_RUNTIME.status(),
    }


@app.post("/api/trade/platform_actions/worker_stop")
def api_trade_platform_action_worker_stop(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    timeout_seconds = float(payload.get("timeout_seconds") or 5)
    stopped = PLATFORM_ACTION_WORKER_RUNTIME.stop(timeout_seconds=timeout_seconds)
    cfg = _load_runtime_config()
    section = dict(cfg.get("trading_worker") or {})
    section["enabled"] = False
    _save_trading_module_config(
        enabled=_auto_trading_enabled_from_config(cfg),
        trading_worker=section,
    )
    return {
        "success": True,
        "stopped": stopped,
        "worker": PLATFORM_ACTION_WORKER_RUNTIME.status(),
    }


@app.post("/api/trade/platform_actions/worker_wake")
def api_trade_platform_action_worker_wake():
    woke = PLATFORM_ACTION_WORKER_RUNTIME.wake()
    return {
        "success": True,
        "woke": woke,
        "worker": PLATFORM_ACTION_WORKER_RUNTIME.status(),
    }


@app.post("/api/trade/platform_actions")
def api_trade_create_platform_action(payload: dict):
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"success": False, "msg": "payload must be an object"})
    try:
        action_type = str(payload.get("action_type") or "").strip().lower()
        platform = normalize_platform(str(payload.get("platform") or ""))
        item_id = int(payload.get("item_id") or 0)
        market_hash_name = str(payload.get("market_hash_name") or "").strip()
        target_price = payload.get("target_price")
        target_price = float(target_price) if target_price is not None else None
        quantity = max(1, int(payload.get("quantity") or 1))
        expected_profit_rate = payload.get("expected_profit_rate")
        expected_profit_rate = float(expected_profit_rate) if expected_profit_rate is not None else None
        if not action_type or not platform or not item_id or not market_hash_name:
            return JSONResponse(status_code=400, content={"success": False, "msg": "action_type/platform/item_id/market_hash_name are required"})
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "msg": f"invalid platform action payload: {exc}"})

    with get_session() as session:
        if is_sell_side_action_type(action_type):
            try:
                result = SellerActionService().create_action(session, payload)
            except (TypeError, ValueError) as exc:
                return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})
            return {
                "success": True,
                "created": result.created,
                "risk": asdict(result.risk),
                "item": _platform_action_to_dict(result.action),
            }

        risk = RiskBudgetService().check_new_action(
            session,
            platform=platform,
            item_id=item_id,
            market_hash_name=market_hash_name,
            risk_category=str(payload.get("risk_category") or ""),
            target_price=target_price,
            quantity=quantity,
            locked_budget_cny=payload.get("locked_budget_cny"),
            expected_profit_rate=expected_profit_rate,
        )
        exposure_decision = LowPriceExposureGuard(load_app_config()).check(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=target_price or 0,
            proposed_quantity=quantity,
            fail_closed=True,
        )
        action, created = create_platform_action(
            session,
            action_type=action_type,
            platform=platform,
            item_id=item_id,
            market_hash_name=market_hash_name,
            risk_category=str(payload.get("risk_category") or ""),
            quantity=quantity,
            target_price=target_price,
            reference_price=payload.get("reference_price"),
            cost_basis_cny=payload.get("cost_basis_cny"),
            expected_profit_rate=expected_profit_rate,
            locked_budget_cny=risk.locked_budget_cny,
            channel=str(payload.get("channel") or "api"),
            next_check_at=payload.get("next_check_at"),
            idempotency_key=payload.get("idempotency_key"),
            request_payload=payload.get("request_payload"),
            raw_context=raw_context_with_test_signal(payload),
        )
        if not risk.allowed or not exposure_decision.allowed:
            error_code = risk.reason if not risk.allowed else exposure_decision.reason
            action.state = PlatformActionState.RISK_BLOCKED
            action.error_code = error_code
            action.error_message = error_code
            action.updated_at = time.time()
            session.add(action)
            session.commit()
            session.refresh(action)
        return {
            "success": True,
            "created": created,
            "risk": asdict(risk),
            "exposure_guard": exposure_decision.to_dict(),
            "item": _platform_action_to_dict(action),
        }


@app.post("/api/trade/purchase_target_actions")
def api_trade_create_purchase_target_actions(payload: dict):
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"success": False, "msg": "payload must be an object"})
    try:
        item_id = int(payload.get("item_id") or 0)
        market_hash_name = str(payload.get("market_hash_name") or "").strip()
        target_quantity = max(1, int(payload.get("target_quantity") or payload.get("quantity") or 1))
        max_unit_price = float(payload.get("max_unit_price") or payload.get("target_price") or 0)
        quotes = payload.get("quotes") or []
        if not item_id or not market_hash_name or max_unit_price <= 0:
            return JSONResponse(status_code=400, content={"success": False, "msg": "item_id/market_hash_name/max_unit_price are required"})
        if not isinstance(quotes, list):
            return JSONResponse(status_code=400, content={"success": False, "msg": "quotes must be a list"})
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "msg": f"invalid purchase target payload: {exc}"})
    with get_session() as session:
        exposure_decision = LowPriceExposureGuard(load_app_config()).check(
            session,
            item_id=item_id,
            market_hash_name=market_hash_name,
            unit_price=max_unit_price,
            proposed_quantity=target_quantity,
            fail_closed=True,
        )
        if not exposure_decision.allowed:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "msg": "low price exposure quota blocked this purchase target",
                    "reason": exposure_decision.reason,
                    "exposure_guard": exposure_decision.to_dict(),
                },
            )
        try:
            result = create_purchase_target_actions(
                session,
                item_id=item_id,
                market_hash_name=market_hash_name,
                target_quantity=target_quantity,
                max_unit_price=max_unit_price,
                quotes=quotes,
                default_order_price=payload.get("default_order_price"),
                channel=str(payload.get("channel") or "purchase_target_api"),
                request_payload=payload.get("request_payload") if isinstance(payload.get("request_payload"), dict) else {},
                raw_context=payload.get("raw_context") if isinstance(payload.get("raw_context"), dict) else {},
                target_id=payload.get("target_id"),
                next_check_at=payload.get("next_check_at"),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})
        return {
            "success": True,
            "target_id": result.target_id,
            "created": result.created_count,
            "existing": result.existing_count,
            "plan": {
                "target_quantity": result.plan.target_quantity,
                "direct_quantity": result.plan.direct_quantity,
                "order_quantity": result.plan.order_quantity,
                "remaining_quantity": result.plan.remaining_quantity,
                "actions": [asdict(action) for action in result.plan.actions],
            },
            "items": [_platform_action_to_dict(action) for action in result.actions],
        }


@app.post("/api/trade/seller_actions")
def api_trade_create_seller_actions(payload: dict):
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"success": False, "msg": "payload must be an object"})
    service = SellerActionService()
    if bool(payload.get("dry_run")):
        try:
            plan = service.plan_from_snapshot(payload)
        except (TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})
        return {
            "success": True,
            "dry_run": True,
            "planned": len(plan.actions),
            "actions": plan.actions,
            "skipped": plan.skipped,
        }
    actions_payload = payload.get("actions")
    if actions_payload is None:
        actions_payload = [payload]
    if not isinstance(actions_payload, list):
        return JSONResponse(status_code=400, content={"success": False, "msg": "actions must be a list"})
    items = []
    errors = []
    with get_session() as session:
        for index, row in enumerate(actions_payload):
            if not isinstance(row, dict):
                errors.append({"index": index, "msg": "action must be an object"})
                continue
            merged = dict(row)
            if payload.get("channel") and "channel" not in merged:
                merged["channel"] = payload.get("channel")
            try:
                result = service.create_action(session, merged)
                items.append({
                    "created": result.created,
                    "risk": asdict(result.risk),
                    "item": _platform_action_to_dict(result.action),
                })
            except (TypeError, ValueError) as exc:
                errors.append({"index": index, "msg": str(exc)})
    status_code = 200 if not errors else 400
    return JSONResponse(
        status_code=status_code,
        content={
            "success": not errors,
            "created": sum(1 for item in items if item.get("created")),
            "items": items,
            "errors": errors,
        },
    )


@app.post("/api/trade/seller_actions/plan")
def api_trade_plan_seller_actions(payload: dict):
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"success": False, "msg": "payload must be an object"})
    commit = bool(payload.get("commit"))
    service = SellerActionService()
    try:
        if not commit:
            plan = service.plan_from_snapshot(payload)
            return {
                "success": True,
                "committed": False,
                "planned": len(plan.actions),
                "actions": plan.actions,
                "skipped": plan.skipped,
            }
        with get_session() as session:
            result = service.plan_and_create(session, payload)
            return {
                "success": True,
                "committed": True,
                "planned": len(result.plan.actions),
                "created": sum(1 for row in result.created if row.created),
                "skipped": result.plan.skipped,
                "items": [
                    {
                        "created": row.created,
                        "risk": asdict(row.risk),
                        "item": _platform_action_to_dict(row.action),
                    }
                    for row in result.created
                ],
            }
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})


@app.post("/api/trade/seller_actions/scan")
def api_trade_scan_seller_actions(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    commit = bool(payload.get("commit"))
    scanner = SellerSnapshotScanner()
    try:
        if commit:
            result = scanner.plan_and_create(get_session, payload)
            return {
                "success": True,
                "committed": True,
                "diagnostics": result.scan.diagnostics,
                "snapshot_counts": {
                    "inventory": len(result.scan.snapshot.get("inventory") or []),
                    "active_assetids": len(result.scan.snapshot.get("active_assetids") or []),
                    "orders": len(result.scan.snapshot.get("orders") or []),
                },
                "planned": len(result.plan.actions),
                "created": sum(1 for row in result.created if row.created),
                "skipped": result.plan.skipped,
                "items": [
                    {
                        "created": row.created,
                        "risk": asdict(row.risk),
                        "item": _platform_action_to_dict(row.action),
                    }
                    for row in result.created
                ],
            }
        result = scanner.plan(payload)
        return {
            "success": True,
            "committed": False,
            "diagnostics": result.scan.diagnostics,
            "snapshot_counts": {
                "inventory": len(result.scan.snapshot.get("inventory") or []),
                "active_assetids": len(result.scan.snapshot.get("active_assetids") or []),
                "orders": len(result.scan.snapshot.get("orders") or []),
            },
            "planned": len(result.plan.actions),
            "actions": result.plan.actions,
            "skipped": result.plan.skipped,
        }
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})


@app.get("/api/trade/seller_actions/scanner_status")
def api_trade_seller_action_scanner_status():
    return {"success": True, "scanner": SELLER_SNAPSHOT_SCANNER_RUNTIME.status()}


@app.post("/api/trade/seller_actions/scanner_start")
def api_trade_seller_action_scanner_start(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = _load_runtime_config()
    if not _auto_trading_enabled_from_config(cfg):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "reason": "auto_trading_disabled",
                "msg": "自动交易已暂停，请先通过模块控制启动交易。",
                "trading": _trading_status_payload(cfg),
            },
        )
    section = dict(cfg.get("seller_snapshot_scanner") or {})
    section["enabled"] = True
    for key in (
        "commit",
        "interval_seconds",
        "error_backoff_seconds",
        "include_inventory",
        "include_steam_listings",
        "include_c5_orders",
        "listing_platform",
        "delivery_platform",
        "channel",
        "snapshot_payload",
    ):
        if key in payload:
            section[key] = payload[key]
    started = SELLER_SNAPSHOT_SCANNER_RUNTIME.start({"seller_snapshot_scanner": section})
    if started or SELLER_SNAPSHOT_SCANNER_RUNTIME.status().get("running"):
        _save_trading_module_config(enabled=True, seller_snapshot_scanner=section)
    return {
        "success": True,
        "started": started,
        "scanner": SELLER_SNAPSHOT_SCANNER_RUNTIME.status(),
    }


@app.post("/api/trade/seller_actions/scanner_stop")
def api_trade_seller_action_scanner_stop(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    timeout_seconds = float(payload.get("timeout_seconds") or 5)
    stopped = SELLER_SNAPSHOT_SCANNER_RUNTIME.stop(timeout_seconds=timeout_seconds)
    cfg = _load_runtime_config()
    section = dict(cfg.get("seller_snapshot_scanner") or {})
    section["enabled"] = False
    _save_trading_module_config(
        enabled=_auto_trading_enabled_from_config(cfg),
        seller_snapshot_scanner=section,
    )
    return {
        "success": True,
        "stopped": stopped,
        "scanner": SELLER_SNAPSHOT_SCANNER_RUNTIME.status(),
    }


@app.post("/api/trade/seller_actions/scanner_wake")
def api_trade_seller_action_scanner_wake():
    woke = SELLER_SNAPSHOT_SCANNER_RUNTIME.wake()
    return {
        "success": True,
        "woke": woke,
        "scanner": SELLER_SNAPSHOT_SCANNER_RUNTIME.status(),
    }


@app.post("/api/trade/seller_actions/scanner_run_once")
def api_trade_seller_action_scanner_run_once(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = load_app_config()
    section = dict(cfg.get("seller_snapshot_scanner") or {})
    for key in (
        "commit",
        "include_inventory",
        "include_steam_listings",
        "include_c5_orders",
        "listing_platform",
        "delivery_platform",
        "channel",
        "snapshot_payload",
    ):
        if key in payload:
            section[key] = payload[key]
    inline_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "enabled",
            "commit",
            "interval_seconds",
            "error_backoff_seconds",
            "include_inventory",
            "include_steam_listings",
            "include_c5_orders",
            "listing_platform",
            "delivery_platform",
            "channel",
            "snapshot_payload",
        }
    }
    if inline_payload:
        snapshot_payload = dict(section.get("snapshot_payload") or {})
        snapshot_payload.update(inline_payload)
        section["snapshot_payload"] = snapshot_payload
    try:
        runtime_config = seller_snapshot_scanner_config_from_app_config({"seller_snapshot_scanner": section})
        result = SELLER_SNAPSHOT_SCANNER_RUNTIME.run_once(runtime_config)
        return {
            "success": True,
            "committed": runtime_config.commit,
            "diagnostics": result.scan.diagnostics,
            "snapshot_counts": {
                "inventory": len(result.scan.snapshot.get("inventory") or []),
                "active_assetids": len(result.scan.snapshot.get("active_assetids") or []),
                "orders": len(result.scan.snapshot.get("orders") or []),
            },
            "planned": len(result.plan.actions),
            "created": sum(1 for row in result.created if row.created),
            "actions": result.plan.actions if not runtime_config.commit else [],
            "skipped": result.plan.skipped,
            "scanner": SELLER_SNAPSHOT_SCANNER_RUNTIME.status(),
        }
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"success": False, "msg": str(exc)})


@app.post("/api/trade/cancel_steam_order")
def api_trade_cancel_steam_order(payload: dict):
    order_id = payload.get("order_id") if isinstance(payload, dict) else None
    if order_id is None:
        return JSONResponse(status_code=400, content={"success": False, "msg": "order_id 娑撳秷鍏樻稉铏光敄"})
    try:
        order_id = str(order_id).strip()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "msg": "order_id 閺嶇厧绱￠柨娆掝嚖"})

    steam_cookie = _normalize_steam_cookie()
    if not steam_cookie:
        return JSONResponse(status_code=400, content={"success": False, "msg": "missing Steam cookie"})

    buyer = SteamBuyer(cookie_str=steam_cookie)
    try:
        result = buyer.cancel_buy_order(order_id)
        if not result.success:
            return JSONResponse(status_code=500, content={"success": False, "msg": result.msg, "detail": result.raw})
        return {"success": True, "msg": result.msg, "detail": result.raw}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": f"閹俱倕宕熸径杈Е: {exc}"})


def _engine_log_files() -> list[Path]:
    log_dir = LOG_PATH.parent
    if not log_dir.exists():
        return [LOG_PATH]
    archives = sorted(
        [p for p in log_dir.glob("aetherswap_engine.log.*") if p.is_file()],
        key=lambda p: p.name,
    )
    files = [p for p in archives if p.exists()]
    if LOG_PATH.exists():
        files.append(LOG_PATH)
    return files or [LOG_PATH]


def _read_engine_log_lines(since: int = 0, limit: int = 300) -> list[dict]:
    max_lines = max(1, min(int(limit or 300), 5000))
    files = [fp for fp in _engine_log_files() if fp.exists()]
    if not files:
        return []

    collected: list[tuple[int, str]] = []
    remaining = max_lines
    for fp in reversed(files):
        if remaining <= 0:
            break
        try:
            collected[0:0] = _tail_file_lines(fp, remaining)
        except Exception:
            continue
        remaining = max_lines - len(collected)
    if since > 0:
        collected = [(idx, line) for idx, line in collected if idx > since]
    sliced = collected[-max_lines:]
    out: list[dict] = []
    for idx, line in sliced:
        out.append({"id": idx, "t": time.time(), "level": "info", "msg": line})
    return out


def _tail_file_lines(path: Path, max_lines: int, block_size: int = 65536) -> list[tuple[int, str]]:
    """Read only the tail of large log files and use byte offsets as stable ids."""
    max_lines = max(1, max_lines)
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        pos = end
        chunks: list[bytes] = []
        newline_count = 0
        while pos > 0 and newline_count <= max_lines:
            read_size = min(block_size, pos)
            pos -= read_size
            fh.seek(pos)
            chunk = fh.read(read_size)
            chunks.insert(0, chunk)
            newline_count += chunk.count(b"\n")
        base_offset = pos
    data = b"".join(chunks)
    if not data:
        return []
    lines: list[tuple[int, str]] = []
    cursor = base_offset
    for raw in data.splitlines(keepends=True):
        text = raw.rstrip(b"\r\n").decode("utf-8", errors="ignore")
        line_id = cursor + 1
        cursor += len(raw)
        if text:
            lines.append((line_id, text))
    return lines[-max_lines:]


@app.get("/api/logs")
def api_logs(lines: int = 300):
    return {"lines": _read_engine_log_lines(0, lines)}


@app.get("/api/log")
def api_log(since: int = 0, limit: int = 300):
    return {"lines": _read_engine_log_lines(since, limit)}


@app.post("/api/log/clear")
def api_log_clear():
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/log/export")
def api_log_export():
    try:
        if not LOG_PATH.exists():
            return {"ok": False, "error": "log file not found"}
        export_path = LOG_PATH.parent / f"aetherswap_engine_{int(time.time())}.log"
        export_path.write_text(LOG_PATH.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        return {"ok": True, "path": str(export_path), "lines": len(LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/tasks")
def api_tasks(limit: int = 50):
    q = get_task_queue()
    return {"tasks": q.list_tasks(limit=limit), "active": q.active_count()}


def _load_credentials() -> dict:
    if not _CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8") or "{}") or {}
    except Exception:
        return {}


def _save_credentials(data: dict) -> None:
    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _create_trade_record(**kwargs) -> int:
    with get_session() as session:
        record = TradeExecutionRecord(
            created_at=time.time(),
            action=kwargs.get("action", ""),
            channel=kwargs.get("channel", "ui"),
            item_id=int(kwargs.get("item_id") or 0),
            market_hash_name=str(kwargs.get("market_hash_name") or ""),
            platform=str(kwargs.get("platform") or ""),
            quantity=int(kwargs.get("quantity") or 1),
            target_price=kwargs.get("target_price"),
            reference_price=kwargs.get("reference_price"),
            status=str(kwargs.get("status") or "queued"),
            request_payload=json.dumps(kwargs.get("request_payload") or {}, ensure_ascii=False),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return int(record.id)


def _update_trade_record(record_id: int, **kwargs) -> None:
    should_notify = False
    notify_payload = None
    with get_session() as session:
        record = session.get(TradeExecutionRecord, record_id)
        if not record:
            return
        previous_status = str(record.status or "")
        if "status" in kwargs and kwargs["status"] is not None:
            record.status = str(kwargs["status"])
        if "response_payload" in kwargs and kwargs["response_payload"] is not None:
            record.response_payload = json.dumps(kwargs["response_payload"], ensure_ascii=False) if not isinstance(kwargs["response_payload"], str) else kwargs["response_payload"]
        if "error_message" in kwargs:
            record.error_message = str(kwargs["error_message"] or "")
        if previous_status != "success" and record.status == "success":
            should_notify = True
            notify_payload = {
                "item_name": record.market_hash_name,
                "action": record.action,
                "price": float(record.target_price or 0),
                "platform": record.platform,
                "quantity": int(record.quantity or 1),
                "extra": {"record_id": record.id, "channel": record.channel},
            }
        session.add(record)
        session.commit()
    if should_notify and notify_payload:
        _notify_trade_success_safe(**notify_payload)


def _record_platform_action_from_manual_buy(
    *,
    action: str,
    platform: str,
    item_id: int,
    market_hash_name: str,
    quantity: int,
    target_price: float,
    payload: dict,
    record_id: int,
    platform_item_id=None,
) -> int | None:
    try:
        safe_payload = _manual_buy_request_payload(
            payload=payload or {},
            platform=platform,
            platform_item_id=platform_item_id,
            market_hash_name=market_hash_name,
        )
        with get_session() as session:
            platform_action, _ = create_platform_action(
                session,
                action_type=str(action or PlatformActionType.PURCHASE_ORDER).strip().lower(),
                platform=platform,
                item_id=item_id,
                market_hash_name=market_hash_name,
                quantity=quantity,
                target_price=target_price,
                cost_basis_cny=target_price,
                channel="ui_manual_buy",
                idempotency_key=f"manual_buy:{record_id}",
                request_payload=safe_payload,
                raw_context={
                    "trade_execution_record_id": record_id,
                    "cost_batch_id": f"manual_buy:{record_id}",
                    "unit_cost_cny": target_price,
                },
            )
            transition_action(
                platform_action,
                PlatformActionState.PROCESSING,
            )
            transition_action(
                platform_action,
                PlatformActionState.WAITING_PLATFORM,
                next_check_at=time.time() + 10 * 365 * 24 * 3600,
                raw_context={
                    "trade_execution_record_id": record_id,
                    "compatibility_bridge": True,
                    "manual_buy_action": action,
                    "cost_batch_id": f"manual_buy:{record_id}",
                    "unit_cost_cny": target_price,
                },
            )
            session.add(platform_action)
            session.commit()
            session.refresh(platform_action)
            return int(platform_action.id) if platform_action.id else None
    except Exception as exc:
        log(f"platform_action bridge failed | record_id={record_id} err={exc}", level="warning")
        return None


def _update_platform_action_from_trade_result(platform_action_id: int | None, *, status: str, result: dict | None = None, error_message: str = "") -> None:
    if not platform_action_id:
        return
    try:
        with get_session() as session:
            action = session.get(PlatformAction, int(platform_action_id))
            if not action:
                return
            if status == "success":
                transition_action(
                    action,
                    PlatformActionState.SUCCEEDED,
                    response_payload=result or {},
                    error_code="",
                    error_message="",
                )
            elif status == "failed":
                transition_action(
                    action,
                    PlatformActionState.FAILED,
                    response_payload=result or {},
                    error_code=str((result or {}).get("reason") or (result or {}).get("code") or "manual_buy_failed"),
                    error_message=error_message or str((result or {}).get("msg") or ""),
                )
            session.add(action)
            session.commit()
    except Exception as exc:
        log(f"platform_action result bridge failed | platform_action_id={platform_action_id} err={exc}", level="warning")


def _notify_trade_success_safe(*, item_name: str, action: str, price: float, platform: str, quantity: int, extra: dict | None = None) -> None:
    try:
        notify_trade_success(
            item_name=item_name,
            action=action,
            price=price,
            platform=platform,
            quantity=quantity,
            extra=extra or {},
        )
    except Exception as exc:
        log(f"trade notifier failed | platform={platform} item={item_name} err={exc}", level="warning")


def _normalize_steam_cookie() -> str:
    creds = _load_credentials().get("steam") or {}
    return str(creds.get("cookies") or creds.get("cookie") or "").strip()


def _extract_credentials_payload(data: dict) -> dict:
    def _normalize_node(node: dict) -> dict:
        if not isinstance(node, dict):
            return {}
        out = dict(node)
        cookies = str(out.get("cookies", out.get("cookie", "")) or "").strip()
        if cookies:
            out["cookies"] = cookies
        return out

    return {
        "buff": _normalize_node(data.get("buff", {}) or {}),
        "uuyp": _normalize_node(data.get("uuyp", {}) or {}),
        "eco": _normalize_node(data.get("eco", {}) or {}),
        "steamdt_openapi": {
            "api_key": str(((data.get("steamdt_openapi") or {}).get("api_key") or "")).strip()
        },
    }


def _cookie_present(data: dict, platform: str) -> bool:
    node = data.get(platform) or {}
    if not isinstance(node, dict):
        return False
    return bool(str(node.get("cookies", "") or node.get("cookie", "") or "").strip())


@app.get("/api/credentials")
def api_get_credentials():
    data = _load_credentials()
    return _extract_credentials_payload(data)


@app.post("/api/credentials")
def api_save_credentials(payload: dict):
    current = _load_credentials()
    incoming = _extract_credentials_payload(payload or {})
    for key in ("buff", "uuyp", "eco", "steamdt_openapi"):
        current[key] = incoming.get(key, {})
    _save_credentials(current)
    return {"success": True}


def _tail_log_lines(path: Path, max_lines: int = 2500) -> list[str]:
    if not path.exists():
        return []
    q: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                q.append(line.rstrip("\n"))
    except Exception:
        return []
    return list(q)


def _extract_ts(line: str) -> str:
    parts = line.split("|", 1)
    return parts[0].strip() if parts else ""


@app.get("/api/platform/connectivity")
def api_platform_connectivity():
    platforms = ["steam", "buff", "uuyp", "eco", "steamdt_openapi", "steamdt_openapi_price"]
    state: dict[str, dict] = {
        p: {
            "platform": p,
            "status": "unknown",
            "rows": 0,
            "saved": 0,
            "reason": "",
            "updated_at": "",
            "cost_seconds": None,
        }
        for p in platforms
    }

    lines = _tail_log_lines(LOG_PATH, max_lines=3000)

    re_committed = re.compile(r"platform results committed \| platform=(\w+) rows=(\d+) saved=(\d+)")
    re_timeout = re.compile(r"platform fetch timeout, skipped \| platform=(\w+).*timeout=(\d+)s")
    re_finished = re.compile(r"platform task finished \| platform=(\w+) rows=(\d+) cost=([\d.]+)s")
    re_fetch_done = re.compile(r"platform fetch done \| platform=(\w+) results=(\d+) cost=([\d.]+)s")
    re_task_failed = re.compile(r"platform task failed \| platform=(\w+).*err=(.*)$")

    has_any_signal = False
    for line in lines:
        ts = _extract_ts(line)

        m = re_committed.search(line)
        if m:
            has_any_signal = True
            p, rows, saved = m.group(1).lower(), int(m.group(2)), int(m.group(3))
            if p in state:
                state[p].update({"status": "ok", "rows": rows, "saved": saved, "reason": "", "updated_at": ts})
            continue

        m = re_timeout.search(line)
        if m:
            has_any_signal = True
            p, timeout_s = m.group(1).lower(), m.group(2)
            if p in state:
                state[p].update({"status": "timeout", "rows": 0, "saved": 0, "reason": f"timeout {timeout_s}s", "updated_at": ts})
            continue

        m = re_finished.search(line)
        if m:
            has_any_signal = True
            p, rows, cost = m.group(1).lower(), int(m.group(2)), float(m.group(3))
            if p in state:
                state[p]["rows"] = rows
                state[p]["cost_seconds"] = cost
                state[p]["updated_at"] = ts
                if state[p]["status"] == "unknown":
                    state[p]["status"] = "running"
            continue

        m = re_fetch_done.search(line)
        if m:
            has_any_signal = True
            p, results, cost = m.group(1).lower(), int(m.group(2)), float(m.group(3))
            if p in state:
                state[p]["rows"] = results
                state[p]["cost_seconds"] = cost
                state[p]["updated_at"] = ts
                if state[p]["status"] == "unknown":
                    state[p]["status"] = "running"
            continue

        m = re_task_failed.search(line)
        if m:
            has_any_signal = True
            p, err = m.group(1).lower(), (m.group(2) or "").strip()
            if p in state:
                state[p].update({"status": "error", "reason": err[:220], "updated_at": ts})
            continue

        if "[steamdt-openapi]" in line:
            has_any_signal = True
            s = state["steamdt_openapi"]
            s["updated_at"] = ts
            if "sync done" in line:
                s["status"] = "ok"
                s["reason"] = ""
            elif "missing_api_key" in line:
                s["status"] = "missing_key"
                s["reason"] = "missing api key"
            elif "reason=disabled" in line:
                s["status"] = "disabled"
                s["reason"] = "disabled"
            elif "request failed" in line or "business error" in line:
                s["status"] = "error"
                s["reason"] = line.split("|")[-1].strip()[:220]

        # platform specific warning fallback
        if "WARNING" in line or "ERROR" in line:
            for p in ("steam", "buff", "uuyp", "eco"):
                if f"DataEngine.{p}_public_monitor" in line and p in state:
                    has_any_signal = True
                    if state[p]["status"] in {"unknown", "running"}:
                        state[p]["status"] = "degraded"
                    state[p]["reason"] = line.split("|")[-1].strip()[:220]
                    state[p]["updated_at"] = ts

    cfg = load_app_config() or {}
    steamdt_cfg = cfg.get("steamdt") if isinstance(cfg.get("steamdt"), dict) else {}
    openapi_cfg = steamdt_cfg.get("openapi") if isinstance(steamdt_cfg.get("openapi"), dict) else {}
    state["steamdt_openapi"]["enabled"] = bool(openapi_cfg.get("enabled", steamdt_cfg.get("openapi_enabled", False)))
    openapi_price_cfg = steamdt_cfg.get("openapi_price") if isinstance(steamdt_cfg.get("openapi_price"), dict) else {}
    state["steamdt_openapi_price"]["enabled"] = bool(openapi_price_cfg.get("enabled", False))
    for p in ("steam", "buff", "uuyp", "eco"):
        p_cfg = cfg.get(p) if isinstance(cfg.get(p), dict) else {}
        state[p]["enabled"] = bool(p_cfg.get("enabled", True))
    try:
        if _PLATFORM_RUNTIME_STATE_PATH.exists():
            payload = json.loads(_PLATFORM_RUNTIME_STATE_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(payload, dict):
                for p in ("steam", "buff", "uuyp", "eco", "steamdt_openapi"):
                    node = payload.get(p) if isinstance(payload.get(p), dict) else {}
                    if not node:
                        continue
                    s = state[p]
                    s["status"] = str(node.get("status") or s["status"])
                    s["rows"] = int(node.get("rows") or s["rows"] or 0)
                    s["saved"] = int(node.get("saved") or s["saved"] or 0)
                    s["reason"] = str(node.get("reason") or s["reason"] or "")
                    s["updated_at"] = str(node.get("updated_at") or s["updated_at"] or "")
                    cost = node.get("cost_seconds")
                    s["cost_seconds"] = float(cost) if cost is not None else s["cost_seconds"]
                    has_any_signal = True
        if _STEAMDT_OPENAPI_PRICE_STATE_PATH.exists():
            payload = json.loads(_STEAMDT_OPENAPI_PRICE_STATE_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(payload, dict):
                s = state["steamdt_openapi_price"]
                s["status"] = str(payload.get("status") or "unknown")
                s["rows"] = int(payload.get("rows") or 0)
                s["saved"] = int(payload.get("saved") or 0)
                s["reason"] = str(payload.get("reason") or "")
                s["updated_at"] = str(payload.get("updated_at") or "")
                cost = payload.get("cost_seconds")
                s["cost_seconds"] = float(cost) if cost is not None else None
                has_any_signal = True
    except Exception:
        pass
    if not has_any_signal:
        for p in ("steam", "buff", "uuyp", "eco"):
            state[p]["status"] = "no_data"
            state[p]["reason"] = "no platform task log detected"

    for p in platforms:
        if state[p]["status"] == "no_data":
            enabled = bool(state[p].get("enabled", True))
            state[p]["status"] = "idle" if enabled else "disabled"
            state[p]["reason"] = "waiting for next run" if enabled else "disabled by config"
        if state[p]["status"] == "unknown":
            enabled = bool(state[p].get("enabled", True))
            state[p]["status"] = "idle" if enabled else "disabled"
            if not state[p]["reason"]:
                state[p]["reason"] = "waiting for first signal" if enabled else "disabled by config"

    return {
        "success": True,
        "source": str(LOG_PATH),
        "note": "log_not_found" if not lines else "",
        "platforms": [state[p] for p in platforms],
    }


@app.post("/api/auth/relogin_start/{platform}")
async def api_relogin_start(platform: str):
    await start_login_browser(platform)
    return {"success": True, "msg": f"opened {platform} login browser"}


@app.post("/api/auth/relogin_finish/{platform}")
async def api_relogin_finish(platform: str):
    platform = str(platform or "").strip().lower()
    if platform == "steamdt":
        capsule_payload = await finish_login_and_extract_capsule("steamdt")
        capsule = register_steamdt_capsule_from_cookie(
            str(capsule_payload.get("cookie_header") or ""),
            user_agent=str(capsule_payload.get("user_agent") or ""),
            device_id=str(capsule_payload.get("device_id") or ""),
            proxy_binding=str(capsule_payload.get("proxy_binding") or "direct"),
            notes="browser relogin import",
        )
        return {
            "success": True,
            "msg": "SteamDT session capsule imported",
            "capsule": {
                "capsule_id": capsule.capsule_id,
                "platform": capsule.platform,
                "device_id": capsule.device_id,
                "proxy_binding": capsule.proxy_binding,
                "status": capsule.status,
            },
        }

    capsule_payload = await finish_login_and_extract_capsule(platform)
    cookie_str = str(capsule_payload.get("cookie_header") or "").strip()
    if cookie_str and not cookie_str.endswith(";"):
        cookie_str += ";"
    current = _load_credentials()
    node = current.get(platform) if isinstance(current.get(platform), dict) else {}
    node = dict(node or {})
    node["cookies"] = cookie_str
    if platform == "steam":
        trade_link = str(capsule_payload.get("trade_link") or "").strip()
        if trade_link:
            node["trade_link"] = trade_link
        session_id = str((capsule_payload.get("cookies") or {}).get("sessionid") or "").strip()
        if session_id:
            node["session_id"] = session_id
    current[platform] = node
    _save_credentials(current)
    return {
        "success": True,
        "msg": f"{platform} cookies saved",
        "cookies": cookie_str,
        "trade_link_saved": bool(platform == "steam" and node.get("trade_link")),
    }


@app.get("/api/session_capsules/{platform}")
def api_session_capsules(platform: str, include_retired: bool = False):
    normalized = str(platform or "").strip().lower()
    pool = SessionCapsulePool(_SESSION_CAPSULES_PATH)
    items = []
    for capsule in pool.list_capsules(normalized, include_retired=include_retired):
        items.append(
            {
                "capsule_id": capsule.capsule_id,
                "platform": capsule.platform,
                "status": capsule.status,
                "device_id": capsule.device_id,
                "proxy_binding": capsule.proxy_binding,
                "created_at": capsule.created_at,
                "last_used_at": capsule.last_used_at,
                "last_ok_at": capsule.last_ok_at,
                "fail_count": capsule.fail_count,
                "consecutive_auth_failures": capsule.consecutive_auth_failures,
                "failure_streak_reason": capsule.failure_streak_reason,
                "failure_streak_count": capsule.failure_streak_count,
                "cooldown_until": capsule.cooldown_until,
                "retire_reason": capsule.retire_reason,
                "last_failure_reason": capsule.last_failure_reason,
                "maintenance_alerted_at": capsule.maintenance_alerted_at,
                "notes": capsule.notes,
            }
        )
    return {"success": True, "summary": pool.status_summary(normalized), "items": items}


@app.post("/api/session_capsules/{platform}/{capsule_id}/clear_cooldown")
def api_session_capsule_clear_cooldown(platform: str, capsule_id: str):
    normalized = str(platform or "").strip().lower()
    pool = SessionCapsulePool(_SESSION_CAPSULES_PATH)
    capsule = pool.clear_cooldown(normalized, str(capsule_id or "").strip())
    if capsule is None:
        return JSONResponse(status_code=404, content={"success": False, "msg": "session capsule not found"})
    return {"success": True, "msg": "cooldown cleared", "summary": pool.status_summary(normalized)}


@app.post("/api/session_capsules/{platform}/{capsule_id}/retire")
def api_session_capsule_retire(platform: str, capsule_id: str, payload: dict | None = None):
    normalized = str(platform or "").strip().lower()
    reason = "manual_retire"
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or reason)
    pool = SessionCapsulePool(_SESSION_CAPSULES_PATH)
    capsule = pool.retire_capsule(normalized, str(capsule_id or "").strip(), reason=reason)
    if capsule is None:
        return JSONResponse(status_code=404, content={"success": False, "msg": "session capsule not found"})
    return {"success": True, "msg": "session capsule retired", "summary": pool.status_summary(normalized)}


@app.post("/api/credentials/test")
def api_test_credentials(payload: dict):
    incoming = _extract_credentials_payload(payload or {})
    result = {}

    # Buff閿涙俺鐨熼悽銊吂閸楁洖宸婚崣鍙夌叀鐠囶澁绱濋懗鑺ユ箒閺佸牆灏崚鍡忊偓婊冨嚒閻ц缍嶉垾婵呯瑢閳ユ粏顫︽搴㈠付/鏉╁洦婀￠垾?
    try:
        from buff import BuffBuyer, BuffAuthExpired

        buff_cookie = str((incoming.get("buff") or {}).get("cookies", "")).strip()
        if not buff_cookie:
            result["buff"] = {"ok": False, "msg": "閺堫亪鍘ょ純?Cookie"}
        else:
            buyer = BuffBuyer(cookie_str=buff_cookie)
            try:
                ok = bool(buyer.check_wait_pay_orders(game="csgo"))
                result["buff"] = {"ok": True, "msg": "Buff 閻ц缍嶉張澶嬫櫏", "detail": {"wait_pay": ok}}
            except BuffAuthExpired:
                result["buff"] = {"ok": False, "msg": "Buff auth expired"}
            except Exception as exc:
                result["buff"] = {"ok": False, "msg": f"Buff 妤犲矁鐦夌拠閿嬬湴瀵倸鐖? {exc}"}
    except Exception as exc:
        result["buff"] = {"ok": False, "msg": f"Buff 妤犲矁鐦夐崳銊ュ灥婵瀵叉径杈Е: {exc}"}

    # UUYP閿涙俺鐨熼悽銊ョ磻濠ф劙銆嶉惄顔兼倱濞嗗墽鏁ら幋铚備繆閹垱甯撮崣锝嗗赴濞?
    try:
        from uuyp import UuypBuyer

        uuyp_node = incoming.get("uuyp") or {}
        uuyp_cookie = str(uuyp_node.get("cookies", "")).strip()

        if not uuyp_cookie:
            result["uuyp"] = {"ok": False, "msg": "閺堫亪鍘ょ純?Cookie"}
        else:
            buyer = UuypBuyer(cookie_str=uuyp_cookie)
            try:
                # 娴ｈ法鏁ゅ鈧┃鎰般€嶉惄顔兼倱濞嗛箖鐛欑拠浣瑰复閸?
                res = buyer._request("GET", "https://api.youpin898.com/api/user/Account/getUserInfo")
                code = str(res.get("Code", res.get("code", ""))).strip()
                ok = code in {"0", "200", "OK", "ok", "Success", "success"} or res.get("success") is True
                # 閸欏矂鍣告穱婵嬫珦閿涙艾褰х憰浣规箒閻劍鍩涙穱鈩冧紖鏉╂柨娲栭崡鍏呰礋閹存劕濮?
                if "Data" in res and isinstance(res["Data"], dict) and "UserId" in res["Data"]:
                    ok = True

                result["uuyp"] = {
                    "ok": ok,
                    "msg": "UUYP 閻ц缍嶉張澶嬫櫏" if ok else f"UUYP 鏉╂柨娲栧鍌氱埗: {res}",
                    "detail": res,
                }
            except Exception as exc:
                result["uuyp"] = {"ok": False, "msg": f"UUYP 妤犲矁鐦夌拠閿嬬湴瀵倸鐖? {exc}"}
    except Exception as exc:
        result["uuyp"] = {"ok": False, "msg": f"UUYP 妤犲矁鐦夐崳銊ュ灥婵瀵叉径杈Е: {exc}"}

    # ECO閿涙矮鎷辩€瑰墎缍夋い?Cookie 娴犲懎浠涙稉濠氥€?閻劍鍩涢幒銉ュ經鏉╃偤鈧碍鈧勫赴濞村绱濇稉宥堢殶閻劌鏅㈤幋?OpenAPI
    try:
        import requests

        eco_cookie = str((incoming.get("eco") or {}).get("cookies", "")).strip()
        if not eco_cookie:
            result["eco"] = {"ok": False, "msg": "閺堫亪鍘ょ純?Cookie"}
        else:
            try:
                session = requests.Session()
                session.headers.update(
                    {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://www.ecosteam.cn/",
                        "Origin": "https://www.ecosteam.cn",
                    }
                )
                cookie_dict = {}
                for part in eco_cookie.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    k, _, v = part.partition("=")
                    cookie_dict[k.strip()] = v.strip()
                session.cookies.update(cookie_dict)
                res = session.get("https://www.ecosteam.cn/", timeout=10)
                result["eco"] = {
                    "ok": bool(res.ok),
                    "msg": "ECO 閻ц缍嶉張澶嬫櫏" if res.ok else f"ECO 娑撳銆夌拠閿嬬湴婢惰精瑙? {res.status_code}",
                    "detail": {"status_code": res.status_code, "url": res.url},
                }
            except Exception as exc:
                result["eco"] = {"ok": False, "msg": f"ECO 妤犲矁鐦夌拠閿嬬湴瀵倸鐖? {exc}"}
    except Exception as exc:
        result["eco"] = {"ok": False, "msg": f"ECO 妤犲矁鐦夐崳銊ュ灥婵瀵叉径杈Е: {exc}"}

    return {"success": True, "result": result}


def _opportunity_direction(buy_platform: str, sell_platform: str) -> str:
    buy_platform = str(buy_platform or "").strip().lower()
    sell_platform = str(sell_platform or "").strip().lower()
    if buy_platform == "steam" and sell_platform and sell_platform != "steam":
        return "steam_to_cash"
    if sell_platform == "steam" and buy_platform and buy_platform != "steam":
        return "cash_to_steam"
    return "unknown"


def _display_opportunity_profit(opportunity: ArbitrageOpportunity, app_config: dict | None = None) -> tuple[float, float]:
    buy_platform = str(opportunity.buy_platform or "").strip().lower()
    sell_platform = str(opportunity.sell_platform or "").strip().lower()
    buy_price = float(opportunity.buy_price or 0)
    sell_price = float(opportunity.sell_price or 0)
    if buy_price > 0 and sell_price > 0 and buy_platform and sell_platform:
        try:
            math = opportunity_profit(
                buy_platform=buy_platform,
                sell_platform=sell_platform,
                buy_price=buy_price,
                sell_price=sell_price,
                balance_cost_ratio=steam_balance_cost_ratio(app_config or {}),
            )
            return round(math.profit_rate * 100.0, 2), round(math.profit_cny, 4)
        except Exception:
            pass
    raw_rate = float(opportunity.profit_rate or 0)
    display_rate = raw_rate * 100.0 if abs(raw_rate) <= 1 else raw_rate
    return round(display_rate, 2), float(opportunity.profit_cny or 0)


def _query_open_opportunities(limit: int = 320):
    with SessionLocal() as session:
        rows = []
        seen_ids: set[int] = set()
        BuyMarketPrice = aliased(MarketPrice)
        SellMarketPrice = aliased(MarketPrice)
        status_batches = [
            (["open"], 200),
            (["verifying"], 100),
            (["success", "failed"], 40),
        ]
        for statuses, batch_limit in status_batches:
            stmt = (
                select(ArbitrageOpportunity, ItemBase, RadarSnapshot)
                .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
                .outerjoin(RadarSnapshot, RadarSnapshot.item_id == ArbitrageOpportunity.item_id)
                .outerjoin(
                    BuyMarketPrice,
                    (BuyMarketPrice.item_id == ArbitrageOpportunity.item_id)
                    & (func.lower(BuyMarketPrice.platform_name) == func.lower(ArbitrageOpportunity.buy_platform)),
                )
                .outerjoin(
                    SellMarketPrice,
                    (SellMarketPrice.item_id == ArbitrageOpportunity.item_id)
                    & (func.lower(SellMarketPrice.platform_name) == func.lower(ArbitrageOpportunity.sell_platform)),
                )
                .where(ArbitrageOpportunity.status.in_(statuses))
                .where(or_(BuyMarketPrice.id.is_(None), func.lower(BuyMarketPrice.data_source) != "baseline"))
                .where(or_(SellMarketPrice.id.is_(None), func.lower(SellMarketPrice.data_source) != "baseline"))
                .order_by(ArbitrageOpportunity.profit_rate.desc(), ArbitrageOpportunity.updated_at.desc(), ArbitrageOpportunity.id.desc())
                .limit(batch_limit)
            )
            for row in session.execute(stmt).all():
                opportunity = row[0]
                opportunity_id = int(getattr(opportunity, "id", 0) or 0)
                if opportunity_id in seen_ids:
                    continue
                seen_ids.add(opportunity_id)
                rows.append(row)
                if len(rows) >= limit:
                    return rows
        return rows


@app.get("/api/opportunities")
def api_opportunities():
    app_config = load_app_config()
    rows = _query_open_opportunities()
    result = []
    guard = LowPriceExposureGuard(app_config)
    with SessionLocal() as guard_session:
        for opportunity, item, snapshot in rows:
            payload = {}
            if snapshot and getattr(snapshot, "platform_payload_json", None):
                try:
                    payload = json.loads(snapshot.platform_payload_json or "{}")
                except Exception:
                    payload = {}
            buy_price = float(opportunity.buy_price or 0)
            exposure_decision = guard.should_hide_signal(
                guard_session,
                item_id=int(opportunity.item_id or 0),
                market_hash_name=item.market_hash_name,
                unit_price=buy_price,
            )
            if not exposure_decision.allowed:
                continue
            buy_platform = str(opportunity.buy_platform or "").strip().lower()
            sell_platform = str(opportunity.sell_platform or "").strip().lower()
            direction = _opportunity_direction(buy_platform, sell_platform)
            display_profit_rate, display_profit_cny = _display_opportunity_profit(opportunity, app_config)
            item_name = item.cn_name or item.market_hash_name
            result.append(
                {
                    "id": opportunity.id,
                    "item_id": opportunity.item_id,
                    "item_name": item_name,
                    "market_hash_name": item.market_hash_name,
                    "status": opportunity.status,
                    "buy_platform": opportunity.buy_platform,
                    "sell_platform": opportunity.sell_platform,
                    "buy_price": buy_price,
                    "sell_price": float(opportunity.sell_price or 0),
                    "profit_cny": display_profit_cny,
                    "profit_rate": display_profit_rate,
                    "raw_profit_rate": float(opportunity.profit_rate or 0),
                    "action": str(getattr(opportunity, "action", "") or ""),
                    "direction": direction,
                    "is_executable": str(opportunity.status or "").strip().lower() == "open",
                    "exposure_guard": exposure_decision.to_dict(),
                    "steam": payload.get("steam") or {},
                    "platforms": payload.get("platforms") or {},
                    "cash_to_steam_profit_rate": float(getattr(snapshot, "cash_to_steam_profit_rate", 0) or 0),
                    "cash_to_steam_profit_cny": float(getattr(snapshot, "cash_to_steam_profit_cny", 0) or 0),
                    "cash_to_steam_platform": getattr(snapshot, "cash_to_steam_platform", None),
                    "cash_to_steam_price": float(getattr(snapshot, "cash_to_steam_price", 0) or 0),
                    "steam_to_cash_profit_rate": float(getattr(snapshot, "steam_to_cash_profit_rate", 0) or 0),
                    "steam_to_cash_profit_cny": float(getattr(snapshot, "steam_to_cash_profit_cny", 0) or 0),
                    "steam_to_cash_platform": getattr(snapshot, "steam_to_cash_platform", None),
                    "steam_to_cash_price": float(getattr(snapshot, "steam_to_cash_price", 0) or 0),
                    "steam_sell_min": float(getattr(snapshot, "steam_sell_min", 0) or 0),
                    "steam_buy_max": float(getattr(snapshot, "steam_buy_max", 0) or 0),
                    "best_platform_buy_max": float(getattr(snapshot, "best_platform_buy_max", 0) or 0),
                    "snapshot_updated_at": snapshot.snapshot_updated_at.isoformat() if snapshot and getattr(snapshot, "snapshot_updated_at", None) else None,
                }
            )
    result.sort(
        key=lambda row: (
            1 if str(row.get("status") or "").lower() == "open" else 0,
            float(row.get("profit_rate") or 0),
            float(row.get("profit_cny") or 0),
            int(row.get("id") or 0),
        ),
        reverse=True,
    )
    return result[:320]


@app.get("/api/get_opportunities")
def api_get_opportunities():
    return api_opportunities()


@app.get("/api/market/radar_legacy")
def api_market_radar(
    limit: int = 30,
    offset: int = 0,
    search: str = "",
    sort_by: str = "profit_rate",
    sort_dir: str = "desc",
):
    limit = max(1, min(int(limit or 30), 200))
    offset = max(0, int(offset or 0))
    sort_by = (sort_by or "profit_rate").lower()
    sort_dir = (sort_dir or "desc").lower()

    with SessionLocal() as session:
        steam_subq = (
            session.query(
                MarketPrice.item_id.label("item_id"),
                func.max(MarketPrice.buy_max).label("steam_buy_max"),
            )
            .filter(MarketPrice.platform_name == "steam")
            .group_by(MarketPrice.item_id)
            .subquery()
        )

        base_stmt = (
            select(ItemBase, MarketPrice, steam_subq.c.steam_buy_max)
            .join(MarketPrice, MarketPrice.item_id == ItemBase.id)
            .outerjoin(steam_subq, steam_subq.c.item_id == ItemBase.id)
            .where(MarketPrice.platform_name != "steam")
        )
        base_stmt = base_stmt.where(ItemBase.crawl_priority > 0)
        if search:
            like = f"%{search.strip()}%"
            base_stmt = base_stmt.where(
                (ItemBase.market_hash_name.like(like)) | (ItemBase.cn_name.like(like))
            )

        if sort_by == "volume":
            order_col = MarketPrice.volume.desc() if sort_dir != "asc" else MarketPrice.volume.asc()
        else:
            profit_expr = ((func.coalesce(steam_subq.c.steam_buy_max, 0) - func.coalesce(MarketPrice.sell_min, 0)) / func.nullif(func.coalesce(MarketPrice.sell_min, 0), 0)) * 100
            order_col = profit_expr.desc() if sort_dir != "asc" else profit_expr.asc()

        rows = session.execute(base_stmt.order_by(order_col)).all()

    items = []
    guard = LowPriceExposureGuard(load_app_config())
    with SessionLocal() as guard_session:
        for item, price, steam_buy_max in rows:
            sell_min = float(price.sell_min or 0)
            exposure_decision = guard.should_hide_signal(
                guard_session,
                item_id=int(item.id or 0),
                market_hash_name=item.market_hash_name,
                unit_price=sell_min,
            )
            if not exposure_decision.allowed:
                continue
            buy_max = float(steam_buy_max or 0)
            profit_rate = ((buy_max - sell_min) / sell_min * 100.0) if sell_min > 0 else 0.0
            items.append(
                {
                    "item_id": item.id,
                    "item_name": item.cn_name or item.market_hash_name,
                    "market_hash_name": item.market_hash_name,
                    "platform_name": price.platform_name,
                    "platform": price.platform_name,
                    "crawl_priority": int(item.crawl_priority or 0),
                    "radar_last_matched_at": item.radar_last_matched_at.isoformat() if getattr(item, "radar_last_matched_at", None) else None,
                    "volume": int(price.volume or 0),
                    "sell_min": sell_min,
                    "steam_buy_max": buy_max,
                    "currency": getattr(price, "currency", "CNY") or "CNY",
                    "profit_rate": round(profit_rate, 2),
                    "exposure_guard": exposure_decision.to_dict(),
                    "updated_at": price.updated_at.isoformat() if getattr(price, "updated_at", None) else None,
                }
            )

    total = len(items)
    items = items[offset : offset + limit]
    return {"success": True, "currency": "CNY", "total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/market/radar")
def api_market_radar_v2(
    limit: int = 30,
    offset: int = 0,
    search: str = "",
    sort_by: str = "profit_rate",
    sort_dir: str = "desc",
    min_profit_rate: float | None = None,
    max_profit_rate: float | None = None,
    min_volume: int | None = None,
    min_liquidity_score: float | None = None,
    min_sell_volume: int | None = None,
    min_buy_volume: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    platform: str = "",
    monitor_status: str = "",
    only_profitable: bool = False,
    require_platform_mapping: bool = False,
    opportunity_mode: str = "best",
    buy_price_mode: str = "direct",
    cashout_price_mode: str = "bid",
    steam_balance_cost_ratio: float | None = None,
):
    limit = max(1, min(int(limit or 30), 200))
    offset = max(0, int(offset or 0))
    sort_by = (sort_by or "profit_rate").lower()
    sort_dir = (sort_dir or "desc").lower()
    platform = (platform or "").lower().strip()
    if platform not in {"", "buff", "uuyp", "eco"}:
        platform = ""
    monitor_status = (monitor_status or "").lower().strip()
    opportunity_mode = (opportunity_mode or "best").lower().strip()
    if opportunity_mode not in {"best", "cash_to_steam", "steam_to_cash"}:
        opportunity_mode = "best"
    buy_price_mode = (buy_price_mode or "direct").lower().strip()
    if buy_price_mode not in {"direct", "order"}:
        buy_price_mode = "direct"
    cashout_price_mode = (cashout_price_mode or "bid").lower().strip()
    if cashout_price_mode not in {"bid", "listing"}:
        cashout_price_mode = "bid"
    balance_ratio_override = None
    if steam_balance_cost_ratio is not None:
        balance_ratio_override = max(0.01, min(float(steam_balance_cost_ratio), 1.0))
    dynamic_cashout_recalc = balance_ratio_override is not None and opportunity_mode in {"best", "steam_to_cash"}
    dynamic_method_recalc = buy_price_mode == "order" or cashout_price_mode == "listing"

    with SessionLocal() as session:
        snapshot_count = session.query(func.count(RadarSnapshot.item_id)).scalar() or 0
        stale_direction_count = (
            session.query(func.count(RadarSnapshot.item_id))
            .filter(RadarSnapshot.crawl_priority > 0)
            .filter(
                (RadarSnapshot.best_direction.in_(["steam_to_platform", "platform_to_steam"]))
                | ((RadarSnapshot.cash_to_steam_profit_rate != 0) & (RadarSnapshot.cash_to_steam_platform.is_(None)))
                | ((RadarSnapshot.steam_to_cash_profit_rate != 0) & (RadarSnapshot.steam_to_cash_platform.is_(None)))
            )
            .limit(1)
            .scalar()
            or 0
        )
        outlier_candidates = session.scalars(
            select(RadarSnapshot)
            .where(RadarSnapshot.crawl_priority > 0, RadarSnapshot.steam_to_cash_profit_rate >= 300)
            .order_by(RadarSnapshot.steam_to_cash_profit_rate.desc())
            .limit(200)
        ).all()
        crossed_bid_item_ids = [
            int(row.item_id)
            for row in outlier_candidates
            if radar_snapshot_has_conditional_bid_outlier(row)
        ]
        baseline_profit_candidates = session.scalars(
            select(RadarSnapshot)
            .where(RadarSnapshot.crawl_priority > 0, RadarSnapshot.cash_to_steam_profit_rate >= 300)
            .order_by(RadarSnapshot.cash_to_steam_profit_rate.desc())
            .limit(500)
        ).all()
        baseline_profit_item_ids = [
            int(row.item_id)
            for row in baseline_profit_candidates
            if radar_snapshot_has_baseline_profit(row)
        ]
    if snapshot_count <= 0 or stale_direction_count > 0:
        refresh_radar_snapshots()
    elif crossed_bid_item_ids or baseline_profit_item_ids:
        refresh_radar_snapshots(sorted(set(crossed_bid_item_ids + baseline_profit_item_ids)))

    reverse = sort_dir != "asc"
    with SessionLocal() as session:
        stmt = select(RadarSnapshot).where(RadarSnapshot.crawl_priority > 0)
        if search:
            like = f"%{search.strip()}%"
            stmt = stmt.where((RadarSnapshot.market_hash_name.like(like)) | (RadarSnapshot.item_name.like(like)))
        mode_profit_col, mode_price_col = radar_mode_columns(opportunity_mode)
        dynamic_cashout_filter = dynamic_cashout_recalc or dynamic_method_recalc
        if platform:
            if dynamic_cashout_filter:
                pass
            elif opportunity_mode == "steam_to_cash":
                stmt = stmt.where(RadarSnapshot.steam_to_cash_platform == platform)
            elif opportunity_mode == "cash_to_steam":
                stmt = stmt.where(RadarSnapshot.cash_to_steam_platform == platform)
            else:
                stmt = stmt.where(RadarSnapshot.best_platform == platform)
        if monitor_status == "monitored":
            stmt = stmt.where(RadarSnapshot.crawl_priority > 0)
        elif monitor_status == "unmonitored":
            stmt = stmt.where(RadarSnapshot.crawl_priority <= 0)
        if only_profitable and not dynamic_cashout_filter:
            stmt = stmt.where(mode_profit_col > 0)
        if require_platform_mapping:
            stmt = stmt.where(
                (RadarSnapshot.buff_goods_id.is_not(None))
                | (RadarSnapshot.uuyp_template_id.is_not(None))
                | (RadarSnapshot.eco_goods_id.is_not(None))
            )
        if min_profit_rate is not None and not dynamic_cashout_filter:
            stmt = stmt.where(mode_profit_col >= float(min_profit_rate))
        if max_profit_rate is not None and not dynamic_cashout_filter:
            stmt = stmt.where(mode_profit_col <= float(max_profit_rate))
        if min_volume is not None:
            stmt = stmt.where(RadarSnapshot.depth >= int(min_volume))
        if min_liquidity_score is not None:
            stmt = stmt.where(RadarSnapshot.liquidity_score >= float(min_liquidity_score))
        if min_sell_volume is not None:
            stmt = stmt.where(RadarSnapshot.sell_volume >= int(min_sell_volume))
        if min_buy_volume is not None:
            stmt = stmt.where(RadarSnapshot.buy_volume >= int(min_buy_volume))
        if min_price is not None and not dynamic_cashout_filter:
            stmt = stmt.where(mode_price_col >= float(min_price))
        if max_price is not None and not dynamic_cashout_filter:
            stmt = stmt.where(mode_price_col <= float(max_price))

        if sort_by == "volume":
            order_col = RadarSnapshot.depth
        elif sort_by == "liquidity":
            order_col = RadarSnapshot.liquidity_score
        elif sort_by == "cash_to_steam":
            order_col = RadarSnapshot.cash_to_steam_profit_rate
        elif sort_by == "steam_to_cash":
            order_col = RadarSnapshot.steam_to_cash_profit_rate
        elif sort_by == "profit_rate" and opportunity_mode in {"cash_to_steam", "steam_to_cash"}:
            order_col = mode_profit_col
        else:
            order_col = RadarSnapshot.best_profit_rate
        rows = session.scalars(
            stmt.order_by(order_col.asc() if not reverse else order_col.desc(), RadarSnapshot.item_id.asc())
        ).all()

    items = []
    guard = LowPriceExposureGuard(load_app_config())
    guard_session = SessionLocal()
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.platform_payload_json or "{}")
        except Exception:
            payload = {}
        mode_payload = radar_row_mode_payload(row, opportunity_mode, payload, cashout_price_mode, buy_price_mode, balance_ratio_override)
        warning_flags = radar_snapshot_warning_flags(row, payload)
        if mode_payload.get("ignored_outliers"):
            warning_flags.append("platform_price_outlier")
        dynamic_profit = float(mode_payload["mode_profit_rate"] or 0)
        dynamic_price = float(mode_payload["mode_price"] or 0)
        if dynamic_cashout_filter:
            if platform and str(mode_payload["mode_platform"] or "").lower() != platform:
                continue
            if only_profitable and dynamic_profit <= 0:
                continue
            if min_profit_rate is not None and dynamic_profit < float(min_profit_rate):
                continue
            if max_profit_rate is not None and dynamic_profit > float(max_profit_rate):
                continue
            if min_price is not None and dynamic_price < float(min_price):
                continue
            if max_price is not None and dynamic_price > float(max_price):
                continue
        if str(mode_payload.get("mode_direction") or "") == "steam_to_cash":
            guard_price = float(row.steam_buy_max or 0) if buy_price_mode == "order" else float(row.steam_sell_min or 0)
        else:
            guard_price = float(mode_payload.get("cash_to_steam_price") or mode_payload.get("mode_price") or 0)
        exposure_decision = guard.should_hide_signal(
            guard_session,
            item_id=int(row.item_id or 0),
            market_hash_name=row.market_hash_name,
            unit_price=guard_price,
        )
        if not exposure_decision.allowed:
            continue

        item = {
            "item_id": int(row.item_id),
            "item_name": row.item_name or row.market_hash_name,
            "market_hash_name": row.market_hash_name,
            "buff_goods_id": row.buff_goods_id,
            "uuyp_template_id": row.uuyp_template_id,
            "eco_goods_id": row.eco_goods_id,
            "crawl_priority": int(row.crawl_priority or 0),
            "priority_score": float(row.priority_score or 0),
            "priority_reason": row.priority_reason,
            "priority_source": row.priority_source,
            "radar_last_matched_at": row.radar_last_matched_at.isoformat() if row.radar_last_matched_at else None,
            "steam": payload.get("steam") or {},
            "platforms": payload.get("platforms") or {},
            "best_platform": row.best_platform,
            "best_platform_price": float(row.best_platform_price or 0),
            "platform_name": mode_payload["mode_platform"],
            "sell_min": float(row.best_platform_price or 0),
            "mode_platform": mode_payload["mode_platform"],
            "mode_price": mode_payload["mode_price"],
            "mode_profit_rate": mode_payload["mode_profit_rate"],
            "mode_profit_cny": mode_payload["mode_profit_cny"],
            "mode_direction": mode_payload["mode_direction"],
            "steam_buy_max": float(row.steam_buy_max or 0),
            "profit_rate": float(row.profit_rate or 0),
            "reverse_profit_rate": float(row.reverse_profit_rate or 0),
            "best_profit_rate": float(row.best_profit_rate or 0),
            "best_direction": row.best_direction,
            "cash_to_steam_profit_rate": mode_payload["cash_to_steam_profit_rate"],
            "cash_to_steam_profit_cny": mode_payload["cash_to_steam_profit_cny"],
            "cash_to_steam_platform": mode_payload["cash_to_steam_platform"],
            "cash_to_steam_price": mode_payload["cash_to_steam_price"],
            "steam_to_cash_profit_rate": mode_payload["steam_to_cash_profit_rate"],
            "steam_to_cash_profit_cny": mode_payload["steam_to_cash_profit_cny"],
            "steam_to_cash_platform": mode_payload["steam_to_cash_platform"],
            "steam_to_cash_price": mode_payload["steam_to_cash_price"],
            "steam_to_cash_price_mode": mode_payload["steam_to_cash_price_mode"],
            "buy_price_mode": buy_price_mode,
            "best_profit_cny": float(getattr(row, "best_profit_cny", 0) or 0),
            "steam_balance_cost_ratio": float(balance_ratio_override or getattr(row, "steam_balance_cost_ratio", 0) or 0),
            "opportunity_mode": opportunity_mode,
            "volume": int(row.volume or 0),
            "volume_24h": int(row.volume_24h or 0),
            "depth": int(row.depth or 0),
            "currency": row.currency or "CNY",
            "steam_crossed_book": bool(row.steam_crossed_book),
            "steam_data_source": row.steam_data_source or "",
            "steam_sell_min": float(row.steam_sell_min or 0),
            "best_platform_buy_max": float(row.best_platform_buy_max or 0),
            "sell_volume": int(row.sell_volume or 0),
            "buy_volume": int(row.buy_volume or 0),
            "orderbook_depth": int(row.orderbook_depth or 0),
            "orderbook_balance": float(row.orderbook_balance or 0),
            "liquidity_platform": row.liquidity_platform,
            "liquidity_score": float(row.liquidity_score or 0),
            "snapshot_updated_at": row.snapshot_updated_at.isoformat() if row.snapshot_updated_at else None,
            "freshness_label": radar_freshness_label(row),
            "warning_flags": warning_flags,
            "steam_bid_available": "steam_bid_missing" not in warning_flags,
            "steam_sell_available": "steam_sell_missing" not in warning_flags,
            "exposure_guard": exposure_decision.to_dict(),
        }
        items.append(item)
    guard_session.close()
    if dynamic_cashout_filter:
        sort_key = (lambda item: float(item.get("mode_profit_rate") or 0))
        if sort_by == "steam_to_cash":
            sort_key = lambda item: float(item.get("steam_to_cash_profit_rate") or 0)
        elif sort_by == "cash_to_steam":
            sort_key = lambda item: float(item.get("cash_to_steam_profit_rate") or 0)
        elif sort_by == "volume":
            sort_key = lambda item: int(item.get("depth") or 0)
        elif sort_by == "liquidity":
            sort_key = lambda item: float(item.get("liquidity_score") or 0)
        items.sort(key=sort_key, reverse=reverse)
    total = len(items)
    items = items[offset : offset + limit]
    return {"success": True, "currency": "CNY", "total": total, "limit": limit, "offset": offset, "items": items}


@app.post("/api/market/radar/rebuild")
def api_market_radar_rebuild():
    try:
        saved = refresh_radar_snapshots()
        return {"success": True, "saved": saved, "msg": f"radar snapshot rebuilt: {saved}"}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": str(exc)})


@app.get("/api/market/steamdt/opportunities")
def api_market_steamdt_opportunities(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    strategy: str = "",
    min_profit_rate: float | None = None,
    min_volume: int | None = None,
    include_monitored: bool = True,
    fresh_minutes: int = 30,
    include_stale: bool = False,
):
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    fresh_minutes = max(1, min(int(fresh_minutes or 30), 24 * 60))
    with SessionLocal() as session:
        stmt = select(SteamDTOpportunity, ItemBase).join(ItemBase, SteamDTOpportunity.item_id == ItemBase.id)
        if not include_stale:
            freshness_cutoff = datetime.now() - timedelta(minutes=fresh_minutes)
            stmt = stmt.where(SteamDTOpportunity.steamdt_updated_at >= freshness_cutoff)
        if search:
            like = f"%{search.strip()}%"
            stmt = stmt.where(
                (SteamDTOpportunity.market_hash_name.like(like))
                | (SteamDTOpportunity.item_name.like(like))
                | (ItemBase.cn_name.like(like))
            )
        if strategy:
            stmt = stmt.where(SteamDTOpportunity.strategy_name == strategy.strip())
        if min_profit_rate is not None:
            stmt = stmt.where(SteamDTOpportunity.profit_rate >= float(min_profit_rate))
        if min_volume is not None:
            stmt = stmt.where(SteamDTOpportunity.transaction_count_24h >= int(min_volume))
        if not include_monitored:
            stmt = stmt.where(ItemBase.crawl_priority <= 0)
        stmt = stmt.order_by(SteamDTOpportunity.profit_rate.desc(), SteamDTOpportunity.transaction_count_24h.desc())
        rows = session.execute(stmt).all()

    items = []
    for opp, item in rows:
        items.append(
            {
                "id": opp.id,
                "item_id": opp.item_id,
                "item_name": opp.item_name or item.cn_name or item.market_hash_name,
                "market_hash_name": opp.market_hash_name,
                "strategy_name": opp.strategy_name,
                "platform_name": opp.platform_name,
                "steam_sell_min": float(opp.steam_sell_min or 0),
                "steam_buy_max": float(opp.steam_buy_max or 0),
                "platform_sell_min": float(opp.platform_sell_min or 0),
                "platform_buy_max": float(opp.platform_buy_max or 0),
                "transaction_count_24h": int(opp.transaction_count_24h or 0),
                "platform_sell_volume": int(opp.platform_sell_volume or 0),
                "platform_buy_volume": int(opp.platform_buy_volume or 0),
                "profit_cny": float(opp.profit_cny or 0),
                "profit_rate": float(opp.profit_rate or 0),
                "currency": opp.currency or "CNY",
                "link_url": opp.link_url,
                "crawl_priority": int(item.crawl_priority or 0),
                "steam_updated_at": opp.steam_updated_at.isoformat() if opp.steam_updated_at else None,
                "platform_updated_at": opp.platform_updated_at.isoformat() if opp.platform_updated_at else None,
                "steamdt_updated_at": opp.steamdt_updated_at.isoformat() if opp.steamdt_updated_at else None,
                "updated_at": opp.updated_at.isoformat() if opp.updated_at else None,
            }
        )
    total = len(items)
    return {
        "success": True,
        "currency": "CNY",
        "total": total,
        "limit": limit,
        "offset": offset,
        "fresh_minutes": fresh_minutes,
        "include_stale": include_stale,
        "items": items[offset : offset + limit],
    }


@app.post("/api/market/radar/boost")
def api_market_radar_boost(payload: dict):
    item_ids = payload.get("item_ids") if isinstance(payload, dict) else None
    if not isinstance(item_ids, list) or not item_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 娑撳秷鍏樻稉铏光敄"})

    unique_ids: list[int] = []
    for raw in item_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id not in unique_ids:
            unique_ids.append(item_id)

    if not unique_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "閺堫亝澹橀崚鐗堟箒閺佸牏娈?item_id"})

    explicit_priority = payload.get("crawl_priority") if isinstance(payload, dict) else None
    states = []
    with SessionLocal() as session:
        for item_id in unique_ids:
            item = session.get(ItemBase, item_id)
            if item is None:
                continue
            current = int(item.crawl_priority or 0)
            if explicit_priority is None:
                next_priority = 0 if current > 0 else 3
            else:
                try:
                    next_priority = max(0, int(explicit_priority))
                except (TypeError, ValueError):
                    next_priority = 3
            item.crawl_priority = next_priority
            item.manual_watch = next_priority > 0
            item.priority_source = "manual_watch" if next_priority > 0 else "manual_pause"
            item.priority_reason = "manual_monitor_enabled" if next_priority > 0 else "manual_monitor_disabled"
            session.add(item)
            states.append({"item_id": item_id, "crawl_priority": next_priority})
        session.commit()

    try:
        refresh_radar_snapshots(unique_ids)
    except Exception:
        pass

    enabled = sum(1 for s in states if s["crawl_priority"] > 0)
    disabled = sum(1 for s in states if s["crawl_priority"] <= 0)
    msg = f"updated {len(states)} monitor states"
    if len(states) == 1:
        msg = "monitor enabled" if enabled else "monitor disabled"
    return {"success": True, "updated": len(states), "enabled": enabled, "disabled": disabled, "states": states, "msg": msg}


@app.post("/api/market/radar/blacklist")
def api_market_radar_blacklist(payload: dict):
    item_ids = payload.get("item_ids") if isinstance(payload, dict) else None
    if not isinstance(item_ids, list) or not item_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 娑撳秷鍏樻稉铏光敄"})

    unique_ids: list[int] = []
    for raw in item_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in unique_ids:
            unique_ids.append(item_id)
        if len(unique_ids) >= 200:
            break

    if not unique_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "閺堫亝澹橀崚鐗堟箒閺?item_id"})

    states = []
    with SessionLocal() as session:
        rows = session.scalars(select(ItemBase).where(ItemBase.id.in_(unique_ids))).all()
        for item in rows:
            item.crawl_priority = -1
            item.manual_watch = False
            item.priority_score = 0.0
            item.priority_source = "manual_blacklist"
            item.priority_reason = "manual_blacklisted_lowest_crawl_queue"
            item.priority_updated_at = datetime.now()
            item.priority_ttl_until = None
            item.priority_cooldown_until = None
            item.priority_up_hits = 0
            item.priority_down_hits = 0
            session.add(item)
            states.append({"item_id": int(item.id), "crawl_priority": -1})
        session.commit()

    try:
        refresh_radar_snapshots([state["item_id"] for state in states])
    except Exception:
        pass

    return {
        "success": True,
        "updated": len(states),
        "states": states,
        "msg": f"blacklisted {len(states)} items",
    }


@app.post("/api/market/radar/add_monitor")
def api_market_radar_add_monitor(payload: dict):
    item_ids = payload.get("item_ids") if isinstance(payload, dict) else None
    if isinstance(item_ids, list) and item_ids:
        unique_ids: list[int] = []
        for raw in item_ids:
            try:
                item_id = int(raw)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and item_id not in unique_ids:
                unique_ids.append(item_id)
            if len(unique_ids) >= 100:
                break
        if not unique_ids:
            return JSONResponse(status_code=400, content={"success": False, "msg": "閺堫亝澹橀崚鐗堟箒閺?item_id"})
        items = []
        with SessionLocal() as session:
            rows = session.scalars(select(ItemBase).where(ItemBase.id.in_(unique_ids))).all()
            for item in rows:
                item.crawl_priority = max(int(item.crawl_priority or 0), 3)
                item.manual_watch = True
                item.priority_source = "manual_watch"
                item.priority_reason = "manual_monitor_enabled"
                item.is_active = True
                session.add(item)
                items.append(
                    {
                        "item_id": int(item.id),
                        "item_name": item.cn_name or item.market_hash_name,
                        "market_hash_name": item.market_hash_name,
                        "crawl_priority": int(item.crawl_priority or 0),
                    }
                )
            session.commit()
        try:
            refresh_radar_snapshots([int(item["item_id"]) for item in items])
        except Exception:
            pass
        return {"success": True, "msg": f"added {len(items)} items to monitor", "items": items, "matches": items}

    keyword = str((payload or {}).get("keyword") or (payload or {}).get("name") or "").strip()
    if not keyword:
        return JSONResponse(status_code=400, content={"success": False, "msg": "keyword is required"})
    limit = 30
    with SessionLocal() as session:
        like = f"%{keyword}%"
        rows = (
            session.query(ItemBase)
            .filter((ItemBase.market_hash_name.like(like)) | (ItemBase.cn_name.like(like)))
            .order_by(ItemBase.crawl_priority.desc(), ItemBase.id.asc())
            .limit(limit)
            .all()
        )
        if not rows:
            normalized_keyword = "".join(ch for ch in keyword.casefold() if not ch.isspace())
            candidates = session.query(ItemBase).order_by(ItemBase.crawl_priority.desc(), ItemBase.id.asc()).limit(50000).all()
            rows = [
                item
                for item in candidates
                if normalized_keyword
                and (
                    normalized_keyword in "".join(ch for ch in str(item.cn_name or "").casefold() if not ch.isspace())
                    or normalized_keyword in "".join(ch for ch in str(item.market_hash_name or "").casefold() if not ch.isspace())
                )
            ][:limit]
        if not rows:
            mapper_matches = search_steam_cn_names(keyword, limit=limit)
            mapper_names = [match["market_hash_name"] for match in mapper_matches]
            if mapper_names:
                rows = (
                    session.query(ItemBase)
                    .filter(ItemBase.market_hash_name.in_(mapper_names))
                    .order_by(ItemBase.crawl_priority.desc(), ItemBase.id.asc())
                    .all()
                )
                cn_by_name = {match["market_hash_name"]: match["cn_name"] for match in mapper_matches}
                for row in rows:
                    if not row.cn_name and cn_by_name.get(row.market_hash_name):
                        row.cn_name = cn_by_name[row.market_hash_name]
                        session.add(row)
                if rows:
                    session.commit()
        if not rows:
            return JSONResponse(status_code=404, content={"success": False, "msg": "no matching item found"})
        items = [
            {
                "item_id": row.id,
                "item_name": row.cn_name or row.market_hash_name,
                "market_hash_name": row.market_hash_name,
                "crawl_priority": int(row.crawl_priority or 0),
            }
            for row in rows
        ]
        if len(items) > 1:
            return {"success": True, "needs_selection": True, "msg": f"found {len(items)} matching items, select items to monitor", "matches": items}
        target = rows[0]
        target_id = int(target.id)
        target_name = target.cn_name or target.market_hash_name
        target.crawl_priority = max(int(target.crawl_priority or 0), 3)
        target.manual_watch = True
        target.priority_source = "manual_watch"
        target.priority_reason = "manual_monitor_enabled"
        target.is_active = True
        session.add(target)
        session.commit()
    try:
        refresh_radar_snapshots([target_id])
    except Exception:
        pass
    return {"success": True, "msg": f"瀹告彃濮為崗銉ф磧閹貉嶇窗{target_name}", "item": items[0], "matches": items}


@app.post("/api/market/radar/jit_refresh")
def api_market_radar_jit_refresh(payload: dict):
    clear_engine_stop()
    raw_ids = payload.get("item_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_ids, list) or not raw_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 娑撳秷鍏樻稉铏光敄"})

    item_ids: list[int] = []
    for raw in raw_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in item_ids:
            item_ids.append(item_id)
        if len(item_ids) >= 100:
            break

    if not item_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "閺堫亝澹橀崚鐗堟箒閺?item_id"})

    raw_platforms = payload.get("platforms") if isinstance(payload, dict) else None
    allowed_platforms = {"steam", "buff", "uuyp", "eco"}
    platforms = {
        str(platform).lower().strip()
        for platform in (raw_platforms if isinstance(raw_platforms, list) else allowed_platforms)
        if str(platform).lower().strip() in allowed_platforms
    }
    if not platforms:
        platforms = allowed_platforms

    try:
        from DataEngine.main_engine import refresh_items_prices

        results = asyncio.run(refresh_items_prices(set(item_ids), platforms, fast=True))
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": f"鐎圭偞妞傚ù瀣╃幆婢惰精瑙? {exc}"})

    with SessionLocal() as session:
        rows = (
            session.query(
                MarketPrice.item_id,
                func.max(MarketPrice.updated_at).label("updated_at"),
            )
            .filter(MarketPrice.item_id.in_(item_ids))
            .filter(MarketPrice.platform_name.in_(list(platforms)))
            .group_by(MarketPrice.item_id)
            .all()
        )
    refreshed = [
        {
            "item_id": int(row.item_id),
            "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
        }
        for row in rows
    ]
    try:
        refresh_radar_snapshots(item_ids)
    except Exception:
        pass

    return {
        "success": True,
        "requested": len(item_ids),
        "updated": len(refreshed),
        "result_count": len(results or []),
        "platforms": sorted(platforms),
        "items": refreshed,
        "msg": f"manual pricing completed: requested {len(item_ids)}, updated {len(refreshed)}",
    }


@app.post("/api/market/radar/jit_refresh_v2")
def api_market_radar_jit_refresh_v2(payload: dict):
    clear_engine_stop()
    payload = payload or {}
    raw_ids = payload.get("item_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 娑撳秷鍏樻稉铏光敄"})

    item_ids: list[int] = []
    for raw in raw_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in item_ids:
            item_ids.append(item_id)
        if len(item_ids) >= 100:
            break
    if not item_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "閺堫亝澹橀崚鐗堟箒閺?item_id"})

    raw_platforms = payload.get("platforms")
    allowed_platforms = {"steam", "buff", "uuyp", "eco"}
    platforms = {
        str(platform).lower().strip()
        for platform in (raw_platforms if isinstance(raw_platforms, list) else allowed_platforms)
        if str(platform).lower().strip() in allowed_platforms
    }
    if not platforms:
        platforms = allowed_platforms

    source = str(payload.get("source") or "precise").strip().lower()
    urgent = bool(payload.get("urgent", False))
    eco_fallback = bool(payload.get("eco_fallback", True))
    results: list[dict] = []
    refresh_note = ""
    try:
        if source == "steamdt":
            from DataEngine.main_engine import refresh_items_prices
            from DataEngine.steamdt_openapi_price import refresh_selected_items

            openapi_result = refresh_selected_items(item_ids, platforms=platforms, urgent=urgent)
            results.append(openapi_result)
            missing_eco_ids: list[int] = []
            if "eco" in platforms and isinstance(openapi_result, dict):
                platform_map = openapi_result.get("platforms_by_item") or {}
                if isinstance(platform_map, dict):
                    for iid in item_ids:
                        got = set(platform_map.get(str(iid), []))
                        if "eco" not in got:
                            missing_eco_ids.append(iid)
            if eco_fallback and missing_eco_ids:
                eco_rows = asyncio.run(refresh_items_prices(set(missing_eco_ids), {"eco"}, fast=True))
                results.append({"eco_fallback_items": len(missing_eco_ids), "eco_fallback_rows": len(eco_rows or [])})
                refresh_note = f" | eco閸ョ偠藟={len(missing_eco_ids)}"
        else:
            from DataEngine.main_engine import refresh_items_prices

            rows = asyncio.run(refresh_items_prices(set(item_ids), platforms, fast=True))
            results.append({"source": "precise", "rows": len(rows or [])})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": f"鐎圭偞妞傚ù瀣╃幆婢惰精瑙? {exc}"})

    with SessionLocal() as session:
        rows = (
            session.query(
                MarketPrice.item_id,
                func.max(MarketPrice.updated_at).label("updated_at"),
            )
            .filter(MarketPrice.item_id.in_(item_ids))
            .filter(MarketPrice.platform_name.in_(list(platforms)))
            .group_by(MarketPrice.item_id)
            .all()
        )
    refreshed = [
        {"item_id": int(row.item_id), "updated_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None}
        for row in rows
    ]
    try:
        refresh_radar_snapshots(item_ids)
    except Exception:
        pass

    return {
        "success": True,
        "requested": len(item_ids),
        "updated": len(refreshed),
        "result_count": len(results),
        "platforms": sorted(platforms),
        "source": source,
        "urgent": urgent,
        "items": refreshed,
        "msg": f"manual pricing completed ({source}): requested {len(item_ids)}, updated {len(refreshed)}{refresh_note}",
    }


@app.post("/api/execute_opportunity/{opportunity_id}")
def api_execute_opportunity(opportunity_id: int, payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    with SessionLocal() as session:
        row = session.execute(
            select(ArbitrageOpportunity, ItemBase)
            .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
            .where(ArbitrageOpportunity.id == opportunity_id)
        ).first()

    if not row:
        return JSONResponse(status_code=404, content={"success": False, "msg": "opportunity not found"})

    opportunity, _item = row
    item_id = int(opportunity.item_id or 0)
    if item_id <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "opportunity item_id is invalid"})

    platform = normalize_platform(str(payload.get("platform") or opportunity.buy_platform or "").strip())
    buy_price = float(opportunity.buy_price or 0)
    if buy_price <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "opportunity buy_price is invalid"})

    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity must be integer"})
    if quantity < 1:
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity must be >= 1"})

    requested_action = payload.get("action")
    action = (
        _normalize_manual_buy_action(requested_action)
        if requested_action not in (None, "")
        else PlatformActionType.DIRECT_BUY
    )
    if action is None:
        return JSONResponse(status_code=400, content={"success": False, "msg": "unsupported manual buy action"})

    # Route legacy opportunity execution through the unified manual-buy bridge so every order
    # is persisted in PlatformAction and picked up by worker/order-tracking lifecycle.
    manual_payload = {
        "item_id": item_id,
        "platform": platform,
        "buy_price": buy_price,
        "quantity": quantity,
        "action": action,
        "opportunity_id": int(opportunity.id),
        "trigger": "opportunity_execute",
    }
    return api_trade_manual_buy(manual_payload)


@app.get("/api/tasks/{task_id}")
def api_task_detail(task_id: str):
    q = get_task_queue()
    info = q.get_task(task_id)
    if info is None:
        return {"ok": False, "error": "task not found"}
    return {"ok": True, "task": info}


@app.post("/api/system/shutdown")
def shutdown_system(background_tasks: BackgroundTasks):
    def _do_shutdown():
        time.sleep(1)
        log("system: 閺€璺哄煂閸撳秶顏崗铏簚鐠囬攱鐪伴敍灞绢劀閸︺劑鈧氨鐓￠幍鈧張?worker 閸嬫粍顒?..", "info", category="system")
        request_stop()
        time.sleep(1)
        log("system: 濮濓絽婀柅鈧崙楦跨箻缁?..", "info", category="system")
        import os, signal
        os.kill(os.getpid(), signal.SIGINT)
    background_tasks.add_task(_do_shutdown)
    return {"ok": True, "message": "濮濓絽婀ぐ璇茬俺闁偓閸戣櫣閮寸紒?.."}
from app.routes import register_routes
register_routes(app)
