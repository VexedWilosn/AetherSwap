import time
from typing import Optional

from sqlmodel import select

from app import database
from app.accounts import get_current_account
from app.database import AccountSession, get_session
from app.secret_box import protect_secret, unprotect_secret
from config import get_buff as get_legacy_buff
from config import get_steam as get_legacy_steam

PROVIDER_STEAM = "steam"
PROVIDER_BUFF = "buff"
_SUCCESS_STATUSES = {"", "ok", "valid"}


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
        "cookies": unprotect_secret(row.cookies or ""),
        "status": row.status or "",
    }
    if row.session_id is not None:
        out["session_id"] = unprotect_secret(row.session_id)
    if row.steam_id is not None:
        out["steam_id"] = row.steam_id
    if row.error is not None:
        out["error"] = row.error
    out["failure_count"] = int(row.failure_count or 0)
    if row.next_retry_at is not None:
        out["next_retry_at"] = row.next_retry_at
    if row.last_validated_at is not None:
        out["last_validated_at"] = row.last_validated_at
    return out


def public_session_summary(account_id: Optional[str]) -> dict:
    """Return non-sensitive session status for UI/API payloads."""
    out = {}
    for provider in (PROVIDER_STEAM, PROVIDER_BUFF):
        session = get_account_session(account_id, provider)
        cookies = (session.get("cookies") or "").strip()
        out[provider] = {
            "configured": bool(cookies),
            "status": session.get("status") or ("ok" if cookies else ""),
            "error": session.get("error") or "",
            "failure_count": int(session.get("failure_count") or 0),
            "next_retry_at": session.get("next_retry_at"),
            "last_validated_at": session.get("last_validated_at"),
            "steam_id": session.get("steam_id") if provider == PROVIDER_STEAM else None,
        }
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
        row.cookies = protect_secret(cookies or "")
        row.session_id = protect_secret(session_id or "") if session_id is not None else None
        row.steam_id = steam_id
        row.status = status or ""
        row.error = error
        row.failure_count = 0
        row.next_retry_at = None
        row.last_validated_at = last_validated_at if last_validated_at is not None else (now if row.status else None)
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def update_account_session_status(
    account_id: Optional[str],
    provider: str,
    *,
    status: str,
    error: Optional[str] = None,
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
        normalized = (status or "").strip().lower()
        row.status = normalized
        row.error = error
        if normalized in _SUCCESS_STATUSES:
            row.failure_count = 0
            row.next_retry_at = None
        else:
            row.failure_count = int(row.failure_count or 0) + 1
            delay_seconds = min(3600, 60 * (2 ** max(0, row.failure_count - 1)))
            row.next_retry_at = now + delay_seconds
        row.last_validated_at = now
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _session_to_dict(row)


def should_retry_session(account_id: Optional[str], provider: str, *, now: Optional[float] = None) -> tuple[bool, str]:
    session = get_account_session(account_id, provider)
    next_retry_at = session.get("next_retry_at")
    if not next_retry_at:
        return True, ""
    now = time.time() if now is None else now
    if float(next_retry_at) <= now:
        return True, ""
    wait_seconds = int(float(next_retry_at) - now)
    return False, f"冷却中，约 {max(1, wait_seconds)} 秒后重试"


def clear_account_session(account_id: Optional[str], provider: str) -> bool:
    _ensure_ready()
    account_id = _resolve_account_id(account_id)
    provider = (provider or "").strip().lower()
    if not account_id or provider not in {PROVIDER_STEAM, PROVIDER_BUFF}:
        return False
    with get_session() as session:
        row = session.get(AccountSession, _session_key(account_id, provider))
        if row is None:
            return True
        session.delete(row)
        session.commit()
    return True


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
                cookies=protect_secret(item.get("cookies") or ""),
                session_id=protect_secret(item.get("session_id") or "") if item.get("session_id") is not None else None,
                steam_id=item.get("steam_id"),
                status=item.get("status") or "",
                error=item.get("error"),
                failure_count=int(item.get("failure_count") or 0),
                next_retry_at=item.get("next_retry_at"),
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
    mirror_legacy: bool = False,
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
        from config import update_steam_credentials
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
    mirror_legacy: bool = False,
) -> dict:
    saved = set_account_session(
        account_id,
        PROVIDER_BUFF,
        cookies=cookies,
        status="ok" if cookies else "",
    )
    if mirror_legacy:
        from config import update_buff_credentials
        update_buff_credentials(cookies)
    return saved
