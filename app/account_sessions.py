import time
from typing import Optional

from sqlmodel import select

from app import database
from app.accounts import get_current_account
from app.database import AccountSession, get_session
from config import get_buff as get_legacy_buff
from config import get_steam as get_legacy_steam
from config import update_buff_credentials, update_steam_credentials

PROVIDER_STEAM = "steam"
PROVIDER_BUFF = "buff"


def _ensure_ready() -> None:
    database.init_db()


def _resolve_account_id(account_id: Optional[str] = None) -> str:
    if account_id:
        return str(account_id).strip()
    account = get_current_account() or {}
    return str(account.get("id") or "").strip()


def _session_key(account_id: str, provider: str) -> str:
    return f"{account_id}:{provider}"


def _steam_id_from_cookies(cookies: str) -> Optional[str]:
    for part in (cookies or "").split(";"):
        part = part.strip()
        if part.lower().startswith("steamloginsecure="):
            val = part.split("=", 1)[1].strip()
            if "%7C%7C" in val:
                return val.split("%7C%7C")[0].strip()
            if "||" in val:
                return val.split("||")[0].strip()
            if val.isdigit():
                return val
    return None


def _session_to_dict(row: AccountSession) -> dict:
    out = {
        "account_id": row.account_id,
        "provider": row.provider,
        "cookies": row.cookies or "",
        "status": row.status or "",
    }
    if row.session_id is not None:
        out["session_id"] = row.session_id
    if row.steam_id is not None:
        out["steam_id"] = row.steam_id
    if row.error is not None:
        out["error"] = row.error
    if row.last_validated_at is not None:
        out["last_validated_at"] = row.last_validated_at
    return out


def get_account_session(account_id: Optional[str], provider: str) -> dict:
    _ensure_ready()
    account_id = _resolve_account_id(account_id)
    provider = (provider or "").strip().lower()
    if not account_id or not provider:
        return {}
    with get_session() as session:
        row = session.get(AccountSession, _session_key(account_id, provider))
        return _session_to_dict(row) if row else {}


def set_account_session(
    account_id: Optional[str],
    provider: str,
    *,
    cookies: str = "",
    session_id: Optional[str] = None,
    steam_id: Optional[str] = None,
    status: str = "ok",
    error: Optional[str] = None,
    last_validated_at: Optional[float] = None,
) -> dict:
    _ensure_ready()
    account_id = _resolve_account_id(account_id)
    provider = (provider or "").strip().lower()
    if not account_id or not provider:
        return {}
    now = time.time()
    with get_session() as session:
        key = _session_key(account_id, provider)
        row = session.get(AccountSession, key)
        if row is None:
            row = AccountSession(
                id=key,
                account_id=account_id,
                provider=provider,
                created_at=now,
            )
        row.cookies = cookies or ""
        row.session_id = session_id
        row.steam_id = steam_id
        row.status = status or ""
        row.error = error
        row.last_validated_at = last_validated_at
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def list_account_sessions() -> list:
    _ensure_ready()
    with get_session() as session:
        rows = session.exec(select(AccountSession).order_by(AccountSession.account_id, AccountSession.provider)).all()
        return [_session_to_dict(row) for row in rows]


def replace_account_sessions(rows: list) -> None:
    _ensure_ready()
    from sqlmodel import delete as sql_delete

    with get_session() as session:
        session.exec(sql_delete(AccountSession))
        now = time.time()
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            account_id = str(item.get("account_id") or "").strip()
            provider = str(item.get("provider") or "").strip().lower()
            if not account_id or not provider:
                continue
            row = AccountSession(
                id=_session_key(account_id, provider),
                account_id=account_id,
                provider=provider,
                cookies=item.get("cookies") or "",
                session_id=item.get("session_id"),
                steam_id=item.get("steam_id"),
                status=item.get("status") or "",
                error=item.get("error"),
                last_validated_at=item.get("last_validated_at"),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        session.commit()


def get_steam_session(account_id: Optional[str] = None) -> dict:
    explicit_account = bool(account_id)
    account_id = _resolve_account_id(account_id)
    if not account_id:
        return get_legacy_steam()
    current = get_account_session(account_id, PROVIDER_STEAM)
    if current.get("cookies"):
        return current
    legacy = get_legacy_steam()
    if not explicit_account and (legacy.get("cookies") or "").strip():
        return set_steam_session(
            cookies=legacy.get("cookies") or "",
            session_id=legacy.get("session_id") or "",
            steam_id=legacy.get("steam_id") or None,
            account_id=account_id,
            mirror_legacy=False,
        )
    return current


def set_steam_session(
    cookies: str,
    session_id: str,
    *,
    account_id: Optional[str] = None,
    steam_id: Optional[str] = None,
    mirror_legacy: bool = True,
) -> dict:
    resolved_steam_id = steam_id or _steam_id_from_cookies(cookies)
    saved = set_account_session(
        account_id,
        PROVIDER_STEAM,
        cookies=cookies,
        session_id=session_id or "",
        steam_id=resolved_steam_id,
        status="ok" if cookies else "",
    )
    if mirror_legacy:
        update_steam_credentials(cookies, session_id or "", resolved_steam_id)
    return saved


def get_buff_session(account_id: Optional[str] = None) -> dict:
    explicit_account = bool(account_id)
    account_id = _resolve_account_id(account_id)
    if not account_id:
        return get_legacy_buff()
    current = get_account_session(account_id, PROVIDER_BUFF)
    if current.get("cookies"):
        return current
    legacy = get_legacy_buff()
    if not explicit_account and (legacy.get("cookies") or "").strip():
        return set_buff_session(
            cookies=legacy.get("cookies") or "",
            account_id=account_id,
            mirror_legacy=False,
        )
    return current


def set_buff_session(
    cookies: str,
    *,
    account_id: Optional[str] = None,
    mirror_legacy: bool = True,
) -> dict:
    saved = set_account_session(
        account_id,
        PROVIDER_BUFF,
        cookies=cookies,
        status="ok" if cookies else "",
    )
    if mirror_legacy:
        update_buff_credentials(cookies)
    return saved
