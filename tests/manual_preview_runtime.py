"""Safe UI fixture: python -m tests.manual_preview_runtime. No trading clients loaded."""
from pathlib import Path
import time
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from app.config_schema import DEFAULTS
from app.runtime_tasks import RuntimeTasks, holding_progress

app = FastAPI()
web = Path(__file__).resolve().parents[1] / "web"
monitor = RuntimeTasks()
mode = "idle"


def scenario(value):
    global monitor, mode
    mode = value
    monitor = RuntimeTasks()
    if value != "idle":
        monitor.begin("sell" if value == "sell" else "full", 500)
        monitor.session["started_at"] -= 482
    monitor.update("receive_worker", "waiting", "等待卖家发货", next_run_at=time.time()+24, last_result="已收取 2 件物品")
    monitor.update("sell_only_worker", "waiting" if value != "idle" else "disabled", "等待下次库存扫描", next_run_at=time.time()+380)
    monitor.update("listing_check_worker", "waiting", "等待下次成交检查", next_run_at=time.time()+220, last_result="检查 6 件，确认售出 1 件")
    monitor.update("exchange_rate_worker", "waiting", "等待下次汇率更新", next_run_at=time.time()+10800)
    monitor.update("holdings_report_worker", "disabled", "定时持仓报告未启用")
    monitor.update("session_keepalive_worker", "disabled", "会话保活未启用")
    if value in {"full", "payment"}:
        monitor.purchase(120)
        monitor.purchase(80)
        monitor.update("buy", "running", "分析候选饰品", item="AK-47 | Redline (Field-Tested)", done=8, total=24)
    if value == "sell":
        monitor.update("sell", "running", "正在提交上架", item="M4A1-S | Decimator", done=2, total=5, listed=2)
    if value == "error":
        monitor.update("buy", "failed", "BUFF 订单结果待核实，需先完成对账", last_error="订单状态未确认")
        monitor.update("sell_only_worker", "blocked", "Steam 登录已失效，请重新登录")


scenario("idle")


@app.get("/preview/{value}")
def switch(value):
    scenario(value)
    return RedirectResponse("/")


@app.get("/api/runtime-panel")
def runtime_panel():
    out = monitor.snapshot()
    out["controls"] = {"buy_running": mode in {"full", "payment"}, "sell_enabled": mode in {"full", "sell", "payment"}, "sell_paused": mode == "idle"}
    out["queue"] = []
    out["buy_blocker"] = {"message": "BUFF 订单结果待核实，请先完成对账"} if mode == "error" else {}
    out["holdings"] = holding_progress([], [])
    for key, count in [("receipt",3),("cooldown",8),("ready",5),("confirmation",1),("listed",6),("sold",12)]:
        out["holdings"][key].update(count=count,cost=count*40)
    return out


@app.api_route("/api/{path:path}", methods=["GET", "POST"])
async def api_stub(path: str, request: Request):
    if path == "pipeline/start":
        scenario("full")
    elif path == "pipeline/sell/start":
        scenario("sell")
    elif path in {"pipeline/stop", "pipeline/sell/stop"}:
        scenario("idle")
    elif path == "pipeline/buy/stop":
        scenario("sell")
    if path == "config":
        return {"ok": True, "config": {**DEFAULTS, "steam_guard": {"shared_secret": "preview-only"}}}
    if path == "status":
        return {"status": "running" if mode in {"full", "payment"} else "idle", "sell_only_enabled": mode in {"full", "sell", "payment"}}
    if path == "stats":
        return {"total_purchased": 1280, "total_sold": 1560, "total_profit": 280, "discount_ratio": 0.8205}
    if path == "pending_payment":
        return {"pending": {"pay_url": "https://example.com/mock-payment", "pay_type": "alipay", "name": "AK-47 | Redline (Field-Tested)"} if mode == "payment" else None}
    if path == "accounts":
        return {"accounts": []}
    return {"ok": True, "items": [], "lines": [], "strategies": [], "transactions": [], "purchases": [], "sales": [], "pending": None}


@app.get("/")
def index():
    return FileResponse(web / "index.html")


app.mount("/css", StaticFiles(directory=web / "css"))
app.mount("/js", StaticFiles(directory=web / "js"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8878, log_level="warning")
