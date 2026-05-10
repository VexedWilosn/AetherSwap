from sqlmodel import Session, SQLModel, create_engine, select

from app.database import PlatformAction
from app.services.trading.sell_scanner import SellerSnapshotScanner
from app.services.trading.states import PlatformActionType


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sell_scanner.db'}")
    SQLModel.metadata.create_all(engine, tables=[PlatformAction.__table__])

    def factory():
        return Session(engine)

    return factory


def test_seller_snapshot_scanner_collects_inventory_listings_and_c5_orders(tmp_path):
    def inventory():
        return True, [
            {
                "item_id": 1,
                "market_hash_name": "AK-47 | Redline (Field-Tested)",
                "assetid": "asset-1",
                "can_sell": True,
                "target_price": 88.8,
            },
            {
                "item_id": 2,
                "market_hash_name": "M4A1-S | Printstream (Field-Tested)",
                "assetid": "asset-listed",
                "can_sell": True,
                "target_price": 77.7,
            },
        ], ""

    def listings(cookies):
        assert cookies == "steam-cookie"
        return True, {"asset-listed"}, "", {"asset-listed": "M4A1-S | Printstream (Field-Tested)"}

    scanner = SellerSnapshotScanner(
        inventory_scanner=inventory,
        steam_listings_scanner=listings,
        credentials_loader=lambda: {"steam": {"cookies": "steam-cookie", "steam_id": "7656"}},
    )

    result = scanner.plan({"include_c5_orders": False, "listing_platform": "steam"})

    assert result.scan.diagnostics["sources"]["inventory"]["ok"] is True
    assert result.scan.snapshot["active_assetids"] == ["asset-listed"]
    assert len(result.plan.actions) == 1
    assert result.plan.actions[0]["action_type"] == PlatformActionType.STEAM_LISTING
    assert result.plan.actions[0]["assetid"] == "asset-1"
    assert result.plan.skipped[0]["reason"] == "already_listed"


def test_seller_snapshot_scanner_commit_writes_planned_actions(tmp_path):
    scanner = SellerSnapshotScanner(
        inventory_scanner=lambda: (True, [], ""),
        steam_listings_scanner=lambda cookies: (True, set(), "", {}),
        credentials_loader=lambda: {"steam": {"cookies": "", "steam_id": "7656"}},
    )
    session_factory = _session_factory(tmp_path)

    result = scanner.plan_and_create(
        session_factory,
        {
            "include_inventory": False,
            "include_steam_listings": False,
            "include_c5_orders": False,
            "orders": [
                {
                    "item_id": 3,
                    "market_hash_name": "AWP | Asiimov (Field-Tested)",
                    "orderId": "c5-order-1",
                    "status": 2,
                    "orderConfirmInfoDTO": {"offerId": "offer-1"},
                }
            ],
        },
    )

    assert len(result.created) == 1
    assert result.created[0].action.action_type == PlatformActionType.ACCEPT_TRADE_OFFER
    with session_factory() as session:
        rows = session.execute(select(PlatformAction)).scalars().all()
        assert len(rows) == 1
        assert rows[0].trade_offer_id == "offer-1"
