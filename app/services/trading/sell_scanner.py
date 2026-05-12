from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .capabilities import normalize_platform
from .sell_actions import SellerActionPlanResult, SellerActionService


InventoryScanner = Callable[[], tuple[bool, list[dict[str, Any]], str]]
SteamListingsScanner = Callable[[Any], tuple[bool, set[str], str, dict[str, str]]]
CredentialsLoader = Callable[[], dict[str, Any]]
SessionFactory = Callable[[], Any]
ConfigLoader = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class SellerSnapshotScan:
    snapshot: dict[str, Any]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class SellerSnapshotRunResult:
    scan: SellerSnapshotScan
    plan: Any
    created: list[Any]


@dataclass(frozen=True)
class SellerSnapshotScannerRuntimeConfig:
    enabled: bool = False
    commit: bool = False
    interval_seconds: float = 3600.0
    error_backoff_seconds: float = 300.0
    include_inventory: bool = True
    include_steam_listings: bool = True
    include_c5_orders: bool = True
    listing_platform: str = "steam"
    delivery_platform: str = "c5game"
    channel: str = "seller_snapshot_scanner"
    snapshot_payload: dict[str, Any] = field(default_factory=dict)


def _default_inventory_scanner() -> tuple[bool, list[dict[str, Any]], str]:
    from app.inventory_cs2 import scan_cs2_inventory

    return scan_cs2_inventory()


def _default_steam_listings_scanner(cookies: Any) -> tuple[bool, set[str], str, dict[str, str]]:
    from app.steam_listings import fetch_my_listings

    return fetch_my_listings(cookies)


def _default_credentials_loader() -> dict[str, Any]:
    from config import get_all_credentials

    return get_all_credentials()


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


def seller_snapshot_scanner_config_from_app_config(config: dict[str, Any] | None) -> SellerSnapshotScannerRuntimeConfig:
    raw = config if isinstance(config, dict) else {}
    section = raw.get("seller_snapshot_scanner") if isinstance(raw.get("seller_snapshot_scanner"), dict) else raw
    section = section if isinstance(section, dict) else {}
    payload = section.get("snapshot_payload") if isinstance(section.get("snapshot_payload"), dict) else {}
    return SellerSnapshotScannerRuntimeConfig(
        enabled=_to_bool(section.get("enabled"), False),
        commit=_to_bool(section.get("commit"), False),
        interval_seconds=_to_float(
            section.get("interval_seconds"),
            3600.0,
            minimum=30.0,
            maximum=86400.0,
        ),
        error_backoff_seconds=_to_float(
            section.get("error_backoff_seconds"),
            300.0,
            minimum=30.0,
            maximum=86400.0,
        ),
        include_inventory=_to_bool(section.get("include_inventory"), True),
        include_steam_listings=_to_bool(section.get("include_steam_listings"), True),
        include_c5_orders=_to_bool(section.get("include_c5_orders"), True),
        listing_platform=normalize_platform(str(section.get("listing_platform") or "steam")) or "steam",
        delivery_platform=normalize_platform(str(section.get("delivery_platform") or "c5game")) or "c5game",
        channel=str(section.get("channel") or "seller_snapshot_scanner"),
        snapshot_payload=dict(payload),
    )


def _empty_config() -> dict[str, Any]:
    return {}


def _c5_app_key(credentials: dict[str, Any]) -> str:
    data = credentials.get("c5game") if isinstance(credentials.get("c5game"), dict) else credentials.get("c5")
    if not isinstance(data, dict):
        data = {}
    return str(
        data.get("app_key")
        or data.get("api_key")
        or data.get("appKey")
        or data.get("AppKey")
        or data.get("app-key")
        or ""
    ).strip()


def _steam_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    data = credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {}
    return data if isinstance(data, dict) else {}


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_inventory_item(row: dict[str, Any], item_id_by_name: dict[str, int]) -> dict[str, Any]:
    out = dict(row)
    name = str(_first(out, "market_hash_name", "marketHashName", "name") or "").strip()
    if not out.get("item_id") and name in item_id_by_name:
        out["item_id"] = item_id_by_name[name]
    return out


def _normalize_order(row: dict[str, Any], item_id_by_name: dict[str, int]) -> dict[str, Any]:
    out = dict(row)
    name = str(_first(out, "market_hash_name", "marketHashName", "name") or "").strip()
    if not out.get("market_hash_name") and name:
        out["market_hash_name"] = name
    if not out.get("item_id") and name in item_id_by_name:
        out["item_id"] = item_id_by_name[name]
    return out


