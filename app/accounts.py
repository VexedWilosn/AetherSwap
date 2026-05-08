import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

from sqlmodel import select

from app import database
from app.database import AccountRecord, AppSetting, get_session
from app.secret_box import is_protected, protect_secret, unprotect_secret

_ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "config" / "accounts.json"
_cache: Optional[dict] = None  # Backward-compatible test/reset hook; DB is the source of truth.
_migration_lock = threading.Lock()
_migration_key: Optional[tuple[str, str]] = None
_schema_key: Optional[str] = None

_SETTING_CURRENT_ID = "accounts.current_id"


def _ensure_ready() -> None:
    global _schema_key
    db_key = str(database._DB_PATH.resolve())
    if _schema_key != db_key:
        database.init_db()
        _schema_key = db_key
    _migrate_from_json_if_needed()
    _encrypt_existing_account_secrets()


def _current_migration_key() -> tuple[str, str]:
    return (str(_ACCOUNTS_FILE.resolve()), str(database._DB_PATH.resolve()))


def _migrate_from_json_if_needed() -> None:
    global _migration_key
    key = _current_migration_key()
    if _migration_key == key:
        return
    with _migration_lock:
        if _migration_key == key:
            return
        with get_session() as session:
            existing = session.exec(select(AccountRecord).limit(1)).first()
            if existing is not None or not _ACCOUNTS_FILE.exists():
                _migration_key = key
                return
            try:
                with open(_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                _migration_key = key
                return
            accounts = data.get("accounts", []) if isinstance(data, dict) else []
            now = time.time()
            seen: set[str] = set()
            for raw in accounts:
                if not isinstance(raw, dict):
                    continue
                account = _normalize_account_dict(raw)
                account_id = account.get("id") or _new_account_id(seen)
                while account_id in seen:
                    account_id = _new_account_id(seen)
                seen.add(account_id)
                account["id"] = account_id
                row = _row_from_dict(account, created_at=now, updated_at=now)
                session.add(row)
            current_id = (data.get("current_id") or "").strip() if isinstance(data, dict) else ""
            if current_id:
                session.merge(AppSetting(key=_SETTING_CURRENT_ID, value=current_id))
            session.commit()
            try:
                backup = _ACCOUNTS_FILE.with_suffix(_ACCOUNTS_FILE.suffix + ".bak")
                if backup.exists():
                    backup.unlink()
                _ACCOUNTS_FILE.rename(backup)
            except OSError:
                pass
        _migration_key = key


def _new_account_id(existing: Optional[set[str]] = None) -> str:
    existing = existing or set()
    while True:
        account_id = str(uuid.uuid4())[:8]
        if account_id in existing:
            continue
        with get_session() as session:
            if session.get(AccountRecord, account_id) is None:
                return account_id


def _safe_json_loads(value: str, fallback: dict) -> dict:
    try:
        data = json.loads(unprotect_secret(value or ""))
        return data if isinstance(data, dict) else dict(fallback)
    except Exception:
        return dict(fallback)


def _normalize_steam_guard(value: Any) -> dict:
    src = value if isinstance(value, dict) else {}
    return {
        "shared_secret": (src.get("shared_secret") or "").strip(),
        "identity_secret": (src.get("identity_secret") or "").strip(),
        "device_id": (src.get("device_id") or "").strip(),
    }


def _normalize_account_dict(value: dict) -> dict:
    src = value if isinstance(value, dict) else {}
    out = {
        "id": (src.get("id") or "").strip(),
        "enabled": bool(src.get("enabled", True)),
        "username": (src.get("username") or "").strip(),
        "password": (src.get("password") or "").strip(),
        "steam_id": (src.get("steam_id") or "").strip(),
        "display_name": (src.get("display_name") or "").strip(),
        "account_note": (src.get("account_note") or "").strip(),
        "avatar_url": (src.get("avatar_url") or "").strip(),
        "steam_guard": _normalize_steam_guard(src.get("steam_guard") or {}),
        "trade_config": dict(src.get("trade_config") or {}) if isinstance(src.get("trade_config"), dict) else {},
    }
    for key in (
        "currency_code", "region_code", "wallet_balance", "balance", "balance_display",
        "wallet_currency_id", "wallet_currency_symbol", "balance_synced_at",
    ):
        if key in src:
            out[key] = src.get(key)
    return out


def _row_from_dict(account: dict, *, created_at: Optional[float] = None, updated_at: Optional[float] = None) -> AccountRecord:
    now = time.time()
    return AccountRecord(
        id=account["id"],
        enabled=bool(account.get("enabled", True)),
        username=account.get("username", ""),
        password=protect_secret(account.get("password", "")),
        steam_id=account.get("steam_id", ""),
        display_name=account.get("display_name", ""),
        account_note=account.get("account_note", ""),
        avatar_url=account.get("avatar_url", ""),
        currency_code=account.get("currency_code"),
        region_code=account.get("region_code"),
        steam_guard_json=protect_secret(json.dumps(_normalize_steam_guard(account.get("steam_guard") or {}), ensure_ascii=False)),
        trade_config_json=json.dumps(account.get("trade_config") or {}, ensure_ascii=False),
        wallet_balance=account.get("wallet_balance"),
        balance=account.get("balance"),
        balance_display=account.get("balance_display"),
        wallet_currency_id=account.get("wallet_currency_id"),
        wallet_currency_symbol=account.get("wallet_currency_symbol"),
        balance_synced_at=account.get("balance_synced_at"),
        created_at=created_at if created_at is not None else now,
        updated_at=updated_at if updated_at is not None else now,
    )


def _row_to_dict(row: AccountRecord) -> dict:
    out = {
        "id": row.id,
        "enabled": bool(row.enabled),
        "username": row.username or "",
        "password": unprotect_secret(row.password or ""),
        "steam_id": row.steam_id or "",
        "display_name": row.display_name or "",
        "account_note": row.account_note or "",
        "avatar_url": row.avatar_url or "",
        "steam_guard": _normalize_steam_guard(_safe_json_loads(row.steam_guard_json, {})),
        "trade_config": _safe_json_loads(row.trade_config_json, {}),
    }
    optional = {
        "currency_code": row.currency_code,
        "region_code": row.region_code,
        "wallet_balance": row.wallet_balance,
        "balance": row.balance,
        "balance_display": row.balance_display,
        "wallet_currency_id": row.wallet_currency_id,
        "wallet_currency_symbol": row.wallet_currency_symbol,
        "balance_synced_at": row.balance_synced_at,
    }
    for key, value in optional.items():
        if value is not None:
            out[key] = value
    return out


def _encrypt_existing_account_secrets() -> None:
    with get_session() as session:
        rows = session.exec(select(AccountRecord)).all()
        changed = False
        for row in rows:
            if row.password and not is_protected(row.password):
                row.password = protect_secret(row.password)
                changed = True
            if row.steam_guard_json and not is_protected(row.steam_guard_json):
                row.steam_guard_json = protect_secret(row.steam_guard_json)
                changed = True
            if changed:
                session.add(row)
        if changed:
            session.commit()


def _get_setting(key: str) -> Optional[str]:
    _ensure_ready()
    with get_session() as session:
        row = session.get(AppSetting, key)
        return row.value if row else None


def list_accounts() -> List[dict]:
    _ensure_ready()
    with get_session() as session:
        rows = session.exec(select(AccountRecord).order_by(AccountRecord.created_at, AccountRecord.id)).all()
        return [_row_to_dict(row) for row in rows]


def get_current_id() -> Optional[str]:
    return _get_setting(_SETTING_CURRENT_ID)


def get_current_account() -> Optional[dict]:
    accs = list_accounts()
    cid = get_current_id()
    if not cid:
        return accs[0] if accs else None
    return next((a for a in accs if a.get("id") == cid), accs[0] if accs else None)


def get_account(account_id: str) -> Optional[dict]:
    if not account_id:
        return None
    _ensure_ready()
    with get_session() as session:
        row = session.get(AccountRecord, account_id)
        return _row_to_dict(row) if row else None


def add_account(
    username: str = "",
    password: str = "",
    steam_id: str = "",
    display_name: str = "",
    avatar_url: str = "",
    account_note: str = "",
) -> dict:
    _ensure_ready()
    account_id = _new_account_id()
    account = {
        "id": account_id,
        "enabled": True,
        "username": (username or "").strip(),
        "password": (password or "").strip(),
        "steam_id": (steam_id or "").strip(),
        "display_name": (display_name or "").strip(),
        "account_note": (account_note or "").strip(),
        "avatar_url": (avatar_url or "").strip(),
        "steam_guard": {"shared_secret": "", "identity_secret": "", "device_id": ""},
        "trade_config": {},
    }
    with get_session() as session:
        session.add(_row_from_dict(account))
        if not session.get(AppSetting, _SETTING_CURRENT_ID):
            session.add(AppSetting(key=_SETTING_CURRENT_ID, value=account_id))
        session.commit()
    return account


def update_account(account_id: str, **kwargs: Any) -> Optional[dict]:
    _ensure_ready()
    allowed = (
        "enabled", "username", "password", "steam_id", "display_name", "account_note", "avatar_url",
        "currency_code", "region_code", "steam_guard", "trade_config",
        "wallet_balance", "balance", "balance_display", "wallet_currency_id",
        "wallet_currency_symbol", "balance_synced_at",
    )
    with get_session() as session:
        row = session.get(AccountRecord, account_id)
        if row is None:
            return None
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == "steam_guard":
                row.steam_guard_json = protect_secret(json.dumps(_normalize_steam_guard(value), ensure_ascii=False))
            elif key == "trade_config":
                payload = dict(value or {}) if isinstance(value, dict) else {}
                row.trade_config_json = json.dumps(payload, ensure_ascii=False)
            elif hasattr(row, key):
                clean_value = (value or "").strip() if isinstance(value, str) else value
                if key == "password":
                    clean_value = protect_secret(clean_value)
                setattr(row, key, clean_value)
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def delete_account(account_id: str) -> bool:
    _ensure_ready()
    with get_session() as session:
        row = session.get(AccountRecord, account_id)
        if row is None:
            return False
        session.delete(row)
        setting = session.get(AppSetting, _SETTING_CURRENT_ID)
        if setting and setting.value == account_id:
            next_row = session.exec(select(AccountRecord).where(AccountRecord.id != account_id).order_by(AccountRecord.created_at, AccountRecord.id)).first()
            if next_row is not None:
                setting.value = next_row.id
                session.add(setting)
            else:
                session.delete(setting)
        session.commit()
        return True


def set_current(account_id: str) -> bool:
    _ensure_ready()
    with get_session() as session:
        row = session.get(AccountRecord, account_id)
        if row is None:
            return False
        setting = session.get(AppSetting, _SETTING_CURRENT_ID)
        if setting is None:
            setting = AppSetting(key=_SETTING_CURRENT_ID, value=account_id)
        else:
            setting.value = account_id
        session.add(setting)
        session.commit()
        return True


def replace_all(data: dict) -> None:
    _ensure_ready()
    from sqlmodel import delete as sql_delete

    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    current_id = (data.get("current_id") or "").strip() if isinstance(data, dict) else ""
    now = time.time()
    with get_session() as session:
        session.exec(sql_delete(AccountRecord))
        session.exec(sql_delete(AppSetting).where(AppSetting.key == _SETTING_CURRENT_ID))
        seen: set[str] = set()
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account = _normalize_account_dict(raw)
            account_id = account.get("id") or _new_account_id(seen)
            while account_id in seen:
                account_id = _new_account_id(seen)
            seen.add(account_id)
            account["id"] = account_id
            session.add(_row_from_dict(account, created_at=now, updated_at=now))
        if current_id:
            session.add(AppSetting(key=_SETTING_CURRENT_ID, value=current_id))
        session.commit()


def get_account_steam_guard(account: Optional[dict], cfg: Optional[dict] = None) -> dict:
    """Return account-level Steam Guard secrets with global config fallback."""
    acc_guard = _normalize_steam_guard((account or {}).get("steam_guard") or {})
    cfg = cfg or {}
    cfg_guard = cfg.get("steam_guard") or {}
    cfg_confirm = cfg.get("steam_confirm") or {}
    return {
        "shared_secret": acc_guard.get("shared_secret") or (cfg_guard.get("shared_secret") or "").strip(),
        "identity_secret": acc_guard.get("identity_secret") or (cfg_confirm.get("identity_secret") or "").strip(),
        "device_id": acc_guard.get("device_id") or (cfg_confirm.get("device_id") or "").strip(),
    }


def public_account(account: dict, cfg: Optional[dict] = None) -> dict:
    """Return an account payload with compatibility guard status metadata."""
    out = copy.deepcopy(account)
    out["has_password"] = bool((out.get("password") or "").strip())
    out.pop("password", None)
    out["account_note"] = (out.get("account_note") or "").strip()
    guard = _normalize_steam_guard(out.get("steam_guard") or {})
    resolved = get_account_steam_guard(out, cfg)
    out["steam_guard"] = guard
    out["steam_guard_status"] = {
        "account_configured": bool(guard.get("shared_secret")),
        "resolved_configured": bool(resolved.get("shared_secret")),
        "identity_configured": bool(resolved.get("identity_secret") and resolved.get("device_id")),
    }
    return out


def get_profile_dir(account_id: Optional[str] = None) -> Path:
    base = Path(__file__).resolve().parent.parent / "config" / "playwright_steam"
    if account_id:
        return base / account_id
    cur = get_current_account()
    if cur:
        return base / cur.get("id", "default")
    return base / "default"
