"""Status, log, plan, and payment-related routes."""
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter
from app.state import (
    clear_log,
    confirm_payment,
    get_log,
    get_pending_payment,
    get_plan,
    get_purchases,
    get_receive_status,
    get_status,
    log,
    set_inventory,
    set_pending_payment,
    set_receive_status,
    update_purchase,
    update_purchase_by_id,
)
from config import get_buff
from pydantic import BaseModel
router = APIRouter()
class ConfirmBody(BaseModel):
    ok: bool
    payment_id: str | None = None
@router.get("/api/status")
def api_status():
    st = get_status()
    buff_creds = get_buff()
    st["buff_no_cookie"] = not bool((buff_creds.get("cookies") or "").strip())
    st["receive"] = get_receive_status()
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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = log_dir / f"debug_{ts}.txt"
    def fmt_time(t):
        if t is None:
            return ""
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    content = "\n".join(
        f"{fmt_time(e.get('t'))} [{e.get('level', 'info')}] {e.get('msg', '')}"
        for e in lines
    ) + "\n"
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
    accepted = confirm_payment(body.ok, payment_id=body.payment_id)
    if accepted:
        set_pending_payment(None)
    return {"ok": accepted, "stale": not accepted}

@router.post("/api/receive_now")
def api_receive_now():
    from app.config_loader import get_buff_credentials, get_steam_credentials
    from app.inventory_cs2 import scan_cs2_inventory
    from app.receive_flow import try_receive_once

    def log_fn(msg: str, level: str = "info"):
        log(msg, level, category="receive")
        set_receive_status("running" if level != "warn" else "warning", msg)

    try:
        set_receive_status("running", "手动触发收货")
        n = try_receive_once(
            get_purchases,
            update_purchase,
            lambda: (get_buff_credentials() or {}).get("cookies", ""),
            get_steam_credentials,
            scan_inventory=scan_cs2_inventory,
            update_purchase_by_id=update_purchase_by_id,
            log_fn=log_fn,
        )
        if n > 0:
            ok_inv, inv_items, inv_err = scan_cs2_inventory()
            if ok_inv:
                set_inventory(inv_items)
            else:
                log_fn(f"手动收货后库存刷新失败: {inv_err or '未知错误'}", "warn")
            set_receive_status("received", f"手动收货成功，处理 {n} 个报价")
        else:
            set_receive_status("idle", "本次没有收取到新的报价")
        return {"ok": True, "received": n, "receive": get_receive_status()}
    except Exception as e:
        msg = f"手动收货失败: {type(e).__name__}: {e}"
        log(msg, "error", category="receive")
        set_receive_status("error", msg)
        return {"ok": False, "error": msg[:200], "receive": get_receive_status()}
