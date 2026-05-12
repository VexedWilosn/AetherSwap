from __future__ import annotations

from pathlib import Path


def test_master_loop_no_legacy_steamdt_fetcher_call():
    path = Path("DataEngine/master_loop.py")
    text = path.read_text(encoding="utf-8")
    assert '_run_script("steamdt_fetcher.py")' not in text
