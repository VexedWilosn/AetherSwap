import threading
from unittest.mock import Mock

import pytest
from app import runtime_tasks as rt
from app.state import State


@pytest.fixture
def monitor(monkeypatch):
    instance = rt.RuntimeTasks()
    monkeypatch.setattr(rt, "runtime", instance)
    return instance


def test_parallel_scopes_do_not_overwrite_each_other(monitor):
    barrier = threading.Barrier(2)
    def run(key):
        with rt.task_scope(key):
            barrier.wait(timeout=2)
            rt.note(key + " detail")
    threads = [threading.Thread(target=run, args=(key,)) for key in ("buy", "sell")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)
        assert not t.is_alive()
    assert {t["id"] for t in monitor.snapshot()["tasks"]} == {"buy", "sell"}
    assert all(t["status"] == "success" for t in monitor.snapshot()["tasks"])


def test_sleep_exposes_next_run_and_preserves_block_reason(monitor):
    captured = []
    with rt.task_scope("receive_worker"):
        rt.note("等待登录 BUFF", "blocked")
        rt.worker_sleep(30, sleep_fn=lambda seconds: captured.append(monitor.snapshot()))
    task = captured[0]["tasks"][0]
    assert task["status"] == "blocked"
    assert task["detail"] == "等待登录 BUFF"
    assert task["next_run_at"] - captured[0]["server_time"] == pytest.approx(30, abs=1)


def test_buy_and_sell_stop_are_independent(monitor):
    state = State()
    state.enable_sell_only()
    state.request_buy_stop()
    assert state.is_stop_requested()
    assert not state.is_sell_stop_requested()
    assert state.get_status()["sell_only_enabled"]
    state.clear_stop()
    state.request_sell_stop()
    assert state.is_sell_stop_requested()
    assert not state.is_stop_requested()
    state.request_stop()
    assert state.is_stop_requested() and state.is_sell_stop_requested()


def test_resume_selling_does_not_clear_buy_stop(monitor):
    state = State()
    state.request_stop()
    state.resume_selling()
    assert state.is_stop_requested()
    assert not state.is_sell_stop_requested()
    assert state.get_status()["sell_only_enabled"]


def test_direct_candidate_status_reaches_panel(monitor):
    state = State()
    state.set_status("running", "STABILITY_CHECK", progress_total=20,
                     progress_done=7, progress_item="item 8", next_progress_item="item 9")
    task = monitor.snapshot()["tasks"][0]
    assert task["detail"] == "检查饰品稳定性"
    assert task["done"] == 7
    assert task["item"] == "item 8"
    assert task["next_item"] == "item 9"


def test_backend_restart_requires_explicit_selling_start(monitor):
    state = State()
    assert state.is_sell_stop_requested()
    assert not state.get_status()["sell_only_enabled"]


def test_new_sell_session_does_not_display_previous_buy_task(monitor):
    monitor.begin("full", 100)
    monitor.purchase(20)
    monitor.update("buy", "success", "买入完成")
    monitor.begin("sell")
    assert monitor.snapshot()["session"]["spent"] == 0
    assert all(t["id"] != "buy" for t in monitor.snapshot()["tasks"])


def test_buy_cost_only_counts_persisted_purchase_in_buy_scope(monkeypatch, monitor):
    from app import state as state_module
    write = Mock()
    monkeypatch.setattr(state_module, "db_append_purchase", write)
    state = State()
    monitor.begin("full", 100)
    state.append_purchase({"price": 90})  # manual edit is not this run's spend
    with rt.task_scope("buy"):
        state.append_purchase({"price": 20})
        write.side_effect = RuntimeError("DB unavailable")
        with pytest.raises(RuntimeError):
            state.append_purchase({"price": 80})
    assert monitor.snapshot()["session"]["spent"] == 20
    assert monitor.snapshot()["session"]["bought"] == 1


def test_holdings_use_evidence_not_assumed_cooldown():
    purchases = [
        {"assetid": "1", "price": 10, "pending_receipt": True},
        {"assetid": "2", "price": 20},
        {"assetid": "3", "price": 30},
        {"assetid": "4", "price": 40, "listing": True, "listing_status": "pending_confirmation"},
        {"assetid": "5", "price": 50, "listing": True},
        {"assetid": "6", "price": 60, "sale_price": 70},
        {"assetid": "7", "price": 70},
    ]
    inventory = [{"assetid": "2", "cooldown_at": rt.time.time()+3600}, {"assetid": "3", "can_sell": True}]
    groups = rt.holding_progress(purchases, inventory)
    assert sum(g["count"] for g in groups.values()) == 7
    for key in ("receipt", "cooldown", "ready", "confirmation", "listed", "sold", "unknown"):
        assert groups[key]["count"] == 1
    assert groups["sold"]["cost"] == 60


