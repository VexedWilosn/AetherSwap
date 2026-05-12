from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import requests
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DataEngine.database import (
    ItemBase,
    PlatformMapping,
    SessionLocal,
    engine,
    normalize_data_timestamp,
    upsert_market_price_if_fresh,
)
from DataEngine.logging_setup import setup_dataengine_logging
from DataEngine.stop_signal import raise_if_stop_requested

setup_dataengine_logging()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "config" / "iflow_datadumps"
DEFAULT_API_BASE_URL = "https://api.iflow.work/export"
DEFAULT_DIR_NAME = "priority_archive"
DEFAULT_TIMEOUT_SECONDS = 120
SUPPORTED_CURRENT_PLATFORMS = {"steam", "buff", "uuyp", "eco"}
PLATFORM_META_KEYS = {
    "steam": "steam_name_id",
    "buff": "buff_id",
    "uuyp": "uuyp_id",
    "igxe": "igxe_id",
    "c5": "c5_id",
    "eco": "eco_id",
}
HOT_FIELD_BY_PLATFORM = {
    "buff": "buff_goods_id",
    "uuyp": "uuyp_template_id",
    "eco": "eco_goods_id",
}
FILENAME_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}-\d{2})\.zip$", re.IGNORECASE)


@dataclass(frozen=True)
class HistoryPriceRow:
    snapshot_at: datetime
    source_dir: str
    source_file: str
    appid: int | None
    market_hash_name: str
    cn_name: str | None
    en_name: str | None
    platform_name: str
    platform_item_id: str | None
    sell_min: float | None = None
    buy_max: float | None = None
    sell_top5_avg: float | None = None
    buy_top5_avg: float | None = None
    volume: int | None = None
    sell_volume: int | None = None
    buy_volume: int | None = None
    update_time: datetime | None = None
    metrics_json: str = "{}"
    payload_json: str = "{}"

    def as_db_params(self) -> dict[str, Any]:
        data = asdict(self)
        data["snapshot_at"] = normalize_data_timestamp(self.snapshot_at)
        data["update_time"] = normalize_data_timestamp(self.update_time) if self.update_time else data["snapshot_at"]
        return data


@dataclass
class ImportStats:
    source_file: str
    source_dir: str
    records_read: int = 0
    price_rows: int = 0
    history_rows_saved: int = 0
    current_rows_saved: int = 0
    items_created: int = 0
    mappings_created: int = 0
    mappings_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("[iflow] read json failed | path=%s err=%s", path, exc)
    return {}


def load_api_key() -> str:
    app_config = _read_json_file(CONFIG_PATH)
    credentials = _read_json_file(CREDENTIALS_PATH)
    iflow_cfg = app_config.get("iflow_datadump") if isinstance(app_config.get("iflow_datadump"), dict) else {}
    iflow_cred = credentials.get("iflow_datadump") if isinstance(credentials.get("iflow_datadump"), dict) else {}
    return str(
        os.getenv("IFLOW_API_KEY")
        or iflow_cfg.get("api_key")
        or iflow_cred.get("api_key")
        or ""
    ).strip()


def parse_datadump_timestamp(file_name: str) -> datetime | None:
    match = FILENAME_TS_RE.search(str(file_name or "").strip())
    if not match:
        return None
    return datetime.strptime(match.group("ts"), "%Y-%m-%d-%H-%M")


def _parse_cli_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%Y-%m-%d-%H-%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                return parsed.replace(hour=23, minute=59, second=59)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"Invalid date/time: {value}")


def filter_datadump_files(
    files: Iterable[str],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    latest: int | None = None,
) -> list[str]:
    rows: list[tuple[datetime, str]] = []
    for name in files:
        ts = parse_datadump_timestamp(name)
        if ts is None:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        rows.append((ts, name))
    rows.sort(key=lambda row: row[0])
    selected = [name for _, name in rows]
    if latest is not None:
        selected = selected[-max(0, int(latest)) :]
    return selected


