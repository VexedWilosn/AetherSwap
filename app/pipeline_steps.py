import re
import statistics
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.services.iflow_client import fetch_iflow_rows as _fetch_iflow_rows
from app.state import get_purchases, set_status
from app.strategy_engine import evaluate_strategy_runtime_modules, is_strategy_module_enabled
from app.services.steam_client import SteamClient
from app.services.analysis_client import StabilityAnalyzer
from app.services.buff_client import count_lowest_price_orders, first_order_at_price
from app.notify import send_pushplus, build_payment_notify_content, wait_email_command
from utils.delay import jittered_sleep
from buff.buyer import BuffAuthExpired, BuffVerificationRequired

STEAM_FEE_FACTOR = 1.15  # Steam take rate for calculating net proceeds

def _fetch_steam_sell_data(
    market_hash_name: str,
    config: dict,
    app_id: int = 730,
    *,
    return_error: bool = False,
):
    from app.config_loader import get_steam_credentials
    from steam.session import create_market_session
    from steam.market_orders import get_sell_orders_cny, compute_smart_list_price
    name = (market_hash_name or "").strip()
    if not name:
        reason = "Steam 市场名为空"
        return (None, reason) if return_error else None
    cred = get_steam_credentials()
    cookies = cred.get("cookies", "")
    steam_id = cred.get("steam_id", "")
    if not cookies or not steam_id:
        missing = []
        if not cookies:
            missing.append("Cookie")
        if not steam_id:
            missing.append("steam_id")
        reason = "Steam 凭据缺失: " + "、".join(missing)
        return (None, reason) if return_error else None
    try:
        session = create_market_session(cookies, steam_id)
        cfg = config.get("pipeline", {})
        wall_volume = int(cfg.get("sell_price_wall_volume", 20))
        max_ignore = int(cfg.get("sell_price_max_ignore_volume", 4))
        usd_to_cny_rate = float(cfg.get("usd_to_cny", 7.2))
        orders_result = get_sell_orders_cny(
            session,
            name,
            app_id=app_id,
            request_delay=1.0,
            return_error=True,
            usd_to_cny_rate=usd_to_cny_rate,
        )
        if isinstance(orders_result, tuple) and len(orders_result) == 2:
            result, reason = orders_result
        else:
            result, reason = orders_result, None
        if not result:
            return (None, reason or "Steam 卖单接口返回空数据") if return_error else None
        if not result.get("sell_orders"):
            return (None, reason or "Steam 返回空卖单图") if return_error else None
        orders = result["sell_orders"]
        price, _ = compute_smart_list_price(
            orders,
            wall_volume_threshold=wall_volume,
            max_ignore_volume=max_ignore,
            min_step=0,
            offset=0,
        )
        if price is None:
            reason = "Steam 卖单已获取，但无法计算智能参考价"
            return (None, reason) if return_error else None
        data = {"sell_orders": orders, "smart_price": price}
        return (data, None) if return_error else data
    except Exception as e:
        detail = str(e).strip()
        if len(detail) > 120:
            detail = detail[:117] + "..."
        reason = f"Steam 卖单获取异常: {type(e).__name__}" + (f" - {detail}" if detail else "")
        return (None, reason) if return_error else None

def _check_buff_price(
    item,
    gid,
    plan_price,
    buff_client,
    config: dict,
    log_fn,
):
    # 拉取 Buff 实时最低价，和 iflow 价格对比确认没有跳动
    # 成功返回 (True, 最新价格)，失败返回 (False, None)
    # 注意：BuffAuthExpired 要直接往上抛，不能在这里吃掉
    buff_cfg = config.get("buff", {})
    game_buff = buff_cfg.get("game", "csgo")
    tolerance = float(buff_cfg.get("price_tolerance", 0.5))
    orders = buff_client.get_sell_orders(gid, game_buff)
    if not orders:
        if log_fn:
            reason = "接口返回 None，可能是网络/鉴权/风控问题" if orders is None else "Buff 当前无在售卖单"
            log_fn(f"[Buff]   → 预检未通过: 无法获取 Buff 卖单信息：{reason} (goods_id={gid})", "warn")
        return False, None
    lowest_price, _ = count_lowest_price_orders(orders)
    if lowest_price <= 0:
        if log_fn:
            log_fn(f"[Buff]   → 预检未通过: Buff 最低价无效 (goods_id={gid})", "warn")
        return False, None
    if plan_price is not None and lowest_price - plan_price > tolerance:
        if log_fn:
            log_fn(f"[Buff]   → 预检未通过: Buff 最低价 {lowest_price:.2f} 较 iflow 参考价 {plan_price:.2f} 超出容忍 (差{lowest_price - plan_price:.2f})", "warn")
        return False, None
    item["_buff_lowest_price"] = lowest_price
    item["_buff_sell_orders"] = orders
    return True, lowest_price

def _adjust_ref_price_for_daily_high(
    market_hash_name: str,
    current_ref_price: float,
    config: dict,
    log_fn: Optional[Callable[[str, str], None]],
    app_id: int = 730,
) -> float:
    from utils.time import parse_steam_history_date
    from utils.money import apply_currency, USD_TO_CNY_DEFAULT
    name = (market_hash_name or "").strip()
    if not name or current_ref_price <= 0:
        return current_ref_price
    steam_client = SteamClient()
    raw = steam_client.fetch_history(name, app_id=app_id, return_currency=True)
    if not raw:
        return current_ref_price
    history = raw.get("history") if isinstance(raw, dict) else raw
    if not isinstance(history, list) or not history:
        return current_ref_price
    currency = raw.get("currency") if isinstance(raw, dict) else None
    usd_cny = float(config.get("pipeline", {}).get("usd_to_cny", USD_TO_CNY_DEFAULT))
    cutoff = datetime.now() - timedelta(hours=24)
    prices_cny: List[float] = []
    for item in history:
        if len(item) < 2:
            continue
        dt = parse_steam_history_date(str(item[0]))
        if dt is None or dt < cutoff:
            continue
        try:
            p = float(item[1])
            converted, _ = apply_currency([p], currency, usd_cny)
            prices_cny.append(converted[0])
        except (ValueError, TypeError):
            continue
    if len(prices_cny) < 2:
        return current_ref_price
    sorted_prices = sorted(prices_cny)
    if len(sorted_prices) >= 3:
        trimmed_high = sorted_prices[-2]
        trimmed_low = sorted_prices[1]
    else:
        trimmed_high = sorted_prices[-1]
        trimmed_low = sorted_prices[0]
    daily_avg = statistics.mean(prices_cny)
    if trimmed_high <= trimmed_low:
        return current_ref_price
    daily_position = (current_ref_price - trimmed_low) / (trimmed_high - trimmed_low)
    if daily_position <= 0.6:
        return current_ref_price
    conservative_price = (current_ref_price + daily_avg) / 2
    if log_fn:
        log_fn(f"[Buff]   → 检测到 Steam 价格处于日内高位 (位置: {daily_position:.2f})，存在虚高风险", "warn")
        log_fn(f"[Buff]   → 降级参考价: {current_ref_price:.2f} -> {conservative_price:.2f} (当前价与均价折中)", "info")
    return conservative_price
