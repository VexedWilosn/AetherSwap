from __future__ import annotations

import time
from pathlib import Path

from app.services.session_capsule_pool import SessionCapsulePool
from DataEngine.steamdt_fetcher import register_steamdt_capsule_from_cookie


def test_capsule_pool_lease_success_and_cooldown(tmp_path: Path):
    pool = SessionCapsulePool(tmp_path / "session_capsules.json")
    capsule = pool.register_capsule(
        platform="steamdt",
        cookie_header="SDT_DeviceId=abc; i18n_redirected=zh",
        device_id="abc",
        user_agent="Mozilla/5.0",
        headers={"x-device-id": "abc"},
    )

    leased = pool.lease_capsule("steamdt", lease_ttl_seconds=1)
    assert leased is not None
    assert leased.capsule_id == capsule.capsule_id

    pool.mark_success("steamdt", capsule.capsule_id)
    refreshed = pool.get_capsule("steamdt", capsule.capsule_id)
    assert refreshed is not None
    assert refreshed.fail_count == 0
    assert refreshed.last_ok_at

    leased_again = pool.lease_capsule("steamdt", lease_ttl_seconds=1)
    assert leased_again is not None
    pool.mark_failure("steamdt", capsule.capsule_id, reason="timeout", cooldown_seconds=2)
    blocked = pool.lease_capsule("steamdt", lease_ttl_seconds=1)
    assert blocked is None

    time.sleep(2.1)
    recovered = pool.lease_capsule("steamdt", lease_ttl_seconds=1)
    assert recovered is not None


def test_capsule_pool_retires_after_auth_failures(tmp_path: Path):
    pool = SessionCapsulePool(tmp_path / "session_capsules.json")
    capsule = pool.register_capsule(platform="steamdt", device_id="abc", cookie_header="SDT_DeviceId=abc")
    for _ in range(3):
        pool.mark_failure("steamdt", capsule.capsule_id, reason="auth_invalid", cooldown_seconds=1, auth_failure=True)
    retired = pool.get_capsule("steamdt", capsule.capsule_id)
    assert retired is not None
    assert retired.status == "retired"


def test_capsule_pool_manual_clear_cooldown_and_retire(tmp_path: Path):
    pool = SessionCapsulePool(tmp_path / "session_capsules.json")
    capsule = pool.register_capsule(
        platform="steamdt",
        device_id="abc",
        cookie_header="SDT_DeviceId=abc; steamdt_token=secret",
        cookies={"SDT_DeviceId": "abc", "steamdt_token": "secret"},
        headers={"cookie": "SDT_DeviceId=abc", "user-agent": "Mozilla/5.0", "x-device-id": "abc"},
        local_storage={"token": "secret"},
        session_storage={"tmp": "secret"},
    )
    pool.mark_failure("steamdt", capsule.capsule_id, reason="timeout", cooldown_seconds=120)

    cooled = pool.get_capsule("steamdt", capsule.capsule_id)
    assert cooled is not None
    assert cooled.cooldown_until > time.time()

    cleared = pool.clear_cooldown("steamdt", capsule.capsule_id)
    assert cleared is not None
    assert cleared.status == "ready"
    assert cleared.cooldown_until == 0
    assert cleared.last_failure_reason == ""

    retired = pool.retire_capsule("steamdt", capsule.capsule_id, reason="manual_retire")
    assert retired is not None
    assert retired.status == "retired"
    assert retired.retire_reason == "manual_retire"
    assert retired.cookie_header == ""
    assert retired.cookies == {}
    assert retired.local_storage == {}
    assert retired.session_storage == {}
    assert "cookie" not in retired.headers
    assert "x-device-id" not in retired.headers
    assert retired.headers.get("user-agent") == "Mozilla/5.0"


def test_capsule_pool_auto_retires_consecutive_waf_failures(tmp_path: Path):
    pool = SessionCapsulePool(tmp_path / "session_capsules.json")
    capsule = pool.register_capsule(platform="steamdt", device_id="abc", cookie_header="SDT_DeviceId=abc")
    for _ in range(3):
        updated = pool.mark_failure(
            "steamdt",
            capsule.capsule_id,
            reason="waf_block",
            cooldown_seconds=1,
            auto_retire_reasons={"waf_block", "empty_soft_block"},
            auto_retire_after=3,
        )
    assert updated is not None
    assert updated.status == "retired"
    assert updated.failure_streak_count == 3
    assert updated.retire_reason == "auto_retire:waf_block:3"
    assert updated.cookie_header == ""
    assert updated.cookies == {}


def test_capsule_pool_recapture_needed_is_rate_limited(tmp_path: Path):
    pool = SessionCapsulePool(tmp_path / "session_capsules.json")
    capsule = pool.register_capsule(platform="steamdt", device_id="abc", cookie_header="SDT_DeviceId=abc")
    pool.retire_capsule("steamdt", capsule.capsule_id, reason="manual_retire")

    needed, reason = pool.recapture_needed("steamdt", min_ready=1, alert_interval_seconds=3600)
    assert needed is True
    assert reason.startswith("ready_below_threshold")

    pool.mark_maintenance_alerted("steamdt", timestamp=time.time())
    needed, reason = pool.recapture_needed("steamdt", min_ready=1, alert_interval_seconds=3600)
    assert needed is False
    assert reason == "alert_suppressed"


def test_register_steamdt_capsule_preserves_cookie_header_device_id():
    capsule = register_steamdt_capsule_from_cookie(
        "SDT_DeviceId=test-device; steamdt_token=abc123; i18n_redirected=zh",
        user_agent="Mozilla/5.0",
        notes="pytest",
    )
    assert capsule.device_id == "test-device"
    assert "steamdt_token=abc123" in capsule.cookie_header
