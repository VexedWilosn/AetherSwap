from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .capabilities import CAPABILITY_REGISTRY, normalize_platform
from .platform_adapters import build_platform_adapters


@dataclass(frozen=True)
class PlatformSmokeResult:
    platform: str
    ok: bool
    safe_mode: bool
    live_preflight: bool
    checked_capabilities: list[str]
    ready_capabilities: list[str]
    missing_capabilities: list[str]
    reason: str = ""
    message: str = ""


class PlatformAutomationSmokeService:
    def __init__(self, credentials: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        self.credentials = credentials or {}
        self.config = config or {}

    def run(
        self,
        *,
        platforms: list[str] | tuple[str, ...] | None = None,
        capabilities: list[str] | tuple[str, ...] | None = None,
        safe_mode: bool = True,
        live_preflight: bool = False,
    ) -> list[dict[str, Any]]:
        requested_platforms = [normalize_platform(p) for p in (platforms or CAPABILITY_REGISTRY.keys()) if str(p or "").strip()]
        requested_capabilities = [str(c or "").strip() for c in (capabilities or []) if str(c or "").strip()]
        adapters = build_platform_adapters(self.credentials, self.config, platforms=tuple(requested_platforms))
        results = []
        for platform in requested_platforms:
            info = CAPABILITY_REGISTRY.get(platform)
            if info is None:
                results.append(asdict(PlatformSmokeResult(platform, False, safe_mode, live_preflight, [], [], [], "unknown_platform")))
                continue
            capability_names = requested_capabilities or list(info.capabilities.keys())
            checked = [name for name in capability_names if name in info.capabilities]
            ready = [name for name in checked if info.supports(name)]
            missing = [name for name in capability_names if name not in info.capabilities or not info.supports(name)]
            reason = ""
            message = ""
            ok = bool(ready) and not missing
            if live_preflight and not safe_mode:
                adapter = adapters.get(platform)
                try:
                    _, _, preflight_error = adapter._client_or_result("smoke") if adapter else (None, None, None)
                except Exception as exc:
                    preflight_error = type("PreflightError", (), {"category": "preflight_exception", "message": str(exc)})()
                if preflight_error is not None:
                    ok = False
                    reason = str(getattr(preflight_error, "category", "") or "preflight_failed")
                    message = str(getattr(preflight_error, "message", "") or reason)
            elif live_preflight and safe_mode:
                reason = "safe_mode_preflight_skipped"
                message = "SAFE_MODE smoke does not instantiate live platform clients"
            results.append(
                asdict(
                    PlatformSmokeResult(
                        platform=platform,
                        ok=ok,
                        safe_mode=safe_mode,
                        live_preflight=live_preflight and not safe_mode,
                        checked_capabilities=checked,
                        ready_capabilities=ready,
                        missing_capabilities=missing,
                        reason=reason,
                        message=message,
                    )
                )
            )
        return results
