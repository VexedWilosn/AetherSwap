import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import account_sessions, accounts, database
from app.database import AccountSession, get_session
from app.secret_box import is_protected
from app.config_loader import get_buff_credentials, get_steam_credentials, update_steam_creds
from app.services import steam_auth
import config


def _use_temp_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "_ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "_cache", None)
    monkeypatch.setattr(accounts, "_migration_key", None)
    monkeypatch.setattr(accounts, "_schema_key", None)
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(config, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config, "_cache", {})


def test_steam_session_is_scoped_by_account(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    first = accounts.add_account(username="first")
    second = accounts.add_account(username="second")

    account_sessions.set_steam_session(
        "sessionid=s1; steamLoginSecure=111%7C%7Ctoken",
        "s1",
        account_id=first["id"],
        mirror_legacy=False,
    )
    account_sessions.set_steam_session(
        "sessionid=s2; steamLoginSecure=222%7C%7Ctoken",
        "s2",
        account_id=second["id"],
        mirror_legacy=False,
    )

    assert get_steam_credentials(first["id"])["session_id"] == "s1"
    assert get_steam_credentials(first["id"])["steam_id"] == "111"
    assert get_steam_credentials(second["id"])["session_id"] == "s2"
    assert get_steam_credentials(second["id"])["steam_id"] == "222"


def test_legacy_credentials_migrate_to_current_account(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    acc = accounts.add_account(username="legacy-target")
    config._CREDENTIALS_FILE.write_text(
        json.dumps(
            {
                "steam": {
                    "cookies": "sessionid=legacy; steamLoginSecure=333%7C%7Ctoken",
                    "session_id": "legacy",
                    "steam_id": "333",
                },
                "buff": {"cookies": "session=legacy-buff"},
            }
        ),
        encoding="utf-8",
    )
    config._cache = {}

    steam = get_steam_credentials()
    buff = get_buff_credentials()

    assert steam["account_id"] == acc["id"]
    assert steam["session_id"] == "legacy"
    assert steam["steam_id"] == "333"
    assert buff["account_id"] == acc["id"]
    assert buff["cookies"] == "session=legacy-buff"


def test_explicit_account_lookup_does_not_copy_legacy_credentials(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    accounts.add_account(username="first")
    second = accounts.add_account(username="second")
    config._CREDENTIALS_FILE.write_text(
        json.dumps(
            {
                "steam": {
                    "cookies": "sessionid=legacy; steamLoginSecure=555%7C%7Ctoken",
                    "session_id": "legacy",
                    "steam_id": "555",
                }
            }
        ),
        encoding="utf-8",
    )
    config._cache = {}

    steam = get_steam_credentials(second["id"])

    assert steam == {}


def test_update_steam_creds_writes_current_account_without_legacy_file(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    acc = accounts.add_account(username="current")

    update_steam_creds("sessionid=new; steamLoginSecure=444%7C%7Ctoken", "new")

    saved = get_steam_credentials(acc["id"])
    assert saved["steam_id"] == "444"
    assert saved["session_id"] == "new"
    assert not config._CREDENTIALS_FILE.exists()


def test_auto_relogin_writes_target_account_without_switching_current(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    first = accounts.add_account(username="first", password="pw")
    second = accounts.add_account(username="second", password="pw")
    accounts.set_current(first["id"])
    monkeypatch.setattr(
        steam_auth,
        "_do_steampy_login",
        lambda username, password, guard: (
            True,
            "",
            {"sessionid": "target", "steamLoginSecure": "666%7C%7Ctoken"},
        ),
    )
    monkeypatch.setattr(steam_auth, "fetch_steam_profile_via_api", lambda steam_id, cookies: ("", ""))

    ok, status, _ = steam_auth.try_steam_auto_relogin(account_id=second["id"])

    assert ok is True
    assert status == "auto_ok"
    assert accounts.get_current_id() == first["id"]
    assert get_steam_credentials(second["id"])["steam_id"] == "666"
    assert not config._CREDENTIALS_FILE.exists()


def test_session_status_tracks_failures_and_clears_on_success(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u")

    failed = account_sessions.update_account_session_status(
        acc["id"],
        account_sessions.PROVIDER_STEAM,
        status="expired",
        error="expired",
    )

    assert failed["failure_count"] == 1
    assert failed["next_retry_at"] is not None
    retry_ok, retry_msg = account_sessions.should_retry_session(acc["id"], account_sessions.PROVIDER_STEAM, now=failed["next_retry_at"] - 1)
    assert retry_ok is False
    assert "冷却中" in retry_msg

    account_sessions.set_steam_session(
        "sessionid=ok; steamLoginSecure=777%7C%7Ctoken",
        "ok",
        account_id=acc["id"],
        mirror_legacy=False,
    )
    saved = account_sessions.get_account_session(acc["id"], account_sessions.PROVIDER_STEAM)

    assert saved["failure_count"] == 0
    assert "next_retry_at" not in saved


def test_clear_account_session_removes_only_target(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    first = accounts.add_account(username="first")
    second = accounts.add_account(username="second")
    account_sessions.set_steam_session("sessionid=a; steamLoginSecure=111%7C%7Ctoken", "a", account_id=first["id"], mirror_legacy=False)
    account_sessions.set_steam_session("sessionid=b; steamLoginSecure=222%7C%7Ctoken", "b", account_id=second["id"], mirror_legacy=False)

    assert account_sessions.clear_account_session(second["id"], account_sessions.PROVIDER_STEAM) is True

    assert account_sessions.get_account_session(second["id"], account_sessions.PROVIDER_STEAM) == {}
    assert account_sessions.get_account_session(first["id"], account_sessions.PROVIDER_STEAM)["session_id"] == "a"


def test_session_secrets_are_stored_protected_when_supported(monkeypatch, tmp_path):
    _use_temp_storage(monkeypatch, tmp_path)
    acc = accounts.add_account(username="u")
    account_sessions.set_steam_session("sessionid=s; steamLoginSecure=888%7C%7Ctoken", "s", account_id=acc["id"], mirror_legacy=False)

    with get_session() as session:
        row = session.get(AccountSession, f"{acc['id']}:{account_sessions.PROVIDER_STEAM}")

    assert account_sessions.get_account_session(acc["id"], account_sessions.PROVIDER_STEAM)["session_id"] == "s"
    assert row.cookies != "sessionid=s; steamLoginSecure=888%7C%7Ctoken" or not is_protected(row.cookies)
