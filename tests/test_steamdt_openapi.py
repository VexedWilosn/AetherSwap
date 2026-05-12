from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from DataEngine import steamdt_openapi
from DataEngine.database import Base, ItemBase, PlatformMapping


def _build_session_factory(tmp_path: Path):
    db_path = tmp_path / "steamdt_openapi_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def test_extract_base_rows_and_platform_ids():
    payload = {
        "success": True,
        "data": [
            {
                "marketHashName": "Nova | Wood Fired (Battle-Scarred)",
                "platformList": [
                    {"name": "STEAM", "itemId": "730123"},
                    {"name": "BUFF", "itemId": "992211"},
                    {"name": "悠悠有品", "itemId": "1397"},
                    {"name": "C5", "itemId": "55"},
                ],
            }
        ],
    }

    rows = steamdt_openapi.extract_base_rows(payload)
    assert len(rows) == 1
    platform_ids = steamdt_openapi.extract_platform_ids_from_base_row(rows[0])
    assert platform_ids == {
        "steam": "730123",
        "buff": "992211",
        "uuyp": "1397",
        "c5": "55",
    }


def test_sync_base_rows_updates_mapping_and_hot_fields(tmp_path: Path):
    Session = _build_session_factory(tmp_path)

    session = Session()
    item = ItemBase(market_hash_name="Nova | Wood Fired (Battle-Scarred)", cn_name="Nova", is_active=True)
    session.add(item)
    session.commit()
    session.refresh(item)
    session.add(PlatformMapping(item_id=item.id, platform_name="uuyp", platform_item_id="1111"))
    session.commit()
    session.close()

    rows = [
        {
            "marketHashName": "Nova | Wood Fired (Battle-Scarred)",
            "platformList": [
                {"name": "BUFF", "itemId": "2002"},
                {"name": "悠悠有品", "itemId": "3333"},
                {"name": "STEAM", "itemId": "730123"},
            ],
        },
        {
            "marketHashName": "Item Not In DB",
            "platformList": [{"name": "BUFF", "itemId": "9999"}],
        },
    ]

    stats = steamdt_openapi.sync_base_rows(
        rows,
        session_factory=Session,
        tracked_platforms={"steam", "buff", "uuyp", "eco"},
    )

    assert stats["rows_total"] == 2
    assert stats["rows_matched_item"] == 1
    assert stats["rows_missing_item"] == 1
    assert stats["mapping_created"] >= 2
    assert stats["mapping_updated"] >= 1
    assert stats["hot_field_updated"] >= 2

    session = Session()
    item = session.query(ItemBase).filter(ItemBase.market_hash_name == "Nova | Wood Fired (Battle-Scarred)").one()
    mapping_rows = (
        session.query(PlatformMapping)
        .filter(PlatformMapping.item_id == item.id)
        .all()
    )
    mapping_by_platform = {row.platform_name: str(row.platform_item_id) for row in mapping_rows}
    session.close()

    assert item.buff_goods_id == 2002
    assert item.uuyp_template_id == 3333
    assert mapping_by_platform["buff"] == "2002"
    assert mapping_by_platform["uuyp"] == "3333"
    assert mapping_by_platform["steam"] == "730123"
