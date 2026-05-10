import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline_steps import _compute_sell_pressure_from_orders, filter_market_rows


def _行(name="Test Item", min_price="10.00", platform="https://buff.163.com/goods/12345", **kw):
    """构造一条最小化 market 行，省得每次都写全"""
    row = SimpleNamespace(
        name=name,
        min_price=min_price,
        sell_ratio="0.85",
        buy_ratio="0.80",
        safe_buy_ratio="0.78",
        recent_ratio="0.82",
        platform=platform,
        steam_link="",
        volume="500",
    )
    for k, v in kw.items():
        setattr(row, k, v)
    return row


_基础配置 = {
    "pipeline": {"exclude_keywords": ["印花", "胶囊"], "market_top_n": 0, "sort_by": "sell"},
}

# ── 卖压计算测试 ──────────────────────────────────────────────────────────

def test_卖压_空订单返回None():
    assert _compute_sell_pressure_from_orders([], 100) is None


def test_卖压_日销量为零返回None():
    orders = [(5.0, 10), (5.1, 5)]
    assert _compute_sell_pressure_from_orders(orders, 0) is None


def test_卖压_大量挂单卖压高():
    # 200件挂单，日销10，压力比 = 20，稳妥超过阈值
    orders = [(5.0, 200)]
    pressure = _compute_sell_pressure_from_orders(orders, 10, n_orders=1)
    assert pressure is not None
    assert pressure > 1.0


def test_卖压_少量挂单卖压低():
    orders = [(5.0, 1), (5.1, 1)]
    pressure = _compute_sell_pressure_from_orders(orders, 1000, n_orders=5)
    assert pressure is not None
    assert pressure < 1.0


def test_卖压_价格断层识别():
    # 3件挂5元，然后跳到10元才有大单——属于薄壁情况，应该降权
    orders_断层 = [(5.0, 3), (10.0, 50)]
    orders_正常 = [(5.0, 25), (5.1, 25)]
    p1 = _compute_sell_pressure_from_orders(orders_断层, 100, n_orders=5)
    p2 = _compute_sell_pressure_from_orders(orders_正常, 100, n_orders=5)
    assert p1 is not None and p2 is not None
    # 薄壁的卖压应该比正常的低（被打折了）
    assert p1 < p2


# ── market行过滤测试 ────────────────────────────────────────────────────────

def test_过滤_正常行通过():
    rows = [_行(name="AWP | Dragon Lore")]
    result = filter_market_rows(rows, _基础配置)
    assert len(result) == 1


def test_过滤_关键词命中被去掉():
    rows = [
        _行(name="印花 | 某队伍"),
        _行(name="胶囊 | 某队伍"),
        _行(name="AWP | 正常枪"),
    ]
    result = filter_market_rows(rows, _基础配置)
    assert len(result) == 1
    assert result[0]["name"] == "AWP | 正常枪"


def test_过滤_价格非正被去掉():
    rows = [
        _行(name="负价格", min_price="-1"),
        _行(name="零价格", min_price="0"),
        _行(name="正常价格", min_price="10.0"),
    ]
    result = filter_market_rows(rows, _基础配置)
    assert len(result) == 1
    assert result[0]["name"] == "正常价格"


def test_过滤_非buff链接被去掉():
    rows = [
        _行(name="c5game的", platform="https://c5game.com/item/999"),
        _行(name="buff的"),
    ]
    result = filter_market_rows(rows, _基础配置)
    assert len(result) == 1
    assert result[0]["name"] == "buff的"


def test_过滤_topN限制数量():
    rows = [_行(name=f"物品{i}") for i in range(20)]
    cfg = {**_基础配置, "pipeline": {**_基础配置["pipeline"], "market_top_n": 5}}
    result = filter_market_rows(rows, cfg)
    assert len(result) <= 5


def test_过滤_goods_id从url解析():
    rows = [_行(platform="https://buff.163.com/goods/98765")]
    result = filter_market_rows(rows, _基础配置)
    assert result[0]["goods_id"] == 98765
