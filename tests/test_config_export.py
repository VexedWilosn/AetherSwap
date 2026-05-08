import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes import config as config_routes
from app import accounts


def test_full_export_is_sanitized_and_keeps_transactions(monkeypatch):
    monkeypatch.setattr(
        config_routes,
        "load_app_config",
        lambda: {
            "steam_guard": {"shared_secret": "shared"},
            "steam_confirm": {"identity_secret": "identity", "device_id": "device"},
            "notify": {"pushplus_token": "push-token"},
            "mail": {"email_user": "user@example.com", "email_pass": "mail-pass"},
            "pipeline": {"max_discount": 0.8},
        },
    )
    monkeypatch.setattr(
        config_routes,
        "list_accounts",
        lambda: [
            {
                "id": "acc01",
                "username": "u",
                "password": "pw",
                "steam_id": "765",
                "steam_guard": {
                    "shared_secret": "account-shared",
                    "identity_secret": "account-identity",
                    "device_id": "account-device",
                },
                "trade_config": {"enabled": True},
            }
        ],
    )
    monkeypatch.setattr(accounts, "get_current_id", lambda: "acc01")
    monkeypatch.setattr(config_routes, "get_purchases", lambda: [{"id": "p1", "account_id": "acc01"}])
    monkeypatch.setattr(config_routes, "get_sales", lambda: [{"id": "s1", "account_id": "acc01"}])
    monkeypatch.setattr(config_routes, "get_log", lambda _offset: [])

    data = config_routes._build_full_export()

    assert data["export_mode"] == "sanitized"
    assert data["transactions"]["purchases"] == [{"id": "p1", "account_id": "acc01"}]
    assert data["transactions"]["sales"] == [{"id": "s1", "account_id": "acc01"}]
    assert data["credentials"] == {}
    assert data["account_sessions"] == []
    assert data["app_config"]["steam_guard"]["shared_secret"] == ""
    assert data["app_config"]["steam_confirm"]["identity_secret"] == ""
    assert data["app_config"]["steam_confirm"]["device_id"] == ""
    assert data["app_config"]["notify"]["pushplus_token"] == ""
    assert data["app_config"]["mail"]["email_pass"] == ""
    assert data["app_config"]["mail"]["email_user"] == "user@example.com"
    exported_account = data["accounts"]["accounts"][0]
    assert exported_account["password"] == ""
    assert exported_account["steam_guard"] == {"shared_secret": "", "identity_secret": "", "device_id": ""}
    assert exported_account["steam_id"] == "765"
