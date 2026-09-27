from unittest.mock import Mock

from app import pipeline, sell_pipeline, state
from app.services import workers


def setup_state(monkeypatch):
    current = state.State()
    monkeypatch.setattr(state, "get_state", lambda: current)
    monkeypatch.setattr(pipeline, "get_state", lambda: current)
    monkeypatch.setattr(pipeline, "_pipeline_thread", None)
    monkeypatch.setattr(pipeline, "_pipeline_maintenance_reason", "")
    monkeypatch.setattr(pipeline, "_shutdown_pending", False)
    return current


def test_sell_only_resumes_after_stop_without_buying(monkeypatch):
    current = setup_state(monkeypatch)
    current.request_stop()
    buy = Mock()
    monkeypatch.setattr(pipeline, "_run_pipeline", buy)
    assert pipeline.start_sell_only()
    assert not current.is_stop_requested()
    assert current.get_status()["sell_only_enabled"]
    buy.assert_not_called()
    current.request_stop()
    assert not current.get_status()["sell_only_enabled"]


def test_sell_only_cannot_clear_stop_of_running_buy(monkeypatch):
    current = setup_state(monkeypatch)
    current.request_stop()
    monkeypatch.setattr(pipeline, "_pipeline_thread", Mock(is_alive=lambda: True))
    assert not pipeline.start_sell_only()
    assert current.is_stop_requested()


def test_background_sells_fresh_inventory_without_frontend(monkeypatch):
    current = setup_state(monkeypatch)
    items = [{"assetid": "owned", "can_sell": True}]
    scan = Mock(return_value=(True, items, None))
    sell = Mock()
    monkeypatch.setattr(workers, "scan_cs2_inventory", scan)
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(sell_pipeline, "_run_sell_phase", sell)
    assert not workers.run_sell_only_once()
    scan.assert_not_called()
    assert pipeline.start_sell_only()
    assert workers.run_sell_only_once()
    sell.assert_called_once_with({}, current, "sell-only", items=items)
    current.request_stop()
    assert not workers.run_sell_only_once()
    assert scan.call_count == 1


def test_stop_during_scan_prevents_listing(monkeypatch):
    current = setup_state(monkeypatch)
    pipeline.start_sell_only()
    def scan():
        current.request_stop()
        return True, [{"assetid": "owned"}], None
    sell = Mock()
    monkeypatch.setattr(workers, "scan_cs2_inventory", scan)
    monkeypatch.setattr(workers, "load_app_config_validated", lambda: {})
    monkeypatch.setattr(sell_pipeline, "_run_sell_phase", sell)
    assert not workers.run_sell_only_once()
    sell.assert_not_called()
