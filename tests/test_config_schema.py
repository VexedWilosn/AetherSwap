import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_schema import DEFAULTS, _validate_ranges, merge, validate_and_fill


# ── merge() 深度合并测试 ──────────────────────────────────────────────────

def test_merge_顶层覆盖():
    result = merge({"a": 1, "b": 2}, {"b": 99})
    assert result["a"] == 1
    assert result["b"] == 99


def test_merge_深层合并不影响未指定key():
    # 用户只改了max_discount，target_balance不能消失
    defaults = {"pipeline": {"target_balance": 100, "max_discount": 0.9}}
    result = merge(defaults, {"pipeline": {"max_discount": 0.85}})
    assert result["pipeline"]["target_balance"] == 100
    assert result["pipeline"]["max_discount"] == 0.85


def test_merge_非dict会整体覆盖():
    result = merge({"x": {"a": 1}}, {"x": 42})
    assert result["x"] == 42


def test_merge_不修改原始defaults():
    defaults = {"a": {"b": 1}}
    merge(defaults, {"a": {"b": 99}})
    assert defaults["a"]["b"] == 1  # 原始不能被污染


# ── validate_and_fill() 类型转换测试 ─────────────────────────────────────

def test_validate_字符串转float():
    result = validate_and_fill({"pipeline": {"max_discount": "0.7"}}, DEFAULTS)
    assert isinstance(result["pipeline"]["max_discount"], float)
    assert result["pipeline"]["max_discount"] == 0.7


def test_validate_字符串转int():
    result = validate_and_fill({"pipeline": {"max_staleness_minutes": "30"}}, DEFAULTS)
    assert result["pipeline"]["max_staleness_minutes"] == 30


def test_validate_缺少section用默认值():
    result = validate_and_fill({}, DEFAULTS)
    assert result["stability"]["cv_threshold"] == DEFAULTS["stability"]["cv_threshold"]


# ── _validate_ranges() 范围校验 ───────────────────────────────────────────

def test_range_max_discount超出被限制():
    cfg = merge(DEFAULTS, {"pipeline": {"max_discount": 1.5}})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _validate_ranges(cfg)
    assert result["pipeline"]["max_discount"] <= 1.0
    assert any("max_discount" in str(w.message) for w in caught)


def test_range_cv_threshold为零被限制():
    cfg = merge(DEFAULTS, {"stability": {"cv_threshold": 0.0}})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        result = _validate_ranges(cfg)
    assert result["stability"]["cv_threshold"] > 0.0


def test_range_正常值不被修改():
    cfg = merge(DEFAULTS, {})
    orig = cfg["pipeline"]["max_discount"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _validate_ranges(cfg)
    assert result["pipeline"]["max_discount"] == orig
    # 正常配置不应该有警告
    assert not [w for w in caught if "max_discount" in str(w.message)]


def test_range_price_tolerance负数被限制():
    # TODO: 测一下0.0这种边界情况
    cfg = merge(DEFAULTS, {"buff": {"price_tolerance": -1.0}})
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        result = _validate_ranges(cfg)
    assert result["buff"]["price_tolerance"] >= 0.0


def test_buff_wallet_pay_method_is_allowed():
    cfg = merge(DEFAULTS, {"buff": {"pay_method": "wallet"}})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _validate_ranges(cfg)
    assert result["buff"]["pay_method"] == "wallet"
    assert not [w for w in caught if "pay_method" in str(w.message)]


def test_cash_platform_partial_update_keeps_other_platform_settings():
    current = merge(
        DEFAULTS,
        {
            "cash_platform_trading": {
                "platforms": {
                    "uuyp": {"purchase_order_weight": 9, "order_poll_interval_seconds": 77},
                    "eco": {"allow_direct_buy": False},
                }
            }
        },
    )

    result = merge(
        current,
        {
            "cash_platform_trading": {
                "primary_platform": "buff",
                "platforms": {"buff": {"order_poll_interval_seconds": 15}},
            }
        },
    )

    assert result["cash_platform_trading"]["platforms"]["buff"]["order_poll_interval_seconds"] == 15
    assert result["cash_platform_trading"]["platforms"]["uuyp"]["purchase_order_weight"] == 9
    assert result["cash_platform_trading"]["platforms"]["uuyp"]["order_poll_interval_seconds"] == 77
    assert result["cash_platform_trading"]["platforms"]["eco"]["allow_direct_buy"] is False


def test_uuyp_direct_buy_default_is_disabled():
    result = validate_and_fill({}, DEFAULTS)

    assert result["cash_platform_trading"]["platforms"]["uuyp"]["allow_direct_buy"] is False
    assert result["cash_platform_trading"]["platforms"]["uuyp"]["allow_purchase_order"] is True


def test_validate_preserves_disabled_auto_trading_module():
    result = validate_and_fill({"automation_modules": {"auto_trading_enabled": False}}, DEFAULTS)
    assert result["automation_modules"]["auto_trading_enabled"] is False
    assert result["automation_modules"]["autostart_on_webui_boot"] is False


def test_validate_fills_low_price_exposure_guard_defaults():
    result = validate_and_fill({}, DEFAULTS)

    guard = result["low_price_exposure_guard"]
    assert guard["enabled"] is True
    assert guard["rule"] == "0-0-0.02-2-0.05-4-0.10-8-0.30"
    assert guard["hide_signals"] is True
    assert guard["block_execution"] is True
