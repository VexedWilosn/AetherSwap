from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from DataEngine import radar_snapshot as radar_mod
from DataEngine.database import Base, ItemBase, MarketPrice, RadarSnapshot
from DataEngine.profit_model import steam_sale_net_price
from DataEngine.radar_snapshot import build_radar_entries, refresh_radar_snapshots, upsert_radar_snapshots


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(radar_mod, "SessionLocal", TestingSessionLocal)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_radar_snapshot_treats_steamdt_openapi_volume_as_depth(db_session):
    item = ItemBase(id=910001, market_hash_name="Radar Snapshot Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=10.0,
            buy_max=9.0,
            volume=9999,
            sell_volume=10,
            buy_volume=20,
            orderbook_depth=30,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=5.0,
            buy_max=4.0,
            volume=8888,
            sell_volume=7,
            buy_volume=11,
            orderbook_depth=18,
            liquidity_score=2.5,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["steam"]["volume"] == 0
    assert entry["platforms"]["buff"]["volume"] == 0
    assert entry["depth"] == 18
    assert entry["volume"] == 0

    saved = upsert_radar_snapshots(db_session, entries)
    db_session.commit()
    assert saved == 1
    snap = db_session.get(RadarSnapshot, item.id)
    assert snap is not None
    assert snap.volume == 0
    assert snap.depth == 18
    assert round(snap.cash_to_steam_profit_rate, 2) == round(((steam_sale_net_price(10.0) - 5.0) / 5.0) * 100.0, 2)
    assert round(snap.steam_to_cash_profit_rate, 2) == round(((4.0 * 0.975 - 10.0 * 0.85) / (10.0 * 0.85)) * 100.0, 2)


def test_radar_snapshot_can_rank_steam_balance_cashout(db_session):
    item = ItemBase(id=910003, market_hash_name="Steam Cashout Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            sell_volume=5,
            buy_volume=5,
            orderbook_depth=10,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=110.0,
            buy_max=93.0,
            sell_volume=5,
            buy_volume=5,
            orderbook_depth=10,
            liquidity_score=2.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.8}})
    entry = entries[0]
    assert entry["best_direction"] == "steam_to_cash"
    assert entry["best_platform"] == "buff"
    assert entry["best_platform_price"] == 110.0
    assert entry["steam_to_cash_profit_rate"] > 0
    assert entry["cash_to_steam_profit_rate"] < 0
    assert entry["steam_to_cash_platform"] == "buff"
    assert entry["steam_to_cash_price"] == 93.0


def test_radar_response_mode_uses_lowest_valid_cashout_bid(db_session):
    item = ItemBase(id=910005, market_hash_name="Mode Platform Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=80.0,
            buy_max=70.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=95.0,
            buy_max=92.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.8}})
    upsert_radar_snapshots(db_session, entries)
    db_session.commit()

    snap = db_session.get(RadarSnapshot, item.id)
    assert snap.cash_to_steam_platform == "buff"
    assert snap.steam_to_cash_platform == "buff"
    from app.api import radar_row_mode_payload

    payload = radar_row_mode_payload(snap, opportunity_mode="steam_to_cash")
    assert payload["mode_platform"] == "buff"
    assert payload["mode_price"] == 70.0


def test_radar_method_payload_ignores_extreme_platform_listing_outlier(db_session):
    item = ItemBase(id=910022, market_hash_name="Outlier Listing Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=90.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=500.0,
            buy_max=80.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="eco",
            data_source="steamdt_openapi",
            sell_min=90.0,
            buy_max=81.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})[0]
    upsert_radar_snapshots(db_session, [entry])
    db_session.commit()
    snap = db_session.get(RadarSnapshot, item.id)
    from app.api import radar_row_mode_payload

    payload = json.loads(snap.platform_payload_json)
    mode = radar_row_mode_payload(snap, opportunity_mode="best", payload=payload, cashout_price_mode="listing")

    assert mode["steam_to_cash_platform"] == "eco"
    assert mode["steam_to_cash_price"] == 90.0
    assert "buff_sell_min" in mode["ignored_outliers"]


