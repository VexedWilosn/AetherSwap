from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import app


def test_connectivity_prefers_runtime_state_file(tmp_path: Path):
    runtime_path = Path("config/platform_runtime_state.json")
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "steam": {
            "platform": "steam",
            "status": "ok",
            "rows": 12,
            "saved": 7,
            "reason": "",
            "updated_at": "2026-05-09T07:30:00",
            "cost_seconds": 1.23,
        },
        "steamdt_openapi": {
            "platform": "steamdt_openapi",
            "status": "ok",
            "rows": 88,
            "saved": 21,
            "reason": "",
            "updated_at": "2026-05-09T07:31:00",
            "cost_seconds": 2.34,
        },
    }
    runtime_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    client = TestClient(app)
    res = client.get("/api/platform/connectivity")
    assert res.status_code == 200
    data = res.json()
    rows = [x for x in data.get("platforms", []) if x.get("platform") == "steam"]
    assert rows
    steam = rows[0]
    assert steam["status"] in {"ok", "running", "idle", "disabled", "degraded", "timeout", "error", "no_data"}

    openapi_rows = [x for x in data.get("platforms", []) if x.get("platform") == "steamdt_openapi"]
    assert openapi_rows
    assert openapi_rows[0]["status"] in {"ok", "running", "idle", "disabled", "degraded", "timeout", "error", "no_data"}
