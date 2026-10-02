"""Status, log, plan, and payment-related routes."""
from pathlib import Path
from fastapi import APIRouter
from app.config_loader import load_app_config_validated
from app.state import (
    clear_log,
    confirm_payment,
    get_log,
    get_pending_payment,
    get_plan,
    get_status,
    set_pending_payment,
)
from config import get_buff
from pydantic import BaseModel
from utils.time import (
    now_in_configured_timezone,
    resolve_configured_timezone,
    timestamp_in_configured_timezone,
)
router = APIRouter()

@router.get("/api/runtime-panel")
def api_runtime_panel():
    from app.runtime_tasks import runtime, holding_progress, LABELS
    from app.state import get_state
    from app.pipeline import is_pipeline_running, get_pipeline_start_blocker
    from app.services.task_queue import get_task_queue
    state = get_state()
    out = runtime.snapshot()
    out["controls"] = {"buy_running": is_pipeline_running(),
                       "sell_enabled": state.get_status().get("sell_only_enabled", False),
                       "sell_paused": state.is_sell_stop_requested()}
    if not out["controls"]["buy_running"] and not out["controls"]["sell_enabled"]:
        if not any(t["id"] == "sell" and t["status"] in {"running", "stopping"} for t in out["tasks"]):
            if out["session"].get("started_at"):
                out["session"]["finished_at"] = max(
                    [out["session"]["started_at"]] +
                    [t["finished_at"] for t in out["tasks"]
                     if t["id"] in {"buy", "sell"} and t.get("finished_at")]
                )
    out["holdings"] = holding_progress(state.get_purchases(), state.get_inventory())
    out["buy_blocker"] = get_pipeline_start_blocker()
    out["queue"] = [{"name": LABELS.get(t["name"], t["name"]), "status": t["status"],
                     "created_at": t["created_at"]} for t in get_task_queue().list_tasks()
                    if t["status"] in {"pending", "retrying", "failed"}]
    return out
class ConfirmBody(BaseModel):
    ok: bool
@router.get("/api/status")
def api_status():
    st = get_status()
    buff_creds = get_buff()
    st["buff_no_cookie"] = not bool((buff_creds.get("cookies") or "").strip())
    return st

@router.get("/api/log")
def api_log(since: int = 0):
    return {"lines": get_log(since)}
@router.post("/api/log/clear")
def api_log_clear():
    clear_log()
    return {"ok": True}
@router.post("/api/log/export")
def api_log_export():
    lines = get_log(0)
    log_dir = Path("log")
    log_dir.mkdir(exist_ok=True)
    configured_timezone, timezone_label = resolve_configured_timezone(
        (load_app_config_validated().get("system") or {})
    )
    ts = now_in_configured_timezone(configured_timezone).strftime("%Y%m%d_%H%M%S")
    filename = log_dir / f"debug_{ts}.txt"
    def fmt_time(t):
        if t is None:
            return ""
        return timestamp_in_configured_timezone(
            t,
            configured_timezone,
        ).strftime("%Y-%m-%d %H:%M:%S")
    content = "\n".join(
        f"{fmt_time(e.get('t'))} [{e.get('level', 'info')}] {e.get('msg', '')}"
        for e in lines
    ) + f"\n# timezone: {timezone_label}\n"
    filename.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(filename), "lines": len(lines)}
@router.get("/api/plan")
def api_plan():
    return {"plan": get_plan()}
@router.get("/api/pending_payment")
def api_pending_payment():
    return {"pending": get_pending_payment()}
@router.post("/api/confirm_payment")
def api_confirm_payment(body: ConfirmBody):
    confirm_payment(body.ok)
    set_pending_payment(None)
    return {"ok": True}