def test_radar_response_mode_can_use_lowest_nonzero_listing_for_cashout(db_session):
    item = ItemBase(id=910015, market_hash_name="Cashout Listing Mode Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=0.0,
            buy_max=70.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=92.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="eco",
            data_source="eco",
            sell_min=88.0,
            buy_max=82.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.8}})
    upsert_radar_snapshots(db_session, entries)
    db_session.commit()

    snap = db_session.get(RadarSnapshot, item.id)
    from app.api import radar_row_mode_payload

    payload = json.loads(snap.platform_payload_json)
    mode = radar_row_mode_payload(snap, opportunity_mode="steam_to_cash", payload=payload, cashout_price_mode="listing")
    assert mode["steam_to_cash_platform"] == "eco"
    assert mode["steam_to_cash_price"] == 88.0
    assert mode["steam_to_cash_price_mode"] == "listing"


def test_radar_row_mode_payload_can_override_balance_cost_ratio(db_session):
    item = ItemBase(id=910016, market_hash_name="Cashout Ratio Override Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=80.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})
    upsert_radar_snapshots(db_session, entries)
    db_session.commit()

    snap = db_session.get(RadarSnapshot, item.id)
    from app.api import radar_row_mode_payload

    default_mode = radar_row_mode_payload(snap, opportunity_mode="steam_to_cash")
    override_mode = radar_row_mode_payload(snap, opportunity_mode="steam_to_cash", steam_balance_cost_ratio_override=0.75)
    assert default_mode["steam_to_cash_profit_rate"] < 0
    assert override_mode["steam_to_cash_profit_rate"] > 0
    assert override_mode["steam_to_cash_profit_rate"] == round(((80.0 * 0.975 - 100.0 * 0.75) / (100.0 * 0.75)) * 100.0, 2)


def test_radar_api_dynamic_cashout_sort_considers_all_matching_rows(monkeypatch):
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
        for idx in range(1, 23):
            session.add(
                RadarSnapshot(
                    item_id=920000 + idx,
                    item_name=f"High Balance Gain {idx}",
                    market_hash_name=f"High Balance Gain {idx}",
                    crawl_priority=3,
                    best_platform="eco",
                    best_platform_price=1.0,
                    steam_sell_min=100.0,
                    best_profit_rate=50.0 + idx,
                    cash_to_steam_profit_rate=50.0 + idx,
                    cash_to_steam_platform="eco",
                    cash_to_steam_price=1.0,
                    steam_to_cash_profit_rate=-20.0,
                    steam_to_cash_platform="eco",
                    steam_to_cash_price=50.0,
                    platform_payload_json='{"steam":{"sell_min":100.0},"platforms":{"eco":{"sell_min":1.0,"buy_max":50.0}}}',
                    snapshot_updated_at=datetime.now(),
                )
            )
        session.add(
            RadarSnapshot(
                item_id=930001,
                item_name="Positive Dynamic Cashout",
                market_hash_name="Positive Dynamic Cashout",
                crawl_priority=3,
                best_platform="buff",
                best_platform_price=99.0,
                steam_sell_min=100.0,
                best_profit_rate=1.0,
                cash_to_steam_profit_rate=1.0,
                cash_to_steam_platform="buff",
                cash_to_steam_price=99.0,
                steam_to_cash_profit_rate=-8.24,
                steam_to_cash_platform="buff",
                steam_to_cash_price=78.0,
                platform_payload_json='{"steam":{"sell_min":100.0},"platforms":{"buff":{"sell_min":99.0,"buy_max":78.0}}}',
                snapshot_updated_at=datetime.now(),
            )
        )
        session.commit()

    client = TestClient(api.app)
    response = client.get(
        "/api/market/radar",
        params={
            "limit": 10,
            "offset": 0,
            "sort_by": "steam_to_cash",
            "sort_dir": "desc",
            "opportunity_mode": "steam_to_cash",
            "steam_balance_cost_ratio": 0.65,
            "only_profitable": "false",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["items"][0]["market_hash_name"] == "Positive Dynamic Cashout"
    assert data["items"][0]["steam_to_cash_profit_rate"] > 0
    assert data["items"][0]["mode_direction"] == "steam_to_cash"
    assert data["items"][0]["mode_profit_rate"] == data["items"][0]["steam_to_cash_profit_rate"]
    engine.dispose()


def test_radar_api_dynamic_payload_updates_cash_to_steam_fields(monkeypatch):
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
        session.add(
            RadarSnapshot(
                item_id=930002,
                item_name="Order Buy Dynamic Balance Gain",
                market_hash_name="Order Buy Dynamic Balance Gain",
                crawl_priority=3,
                best_platform="buff",
                best_platform_price=80.0,
                steam_sell_min=100.0,
                best_profit_rate=-1.0,
                cash_to_steam_profit_rate=-1.0,
                cash_to_steam_profit_cny=-0.8,
                cash_to_steam_platform="buff",
                cash_to_steam_price=80.0,
                steam_to_cash_profit_rate=-20.0,
                steam_to_cash_platform="buff",
                steam_to_cash_price=40.0,
                platform_payload_json='{"steam":{"sell_min":100.0},"platforms":{"buff":{"sell_min":80.0,"buy_max":60.0},"eco":{"sell_min":90.0,"buy_max":50.0}}}',
                snapshot_updated_at=datetime.now(),
            )
        )
        session.commit()

    client = TestClient(api.app)
    response = client.get(
        "/api/market/radar",
        params={
            "limit": 10,
            "offset": 0,
            "sort_by": "profit_rate",
            "sort_dir": "desc",
            "buy_price_mode": "order",
            "only_profitable": "false",
        },
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["cash_to_steam_platform"] == "eco"
    assert item["cash_to_steam_price"] == 50.0
    assert item["cash_to_steam_profit_rate"] > 60
    assert item["mode_direction"] == "cash_to_steam"
    assert item["mode_profit_rate"] == item["cash_to_steam_profit_rate"]
    engine.dispose()


def test_radar_row_mode_payload_bid_mode_uses_best_cashout_return():
    from app.api import radar_row_mode_payload

    snap = RadarSnapshot(
        item_id=930003,
        item_name="Best Bid Cashout",
        market_hash_name="Best Bid Cashout",
        crawl_priority=3,
        best_platform="eco",
        best_platform_price=1.0,
        steam_sell_min=100.0,
        steam_buy_max=100.0,
        best_profit_rate=-10.0,
        cash_to_steam_profit_rate=-10.0,
        cash_to_steam_platform="eco",
        cash_to_steam_price=1.0,
        steam_to_cash_profit_rate=-30.0,
        steam_to_cash_platform="buff",
        steam_to_cash_price=60.0,
        steam_balance_cost_ratio=0.65,
    )
    payload = {
        "steam": {"sell_min": 100.0, "buy_max": 100.0},
        "platforms": {
            "buff": {"sell_min": 80.0, "buy_max": 60.0},
            "eco": {"sell_min": 95.0, "buy_max": 78.0},
        },
    }

    mode = radar_row_mode_payload(
        snap,
        opportunity_mode="steam_to_cash",
        payload=payload,
        cashout_price_mode="bid",
        steam_balance_cost_ratio_override=0.65,
    )

    assert mode["steam_to_cash_platform"] == "eco"
    assert mode["steam_to_cash_price"] == 78.0
    assert mode["steam_to_cash_profit_rate"] > 15


def test_radar_snapshot_does_not_show_bid_as_listing_price(db_session):
    item = ItemBase(id=910009, market_hash_name="No Listing Bid Only Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=20.0,
            buy_max=0.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=0.0,
            buy_max=6.3,
            sell_volume=0,
            buy_volume=3,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=69.0,
            buy_max=0.0,
            sell_volume=5,
            buy_volume=0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.8}})[0]

    assert entry["best_platform"] == "buff"
    assert entry["best_platform_price"] == 69.0
    assert entry["cash_to_steam_price"] == 69.0
    assert entry["steam_to_cash_platform"] == "uuyp"
    assert entry["steam_to_cash_price"] == 6.3


def test_radar_snapshot_ignores_impossible_platform_bid(db_session):
    item = ItemBase(id=910006, market_hash_name="Impossible Bid Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=33.83,
            buy_max=30.09,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=19.35,
            buy_max=3210.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=18.78,
            buy_max=18.1,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entries = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})
    entry = entries[0]
    assert entry["platforms"]["uuyp"]["buy_max"] == 0.0
    assert entry["steam_to_cash_platform"] == "buff"
    assert entry["best_direction"] == "cash_to_steam"


def test_radar_snapshot_drops_doppler_like_peer_bid_outlier(db_session):
    item = ItemBase(id=910010, market_hash_name="Doppler Outlier Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=1153.83,
            buy_max=0.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=833.0,
            buy_max=791.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=824.5,
            buy_max=1930.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})[0]

    assert entry["platforms"]["uuyp"]["buy_max"] == 0.0
    assert entry["steam_to_cash_platform"] == "buff"
    assert entry["steam_to_cash_price"] == 791.0


