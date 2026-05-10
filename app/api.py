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
from sqlalchemy import func, select

from DataEngine.database import ArbitrageOpportunity, ItemBase, MarketPrice, PlatformMapping, RadarSnapshot, SessionLocal, SteamDTOpportunity
from DataEngine.cn_name_mapper import search_steam_cn_names
from DataEngine.radar_snapshot import refresh_radar_snapshots
from DataEngine.profit_model import cash_to_steam_profit, steam_to_cash_profit
from DataEngine.stop_signal import clear_stop as clear_engine_stop, request_stop as request_engine_stop
from app.database import init_db, migrate_from_json
from app.state import log, request_stop
from app.database import PlatformAction, TradeExecutionRecord, get_session
from app.services.browser_auth import start_login_browser, finish_login_and_extract, finish_login_and_extract_capsule
from app.services.steam_buyer import SteamBuyer, build_steam_market_url
from app.services.notifier import notify_trade_success
from app.services.platform_sessions import PlatformClientFactory
from app.services.session_capsule_pool import SessionCapsulePool
from app.services.task_queue import get_task_queue
from app.services.trading.actions import create_platform_action, transition_action
from app.services.trading.capabilities import CAPABILITY_REGISTRY, normalize_platform
from app.services.trading.platform_adapters import build_platform_adapters
from app.services.trading.risk_budget import RiskBudgetService
from app.services.trading.runtime import (
    PlatformActionWorkerRuntime,
    platform_action_worker_config_from_app_config,
)
from app.services.trading.sell_actions import SellerActionService, is_sell_side_action_type
from app.services.trading.sell_scanner import (
    SellerSnapshotScanner,
    SellerSnapshotScannerRuntime,
    seller_snapshot_scanner_config_from_app_config,
)
from app.services.trading.smoke import PlatformAutomationSmokeService
from app.services.trading.states import CLAIMABLE_STATES, TERMINAL_STATES, PlatformActionState
from app.services.trading.trade_offers import TradeOfferService
from app.services.trading.worker import PlatformActionWorker
from DataEngine.steamdt_fetcher import register_steamdt_capsule_from_cookie
from config import load_app_config

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
    balance_ratio = float(steam_balance_cost_ratio_override or row.steam_balance_cost_ratio or 0.85)
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
    balance_ratio = float(steam_balance_cost_ratio_override or row.steam_balance_cost_ratio or 0.85)
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


@asynccontextmanager
async def _lifespan(application: FastAPI):
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
    # FastAPI 鍏抽棴鏃讹紝纭繚搴曞眰寮曟搸瀛愯繘绋嬭褰诲簳娓呯悊
    try:
        api_stop()
    except Exception as e:
        print(f"娓呯悊搴曞眰寮曟搸杩涚▼鏃跺嚭閿? {e}")


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


@app.post("/api/start")
def api_start():
    global ENGINE_PROCESS
    with ENGINE_LOCK:
        if _is_process_running(ENGINE_PROCESS):
            return {"success": True, "running": True, "msg": "AetherSwap engine is already running"}
        ENGINE_PROCESS = _start_engine_process()
    return {"success": True, "running": True, "msg": "AetherSwap engine started"}


@app.post("/api/stop")
def api_stop():
    global ENGINE_PROCESS
    with ENGINE_LOCK:
        proc = ENGINE_PROCESS
        ENGINE_PROCESS = None
    request_stop()
    cancelled_tasks = get_task_queue().cancel_all()
    _stop_engine_process(proc)
    return {"success": True, "running": False, "cancelled_tasks": cancelled_tasks, "msg": "AetherSwap engine stopped"}


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
            content={"success": False, "msg": "楗板搧瀛楀吀鍚屾澶辫触", "detail": output[-2000:]},
        )
    log("sync_items completed successfully", level="info")
    return {"success": True, "msg": "楗板搧瀛楀吀鍚屾瀹屾垚"}


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


