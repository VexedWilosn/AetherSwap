import base64
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

_DPAPI_PREFIX = "dpapi:v1:"
_FERNET_PREFIX = "fernet:v1:"
_SECRET_KEY_ENV = "AETHERSWAP_SECRET_KEY"
_SECRET_KEY_FILE_ENV = "AETHERSWAP_SECRET_KEY_FILE"
_DEFAULT_SECRET_KEY_FILE = Path(__file__).resolve().parent.parent / "config" / "secret.key"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_protected(value: str) -> bool:
    return isinstance(value, str) and (value.startswith(_DPAPI_PREFIX) or value.startswith(_FERNET_PREFIX))


def _dpapi_available() -> bool:
    return sys.platform.startswith("win")


def _load_fernet():
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None, None
    key = (os.environ.get(_SECRET_KEY_ENV) or "").strip()
    if key:
        return Fernet, key.encode("utf-8")
    key_file = Path(os.environ.get(_SECRET_KEY_FILE_ENV) or _DEFAULT_SECRET_KEY_FILE)
    try:
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return Fernet, key.encode("utf-8")
        key_file.parent.mkdir(parents=True, exist_ok=True)
        generated = Fernet.generate_key()
        key_file.write_text(generated.decode("ascii"), encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return Fernet, generated
    except Exception:
        return None, None


def protect_secret(value: str) -> str:
    if not isinstance(value, str) or not value or is_protected(value):
        return value or ""
    Fernet, fernet_key = _load_fernet()
    if Fernet and fernet_key:
        try:
            return _FERNET_PREFIX + Fernet(fernet_key).encrypt(value.encode("utf-8")).decode("ascii")
        except Exception:
            pass
    if not _dpapi_available():
        return value
    raw = value.encode("utf-8")
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        return value
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return _DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def unprotect_secret(value: str) -> str:
    if not is_protected(value):
        return value or ""
    if value.startswith(_FERNET_PREFIX):
        Fernet, fernet_key = _load_fernet()
        if not Fernet or not fernet_key:
            return ""
        try:
            token = value[len(_FERNET_PREFIX):].encode("ascii")
            return Fernet(fernet_key).decrypt(token).decode("utf-8")
        except Exception:
            return ""
    if not _dpapi_available():
        return ""
    payload = value[len(_DPAPI_PREFIX):]
    try:
        encrypted = base64.b64decode(payload)
    except Exception:
        return ""
    in_blob = _DATA_BLOB(len(encrypted), ctypes.cast(ctypes.create_string_buffer(encrypted), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        return ""
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return raw.decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