def test_radar_snapshot_cashout_uses_lowest_valid_bid_even_when_higher_bid_exists(db_session):
    item = ItemBase(id=910011, market_hash_name="Conservative Cashout Bid Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=92.0,
            buy_max=82.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=91.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.75}})[0]

    assert entry["steam_to_cash_platform"] == "buff"
    assert entry["steam_to_cash_price"] == 82.0
    assert round(entry["steam_to_cash_profit_rate"], 2) == round(((82.0 * 0.975 - 100.0 * 0.75) / (100.0 * 0.75)) * 100.0, 2)


def test_radar_snapshot_cashout_ignores_zero_bid_then_uses_lowest_nonzero_bid(db_session):
    item = ItemBase(id=910014, market_hash_name="Lowest Nonzero Cashout Bid Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=92.0,
            buy_max=0.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="eco",
            data_source="eco",
            sell_min=93.0,
            buy_max=82.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="steamdt_openapi",
            sell_min=91.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.75}})[0]

    assert entry["steam_to_cash_platform"] == "eco"
    assert entry["steam_to_cash_price"] == 82.0


def test_radar_snapshot_ignores_baseline_for_profit(db_session):
    item = ItemBase(id=910012, market_hash_name="Baseline Placeholder Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=0.47,
            buy_max=0.42,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="uuyp",
            data_source="baseline",
            sell_min=0.01,
            buy_max=0.0,
            sell_top5_avg=0.0,
            buy_top5_avg=0.0,
            volume=0,
            sell_volume=0,
            buy_volume=0,
            orderbook_depth=0,
            liquidity_score=0.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=0.4,
            buy_max=0.35,
            sell_volume=5,
            buy_volume=4,
            orderbook_depth=9,
            liquidity_score=1.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})[0]

    assert entry["platforms"]["uuyp"]["ignored_for_profit"] is True
    assert entry["platforms"]["uuyp"]["cash_to_steam_profit_rate"] == 0.0
    assert entry["best_platform"] == "buff"
    assert entry["cash_to_steam_platform"] == "buff"
    assert entry["cash_to_steam_profit_rate"] < 10_000


