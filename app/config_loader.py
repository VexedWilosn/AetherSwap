import threading
import time as _time
from app.config_schema import DEFAULTS, merge, validate_and_fill
from config import (
    get_buff,
    get_steam,
    load_app_config,
    save_app_config,
    update_buff_credentials,
    update_steam_credentials,
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
def get_steam_credentials() -> dict:
    return get_steam()
def get_buff_credentials() -> dict:
    return get_buff()
def update_steam_creds(cookies: str, session_id: str, steam_id: str = None) -> None:
    update_steam_credentials(cookies, session_id, steam_id)
def update_buff_creds(cookies: str) -> None:
    update_buff_credentials(cookies)
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
