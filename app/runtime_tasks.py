"""In-process execution telemetry. Never retries or schedules trade writes."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from collections import deque
import threading
import time

CURRENT_TASK = ContextVar("runtime_task", default=None)
LABELS = {
    "buy": "购买流水线", "sell": "出售流水线",
    "receive_worker": "自动收货", "sell_only_worker": "库存扫描",
    "listing_check_worker": "成交检查", "exchange_rate_worker": "汇率更新",
    "holdings_report_worker": "持仓报告", "session_keepalive_worker": "BUFF 会话保活",
    "sync_account_region": "账号地区同步",
}
STEPS = {
    "STARTING": "准备买入", "PROXY_WARMUP": "预热代理",
    "FETCHING": "拉取行情", "FETCHING_DEALS": "拉取行情",
    "CHECKING_STABILITY": "分析候选饰品", "CHECKOUT_PENDING": "等待订单与支付确认",
    "STABILITY_CHECK": "检查饰品稳定性",
    "WAITING_RETRY": "等待下一轮选品", "TIME_LIMIT_WAIT": "等待购买时间窗",
    "NETWORK_OFFLINE": "等待网络恢复", "STEAM_COOLDOWN": "买入完成，等待交付和交易冷却",
    "CONFIG_ERROR": "购买配置不完整",
    "BUFF_AUTH_EXPIRED": "BUFF 登录已失效", "BUFF_VERIFICATION_REQUIRED": "等待 BUFF 安全验证",
    "BUFF_RATE_LIMITED": "BUFF 请求限流，已停止买入", "BUFF_REQUEST_BLOCKED": "BUFF 请求被安全策略阻止",
    "BUFF_WRITE_RESULT_UNKNOWN": "订单结果未知，等待对账", "BUFF_POST_COMMIT_WRITE_UNKNOWN": "已成交，后续请求结果待核实",
    "BUFF_ORDER_CREATED_PENDING": "订单已创建，等待核实完成状态", "BUFF_COOLING_DOWN": "BUFF 冷却中，等待核实订单",
    "BUFF_POST_COMMIT_HALT": "成交后发生异常，已停止继续买入", "PIPELINE_UNEXPECTED_ERROR": "买入异常，请查看运行日志",
}
WAIT_STEPS = {"CHECKOUT_PENDING", "WAITING_RETRY", "TIME_LIMIT_WAIT", "NETWORK_OFFLINE"}


class RuntimeTasks:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks = {}
        self.events = deque(maxlen=160)
        self.session = {"mode": "idle", "started_at": None, "target": 0, "spent": 0, "bought": 0}

    def update(self, key, status=None, detail=None, **fields):
        with self.lock:
            now = time.time()
            task = self.tasks.setdefault(key, {
                "id": key, "name": LABELS.get(key, key), "status": "idle",
                "detail": "尚未执行", "started_at": None, "step_started_at": now,
                "finished_at": None, "next_run_at": None, "last_result": "",
                "last_error": "", "done": 0, "total": 0, "updated_at": now,
            })
            changed = ((status is not None and task["status"] != status)
                       or (detail is not None and task["detail"] != detail))
            if status == "running" and task["status"] != "running":
                task["started_at"] = now
                task["finished_at"] = None
                task["next_run_at"] = None
            if status in {"success", "failed", "stopped"}:
                task["finished_at"] = now
                task["next_run_at"] = None
            if changed:
                task["step_started_at"] = now
            if status is not None:
                task["status"] = status
            if detail is not None:
                task["detail"] = str(detail)[:500]
            task.update(fields, updated_at=now)
            if changed:
                self.events.append({"at": now, "task": key, "name": task["name"],
                                    "status": task["status"], "detail": task["detail"]})

    def begin(self, mode, target=0):
        with self.lock:
            self.session = {"mode": mode, "started_at": time.time(), "target": target,
                            "spent": 0, "bought": 0}
            self.tasks.pop("buy", None)
        if mode == "full":
            self.update("buy", "pending", "等待购买线程开始", done=0, total=0)

    def purchase(self, cost):
        with self.lock:
            self.session["spent"] = round(self.session["spent"] + float(cost), 2)
            self.session["bought"] += 1

    def snapshot(self):
        with self.lock:
            return deepcopy({"session": self.session, "tasks": list(self.tasks.values()),
                             "events": list(reversed(self.events)), "server_time": time.time()})

    def stop_task(self, key, detail):
        with self.lock:
            active = self.tasks.get(key, {}).get("status") == "running"
        self.update(key, "stopping" if active else "stopped", detail, next_run_at=None)


runtime = RuntimeTasks()


def record_buy_status(stage, step, total=0, done=0, item="", next_item=""):
    status = "waiting" if step in WAIT_STEPS else {"error": "failed", "idle": "success"}.get(stage, stage)
    runtime.update("buy", status, STEPS.get(step, step or "买入阶段完成"),
                   item=item, next_item=next_item, done=done, total=total,
                   next_run_at=None)


@contextmanager
def task_scope(key):
    token = CURRENT_TASK.set(key)
    runtime.update(key, "running", "开始执行", execution_started_at=time.time(), last_error="")
    try:
        yield
    except Exception as exc:
        runtime.update(key, "failed", f"执行失败：{type(exc).__name__}", last_error=type(exc).__name__)
        raise
    else:
        with runtime.lock:
            status = runtime.tasks[key]["status"]
            result = runtime.tasks[key]["detail"]
        if status == "running":
            runtime.update(key, "success", "本轮执行结束", last_result=result)
    finally:
        CURRENT_TASK.reset(token)


def note(detail, status=None, **fields):
    key = CURRENT_TASK.get()
    if key:
        runtime.update(key, status, detail, **fields)


def worker_sleep(seconds, sleep_fn=time.sleep):
    """Track sleep separately from execution; keep errors/results visible."""
    key = CURRENT_TASK.get()
    if key:
        with runtime.lock:
            task = runtime.tasks.get(key, {})
            status = task.get("status")
            detail = task.get("detail", "")
        runtime.update(key, status if status in {"blocked", "disabled", "failed"} else "waiting",
                       detail if status in {"blocked", "disabled", "failed"} else "等待下次执行",
                       next_run_at=time.time() + seconds, last_result=detail,
                       finished_at=time.time())
    sleep_fn(seconds)
    if key:
        runtime.update(key, "running", "检查执行条件")


def holding_progress(purchases, inventory):
    inventory_by_id = {str(i.get("assetid")): i for i in inventory}
    groups = {k: {"count": 0, "cost": 0, "oldest_at": None} for k in
              ("receipt", "cooldown", "ready", "confirmation", "listed", "sold", "unknown", "error")}
    for p in purchases:
        item = inventory_by_id.get(str(p.get("assetid")))
        if float(p.get("sale_price") or 0) > 0:
            key = "sold"
        elif p.get("listing_status") == "error":
            key = "error"
        elif p.get("listing_status") == "pending_confirmation":
            key = "confirmation"
        elif p.get("listing"):
            key = "listed"
        elif p.get("pending_receipt"):
            key = "receipt"
        elif item and item.get("can_sell"):
            key = "ready"
        elif item and item.get("cooldown_at") and item["cooldown_at"] > time.time():
            key = "cooldown"
        else:
            key = "unknown"
        g = groups[key]
        g["count"] += 1
        g["cost"] = round(g["cost"] + float(p.get("price") or 0), 2)
        at = p.get("at")
        if at and (g["oldest_at"] is None or at < g["oldest_at"]):
            g["oldest_at"] = at
    return groups
