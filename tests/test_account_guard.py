import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import accounts
from app import database
from app.services.steam_auth import _build_steam_guard_dict


def _use_temp_accounts_file(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "_ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "_cache", None)
    monkeypatch.setattr(accounts, "_migration_key", None)
    monkeypatch.setattr(accounts, "_schema_key", None)
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)


def test_account_steam_guard_saved_and_preferred(monkeypatch, tmp_path):
    _use_temp_accounts_file(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u", steam_id="765")
    accounts.update_account(
        acc["id"],
        steam_guard={
            "shared_secret": "account-shared",
            "identity_secret": "account-identity",
            "device_id": "android:account",
        },
    )
    acc = accounts.get_account(acc["id"])
    cfg = {
        "steam_guard": {"shared_secret": "global-shared"},
        "steam_confirm": {"identity_secret": "global-identity", "device_id": "android:global"},
    }

    guard = accounts.get_account_steam_guard(acc, cfg)

    assert guard["shared_secret"] == "account-shared"
    assert guard["identity_secret"] == "account-identity"
    assert guard["device_id"] == "android:account"


def test_account_steam_guard_falls_back_to_global(monkeypatch, tmp_path):
    _use_temp_accounts_file(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u", steam_id="765")
    cfg = {
        "steam_guard": {"shared_secret": "global-shared"},
        "steam_confirm": {"identity_secret": "global-identity", "device_id": "android:global"},
    }

    guard = accounts.get_account_steam_guard(acc, cfg)

    assert guard["shared_secret"] == "global-shared"
    assert guard["identity_secret"] == "global-identity"
    assert guard["device_id"] == "android:global"


def test_steam_auth_builds_guard_from_account_first(monkeypatch, tmp_path):
    _use_temp_accounts_file(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u", steam_id="765")
    accounts.update_account(
        acc["id"],
        steam_guard={
            "shared_secret": "YWJjZA==",
            "identity_secret": "account-identity",
            "device_id": "android:account",
        },
    )

    guard = _build_steam_guard_dict(accounts.get_account(acc["id"]), {})

    assert guard["steamid"] == "765"
    assert guard["shared_secret"] == "YWJjZA=="
    assert guard["identity_secret"] == "account-identity"
    assert guard["device_id"] == "android:account"


def test_accounts_json_migrates_to_sqlite(monkeypatch, tmp_path):
    _use_temp_accounts_file(monkeypatch, tmp_path)
    accounts._ACCOUNTS_FILE.write_text(
        """
{
  "current_id": "legacy01",
  "accounts": [
    {
      "id": "legacy01",
      "username": "legacy-user",
      "password": "pw",
      "steam_id": "765",
      "display_name": "Legacy",
      "account_note": "note",
      "balance_synced_at": "2026-05-08T17:48:13.560044+00:00",
      "steam_guard": {
        "shared_secret": "shared",
        "identity_secret": "identity",
        "device_id": "android:legacy"
      },
      "trade_config": {"enabled": true}
    }
  ]
}
""",
        encoding="utf-8",
    )

    migrated = accounts.list_accounts()

    assert len(migrated) == 1
    assert migrated[0]["id"] == "legacy01"
    assert migrated[0]["username"] == "legacy-user"
    assert migrated[0]["balance_synced_at"] == "2026-05-08T17:48:13.560044+00:00"
    assert migrated[0]["steam_guard"]["shared_secret"] == "shared"
    assert migrated[0]["trade_config"]["enabled"] is True
    assert accounts.get_current_id() == "legacy01"


def test_account_enabled_defaults_and_updates(monkeypatch, tmp_path):
    _use_temp_accounts_file(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u")

    assert accounts.get_account(acc["id"])["enabled"] is True

    updated = accounts.update_account(acc["id"], enabled=False)

    assert updated["enabled"] is False
    assert accounts.public_account(updated)["enabled"] is False
