from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from DataEngine.iflow_datadump import (
    ensure_iflow_history_schema,
    filter_datadump_files,
    history_rows_from_record,
    import_datadump_zip,
    iter_datadump_records,
    parse_datadump_timestamp,
)


def _sample_record() -> dict:
    return {
        "hash_name": "Sticker | Outsiders (Glitter) | Antwerp 2022",
        "appid": 730,
        "cn_name": "Outsiders",
        "en_name": "Sticker | Outsiders (Glitter) | Antwerp 2022",
        "meta_info": {
            "steam_name_id": 176312051,
            "buff_id": 894137,
            "uuyp_id": 101647,
        },
        "buff_buy": {"price": 3.7, "orders": [3.7, 3.6], "count": 27},
        "buff_sell": {"price": 3.86, "orders": [3.86, 3.88, 3.9], "count": 91},
        "uuyp_buy": {"price": 3.6, "orders": [3.6, 3.5], "count": 9},
        "uuyp_sell": {"price": 3.87, "orders": [3.87, 3.88], "count": 500},
        "metrics": {"steam_buy_price": 3.9, "eco_price": 4.01},
        "steam_order": {
            "buy_order_count": 23507,
            "buy_orders": [[4.54, 1], [4.27, 5]],
            "buy_price": 4.54,
            "sell_order_count": 949,
            "sell_orders": [[4.69, 2], [4.75, 1]],
            "sell_price": 4.69,
        },
        "steam_volume": {"volume": 119},
        "update_time": 1707754330,
    }


def test_parse_and_filter_datadump_file_names():
    assert parse_datadump_timestamp("2026-05-10-00-15.zip") == datetime(2026, 5, 10, 0, 15)
    assert parse_datadump_timestamp("bad.zip") is None

    files = [
        "2026-05-08-00-15.zip",
        "bad.zip",
        "2026-05-09-12-15.zip",
        "2026-05-10-00-15.zip",
    ]
    selected = filter_datadump_files(files, start=datetime(2026, 5, 9), latest=1)
    assert selected == ["2026-05-10-00-15.zip"]


def test_history_rows_from_record_extracts_platform_quotes():
    rows = history_rows_from_record(
        _sample_record(),
        snapshot_at=datetime(2024, 2, 13, 0, 15),
        source_dir="priority_archive",
        source_file="2024-02-13-00-15.zip",
    )
    by_platform = {row.platform_name: row for row in rows}

    assert set(by_platform) >= {"steam", "buff", "uuyp", "eco"}
    assert by_platform["buff"].sell_min == 3.86
    assert by_platform["buff"].buy_max == 3.7
    assert by_platform["buff"].sell_top5_avg == 3.88
    assert by_platform["steam"].volume == 119
    assert by_platform["steam"].platform_item_id == "176312051"
    assert by_platform["eco"].sell_min == 4.01


def test_iter_datadump_records_reads_json_lines_zip(tmp_path: Path):
    zip_path = tmp_path / "2024-02-13-00-15.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "2024-02-13-00-15.json",
            "\n".join(json.dumps(_sample_record()) for _ in range(2)),
        )

    rows = list(iter_datadump_records(zip_path))
    assert len(rows) == 2
    assert rows[0]["hash_name"] == "Sticker | Outsiders (Glitter) | Antwerp 2022"


def test_import_datadump_zip_writes_history_table(tmp_path: Path):
    zip_path = tmp_path / "2024-02-13-00-15.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("2024-02-13-00-15.json", json.dumps(_sample_record()) + "\n")

    db_engine = create_engine(f"sqlite:///{tmp_path / 'iflow.db'}", future=True)
    ensure_iflow_history_schema(db_engine)

    stats = import_datadump_zip(
        zip_path,
        db_engine=db_engine,
        sync_current=False,
    )

    assert stats.records_read == 1
    assert stats.history_rows_saved >= 4

    with db_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM iflow_datadump_price")).scalar_one()
        buff = conn.execute(
            text("SELECT sell_min FROM iflow_datadump_price WHERE platform_name='buff'")
        ).scalar_one()

    assert count == stats.history_rows_saved
    assert buff == 3.86