def test_radar_snapshot_ignores_baseline_with_depth_for_profit(db_session):
    item = ItemBase(id=910013, market_hash_name="Baseline With Depth Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=6.28,
            buy_max=0.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="baseline",
            sell_min=0.24,
            buy_max=0.16,
            sell_volume=54,
            buy_volume=114,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="eco",
            data_source="eco",
            sell_min=8.74,
            buy_max=0.0,
            volume=9,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    entry = build_radar_entries(db_session, item_ids=[item.id], config={"pipeline": {"steam_balance_cost_ratio": 0.85}})[0]

    assert entry["platforms"]["buff"]["ignored_for_profit"] is True
    assert entry["platforms"]["buff"]["cash_to_steam_profit_rate"] == 0.0
    assert entry["best_platform"] == "eco"
    assert entry["cash_to_steam_platform"] == "eco"


def test_radar_profit_sort_uses_selected_mode_column():
    from app.api import radar_mode_columns

    profit_col, price_col = radar_mode_columns("steam_to_cash")
    assert profit_col is RadarSnapshot.steam_to_cash_profit_rate
    assert price_col is RadarSnapshot.steam_to_cash_price


def test_api_detects_stale_snapshot_with_conditional_bid_outlier(db_session):
    from app.api import (
        radar_payload_has_baseline_profit,
        radar_payload_has_conditional_bid_outlier,
        radar_snapshot_has_baseline_profit,
        radar_snapshot_has_conditional_bid_outlier,
        radar_snapshot_warning_flags,
    )

    snap = RadarSnapshot(
        item_id=910007,
        item_name="Crossed Snapshot",
        market_hash_name="Crossed Snapshot",
        crawl_priority=3,
        best_platform="uuyp",
        best_platform_price=1070.0,
        steam_to_cash_profit_rate=8000.0,
        steam_to_cash_platform="uuyp",
        steam_to_cash_price=1070.0,
        platform_payload_json='{"platforms":{"buff":{"sell_min":9.08,"buy_max":9.0},"uuyp":{"sell_min":8.99,"buy_max":1070.0}}}',
    )
    assert radar_snapshot_has_conditional_bid_outlier(snap) is True
    flags = radar_snapshot_warning_flags(snap, {"steam": {"sell_min": 10, "buy_max": 0}})
    assert "conditional_bid_outlier" in flags
    assert "steam_bid_missing" in flags
    assert "suspicious_profit" in flags
    assert radar_payload_has_conditional_bid_outlier({"platforms": {"buff": {"buy_max": "bad"}}}) is False

    baseline_snap = RadarSnapshot(
        item_id=910008,
        item_name="Baseline Snapshot",
        market_hash_name="Baseline Snapshot",
        crawl_priority=3,
        cash_to_steam_profit_rate=3000.0,
        cash_to_steam_platform="uuyp",
        platform_payload_json='{"platforms":{"buff":{"sell_min":0.12,"buy_max":0.02,"data_source":"steamdt_openapi"},"uuyp":{"sell_min":0.01,"buy_max":0,"sell_top5_avg":0,"buy_top5_avg":0,"volume":0,"sell_volume":0,"buy_volume":0,"orderbook_depth":0,"liquidity_score":0,"data_source":"baseline"}}}',
    )
    assert radar_payload_has_baseline_profit(baseline_snap.platform_payload_json, "uuyp") is True
    assert radar_snapshot_has_baseline_profit(baseline_snap) is True
    assert "baseline_price" in radar_snapshot_warning_flags(baseline_snap)


def test_refresh_radar_snapshots_uses_loaded_app_config(db_session, monkeypatch):
    item = ItemBase(id=910004, market_hash_name="Config Ratio Test", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steamdt_openapi",
            sell_min=100.0,
            buy_max=95.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="buff",
            data_source="steamdt_openapi",
            sell_min=120.0,
            buy_max=90.0,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()
    monkeypatch.setattr(radar_mod, "load_app_config", lambda: {"pipeline": {"steam_balance_cost_ratio": 0.75}})

    assert refresh_radar_snapshots([item.id]) == 1
    snap = db_session.get(RadarSnapshot, item.id)
    assert snap is not None
    assert snap.steam_balance_cost_ratio == 0.75
    assert round(snap.steam_to_cash_profit_rate, 2) == round(((90.0 * 0.975 - 100.0 * 0.75) / (100.0 * 0.75)) * 100.0, 2)


def test_radar_snapshot_refresh_removes_unwatched_items(db_session):
    item = ItemBase(id=910002, market_hash_name="Radar Snapshot Removed", crawl_priority=3, is_active=True)
    db_session.add(item)
    db_session.add(
        MarketPrice(
            item_id=item.id,
            platform_name="steam",
            data_source="steam",
            sell_min=10.0,
            buy_max=9.0,
            volume=12,
            updated_at=datetime.now(),
            currency="CNY",
        )
    )
    db_session.commit()

    assert refresh_radar_snapshots([item.id]) == 1
    assert db_session.get(RadarSnapshot, item.id) is not None

    item.crawl_priority = 0
    db_session.add(item)
    db_session.commit()
    assert refresh_radar_snapshots([item.id]) == 0
    db_session.expire_all()
    assert db_session.get(RadarSnapshot, item.id) is None
