import threading
import time
import uuid
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_loader import get_buff_credentials, load_app_config_validated
from app.config_schema import DEFAULTS, merge
from app.pipeline_context import PipelineContext
from app.pipeline_steps import (
    TARGET_REACHED,
    SKIP_NO_FAILED,
    SKIP_VERIFICATION_FAILED,
    filter_iflow_rows,
    lock_and_confirm_payment,
    pick_stable_item,
)
from app.services.iflow_client import fetch_iflow_rows
from app.services.analysis_client import StabilityAnalyzer
from app.services.buff_client import create_buff_client_from_config
from app.services.steam_client import SteamClient
from app.state import get_state, append_sale
from app.strategy_engine import apply_strategy_to_config
from buff.buyer import BuffAuthExpired, BuffVerificationRequired
from steamdt.models import SteamDTQueryParams
from utils.delay import jittered_sleep
from utils.money import USD_TO_CNY_DEFAULT, list_price_display_to_cents
from utils.network_check import get_network_checker
from utils.proxy_manager import get_proxy_manager
from datetime import datetime

from app.sell_pipeline import run_sell_phase_on_inventory_update

DEFAULT_RETRY_INTERVAL_SECONDS = 300
DEFAULT_START_TIME_HOUR = 8
DEFAULT_END_TIME_HOUR = 22
FAILED_GOODS_TTL_SECONDS = 1800


def _is_in_time_window(start_hour: int, end_hour: int) -> bool:
    hour = datetime.now().hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _fetch_and_filter_deals(ctx: PipelineContext, cfg: dict, retry_interval: int):
    ctx.set_status("running", "FETCHING_DEALS", progress_total=0, progress_done=0, progress_item="")
    ctx.log("正在拉取 SteamDT 数据…", "info", category="steamdt")
    try:
        rows = fetch_iflow_rows(cfg)
    except Exception as e:
        ctx.log(f"SteamDT 拉取失败: {type(e).__name__}: {e}，{retry_interval}秒后重试", "warn", category="steamdt")
        return None, True  # (rows, fetch_failed)
    if not rows:
        ctx.log(f"SteamDT 未返回任何数据，{retry_interval}秒后重试", "warn", category="steamdt")
        return None, False
    ctx.log(f"SteamDT 返回 {len(rows)} 条原始数据", "info", category="steamdt")
    if ctx.verbose:
        iflow_cfg = cfg.get("steamdt") or cfg.get("iflow", {})
        q = SteamDTQueryParams(
            page=int(iflow_cfg.get("page_num", 1)),
            page_size=int(iflow_cfg.get("page_size", 200)),
            min_sell_price=str(iflow_cfg.get("min_price", 2)),
            max_sell_price=int(iflow_cfg.get("max_price", 5000)),
            min_transaction_count=str(iflow_cfg.get("min_volume", 200)),
        )
        ctx.debug(f"[详细流程] SteamDT 请求参数: page={q.page} pageSize={q.page_size} minPrice={q.min_sell_price} maxPrice={q.max_sell_price} minTx={q.min_transaction_count}")
        ctx.debug(f"[详细流程] 原始数据共 {len(rows)} 条，SteamDT 返回顺序（前20条）:")
        for i, r in enumerate(rows[:20]):
            nm = (getattr(r, "name", None) or "")[:42]
            ctx.debug(f"  {i+1:2}. {nm} | sell={getattr(r, 'sell_ratio', '')} buy={getattr(r, 'buy_ratio', '')}")
    filtered = filter_iflow_rows(rows, cfg, log_fn=lambda msg, lvl="info": ctx.log(msg, lvl))
    ctx.log(f"筛选后剩余 {len(filtered)} 条", "info")
    if ctx.verbose and filtered:
        ctx.debug(f"[详细流程] 筛选后共 {len(filtered)} 条，顺序不变（前20条）:")
        for i, item in enumerate(filtered[:20]):
            nm = (item.get("name") or "")[:42]
            ctx.debug(f"  {i+1:2}. {nm} | 比例={item.get('ratio', '')} 最低价={item.get('min_price', '')}")
    return filtered, False


