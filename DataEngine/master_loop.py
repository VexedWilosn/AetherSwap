import logging
import os
import subprocess
import sys
import time
import json
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from DataEngine.stop_signal import StopRequested, clear_stop, is_stop_requested

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"

_root_logger = logging.getLogger()
if not _root_logger.handlers:
    _root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    _root_logger.addHandler(console_handler)

    file_handler = TimedRotatingFileHandler(
        LOG_PATH,
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = "%Y-%m-%d"
    _root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

def _load_app_config() -> dict:
    path = BASE_DIR / "config" / "app_config.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        logger.warning("load app_config failed: %s", exc)
    return {}

def _steamdt_openapi_enabled(config: dict) -> bool:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    openapi = steamdt.get("openapi") if isinstance(steamdt.get("openapi"), dict) else {}
    if not openapi and isinstance(steamdt.get("open_api"), dict):
        openapi = steamdt.get("open_api")
    raw = os.getenv("STEAMDT_OPENAPI_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    credentials_has_key = False
    try:
        cred_path = BASE_DIR / "config" / "credentials.json"
        if cred_path.exists():
            credentials = json.loads(cred_path.read_text(encoding="utf-8") or "{}")
            steamdt_openapi = (
                credentials.get("steamdt_openapi")
                if isinstance(credentials.get("steamdt_openapi"), dict)
                else {}
            )
            if not steamdt_openapi and isinstance(credentials.get("steamdt"), dict):
                steamdt_cred = credentials.get("steamdt")
                steamdt_openapi = steamdt_cred.get("openapi") if isinstance(steamdt_cred.get("openapi"), dict) else {}
            credentials_has_key = bool(
                str(steamdt_openapi.get("api_key") or steamdt_openapi.get("token") or "").strip()
            )
    except Exception:
        credentials_has_key = False

    has_key = bool(
        str(os.getenv("STEAMDT_OPENAPI_API_KEY") or "").strip()
        or str(openapi.get("api_key") or steamdt.get("openapi_api_key") or "").strip()
        or credentials_has_key
    )
    return bool(openapi.get("enabled", steamdt.get("openapi_enabled", has_key)))


def _steamdt_openapi_interval_seconds(config: dict) -> int:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    openapi = steamdt.get("openapi") if isinstance(steamdt.get("openapi"), dict) else {}
    if not openapi and isinstance(steamdt.get("open_api"), dict):
        openapi = steamdt.get("open_api")
    raw = os.getenv(
        "STEAMDT_OPENAPI_SYNC_INTERVAL_SECONDS",
        openapi.get("sync_interval_seconds", steamdt.get("openapi_sync_interval_seconds", 24 * 3600)),
    )
    try:
        return max(3600, int(raw or 24 * 3600))
    except Exception:
        return 24 * 3600


def _steamdt_openapi_price_enabled(config: dict) -> bool:
    steamdt = config.get("steamdt") if isinstance(config.get("steamdt"), dict) else {}
    openapi_price = steamdt.get("openapi_price") if isinstance(steamdt.get("openapi_price"), dict) else {}
    raw = os.getenv("STEAMDT_OPENAPI_PRICE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    credentials_has_key = False
    try:
        cred_path = BASE_DIR / "config" / "credentials.json"
        if cred_path.exists():
            credentials = json.loads(cred_path.read_text(encoding="utf-8") or "{}")
            steamdt_openapi = (
                credentials.get("steamdt_openapi")
                if isinstance(credentials.get("steamdt_openapi"), dict)
                else {}
            )
            credentials_has_key = bool(str(steamdt_openapi.get("api_key") or "").strip())
    except Exception:
        credentials_has_key = False
    has_key = bool(
        str(os.getenv("STEAMDT_OPENAPI_API_KEY") or "").strip()
        or str(openapi_price.get("api_key") or "").strip()
        or credentials_has_key
    )
    return bool(openapi_price.get("enabled", has_key))



def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()


def _cfg_int(config: dict, section: str, key: str, default: int, minimum: int = 0) -> int:
    raw_section = config.get(section) if isinstance(config.get(section), dict) else {}
    try:
        return max(minimum, int(raw_section.get(key, default) or default))
    except Exception:
        return default


def _run_script(script_name: str, *args: str) -> None:
    script_path = BASE_DIR / "DataEngine" / script_name
    logger.info("script start | path=%s", script_path)
    if is_stop_requested():
        raise StopRequested()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(script_path), *args],
        cwd=str(BASE_DIR),
        env=env,
        creationflags=creationflags,
    )

    while proc.poll() is None:
        if is_stop_requested():
            logger.warning("stop requested; terminating script | script=%s", script_name)
            _terminate_process_tree(proc)
            raise StopRequested()
        time.sleep(0.2)

    if proc.returncode not in (0, None):
        raise RuntimeError(f"{script_name} exited with code {proc.returncode}")

    logger.info("script done | path=%s", script_path)


def _sleep_interruptible(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_stop_requested():
            raise StopRequested()
        time.sleep(min(0.2, max(0.0, deadline - time.time())))


def main() -> None:
    logger.info("AetherSwap DataEngine master loop started")
    clear_stop()
    last_selector_run = 0.0
    last_steamdt_openapi_run = 0.0
    last_priority_run = 0.0
    last_low_crawl_run = 0.0
    selector_interval_sec = 3600

    while True:
        try:
            if is_stop_requested():
                raise StopRequested()
            now = time.time()
            app_config = _load_app_config()
            if now - last_selector_run >= selector_interval_sec:
                logger.info("running smart_selector")
                _run_script("smart_selector.py")
                last_selector_run = now

            if _steamdt_openapi_enabled(app_config) and now - last_steamdt_openapi_run >= _steamdt_openapi_interval_seconds(app_config):
                logger.info("running SteamDT OpenAPI base mapping sync")
                _run_script("steamdt_openapi.py")
                last_steamdt_openapi_run = now

            priority_interval_sec = _cfg_int(app_config, "priority_scheduler", "global_interval_seconds", 900, 60)
            if now - last_priority_run >= priority_interval_sec:
                logger.info("running priority scheduler")
                _run_script("priority_scheduler.py")
                last_priority_run = now

            logger.info("market crawl and arbitrage round started")
            crawl_cfg = app_config.get("crawl_layers") if isinstance(app_config.get("crawl_layers"), dict) else {}
            low_interval = _cfg_int(app_config, "crawl_layers", "low_interval_seconds", 8 * 3600, 3600)
            low_limit = int(crawl_cfg.get("low_limit", 500) or 500)
            if now - last_low_crawl_run >= low_interval:
                _run_script("sync_baseline.py")
                _run_script("main_engine.py", "--min-priority", "1", "--limit", str(low_limit))
                last_low_crawl_run = now
            if _steamdt_openapi_price_enabled(app_config):
                _run_script("steamdt_openapi_price.py", "--once")
            else:
                logger.info("steamdt openapi price scheduler skipped | reason=disabled")
            _run_script("arbitrage_engine.py")
            _run_script("action_policy.py")
            _run_script("trade_executor.py")
            logger.info("round completed; sleeping 60s")
            _sleep_interruptible(60)
        except StopRequested:
            logger.info("DataEngine master loop stopped")
            return
        except Exception:
            logger.exception("master_loop failed")
            _sleep_interruptible(60)


if __name__ == "__main__":
    main()
