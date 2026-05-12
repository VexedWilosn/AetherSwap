from __future__ import annotations

import json
import os
import time
import base64
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATE_PATH = BASE_DIR / "config" / "platform_session_state.json"
DEFAULT_COOLDOWN_SECONDS = 180
_STATE_WRITE_LOCK = threading.RLock()
_WINDOWS_REPLACE_RETRY_ERRORS = {5, 32}


AUTH_ERROR_TOKENS = (
    "login",
    "auth",
    "unauthorized",
    "token",
    "csrf",
    "expired",
    "risk",
    "\u767b\u5f55",
    "\u767b\u9678",
    "\u9274\u6743",
    "\u8ba4\u8bc1",
    "\u8fc7\u671f",
    "\u98ce\u63a7",
    "\u9a8c\u8bc1",
)


BUFF_PAY_METHOD_ALIASES = {
    "wallet": 1,
    "balance": 1,
    "pending": 1,
    "wallet_balance": 1,
    "account_balance": 1,
    "platform_wallet": 1,
    "netease_pay": 1,
    "nepay": 1,
    "wechat": 6,
    "wechatpay": 6,
    "alipay": 51,
}


def resolve_buff_pay_method(raw_pay_method: Any, default: int = 51) -> int:
    if isinstance(raw_pay_method, str):
        token = raw_pay_method.strip().lower()
        if not token:
            return int(default)
        pay_method = BUFF_PAY_METHOD_ALIASES.get(token)
        if pay_method is not None:
            return pay_method
        try:
            return int(token)
        except ValueError:
            return int(default)
    try:
        return int(raw_pay_method or default)
    except (TypeError, ValueError):
        return int(default)


def _now() -> float:
    return time.time()


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "***"
    return value[:6] + "***" + value[-4:]


def _parse_cookie_str(cookie_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(cookie_str or "").split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key:
            out[key.strip()] = val.strip()
    return out


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1].replace("-", "+").replace("_", "/")
        payload += "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _credential_blob(platform_data: dict[str, Any], env_key: str = "") -> str:
    if env_key:
        val = os.getenv(env_key, "").strip()
        if val:
            return val
    for key in ("cookies", "cookie", "token", "auth", "session", "value", "app_key", "api_key"):
        val = str(platform_data.get(key, "")).strip()
        if val:
            return val
    return ""


def _platform_data(credentials: dict[str, Any], platform: str) -> dict[str, Any]:
    data = (credentials or {}).get(platform) or {}
    return data if isinstance(data, dict) else {}


@dataclass
class PlatformState:
    platform: str
    auth_type: str = ""
    health_status: str = "unknown"
    last_validated_at: float = 0.0
    last_success_at: float = 0.0
    last_error_code: str = ""
    last_error_message: str = ""
    cooldown_until: float = 0.0
    proxy_policy: str = ""
    proxy_tag: str = ""
    credential_version: str = ""
    headers_snapshot: dict[str, str] = field(default_factory=dict)
    cookies_snapshot: dict[str, str] = field(default_factory=dict)

    @property
    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - _now()))

    @property
    def is_cooldown_open(self) -> bool:
        return self.cooldown_remaining > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "auth_type": self.auth_type,
            "health_status": self.health_status,
            "last_validated_at": self.last_validated_at,
            "last_success_at": self.last_success_at,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "cooldown_until": self.cooldown_until,
            "proxy_policy": self.proxy_policy,
            "proxy_tag": self.proxy_tag,
            "credential_version": self.credential_version,
            "headers_snapshot": dict(self.headers_snapshot),
            "cookies_snapshot": dict(self.cookies_snapshot),
        }

    @classmethod
    def from_dict(cls, platform: str, data: dict[str, Any]) -> "PlatformState":
        state = cls(platform=platform)
        for key in state.to_dict().keys():
            if key == "platform":
                continue
            if key in data:
                setattr(state, key, data[key])
        return state


class PlatformSessionStateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _write(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with _STATE_WRITE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
            )
            tmp.write_text(payload, encoding="utf-8")
            last_error: OSError | None = None
            try:
                for attempt in range(6):
                    try:
                        os.replace(tmp, self.path)
                        return
                    except OSError as exc:
                        last_error = exc
                        winerror = getattr(exc, "winerror", None)
                        if winerror not in _WINDOWS_REPLACE_RETRY_ERRORS and not isinstance(exc, PermissionError):
                            raise
                        time.sleep(0.02 * (attempt + 1))
                if last_error is not None:
                    raise last_error
                os.replace(tmp, self.path)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, platform: str) -> PlatformState:
        platform = platform.lower().strip()
        data = self._read().get(platform) or {}
        return PlatformState.from_dict(platform, data if isinstance(data, dict) else {})

    def save(self, state: PlatformState) -> None:
        with _STATE_WRITE_LOCK:
            data = self._read()
            data[state.platform] = state.to_dict()
            self._write(data)

    def mark_valid(self, platform: str, auth_type: str, *, proxy_policy: str = "", headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> PlatformState:
        state = self.get(platform)
        state.auth_type = auth_type
        state.health_status = "valid"
        state.last_validated_at = _now()
        state.last_error_code = ""
        state.last_error_message = ""
        state.proxy_policy = proxy_policy or state.proxy_policy
        state.headers_snapshot = {k: _mask(v) for k, v in (headers or {}).items() if v}
        state.cookies_snapshot = {k: _mask(v) for k, v in (cookies or {}).items() if v}
        self.save(state)
        return state

    def mark_success(self, platform: str) -> PlatformState:
        state = self.get(platform)
        state.health_status = "valid"
        state.last_success_at = _now()
        state.last_error_code = ""
        state.last_error_message = ""
        self.save(state)
        return state

    def mark_error(self, platform: str, code: str, message: str, *, cooldown_seconds: int = 0, status: str = "error") -> PlatformState:
        state = self.get(platform)
        state.health_status = status
        state.last_error_code = str(code or "")
        state.last_error_message = str(message or "")[:500]
        if cooldown_seconds > 0:
            state.cooldown_until = _now() + cooldown_seconds
        self.save(state)
        return state


@dataclass
class PreflightResult:
    ok: bool
    reason: str = ""
    message: str = ""
    status: str = "valid"
    cooldown_remaining: int = 0

    def as_result(self) -> dict[str, Any]:
        return {
            "success": False,
            "msg": self.message or self.reason,
            "reason": self.reason,
            "status": self.status,
            "cooldown_remaining": self.cooldown_remaining,
        }


class PlatformSessionProvider:
    platform = ""
    auth_type = ""
    proxy_policy = ""

    def __init__(self, credentials: dict[str, Any], config: dict[str, Any] | None = None, store: PlatformSessionStateStore | None = None):
        self.credentials = credentials or {}
        self.config = config or {}
        self.store = store or PlatformSessionStateStore()
        self.data = _platform_data(self.credentials, self.platform)

    def get_state(self) -> PlatformState:
        return self.store.get(self.platform)

    def has_credentials(self) -> bool:
        return bool(_credential_blob(self.data))

    def preflight(self, purpose: str = "buy") -> PreflightResult:
        state = self.get_state()
        if state.is_cooldown_open:
            return PreflightResult(
                ok=False,
                reason="risk_cooldown",
                message=f"{self.platform} is cooling down after auth/risk error",
                status="cooldown",
                cooldown_remaining=state.cooldown_remaining,
            )
        if not self.has_credentials():
            self.store.mark_error(self.platform, "missing_credentials", "credentials missing", status="missing")
            return PreflightResult(ok=False, reason="missing_credentials", message=f"{self.platform} credentials missing", status="missing")
        return PreflightResult(ok=True)

    def classify_result(self, result: dict[str, Any]) -> None:
        if result.get("success"):
            self.store.mark_success(self.platform)
            return
        msg = str(result.get("msg") or result.get("message") or result.get("error") or "")
        code = str(result.get("code") or result.get("Code") or result.get("status_code") or "")
        text = f"{code} {msg}".lower()
        if result.get("auth_required") or any(token in text for token in AUTH_ERROR_TOKENS):
            self.store.mark_error(
                self.platform,
                code or "auth_or_risk_error",
                msg or "auth/risk error",
                cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
                status="risk_blocked",
            )
        else:
            self.store.mark_error(self.platform, code or "business_error", msg or "business error", status="error")

    def get_client(self) -> Any:
        raise NotImplementedError


class BuffSessionProvider(PlatformSessionProvider):
    platform = "buff"
    auth_type = "cookie_csrf"
    proxy_policy = "configurable"

    def has_credentials(self) -> bool:
        return bool(_credential_blob(self.data, "BUFF_COOKIE") or self.config.get("BUFF_COOKIE"))

    def cookie_str(self) -> str:
        return (
            _credential_blob(self.data, "BUFF_COOKIE")
            or str(self.config.get("BUFF_COOKIE") or "").strip()
        )

    def get_client(self) -> Any:
        from buff import BuffBuyer, PAY_METHOD_ALIPAY

        cookie_str = self.cookie_str()
        cookies = _parse_cookie_str(cookie_str)
        cookies["locale"] = "zh-Hans"
        cookies["Locale-Supported"] = "zh-Hans"
        cookies["currency"] = "CNY"
        self.store.mark_valid(self.platform, self.auth_type, proxy_policy=self.proxy_policy, cookies=cookies)
        buff_cfg = self.config.get("buff") if isinstance(self.config.get("buff"), dict) else {}
        raw_pay_method = self.config.get("BUFF_PAY_METHOD", buff_cfg.get("pay_method", PAY_METHOD_ALIPAY))
        pay_method = resolve_buff_pay_method(raw_pay_method, PAY_METHOD_ALIPAY)
        return BuffBuyer(cookie_str=cookie_str, pay_method=pay_method)


class UuypAppSessionProvider(PlatformSessionProvider):
    platform = "uuyp"
    auth_type = "app_token"
    proxy_policy = "direct"

    @staticmethod
    def _merge_cookie_blob(merged: dict[str, str], raw_cookie: str) -> None:
        parsed = _parse_cookie_str(raw_cookie)
        for key, value in parsed.items():
            merged.setdefault(key, value)

    @staticmethod
    def _load_cached_headers() -> dict[str, str]:
        headers_path = BASE_DIR / "DataEngine" / "uuyp_headers.json"
        try:
            if headers_path.exists():
                data = json.loads(headers_path.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v is not None and str(v).strip()}
        except Exception:
            pass
        return {}

    @staticmethod
    def _augment_order_credentials(merged: dict[str, str]) -> None:
        token = (
            str(merged.get("uu_token") or "").strip()
            or str(merged.get("UU_TOKEN") or "").strip()
            or str(merged.get("token") or "").strip()
        )
        authorization = str(merged.get("Authorization") or merged.get("authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            token = token or authorization.split(" ", 1)[1].strip()
        elif authorization:
            token = token or authorization
            merged.setdefault("Authorization", f"Bearer {authorization}")
        if token:
            merged.setdefault("uu_token", token)
            merged.setdefault("Authorization", f"Bearer {token}")
            payload = _decode_jwt_payload(token)
            device_id = str(payload.get("deviceId") or payload.get("deviceid") or "").strip()
            if device_id:
                merged.setdefault("deviceId", device_id)
                merged.setdefault("DeviceId", device_id)
                merged.setdefault("Device-Id", device_id)
                # Steamauto uses one generated device token as DeviceId, DeviceToken and Sessionid.
                merged.setdefault("DeviceToken", device_id)
                merged.setdefault("deviceToken", device_id)
                merged.setdefault("Sessionid", device_id)

    def _merged_credentials(self) -> dict[str, str]:
        merged = {str(k): str(v) for k, v in self.data.items() if v is not None}
        for key in ("cookies", "cookie", "session", "value"):
            if merged.get(key):
                self._merge_cookie_blob(merged, merged[key])
        raw_cookie = os.getenv("UUYP_COOKIE", "").strip() or str(self.config.get("UUYP_COOKIE") or "").strip()
        if raw_cookie:
            merged.update(_parse_cookie_str(raw_cookie))
            merged.setdefault("cookies", raw_cookie)
        raw_token = os.getenv("UUYP_TOKEN", "").strip()
        if raw_token:
            merged.setdefault("uu_token", raw_token)
        self._augment_order_credentials(merged)
        for key, value in self._load_cached_headers().items():
            merged.setdefault(key, value)
        self._augment_order_credentials(merged)
        return merged

    def has_credentials(self) -> bool:
        merged = self._merged_credentials()
        blob = " ".join(str(v) for v in merged.values() if v)
        return any(token in blob for token in ("uu_token", "UU_TOKEN", "Authorization", "Bearer"))

    @staticmethod
    def _missing_order_credentials(merged: dict[str, Any]) -> list[str]:
        token = (
            str(merged.get("Authorization") or "").strip()
            or str(merged.get("authorization") or "").strip()
            or str(merged.get("uu_token") or "").strip()
            or str(merged.get("UU_TOKEN") or "").strip()
        )
        device_id = (
            str(merged.get("deviceId") or "").strip()
            or str(merged.get("deviceid") or "").strip()
            or str(merged.get("DeviceId") or "").strip()
            or str(merged.get("Device-Id") or "").strip()
        )
        session_id = (
            str(merged.get("Sessionid") or "").strip()
            or str(merged.get("sessionid") or "").strip()
            or str(merged.get("session_id") or "").strip()
        )
        uk = (
            str(merged.get("uk") or "").strip()
            or str(merged.get("deviceUk") or "").strip()
            or str(merged.get("deviceUK") or "").strip()
        )
        missing: list[str] = []
        if not token:
            missing.append("uu_token/Authorization")
        if not device_id:
            missing.append("deviceId")
        if not session_id:
            missing.append("Sessionid")
        if not uk:
            missing.append("uk/deviceUk")
        return missing

    def preflight(self, purpose: str = "buy") -> PreflightResult:
        try:
            from DataEngine.uuyp_public_monitor import is_uuyp_auth_circuit_open, uuyp_auth_circuit_remaining_seconds

            if is_uuyp_auth_circuit_open():
                return PreflightResult(
                    ok=False,
                    reason="risk_cooldown",
                    message="uuyp auth/business circuit is open",
                    status="cooldown",
                    cooldown_remaining=uuyp_auth_circuit_remaining_seconds(),
                )
        except Exception:
            pass
        base = super().preflight(purpose=purpose)
        if not base.ok:
            return base
        normalized_purpose = str(purpose or "").strip().lower()
        if normalized_purpose in {"direct_buy", "purchase_order", "buy", "order_status", "smoke"}:
            merged = self._merged_credentials()
            missing = self._missing_order_credentials(merged)
            if missing:
                msg = f"uuyp order credentials missing: {', '.join(missing)}"
                self.store.mark_error(
                    self.platform,
                    "missing_order_credentials",
                    msg,
                    status="missing",
                )
                return PreflightResult(
                    ok=False,
                    reason="missing_order_credentials",
                    message=msg,
                    status="missing",
                )
        return base

    def get_client(self) -> Any:
        from uuyp import UuypBuyer

        creds = self._merged_credentials()
        headers = {}
        for key in ("Authorization", "deviceId", "deviceid", "DeviceId", "Device-Id", "DeviceToken", "Sessionid", "uk", "deviceUk"):
            if creds.get(key):
                headers[key] = str(creds[key])
        self.store.mark_valid(self.platform, self.auth_type, proxy_policy=self.proxy_policy, headers=headers, cookies=creds)
        return UuypBuyer(cookie_str=creds)


class EcoSessionProvider(PlatformSessionProvider):
    platform = "eco"
    auth_type = "openapi_rsa"
    proxy_policy = "direct"

    def __init__(self, credentials: dict[str, Any], config: dict[str, Any] | None = None, store: PlatformSessionStateStore | None = None):
        super().__init__(credentials, config, store)
        openapi_data = (credentials or {}).get("eco_openapi") or {}
        if isinstance(openapi_data, dict):
            merged = dict(self.data)
            merged.update(openapi_data)
            self.data = merged

    def has_credentials(self) -> bool:
        partner_id = str(
            self.data.get("PartnerId")
            or self.data.get("partnerId")
            or self.data.get("partner_id")
            or ""
        ).strip()
        service_provider_id = str(self.data.get("ServiceProviderId") or self.data.get("serviceProviderId") or "").strip()
        open_id = str(self.data.get("OpenID") or self.data.get("openId") or "").strip()
        private_key = str(
            self.data.get("RsaPrivateKey")
            or self.data.get("rsaPrivateKey")
            or self.data.get("rsa_private_key")
            or self.data.get("private_key")
            or ""
        ).strip()
        return bool((partner_id or (service_provider_id and open_id)) and private_key)

    def _trade_link(self) -> str:
        steam_data = self.credentials.get("steam") if isinstance(self.credentials.get("steam"), dict) else {}
        for data in (self.data, steam_data):
            value = (
                data.get("trade_link")
                or data.get("TradeLink")
                or data.get("tradeLink")
                or ""
            )
            if str(value or "").strip():
                return str(value).strip()
        return ""

    def preflight(self, purpose: str = "buy") -> PreflightResult:
        base = super().preflight(purpose=purpose)
        if not base.ok:
            return base
        normalized_purpose = str(purpose or "").strip().lower()
        if normalized_purpose in {"direct_buy", "buy"} and not self._trade_link():
            msg = "eco trade_link is required for direct_buy"
            self.store.mark_error(
                self.platform,
                "missing_trade_link",
                msg,
                status="missing",
            )
            return PreflightResult(
                ok=False,
                reason="missing_trade_link",
                message=msg,
                status="missing",
            )
        return base

    def get_client(self) -> Any:
        from eco import EcoBuyer

        headers = {"PartnerId": self.data.get("PartnerId", ""), "ServiceProviderId": self.data.get("ServiceProviderId", "")}
        self.store.mark_valid(self.platform, self.auth_type, proxy_policy=self.proxy_policy, headers={k: v for k, v in headers.items() if v})
        return EcoBuyer(cookie_str=self.data)


class SteamSessionProvider(PlatformSessionProvider):
    platform = "steam"
    auth_type = "cookie_sessionid"
    proxy_policy = "direct"

    def has_credentials(self) -> bool:
        cookie_blob = _credential_blob(self.data, "STEAM_COOKIE")
        if not cookie_blob:
            cookie_blob = str(self.config.get("STEAM_COOKIE") or "").strip()
        cookies = _parse_cookie_str(cookie_blob)
        session_id = str(
            self.data.get("sessionid")
            or self.data.get("session_id")
            or self.config.get("STEAM_SESSION_ID")
            or cookies.get("sessionid")
            or ""
        ).strip()
        return bool(cookie_blob and session_id)

    def cookie_str(self) -> str:
        return (
            _credential_blob(self.data, "STEAM_COOKIE")
            or str(self.config.get("STEAM_COOKIE") or "").strip()
        )

    def session_id(self) -> str:
        cookies = _parse_cookie_str(self.cookie_str())
        return str(
            self.data.get("sessionid")
            or self.data.get("session_id")
            or self.config.get("STEAM_SESSION_ID")
            or cookies.get("sessionid")
            or ""
        ).strip()

    def get_client(self) -> Any:
        from app.services.steam_buyer import SteamBuyer

        cookie_str = self.cookie_str()
        session_id = self.session_id()
        currency = int(self.config.get("STEAM_CURRENCY", self.data.get("currency", 23)) or 23)
        self.store.mark_valid(
            self.platform,
            self.auth_type,
            proxy_policy=self.proxy_policy,
            cookies=_parse_cookie_str(cookie_str),
        )
        return SteamBuyer(cookie_str=cookie_str, session_id=session_id, currency=currency)


class C5OpenApiProvider(PlatformSessionProvider):
    platform = "c5game"
    auth_type = "api_key"
    proxy_policy = "direct"

    def app_key(self) -> str:
        return str(
            self.data.get("app_key")
            or self.data.get("api_key")
            or self.data.get("appKey")
            or self.data.get("AppKey")
            or self.data.get("app-key")
            or self.config.get("C5GAME_APP_KEY")
            or os.getenv("C5GAME_APP_KEY", "")
        ).strip()

    def has_credentials(self) -> bool:
        return bool(self.app_key())

    def get_client(self) -> Any:
        from c5game import C5GameClient

        app_key = self.app_key()
        self.store.mark_valid(self.platform, self.auth_type, proxy_policy=self.proxy_policy, headers={"app-key": app_key})
        return C5GameClient(
            app_key=app_key,
            timeout=int(self.config.get("C5GAME_TIMEOUT", 15) or 15),
            base_url=str(self.config.get("C5GAME_BASE_URL") or "http://openapi.c5game.com"),
        )


class PlatformClientFactory:
    def __init__(self, credentials: dict[str, Any] | None = None, config: dict[str, Any] | None = None, store: PlatformSessionStateStore | None = None):
        self.credentials = credentials or {}
        self.config = config or {}
        self.store = store or PlatformSessionStateStore()

    def provider(self, platform: str) -> PlatformSessionProvider:
        platform = (platform or "").lower().strip()
        providers: dict[str, type[PlatformSessionProvider]] = {
            "buff": BuffSessionProvider,
            "uuyp": UuypAppSessionProvider,
            "eco": EcoSessionProvider,
            "steam": SteamSessionProvider,
            "c5": C5OpenApiProvider,
            "c5game": C5OpenApiProvider,
        }
        cls = providers.get(platform)
        if cls is None:
            raise ValueError(f"unsupported platform: {platform}")
        return cls(self.credentials, self.config, self.store)

    def preflight(self, platform: str, purpose: str = "buy") -> PreflightResult:
        return self.provider(platform).preflight(purpose=purpose)

    def client(self, platform: str, purpose: str = "buy") -> tuple[Any | None, PreflightResult, PlatformSessionProvider]:
        provider = self.provider(platform)
        preflight = provider.preflight(purpose=purpose)
        if not preflight.ok:
            return None, preflight, provider
        return provider.get_client(), preflight, provider