class SellerSnapshotScanner:
    """Collects local platform snapshots and feeds SellerActionService.

    This class gathers data only. It never executes platform trading actions.
    Commit mode writes PlatformAction rows for the worker to process later.
    """

    def __init__(
        self,
        *,
        inventory_scanner: InventoryScanner | None = None,
        steam_listings_scanner: SteamListingsScanner | None = None,
        credentials_loader: CredentialsLoader | None = None,
        seller_action_service: SellerActionService | None = None,
    ):
        self.inventory_scanner = inventory_scanner or _default_inventory_scanner
        self.steam_listings_scanner = steam_listings_scanner or _default_steam_listings_scanner
        self.credentials_loader = credentials_loader or _default_credentials_loader
        self.seller_action_service = seller_action_service or SellerActionService()

    def collect_snapshot(self, payload: dict[str, Any] | None = None) -> SellerSnapshotScan:
        payload = payload if isinstance(payload, dict) else {}
        credentials = self.credentials_loader()
        steam = _steam_credentials(credentials)
        item_id_by_name = {
            str(name).strip(): int(item_id)
            for name, item_id in (payload.get("item_id_by_name") or {}).items()
            if str(name).strip() and item_id
        }
        diagnostics: dict[str, Any] = {"sources": {}}
        explicit_inventory = [
            _normalize_inventory_item(row, item_id_by_name)
            for row in (payload.get("inventory") or payload.get("items") or [])
            if isinstance(row, dict)
        ]
        explicit_active_assetids = {
            str(x).strip()
            for x in (payload.get("active_assetids") or payload.get("active_listing_assetids") or [])
            if str(x).strip()
        }
        explicit_orders = [
            _normalize_order(row, item_id_by_name)
            for row in (payload.get("orders") or payload.get("deliveries") or [])
            if isinstance(row, dict)
        ]
        snapshot: dict[str, Any] = {
            "channel": str(payload.get("channel") or "seller_snapshot_scan"),
            "listing_platform": str(payload.get("listing_platform") or "steam"),
            "delivery_platform": str(payload.get("delivery_platform") or "c5game"),
            "steam_id": str(payload.get("steam_id") or steam.get("steam_id") or ""),
            "target_price": payload.get("target_price"),
            "expected_profit_rate": payload.get("expected_profit_rate"),
            "inventory": explicit_inventory,
            "active_assetids": sorted(explicit_active_assetids),
            "orders": explicit_orders,
            "reprices": payload.get("reprices") or [],
            "cancellations": payload.get("cancellations") or [],
        }

        if bool(payload.get("include_inventory", True)):
            ok, items, err = self.inventory_scanner()
            diagnostics["sources"]["inventory"] = {"ok": bool(ok), "count": len(items or []), "error": err or ""}
            if ok:
                snapshot["inventory"].extend(
                    _normalize_inventory_item(row, item_id_by_name)
                    for row in (items or [])
                    if isinstance(row, dict)
                )

        if bool(payload.get("include_steam_listings", True)):
            ok, assetids, err, name_by_assetid = self.steam_listings_scanner(steam.get("cookies") or "")
            diagnostics["sources"]["steam_listings"] = {
                "ok": bool(ok),
                "count": len(assetids or []),
                "error": err or "",
            }
            if ok:
                merged_assetids = set(snapshot["active_assetids"])
                merged_assetids.update(str(x).strip() for x in (assetids or set()) if str(x).strip())
                snapshot["active_assetids"] = sorted(merged_assetids)
                snapshot["active_listing_names"] = dict(name_by_assetid or {})

        if bool(payload.get("include_c5_orders", True)):
            orders, diag = self._collect_c5_orders(credentials, payload, item_id_by_name)
            diagnostics["sources"]["c5_orders"] = diag
            snapshot["orders"].extend(orders)

        return SellerSnapshotScan(snapshot=snapshot, diagnostics=diagnostics)

    def plan(self, payload: dict[str, Any] | None = None):
        scan = self.collect_snapshot(payload)
        plan = self.seller_action_service.plan_from_snapshot(scan.snapshot)
        return SellerSnapshotRunResult(scan=scan, plan=plan, created=[])

    def plan_and_create(self, session_factory: SessionFactory, payload: dict[str, Any] | None = None) -> SellerSnapshotRunResult:
        scan = self.collect_snapshot(payload)
        with session_factory() as session:
            result: SellerActionPlanResult = self.seller_action_service.plan_and_create(session, scan.snapshot)
        return SellerSnapshotRunResult(scan=scan, plan=result.plan, created=result.created)

    def _collect_c5_orders(
        self,
        credentials: dict[str, Any],
        payload: dict[str, Any],
        item_id_by_name: dict[str, int],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        app_key = _c5_app_key(credentials)
        if not app_key:
            return [], {"ok": False, "count": 0, "error": "missing_c5game_app_key"}
        try:
            from c5game import C5GameClient

            client = C5GameClient(
                app_key=app_key,
                timeout=int(payload.get("c5_timeout") or 15),
                base_url=str(payload.get("c5_base_url") or "http://openapi.c5game.com"),
            )
            steam_id = str(payload.get("steam_id") or _steam_credentials(credentials).get("steam_id") or "")
            statuses = payload.get("c5_statuses") or [1, 2]
            rows: list[dict[str, Any]] = []
            errors: list[str] = []
            for status in statuses:
                result = client.order_list(status=int(status), page=1, steam_id=steam_id)
                if not result.get("success"):
                    errors.append(str(result.get("msg") or result.get("message") or status))
                    continue
                rows.extend(_normalize_order(row, item_id_by_name) for row in (result.get("data") or []) if isinstance(row, dict))
            return rows, {"ok": not errors, "count": len(rows), "error": "; ".join(errors)}
        except Exception as exc:
            return [], {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}


class SellerSnapshotScannerRuntime:
    """Periodically plans seller actions from platform snapshots.

    Commit mode writes PlatformAction rows only. Execution remains owned by the
    separate PlatformActionWorker runtime.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        config_loader: ConfigLoader | None = None,
        scanner_factory: Callable[[], SellerSnapshotScanner] | None = None,
        name: str = "seller-snapshot-scanner",
    ):
        self.session_factory = session_factory
        self.config_loader = config_loader or _empty_config
        self.scanner_factory = scanner_factory or SellerSnapshotScanner
        self.name = name

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._config = SellerSnapshotScannerRuntimeConfig()
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
            "total_planned": 0,
            "total_created": 0,
            "total_skipped": 0,
            "total_errors": 0,
        }

    def start_from_config(self) -> bool:
        config = seller_snapshot_scanner_config_from_app_config(self.config_loader())
        with self._lock:
            self._config = config
        if not config.enabled:
            return False
        return self.start(config)

    def start(self, config: SellerSnapshotScannerRuntimeConfig | dict[str, Any] | None = None) -> bool:
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

    def run_once(self, config: SellerSnapshotScannerRuntimeConfig | dict[str, Any] | None = None) -> SellerSnapshotRunResult:
        runtime_config = self._coerce_config(config)
        with self._lock:
            self._config = runtime_config
        result = self._run_scanner_once(runtime_config)
        self._record_result(result, committed=runtime_config.commit)
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._status)
            alive = self._is_alive_locked()
            if not alive:
                data["running"] = False
            config = asdict(self._config)
            config["snapshot_payload_keys"] = sorted(config.get("snapshot_payload") or {})
            config.pop("snapshot_payload", None)
            data.update(config)
            data["running"] = alive and bool(data.get("running"))
            data["thread_name"] = self._thread.name if alive and self._thread else None
            return data

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                delay = self._config.interval_seconds
                try:
                    result = self._run_scanner_once(self._config)
                    self._record_result(result, committed=self._config.commit)
                except Exception as exc:
                    self._record_error(exc)
                    delay = self._config.error_backoff_seconds
                self._wait(delay)
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["stopped_at"] = time.time()

    def _run_scanner_once(self, config: SellerSnapshotScannerRuntimeConfig) -> SellerSnapshotRunResult:
        scanner = self.scanner_factory()
        payload = self._payload_from_config(config)
        if config.commit:
            return scanner.plan_and_create(self.session_factory, payload)
        return scanner.plan(payload)

    def _payload_from_config(self, config: SellerSnapshotScannerRuntimeConfig) -> dict[str, Any]:
        payload = dict(config.snapshot_payload or {})
        payload.setdefault("include_inventory", config.include_inventory)
        payload.setdefault("include_steam_listings", config.include_steam_listings)
        payload.setdefault("include_c5_orders", config.include_c5_orders)
        payload.setdefault("listing_platform", config.listing_platform)
        payload.setdefault("delivery_platform", config.delivery_platform)
        payload.setdefault("channel", config.channel)
        return payload

    def _record_result(self, result: SellerSnapshotRunResult, *, committed: bool) -> None:
        now = time.time()
        result_dict = {
            "committed": bool(committed),
            "snapshot_counts": {
                "inventory": len(result.scan.snapshot.get("inventory") or []),
                "active_assetids": len(result.scan.snapshot.get("active_assetids") or []),
                "orders": len(result.scan.snapshot.get("orders") or []),
            },
            "diagnostics": result.scan.diagnostics,
            "planned": len(result.plan.actions),
            "created": sum(1 for row in result.created if getattr(row, "created", False)),
            "skipped": len(result.plan.skipped),
        }
        with self._lock:
            self._status["last_run_at"] = now
            self._status["last_success_at"] = now
            self._status["last_result"] = result_dict
            self._status["last_error"] = ""
            self._status["total_runs"] += 1
            self._status["total_planned"] += result_dict["planned"]
            self._status["total_created"] += result_dict["created"]
            self._status["total_skipped"] += result_dict["skipped"]

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
        config: SellerSnapshotScannerRuntimeConfig | dict[str, Any] | None,
    ) -> SellerSnapshotScannerRuntimeConfig:
        if isinstance(config, SellerSnapshotScannerRuntimeConfig):
            return config
        if isinstance(config, dict):
            return seller_snapshot_scanner_config_from_app_config(config)
        return seller_snapshot_scanner_config_from_app_config(self.config_loader())

    def _is_alive_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