@app.post("/api/trade/manual_buy")
def api_trade_manual_buy(payload: dict):
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    platform = str(payload.get("platform") or payload.get("buy_platform") or "").strip().lower() if isinstance(payload, dict) else ""
    if item_id is None or not platform:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 鍜?platform 涓嶈兘涓虹┖"})

    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 鏍煎紡閿欒"})

    try:
        buy_price = float(payload.get("buy_price") or 0)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 鏍煎紡閿欒"})
    if buy_price <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 蹇呴』澶т簬 0"})

    try:
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 鏍煎紡閿欒"})
    if quantity < 1:
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 蹇呴』澶т簬 0"})

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

    action = str(payload.get("action") or "platform_order").strip()
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
    )

    if platform in {"buff", "uuyp", "eco"}:
        platform_id = {"buff": buff_goods_id, "uuyp": uuyp_template_id, "eco": eco_goods_id}.get(platform)
        if not platform_id and platform != "eco":
            msg = f"{platform} mapping_missing: missing platform item id"
            result = {"success": False, "msg": msg, "reason": "mapping_missing"}
            _update_trade_record(record_id, status="failed", response_payload=result, error_message=msg)
            _update_platform_action_from_trade_result(platform_action_id, status="failed", result=result, error_message=msg)
            return JSONResponse(status_code=400, content={**result, "record_id": record_id})

        factory = PlatformClientFactory(credentials=_load_credentials(), config={})
        try:
            buyer, preflight, provider = factory.client(platform, purpose="manual_buy")
            if not preflight.ok or buyer is None:
                result = preflight.as_result()
                _update_trade_record(record_id, status="failed", response_payload=result, error_message=result.get("msg"))
                _update_platform_action_from_trade_result(platform_action_id, status="failed", result=result, error_message=result.get("msg") or "")
                return JSONResponse(status_code=400, content={**result, "record_id": record_id})

            if platform == "buff":
                result = buyer.create_buy_order(goods_id=platform_id, price=buy_price, num=quantity)
            elif platform == "uuyp":
                result = buyer.create_buy_order(
                    goods_id=platform_id,
                    template_id=platform_id,
                    market_hash_name=market_hash_name,
                    commodity_name=market_hash_name,
                    template_hash_name=market_hash_name,
                    price=buy_price,
                    num=quantity,
                )
            else:
                trade_link = str(payload.get("trade_link") or payload.get("TradeLink") or payload.get("tradeLink") or "").strip()
                steam_id = str(payload.get("steam_id") or payload.get("SteamId") or payload.get("steamId") or "").strip()
                credentials = _load_credentials()
                steam_creds = credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {}
                eco_creds = credentials.get("eco") if isinstance(credentials.get("eco"), dict) else {}
                eco_openapi = credentials.get("eco_openapi") if isinstance(credentials.get("eco_openapi"), dict) else {}
                trade_link = trade_link or str(eco_creds.get("trade_link") or eco_creds.get("TradeLink") or eco_openapi.get("trade_link") or eco_openapi.get("TradeLink") or steam_creds.get("trade_link") or steam_creds.get("TradeLink") or "").strip()
                steam_id = steam_id or str(eco_creds.get("steam_id") or eco_creds.get("SteamId") or eco_openapi.get("steam_id") or eco_openapi.get("SteamId") or steam_creds.get("steam_id") or "").strip()
                if action.lower() in {"purchase", "buy_order", "purchase_order"}:
                    result = buyer.create_purchase_order(
                        market_hash_name=market_hash_name,
                        price=buy_price,
                        num=quantity,
                        trade_link=trade_link,
                        steam_id=steam_id,
                    )
                else:
                    result = buyer.create_buy_order(
                        market_hash_name=market_hash_name,
                        commodity_name=market_hash_name,
                        price=buy_price,
                        num=quantity,
                        trade_link=trade_link,
                        steam_id=steam_id,
                    )

            if not isinstance(result, dict):
                result = {"success": False, "msg": str(result)}
            provider.classify_result(result)
            status = "success" if result.get("success") else "failed"
            _update_trade_record(record_id, status=status, response_payload=result, error_message="" if result.get("success") else result.get("msg"))
            _update_platform_action_from_trade_result(platform_action_id, status=status, result=result, error_message="" if result.get("success") else result.get("msg") or "")
            log(f"manual_buy finished | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} success={status == 'success'}", level="info")
            return {"success": bool(result.get("success")), "msg": result.get("msg") or "platform order submitted", "result": result, "market_hash_name": market_hash_name, "record_id": record_id, "platform_action_id": platform_action_id}
        except Exception as exc:
            _update_trade_record(record_id, status="failed", error_message=str(exc))
            _update_platform_action_from_trade_result(platform_action_id, status="failed", result={"success": False, "msg": str(exc)}, error_message=str(exc))
            log(f"manual_buy failed | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} err={exc}", level="error")
            return JSONResponse(status_code=500, content={"success": False, "msg": f"{platform} order failed: {exc}", "record_id": record_id})

    if platform == "buff":
        try:
            from buff.buyer import BuffBuyer, PAY_METHOD_ALIPAY

            creds = _load_credentials().get("buff") or {}
            cookie_str = str(creds.get("cookies") or creds.get("cookie") or "").strip()
            if not cookie_str:
                _update_trade_record(record_id, status="failed", error_message="鏈厤缃?Buff Cookie")
                return JSONResponse(status_code=400, content={"success": False, "msg": "鏈厤缃?Buff Cookie"})
            buyer = BuffBuyer(cookie_str=cookie_str, pay_method=PAY_METHOD_ALIPAY)
            goods_id = buff_goods_id
            result = buyer.create_buy_order(goods_id=goods_id, price=buy_price, num=quantity)
            status = "success" if result.get("success") else "failed"
            _update_trade_record(record_id, status=status, response_payload=result, error_message="" if result.get("success") else result.get("msg"))
            _update_platform_action_from_trade_result(platform_action_id, status=status, result=result, error_message="" if result.get("success") else result.get("msg") or "")
            if result.get("success"):
                _notify_trade_success_safe(item_name=market_hash_name, action=action, price=buy_price, platform=platform, quantity=quantity, extra={"record_id": record_id})
            log(f"manual_buy success | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity}", level="info")
            return {"success": bool(result.get("success")), "msg": result.get("msg") or "manual buy submitted", "result": result, "market_hash_name": market_hash_name, "record_id": record_id, "platform_action_id": platform_action_id}
        except Exception as exc:
            _update_trade_record(record_id, status="failed", error_message=str(exc))
            _update_platform_action_from_trade_result(platform_action_id, status="failed", result={"success": False, "msg": str(exc)}, error_message=str(exc))
            log(f"manual_buy failed | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} err={exc}", level="error")
            return JSONResponse(status_code=500, content={"success": False, "msg": f"璐拱澶辫触: {exc}"})

    if platform == "uuyp":
        try:
            from uuyp import UuypBuyer

            creds = _load_credentials().get("uuyp") or {}
            cookie_str = str(creds.get("cookies") or creds.get("cookie") or "").strip()
            if not cookie_str:
                _update_trade_record(record_id, status="failed", error_message="鏈厤缃?UUYP Cookie")
                return JSONResponse(status_code=400, content={"success": False, "msg": "鏈厤缃?UUYP Cookie"})
            buyer = UuypBuyer(cookie_str=cookie_str)
            result = buyer.create_buy_order(
                goods_id=uuyp_template_id,
                template_id=uuyp_template_id,
                market_hash_name=market_hash_name,
                price=buy_price,
                num=quantity,
            )
            status = "success" if result.get("success") else "failed"
            _update_trade_record(record_id, status=status, response_payload=result, error_message="" if result.get("success") else result.get("msg"))
            return {"success": bool(result.get("success")), "msg": result.get("msg") or "宸叉彁浜?UUYP 姹傝喘璇锋眰", "result": result, "market_hash_name": market_hash_name, "record_id": record_id}
        except Exception as exc:
            _update_trade_record(record_id, status="failed", error_message=str(exc))
            log(f"manual_buy failed | platform={platform} item_id={item_id} market_hash_name={market_hash_name} buy_price={buy_price} qty={quantity} err={exc}", level="error")
            return JSONResponse(status_code=500, content={"success": False, "msg": f"UUYP 姹傝喘澶辫触: {exc}"})

    _update_trade_record(record_id, status="failed", error_message=f"鏆備笉鏀寔骞冲彴 {platform}")
    return JSONResponse(status_code=400, content={"success": False, "msg": f"鏆備笉鏀寔骞冲彴 {platform}"})


