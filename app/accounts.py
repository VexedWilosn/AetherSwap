import copy
import json
import uuid
from pathlib import Path
from typing import Any, List, Optional
_ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "config" / "accounts.json"
_cache: Optional[dict] = None
def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if _ACCOUNTS_FILE.exists():
        try:
            with open(_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {"accounts": [], "current_id": None}
    else:
        _cache = {"accounts": [], "current_id": None}
    return _cache
def _save(data: dict) -> None:
    global _cache
    _ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = data
def list_accounts() -> List[dict]:
    data = _load()
    return data.get("accounts", [])
def get_current_id() -> Optional[str]:
    return _load().get("current_id")
def get_current_account() -> Optional[dict]:
    accs = list_accounts()
    cid = get_current_id()
    if not cid:
        return accs[0] if accs else None
    return next((a for a in accs if a.get("id") == cid), accs[0] if accs else None)
def get_account(account_id: str) -> Optional[dict]:
    return next((a for a in list_accounts() if a.get("id") == account_id), None)
def add_account(username: str = "", password: str = "", steam_id: str = "", display_name: str = "", avatar_url: str = "", account_note: str = "") -> dict:
    data = _load()
    accs = data.get("accounts", [])
    aid = str(uuid.uuid4())[:8]
    acc = {
        "id": aid,
        "username": (username or "").strip(),
        "password": (password or "").strip(),
        "steam_id": (steam_id or "").strip(),
        "display_name": (display_name or "").strip(),
        "account_note": (account_note or "").strip(),
        "avatar_url": (avatar_url or "").strip(),
        "steam_guard": {
            "shared_secret": "",
            "identity_secret": "",
            "device_id": "",
        },
        "trade_config": {},
    }
    accs.append(acc)
    if not data.get("current_id"):
        data["current_id"] = aid
    data["accounts"] = accs
    _save(data)
    return acc
def update_account(account_id: str, **kwargs: Any) -> Optional[dict]:
    data = _load()
    accs = data.get("accounts", [])
    allowed = (
        "username", "password", "steam_id", "display_name", "account_note", "avatar_url",
        "currency_code", "region_code", "steam_guard", "trade_config",
        "wallet_balance", "balance", "balance_display", "wallet_currency_id",
        "wallet_currency_symbol", "balance_synced_at",
    )
    for a in accs:
        if a.get("id") == account_id:
            for k, v in kwargs.items():
                if k in allowed:
                    if k == "steam_guard":
                        a[k] = _normalize_steam_guard(v)
                    elif k == "trade_config":
                        a[k] = dict(v or {}) if isinstance(v, dict) else {}
                    else:
                        a[k] = (v or "").strip() if isinstance(v, str) else v
            _save(data)
            return a
    return None
def delete_account(account_id: str) -> bool:
    data = _load()
    accs = [a for a in data.get("accounts", []) if a.get("id") != account_id]
    if len(accs) == len(data.get("accounts", [])):
        return False
    data["accounts"] = accs
    if data.get("current_id") == account_id:
        data["current_id"] = accs[0]["id"] if accs else None
    _save(data)
    return True
def set_current(account_id: str) -> bool:
    data = _load()
    if not any(a.get("id") == account_id for a in data.get("accounts", [])):
        return False
    data["current_id"] = account_id
    _save(data)
    return True
def replace_all(data: dict) -> None:
    payload = {
        "accounts": list(data.get("accounts", [])),
        "current_id": data.get("current_id"),
    }
    _save(payload)

def _normalize_steam_guard(value: Any) -> dict:
    src = value if isinstance(value, dict) else {}
    return {
        "shared_secret": (src.get("shared_secret") or "").strip(),
        "identity_secret": (src.get("identity_secret") or "").strip(),
        "device_id": (src.get("device_id") or "").strip(),
    }

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
