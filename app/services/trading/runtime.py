from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .canary import live_canary_config_from_app_config
from .platform_adapters import build_platform_adapters
from .trade_offers import TradeOfferService
from .worker import PlatformActionWorker, WorkerRunResult


SessionFactory = Callable[[], Any]
ConfigLoader = Callable[[], dict[str, Any]]
CredentialsLoader = Callable[[], dict[str, Any]]
AdapterBuilder = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PlatformActionWorkerRuntimeConfig:
    enabled: bool = False
    safe_mode: bool = True
    poll_interval_seconds: float = 10.0
    batch_size: int = 10
    lease_seconds: int = 60
    error_backoff_seconds: float = 60.0


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


def _to_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _to_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def platform_action_worker_config_from_app_config(config: dict[str, Any] | None) -> PlatformActionWorkerRuntimeConfig:
    raw = config if isinstance(config, dict) else {}
    section = raw.get("trading_worker") if isinstance(raw.get("trading_worker"), dict) else raw
    section = section if isinstance(section, dict) else {}
    legacy_safe_mode = _to_bool(raw.get("SAFE_MODE_ENABLED"), True)
    return PlatformActionWorkerRuntimeConfig(
        enabled=_to_bool(section.get("enabled"), False),
        safe_mode=_to_bool(section.get("safe_mode"), legacy_safe_mode),
        poll_interval_seconds=_to_float(
            section.get("poll_interval_seconds"),
            10.0,
            minimum=0.1,
            maximum=3600.0,
        ),
        batch_size=_to_int(section.get("batch_size"), 10, minimum=1, maximum=100),
        lease_seconds=_to_int(section.get("lease_seconds"), 60, minimum=5, maximum=3600),
        error_backoff_seconds=_to_float(
            section.get("error_backoff_seconds"),
            60.0,
            minimum=1.0,
            maximum=3600.0,
        ),
    )


def _empty_config() -> dict[str, Any]:
    return {}


class PlatformActionWorkerRuntime:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        config_loader: ConfigLoader | None = None,
        credentials_loader: CredentialsLoader | None = None,
        adapter_builder: AdapterBuilder | None = None,
        name: str = "platform-action-worker",
    ):
        self.session_factory = session_factory
        self.config_loader = config_loader or _empty_config
        self.credentials_loader = credentials_loader or _empty_config
        self.adapter_builder = adapter_builder or build_platform_adapters
        self.name = name

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._config = PlatformActionWorkerRuntimeConfig()
        self._status: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "stopped_at": None,
            "last_run_at": None,
            "last_success_at": None,
            "last_error_at": None,
            "last_error": "",
            "last_result": None,
            "total_runs": 0,
            "total_claimed": 0,
            "total_succeeded": 0,
            "total_waiting": 0,
            "total_failed": 0,
            "total_risk_blocked": 0,
            "total_errors": 0,
        }

    def start_from_config(self) -> bool:
        raw_config = self.config_loader()
        config = platform_action_worker_config_from_app_config(raw_config)
        with self._lock:
            self._config = config
        if not config.enabled:
            return False
        if not config.safe_mode and not live_canary_config_from_app_config(raw_config).allow_background_worker:
            return False
        return self.start(config)

    def start(self, config: PlatformActionWorkerRuntimeConfig | dict[str, Any] | None = None) -> bool:
        runtime_config = self._coerce_config(config)
        with self._lock:
            if self._is_alive_locked():
                return False
            self._config = runtime_config
            self._stop_event.clear()
            self._wake_event.clear()
            now = time.time()
            self._status.update(
                {
                    "running": True,
                    "started_at": now,
                    "stopped_at": None,
                    "last_error_at": None,
                    "last_error": "",
                }
            )
            self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._status["running"] = False
                self._status["stopped_at"] = time.time()
                self._thread = None
                return True
        self._stop_event.set()
        self._wake_event.set()
        if threading.current_thread() is not thread:
            thread.join(timeout=max(0.1, float(timeout_seconds or 0.1)))
        with self._lock:
            stopped = not thread.is_alive()
            if stopped:
                self._status["running"] = False
                self._status["stopped_at"] = time.time()
                self._thread = None
            return stopped

    def wake(self) -> bool:
        with self._lock:
            running = self._is_alive_locked()
        if running:
            self._wake_event.set()
        return running

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._status)
            alive = self._is_alive_locked()
            if not alive:
                data["running"] = False
            data.update(asdict(self._config))
            data["running"] = alive and bool(data.get("running"))
            data["thread_name"] = self._thread.name if alive and self._thread else None
            return data

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                delay = self._config.poll_interval_seconds
                try:
                    result = self._run_worker_once(self._config)
                    self._record_result(result)
                    if result.claimed >= self._config.batch_size:
                        delay = 0.1
                except Exception as exc:
                    self._record_error(exc)
                    delay = self._config.error_backoff_seconds
                self._wait(delay)
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["stopped_at"] = time.time()

    def _run_worker_once(self, config: PlatformActionWorkerRuntimeConfig) -> WorkerRunResult:
        adapters = {}
        app_config = self.config_loader()
        credentials = {}
        if not config.safe_mode:
            credentials = self.credentials_loader()
            adapters = self.adapter_builder(credentials, app_config)
        worker = PlatformActionWorker(
            self.session_factory,
            adapters=adapters,
            app_config=app_config,
            trade_offer_service=TradeOfferService(credentials=credentials, config=app_config),
            safe_mode=config.safe_mode,
            lease_seconds=config.lease_seconds,
        )
        return worker.run_once(limit=config.batch_size)

    def _record_result(self, result: WorkerRunResult) -> None:
        now = time.time()
        result_dict = asdict(result)
        with self._lock:
            self._status["last_run_at"] = now
            self._status["last_success_at"] = now
            self._status["last_result"] = result_dict
            self._status["last_error"] = ""
            self._status["total_runs"] += 1
            self._status["total_claimed"] += result.claimed
            self._status["total_succeeded"] += result.succeeded
            self._status["total_waiting"] += result.waiting
            self._status["total_failed"] += result.failed
            self._status["total_risk_blocked"] += result.risk_blocked

    def _record_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        now = time.time()
        with self._lock:
            self._status["last_run_at"] = now
            self._status["last_error_at"] = now
            self._status["last_error"] = message[:500]
            self._status["total_errors"] += 1

    def _wait(self, delay: float) -> None:
        if delay <= 0:
            return
        self._wake_event.wait(delay)
        self._wake_event.clear()

    def _coerce_config(
        self,
        config: PlatformActionWorkerRuntimeConfig | dict[str, Any] | None,
    ) -> PlatformActionWorkerRuntimeConfig:
        if isinstance(config, PlatformActionWorkerRuntimeConfig):
            return config
        if isinstance(config, dict):
            return platform_action_worker_config_from_app_config(config)
        return platform_action_worker_config_from_app_config(self.config_loader())

    def _is_alive_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
