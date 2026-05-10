from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from DataEngine.database import Base, ItemBase


def test_add_monitor_chinese_search_returns_selection(monkeypatch):
    import app.api as api

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(api, "refresh_radar_snapshots", lambda item_ids=None: 0)

    with TestingSessionLocal() as session:
        session.add_all(
            [
                ItemBase(id=1, market_hash_name="AK-47 | Redline (Field-Tested)", cn_name="AK-47 | 红线 (久经沙场)", crawl_priority=1),
                ItemBase(id=2, market_hash_name="AK-47 | Redline (Minimal Wear)", cn_name="AK-47 | 红线 (略有磨损)", crawl_priority=0),
            ]
        )
        session.commit()

    client = TestClient(api.app)
    response = client.post("/api/market/radar/add_monitor", json={"keyword": "红线"})

    assert response.status_code == 200
    data = response.json()
    assert data["needs_selection"] is True
    assert len(data["matches"]) == 2


def test_add_monitor_accepts_multiple_selected_item_ids(monkeypatch):
    import app.api as api

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(api, "refresh_radar_snapshots", lambda item_ids=None: 0)

    with TestingSessionLocal() as session:
        session.add_all(
            [
                ItemBase(id=10, market_hash_name="Sticker | Alpha", cn_name="印花 | 甲", crawl_priority=0),
                ItemBase(id=11, market_hash_name="Sticker | Beta", cn_name="印花 | 乙", crawl_priority=1),
            ]
        )
        session.commit()

    client = TestClient(api.app)
    response = client.post("/api/market/radar/add_monitor", json={"item_ids": [10, 11]})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["items"]) == 2
    with TestingSessionLocal() as session:
        assert session.get(ItemBase, 10).manual_watch is True
        assert session.get(ItemBase, 10).crawl_priority == 3
        assert session.get(ItemBase, 11).manual_watch is True
        assert session.get(ItemBase, 11).crawl_priority == 3


def test_add_monitor_falls_back_to_mapper_when_cn_name_missing(monkeypatch):
    import app.api as api

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(api, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(api, "refresh_radar_snapshots", lambda item_ids=None: 0)
    monkeypatch.setattr(
        api,
        "search_steam_cn_names",
        lambda keyword, limit=30: [
            {"market_hash_name": "R8 Revolver | Junk Yard (Field-Tested)", "cn_name": "R8 左轮手枪 | 废物王 (久经沙场)"},
            {"market_hash_name": "R8 Revolver | Junk Yard (Minimal Wear)", "cn_name": "R8 左轮手枪 | 废物王 (略有磨损)"},
        ],
    )

    with TestingSessionLocal() as session:
        session.add_all(
            [
                ItemBase(id=20, market_hash_name="R8 Revolver | Junk Yard (Field-Tested)", cn_name=None, crawl_priority=0),
                ItemBase(id=21, market_hash_name="R8 Revolver | Junk Yard (Minimal Wear)", cn_name=None, crawl_priority=0),
            ]
        )
        session.commit()

    client = TestClient(api.app)
    response = client.post("/api/market/radar/add_monitor", json={"keyword": "废物王"})

    assert response.status_code == 200
    data = response.json()
    assert data["needs_selection"] is True
    assert len(data["matches"]) == 2
    assert data["matches"][0]["item_name"] == "R8 左轮手枪 | 废物王 (久经沙场)"
    with TestingSessionLocal() as session:
        assert session.get(ItemBase, 20).cn_name == "R8 左轮手枪 | 废物王 (久经沙场)"