def test_sell_context_does_not_mutate_buy_status(monitor):
    from app.sell_pipeline import SellContext
    state = State()
    state.set_status("running", "CHECKING_STABILITY")
    ctx = SellContext(state, "sell")
    state.request_sell_stop()
    assert ctx.is_stop_requested()
    ctx.set_status("stopped", "出售已停止")
    assert state.get_status()["step"] == "CHECKING_STABILITY"


def test_panel_endpoint_is_read_only(monkeypatch, monitor):
    from app.routes.status import api_runtime_panel
    from app import state as state_module, pipeline
    state = State()
    monkeypatch.setattr(state_module, "get_state", lambda: state)
    monkeypatch.setattr(state, "get_purchases", lambda: [])
    monkeypatch.setattr(pipeline, "is_pipeline_running", lambda: False)
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {"message": "请完成对账"})
    result = api_runtime_panel()
    assert result["controls"]["buy_running"] is False
    assert result["buy_blocker"]["message"] == "请完成对账"
    assert result["session"]["started_at"] is None
    monitor.begin("sell")
    monitor.stop_task("sell", "停止")
    first = api_runtime_panel()
    second = api_runtime_panel()
    assert first["session"]["finished_at"] == second["session"]["finished_at"]
    assert "finished_at" not in monitor.session


def test_confirmation_pending_recorded_without_marking_sold():
    from app.sell_pipeline import _record_confirmation_state
    ctx = Mock()
    ctx.state.get_purchases.return_value = [{"_db_id": 7, "assetid": "asset"}]
    _record_confirmation_state(ctx, "asset", {"needs_mobile_confirmation": True})
    ctx.state.update_purchase_by_id.assert_called_once_with(7, {"listing_status": "pending_confirmation"})


def test_full_start_leaves_background_selling_enabled_after_buy_finishes(monkeypatch, monitor):
    from app import pipeline, state as state_module
    current = State()
    monkeypatch.setattr(pipeline, "get_state", lambda: current)
    monkeypatch.setattr(state_module, "get_state", lambda: current)
    monkeypatch.setattr(pipeline, "_pipeline_thread", None)
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    monkeypatch.setattr(pipeline, "get_unresolved_checkout", lambda: None)
    monkeypatch.setattr(pipeline, "get_pipeline_start_blocker", lambda: {})
    monkeypatch.setattr(pipeline, "_run_pipeline", lambda config: current.set_status("idle", ""))
    assert pipeline.start_pipeline({"pipeline": {"target_balance": 100}})
    with pipeline._pipeline_start_lock:
        thread = pipeline._pipeline_thread
    if thread:
        thread.join(timeout=2)
    assert not pipeline.is_pipeline_running()
    assert current.get_status()["sell_only_enabled"]
    assert not current.is_sell_stop_requested()
    assert monitor.snapshot()["session"]["mode"] == "full"


def test_resume_route_preserves_buy_stop_and_requires_sell_strategy(monkeypatch, monitor):
    from app.routes.pipeline import api_sell_resume
    from app import state as state_module, config_loader, strategy_engine, pipeline
    current = State()
    current.request_stop()
    monkeypatch.setattr(state_module, "get_state", lambda: current)
    monkeypatch.setattr(pipeline, "is_shutdown_pending", lambda: False)
    monkeypatch.setattr(config_loader, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(config_loader, "get_steam_credentials", lambda: {"cookies": "fixture"})
    monkeypatch.setattr(strategy_engine, "apply_strategy_to_config", lambda *a: {"pipeline": {"sell_strategy": 4}})
    assert not api_sell_resume()["ok"]
    assert current.is_sell_stop_requested()
    monkeypatch.setattr(strategy_engine, "apply_strategy_to_config", lambda *a: {"pipeline": {"sell_strategy": 1}})
    assert api_sell_resume()["ok"]
    assert current.is_stop_requested()
    assert not current.is_sell_stop_requested()
