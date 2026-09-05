import copy
import sys
import threading
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_validate_and_fill_preserves_real_boolean_values():
    from app.config_schema import DEFAULTS, merge, validate_and_fill

    cfg = validate_and_fill(
        merge(
            DEFAULTS,
            {
                "stability": {"use_vwap": False},
                "pipeline": {
                    "verbose_debug": True,
                    "steam_listings_debug": True,
                    "start_time_limit_enabled": True,
                },
                "notify": {"holdings_report_drop_enabled": False},
                "steam_confirm": {"enabled": True},
                "system": {"buff_session_keepalive_enabled": True},
                "proxy_pool": {"enabled": True},
                "steam_deals": {"enabled": True},
            },
        )
    )

    assert cfg["stability"]["use_vwap"] is False
    assert cfg["pipeline"]["verbose_debug"] is True
    assert cfg["pipeline"]["steam_listings_debug"] is True
    assert cfg["pipeline"]["start_time_limit_enabled"] is True
    assert cfg["notify"]["holdings_report_drop_enabled"] is False
    assert cfg["steam_confirm"]["enabled"] is True
    assert cfg["system"]["buff_session_keepalive_enabled"] is True
    assert cfg["proxy_pool"]["enabled"] is True
    assert cfg["steam_deals"]["enabled"] is True


def test_removed_shipping_opt_out_is_not_exposed_in_validated_config():
    from app.config_schema import DEFAULTS, merge, validate_and_fill

    cfg = validate_and_fill(merge(DEFAULTS, {"buff": {"auto_ask_seller_to_send": False}}))

    assert "auto_ask_seller_to_send" not in cfg["buff"]


def test_load_app_config_validated_applies_range_validation(monkeypatch):
    from app import config_loader

    monkeypatch.setattr(config_loader, "_config_cache", {})
    monkeypatch.setattr(config_loader, "_config_cache_ts", 0.0)
    monkeypatch.setattr(config_loader, "load_app_config", lambda: {"pipeline": {"max_discount": 9}})

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        cfg = config_loader.load_app_config_validated()

    assert cfg["pipeline"]["max_discount"] == 1.0


def test_save_app_config_validated_applies_range_validation(monkeypatch):
    from app import config_loader

    saved = {}
    monkeypatch.setattr(config_loader, "save_app_config", lambda data: saved.update(data))

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        config_loader.save_app_config_validated({"buff": {"price_tolerance": -1}})

    assert saved["buff"]["price_tolerance"] == 0.0


def test_update_app_config_validated_merges_partial_sections(monkeypatch):
    from app import config_loader

    stored = {
        "pipeline": {
            "max_discount": 0.8,
            "start_time_limit_enabled": False,
        },
        "system": {"buff_session_keepalive_enabled": False},
    }

    monkeypatch.setattr(
        config_loader,
        "load_app_config",
        lambda: copy.deepcopy(stored),
    )

    def fake_save(data):
        stored.clear()
        stored.update(copy.deepcopy(data))

    monkeypatch.setattr(config_loader, "save_app_config", fake_save)
    monkeypatch.setattr(config_loader, "_config_file_revision", lambda: None)
    config_loader._invalidate_config_cache()

    try:
        updated = config_loader.update_app_config_validated(
            {"system": {"buff_session_keepalive_enabled": True}}
        )

        assert updated["pipeline"]["max_discount"] == 0.8
        assert updated["pipeline"]["start_time_limit_enabled"] is False
        assert updated["system"]["buff_session_keepalive_enabled"] is True
    finally:
        config_loader._invalidate_config_cache()


def test_concurrent_partial_config_updates_do_not_overwrite_each_other(
    monkeypatch,
):
    from app import config_loader

    stored = {
        "pipeline": {"start_time_limit_enabled": False},
        "system": {"buff_session_keepalive_enabled": False},
    }
    first_save_entered = threading.Event()
    release_first_save = threading.Event()
    second_done = threading.Event()
    errors = []

    monkeypatch.setattr(
        config_loader,
        "load_app_config",
        lambda: copy.deepcopy(stored),
    )
    monkeypatch.setattr(config_loader, "_config_file_revision", lambda: None)

    def fake_save(data):
        pipeline = data.get("pipeline") or {}
        system = data.get("system") or {}
        if (
            pipeline.get("start_time_limit_enabled") is True
            and system.get("buff_session_keepalive_enabled") is False
        ):
            first_save_entered.set()
            if not release_first_save.wait(2):
                raise TimeoutError("first config save was not released")
        stored.clear()
        stored.update(copy.deepcopy(data))

    monkeypatch.setattr(config_loader, "save_app_config", fake_save)
    config_loader._invalidate_config_cache()

    def update(patch, done=None):
        try:
            config_loader.update_app_config_validated(patch)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(
        target=update,
        args=({"pipeline": {"start_time_limit_enabled": True}},),
    )
    second = threading.Thread(
        target=update,
        args=(
            {"system": {"buff_session_keepalive_enabled": True}},
            second_done,
        ),
    )

    try:
        first.start()
        assert first_save_entered.wait(1)
        second.start()
        assert not second_done.wait(0.05)
        release_first_save.set()
        first.join(2)
        second.join(2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert stored["pipeline"]["start_time_limit_enabled"] is True
        assert stored["system"]["buff_session_keepalive_enabled"] is True
    finally:
        release_first_save.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
        config_loader._invalidate_config_cache()


def test_config_cache_reloads_external_file_changes_and_returns_copies(
    monkeypatch,
    tmp_path,
):
    import config as config_store
    from app import config_loader

    config_file = tmp_path / "app_config.json"
    monkeypatch.setattr(config_store, "_APP_CONFIG_FILE", config_file)
    config_loader._invalidate_config_cache()

    try:
        config_store.save_app_config(
            {"pipeline": {"start_time_limit_enabled": False}}
        )
        first = config_loader.load_app_config_validated()
        first["pipeline"]["start_time_limit_enabled"] = True

        cached = config_loader.load_app_config_validated()
        assert cached["pipeline"]["start_time_limit_enabled"] is False

        # Simulate a user editing app_config.json while the process is alive.
        # The size change also makes this deterministic on coarse-mtime filesystems.
        config_store.save_app_config(
            {"pipeline": {"start_time_limit_enabled": True}}
        )
        refreshed = config_loader.load_app_config_validated()

        assert refreshed["pipeline"]["start_time_limit_enabled"] is True
    finally:
        config_loader._invalidate_config_cache()


def test_config_cache_retries_when_file_changes_during_read(monkeypatch):
    from app import config_loader

    current = {
        "revision": 1,
        "config": {"pipeline": {"start_time_limit_enabled": False}},
    }
    first_read = True

    def fake_revision():
        return current["revision"]

    def fake_load():
        nonlocal first_read
        loaded = copy.deepcopy(current["config"])
        if first_read:
            first_read = False
            current["config"] = {
                "pipeline": {"start_time_limit_enabled": True}
            }
            current["revision"] = 2
        return loaded

    monkeypatch.setattr(config_loader, "_config_file_revision", fake_revision)
    monkeypatch.setattr(config_loader, "load_app_config", fake_load)
    config_loader._invalidate_config_cache()

    try:
        loaded = config_loader.load_app_config_validated()

        assert loaded["pipeline"]["start_time_limit_enabled"] is True
        assert config_loader._config_cache_revision == 2
    finally:
        config_loader._invalidate_config_cache()
