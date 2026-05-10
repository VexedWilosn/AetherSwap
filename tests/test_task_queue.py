import time

from app import api
from app.services.task_queue import TaskQueue, TaskStatus


def test_cancel_pending_task_marks_cancelled():
    queue = TaskQueue(max_workers=1)

    first = queue.submit(time.sleep, 0.5, name="blocking")
    second = queue.submit(lambda: "done", name="pending")

    assert queue.cancel_task(second) is True

    task = queue.get_task(second)
    assert task is not None
    assert task["status"] == TaskStatus.CANCELLED.value
    assert task["finished_at"] is not None

    queue.cancel_task(first)
    queue.shutdown(wait=False)


def test_cancel_all_requests_running_task_cancel():
    queue = TaskQueue(max_workers=1)
    task_id = queue.submit(time.sleep, 1, name="long-running")

    for _ in range(20):
        task = queue.get_task(task_id)
        if task and task["status"] == TaskStatus.RUNNING.value:
            break
        time.sleep(0.01)

    assert queue.cancel_all() >= 1
    task = queue.get_task(task_id)
    assert task is not None
    assert task["cancel_requested"] is True
    queue.shutdown(wait=False)


def test_radar_jit_refresh_uses_requested_platforms(monkeypatch):
    captured = {}

    async def fake_refresh_items_prices(item_ids, platforms, fast=True):
        captured["item_ids"] = item_ids
        captured["platforms"] = platforms
        captured["fast"] = fast
        return []

    monkeypatch.setattr("DataEngine.main_engine.refresh_items_prices", fake_refresh_items_prices)

    result = api.api_market_radar_jit_refresh({"item_ids": [1], "platforms": ["buff"]})

    assert result["success"] is True
    assert captured["item_ids"] == {1}
    assert captured["platforms"] == {"buff"}
    assert "steam" not in captured["platforms"]


def test_radar_jit_refresh_defaults_include_steam(monkeypatch):
    captured = {}

    async def fake_refresh_items_prices(item_ids, platforms, fast=True):
        captured["platforms"] = platforms
        return []

    monkeypatch.setattr("DataEngine.main_engine.refresh_items_prices", fake_refresh_items_prices)

    result = api.api_market_radar_jit_refresh({"item_ids": [1]})

    assert result["success"] is True
    assert captured["platforms"] == {"steam", "buff", "uuyp", "eco"}


def test_radar_jit_refresh_clears_dataengine_stop_flag(monkeypatch):
    from DataEngine.stop_signal import STOP_FLAG_PATH

    captured = {}

    async def fake_refresh_items_prices(item_ids, platforms, fast=True):
        captured["stop_exists_during_refresh"] = STOP_FLAG_PATH.exists()
        return []

    STOP_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG_PATH.write_text("stop", encoding="utf-8")
    monkeypatch.setattr("DataEngine.main_engine.refresh_items_prices", fake_refresh_items_prices)

    result = api.api_market_radar_jit_refresh({"item_ids": [1], "platforms": ["buff"]})

    assert result["success"] is True
    assert captured["stop_exists_during_refresh"] is False
