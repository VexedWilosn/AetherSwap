from __future__ import annotations

import json
import random
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_PATH = BASE_DIR / "config" / "session_capsules.json"


def _now_ts() -> float:
    return time.time()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@dataclass
class SessionCapsule:
    capsule_id: str
    platform: str
    status: str = "ready"
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_header: str = ""
    device_id: str = ""
    user_agent: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    local_storage: dict[str, Any] = field(default_factory=dict)
    session_storage: dict[str, Any] = field(default_factory=dict)
    proxy_binding: str = "direct"
    tls_profile: str = ""
    created_at: str = field(default_factory=_iso_now)
    last_used_at: str = ""
    last_ok_at: str = ""
    fail_count: int = 0
    consecutive_auth_failures: int = 0
    failure_streak_reason: str = ""
    failure_streak_count: int = 0
    cooldown_until: float = 0.0
    lease_until: float = 0.0
    retire_reason: str = ""
    last_failure_reason: str = ""
    maintenance_alerted_at: float = 0.0
    notes: str = ""

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    @property
    def is_cooled_down(self) -> bool:
        return self.cooldown_until <= _now_ts()

    @property
    def is_leased(self) -> bool:
        return self.lease_until > _now_ts()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def scrub_sensitive_state(self) -> None:
        self.cookies = {}
        self.cookie_header = ""
        self.headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"cookie", "authorization", "x-csrf-token", "x-device-id"}
        }
        self.local_storage = {}
        self.session_storage = {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionCapsule":
        payload = dict(data or {})
        payload.setdefault("capsule_id", f"{payload.get('platform', 'capsule')}-{uuid.uuid4().hex[:8]}")
        payload.setdefault("platform", "")
        allowed = {field.name for field in fields(cls)}
        payload = {key: value for key, value in payload.items() if key in allowed}
        return cls(**payload)


class SessionCapsulePool:
    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load_platform(self, platform: str) -> list[SessionCapsule]:
        raw = self._read().get(platform, [])
        if not isinstance(raw, list):
            return []
        return [SessionCapsule.from_dict(row) for row in raw if isinstance(row, dict)]

    def _save_platform(self, platform: str, capsules: list[SessionCapsule]) -> None:
        payload = self._read()
        payload[platform] = [capsule.to_dict() for capsule in capsules]
        self._write(payload)

    def list_capsules(self, platform: str, *, include_retired: bool = False) -> list[SessionCapsule]:
        platform = str(platform or "").strip().lower()
        capsules = self._load_platform(platform)
        if include_retired:
            return capsules
        return [capsule for capsule in capsules if capsule.status != "retired"]

    def upsert_capsule(self, capsule: SessionCapsule) -> SessionCapsule:
        platform = capsule.platform.strip().lower()
        capsule.platform = platform
        capsules = self._load_platform(platform)
        replaced = False
        for idx, existing in enumerate(capsules):
            if existing.capsule_id == capsule.capsule_id:
                capsules[idx] = capsule
                replaced = True
                break
        if not replaced:
            capsules.append(capsule)
        self._save_platform(platform, capsules)
        return capsule

    def register_capsule(
        self,
        *,
        platform: str,
        cookies: dict[str, str] | None = None,
        cookie_header: str = "",
        device_id: str = "",
        user_agent: str = "",
        headers: dict[str, str] | None = None,
        local_storage: dict[str, Any] | None = None,
        session_storage: dict[str, Any] | None = None,
        proxy_binding: str = "direct",
        tls_profile: str = "",
        notes: str = "",
        capsule_id: str | None = None,
    ) -> SessionCapsule:
        platform = str(platform or "").strip().lower()
        capsule = SessionCapsule(
            capsule_id=capsule_id or f"{platform}-{uuid.uuid4().hex[:8]}",
            platform=platform,
            cookies=dict(cookies or {}),
            cookie_header=str(cookie_header or ""),
            device_id=str(device_id or ""),
            user_agent=str(user_agent or ""),
            headers={str(k): str(v) for k, v in dict(headers or {}).items() if v is not None},
            local_storage=dict(local_storage or {}),
            session_storage=dict(session_storage or {}),
            proxy_binding=str(proxy_binding or "direct"),
            tls_profile=str(tls_profile or ""),
            notes=str(notes or ""),
        )
        return self.upsert_capsule(capsule)

    def lease_capsule(self, platform: str, *, lease_ttl_seconds: int = 45) -> SessionCapsule | None:
        platform = str(platform or "").strip().lower()
        capsules = self._load_platform(platform)
        ready = [
            capsule
            for capsule in capsules
            if capsule.is_ready and capsule.is_cooled_down and not capsule.is_leased
        ]
        if not ready:
            return None
        ready.sort(key=lambda capsule: (capsule.last_used_at or "", capsule.created_at))
        candidates = ready[: min(3, len(ready))]
        leased = random.choice(candidates)
        leased.last_used_at = _iso_now()
        leased.lease_until = _now_ts() + max(1, int(lease_ttl_seconds))
        self.upsert_capsule(leased)
        return leased

    def release_capsule(self, platform: str, capsule_id: str) -> SessionCapsule | None:
        capsule = self.get_capsule(platform, capsule_id)
        if capsule is None:
            return None
        capsule.lease_until = 0.0
        return self.upsert_capsule(capsule)

    def get_capsule(self, platform: str, capsule_id: str) -> SessionCapsule | None:
        for capsule in self._load_platform(str(platform or "").strip().lower()):
            if capsule.capsule_id == capsule_id:
                return capsule
        return None

    def mark_success(self, platform: str, capsule_id: str) -> SessionCapsule | None:
        capsule = self.get_capsule(platform, capsule_id)
        if capsule is None:
            return None
        capsule.status = "ready"
        capsule.fail_count = 0
        capsule.consecutive_auth_failures = 0
        capsule.failure_streak_reason = ""
        capsule.failure_streak_count = 0
        capsule.last_ok_at = _iso_now()
        capsule.last_failure_reason = ""
        capsule.cooldown_until = 0.0
        capsule.lease_until = 0.0
        capsule.maintenance_alerted_at = 0.0
        return self.upsert_capsule(capsule)

    def mark_failure(
        self,
        platform: str,
        capsule_id: str,
        *,
        reason: str,
        cooldown_seconds: int = 0,
        status: str = "ready",
        auth_failure: bool = False,
        retire_after_auth_failures: int = 3,
        auto_retire_reasons: set[str] | None = None,
        auto_retire_after: int = 0,
    ) -> SessionCapsule | None:
        capsule = self.get_capsule(platform, capsule_id)
        if capsule is None:
            return None
        normalized_reason = str(reason or "")
        capsule.fail_count += 1
        capsule.last_failure_reason = normalized_reason
        if capsule.failure_streak_reason == normalized_reason:
            capsule.failure_streak_count += 1
        else:
            capsule.failure_streak_reason = normalized_reason
            capsule.failure_streak_count = 1
        capsule.lease_until = 0.0
        capsule.cooldown_until = _now_ts() + max(0, int(cooldown_seconds))
        capsule.status = str(status or "ready")
        if auth_failure:
            capsule.consecutive_auth_failures += 1
            if capsule.consecutive_auth_failures >= max(1, int(retire_after_auth_failures)):
                capsule.status = "retired"
                capsule.retire_reason = f"auth_failure:{reason}"
        if (
            capsule.status != "retired"
            and auto_retire_reasons
            and normalized_reason in auto_retire_reasons
            and auto_retire_after > 0
            and capsule.failure_streak_count >= int(auto_retire_after)
        ):
            capsule.status = "retired"
            capsule.cooldown_until = 0.0
            capsule.retire_reason = f"auto_retire:{normalized_reason}:{capsule.failure_streak_count}"
        if capsule.status == "retired":
            capsule.cooldown_until = 0.0
            capsule.lease_until = 0.0
            capsule.scrub_sensitive_state()
        return self.upsert_capsule(capsule)

    def clear_cooldown(self, platform: str, capsule_id: str) -> SessionCapsule | None:
        capsule = self.get_capsule(platform, capsule_id)
        if capsule is None:
            return None
        if capsule.status == "retired":
            return capsule
        capsule.status = "ready"
        capsule.cooldown_until = 0.0
        capsule.lease_until = 0.0
        capsule.last_failure_reason = ""
        capsule.failure_streak_reason = ""
        capsule.failure_streak_count = 0
        return self.upsert_capsule(capsule)

    def retire_capsule(self, platform: str, capsule_id: str, *, reason: str = "manual_retire") -> SessionCapsule | None:
        capsule = self.get_capsule(platform, capsule_id)
        if capsule is None:
            return None
        capsule.status = "retired"
        capsule.cooldown_until = 0.0
        capsule.lease_until = 0.0
        capsule.retire_reason = str(reason or "manual_retire")
        capsule.scrub_sensitive_state()
        return self.upsert_capsule(capsule)

    def mark_maintenance_alerted(self, platform: str, *, timestamp: float | None = None) -> int:
        platform = str(platform or "").strip().lower()
        capsules = self._load_platform(platform)
        ts = float(timestamp if timestamp is not None else _now_ts())
        changed = 0
        for capsule in capsules:
            if capsule.status == "retired" or (capsule.status == "ready" and capsule.cooldown_until <= _now_ts()):
                capsule.maintenance_alerted_at = ts
                changed += 1
        if changed:
            self._save_platform(platform, capsules)
        return changed

    def recapture_needed(
        self,
        platform: str,
        *,
        min_ready: int = 1,
        alert_interval_seconds: int = 3600,
    ) -> tuple[bool, str]:
        summary = self.status_summary(platform)
        ready = int(summary.get("ready", 0))
        total = int(summary.get("total", 0))
        if total <= 0:
            return True, "no_capsules"
        if ready >= max(0, int(min_ready)):
            return False, ""
        capsules = self.list_capsules(platform, include_retired=True)
        last_alert = max((float(c.maintenance_alerted_at or 0) for c in capsules), default=0.0)
        if _now_ts() - last_alert < max(60, int(alert_interval_seconds)):
            return False, "alert_suppressed"
        return True, f"ready_below_threshold:{ready}/{min_ready}"

    def status_summary(self, platform: str) -> dict[str, int]:
        capsules = self.list_capsules(platform, include_retired=True)
        summary = {"total": len(capsules), "ready": 0, "cooldown": 0, "leased": 0, "retired": 0}
        now = _now_ts()
        for capsule in capsules:
            if capsule.status == "retired":
                summary["retired"] += 1
            elif capsule.lease_until > now:
                summary["leased"] += 1
            elif capsule.cooldown_until > now:
                summary["cooldown"] += 1
            else:
                summary["ready"] += 1
        return summary