def _process_deals_for_target(
    ctx: PipelineContext,
    filtered: list,
    cfg: dict,
    target: float,
    current_acc: float,
    total_bought: int,
    steam_client,
    analyzer,
    buyer,
    failed_goods_ids: set,
    skipped_this_round: set,
    stability_failed_this_round: set,
):
    acc = current_acc
    bought = total_bought
    n_filtered = len(filtered)

    def buff_log(msg: str, level: str = "info") -> None:
        ctx.log(msg, level, category="buff")

    while acc < target:
        if ctx.is_stop_requested():
            ctx.log("用户请求停止", "warn", category="buff")
            ctx.set_status("stopped", "已停止")
            return acc, bought, True

        ctx.set_status("running", "CHECKING_STABILITY", progress_total=n_filtered, progress_done=0, progress_item="")
        chosen, new_stability_failed = pick_stable_item(
            filtered, cfg, steam_client, analyzer, ctx.is_stop_requested,
            log_fn=ctx.log,
            exclude_goods_ids=failed_goods_ids | skipped_this_round | stability_failed_this_round,
            buff_client=buyer,
        )
        stability_failed_this_round |= new_stability_failed

        if ctx.is_stop_requested():
            ctx.set_status("stopped", "已停止")
            return acc, bought, True
        if chosen is None:
            break
        if acc >= target:
            break

        ctx.log(f"购买本件: {chosen['name']} goods_id={chosen['goods_id']} 参考价={chosen.get('min_price')}", "info", category="buff")

        def on_entering_payment() -> None:
            ctx.set_status("running", "CHECKOUT_PENDING", progress_item=chosen.get("name", ""))

        paid = lock_and_confirm_payment(
            buyer, chosen, cfg, target, acc,
            ctx.state.set_pending_payment,
            ctx.state.wait_payment_confirm,
            ctx.state.confirm_payment,
            ctx.state.is_stop_requested,
            ctx.state.append_purchase,
            log_fn=buff_log,
            on_entering_payment=on_entering_payment,
        )

        if ctx.is_stop_requested():
            ctx.set_status("stopped", "已停止")
            return acc, bought, True

        if paid is TARGET_REACHED:
            ctx.log("累计已达/超过目标，结束购买", "info", category="buff")
            acc = target
            break
        if paid is SKIP_NO_FAILED:
            gid = chosen.get("goods_id")
            if gid is not None:
                skipped_this_round.add(gid)
            ctx.log("安全采购上限不足，跳过本件", "warn", category="buff")
            continue
        if paid is SKIP_VERIFICATION_FAILED:
            gid = chosen.get("goods_id")
            if gid is not None:
                failed_goods_ids.add(gid)
            ctx.log("二次验证未通过，跳过本件", "warn", category="buff")
            continue
        if paid is None:
            gid = chosen.get("goods_id")
            if gid is not None:
                failed_goods_ids.add(gid)
            ctx.log("锁单/确认未成功，跳过本件", "warn", category="buff")
            continue

        acc += paid
        acc = round(acc, 2)
        bought += 1
        ctx.set_status("running", "CHECKOUT_PENDING", progress_done=bought, progress_item=chosen.get("name", ""))
        ctx.log(f"已确认付款 本笔={paid:.2f} 累计={acc:.2f}/{target}", "info", category="buff")
        if acc >= target:
            break
        ctx.debug("下一件将重新按 SteamDT 顺序试稳定性")
        jittered_sleep(1.0, 0.0)

    return acc, bought, False


