
import pytest

from app import pipeline_steps as steps


@pytest.mark.parametrize(
    "raw_ratio, adjusted_ratio, expected_adjust_calls, expected_history_calls",
    [
        (0.80, 0.80, 0, 0),  # A: 明显不达标
        (0.70, 0.70, 0, 0),  # B: 恰好等于阈值
        (0.68, 0.68, 1, 1),  # C: 通过两次折扣检查
        (0.68, 0.73, 1, 0),  # D: 历史修正后不达标
    ],
)
def test_early_discount_precheck(
    monkeypatch,
    raw_ratio,
    adjusted_ratio,
    expected_adjust_calls,
    expected_history_calls,
):
    calls = {
        "adjust": 0,
        "history": 0,
        "analyze": 0,
        "buff": 0,
    }

    # 模拟一件饰品。
    # Steam 参考价格固定为 100 元。
    smart_price = 100.0

    # 根据目标成本率反推 Buff 计划购买价。
    plan_price = raw_ratio * smart_price / steps.STEAM_FEE_FACTOR

    item = {
        "name": "Test Item",
        "steam_market_name": "Test Item",
        "goods_id": 12345,
        "min_price": plan_price,
        "daily_volume": 500,
    }

    config = {
        "pipeline": {
            "max_discount": 0.70,
            "sell_pressure_threshold": 0,
            "verbose_debug": False,
        },
        "stability": {
            "days": 30,
            "request_interval_seconds": 0,
            "request_failure_delay_seconds": 0,
        },
    }

    # 仅启用本测试需要的策略模块。
    enabled_modules = {
        "buy.steam_sell_depth",
        "guard.max_discount",
        "guard.history_data_window",
    }

    def fake_module_enabled(
        _config, _side, module_id, default=True
    ):
        return module_id in enabled_modules

    monkeypatch.setattr(
        steps,
        "is_strategy_module_enabled",
        fake_module_enabled,
    )

    # 禁止真实 Steam 卖单请求。
    def fake_steam_sell_data(*args, **kwargs):
        data = {
            "smart_price": smart_price,
            "sell_orders": [(100.0, 20)],
        }
        if kwargs.get("return_error"):
            return data, None
        return data

    monkeypatch.setattr(
        steps,
        "_fetch_steam_sell_data",
        fake_steam_sell_data,
    )

    # 模拟历史价格修正。
    def fake_adjust(name, price, *args, **kwargs):
        calls["adjust"] += 1
        return raw_ratio * smart_price / adjusted_ratio

    monkeypatch.setattr(
        steps,
        "_adjust_ref_price_for_daily_high",
        fake_adjust,
    )

    # 禁止真实历史接口请求。
    class FakeSteamClient:
        def fetch_history(self, *args, **kwargs):
            calls["history"] += 1
            return {
                "history": [["Oct 01 2026 00: +0", 100.0, "10"]],
                "currency": "CNY",
            }

    # 模拟稳定性分析器。
    # 返回无效结果，以便在此处结束流程。
    class FakeAnalyzer:
        def analyze(self, *args, **kwargs):
            calls["analyze"] += 1
            return {
                "valid": False,
                "msg": "Test stopped at analyzer",
                "is_stable": False,
            }

    # 禁止意外进入 Buff 实时购买相关检查。
    class FakeBuffClient:
        def get_sell_orders(self, *args, **kwargs):
            calls["buff"] += 1
            raise AssertionError("Unexpected Buff request")

    # 禁止等待与外部状态操作。
    monkeypatch.setattr(
        steps, "jittered_sleep", lambda *_args, **_kwargs: None
    )

    monkeypatch.setattr(
        steps, "set_status", lambda *_args, **_kwargs: None
    )

    selected, failed = steps.pick_stable_item(
        filtered=[item],
        config=config,
        steam_client=FakeSteamClient(),
        analyzer=FakeAnalyzer(),
        is_stop_requested=lambda: False,
        log_fn=lambda *_args: None,
        buff_client=FakeBuffClient(),
    )

    # 四个场景最终都应拒绝测试候选。
    assert selected is None
    assert 12345 in failed

    # 核心断言：验证请求执行顺序。
    assert calls["adjust"] == expected_adjust_calls
    assert calls["history"] == expected_history_calls
    assert calls["analyze"] == expected_history_calls

    # 不允许访问真实 Buff。
    assert calls["buff"] == 0
