from pathlib import Path

from app.runtime_env import (
    RuntimeProfile,
    get_bind_host,
    get_local_url,
    get_port,
    get_runtime_profile,
)


def test_server_requirements_omit_desktop_webview_dependency():
    text = Path("requirements-server.txt").read_text(encoding="utf-8").lower()
    deps = [line.strip().split(">", 1)[0] for line in text.splitlines() if line.strip() and not line.startswith("#")]

    assert "pywebview" not in deps


def test_legacy_app_main_exposes_fastapi_app_lazily():
    import app.main as main

    assert getattr(main, "app").title == "aetherswap"


def test_auto_linux_without_display_uses_server_mode():
    profile = get_runtime_profile(env={}, platform_name="linux")

    assert profile.mode == "server"
    assert profile.headless is True
    assert profile.can_launch_headful_browser is False
    assert get_bind_host(profile, env={}) == "0.0.0.0"


def test_no_display_stays_effectively_headless_even_if_env_disables_headless():
    profile = get_runtime_profile(env={"AETHERSWAP_HEADLESS": "0"}, platform_name="linux")

    assert profile.mode == "server"
    assert profile.headless is True
    assert profile.can_open_gui is False


def test_auto_linux_with_display_uses_desktop_mode():
    profile = get_runtime_profile(env={"DISPLAY": ":0"}, platform_name="linux")

    assert profile.mode == "desktop"
    assert profile.can_launch_headful_browser is True
    assert get_bind_host(profile, env={}) == "127.0.0.1"


def test_env_overrides_host_port_and_local_url():
    env = {"AETHERSWAP_MODE": "server", "AETHERSWAP_HOST": "127.0.0.1", "AETHERSWAP_PORT": "3000"}
    profile = get_runtime_profile(env=env, platform_name="linux")

    assert profile.mode == "server"
    assert get_bind_host(profile, env=env) == "127.0.0.1"
    assert get_port(env=env) == 3000
    assert get_local_url("0.0.0.0", 28472) == "http://127.0.0.1:28472"


def test_relogin_start_requires_manual_cookie_without_gui(monkeypatch):
    from app.routes import auth

    profile = RuntimeProfile(
        platform="linux",
        requested_mode="auto",
        mode="server",
        display_available=False,
        headless=True,
        can_open_gui=False,
        can_launch_headful_browser=False,
        open_browser=False,
        reason="no graphical display detected",
    )
    monkeypatch.setattr(auth, "get_runtime_profile", lambda: profile)

    def fail_thread(*args, **kwargs):
        raise AssertionError("headful relogin should not start without a GUI")

    monkeypatch.setattr(auth.threading, "Thread", fail_thread)

    result = auth._relogin_start("steam")

    assert result["ok"] is False
    assert result["code"] == "manual_cookie_required"
    assert result["manual_cookie_required"] is True
