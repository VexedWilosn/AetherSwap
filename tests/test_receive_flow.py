import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import receive_flow


def test_receive_logs_missing_steam_cookie():
    logs = []

    n = receive_flow.try_receive_once(
        get_purchases=lambda: [{"_db_id": 1, "pending_receipt": True, "name": "AK-47 | Test"}],
        update_purchase=lambda idx, data: True,
        get_buff_cookies=lambda: "session=ok",
        get_steam_credentials=lambda: {"cookies": "sessionid=abc"},
        log_fn=lambda msg, level="info": logs.append((level, msg)),
    )

    assert n == 0
    assert any("steamLoginSecure" in msg for _, msg in logs)


def test_receive_accepts_offer_and_updates_purchase(monkeypatch):
    updates = []
    logs = []
    purchases = [
        {
            "_db_id": 7,
            "pending_receipt": True,
            "name": "AK-47 | Test",
            "goods_id": 123,
        }
    ]

    monkeypatch.setattr(
        receive_flow,
        "fetch_buff_steam_trade",
        lambda cookies: (
            True,
            [
                {
                    "tradeofferid": "offer-1",
                    "created_at": 1,
                    "items": [
                        {
                            "assetid": "asset-from-buff",
                            "market_hash_name": "AK-47 | Test",
                            "goods_id": 123,
                        }
                    ],
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        receive_flow,
        "accept_steam_trade_offer",
        lambda trade_offer_id, steam_cookies, log_fn=None: True,
    )
    monkeypatch.setattr(receive_flow, "jittered_sleep", lambda *args, **kwargs: None)

    n = receive_flow.try_receive_once(
        get_purchases=lambda: purchases,
        update_purchase=lambda idx, data: updates.append(("idx", idx, data)) or True,
        get_buff_cookies=lambda: "session=ok",
        get_steam_credentials=lambda: {
            "cookies": "steamLoginSecure=secure; sessionid=abc",
            "session_id": "abc",
        },
        update_purchase_by_id=lambda db_id, data: updates.append(("id", db_id, data)) or True,
        log_fn=lambda msg, level="info": logs.append((level, msg)),
    )

    assert n == 1
    assert updates == [("id", 7, {"assetid": "asset-from-buff", "pending_receipt": False})]
    assert any("收货记录已更新" in msg for _, msg in logs)