@app.post("/api/trade/manual_steam_order")
def api_trade_manual_steam_order(payload: dict):
    item_id = payload.get("item_id") if isinstance(payload, dict) else None
    buy_price = payload.get("buy_price") if isinstance(payload, dict) else None
    quantity = payload.get("quantity") if isinstance(payload, dict) else 1
    if item_id is None or buy_price is None:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_id 鍜?buy_price 涓嶈兘涓虹┖"})
    try:
        item_id = int(item_id)
        buy_price = float(buy_price)
        quantity = int(quantity)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "msg": "鍙傛暟鏍煎紡閿欒"})
    if buy_price <= 0:
        return JSONResponse(status_code=400, content={"success": False, "msg": "buy_price 蹇呴』澶т簬 0"})
    if quantity < 1:
        return JSONResponse(status_code=400, content={"success": False, "msg": "quantity 蹇呴』澶т簬 0"})

    with SessionLocal() as session:
        item = session.get(ItemBase, item_id)
        if item is None:
            return JSONResponse(status_code=404, content={"success": False, "msg": "item not found"})
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
        market_hash_name=item.market_hash_name,
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
            market_hash_name=item.market_hash_name,
            price=buy_price,
            quantity=quantity,
        )
        if (not result.success) and ("session" in (result.msg or "").lower() or "csrf" in (result.msg or "").lower()):
            try:
                refreshed_cookie = asyncio.run(finish_login_and_extract("steam"))
                if refreshed_cookie:
                    buyer = SteamBuyer(cookie_str=refreshed_cookie)
                    result = buyer.create_buy_order(
                        market_hash_name=item.market_hash_name,
                        price=buy_price,
                        quantity=quantity,
                    )
            except Exception as retry_exc:
                log(f"manual_steam_order retry refresh failed | item_id={item_id} err={retry_exc}", level="warning")
        
        if not result.success:
            _update_trade_record(record_id, status="failed", response_payload=result.raw or {}, error_message=result.msg)
            log(
                f"manual_steam_order failed | item_id={item_id} market_hash_name={item.market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity} err={result.msg}",
                level="error",
            )
            return JSONResponse(status_code=500, content={"success": False, "msg": result.msg, "detail": result.raw})
        _update_trade_record(record_id, status="success", response_payload=result.raw or {}, error_message="")
        log(
            f"manual_steam_order success | item_id={item_id} market_hash_name={item.market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity}",
            level="info",
        )
        return {
            "success": True,
            "msg": result.msg,
            "item_id": item_id,
            "market_hash_name": item.market_hash_name,
            "steam_buy_max": steam_buy_max,
            "buy_price": buy_price,
            "quantity": quantity,
            "detail": result.raw,
            "record_id": record_id,
        }
    except Exception as exc:
        _update_trade_record(record_id, status="failed", error_message=str(exc))
        log(
            f"manual_steam_order exception | item_id={item_id} market_hash_name={item.market_hash_name} steam_buy_max={steam_buy_max} buy_price={buy_price} qty={quantity} err={exc}",
            level="error",
        )
        return JSONResponse(status_code=500, content={"success": False, "msg": f"Steam 姹傝喘澶辫触: {exc}"})


