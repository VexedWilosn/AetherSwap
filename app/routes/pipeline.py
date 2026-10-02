"""Pipeline start/stop routes."""
from fastapi import APIRouter
from pydantic import BaseModel
from app.pipeline import get_pipeline_start_blocker, start_pipeline
from app.state import request_stop, set_status, log
router = APIRouter()

@router.post("/api/pipeline/sell/start")
def api_sell_start():
    from app.config_loader import get_steam_credentials, load_app_config_validated
    from app.strategy_engine import apply_strategy_to_config
    from app.pipeline import start_sell_only

    cfg = apply_strategy_to_config(load_app_config_validated(), "sell")
    if int(cfg.get("pipeline", {}).get("sell_strategy", 1)) == 4:
        return {"ok": False, "error": "当前策略暂停自动出售，请先选择出售策略"}
    if not get_steam_credentials().get("cookies"):
        return {"ok": False, "error": "请先登录 Steam"}
    if not start_sell_only():
        return {"ok": False, "error": "买入任务仍在运行或系统正在维护，请稍后重试"}
    log("已启用独立卖出，后台将定时扫描库存并上架", category="steam")
    return {"ok": True}

class ConfigBody(BaseModel):
    config: dict
    acknowledge_buff_reconciliation: bool = False
    buff_reconciliation_intent_id: str = ""

@router.post("/api/pipeline/sell/resume")
def api_sell_resume():
    from app.state import get_state
    from app.pipeline import is_shutdown_pending
    from app.runtime_tasks import runtime
    from app.config_loader import load_app_config_validated, get_steam_credentials
    from app.strategy_engine import apply_strategy_to_config
    cfg = apply_strategy_to_config(load_app_config_validated(), "sell")
    if is_shutdown_pending() or int(cfg.get("pipeline", {}).get("sell_strategy", 1)) == 4:
        return {"ok": False, "error": "系统正在退出或当前策略暂停自动出售"}
    if not get_steam_credentials().get("cookies"):
        return {"ok": False, "error": "请先登录 Steam"}
    get_state().resume_selling()
    runtime.update("sell_only_worker", "pending", "等待后台扫描库存", next_run_at=None)
    return {"ok": True}
@router.post("/api/pipeline/start")
def api_pipeline_start(body: ConfigBody):
    blocker = get_pipeline_start_blocker()
    if (
        blocker.get("code") == "BUFF_RECONCILIATION_REQUIRED"
        and not body.acknowledge_buff_reconciliation
    ):
        return {
            "ok": False,
            "reconciliation_required": True,
            "code": blocker["code"],
            "error": blocker["message"],
            "checkout": blocker.get("checkout") or {},
        }
    if blocker and blocker.get("code") != "BUFF_RECONCILIATION_REQUIRED":
        return {
            "ok": False,
            "code": blocker.get("code"),
            "error": blocker.get("message"),
        }
    if not start_pipeline(
        body.config,
        acknowledge_buff_reconciliation=body.acknowledge_buff_reconciliation,
        buff_reconciliation_intent_id=body.buff_reconciliation_intent_id,
    ):
        blocker = get_pipeline_start_blocker()
        if blocker:
            return {
                "ok": False,
                "reconciliation_required": (
                    blocker.get("code") == "BUFF_RECONCILIATION_REQUIRED"
                ),
                "code": blocker.get("code"),
                "error": blocker.get("message"),
                "checkout": blocker.get("checkout") or {},
            }
        log("买入流水线已在运行，忽略重复启动请求", level="warn", category="pipeline")
        return {"ok": False, "already_running": True, "error": "买入流水线已在运行，请勿重复启动"}
    return {"ok": True}


@router.get("/api/pipeline/buff_checkout_guard")
def api_buff_checkout_guard():
    blocker = get_pipeline_start_blocker()
    return {
        "reconciliation_required": (
            blocker.get("code") == "BUFF_RECONCILIATION_REQUIRED"
        ),
        "checkout": blocker.get("checkout") or {},
    }
@router.post("/api/pipeline/stop")
def api_pipeline_stop():
    request_stop()
    from app.runtime_tasks import runtime
    runtime.stop_task("buy", "用户停止买入")
    runtime.stop_task("sell", "用户停止上架，已发出的请求可能仍在完成")
    log("接收到停止运行指令，正在终止任务...", level="warn", category="system")
    set_status("stopped", "正在停止并清理...")
    return {"ok": True}


@router.post("/api/pipeline/buy/stop")
def api_buy_stop():
    from app.state import get_state
    from app.runtime_tasks import runtime
    get_state().request_buy_stop()
    runtime.stop_task("buy", "停止买入，等待已发出的请求返回")
    return {"ok": True}


@router.post("/api/pipeline/sell/stop")
def api_sell_stop():
    from app.state import get_state
    from app.runtime_tasks import runtime
    get_state().request_sell_stop()
    runtime.update("sell_only_worker", "disabled", "用户暂停自动上架", next_run_at=None)
    runtime.stop_task("sell", "已停止上架，已上架物品继续留在市场")
    return {"ok": True}
