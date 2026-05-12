import json
from pathlib import Path
from typing import Any, Optional
_CONFIG_DIR = Path(__file__).resolve().parent
_CREDENTIALS_FILE = _CONFIG_DIR / "credentials.json"
_cache: dict = {}
def _load() -> dict:
    global _cache
    if _cache:
        return _cache
    if _CREDENTIALS_FILE.exists():
        try:
            with open(_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    else:
        _cache = {}
    return _cache
def get(section: str, key: str = None, default: Any = None) -> Any:
    data = _load()
    val = data.get(section, default if key is None else {})
    if key is not None:
        val = val.get(key, default) if isinstance(val, dict) else default
    return val
def get_steam() -> dict:
    return _load().get("steam", {})
def get_buff() -> dict:
    return _load().get("buff", {})
def get_all_credentials() -> dict:
    return dict(_load())
def save_credentials(data: dict) -> None:
    global _cache
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = {}
_STEAM_COOKIE_NAMES = ("sessionid", "steamCountry", "steamLoginSecure")
def _filter_steam_cookies(cookie_str: str) -> str:
    seen = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, val = part.partition("=")
            n = name.strip()
            if n in _STEAM_COOKIE_NAMES:
                seen[n] = val.strip()
    return "; ".join(f"{k}={seen[k]}" for k in _STEAM_COOKIE_NAMES if k in seen)
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
def update_steam_credentials(cookies: str, session_id: str, steam_id: str = None, trade_link: str = None) -> None:
    global _cache
    data = _load().copy()
    steam = dict(data.get("steam", {}))
    steam["cookies"] = _filter_steam_cookies(cookies)
    steam["session_id"] = session_id
    sid = steam_id or _steam_id_from_cookies(cookies)
    if sid:
        steam["steam_id"] = sid
    if trade_link:
        steam["trade_link"] = str(trade_link).strip()
    data["steam"] = steam
    with open(_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = {}
def update_buff_credentials(cookies: str) -> None:
    global _cache
    data = _load().copy()
    buff = dict(data.get("buff", {}))
    buff["cookies"] = cookies
    data["buff"] = buff
    with open(_CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = {}
_APP_CONFIG_FILE = _CONFIG_DIR / "app_config.json"
def get_app_config_path() -> Path:
    return _APP_CONFIG_FILE
def load_app_config() -> dict:
    if _APP_CONFIG_FILE.exists():
        try:
            with open(_APP_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
def save_app_config(data: dict) -> None:
    with open(_APP_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