@app.get("/api/status")
def api_status():
    with ENGINE_LOCK:
        running = _is_process_running(ENGINE_PROCESS)
        pid = ENGINE_PROCESS.pid if running and ENGINE_PROCESS is not None else None
    q = get_task_queue()
    active_tasks = q.active_count()
    return {"success": True, "running": running or active_tasks > 0, "engine_running": running, "pid": pid, "active_tasks": active_tasks, "log_path": str(LOG_PATH)}


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
        return JSONResponse(status_code=500, content={"success": False, "msg": f"鑾峰彇 Steam 娲昏穬姹傝喘鍗曞け璐? {exc}"})


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
    item_id: int | None = None,
):
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    with get_session() as session:
        stmt = select(PlatformAction)
        if action_type:
            stmt = stmt.where(PlatformAction.action_type == action_type)
        if platform:
            stmt = stmt.where(PlatformAction.platform == normalize_platform(platform))
        if state:
            stmt = stmt.where(PlatformAction.state == state)
        if item_id is not None:
            stmt = stmt.where(PlatformAction.item_id == int(item_id))
        total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = session.execute(
            stmt.order_by(PlatformAction.created_at.desc()).offset(offset).limit(limit)
        ).scalars().all()
        return {"success": True, "total": int(total or 0), "items": [_platform_action_to_dict(row) for row in rows]}


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
        .group_by(PlatformAction.state)
    ).all()
    platform_rows = session.execute(
        select(PlatformAction.platform, func.count(), func.coalesce(func.sum(PlatformAction.locked_budget_cny), 0.0))
        .where(PlatformAction.state.notin_(terminal_states))
        .group_by(PlatformAction.platform)
    ).all()
    due_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state.in_(active_states))
        .where(PlatformAction.next_check_at <= now)
        .order_by(PlatformAction.next_check_at.asc())
        .limit(20)
    ).scalars().all()
    stuck_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state.in_(attention_states))
        .order_by(PlatformAction.updated_at.asc())
        .limit(20)
    ).scalars().all()
    risk_rows = session.execute(
        select(PlatformAction)
        .where(PlatformAction.state == PlatformActionState.RISK_BLOCKED)
        .order_by(PlatformAction.updated_at.desc())
        .limit(20)
    ).scalars().all()
    active_budget = session.execute(
        select(func.coalesce(func.sum(PlatformAction.locked_budget_cny), 0.0))
        .where(PlatformAction.state.notin_(terminal_states))
    ).scalar_one()
    due_count = session.execute(
        select(func.count())
        .where(PlatformAction.state.in_(active_states))
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
        alerts.append({"level": "warning", "kind": "due_worker_stopped", "message": f"{due_count} 个动作已到期但执行 worker 未运行"})
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
        alerts.append({"level": "info", "kind": "waiting_actions", "message": f"{waiting_count} 个动作处于等待/重试状态"})
    risk_count = int((summary.get("by_state") or {}).get(PlatformActionState.RISK_BLOCKED, {}).get("count") or 0)
    if risk_count:
        alerts.append({"level": "warning", "kind": "risk_blocked", "message": f"{risk_count} 个动作被风控锁定"})
    locked = float(summary.get("active_locked_budget_cny") or 0)
    if locked > 0:
        alerts.append({"level": "info", "kind": "locked_budget", "message": f"当前动作占用预算 ¥{locked:.2f}"})
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


@app.post("/api/trade/platform_actions/run_once")
def api_trade_platform_actions_run_once(payload: dict | None = None):
    payload = payload if isinstance(payload, dict) else {}
    cfg = load_app_config()
    worker_config = platform_action_worker_config_from_app_config(cfg)
    safe_mode = bool(payload.get("safe_mode", worker_config.safe_mode))
    limit = max(1, min(int(payload.get("limit") or 10), 100))
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
    cfg = load_app_config()
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
    started = PLATFORM_ACTION_WORKER_RUNTIME.start({"trading_worker": section, "SAFE_MODE_ENABLED": cfg.get("SAFE_MODE_ENABLED")})
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
            raw_context=payload.get("raw_context"),
        )
        if not risk.allowed:
            action.state = PlatformActionState.RISK_BLOCKED
            action.error_code = risk.reason
            action.error_message = risk.reason
            action.updated_at = time.time()
            session.add(action)
            session.commit()
            session.refresh(action)
        return {
            "success": True,
            "created": created,
            "risk": asdict(risk),
            "item": _platform_action_to_dict(action),
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
    cfg = load_app_config()
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
        return JSONResponse(status_code=400, content={"success": False, "msg": "order_id 涓嶈兘涓虹┖"})
    try:
        order_id = str(order_id).strip()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "msg": "order_id 鏍煎紡閿欒"})

    steam_cookie = _normalize_steam_cookie()
    if not steam_cookie:
        return JSONResponse(status_code=400, content={"success": False, "msg": "鏈厤缃?Steam 鐧诲綍鎬侊紝璇峰厛瀹屾垚 Steam 鐧诲綍"})

    buyer = SteamBuyer(cookie_str=steam_cookie)
    try:
        result = buyer.cancel_buy_order(order_id)
        if not result.success:
            return JSONResponse(status_code=500, content={"success": False, "msg": result.msg, "detail": result.raw})
        return {"success": True, "msg": result.msg, "detail": result.raw}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": f"鎾ゅ崟澶辫触: {exc}"})


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
    files = _engine_log_files()
    collected: list[str] = []
    for fp in files:
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if content:
            collected.extend(content.splitlines())
    if not collected:
        return []
    start_idx = max(since, 0)
    sliced = collected[start_idx:start_idx + limit] if limit and limit > 0 else collected[start_idx:]
    out: list[dict] = []
    for idx, line in enumerate(sliced, start=start_idx + 1):
        out.append({"id": idx, "t": time.time(), "level": "info", "msg": line})
    return out


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
) -> int | None:
    try:
        safe_payload = {k: v for k, v in (payload or {}).items() if k not in {"cookies", "cookie"}}
        with get_session() as session:
            platform_action, _ = create_platform_action(
                session,
                action_type=str(action or "platform_order").strip().lower(),
                platform=platform,
                item_id=item_id,
                market_hash_name=market_hash_name,
                quantity=quantity,
                target_price=target_price,
                channel="ui_manual_buy",
                idempotency_key=f"manual_buy:{record_id}",
                request_payload=safe_payload,
                raw_context={"trade_execution_record_id": record_id},
            )
            transition_action(
                platform_action,
                PlatformActionState.PROCESSING,
            )
            transition_action(
                platform_action,
                PlatformActionState.WAITING_PLATFORM,
                next_check_at=time.time() + 10 * 365 * 24 * 3600,
                raw_context={"trade_execution_record_id": record_id, "compatibility_bridge": True},
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

    cookie_str = await finish_login_and_extract(platform)
    current = _load_credentials()
    node = current.get(platform) if isinstance(current.get(platform), dict) else {}
    node = dict(node or {})
    node["cookies"] = cookie_str
    current[platform] = node
    _save_credentials(current)
    return {"success": True, "msg": f"{platform} cookies saved", "cookies": cookie_str}


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

    # Buff锛氳皟鐢ㄨ鍗曞巻鍙叉煡璇紝鑳芥湁鏁堝尯鍒嗏€滃凡鐧诲綍鈥濅笌鈥滆椋庢帶/杩囨湡鈥?
    try:
        from buff import BuffBuyer, BuffAuthExpired

        buff_cookie = str((incoming.get("buff") or {}).get("cookies", "")).strip()
        if not buff_cookie:
            result["buff"] = {"ok": False, "msg": "鏈厤缃?Cookie"}
        else:
            buyer = BuffBuyer(cookie_str=buff_cookie)
            try:
                ok = bool(buyer.check_wait_pay_orders(game="csgo"))
                result["buff"] = {"ok": True, "msg": "Buff 鐧诲綍鏈夋晥", "detail": {"wait_pay": ok}}
            except BuffAuthExpired:
                result["buff"] = {"ok": False, "msg": "Buff auth expired"}
            except Exception as exc:
                result["buff"] = {"ok": False, "msg": f"Buff 楠岃瘉璇锋眰寮傚父: {exc}"}
    except Exception as exc:
        result["buff"] = {"ok": False, "msg": f"Buff 楠岃瘉鍣ㄥ垵濮嬪寲澶辫触: {exc}"}

    # UUYP锛氳皟鐢ㄥ紑婧愰」鐩悓娆剧敤鎴蜂俊鎭帴鍙ｆ帰娴?
    try:
        from uuyp import UuypBuyer

        uuyp_node = incoming.get("uuyp") or {}
        uuyp_cookie = str(uuyp_node.get("cookies", "")).strip()

        if not uuyp_cookie:
            result["uuyp"] = {"ok": False, "msg": "鏈厤缃?Cookie"}
        else:
            buyer = UuypBuyer(cookie_str=uuyp_cookie)
            try:
                # 浣跨敤寮€婧愰」鐩悓娆鹃獙璇佹帴鍙?
                res = buyer._request("GET", "https://api.youpin898.com/api/user/Account/getUserInfo")
                code = str(res.get("Code", res.get("code", ""))).strip()
                ok = code in {"0", "200", "OK", "ok", "Success", "success"} or res.get("success") is True
                # 鍙岄噸淇濋櫓锛氬彧瑕佹湁鐢ㄦ埛淇℃伅杩斿洖鍗充负鎴愬姛
                if "Data" in res and isinstance(res["Data"], dict) and "UserId" in res["Data"]:
                    ok = True

                result["uuyp"] = {
                    "ok": ok,
                    "msg": "UUYP 鐧诲綍鏈夋晥" if ok else f"UUYP 杩斿洖寮傚父: {res}",
                    "detail": res,
                }
            except Exception as exc:
                result["uuyp"] = {"ok": False, "msg": f"UUYP 楠岃瘉璇锋眰寮傚父: {exc}"}
    except Exception as exc:
        result["uuyp"] = {"ok": False, "msg": f"UUYP 楠岃瘉鍣ㄥ垵濮嬪寲澶辫触: {exc}"}

    # ECO锛氫拱瀹剁綉椤?Cookie 浠呭仛涓婚〉/鐢ㄦ埛鎺ュ彛杩為€氭€ф帰娴嬶紝涓嶈皟鐢ㄥ晢鎴?OpenAPI
    try:
        import requests

        eco_cookie = str((incoming.get("eco") or {}).get("cookies", "")).strip()
        if not eco_cookie:
            result["eco"] = {"ok": False, "msg": "鏈厤缃?Cookie"}
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
                    "msg": "ECO 鐧诲綍鏈夋晥" if res.ok else f"ECO 涓婚〉璇锋眰澶辫触: {res.status_code}",
                    "detail": {"status_code": res.status_code, "url": res.url},
                }
            except Exception as exc:
                result["eco"] = {"ok": False, "msg": f"ECO 楠岃瘉璇锋眰寮傚父: {exc}"}
    except Exception as exc:
        result["eco"] = {"ok": False, "msg": f"ECO 楠岃瘉鍣ㄥ垵濮嬪寲澶辫触: {exc}"}

    return {"success": True, "result": result}


