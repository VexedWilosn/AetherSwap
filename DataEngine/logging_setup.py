from __future__ import annotations

import logging
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"


def setup_dataengine_logging() -> None:
    """Configure DataEngine logging with UTF-8 handlers once per process."""

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    has_file = False
    has_stream = False
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            has_file = True
            try:
                handler.setFormatter(formatter)
            except Exception:
                pass
        elif isinstance(handler, logging.StreamHandler):
            has_stream = True
            try:
                handler.setFormatter(formatter)
            except Exception:
                pass

    if not has_stream:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    if not has_file:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