def list_datadump_files(
    *,
    dir_name: str = DEFAULT_DIR_NAME,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    response = requests.get(
        f"{api_base_url.rstrip('/')}/list",
        params={"dir_name": dir_name},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"iflow list failed: {payload}")
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise RuntimeError(f"iflow list response missing files: {payload}")
    return [str(name) for name in files]


def download_datadump_file(
    file_name: str,
    *,
    dir_name: str = DEFAULT_DIR_NAME,
    output_dir: Path = DEFAULT_DOWNLOAD_DIR,
    api_key: str | None = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
    overwrite: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / file_name
    if target.exists() and not overwrite:
        return target

    params = {"dir_name": dir_name, "file_name": file_name}
    if api_key:
        params["key"] = api_key

    tmp_path = target.with_suffix(target.suffix + ".part")
    with requests.get(
        f"{api_base_url.rstrip('/')}/download",
        params=params,
        stream=True,
        allow_redirects=True,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                raise_if_stop_requested()
                if chunk:
                    fh.write(chunk)
    tmp_path.replace(target)
    return target


def _extract_json_rows(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("hash_name") or payload.get("market_hash_name"):
            yield payload
            return
        for key in ("data", "items", "rows", "records", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        yield row
                return
        for value in payload.values():
            if isinstance(value, dict):
                yield value
            elif isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        yield row
    elif isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                yield row


def _iter_zip_entry_rows(zip_handle: zipfile.ZipFile, entry_name: str) -> Iterable[dict[str, Any]]:
    saw_json_decode_error = False
    with zip_handle.open(entry_name) as fh:
        for raw_line in fh:
            raise_if_stop_requested()
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                saw_json_decode_error = True
                break
            yield from _extract_json_rows(payload)
    if not saw_json_decode_error:
        return

    text_payload = zip_handle.read(entry_name).decode("utf-8-sig")
    yield from _extract_json_rows(json.loads(text_payload))


def iter_datadump_records(zip_path: Path, *, limit: int | None = None) -> Iterable[dict[str, Any]]:
    count = 0
    with zipfile.ZipFile(zip_path) as zip_handle:
        for entry in zip_handle.infolist():
            if entry.is_dir():
                continue
            if not entry.filename.lower().endswith((".json", ".jsonl")):
                continue
            for row in _iter_zip_entry_rows(zip_handle, entry.filename):
                yield row
                count += 1
                if limit is not None and count >= limit:
                    return


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _avg_order_prices(orders: Any, *, limit: int = 5) -> float | None:
    if not isinstance(orders, list) or not orders:
        return None
    prices: list[float] = []
    for row in orders[:limit]:
        if isinstance(row, (list, tuple)):
            price = _to_float(row[0] if row else None)
        else:
            price = _to_float(row)
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return round(sum(prices) / len(prices), 4)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _timestamp_from_row(row: dict[str, Any], fallback: datetime) -> datetime:
    for key in ("update_time", "updated_at", "timestamp"):
        value = row.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return normalize_data_timestamp(datetime.fromtimestamp(float(value)))
        if isinstance(value, str) and value.strip().isdigit():
            return normalize_data_timestamp(datetime.fromtimestamp(float(value)))
        return normalize_data_timestamp(str(value))
    return fallback


def _has_quote_values(*values: Any) -> bool:
    for value in values:
        number = _to_float(value)
        if number is not None and number > 0:
            return True
    return False


def _platform_history_row(
    source: dict[str, Any],
    *,
    snapshot_at: datetime,
    source_dir: str,
    source_file: str,
    platform_name: str,
    sell_payload: dict[str, Any],
    buy_payload: dict[str, Any],
    sell_min: float | None,
    buy_max: float | None,
    sell_top5_avg: float | None,
    buy_top5_avg: float | None,
    volume: int | None,
    sell_volume: int | None,
    buy_volume: int | None,
) -> HistoryPriceRow | None:
    market_hash_name = str(source.get("hash_name") or source.get("market_hash_name") or "").strip()
    if not market_hash_name:
        return None
    if not _has_quote_values(sell_min, buy_max, sell_top5_avg, buy_top5_avg, volume, sell_volume, buy_volume):
        return None

    meta_info = source.get("meta_info") if isinstance(source.get("meta_info"), dict) else {}
    metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    update_time = _timestamp_from_row(source, snapshot_at)
    platform_item_id = meta_info.get(PLATFORM_META_KEYS.get(platform_name, ""))
    payload = {
        "buy": buy_payload or {},
        "sell": sell_payload or {},
        "steam_order": source.get("steam_order") if platform_name == "steam" else None,
        "steam_volume": source.get("steam_volume") if platform_name == "steam" else None,
    }

    return HistoryPriceRow(
        snapshot_at=snapshot_at,
        source_dir=source_dir,
        source_file=source_file,
        appid=_to_int(source.get("appid")),
        market_hash_name=market_hash_name,
        cn_name=str(source.get("cn_name") or "").strip() or None,
        en_name=str(source.get("en_name") or "").strip() or None,
        platform_name=platform_name,
        platform_item_id=str(platform_item_id).strip() if platform_item_id not in {None, ""} else None,
        sell_min=sell_min,
        buy_max=buy_max,
        sell_top5_avg=sell_top5_avg if sell_top5_avg is not None else sell_min,
        buy_top5_avg=buy_top5_avg if buy_top5_avg is not None else buy_max,
        volume=volume,
        sell_volume=sell_volume,
        buy_volume=buy_volume,
        update_time=update_time,
        metrics_json=_json_dumps(metrics),
        payload_json=_json_dumps(payload),
    )


def history_rows_from_record(
    row: dict[str, Any],
    *,
    snapshot_at: datetime,
    source_dir: str,
    source_file: str,
) -> list[HistoryPriceRow]:
    result: list[HistoryPriceRow] = []

    steam_order = row.get("steam_order") if isinstance(row.get("steam_order"), dict) else {}
    steam_volume = row.get("steam_volume") if isinstance(row.get("steam_volume"), dict) else {}
    steam_row = _platform_history_row(
        row,
        snapshot_at=snapshot_at,
        source_dir=source_dir,
        source_file=source_file,
        platform_name="steam",
        sell_payload={"orders": steam_order.get("sell_orders"), "count": steam_order.get("sell_order_count")},
        buy_payload={"orders": steam_order.get("buy_orders"), "count": steam_order.get("buy_order_count")},
        sell_min=_to_float(steam_order.get("sell_price")),
        buy_max=_to_float(steam_order.get("buy_price")),
        sell_top5_avg=_avg_order_prices(steam_order.get("sell_orders")),
        buy_top5_avg=_avg_order_prices(steam_order.get("buy_orders")),
        volume=_to_int(steam_volume.get("volume")),
        sell_volume=_to_int(steam_order.get("sell_order_count")),
        buy_volume=_to_int(steam_order.get("buy_order_count")),
    )
    if steam_row:
        result.append(steam_row)

    for platform in ("buff", "uuyp", "igxe", "c5"):
        sell_payload = row.get(f"{platform}_sell") if isinstance(row.get(f"{platform}_sell"), dict) else {}
        buy_payload = row.get(f"{platform}_buy") if isinstance(row.get(f"{platform}_buy"), dict) else {}
        platform_row = _platform_history_row(
            row,
            snapshot_at=snapshot_at,
            source_dir=source_dir,
            source_file=source_file,
            platform_name=platform,
            sell_payload=sell_payload,
            buy_payload=buy_payload,
            sell_min=_to_float(sell_payload.get("price")),
            buy_max=_to_float(buy_payload.get("price")),
            sell_top5_avg=_avg_order_prices(sell_payload.get("orders")),
            buy_top5_avg=_avg_order_prices(buy_payload.get("orders")),
            volume=_to_int(sell_payload.get("count")),
            sell_volume=_to_int(sell_payload.get("count")),
            buy_volume=_to_int(buy_payload.get("count")),
        )
        if platform_row:
            result.append(platform_row)

    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    eco_price = _to_float(metrics.get("eco_price"))
    if eco_price is not None and eco_price > 0:
        eco_row = _platform_history_row(
            row,
            snapshot_at=snapshot_at,
            source_dir=source_dir,
            source_file=source_file,
            platform_name="eco",
            sell_payload={"price": eco_price},
            buy_payload={},
            sell_min=eco_price,
            buy_max=None,
            sell_top5_avg=eco_price,
            buy_top5_avg=None,
            volume=None,
            sell_volume=None,
            buy_volume=None,
        )
        if eco_row:
            result.append(eco_row)

    return result


def ensure_iflow_history_schema(db_engine: Engine = engine) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS iflow_datadump_price (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at DATETIME NOT NULL,
        source_dir VARCHAR(80) NOT NULL,
        source_file VARCHAR(255) NOT NULL,
        appid INTEGER,
        market_hash_name VARCHAR(255) NOT NULL,
        cn_name VARCHAR(255),
        en_name VARCHAR(255),
        platform_name VARCHAR(50) NOT NULL,
        platform_item_id VARCHAR(255),
        sell_min FLOAT,
        buy_max FLOAT,
        sell_top5_avg FLOAT,
        buy_top5_avg FLOAT,
        volume INTEGER,
        sell_volume INTEGER,
        buy_volume INTEGER,
        update_time DATETIME,
        metrics_json TEXT NOT NULL DEFAULT '{}',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(snapshot_at, market_hash_name, platform_name)
    )
    """
    indexes = (
        "CREATE INDEX IF NOT EXISTS ix_iflow_datadump_price_hash ON iflow_datadump_price(market_hash_name)",
        "CREATE INDEX IF NOT EXISTS ix_iflow_datadump_price_platform_time ON iflow_datadump_price(platform_name, snapshot_at)",
        "CREATE INDEX IF NOT EXISTS ix_iflow_datadump_price_appid_time ON iflow_datadump_price(appid, snapshot_at)",
    )
    with db_engine.begin() as conn:
        conn.execute(text(ddl))
        for sql in indexes:
            conn.execute(text(sql))


INSERT_HISTORY_SQL = text(
    """
    INSERT INTO iflow_datadump_price (
        snapshot_at, source_dir, source_file, appid, market_hash_name, cn_name, en_name,
        platform_name, platform_item_id, sell_min, buy_max, sell_top5_avg, buy_top5_avg,
        volume, sell_volume, buy_volume, update_time, metrics_json, payload_json
    ) VALUES (
        :snapshot_at, :source_dir, :source_file, :appid, :market_hash_name, :cn_name, :en_name,
        :platform_name, :platform_item_id, :sell_min, :buy_max, :sell_top5_avg, :buy_top5_avg,
        :volume, :sell_volume, :buy_volume, :update_time, :metrics_json, :payload_json
    )
    ON CONFLICT(snapshot_at, market_hash_name, platform_name) DO UPDATE SET
        source_dir=excluded.source_dir,
        source_file=excluded.source_file,
        appid=excluded.appid,
        cn_name=excluded.cn_name,
        en_name=excluded.en_name,
        platform_item_id=excluded.platform_item_id,
        sell_min=excluded.sell_min,
        buy_max=excluded.buy_max,
        sell_top5_avg=excluded.sell_top5_avg,
        buy_top5_avg=excluded.buy_top5_avg,
        volume=excluded.volume,
        sell_volume=excluded.sell_volume,
        buy_volume=excluded.buy_volume,
        update_time=excluded.update_time,
        metrics_json=excluded.metrics_json,
        payload_json=excluded.payload_json
    """
)


def save_history_rows(
    rows: list[HistoryPriceRow],
    *,
    db_engine: Engine = engine,
    batch_size: int = 500,
) -> int:
    if not rows:
        return 0
    ensure_iflow_history_schema(db_engine)
    saved = 0
    with db_engine.begin() as conn:
        for idx in range(0, len(rows), batch_size):
            batch = rows[idx : idx + batch_size]
            conn.execute(INSERT_HISTORY_SQL, [row.as_db_params() for row in batch])
            saved += len(batch)
    return saved


def _normalize_hash(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _ensure_item_for_row(db: Session, row: HistoryPriceRow, *, create_missing: bool) -> tuple[ItemBase | None, bool]:
    existing = (
        db.query(ItemBase)
        .filter(ItemBase.market_hash_name == row.market_hash_name)
        .one_or_none()
    )
    if existing:
        if row.cn_name and not existing.cn_name:
            existing.cn_name = row.cn_name
        return existing, False

    if not create_missing:
        return None, False
    item = ItemBase(
        market_hash_name=row.market_hash_name,
        cn_name=row.cn_name,
        game="csgo" if row.appid == 730 else "dota2" if row.appid == 570 else "steam",
        is_active=True,
        crawl_priority=1,
        priority_source="iflow_datadump",
        priority_updated_at=row.snapshot_at,
    )
    db.add(item)
    db.flush()
    return item, True


def _upsert_mapping(db: Session, item: ItemBase, row: HistoryPriceRow) -> str | None:
    if not row.platform_item_id:
        return None
    mapping = (
        db.query(PlatformMapping)
        .filter(
            PlatformMapping.item_id == item.id,
            PlatformMapping.platform_name == row.platform_name,
        )
        .one_or_none()
    )
    if mapping:
        if str(mapping.platform_item_id) != str(row.platform_item_id):
            mapping.platform_item_id = str(row.platform_item_id)
            return "updated"
        return None
    db.add(
        PlatformMapping(
            item_id=item.id,
            platform_name=row.platform_name,
            platform_item_id=str(row.platform_item_id),
        )
    )
    return "created"


def save_current_market_rows(
    rows: list[HistoryPriceRow],
    *,
    session_factory: sessionmaker[Session] = SessionLocal,
    create_items: bool = False,
) -> dict[str, int]:
    stats = {
        "current_rows_saved": 0,
        "items_created": 0,
        "mappings_created": 0,
        "mappings_updated": 0,
    }
    if not rows:
        return stats
    by_hash_cache: dict[str, ItemBase | None] = {}
    with session_factory() as db:
        for row in rows:
            raise_if_stop_requested()
            if row.platform_name not in SUPPORTED_CURRENT_PLATFORMS:
                continue
            cache_key = _normalize_hash(row.market_hash_name)
            item = by_hash_cache.get(cache_key)
            created = False
            if cache_key not in by_hash_cache:
                item, created = _ensure_item_for_row(db, row, create_missing=create_items)
                by_hash_cache[cache_key] = item
            if item is None:
                continue
            if created:
                stats["items_created"] += 1

            mapping_status = _upsert_mapping(db, item, row)
            if mapping_status == "created":
                stats["mappings_created"] += 1
            elif mapping_status == "updated":
                stats["mappings_updated"] += 1

            hot_field = HOT_FIELD_BY_PLATFORM.get(row.platform_name)
            if hot_field and row.platform_item_id:
                try:
                    setattr(item, hot_field, int(row.platform_item_id))
                except (TypeError, ValueError):
                    pass

            if upsert_market_price_if_fresh(
                db,
                item_id=int(item.id),
                platform_name=row.platform_name,
                data_source="iflow_datadump",
                sell_min=row.sell_min,
                buy_max=row.buy_max,
                sell_top5_avg=row.sell_top5_avg,
                buy_top5_avg=row.buy_top5_avg,
                volume=row.volume,
                sell_volume=row.sell_volume,
                buy_volume=row.buy_volume,
                currency="CNY",
                new_timestamp=row.update_time or row.snapshot_at,
                log=logger,
            ):
                stats["current_rows_saved"] += 1
        db.commit()
    return stats


def import_datadump_zip(
    zip_path: Path,
    *,
    source_dir: str = DEFAULT_DIR_NAME,
    source_file: str | None = None,
    appid: int | None = 730,
    limit: int | None = None,
    sync_current: bool = False,
    create_items: bool = False,
    dry_run: bool = False,
    db_engine: Engine = engine,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> ImportStats:
    source_file = source_file or zip_path.name
    snapshot_at = parse_datadump_timestamp(source_file) or datetime.now()
    stats = ImportStats(source_file=source_file, source_dir=source_dir)
    pending_history: list[HistoryPriceRow] = []

    for record in iter_datadump_records(zip_path, limit=limit):
        raise_if_stop_requested()
        stats.records_read += 1
        if appid is not None and _to_int(record.get("appid")) != int(appid):
            continue
        rows = history_rows_from_record(
            record,
            snapshot_at=snapshot_at,
            source_dir=source_dir,
            source_file=source_file,
        )
        stats.price_rows += len(rows)
        pending_history.extend(rows)

    if dry_run:
        return stats

    stats.history_rows_saved = save_history_rows(pending_history, db_engine=db_engine)
    if sync_current:
        current_stats = save_current_market_rows(
            pending_history,
            session_factory=session_factory,
            create_items=create_items,
        )
        stats.current_rows_saved = current_stats["current_rows_saved"]
        stats.items_created = current_stats["items_created"]
        stats.mappings_created = current_stats["mappings_created"]
        stats.mappings_updated = current_stats["mappings_updated"]
    return stats


def _selected_files_from_args(args: argparse.Namespace) -> list[str]:
    if args.file:
        return list(dict.fromkeys(args.file))
    files = list_datadump_files(
        dir_name=args.dir_name,
        api_base_url=args.api_base_url,
        timeout=args.timeout,
    )
    return filter_datadump_files(
        files,
        start=_parse_cli_datetime(args.start),
        end=_parse_cli_datetime(args.end, end_of_day=True),
        latest=args.latest,
    )


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_list(args: argparse.Namespace) -> None:
    files = list_datadump_files(
        dir_name=args.dir_name,
        api_base_url=args.api_base_url,
        timeout=args.timeout,
    )
    selected = filter_datadump_files(
        files,
        start=_parse_cli_datetime(args.start),
        end=_parse_cli_datetime(args.end, end_of_day=True),
        latest=args.latest,
    )
    _print_json({"dir_name": args.dir_name, "count": len(selected), "files": selected})


def cmd_download(args: argparse.Namespace) -> None:
    api_key = args.api_key or load_api_key()
    downloaded = []
    for file_name in _selected_files_from_args(args):
        path = download_datadump_file(
            file_name,
            dir_name=args.dir_name,
            output_dir=Path(args.output_dir),
            api_key=api_key,
            api_base_url=args.api_base_url,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
        downloaded.append(str(path))
    _print_json({"dir_name": args.dir_name, "downloaded": downloaded, "count": len(downloaded)})


def cmd_import(args: argparse.Namespace) -> None:
    summaries = []
    for zip_file in args.zip:
        stats = import_datadump_zip(
            Path(zip_file),
            source_dir=args.dir_name,
            source_file=Path(zip_file).name,
            appid=args.appid,
            limit=args.limit,
            sync_current=args.sync_current,
            create_items=args.create_items,
            dry_run=args.dry_run,
        )
        summaries.append(stats.as_dict())
    _print_json({"imports": summaries, "count": len(summaries)})


def cmd_sync(args: argparse.Namespace) -> None:
    api_key = args.api_key or load_api_key()
    selected = _selected_files_from_args(args)
    summaries = []
    for file_name in selected:
        path = download_datadump_file(
            file_name,
            dir_name=args.dir_name,
            output_dir=Path(args.output_dir),
            api_key=api_key,
            api_base_url=args.api_base_url,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
        stats = import_datadump_zip(
            path,
            source_dir=args.dir_name,
            source_file=file_name,
            appid=args.appid,
            limit=args.limit,
            sync_current=args.sync_current,
            create_items=args.create_items,
            dry_run=args.dry_run,
        )
        summaries.append(stats.as_dict())
    _print_json({"dir_name": args.dir_name, "files": selected, "imports": summaries, "count": len(summaries)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and import public iflow.work SteamTradingSiteTracker datadumps.",
    )
    parser.set_defaults(func=None)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--dir-name", default=DEFAULT_DIR_NAME, help="Datadump directory name.")
        subparser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL, help="iflow export API base URL.")
        subparser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout in seconds.")

    def add_selector(subparser: argparse.ArgumentParser, *, default_latest: int | None) -> None:
        subparser.add_argument("--file", action="append", help="Specific zip file name. Can be repeated.")
        subparser.add_argument("--start", help="Start timestamp/date, e.g. 2026-05-01 or 2026-05-01-00-15.")
        subparser.add_argument("--end", help="End timestamp/date, e.g. 2026-05-10 or 2026-05-10-00-15.")
        subparser.add_argument("--latest", type=int, default=default_latest, help="Select only the latest N matching files.")

    subcommands = parser.add_subparsers(dest="command")

    list_parser = subcommands.add_parser("list", help="List available datadump files.")
    add_common(list_parser)
    add_selector(list_parser, default_latest=10)
    list_parser.set_defaults(func=cmd_list)

    download_parser = subcommands.add_parser("download", help="Download selected datadump zip files.")
    add_common(download_parser)
    add_selector(download_parser, default_latest=1)
    download_parser.add_argument("--output-dir", default=str(DEFAULT_DOWNLOAD_DIR), help="Download cache directory.")
    download_parser.add_argument("--api-key", default="", help="API key for key-protected directories.")
    download_parser.add_argument("--overwrite", action="store_true", help="Re-download even if the file exists.")
    download_parser.set_defaults(func=cmd_download)

    import_parser = subcommands.add_parser("import", help="Import local datadump zip files into SQLite.")
    import_parser.add_argument("--zip", action="append", required=True, help="Local zip file path. Can be repeated.")
    import_parser.add_argument("--dir-name", default=DEFAULT_DIR_NAME, help="Source directory label.")
    import_parser.add_argument("--appid", type=int, default=730, help="Filter appid. Use 730 for CS2, 570 for Dota2.")
    import_parser.add_argument("--limit", type=int, default=None, help="Import only the first N item records.")
    import_parser.add_argument("--sync-current", action="store_true", help="Also update current market_price rows if fresh.")
    import_parser.add_argument("--create-items", action="store_true", help="Create missing item_base rows during --sync-current.")
    import_parser.add_argument("--dry-run", action="store_true", help="Parse and count rows without writing.")
    import_parser.set_defaults(func=cmd_import)

    sync_parser = subcommands.add_parser("sync", help="List, download, and import selected datadumps.")
    add_common(sync_parser)
    add_selector(sync_parser, default_latest=1)
    sync_parser.add_argument("--output-dir", default=str(DEFAULT_DOWNLOAD_DIR), help="Download cache directory.")
    sync_parser.add_argument("--api-key", default="", help="API key for key-protected directories.")
    sync_parser.add_argument("--overwrite", action="store_true", help="Re-download even if the file exists.")
    sync_parser.add_argument("--appid", type=int, default=730, help="Filter appid. Use 730 for CS2, 570 for Dota2.")
    sync_parser.add_argument("--limit", type=int, default=None, help="Import only the first N item records.")
    sync_parser.add_argument("--sync-current", action="store_true", help="Also update current market_price rows if fresh.")
    sync_parser.add_argument("--create-items", action="store_true", help="Create missing item_base rows during --sync-current.")
    sync_parser.add_argument("--dry-run", action="store_true", help="Parse and count rows without writing.")
    sync_parser.set_defaults(func=cmd_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
