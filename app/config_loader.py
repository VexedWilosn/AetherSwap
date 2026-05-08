import threading
import time as _time
from app.config_schema import DEFAULTS, merge, validate_and_fill
from config import (
    load_app_config,
    save_app_config,
)
_config_cache: dict = {}
_config_cache_ts: float = 0.0
_CONFIG_CACHE_TTL = 5.0  
_config_cache_lock = threading.Lock()
def _invalidate_config_cache() -> None:
    global _config_cache, _config_cache_ts
    with _config_cache_lock:
        _config_cache = {}
        _config_cache_ts = 0.0
def get_steam_credentials(account_id: str = "") -> dict:
    from app.account_sessions import get_steam_session
    return get_steam_session(account_id or None)
def get_buff_credentials(account_id: str = "") -> dict:
    from app.account_sessions import get_buff_session
    return get_buff_session(account_id or None)
def update_steam_creds(cookies: str, session_id: str, account_id: str = "", steam_id: str = None) -> None:
    from app.account_sessions import set_steam_session
    set_steam_session(cookies, session_id, account_id=account_id or None, steam_id=steam_id)
def update_buff_creds(cookies: str, account_id: str = "") -> None:
    from app.account_sessions import set_buff_session
    set_buff_session(cookies, account_id=account_id or None)
def load_app_config_validated() -> dict:
    global _config_cache, _config_cache_ts
    now = _time.monotonic()
    with _config_cache_lock:
        if _config_cache and (now - _config_cache_ts) < _CONFIG_CACHE_TTL:
            return _config_cache
        raw = load_app_config()
        result = validate_and_fill(merge(DEFAULTS, raw))
        _config_cache = result
        _config_cache_ts = now
        return result
def save_app_config_validated(data: dict) -> None:
    filled = validate_and_fill(merge(DEFAULTS, data))
    save_app_config(filled)
    _invalidate_config_cache()  