def _compute_sell_pressure_from_orders(
    sell_orders: list,
    daily_volume: int,
    n_orders: int = 5,
) -> Optional[float]:
    if daily_volume <= 0 or not sell_orders:
        return None
    target_orders = sorted(sell_orders, key=lambda x: x[0])[:n_orders]
    total_vol = sum(vol for _, vol in target_orders)
    base_pressure = total_vol / daily_volume
    if len(target_orders) < 2:
        return base_pressure
    current_vol = 0
    wall_vol = 0
    for i in range(len(target_orders) - 1):
        p_curr, c_curr = target_orders[i]
        p_next, _ = target_orders[i + 1]
        current_vol += c_curr
        if p_curr < 5.0:
            gap_abs, gap_rel = 0.10, 0.08
        elif p_curr < 20.0:
            gap_abs, gap_rel = 0.30, 0.05
        elif p_curr < 100.0:
            gap_abs, gap_rel = 1.0, 0.03
        elif p_curr < 500.0:
            gap_abs, gap_rel = 5.0, 0.02
        else:
            gap_abs, gap_rel = 10.0, 0.015
        threshold = max(gap_abs, p_curr * gap_rel)
        if (p_next - p_curr) > threshold:
            wall_vol = current_vol
            break
    if wall_vol > 0 and (wall_vol <= max(3, daily_volume * 0.15)):
        return base_pressure * 0.4
    return base_pressure


def _compute_sell_pressure(
    market_hash_name: str,
    daily_volume: int,
    config: dict,
    n_orders: int = 5,
    app_id: int = 730,
) -> Optional[float]:
    data = _fetch_steam_sell_data(market_hash_name, config, app_id)
    if not data or not data.get("sell_orders"):
        return None
    return _compute_sell_pressure_from_orders(data["sell_orders"], daily_volume, n_orders)
def _fetch_smart_market_price(market_hash_name: str, config: dict, app_id: int = 730) -> Optional[float]:
    data = _fetch_steam_sell_data(market_hash_name, config, app_id)
    return data.get("smart_price") if data else None


TARGET_REACHED = object()
SKIP_NO_FAILED = object()
SKIP_VERIFICATION_FAILED = object()


