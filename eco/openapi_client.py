from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = BASE_DIR / "config" / "credentials.json"
ECO_OPENAPI_BASE_URL = "https://openapi.ecosteam.cn"


@dataclass(frozen=True)
class EcoOpenAPIConfig:
    partner_id: str = ""
    private_key: str = ""
    service_provider_id: str = ""
    open_id: str = ""

    @property
    def is_service_provider_mode(self) -> bool:
        return bool(self.service_provider_id and self.open_id)

    @property
    def is_valid(self) -> bool:
        return bool((self.partner_id or self.is_service_provider_mode) and self.private_key)


class EcoOpenAPIError(RuntimeError):
    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None):
        super().__init__(f"ECO OpenAPI error {code}: {message}")
        self.code = str(code or "")
        self.message = str(message or "")
        self.payload = payload or {}


class RSAPrvCrypt:
    """ECO OpenAPI SHA256withRSA signer."""

    def __init__(self, private_key: str):
        self.private_key_pem = self._normalize_private_key(private_key)

    @staticmethod
    def _normalize_private_key(private_key: str) -> str:
        key = (private_key or "").strip()
        if not key:
            return ""
        if "BEGIN" in key:
            return key
        raw = key.replace("\r", "").replace("\n", "").replace(" ", "")
        lines = [raw[i:i + 64] for i in range(0, len(raw), 64)]
        return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----"

    @staticmethod
    def sign_data(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in sorted(payload.keys()):
            if key == "Sign" or key.lower() == "sign":
                continue
            value = payload.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, (list, dict)):
                value_str = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                value_str = str(value)
            parts.append(f"{key}={value_str}")
        return "&".join(parts)

    def sign(self, payload: dict[str, Any]) -> str:
        if not self.private_key_pem:
            raise ValueError("ECO RsaPrivateKey is empty")
        try:
            from Crypto.Hash import SHA256
            from Crypto.PublicKey import RSA
            from Crypto.Signature import pkcs1_15
        except Exception as exc:
            raise RuntimeError(f"pycryptodome is required for ECO RSA signing: {exc}") from exc

        key = RSA.import_key(self.private_key_pem.encode("utf-8"))
        digest = SHA256.new(self.sign_data(payload).encode("utf-8"))
        signature = pkcs1_15.new(key).sign(digest)
        return base64.b64encode(signature).decode("utf-8")


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.error("Failed to read %s: %s", path.name, exc)
        return {}


def load_eco_openapi_config(credentials: dict[str, Any] | None = None) -> Optional[EcoOpenAPIConfig]:
    root = credentials if isinstance(credentials, dict) else _load_json_file(CREDENTIALS_PATH)
    if any(key in root for key in ("PartnerId", "partnerId", "RsaPrivateKey", "rsaPrivateKey", "rsa_private_key")):
        eco_cfg = root
    else:
        eco_cfg = root.get("eco_openapi") if isinstance(root.get("eco_openapi"), dict) else root.get("eco")
    if not isinstance(eco_cfg, dict):
        eco_cfg = {}

    cfg = EcoOpenAPIConfig(
        partner_id=str(
            eco_cfg.get("PartnerId")
            or eco_cfg.get("partnerId")
            or eco_cfg.get("partner_id")
            or eco_cfg.get("partnerid")
            or ""
        ).strip(),
        private_key=str(
            eco_cfg.get("RsaPrivateKey")
            or eco_cfg.get("rsaPrivateKey")
            or eco_cfg.get("rsa_private_key")
            or eco_cfg.get("private_key")
            or ""
        ).strip(),
        service_provider_id=str(
            eco_cfg.get("ServiceProviderId")
            or eco_cfg.get("serviceProviderId")
            or eco_cfg.get("service_provider_id")
            or ""
        ).strip(),
        open_id=str(eco_cfg.get("OpenID") or eco_cfg.get("openId") or eco_cfg.get("open_id") or "").strip(),
    )
    if not cfg.is_valid:
        logger.error("[ECO] Missing eco_openapi PartnerId/ServiceProviderId+OpenID or RsaPrivateKey")
        return None
    return cfg


def normalize_eco_response_payload(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    status_data = data.get("StatusData") or data.get("statusData")
    if isinstance(status_data, dict):
        return status_data
    return data


class EcoOpenAPIClient:
    def __init__(
        self,
        config: EcoOpenAPIConfig | None = None,
        *,
        credentials: dict[str, Any] | None = None,
        base_url: str = ECO_OPENAPI_BASE_URL,
        timeout: int = 15,
    ):
        cfg = config or load_eco_openapi_config(credentials)
        if cfg is None:
            raise ValueError("ECO OpenAPI credentials are missing")
        self.config = cfg
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.signer = RSAPrvCrypt(cfg.private_key)
        self.session = requests.Session()
        self.session.headers.update(self.default_headers())

    @staticmethod
    def default_headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": "AetherSwap/1.0 (+https://openapi.ecosteam.cn)",
        }

    def signed_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out = dict(payload or {})
        out.setdefault("Timestamp", str(int(time.time())))
        if self.config.is_service_provider_mode:
            out.setdefault("ServiceProviderId", self.config.service_provider_id)
            out.setdefault("OpenID", self.config.open_id)
        else:
            out.setdefault("PartnerId", self.config.partner_id)
        out["Sign"] = self.signer.sign(out)
        return out

    @staticmethod
    def normalize_result(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"ResultCode": "-1", "ResultMsg": f"Invalid ECO response type: {type(data).__name__}", "ResultData": None}
        return normalize_eco_response_payload(data)

    @staticmethod
    def is_success(data: dict[str, Any]) -> bool:
        code = str(data.get("ResultCode", data.get("StatusCode", data.get("code", data.get("Code", ""))))).strip()
        return code in {"0", "200", "OK", "ok", "success"}

    @staticmethod
    def result_message(data: dict[str, Any]) -> str:
        return str(data.get("ResultMsg") or data.get("StatusMsg") or data.get("msg") or data.get("message") or "")

    def post(self, path: str, payload: dict[str, Any], *, timeout: int | None = None, raise_on_error: bool = False) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        signed = self.signed_payload(payload)
        resp = self.session.post(url, json=signed, timeout=timeout or self.timeout)
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {"ResultCode": str(resp.status_code), "ResultMsg": resp.text[:500], "ResultData": None}
        data = self.normalize_result(data)
        if resp.status_code != 200 and not data.get("ResultCode"):
            data["ResultCode"] = str(resp.status_code)
            data["ResultMsg"] = data.get("ResultMsg") or resp.text[:500]
        if raise_on_error and not self.is_success(data):
            raise EcoOpenAPIError(str(data.get("ResultCode") or ""), self.result_message(data), data)
        return data

    async def async_post(self, session: Any, path: str, payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        signed = self.signed_payload(payload)
        resp = await session.post(url, headers=self.default_headers(), json=signed, timeout=timeout or self.timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"ResultCode": str(getattr(resp, "status_code", "")), "ResultMsg": getattr(resp, "text", "")[:500], "ResultData": None}
        data = self.normalize_result(data)
        if getattr(resp, "status_code", 200) != 200 and not data.get("ResultCode"):
            data["ResultCode"] = str(getattr(resp, "status_code", ""))
        return data


__all__ = [
    "EcoOpenAPIClient",
    "EcoOpenAPIConfig",
    "EcoOpenAPIError",
    "ECO_OPENAPI_BASE_URL",
    "RSAPrvCrypt",
    "load_eco_openapi_config",
    "normalize_eco_response_payload",
]
