from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STOP_FLAG_PATH = BASE_DIR / "flags" / "dataengine_stop.flag"


def request_stop() -> None:
    STOP_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG_PATH.write_text("stop", encoding="utf-8")


def clear_stop() -> None:
    try:
        STOP_FLAG_PATH.unlink()
    except FileNotFoundError:
        pass


def is_stop_requested() -> bool:
    return STOP_FLAG_PATH.exists()


class StopRequested(RuntimeError):
    pass


def raise_if_stop_requested() -> None:
    if is_stop_requested():
        raise StopRequested("DataEngine stop requested")