def _parse_threshold(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _goods_id_from_buff_url(url: str) -> int:
    if not url or "buff.163.com" not in url:
        return 0
    m = re.search(r"/goods/(\d+)", url)
    return int(m.group(1)) if m else 0


_RATIO_ATTR = {"sell": "sell_ratio", "buy": "buy_ratio"}


def _log_stability_rejection(
    report: dict,
    stability_cfg: dict,
    smart_price,
    log_fn,
) -> None:
    # 打印一行可读的拒绝原因，方便排查为什么这件东西被跳过
    if not log_fn:
        return
    msg = report.get("msg", "指标验证不通过")
    st = report.get("status", "")
    cv = report.get("cv", 0)
    r2 = report.get("r_squared", 0)
    avg = report.get("avg", 0)
    slope = report.get("slope", 0)
    pp = report.get("price_percentile")
    pp_str = f" 分位={pp:.2f}" if pp is not None else ""
    smart_str = f" 智能选价={smart_price:.2f}" if smart_price is not None else ""
    ma_str = f" EMA7={report.get('ma7',0):.2f} EMA30={report.get('ma30',0):.2f}"
    bb_upper = report.get("bb_upper")
    bb_str = f" BB+={bb_upper:.2f}" if bb_upper is not None else ""
    log_fn(f"[稳定性]   → 拒绝: {msg} status={st} cv={cv:.3f} R2={r2:.3f} 均价={avg:.2f} slope={slope:.4f}{ma_str}{bb_str}{smart_str}{pp_str}", "warn")


def filter_iflow_rows(
    rows: List[Any],
    config: dict,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> List[Dict[str, Any]]:
    pipeline_cfg = config.get("pipeline", {})
    iflow_cfg = config.get("steamdt") or config.get("iflow", {})
    exclude = pipeline_cfg.get("exclude_keywords", [])
    top_n = int(pipeline_cfg.get("iflow_top_n", 0) or 0)
    if top_n > 0:
        rows = rows[:top_n]
    sort_by = (iflow_cfg.get("sort_by") or "sell").strip()
    ratio_attr = _RATIO_ATTR.get(sort_by, "sell_ratio")
    steam_client = SteamClient()
    filtered = []
    skipped_keyword = 0
    skipped_price = 0
    skipped_no_buff = 0
    for r in rows:
        name = (getattr(r, "name", None) or "").lower()
        name_cn = (getattr(r, "name_cn", None) or "").lower()
        if any(kw in name or kw in name_cn for kw in exclude):
            skipped_keyword += 1
            continue
        try:
            price = float(getattr(r, "min_price", 0))
        except (ValueError, TypeError):
            skipped_price += 1
            continue
        if price <= 0:
            skipped_price += 1
            continue
        try:
            ratio_val = float(getattr(r, ratio_attr, 0) or 0)
        except (ValueError, TypeError):
            ratio_val = 0
        gid = _goods_id_from_buff_url(getattr(r, "platform", "") or "")
        if gid <= 0:
            skipped_no_buff += 1
            continue
        steam_link = getattr(r, "steam_link", None) or ""
        steam_market_name = steam_client.market_hash_name_from_listing_url(
            steam_link
        ) or name
        try:
            vol = int(getattr(r, "volume", "0") or 0)
        except (ValueError, TypeError):
            vol = 0
        filtered.append({
            "name": getattr(r, "name", ""),
            "min_price": price,
            "goods_id": gid,
            "platform": getattr(r, "platform", ""),
            "steam_market_name": steam_market_name,
            "steam_link": steam_link,
            "ratio": ratio_val,
            "daily_volume": vol,
        })
    if log_fn:
        parts = [f"排除关键词={skipped_keyword}", f"价格无效={skipped_price}"]
        if top_n > 0:
            parts.append(f"取前{top_n}条")
        parts.extend([f"非Buff链接={skipped_no_buff}", f"→ 通过 {len(filtered)} 条"])
        log_fn(f"[筛选] {' '.join(parts)}", "info")
    return filtered

def _check_sell_pressure_precheck(
    item,
    steam_sell_data,
    sell_pressure_threshold,
    pipeline_cfg,
    log_fn,
) -> bool:
    # 检查卖压是否过高
    if sell_pressure_threshold is not None and sell_pressure_threshold > 0:
        daily_vol = int(item.get("daily_volume", 0) or 0)
        sell_orders = steam_sell_data.get("sell_orders")
        n_sell_orders = int(pipeline_cfg.get("sell_pressure_orders_n", 5) or 5)
        if daily_vol > 0 and sell_orders:
            pressure = _compute_sell_pressure_from_orders(sell_orders, daily_vol, n_sell_orders)
            if pressure is not None and pressure > sell_pressure_threshold:
                if log_fn:
                    log_fn(f"[稳定性]   → 预检未通过: 卖压过高 前{n_sell_orders}档总量/日销={pressure:.2f} 阈值={sell_pressure_threshold}", "warn")
                return False
        elif daily_vol <= 0 and log_fn:
            log_fn("[稳定性]   → 卖压检查: 日销量为0，跳过", "info")
    return True

def _check_max_discount_precheck(
    item,
    gid,
    smart_price,
    est_ratio,
    ref_price_est,
    plan_price,
    max_discount,
    log_fn,
) -> bool:
    # 检查买入价占Steam参考价的比例是否低于max_discount，超了就不够利润
    if max_discount is not None:
        max_discount_float = float(max_discount)
        if smart_price is None or smart_price <= 0:
            if log_fn:
                log_fn("[稳定性]   → 预检未通过: Steam 卖单已返回，但智能参考价为空或无效", "warn")
            return False
        if est_ratio is None or est_ratio <= 0:
            if log_fn:
                plan_str = f"{plan_price:.2f}" if isinstance(plan_price, (int, float)) else "无效"
                ref_str = f"{ref_price_est:.2f}" if isinstance(ref_price_est, (int, float)) else "无效"
                log_fn(f"[稳定性]   → 预检未通过: 无法计算预估比例 (Buff最低价={plan_str}, Steam参考价={ref_str})", "warn")
            return False
        if est_ratio >= max_discount_float:
            if log_fn:
                log_fn(f"[稳定性]   → 预检未通过: (Buff最低价/Steam参考价)×1.15={est_ratio:.4f} 需<{max_discount_float} (Steam参考价={ref_price_est:.2f})", "warn")
            return False
    return True


def _build_buy_strategy_outputs(
    item: Dict[str, Any],
    steam_sell_data: Optional[Dict[str, Any]] = None,
    smart_price: Optional[float] = None,
    est_ratio: Optional[float] = None,
    ref_price_est: Optional[float] = None,
    report: Optional[Dict[str, Any]] = None,
    pipeline_cfg: Optional[dict] = None,
) -> Dict[str, Any]:
    outputs: Dict[str, Any] = {}
    if steam_sell_data:
        orders = steam_sell_data.get("sell_orders") or []
        daily_volume = int(item.get("daily_volume", 0) or 0)
        sell_pressure = None
        if daily_volume > 0 and orders:
            try:
                n = int((pipeline_cfg or {}).get("sell_pressure_orders_n", 5) or 5)
                top_orders = orders[:max(1, n)]
                volume = sum(int(o.get("quantity", 0) or 0) for o in top_orders if isinstance(o, dict))
                sell_pressure = volume / daily_volume
            except Exception:
                sell_pressure = None
        outputs["buy.steam_sell_depth"] = {
            "smart_price": smart_price if smart_price is not None else steam_sell_data.get("smart_price"),
            "sell_orders_count": len(orders),
            "sell_pressure": sell_pressure,
            "reference_price": ref_price_est,
            "estimated_ratio": est_ratio,
        }
    if report:
        outputs["guard.history_data_window"] = {
            key: report.get(key)
            for key in (
                "status", "avg", "cv", "r_squared", "slope", "price_percentile",
                "ma7", "ma30", "is_stable",
            )
        }
    if est_ratio is not None:
        outputs["guard.max_discount"] = {
            "estimated_ratio": est_ratio,
            "limit": (pipeline_cfg or {}).get("max_discount"),
        }
    return outputs


def _passes_custom_buy_modules(
    item: Dict[str, Any],
    config: dict,
    *,
    steam_sell_data: Optional[Dict[str, Any]] = None,
    smart_price: Optional[float] = None,
    est_ratio: Optional[float] = None,
    ref_price_est: Optional[float] = None,
    report: Optional[Dict[str, Any]] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> bool:
    outputs = _build_buy_strategy_outputs(
        item,
        steam_sell_data=steam_sell_data,
        smart_price=smart_price,
        est_ratio=est_ratio,
        ref_price_est=ref_price_est,
        report=report,
        pipeline_cfg=config.get("pipeline") or {},
    )
    context = {
        "item": item,
        "config": config,
    }
    results, blocking = evaluate_strategy_runtime_modules(
        config,
        "buy",
        "buy.candidate_guard",
        context=context,
        outputs=outputs,
    )
    if log_fn:
        for result in results:
            level = "warn" if result.get("status") in {"reject", "error"} else "info"
            log_fn(f"[策略模块] {result.get('module_name')}: {result.get('reason')} ({result.get('status')})", level)
    return blocking is None


def pick_stable_item(
    filtered: List[Dict[str, Any]],
    config: dict,
    steam_client: SteamClient,
    analyzer: StabilityAnalyzer,
    is_stop_requested: callable,
    log_fn: Optional[Callable[[str, str], None]] = None,
    exclude_goods_ids: Optional[set] = None,
    buff_client: Optional[Any] = None,
) -> Tuple[Optional[Dict[str, Any]], Set[int]]:
    # 遍历候选饰品，返回第一个通过所有检测的
    # 检测顺序: Buff价格预检 → Steam卖单 → 卖压 → 最高折扣 → 历史稳定性
    stability_cfg = config.get("stability", {})
    stability_days = int(stability_cfg.get("days", 30))
    cv_threshold = float(stability_cfg.get("cv_threshold", 0.05))
    r2_threshold = float(stability_cfg.get("r2_threshold", 0.6))
    min_daily_trades = float(stability_cfg.get("min_daily_trades", 5))
    excluded = exclude_goods_ids or set()
    stability_failed: Set[int] = set()
    n = len(filtered)
    request_interval = float(stability_cfg.get("request_interval_seconds", 2.5))
    failure_delay = max(0, float(stability_cfg.get("request_failure_delay_seconds", 5) or 5))
    legacy_history_enabled = is_strategy_module_enabled(config, "buy", "guard.history_stability", default=False)
    history_data_enabled = legacy_history_enabled or is_strategy_module_enabled(config, "buy", "guard.history_data_window")
    volatility_enabled = legacy_history_enabled or is_strategy_module_enabled(config, "buy", "guard.volatility_cv")
    trend_quality_enabled = legacy_history_enabled or is_strategy_module_enabled(config, "buy", "guard.trend_quality")
    price_position_enabled = legacy_history_enabled or is_strategy_module_enabled(config, "buy", "guard.price_position")
    history_analysis_enabled = any((
        legacy_history_enabled,
        history_data_enabled,
        volatility_enabled,
        trend_quality_enabled,
        price_position_enabled,
    ))
    for i, item in enumerate(filtered):
        if is_stop_requested():
            return None, stability_failed
        gid = item.get("goods_id")
        if gid is not None and gid in excluded:
            continue
        if i > 0 and request_interval > 0:
            jittered_sleep(request_interval)
        name = item.get("name", "")
        market_hash_name = item.get("steam_market_name") or name
        # 带饰品名称前缀的日志包装，方便追踪每条日志对应哪个饰品
        _short = (name[:30] + "…") if len(name) > 30 else name
        item_log = (lambda msg, level, _n=_short: log_fn(f"[{_n}] {msg}", level)) if log_fn else None
        next_name = filtered[i + 1].get("name", "") if i + 1 < n else ""
        set_status("running", step="STABILITY_CHECK", progress_total=n, progress_done=i, progress_item=f"({i+1}/{n}) {name}", next_progress_item=f"({i+2}/{n}) {next_name}" if next_name else "")
        pipeline_cfg = config.get("pipeline", {})
        verbose_detail = bool(pipeline_cfg.get("verbose_debug", False))
        if item_log and verbose_detail:
            item_log(f"[稳定性] 开始预检 ({i+1}/{n}) Buff参考价={item.get('min_price')} 比例={item.get('ratio')}", "debug")
        max_discount = pipeline_cfg.get("max_discount") if is_strategy_module_enabled(config, "buy", "guard.max_discount") else None
        sell_pressure_threshold = _parse_threshold(pipeline_cfg.get("sell_pressure_threshold")) if is_strategy_module_enabled(config, "buy", "guard.sell_pressure") else None
        steam_depth_enabled = is_strategy_module_enabled(config, "buy", "buy.steam_sell_depth")
        need_steam = (
            steam_depth_enabled
            and (
                max_discount is not None
                or (sell_pressure_threshold is not None and sell_pressure_threshold > 0 and int(item.get("daily_volume", 0) or 0) > 0)
            )
        )
        steam_sell_data: Optional[Dict[str, Any]] = None
        smart_price: Optional[float] = None
        est_ratio: Optional[float] = None
        ref_price_est: Optional[float] = None

        if need_steam:
            plan_price = item.get("min_price")

            # 1. Buff 价格预检
            if buff_client and is_strategy_module_enabled(config, "buy", "buy.buff_realtime_price"):
                if item_log and verbose_detail:
                    item_log("[稳定性] 检查 Buff 实时卖单…", "debug")
                buff_ok, plan_price = _check_buff_price(
                    item, gid, plan_price, buff_client, config, item_log
                )
                if not buff_ok:
                    if gid:
                        stability_failed.add(gid)
                    continue

            # 2. 拉取 Steam 挂单数据
            if item_log and verbose_detail:
                item_log("[稳定性] 拉取 Steam 卖单深度…", "debug")
            steam_sell_data, steam_error = _fetch_steam_sell_data(
                market_hash_name, config, app_id=730, return_error=True
            )
            if not steam_sell_data:
                if item_log:
                    item_log(f"[稳定性] 预检未通过: 无法获取 Steam 卖单信息：{steam_error or '未知原因'}", "warn")
                if gid:
                    stability_failed.add(gid)
                if failure_delay > 0:
                    jittered_sleep(failure_delay)
                continue
            if item_log and verbose_detail:
                orders_count = len(steam_sell_data.get("sell_orders") or [])
                smart_dbg = steam_sell_data.get("smart_price")
                smart_str = f"{smart_dbg:.2f}" if isinstance(smart_dbg, (int, float)) else "无"
                item_log(f"[稳定性] Steam 卖单获取成功: {orders_count} 档 智能参考价={smart_str}", "debug")

            # 3. 卖压检测
            if not _check_sell_pressure_precheck(
                item, steam_sell_data, sell_pressure_threshold, pipeline_cfg, item_log
            ):
                if gid:
                    stability_failed.add(gid)
                if failure_delay > 0:
                    jittered_sleep(failure_delay)
                continue

            # 4. 计算智能价和预估比例
            smart_price = steam_sell_data.get("smart_price")
            if smart_price is not None and smart_price > 0 and plan_price and plan_price > 0:
                ref_price_est = _adjust_ref_price_for_daily_high(
                    market_hash_name, smart_price, config, log_fn, app_id=730
                )
                est_ratio = (plan_price / ref_price_est) * STEAM_FEE_FACTOR

            # 5. 最高折扣检测
            if not _check_max_discount_precheck(
                item, gid, smart_price, est_ratio, ref_price_est, plan_price, max_discount, item_log
            ):
                if gid:
                    stability_failed.add(gid)
                if failure_delay > 0:
                    jittered_sleep(failure_delay)
                continue

        if not history_analysis_enabled:
            if smart_price is None and not need_steam:
                steam_sell_data = _fetch_steam_sell_data(market_hash_name, config, app_id=730)
                smart_price = steam_sell_data.get("smart_price") if steam_sell_data else None
            item["_steam_sell_data"] = steam_sell_data
            if not _passes_custom_buy_modules(
                item,
                config,
                steam_sell_data=steam_sell_data,
                smart_price=smart_price,
                est_ratio=est_ratio,
                ref_price_est=ref_price_est,
                log_fn=item_log,
            ):
                if gid:
                    stability_failed.add(gid)
                continue
            if item_log:
                item_log("[稳定性] 历史稳定性模块未启用，跳过历史分析，选定本件", "info")
            return item, stability_failed

        # 6. 拉历史K线 + 稳定性分析
        if item_log:
            item_log("[稳定性] 拉取历史价格…", "info")
        raw = steam_client.fetch_history(market_hash_name, return_currency=True)
        if raw and isinstance(raw, dict):
            history = raw.get("history")
            currency = raw.get("currency")
        else:
            history = raw if isinstance(raw, list) else None
            currency = None
        if not history:
            if gid:
                stability_failed.add(gid)
            if item_log:
                item_log("[稳定性] 无历史数据或请求失败，试下一个", "warn")
            if failure_delay > 0:
                jittered_sleep(failure_delay)
            continue

        # 利润特别大时适当放宽价格分位限制
        dyn_price_percentile_ceil = float(stability_cfg.get("price_percentile_ceil", 0.8)) if price_position_enabled else 999.0
        if est_ratio is not None and est_ratio > 0 and max_discount is not None:
            max_discount_float = float(max_discount)
            huge_offset = float(pipeline_cfg.get("huge_profit_offset", 0.05))
            huge_ratio = max_discount_float - huge_offset
            high_ratio = max_discount_float - (huge_offset / 2.0)
            if est_ratio < huge_ratio:
                dyn_price_percentile_ceil = 0.88
                if item_log:
                    item_log(f"[稳定性] 检测到巨额预期利润 (比例={est_ratio:.4f} < {huge_ratio:.4f})，放宽价格分位点限制至 {dyn_price_percentile_ceil}", "info")
            elif est_ratio < high_ratio:
                dyn_price_percentile_ceil = max(dyn_price_percentile_ceil, 0.85)
                if item_log:
                    item_log(f"[稳定性] 检测到极高预期利润 (比例={est_ratio:.4f} < {high_ratio:.4f})，放宽价格分位点限制至 {dyn_price_percentile_ceil}", "info")

        report = analyzer.analyze(
            history,
            days=stability_days,
            currency=currency,
            cv_threshold=cv_threshold if volatility_enabled else 999.0,
            r2_threshold=r2_threshold if trend_quality_enabled else 2.0,
            min_daily_trades=min_daily_trades if history_data_enabled else 0,
            current_price=smart_price,
            price_percentile_ceil=dyn_price_percentile_ceil,
            r2_rising_threshold=float(stability_cfg.get("r2_rising_threshold", 0.8)) if trend_quality_enabled else -1.0,
            slope_pct_ceil=float(stability_cfg.get("slope_pct_ceil", 0.01)) if trend_quality_enabled else 999.0,
            ma_deviation_ceil=float(stability_cfg.get("ma_deviation_ceil", 1.1)) if price_position_enabled else 999.0,
            last_price_ma30_ceil=float(stability_cfg.get("last_price_ma30_ceil", 1.05)) if price_position_enabled else 999.0,
            slope_stable_floor=float(stability_cfg.get("slope_stable_floor", -0.005)) if trend_quality_enabled else -999.0,
            price_percentile_ceil_rising=float(stability_cfg.get("price_percentile_ceil_rising", 0.5)) if price_position_enabled else 999.0,
            use_vwap=bool(stability_cfg.get("use_vwap", True)),
        )
        if smart_price is not None and not report.get("valid"):
            if gid:
                stability_failed.add(gid)
            if item_log:
                item_log(f"[稳定性] 分析异常: {report.get('msg', '无效')}，试下一个", "warn")
            if failure_delay > 0:
                jittered_sleep(failure_delay)
            continue

        if not report.get("is_stable"):
            _log_stability_rejection(report, stability_cfg, smart_price, item_log)
            if gid:
                stability_failed.add(gid)
            if failure_delay > 0:
                jittered_sleep(failure_delay)
            continue
        if smart_price is None and not need_steam:
            steam_sell_data = _fetch_steam_sell_data(market_hash_name, config, app_id=730)
            smart_price = steam_sell_data.get("smart_price") if steam_sell_data else None
        item["_steam_sell_data"] = steam_sell_data
        if not _passes_custom_buy_modules(
            item,
            config,
            steam_sell_data=steam_sell_data,
            smart_price=smart_price,
            est_ratio=est_ratio,
            ref_price_est=ref_price_est,
            report=report,
            log_fn=item_log,
        ):
            if gid:
                stability_failed.add(gid)
            if failure_delay > 0:
                jittered_sleep(failure_delay)
            continue
        if item_log:
            st = report.get("status", "")
            sl = report.get("slope", 0)
            r2 = report.get("r_squared", 0)
            pp = report.get("price_percentile")
            pp_str = f" 分位={pp:.2f}" if pp is not None else ""
            smart_str = f" 智能选价={smart_price:.2f}" if smart_price is not None else ""
            ma_str = f" EMA7={report.get('ma7',0):.2f} EMA30={report.get('ma30',0):.2f}"
            bb_upper = report.get("bb_upper")
            bb_str = f" BB+={bb_upper:.2f}" if bb_upper is not None else ""
            item_log(f"[稳定性] ✓ 通过 status={st} cv={report.get('cv',0):.3f} R2={r2:.3f} 均价={report.get('avg',0):.2f} slope={sl:.4f}{ma_str}{bb_str}{smart_str}{pp_str}，选定本件", "info")
        return item, stability_failed
    return None, stability_failed
def _do_payment_notify_and_wait(
    item: Dict[str, Any],
    config: dict,
    unit_price: float,
    num: int,
    pay_url: str,
    pay_type: str,
    order_id: str,
    acc: float,
    set_pending_payment: callable,
    wait_payment_confirm: callable,
    confirm_payment: callable,
    is_stop_requested: callable,
    log_fn: Optional[Callable[[str, str], None]],
    on_entering_payment: Optional[Callable[[], None]] = None,
) -> bool:
    """Handle notification and wait for user payment confirmation.
    Returns True if user confirmed, False on cancel/timeout/stop.
    """
    name = item.get("name", "")
    set_pending_payment({
        "pay_url": pay_url,
        "pay_type": pay_type,
        "name": name,
        "order_id": order_id,
    })
    if on_entering_payment:
        on_entering_payment()
    notify_cfg = config.get("notify") or {}
    push_token = (notify_cfg.get("pushplus_token") or "").strip()
    if push_token:
        sell_ratio = None
        value_ratio = item.get("value_ratio")
        try:
            rv = item.get("ratio")
            if rv is not None:
                if isinstance(rv, str):
                    rv = rv.strip().replace('%', '')
                sell_ratio = float(rv)
        except (TypeError, ValueError):
            pass
        mhn = (item.get("steam_market_name") or item.get("name") or "").strip()
        sl = item.get("steam_link")
        content = build_payment_notify_content(
            name, unit_price, pay_url, pay_type, acc,
            sell_ratio=sell_ratio, num=num, value_ratio=value_ratio,
            steam_market_hash_name=mhn, steam_link=sl
        )
        try:
            if send_pushplus(push_token, "Buff 待付款", content):
                if log_fn:
                    log_fn("[Buff]   → PushPlus 推送已发送", "info")
            else:
                if log_fn:
                    log_fn("[Buff]   → PushPlus 推送发送失败 (返回False)", "warn")
        except Exception as e:
            if log_fn:
                log_fn(f"[Buff]   → PushPlus 推送发送异常: {e}", "warn")
    email_user = (notify_cfg.get("email_user") or "").strip()
    email_pass = (notify_cfg.get("email_pass") or "").strip()
    timeout_sec = int(notify_cfg.get("email_timeout_seconds", 300))
    if email_user and email_pass:
        def _email_waiter() -> None:
            res = wait_email_command(config, timeout_seconds=timeout_sec, is_stop_requested=is_stop_requested, log_fn=log_fn)
            confirm_payment(res == "success")
        t = threading.Thread(target=_email_waiter, daemon=True)
        t.start()
        ok = wait_payment_confirm()
    else:
        ok = wait_payment_confirm(timeout_seconds=timeout_sec)
    set_pending_payment(None)
    if log_fn:
        log_fn(f"[Buff]   → 用户确认={'成功' if ok else '取消/失败'}", "info")
    return ok
def _do_batch_wait_finalize_and_append(
    buff_client: Any,
    item: Dict[str, Any],
    config: dict,
    unit_price: float,
    num: int,
    goods_id: int,
    batch_id: str,
    game_buff: str,
    pay_url: str,
    acc: float,
    set_pending_payment: callable,
    wait_payment_confirm: callable,
    confirm_payment: callable,
    is_stop_requested: callable,
    append_purchase: callable,
    log_fn: Optional[Callable[[str, str], None]],
    market_price: Optional[float] = None,
    on_entering_payment: Optional[Callable[[], None]] = None,
) -> Optional[float]:
    ok = _do_payment_notify_and_wait(
        item, config, unit_price, num, pay_url, "wechat", batch_id, acc,
        set_pending_payment, wait_payment_confirm, confirm_payment,
        is_stop_requested, log_fn, on_entering_payment,
    )
    if is_stop_requested() or not ok:
        return None
    if log_fn:
        log_fn("[Buff]   → 正在扫描市场匹配卖家并核销…", "info")
    matched = buff_client.batch_buy_find_and_finalize(
        goods_id, game_buff, unit_price, num, batch_id
    )
    if not matched:
        if log_fn:
            log_fn("[Buff]   → 未找到符合价格的商品，冻结资金将自动退回", "warn")
        return None
    if log_fn:
        log_fn(f"[Buff]   → 核销成功 {len(matched)} 件", "info")
    if market_price is None:
        mhn = (item.get("steam_market_name") or item.get("name") or "").strip()
        market_price = _fetch_smart_market_price(mhn, config, app_id=730)
    saved_name = (item.get("steam_market_name") or item.get("name") or "").strip()
    total = 0.0
    for m in matched:
        p = m.get("price", 0)
        total += p
        rec = {"name": saved_name, "goods_id": goods_id, "price": p, "at": time.time(), "pending_receipt": True}
        if market_price is not None and market_price > 0:
            rec["market_price"] = round(float(market_price), 2)
        append_purchase(rec)
    bill_order_ids = [m.get("bill_order_id") for m in matched if m.get("bill_order_id")]
    if bill_order_ids:
        try:
            if buff_client.ask_seller_to_send(bill_order_ids, game_buff) and log_fn:
                log_fn("[Buff]   → 已提醒卖家发货，请留意 Steam 报价", "info")
            elif log_fn:
                log_fn("[Buff]   → 提醒卖家发货未成功（可稍后在订单页手动催发货）", "warn")
        except (BuffAuthExpired, BuffVerificationRequired):
            raise
        except Exception:
            if log_fn:
                log_fn("[Buff]   → 提醒卖家发货请求异常，可稍后在订单页手动催发货", "warn")
    return total
def _do_wait_payment_and_append(
    buff_client: Any,
    item: Dict[str, Any],
    config: dict,
    unit_price: float,
    num: int,
    goods_id: int,
    pay_url: str,
    pay_type: str,
    order_id: str,
    acc: float,
    set_pending_payment: callable,
    wait_payment_confirm: callable,
    confirm_payment: callable,
    is_stop_requested: callable,
    append_purchase: callable,
    log_fn: Optional[Callable[[str, str], None]],
    game_buff: str,
    market_price: Optional[float] = None,
    on_entering_payment: Optional[Callable[[], None]] = None,
) -> Optional[float]:
    ok = _do_payment_notify_and_wait(
        item, config, unit_price, num, pay_url, pay_type, order_id, acc,
        set_pending_payment, wait_payment_confirm, confirm_payment,
        is_stop_requested, log_fn, on_entering_payment,
    )
    if is_stop_requested() or not ok:
        return None
    if market_price is None:
        mhn = (item.get("steam_market_name") or item.get("name") or "").strip()
        market_price = _fetch_smart_market_price(mhn, config, app_id=730)
    saved_name = (item.get("steam_market_name") or item.get("name") or "").strip()
    base_rec = {"name": saved_name, "goods_id": goods_id, "price": unit_price, "at": time.time(), "pending_receipt": True}
    if market_price is not None and market_price > 0:
        base_rec["market_price"] = round(float(market_price), 2)
    for _ in range(num):
        append_purchase(dict(base_rec))
    try:
        if buff_client.ask_seller_to_send(order_id, game_buff) and log_fn:
            log_fn("[Buff]   → 已提醒卖家发货，请留意 Steam 报价", "info")
        elif log_fn:
            log_fn("[Buff]   → 提醒卖家发货未成功（可稍后在订单页手动催发货）", "warn")
    except (BuffAuthExpired, BuffVerificationRequired):
        raise
    except Exception:
        if log_fn:
            log_fn("[Buff]   → 提醒卖家发货请求异常，可稍后在订单页手动催发货", "warn")
    return unit_price * num
def lock_and_confirm_payment(
    buff_client: Any,
    item: Dict[str, Any],
    config: dict,
    target_balance: float,
    acc: float,
    set_pending_payment: callable,
    wait_payment_confirm: callable,
    confirm_payment: callable,
    is_stop_requested: callable,
    append_purchase: callable,
    log_fn: Optional[Callable[[str, str], None]] = None,
    on_entering_payment: Optional[Callable[[], None]] = None,
) -> Optional[float]:
    buff_cfg = config.get("buff", {})
    game_buff = buff_cfg.get("game", "csgo")
    tolerance = float(buff_cfg.get("price_tolerance", 0.5))
    goods_id = item["goods_id"]
    name = item["name"]
    plan_price = item.get("_buff_lowest_price") or item.get("min_price")
    orders = item.get("_buff_sell_orders")
    if not orders:
        orders = buff_client.get_sell_orders(goods_id, game_buff)
        if log_fn:
            log_fn(f"[Buff] 拉取在售 goods_id={goods_id} game={game_buff} → {len(orders or [])} 条", "info")
    else:
        if log_fn:
            log_fn(f"[Buff]   → 复用预检阶段缓存的 Buff 卖单数据 → {len(orders)} 条", "info")
    if not orders:
        if log_fn:
            reason = "接口返回 None，可能是网络/鉴权/风控问题" if orders is None else "Buff 当前无在售卖单"
            log_fn(f"[Buff]   → 无法获取 Buff 卖单信息：{reason}，跳过本件", "warn")
        return None
    lowest_price, count_at_lowest = count_lowest_price_orders(orders)
    if log_fn:
        log_fn(f"[Buff]   → 最低价={lowest_price:.2f} 同价数量={count_at_lowest} 参考价={plan_price} 容忍={tolerance} 累计={acc:.2f} 目标={target_balance}", "info")
    if acc + lowest_price > target_balance:
        if log_fn:
            log_fn(f"[Buff]   → 累计+本件={acc + lowest_price:.2f} 已达/超过目标，不再锁单", "info")
        return TARGET_REACHED
    if plan_price is not None and lowest_price - plan_price > tolerance:
        if log_fn:
            log_fn(f"[Buff]   → 当前价较参考价超出容忍 (差{lowest_price - plan_price:.2f})，跳过", "warn")
        return None
    market_hash_name = (item.get("steam_market_name") or item.get("name") or "").strip()
    scfg = config.get("pipeline", {})
    max_discount = scfg.get("max_discount") if is_strategy_module_enabled(config, "buy", "guard.max_discount") else None
    sell_pressure_threshold = _parse_threshold(scfg.get("sell_pressure_threshold")) if is_strategy_module_enabled(config, "buy", "guard.sell_pressure") else None
    steam_depth_enabled = is_strategy_module_enabled(config, "buy", "buy.steam_sell_depth")
    need_steam = (
        steam_depth_enabled
        and (
            max_discount is not None
            or (sell_pressure_threshold is not None and sell_pressure_threshold > 0 and int(item.get("daily_volume", 0) or 0) > 0)
        )
    )
    cached_steam_data = item.get("_steam_sell_data")
    steam_sell_error = None
    if need_steam:
        if cached_steam_data is not None:
            steam_sell_data = cached_steam_data
        else:
            steam_sell_data, steam_sell_error = _fetch_steam_sell_data(
                market_hash_name, config, app_id=730, return_error=True
            )
        if cached_steam_data is not None and log_fn:
            log_fn("[Buff]   → 复用稳定性阶段已缓存的 Steam 卖单数据", "info")
    else:
        steam_sell_data = None
    ref_price = steam_sell_data.get("smart_price") if steam_sell_data else None
    sell_orders = steam_sell_data.get("sell_orders") if steam_sell_data else None
    if max_discount is not None:
        max_discount = float(max_discount)
        if ref_price is None or ref_price <= 0:
            if log_fn:
                reason = steam_sell_error or "Steam 卖单为空或智能参考价无效"
                log_fn(f"[Buff]   → 二次验证: 无法获取 Steam 参考价：{reason}，跳过本件", "warn")
            return SKIP_VERIFICATION_FAILED
        ref_price = _adjust_ref_price_for_daily_high(
            market_hash_name, ref_price, config, log_fn, app_id=730
        )
        if lowest_price > 0:
            value_ratio = (lowest_price / ref_price) * 1.15
            if value_ratio >= max_discount:
                if log_fn:
                    log_fn(f"[Buff]   → 二次验证未通过 (Buff最低价/参考价)×1.15={value_ratio:.4f} 需<{max_discount} (参考价={ref_price:.2f})", "warn")
                return SKIP_VERIFICATION_FAILED
            if log_fn:
                log_fn(f"[Buff]   → 二次验证通过 (Buff最低价/参考价)×1.15={value_ratio:.4f} 参考价={ref_price:.2f}", "info")
    if ref_price and lowest_price > 0:
        item["value_ratio"] = (lowest_price / ref_price) * 1.15
    n_sell_orders = int(scfg.get("sell_pressure_orders_n", 5) or 5)
    if sell_pressure_threshold is not None and sell_pressure_threshold > 0:
        daily_vol = int(item.get("daily_volume", 0) or 0)
        if daily_vol > 0 and sell_orders:
            pressure = _compute_sell_pressure_from_orders(sell_orders, daily_vol, n_sell_orders)
            if pressure is not None and pressure > sell_pressure_threshold:
                if log_fn:
                    log_fn(f"[Buff]   → 卖压过高 前{n_sell_orders}档总量/日销={pressure:.2f} 阈值={sell_pressure_threshold}，跳过", "warn")
                return None
        elif daily_vol <= 0 and log_fn:
            log_fn("[Buff]   → 卖压检查: 日销量为0，跳过", "info")
    buy_runtime = ((config or {}).get("_strategy_runtime") or {}).get("buy")
    if buy_runtime:
        legacy_safe_enabled = is_strategy_module_enabled(config, "buy", "guard.safe_purchase_limit", default=False)
        hard_cap_enabled = legacy_safe_enabled or is_strategy_module_enabled(config, "buy", "guard.purchase_hard_cap", default=False)
        liquidity_cap_enabled = legacy_safe_enabled or is_strategy_module_enabled(config, "buy", "guard.purchase_liquidity_cap", default=False)
        low_price_guard_enabled = legacy_safe_enabled or is_strategy_module_enabled(config, "buy", "guard.low_price_purchase_guard", default=False)
        held_same_guard_enabled = legacy_safe_enabled or is_strategy_module_enabled(config, "buy", "guard.held_same_item_guard", default=False)
    else:
        hard_cap_enabled = True
        liquidity_cap_enabled = True
        low_price_guard_enabled = True
        held_same_guard_enabled = True
    safe_purchase_enabled = any((
        hard_cap_enabled,
        liquidity_cap_enabled,
        low_price_guard_enabled,
        held_same_guard_enabled,
    ))
    if safe_purchase_enabled:
        cap_candidates = []
        daily_volume = int(item.get("daily_volume", 0) or 0)
        is_low_price = lowest_price < float(scfg.get("safe_purchase_low_price_threshold", 5.0))
        if hard_cap_enabled:
            cap_candidates.append(int(scfg.get("safe_purchase_hard_qty_cap", 50)))
        if liquidity_cap_enabled:
            volume_cap = int(daily_volume * float(scfg.get("safe_purchase_liquidity_ratio", 0.05)))
            if low_price_guard_enabled and is_low_price:
                volume_cap = int(volume_cap * float(scfg.get("safe_purchase_low_price_penalty", 0.5)))
            cap_candidates.append(volume_cap)
        if low_price_guard_enabled and is_low_price:
            cap_candidates.append(int(scfg.get("safe_purchase_low_price_hard_cap", 30)))
        safe_limit = max(min(cap_candidates), 0) if cap_candidates else count_at_lowest
    else:
        safe_limit = count_at_lowest
    item_name = market_hash_name
    if item_name and held_same_guard_enabled:
        purchases_snapshot = get_purchases()
        holdings = [p for p in purchases_snapshot if not (p.get("sale_price") and float(p.get("sale_price", 0) or 0) > 0)]
        held_same = sum(1 for p in holdings if (p.get("name") or "").strip() == item_name)
        safe_limit = max(0, safe_limit - held_same)
        if log_fn and held_same > 0:
            log_fn(f"[Buff]   → 已持有同名(英文) {held_same} 件，安全上限 {safe_limit + held_same} → {safe_limit}", "info")
    if safe_limit <= 0:
        if log_fn:
            log_fn("[Buff]   → 安全采购模块限制为0，跳过本件", "warn")
        return SKIP_NO_FAILED
    remaining = target_balance - acc
    num_to_buy = min(count_at_lowest, max(1, int(remaining / lowest_price)))
    orig_num = num_to_buy
    num_to_buy = min(num_to_buy, max(1, safe_limit))
    if log_fn and orig_num > num_to_buy:
        log_fn(f"[Buff]   → 安全采购上限={safe_limit}，原计划={orig_num} 实际购买={num_to_buy}", "info")
    def _try_single_buy():
        o = first_order_at_price(orders, lowest_price)
        if not o:
            return None
        p = float(o.get("price", 0))
        if log_fn:
            log_fn(f"[Buff]   → 锁单 order_id={o.get('id')} price={o.get('price')}", "info")
        try:
            result = buff_client.lock_and_get_pay_url(game_buff, goods_id, o["id"], o["price"])
        except (BuffAuthExpired, BuffVerificationRequired):
            raise
        except Exception as e:
            if log_fn:
                log_fn(f"[Buff]   → 锁单网络/接口异常: {e}", "warn")
            return None
        if not result or not result.get("success"):
            if log_fn:
                code_str = result.get('code') if result else '未知'
                msg_str = result.get('msg', '无响应内容') if result else '请求失败或超时'
                log_fn(f"[Buff]   → 锁单失败 code={code_str} msg={msg_str}", "warn")
            return None
        if log_fn:
            log_fn(f"[Buff]   → 锁单成功 order_id={result.get('order_id')} 等待用户确认付款…", "info")
        return _do_wait_payment_and_append(
            buff_client,
            item,
            config,
            p,
            1,
            goods_id,
            result.get("pay_url") or "",
            result.get("pay_type") or "alipay",
            result.get("order_id", ""),
            acc,
            set_pending_payment,
            wait_payment_confirm,
            confirm_payment,
            is_stop_requested,
            append_purchase,
            log_fn,
            game_buff,
            market_price=ref_price,
            on_entering_payment=on_entering_payment,
        )
    def _try_batch_buy():
        try:
            batch_result = buff_client.try_batch_buy(goods_id, game_buff, orders, lowest_price, num_to_buy)
        except (BuffAuthExpired, BuffVerificationRequired):
            raise
        except Exception as e:
            if log_fn:
                log_fn(f"[Buff]   → 批量购买接口异常: {e}", "warn")
            return None
        if not batch_result or not batch_result.get("success"):
            if log_fn:
                log_fn("[Buff]   → 批量锁单失败，接口未返回成功状态", "warn")
            return None
        if log_fn:
            log_fn(f"[Buff]   → 批量锁单成功 batch_id={batch_result.get('batch_id')} 数量={num_to_buy} 单价={lowest_price:.2f} 总价={batch_result.get('total_price', 0):.2f} 等待用户确认付款…", "info")
        return _do_batch_wait_finalize_and_append(
            buff_client,
            item,
            config,
            lowest_price,
            num_to_buy,
            goods_id,
            batch_result.get("batch_id", ""),
            game_buff,
            batch_result["pay_url"],
            acc,
            set_pending_payment,
            wait_payment_confirm,
            confirm_payment,
            is_stop_requested,
            append_purchase,
            log_fn,
            market_price=ref_price,
            on_entering_payment=on_entering_payment,
        )
    if num_to_buy == 1:
        retry_delay = max(0, int(config.get("pipeline", {}).get("buff_retry_delay_seconds", 5) or 5))
        for attempt in range(3):
            paid = _try_single_buy()
            if paid is not None:
                return paid
            if attempt < 2:
                if log_fn:
                    log_fn(f"[Buff]   → 单件购买失败，{retry_delay}秒后重试 ({attempt + 2}/3)…", "info")
                if retry_delay > 0:
                    jittered_sleep(retry_delay)
        if log_fn:
            log_fn("[Buff]   → 单件购买重试2次后仍失败，跳过", "warn")
        return None
    else:
        paid = _try_batch_buy()
        if paid is not None:
            return paid
        if log_fn:
            log_fn(f"[Buff]   → 批量购买不可用，尝试单个购买", "info")
        paid = _try_single_buy()
        if paid is not None:
            return paid
    if log_fn:
        log_fn("[Buff]   → 未找到最低价订单，跳过", "warn")
    return None