def _query_open_opportunities():
    with SessionLocal() as session:
        stmt = (
            select(ArbitrageOpportunity, ItemBase)
            .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
            .where(ArbitrageOpportunity.status.in_(["open", "verifying", "success", "failed"]))
            .order_by(ArbitrageOpportunity.id.desc())
            .limit(50)
        )
        return session.execute(stmt).all()


@app.get("/api/opportunities")
def api_opportunities():
    rows = _query_open_opportunities()
    result = []
    for opportunity, item in rows:
        item_name = item.cn_name or item.market_hash_name
        result.append(
            {
                "id": opportunity.id,
                "item_name": item_name,
                "market_hash_name": item.market_hash_name,
                "status": opportunity.status,
                "buy_platform": opportunity.buy_platform,
                "sell_platform": opportunity.sell_platform,
                "buy_price": float(opportunity.buy_price or 0),
                "sell_price": float(opportunity.sell_price or 0),
                "profit_cny": float(opportunity.profit_cny or 0),
                "profit_rate": float(opportunity.profit_rate or 0),
            }
        )
    return result


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

        total = session.execute(select(func.count()).select_from(base_stmt.subquery())).scalar_one()
        rows = session.execute(base_stmt.order_by(order_col).offset(offset).limit(limit)).all()

    items = []
    for item, price, steam_buy_max in rows:
        sell_min = float(price.sell_min or 0)
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
                "updated_at": price.updated_at.isoformat() if getattr(price, "updated_at", None) else None,
            }
        )

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

        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
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
        if dynamic_cashout_filter:
            rows = session.scalars(
                stmt.order_by(order_col.asc() if not reverse else order_col.desc(), RadarSnapshot.item_id.asc())
            ).all()
        else:
            stmt = stmt.order_by(order_col.asc() if not reverse else order_col.desc(), RadarSnapshot.item_id.asc())
            rows = session.scalars(stmt.offset(offset).limit(limit)).all()

    items = []
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
        }
        items.append(item)
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
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 涓嶈兘涓虹┖"})

    unique_ids: list[int] = []
    for raw in item_ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id not in unique_ids:
            unique_ids.append(item_id)

    if not unique_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "鏈壘鍒版湁鏁堢殑 item_id"})

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
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 涓嶈兘涓虹┖"})

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
        return JSONResponse(status_code=400, content={"success": False, "msg": "鏈壘鍒版湁鏁?item_id"})

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
            return JSONResponse(status_code=400, content={"success": False, "msg": "鏈壘鍒版湁鏁?item_id"})
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
    return {"success": True, "msg": f"宸插姞鍏ョ洃鎺э細{target_name}", "item": items[0], "matches": items}