def _run_pipeline(config: dict) -> None:
    state = get_state()
    state.clear_stop()
    state.set_buff_auth_expired(False)
    state.set_buff_verification_required(False)
    cfg = apply_strategy_to_config(merge(DEFAULTS, config), "buy")
    pipeline_cfg = cfg.get("pipeline", {})
    verbose = bool(pipeline_cfg.get("verbose_debug", False))
    ctx = PipelineContext(state, str(uuid.uuid4())[:8], verbose=verbose)

    target = float(pipeline_cfg.get("target_balance", 100))
    exclude = pipeline_cfg.get("exclude_keywords", [])
    cred_buff = get_buff_credentials()
    cookies_buff = cred_buff.get("cookies", "")
    if not cookies_buff:
        ctx.log("未配置 Buff cookies", "error", category="config")
        ctx.set_status("error", "CONFIG_ERROR")
        return

    ctx.log("买入阶段启动", "info")
    max_discount = pipeline_cfg.get("max_discount")
    sort_by = (cfg.get("steamdt") or cfg.get("iflow") or {}).get("sort_by", "sell")
    sort_labels = {"sell": "最优寄售", "buy": "最优求购"}
    sort_desc = sort_labels.get(sort_by, sort_by)
    retry_interval = int(pipeline_cfg.get("retry_interval_seconds", DEFAULT_RETRY_INTERVAL_SECONDS))
    ctx.log(
        f"配置: 目标余额={target}, 排除关键词={exclude}, 最高折扣={max_discount}, "
        f"排序={sort_desc}({sort_by}), 无符合时{retry_interval}秒后重试",
        "info",
    )
    if ctx.verbose:
        ctx.debug("详细调试已开启")

    proxy_manager = get_proxy_manager()
    if proxy_manager.is_proxy_enabled():
        ctx.set_status("running", "PROXY_WARMUP")
        ctx.log("代理池已启用，预热将在后台启动，pipeline 同步开始运行...", "info")
        proxy_warmup_thread = threading.Thread(
            target=proxy_manager.warmup, daemon=True, name="proxy-warmup"
        )
        proxy_warmup_thread.start()
    else:
        ctx.debug("代理池未启用或策略为关闭，跳过预热")

    acc = 0.0
    total_bought = 0
    time_limit_enabled = bool(pipeline_cfg.get("start_time_limit_enabled", False))
    start_time_hour = max(0, min(23, int(pipeline_cfg.get("start_time_hour", DEFAULT_START_TIME_HOUR))))
    end_time_hour = max(0, min(23, int(pipeline_cfg.get("end_time_hour", DEFAULT_END_TIME_HOUR))))

    steam_client = SteamClient()
    analyzer = StabilityAnalyzer(usd_to_cny=USD_TO_CNY_DEFAULT)
    buyer = create_buff_client_from_config(cred_buff, cfg)
    failed_goods_ids_ttl: dict = {}

    while True:
        if ctx.is_stop_requested():
            ctx.log("用户请求停止", "warn")
            ctx.set_status("stopped", "已停止")
            return

        if time_limit_enabled and not _is_in_time_window(start_time_hour, end_time_hour):
            ctx.set_status("running", "TIME_LIMIT_WAIT", progress_item=f"Allowed window: {start_time_hour}:00-{end_time_hour}:00")
            ctx.log(f"启动时间限制: 当前不在 {start_time_hour}:00–{end_time_hour}:00 内，60 秒后重试", "info")
            if ctx.wait_retry(60):
                return
            continue

        try:
            filtered, fetch_failed = _fetch_and_filter_deals(ctx, cfg, retry_interval)
            net = get_network_checker()
            if fetch_failed:
                offline = net.report_failure(
                    log_fn=lambda msg, lvl: ctx.log(msg, lvl, category="network")
                )
                if offline:
                    ctx.set_status("running", "NETWORK_OFFLINE")
                    recovered = net.wait_until_online(
                        is_stop_fn=ctx.is_stop_requested,
                        log_fn=lambda msg, lvl: ctx.log(msg, lvl, category="network"),
                    )
                    if not recovered:
                        ctx.set_status("stopped", "已停止")
                        return
                    continue
            else:
                net.report_success()

            if ctx.is_stop_requested():
                ctx.set_status("stopped", "已停止")
                return
            if not filtered:
                if ctx.wait_retry(retry_interval):
                    return
                continue

            ctx.log("支付方式与 Buff 客户端已就绪", "info", category="buff")
            now_ts = time.time()
            expired_ids = [gid for gid, exp in failed_goods_ids_ttl.items() if now_ts >= exp]
            for gid in expired_ids:
                del failed_goods_ids_ttl[gid]
            if expired_ids:
                ctx.log(f"Unblocked {len(expired_ids)} expired failed goods_id", "info", category="pipeline")
            failed_goods_ids = set(failed_goods_ids_ttl.keys())

            acc, total_bought, stopped = _process_deals_for_target(
                ctx, filtered, cfg, target, acc, total_bought,
                steam_client, analyzer, buyer,
                failed_goods_ids,
                set(),
                set(),
            )
            if stopped:
                return

            expire_ts = time.time() + FAILED_GOODS_TTL_SECONDS
            for gid in failed_goods_ids:
                if gid not in failed_goods_ids_ttl:
                    failed_goods_ids_ttl[gid] = expire_ts

        except BuffAuthExpired:
            ctx.state.set_buff_auth_expired(True)
            ctx.log("Buff 登录已过期，请在界面重新登录", "error", category="buff")
            ctx.set_status("error", "BUFF_AUTH_EXPIRED")
            return
        except BuffVerificationRequired as e:
            reason = str(e) or "Buff 需要刷新页面或完成人机验证"
            ctx.state.set_buff_verification_required(True, reason)
            ctx.log(f"Buff 需要刷新页面状态或完成人机验证: {reason}", "error", category="buff")
            ctx.set_status("error", "BUFF_VERIFICATION_REQUIRED")
            return

        if acc >= target:
            break
        ctx.debug(f"本轮无满足条件饰品，等待 {retry_interval}s 重新拉取")
        if ctx.wait_retry(retry_interval):
            return

    ctx.set_status("running", "STEAM_COOLDOWN")
    ctx.log("买入阶段完成", "info")
    ctx.log(f"本次共成功购买 {total_bought} 单。Steam 交易冷却。", "info")
    ctx.set_status("idle", "")


_pipeline_thread = None
_pipeline_start_lock = threading.Lock()


def is_pipeline_running() -> bool:
    with _pipeline_start_lock:
        return _pipeline_thread is not None and _pipeline_thread.is_alive()


def _run_pipeline_guarded(config: dict) -> None:
    global _pipeline_thread
    try:
        _run_pipeline(config)
    finally:
        with _pipeline_start_lock:
            _pipeline_thread = None


def start_pipeline(config: dict) -> bool:
    global _pipeline_thread
    with _pipeline_start_lock:
        if _pipeline_thread is not None and _pipeline_thread.is_alive():
            return False
        t = threading.Thread(target=_run_pipeline_guarded, args=(config,), daemon=True, name="buy-pipeline")
        _pipeline_thread = t
        t.start()
        return True