@app.post("/api/market/radar/jit_refresh")
def api_market_radar_jit_refresh(payload: dict):
    clear_engine_stop()
    raw_ids = payload.get("item_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_ids, list) or not raw_ids:
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 涓嶈兘涓虹┖"})

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
        return JSONResponse(status_code=400, content={"success": False, "msg": "鏈壘鍒版湁鏁?item_id"})

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
        return JSONResponse(status_code=500, content={"success": False, "msg": f"瀹炴椂娴嬩环澶辫触: {exc}"})

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
        return JSONResponse(status_code=400, content={"success": False, "msg": "item_ids 涓嶈兘涓虹┖"})

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
        return JSONResponse(status_code=400, content={"success": False, "msg": "鏈壘鍒版湁鏁?item_id"})

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
                refresh_note = f" | eco鍥炶ˉ={len(missing_eco_ids)}"
        else:
            from DataEngine.main_engine import refresh_items_prices

            rows = asyncio.run(refresh_items_prices(set(item_ids), platforms, fast=True))
            results.append({"source": "precise", "rows": len(rows or [])})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "msg": f"瀹炴椂娴嬩环澶辫触: {exc}"})

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
def api_execute_opportunity(opportunity_id: int):
    with SessionLocal() as session:
        row = session.execute(
            select(ArbitrageOpportunity, ItemBase)
            .join(ItemBase, ArbitrageOpportunity.item_id == ItemBase.id)
            .where(ArbitrageOpportunity.id == opportunity_id)
        ).first()

    if not row:
            return {"success": False, "msg": "opportunity not found"}

    opportunity, item = row
    buy_platform = str(opportunity.buy_platform or "").lower()
    item_name = item.cn_name or item.market_hash_name

    if buy_platform == "buff":
        try:
            from pathlib import Path
            import json as _json
            from buff.buyer import BuffBuyer, PAY_METHOD_ALIPAY, BuffAuthExpired

            cookie_str = ""
            cfg_path = Path("config/app_config.json")
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                cookie_str = cfg.get("buff_cookie") or cfg.get("cookie_str") or ""
            if not cookie_str:
                return {"success": False, "msg": "鏈厤缃?Buff Cookie"}
            buyer = BuffBuyer(cookie_str=cookie_str, pay_method=PAY_METHOD_ALIPAY)
            goods_id = int(opportunity.item_id)
            result = buyer.create_buy_order(goods_id=goods_id, price=float(opportunity.buy_price or 0), num=1)
            if result.get("success"):
                result["item_name"] = item_name
                return result
            return result
        except BuffAuthExpired:
            return {"success": False, "msg": "Buff auth expired"}
        except Exception as e:
            return {"success": False, "msg": str(e)}

    if buy_platform in {"uu", "uusell"}:
        try:
            from app.UUAutoSellItem import UUAutoSellItem
        except Exception:
            return {"success": False, "msg": "UUAutoSellItem.py 瀵煎叆澶辫触"}
        return {"success": False, "msg": "UU execution is not implemented"}

    if buy_platform in {"eco", "ecosteam"}:
        try:
            from app.ECOsteam import ECOsteam
        except Exception:
            return {"success": False, "msg": "ECOsteam.py 瀵煎叆澶辫触"}
        return {"success": False, "msg": "ECO execution is not implemented"}

    return {"success": False, "msg": f"unsupported buy_platform={buy_platform}"}


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
        log("system: 鏀跺埌鍓嶇鍏虫満璇锋眰锛屾鍦ㄩ€氱煡鎵€鏈?worker 鍋滄...", "info", category="system")
        request_stop()  
        time.sleep(1)  
        log("system: 姝ｅ湪閫€鍑鸿繘绋?..", "info", category="system")
        import os, signal
        os.kill(os.getpid(), signal.SIGINT)
    background_tasks.add_task(_do_shutdown)
    return {"ok": True, "message": "姝ｅ湪褰诲簳閫€鍑虹郴缁?.."}
from app.routes import register_routes
register_routes(app)

