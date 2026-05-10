from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from DataEngine import buff_public_monitor, main_engine, steam_public_monitor, uuyp_public_monitor
from DataEngine.arbitrage_engine import ArbitrageScanner
from DataEngine.action_policy import (
    ACTION_BLOCKED,
    ACTION_CREATE_BUY_ORDER,
    ACTION_DIRECT_BUY,
    ActionPolicyDecision,
    compute_action_decision,
    action_policy_config,
    estimate_holding_state,
    save_action_decisions,
    select_risk_segment,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from DataEngine.database import ActionDecision, Base, ItemBase, MarketPrice
from DataEngine import priority_scheduler as priority_scheduler_mod
from DataEngine.priority_scheduler import PRIORITY_HIGH_FREQ, PRIORITY_LOW_FREQ, PRIORITY_STEAMDT_CANDIDATE, compute_priority_decision, recalculate_priorities
from DataEngine.proxy_pool import classify_request_failure, proxy_cooldown_for_reason
from DataEngine.stop_signal import StopRequested
from DataEngine.trade_executor import OpportunityView, SUPPORTED_BUY_PLATFORMS, should_skip_jit_refresh
from utils.proxy_manager import ProxyManager, _build_proxy_url


def test_proxy_manager_cools_down_407_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr("utils.proxy_manager.PROXY_HEALTH_STATE_PATH", tmp_path / "proxy_health_state.json")
    proxy_cfg = {"host": "10.0.0.1", "port": 8080, "username": "u", "password": "p"}
    monkeypatch.setattr("utils.proxy_manager._load_proxy_pool_cfg", lambda: {"enabled": True, "strategy": 2, "proxies": [proxy_cfg]})

    manager = ProxyManager()
    assert manager.get_next_proxy_dict() is not None

    assert manager.mark_proxy_failure(_build_proxy_url(proxy_cfg), reason="407", cooldown_seconds=900)
    assert manager.get_next_proxy_dict() is None


def test_proxy_manager_persists_health_state(monkeypatch, tmp_path):
    state_path = tmp_path / "proxy_health_state.json"
    proxy_cfg = {"host": "10.0.0.2", "port": 8081, "username": "u", "password": "p"}
    monkeypatch.setattr("utils.proxy_manager.PROXY_HEALTH_STATE_PATH", state_path)
    monkeypatch.setattr("utils.proxy_manager._load_proxy_pool_cfg", lambda: {"enabled": True, "strategy": 2, "proxies": [proxy_cfg]})

    manager = ProxyManager()
    url = _build_proxy_url(proxy_cfg)
    assert manager.mark_proxy_failure(url, reason="proxy_auth", cooldown_seconds=900)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["10.0.0.2:8081"]["last_failure_reason"] == "proxy_auth"

    reloaded = ProxyManager()
    assert reloaded.get_next_proxy_dict() is None


def test_proxy_failure_classification():
    assert classify_request_failure(Exception("CONNECT tunnel failed, response 407")) == "proxy_auth"
    assert classify_request_failure(status_code=403) == "blocked"
    assert classify_request_failure(status_code=429) == "rate_limited"
    assert classify_request_failure(Exception("Recv failure: Connection was reset")) == "connection_reset"
    assert proxy_cooldown_for_reason("proxy_auth") > proxy_cooldown_for_reason("connection_reset")


def test_empty_action_does_not_skip_jit_refresh(monkeypatch):
    monkeypatch.setattr("DataEngine.trade_executor._load_price_map", lambda session, item_id, platforms: {})
    opportunity = OpportunityView(
        id=1,
        item_id=42,
        market_hash_name="AK-47 | Slate (Field-Tested)",
        buy_price=10.0,
        buy_platform="buff",
        sell_price=12.0,
        sell_platform="steam",
        profit_rate=0.1,
        status="open",
    )

    skip, reason = should_skip_jit_refresh(
        session=SimpleNamespace(),
        opportunity=opportunity,
        config={"pipeline": {"JIT_BYPASS_MINUTES": 5}},
        platforms={"buff", "steam"},
    )

    assert not skip
    assert reason == "trade_action_requires_jit"


def test_direct_trade_does_not_skip_jit_even_when_prices_are_fresh(monkeypatch):
    now = datetime.now()
    price_map = {
        "buff": SimpleNamespace(updated_at=now, sell_min=10.0, buy_max=9.0, data_source="steamdt", volume=100),
        "steam": SimpleNamespace(updated_at=now, sell_min=12.0, buy_max=11.0, data_source="steamdt", volume=100),
    }
    monkeypatch.setattr("DataEngine.trade_executor._load_price_map", lambda session, item_id, platforms: price_map)
    opportunity = OpportunityView(
        id=11,
        item_id=42,
        market_hash_name="AK-47 | Slate (Field-Tested)",
        buy_price=10.0,
        buy_platform="buff",
        sell_price=12.0,
        sell_platform="steam",
        profit_rate=0.1,
        status="open",
        action="direct_trade",
    )

    skip, reason = should_skip_jit_refresh(
        session=SimpleNamespace(),
        opportunity=opportunity,
        config={"pipeline": {"JIT_BYPASS_MINUTES": 5}, "steamdt": {"allow_jit_bypass": True}},
        platforms={"buff", "steam"},
    )

    assert not skip
    assert reason == "trade_action_requires_jit"


def test_purchase_order_skips_jit_only_when_prices_are_fresh(monkeypatch):
    now = datetime.now()
    price_map = {
        "eco": SimpleNamespace(updated_at=now, sell_min=10.0, buy_max=9.0, data_source="baseline", volume=100),
        "steam": SimpleNamespace(updated_at=now, sell_min=12.0, buy_max=11.0, data_source="baseline", volume=100),
    }
    monkeypatch.setattr("DataEngine.trade_executor._load_price_map", lambda session, item_id, platforms: price_map)
    opportunity = OpportunityView(
        id=2,
        item_id=42,
        market_hash_name="AK-47 | Slate (Field-Tested)",
        buy_price=10.0,
        buy_platform="eco",
        sell_price=12.0,
        sell_platform="steam",
        profit_rate=0.1,
        status="open",
    )
    opportunity.action = "purchase_order"

    skip, reason = should_skip_jit_refresh(
        session=SimpleNamespace(),
        opportunity=opportunity,
        config={"pipeline": {"purchase_order_jit_bypass_minutes": 15}},
        platforms={"eco", "steam"},
    )

    assert skip
    assert reason == "fresh_purchase_order_within_15m"


def test_purchase_order_requires_jit_when_prices_are_stale(monkeypatch):
    old = datetime.now() - timedelta(minutes=20)
    price_map = {
        "eco": SimpleNamespace(updated_at=old, sell_min=10.0, buy_max=9.0, data_source="baseline", volume=100),
        "steam": SimpleNamespace(updated_at=old, sell_min=12.0, buy_max=11.0, data_source="baseline", volume=100),
    }
    monkeypatch.setattr("DataEngine.trade_executor._load_price_map", lambda session, item_id, platforms: price_map)
    opportunity = OpportunityView(
        id=3,
        item_id=42,
        market_hash_name="AK-47 | Slate (Field-Tested)",
        buy_price=10.0,
        buy_platform="eco",
        sell_price=12.0,
        sell_platform="steam",
        profit_rate=0.1,
        status="open",
    )
    opportunity.action = "purchase_order"

    skip, reason = should_skip_jit_refresh(
        session=SimpleNamespace(),
        opportunity=opportunity,
        config={"pipeline": {"purchase_order_jit_bypass_minutes": 15}},
        platforms={"eco", "steam"},
    )

    assert not skip
    assert reason == "purchase_order_prices_stale"


def test_priority_scheduler_promotes_high_volume_spread_item():
    item = SimpleNamespace(id=1)
    prices = [
        SimpleNamespace(platform_name="steam", sell_min=12.0, buy_max=11.0, volume=120),
        SimpleNamespace(platform_name="buff", sell_min=8.0, buy_max=7.5, volume=120),
    ]

    decision = compute_priority_decision(
        item,
        prices,
        [],
        config={"priority_scheduler": {"min_volume_24h": 20, "min_net_profit_rate": 0.06, "promote_p3_score": 50}},
    )

    assert decision.priority == PRIORITY_STEAMDT_CANDIDATE
    assert decision.score > 50
    assert "p1_to_p2" in decision.reason


def test_priority_scheduler_promotes_cashout_below_steam_nominal_price():
    item = SimpleNamespace(id=1)
    prices = [
        SimpleNamespace(platform_name="steam", sell_min=100.0, buy_max=0.0, volume=0, data_source="steamdt_openapi", liquidity_score=0),
        SimpleNamespace(
            platform_name="buff",
            sell_min=95.0,
            buy_max=80.0,
            volume=0,
            data_source="steamdt_openapi",
            sell_volume=10,
            buy_volume=20,
            orderbook_depth=30,
            liquidity_score=2.0,
        ),
    ]

    decision = compute_priority_decision(
        item,
        prices,
        [],
        config={
            "pipeline": {"steam_balance_cost_ratio": 0.6},
            "priority_scheduler": {"min_volume_24h": 999, "min_liquidity_score": 1.0, "min_net_profit_rate": 0.06},
        },
    )

    assert decision.priority == PRIORITY_HIGH_FREQ
    assert "cashout_75_discount_profit" in decision.reason
    assert "spread=0.3000" in decision.reason


def test_priority_scheduler_cashout_discount_tiers_target_priorities():
    base_prices = [
        SimpleNamespace(platform_name="steam", sell_min=100.0, buy_max=0.0, volume=0, data_source="steamdt_openapi", liquidity_score=0),
        SimpleNamespace(
            platform_name="buff",
            sell_min=95.0,
            buy_max=78.0,
            volume=0,
            data_source="steamdt_openapi",
            sell_volume=10,
            buy_volume=20,
            orderbook_depth=30,
            liquidity_score=2.0,
        ),
    ]
    cfg = {"priority_scheduler": {"min_volume_24h": 999, "min_liquidity_score": 1.0, "min_net_profit_rate": 0.06}}

    p75 = compute_priority_decision(SimpleNamespace(id=1, crawl_priority=1), base_prices, [], config=cfg)
    assert p75.priority == PRIORITY_HIGH_FREQ
    assert "cashout_75_discount_profit" in p75.reason

    base_prices[1].buy_max = 73.0
    p70 = compute_priority_decision(SimpleNamespace(id=1, crawl_priority=3), base_prices, [], config=cfg)
    assert p70.priority == PRIORITY_STEAMDT_CANDIDATE
    assert "cashout_70_discount_profit" in p70.reason

    base_prices[1].buy_max = 68.0
    p65 = compute_priority_decision(SimpleNamespace(id=1, crawl_priority=3), base_prices, [], config=cfg)
    assert p65.priority == PRIORITY_LOW_FREQ
    assert "cashout_65_discount_profit" in p65.reason


def test_recalculate_priorities_uses_full_app_config_for_cashout_ratio(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(priority_scheduler_mod, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(priority_scheduler_mod, "load_app_config", lambda: {
        "pipeline": {"steam_balance_cost_ratio": 0.6},
        "priority_scheduler": {
            "enabled": True,
            "min_volume_24h": 999,
            "min_liquidity_score": 1.0,
            "min_net_profit_rate": 0.06,
        },
    })
    monkeypatch.setattr("DataEngine.radar_snapshot.refresh_radar_snapshots", lambda item_ids: None)

    with TestingSessionLocal() as session:
        item = ItemBase(id=880001, market_hash_name="Cashout Config Ratio Test", crawl_priority=1, is_active=True)
        session.add(item)
        session.add_all(
            [
                MarketPrice(
                    item_id=item.id,
                    platform_name="steam",
                    data_source="steamdt_openapi",
                    sell_min=100.0,
                    buy_max=0.0,
                    volume=0,
                    liquidity_score=0,
                    currency="CNY",
                    updated_at=datetime.now(),
                ),
                MarketPrice(
                    item_id=item.id,
                    platform_name="buff",
                    data_source="steamdt_openapi",
                    sell_min=95.0,
                    buy_max=80.0,
                    volume=0,
                    orderbook_depth=30,
                    liquidity_score=2.0,
                    currency="CNY",
                    updated_at=datetime.now(),
                ),
            ]
        )
        session.commit()

    assert recalculate_priorities() == 1

    with TestingSessionLocal() as session:
        item = session.get(ItemBase, 880001)
        assert item.crawl_priority == PRIORITY_HIGH_FREQ
        assert "spread=0.3000" in item.priority_reason

    engine.dispose()


def test_priority_scheduler_uses_hysteresis_for_p2_to_p3():
    item = SimpleNamespace(id=1, crawl_priority=PRIORITY_STEAMDT_CANDIDATE, priority_up_hits=1, priority_down_hits=0)
    prices = [
        SimpleNamespace(platform_name="steam", sell_min=12.0, buy_max=11.0, volume=120),
        SimpleNamespace(platform_name="buff", sell_min=8.0, buy_max=7.5, volume=120),
    ]
    opps = [SimpleNamespace(steamdt_updated_at=datetime.now(), transaction_count_24h=120, profit_rate=20)]

    decision = compute_priority_decision(
        item,
        prices,
        opps,
        config={"priority_scheduler": {"min_volume_24h": 20, "min_net_profit_rate": 0.06, "p2_to_p3_score": 50, "p2_to_p3_hit_rounds": 2}},
    )

    assert decision.priority == PRIORITY_HIGH_FREQ
    assert "p2_to_p3" in decision.reason


def test_priority_scheduler_does_not_let_steamdt_opportunity_override_market_spread():
    item = SimpleNamespace(id=1, crawl_priority=PRIORITY_STEAMDT_CANDIDATE, priority_up_hits=1, priority_down_hits=0)
    prices = [
        SimpleNamespace(platform_name="steam", sell_min=100.0, buy_max=0.0, volume=120),
        SimpleNamespace(platform_name="buff", sell_min=120.0, buy_max=1.0, volume=120),
    ]
    opps = [SimpleNamespace(steamdt_updated_at=datetime.now(), transaction_count_24h=120, profit_rate=8000)]

    decision = compute_priority_decision(
        item,
        prices,
        opps,
        config={"priority_scheduler": {"min_volume_24h": 20, "min_net_profit_rate": 0.06, "p2_to_p3_score": 50, "p2_to_p3_hit_rounds": 2}},
    )

    assert decision.priority == PRIORITY_STEAMDT_CANDIDATE
    assert "spread=-0.2917" in decision.reason


def test_priority_scheduler_uses_steamdt_opportunity_only_without_market_signal():
    item = SimpleNamespace(id=1, crawl_priority=PRIORITY_STEAMDT_CANDIDATE, priority_up_hits=1, priority_down_hits=0)
    prices = []
    opps = [SimpleNamespace(steamdt_updated_at=datetime.now(), transaction_count_24h=120, profit_rate=20)]

    decision = compute_priority_decision(
        item,
        prices,
        opps,
        config={"priority_scheduler": {"min_volume_24h": 20, "min_net_profit_rate": 0.06, "p2_to_p3_score": 50, "p2_to_p3_hit_rounds": 2}},
    )

    assert decision.priority == PRIORITY_HIGH_FREQ
    assert "spread=0.2000" in decision.reason


def test_arbitrage_scanner_emits_steam_buy_opportunities(monkeypatch):
    captured = []

    class FakeSession:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    scanner = ArbitrageScanner(session_factory=FakeSession)
    monkeypatch.setattr(
        scanner,
        "_load_latest_prices",
        lambda session: {
            1: {
                "steam": SimpleNamespace(sell_min=10.0, buy_max=9.0),
                "buff": SimpleNamespace(sell_min=30.0, buy_max=20.0),
            }
        },
    )
    monkeypatch.setattr(scanner, "_bulk_upsert", lambda session, opportunities: captured.extend(opportunities))

    opportunities = scanner.scan_opportunities()

    assert len(opportunities) == 1
    assert opportunities[0]["buy_platform"] == "steam"
    assert opportunities[0]["sell_platform"] == "buff"
    assert captured == opportunities
    assert "steam" in SUPPORTED_BUY_PLATFORMS


def test_arbitrage_scanner_keeps_profitable_cashout_below_nominal_steam_price(monkeypatch):
    captured = []

    class FakeSession:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    scanner = ArbitrageScanner(session_factory=FakeSession)
    monkeypatch.setattr(
        scanner,
        "_load_latest_prices",
        lambda session: {
            1: {
                "steam": SimpleNamespace(sell_min=100.0, buy_max=0.0),
                "buff": SimpleNamespace(sell_min=95.0, buy_max=80.0),
            }
        },
    )
    monkeypatch.setattr(scanner, "_bulk_upsert", lambda session, opportunities: captured.extend(opportunities))
    monkeypatch.setattr(
        "DataEngine.arbitrage_engine.load_pipeline_config",
        lambda: {"pipeline": {"steam_balance_cost_ratio": 0.6, "max_discount": 0.5}},
    )

    opportunities = scanner.scan_opportunities()

    assert len(opportunities) == 1
    assert opportunities[0]["buy_platform"] == "steam"
    assert opportunities[0]["sell_platform"] == "buff"
    assert opportunities[0]["profit_rate"] > 0


def test_fetch_all_platforms_commits_each_platform_as_it_finishes(monkeypatch):
    committed = []

    class FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_platform_fetch(name, coro, item_count, timeout):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        if name == "buff":
            await asyncio.sleep(0.02)
        rows = [{"item_id": 1, "platform_name": name}] if item_count else []
        return rows, "ok", "", 0.01

    monkeypatch.setattr(main_engine, "AsyncSession", lambda **kwargs: FakeAsyncSession())
    monkeypatch.setattr(main_engine, "_run_platform_fetch", fake_run_platform_fetch)

    async def run_case():
        return await main_engine._fetch_all_platforms(
            buff_items=[{"item_id": 1}],
            uuyp_items=[{"item_id": 1}],
            eco_items=[],
            steam_items=[],
            fast=False,
            on_platform_result=lambda name, rows: committed.append((name, rows)),
        )

    results = asyncio.run(run_case())

    assert {name for name, _ in committed} == {"buff", "uuyp", "eco", "steam"}
    assert any(row["platform_name"] == "uuyp" for row in results)


def test_fetch_all_platforms_does_not_import_steam_when_no_steam_items(monkeypatch):
    imported = []

    class FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_platform_fetch(name, coro, item_count, timeout):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return [], "ok", "", 0.01

    real_import = __import__

    def guard_import(name, *args, **kwargs):
        if name == "DataEngine.steam_public_monitor":
            imported.append(name)
            raise AssertionError("steam monitor should not be imported without steam items")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(main_engine, "AsyncSession", lambda **kwargs: FakeAsyncSession())
    monkeypatch.setattr(main_engine, "_run_platform_fetch", fake_run_platform_fetch)
    monkeypatch.setattr("builtins.__import__", guard_import)

    async def run_case():
        return await main_engine._fetch_all_platforms(
            buff_items=[],
            uuyp_items=[],
            eco_items=[],
            steam_items=[],
            fast=True,
        )

    assert asyncio.run(run_case()) == []
    assert imported == []


def test_fetch_all_platforms_uses_relaxed_buff_jit_timeout(monkeypatch):
    observed = []

    class FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_platform_fetch(name, coro, item_count, timeout):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        observed.append((name, timeout))
        return [], "ok", "", 0.01

    monkeypatch.setattr(main_engine, "AsyncSession", lambda **kwargs: FakeAsyncSession())
    monkeypatch.setattr(main_engine, "_run_platform_fetch", fake_run_platform_fetch)

    async def run_case():
        return await main_engine._fetch_all_platforms(
            buff_items=[{"item_id": 1, "platform_id": "1"}],
            uuyp_items=[],
            eco_items=[],
            steam_items=[],
            fast=True,
        )

    asyncio.run(run_case())

    assert ("buff", 25) in observed


def test_fetch_all_platforms_propagates_user_stop(monkeypatch):
    class FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_platform_fetch(name, coro, item_count, timeout):
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        if name == "buff":
            raise StopRequested("DataEngine stop requested")
        await asyncio.sleep(0.05)
        return [{"item_id": 1, "platform_name": name}], "ok", "", 0.01

    monkeypatch.setattr(main_engine, "AsyncSession", lambda **kwargs: FakeAsyncSession())
    monkeypatch.setattr(main_engine, "_run_platform_fetch", fake_run_platform_fetch)

    async def run_case():
        return await main_engine._fetch_all_platforms(
            buff_items=[{"item_id": 1}],
            uuyp_items=[{"item_id": 1}],
            eco_items=[{"item_id": 1}],
            steam_items=[{"item_id": 1}],
            fast=False,
            on_platform_result=lambda name, rows: pytest.fail("stop should abort before commit callback"),
        )

    with pytest.raises(StopRequested):
        asyncio.run(run_case())


def test_run_platform_fetch_timeout_logs_and_skips(monkeypatch):
    async def slow_fetch():
        await asyncio.sleep(0.05)
        return [{"item_id": 1}]

    async def run_case():
        return await main_engine._run_platform_fetch("buff", slow_fetch(), 1, timeout=0.001)

    warnings: list[str] = []
    original_warning = main_engine.logger.warning

    def capture_warning(message, *args, **kwargs):
        rendered = str(message) % args if args else str(message)
        warnings.append(rendered)
        return original_warning(message, *args, **kwargs)

    monkeypatch.setattr(main_engine.logger, "warning", capture_warning)
    result = asyncio.run(run_case())

    assert result[0] == []
    assert result[1] == "timeout"
    assert any("platform fetch timeout, skipped" in message for message in warnings)


def test_fetch_buff_prices_keeps_partial_results_when_one_item_times_out(monkeypatch):
    async def fake_fetch_single(item, session, fast=False):
        if str(item.get("platform_id")) == "slow":
            await asyncio.sleep(0.05)
            return {"item_id": 2, "platform_name": "buff"}
        await asyncio.sleep(0)
        return {"item_id": 1, "platform_name": "buff"}

    monkeypatch.setattr(buff_public_monitor, "_fetch_single_buff_item", fake_fetch_single)
    monkeypatch.setattr(buff_public_monitor, "raise_if_stop_requested", lambda: None)
    monkeypatch.setattr(buff_public_monitor, "_buff_circuit_is_open", lambda: False)
    monkeypatch.setattr(buff_public_monitor, "JIT_TIMEOUT", 0.01)

    async def run_case():
        return await buff_public_monitor.fetch_buff_prices(
            [{"item_id": 1, "platform_id": "ok"}, {"item_id": 2, "platform_id": "slow"}],
            SimpleNamespace(),
            fast=True,
        )

    result = asyncio.run(run_case())

    assert result == [{"item_id": 1, "platform_name": "buff"}]


def test_fetch_single_uuyp_item_refreshes_stale_template_mapping(monkeypatch):
    payloads = {
        "old": [{"CommodityHashName": "Sticker | huNter- | Paris 2023", "Price": 31.97, "OnSaleCount": 26467}],
        "new": [{"CommodityHashName": "Nova | Wood Fired (Battle-Scarred)", "Price": 3.2, "OnSaleCount": 12}],
    }
    calls = []
    persisted = []

    async def fake_fetch_uuyp_item_app(template_id, session):
        calls.append(str(template_id))
        return payloads.get(str(template_id))

    monkeypatch.setattr(uuyp_public_monitor, "fetch_uuyp_item_app", fake_fetch_uuyp_item_app)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "fetch_uuyp_template_detail_name",
        lambda template_id, session: asyncio.sleep(0, result={
            "old": "Sticker | huNter- | Paris 2023",
            "new": "Nova | Wood Fired (Battle-Scarred)",
        }.get(str(template_id))),
    )
    monkeypatch.setattr(uuyp_public_monitor, "_lookup_local_uuyp_template_id", lambda item_id, current_template_id: "new")
    monkeypatch.setattr(
        uuyp_public_monitor,
        "_persist_uuyp_template_mapping",
        lambda item_id, market_hash_name, resolved_template_id: persisted.append(
            (item_id, market_hash_name, resolved_template_id)
        ),
    )

    async def run_case():
        return await uuyp_public_monitor._fetch_single_uuyp_item(
            {
                "item_id": 8147,
                "platform_id": "old",
                "market_hash_name": "Nova | Wood Fired (Battle-Scarred)",
            },
            SimpleNamespace(),
        )

    result = asyncio.run(run_case())

    assert calls == ["new"]
    assert persisted == [(8147, "Nova | Wood Fired (Battle-Scarred)", "new")]
    assert result is not None
    assert result["item_id"] == 8147
    assert result["sell_min"] == 3.2
    assert result["volume"] == 12


def test_fetch_single_uuyp_item_skips_mismatched_quote_when_refresh_still_wrong(monkeypatch):
    calls = []

    async def fake_fetch_uuyp_item_app(template_id, session):
        calls.append(str(template_id))
        return [{"CommodityHashName": "Sticker | mir | Copenhagen 2024", "Price": 102.4, "OnSaleCount": 99}]

    monkeypatch.setattr(uuyp_public_monitor, "fetch_uuyp_item_app", fake_fetch_uuyp_item_app)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "fetch_uuyp_template_detail_name",
        lambda template_id, session: asyncio.sleep(0, result={
            "old": "Sticker | huNter- | Paris 2023",
            "new": "Sticker | latto | Copenhagen 2024",
        }.get(str(template_id))),
    )
    monkeypatch.setattr(uuyp_public_monitor, "_lookup_local_uuyp_template_id", lambda item_id, current_template_id: "new")
    monkeypatch.setattr(uuyp_public_monitor, "_persist_uuyp_template_mapping", lambda *args, **kwargs: pytest.fail("should not persist bad mapping"))

    async def run_case():
        return await uuyp_public_monitor._fetch_single_uuyp_item(
            {
                "item_id": 26169,
                "platform_id": "old",
                "market_hash_name": "Sealed Graffiti | Heart (Blood Red)",
            },
            SimpleNamespace(),
        )

    result = asyncio.run(run_case())

    assert calls == []
    assert result is None


def test_fetch_single_uuyp_item_skips_when_steamdt_lookup_fails(monkeypatch):
    calls = []

    async def fake_fetch_uuyp_item_app(template_id, session):
        calls.append(str(template_id))
        return [{"CommodityHashName": "Sticker | mir | Copenhagen 2024", "Price": 102.4, "OnSaleCount": 99}]

    monkeypatch.setattr(uuyp_public_monitor, "fetch_uuyp_item_app", fake_fetch_uuyp_item_app)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "fetch_uuyp_template_detail_name",
        lambda template_id, session: asyncio.sleep(0, result="Sticker | huNter- | Paris 2023"),
    )
    monkeypatch.setattr(uuyp_public_monitor, "_lookup_local_uuyp_template_id", lambda item_id, current_template_id: None)
    monkeypatch.setattr(uuyp_public_monitor, "_persist_uuyp_template_mapping", lambda *args, **kwargs: pytest.fail("should not persist when steamdt lookup fails"))

    async def run_case():
        return await uuyp_public_monitor._fetch_single_uuyp_item(
            {
                "item_id": 26169,
                "platform_id": "old",
                "market_hash_name": "Sealed Graffiti | Heart (Blood Red)",
            },
            SimpleNamespace(),
        )

    result = asyncio.run(run_case())

    assert calls == []
    assert result is None


def test_fetch_single_uuyp_item_skips_when_verified_template_still_returns_wrong_quote(monkeypatch):
    async def fake_fetch_uuyp_item_app(template_id, session):
        return [{"CommodityHashName": "Sticker | latto | Copenhagen 2024", "Price": 88.8, "OnSaleCount": 42}]

    monkeypatch.setattr(uuyp_public_monitor, "fetch_uuyp_item_app", fake_fetch_uuyp_item_app)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "fetch_uuyp_template_detail_name",
        lambda template_id, session: asyncio.sleep(0, result="Nova | Wood Fired (Battle-Scarred)"),
    )
    monkeypatch.setattr(
        uuyp_public_monitor,
        "_lookup_local_uuyp_template_id",
        lambda item_id, current_template_id: pytest.fail("local fallback should not be called"),
    )

    async def run_case():
        return await uuyp_public_monitor._fetch_single_uuyp_item(
            {
                "item_id": 8147,
                "platform_id": "1397",
                "market_hash_name": "Nova | Wood Fired (Battle-Scarred)",
            },
            SimpleNamespace(),
        )

    result = asyncio.run(run_case())

    assert result is None


def test_fetch_uuyp_item_app_uses_pc_sale_list_endpoint(monkeypatch):
    observed = {}

    async def fake_post_uuyp_json(request_name, url, payload, template_id, session):
        observed["request_name"] = request_name
        observed["url"] = url
        observed["payload"] = dict(payload)
        observed["template_id"] = template_id
        return (
            {
                "Code": 0,
                "Msg": "success",
                "Data": [{"commodityHashName": "Nova | Wood Fired (Battle-Scarred)", "price": "2.98"}],
            },
            SimpleNamespace(status_code=200, text="ok", headers={}),
        )

    monkeypatch.setattr(uuyp_public_monitor, "_post_uuyp_json", fake_post_uuyp_json)
    monkeypatch.setattr(uuyp_public_monitor, "raise_if_stop_requested", lambda: None)

    async def run_case():
        return await uuyp_public_monitor.fetch_uuyp_item_app("1397", SimpleNamespace())

    result = asyncio.run(run_case())

    assert observed["request_name"] == "sale list"
    assert observed["url"].endswith("/api/homepage/pc/goods/market/queryOnSaleCommodityList")
    assert observed["payload"]["templateId"] == "1397"
    assert observed["payload"]["gameId"] == "730"
    assert observed["payload"]["listType"] == "10"
    assert result[0]["commodityHashName"] == "Nova | Wood Fired (Battle-Scarred)"


def test_uuyp_403_does_not_open_auth_circuit(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 403
        text = "forbidden"
        headers = {}

        def json(self):
            return {}

    class FakeSession:
        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(uuyp_public_monitor, "_auth_circuit_open_until", 0.0)
    monkeypatch.setattr(uuyp_public_monitor, "raise_if_stop_requested", lambda: None)
    monkeypatch.setattr(
        uuyp_public_monitor,
        "get_request_proxies",
        lambda **kwargs: {"http": "http://u:p@10.0.0.9:8080/", "https": "http://u:p@10.0.0.9:8080/"},
    )
    monkeypatch.setattr(
        uuyp_public_monitor,
        "mark_proxy_failure",
        lambda proxies, reason, cooldown_seconds: calls.append((reason, cooldown_seconds)) or True,
    )

    async def run_case():
        return await uuyp_public_monitor.fetch_uuyp_item_app("1397", FakeSession())

    result = asyncio.run(run_case())

    assert result is None
    assert calls
    assert calls[-1][0] == "uuyp_blocked"
    assert not uuyp_public_monitor.is_uuyp_auth_circuit_open()


def test_buff_403_cools_proxy_and_opens_circuit(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 403

    class FakeSession:
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(buff_public_monitor, "_circuit_failures", 0)
    monkeypatch.setattr(buff_public_monitor, "_circuit_open_until", 0.0)
    monkeypatch.setattr(buff_public_monitor, "raise_if_stop_requested", lambda: None)
    monkeypatch.setattr(buff_public_monitor, "get_request_proxies", lambda **kwargs: {"http": "http://u:p@10.0.0.3:8080/", "https": "http://u:p@10.0.0.3:8080/"})
    monkeypatch.setattr(buff_public_monitor, "mark_proxy_failure", lambda proxies, reason, cooldown_seconds: calls.append((reason, cooldown_seconds)) or True)

    async def run_case():
        for _ in range(buff_public_monitor.CIRCUIT_FAILURE_THRESHOLD):
            await buff_public_monitor.fetch_order_book(FakeSession(), 123, "sell_order", max_retries=0)

    asyncio.run(run_case())

    assert calls
    assert calls[-1][0] == "buff_blocked"
    assert calls[-1][1] == 600
    assert buff_public_monitor.is_buff_circuit_open()


def test_steam_reset_cools_proxy_and_opens_circuit(monkeypatch):
    calls = []
    item = {"item_id": 1, "hash_name": "AK-47 | Slate (Field-Tested)"}

    monkeypatch.setitem(steam_public_monitor.steam_dict, item["hash_name"], {"name_id": "123"})
    monkeypatch.setattr(steam_public_monitor, "_circuit_failures", 0)
    monkeypatch.setattr(steam_public_monitor, "_circuit_open_until", 0.0)
    monkeypatch.setattr(steam_public_monitor, "raise_if_stop_requested", lambda: None)
    monkeypatch.setattr(steam_public_monitor, "get_proxy", lambda: None)
    monkeypatch.setattr(steam_public_monitor, "get_request_proxies", lambda **kwargs: {"http": "http://u:p@10.0.0.4:8080/", "https": "http://u:p@10.0.0.4:8080/"})
    monkeypatch.setattr(steam_public_monitor, "mark_proxy_failure", lambda proxies, reason, cooldown_seconds: calls.append((reason, cooldown_seconds)) or True)

    async def failing_request(*args, **kwargs):
        raise RuntimeError("Recv failure: Connection was reset")

    monkeypatch.setattr(steam_public_monitor, "_request_steam_histogram", failing_request)

    async def run_case():
        for _ in range(steam_public_monitor.CIRCUIT_FAILURE_THRESHOLD):
            await steam_public_monitor._fetch_single_steam_item(item, SimpleNamespace(), retry_count=0, request_timeout=1)

    asyncio.run(run_case())

    assert calls
    assert calls[-1][0] == "connection_reset"
    assert calls[-1][1] == 180
    assert steam_public_monitor.is_steam_circuit_open()


def test_request_steam_histogram_disables_ssl_verification(monkeypatch):
    observed = {}

    class FakeSession:
        async def get(self, url, **kwargs):
            observed["url"] = url
            observed["kwargs"] = kwargs
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(steam_public_monitor, "get_proxy", lambda: None)

    async def run_case():
        return await steam_public_monitor._request_steam_histogram(
            FakeSession(),
            "https://steamcommunity.com/test",
            {"X-Test": "1"},
            timeout=7,
            proxies={"http": "http://proxy/", "https": "http://proxy/"},
        )

    asyncio.run(run_case())

    assert observed["url"] == "https://steamcommunity.com/test"
    assert observed["kwargs"]["verify"] is False
    assert observed["kwargs"]["timeout"] == 7
    assert observed["kwargs"]["proxies"]["http"] == "http://proxy/"


def test_proxy_auth_407_state_transition(monkeypatch, tmp_path):
    state_path = tmp_path / "proxy_health_state.json"
    proxy_cfg = {"host": "10.0.0.5", "port": 8080, "username": "u", "password": "p"}
    monkeypatch.setattr("utils.proxy_manager.PROXY_HEALTH_STATE_PATH", state_path)
    monkeypatch.setattr("utils.proxy_manager._load_proxy_pool_cfg", lambda: {"enabled": True, "strategy": 2, "proxies": [proxy_cfg]})

    manager = ProxyManager()
    manager.mark_proxy_failure(_build_proxy_url(proxy_cfg), reason="proxy_auth", cooldown_seconds=900)
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["10.0.0.5:8080"]["last_failure_reason"] == "proxy_auth"
    assert manager.get_next_proxy_dict() is None


def test_platform_client_factory_supports_steam(tmp_path):
    from app.services.platform_sessions import PlatformClientFactory, PlatformSessionStateStore
    from app.services.steam_buyer import SteamBuyer

    credentials = {
        "steam": {
            "cookies": "sessionid=sid123; steamLoginSecure=token",
            "sessionid": "sid123",
        }
    }
    factory = PlatformClientFactory(
        credentials=credentials,
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("steam", purpose="auto_buy")
    client, _, _ = factory.client("steam", purpose="auto_buy")

    assert preflight.ok
    assert isinstance(client, SteamBuyer)


def test_action_policy_selects_segment_by_price():
    cfg = {
        "action_policy": {
            "risk_segments": [
                {"min_price": 0, "max_price": 10, "max_capital_per_item": 80, "max_inventory_per_item": 8},
                {"min_price": 10, "max_price": 100, "max_capital_per_item": 300, "max_inventory_per_item": 3},
            ]
        }
    }

    seg = select_risk_segment(12, cfg["action_policy"])

    assert seg.max_capital_per_item == 300
    assert seg.max_inventory_per_item == 3


def test_action_policy_blocks_when_segment_capital_limit_exceeded():
    opportunity = SimpleNamespace(
        id=1,
        item_id=42,
        buy_platform="buff",
        sell_platform="steam",
        buy_price=50.0,
        sell_price=80.0,
    )
    item = SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)")
    prices = {"buff": SimpleNamespace(volume=100), "steam": SimpleNamespace(volume=100)}

    decision = compute_action_decision(
        opportunity,
        item,
        prices,
        config={
            "action_policy": {
                "min_24h_volume": 20,
                "risk_segments": [{"min_price": 0, "max_price": None, "max_capital_per_item": 40, "max_inventory_per_item": 10}],
            }
        },
    )

    assert decision.action == ACTION_BLOCKED
    assert "capital_limit" in decision.risk_flags


def test_action_policy_direct_buy_requires_jit():
    opportunity = SimpleNamespace(
        id=2,
        item_id=42,
        buy_platform="buff",
        sell_platform="steam",
        buy_price=50.0,
        sell_price=100.0,
    )
    item = SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)")
    prices = {"buff": SimpleNamespace(volume=100), "steam": SimpleNamespace(volume=100)}

    decision = compute_action_decision(
        opportunity,
        item,
        prices,
        config={"action_policy": {"min_24h_volume": 20, "allow_buy_order": False, "direct_buy_min_profit_rate": 0.05}},
    )

    assert decision.action == ACTION_DIRECT_BUY
    assert decision.requires_jit


def test_action_policy_buy_order_can_skip_jit():
    opportunity = SimpleNamespace(
        id=3,
        item_id=42,
        buy_platform="eco",
        sell_platform="steam",
        buy_price=50.0,
        sell_price=100.0,
    )
    item = SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)")
    prices = {"eco": SimpleNamespace(volume=100), "steam": SimpleNamespace(volume=100)}

    decision = compute_action_decision(
        opportunity,
        item,
        prices,
        config={
            "action_policy": {
                "min_24h_volume": 20,
                "allow_direct_buy": False,
                "allow_buy_order": True,
                "buy_order_min_profit_rate": 0.1,
            }
        },
    )

    assert decision.action == ACTION_CREATE_BUY_ORDER
    assert not decision.requires_jit


def test_action_policy_accepts_openapi_orderbook_depth_without_24h_volume():
    opportunity = SimpleNamespace(
        id=5,
        item_id=42,
        buy_platform="steam",
        sell_platform="buff",
        buy_price=100.0,
        sell_price=80.0,
    )
    item = SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)")
    prices = {
        "steam": SimpleNamespace(volume=0, orderbook_depth=0, sell_volume=10, buy_volume=0, liquidity_score=0),
        "buff": SimpleNamespace(volume=0, orderbook_depth=30, sell_volume=10, buy_volume=20, liquidity_score=2.0),
    }

    decision = compute_action_decision(
        opportunity,
        item,
        prices,
        config={
            "pipeline": {"steam_balance_cost_ratio": 0.6},
            "action_policy": {"min_24h_volume": 20, "allow_buy_order": False, "direct_buy_min_profit_rate": 0.05},
        },
    )

    assert decision.action == ACTION_DIRECT_BUY
    assert decision.expected_profit_rate > 0


def test_action_policy_accepts_flattened_config_from_scheduler():
    cfg = action_policy_config({"allow_direct_buy": False, "min_24h_volume": 5})

    assert cfg["allow_direct_buy"] is False
    assert cfg["min_24h_volume"] == 5


def test_action_policy_prefers_best_scored_candidate():
    opportunity = SimpleNamespace(
        id=4,
        item_id=42,
        buy_platform="eco",
        sell_platform="steam",
        buy_price=50.0,
        sell_price=100.0,
    )
    item = SimpleNamespace(id=42, market_hash_name="AK-47 | Slate (Field-Tested)")
    prices = {"eco": SimpleNamespace(volume=100), "steam": SimpleNamespace(volume=100)}

    decision = compute_action_decision(
        opportunity,
        item,
        prices,
        config={
            "action_policy": {
                "min_24h_volume": 20,
                "allow_direct_buy": True,
                "allow_buy_order": True,
                "direct_buy_min_profit_rate": 0.05,
                "buy_order_min_profit_rate": 0.1,
                "direct_buy_score_weight": 0.1,
                "buy_order_score_weight": 2.0,
                "jit_cost_penalty": 0.5,
            }
        },
    )

    assert decision.action == ACTION_CREATE_BUY_ORDER
    assert not decision.requires_jit


def test_action_policy_exposure_uses_item_id_before_name(monkeypatch):
    monkeypatch.setattr(
        "app.state.get_purchases",
        lambda: [{"item_id": 42, "name": "renamed", "price": 10.0, "quantity": 2}],
    )
    monkeypatch.setattr(
        "app.state.get_inventory",
        lambda: [{"item_id": 42, "market_hash_name": "other", "price": 8.0, "quantity": 1}],
    )
    monkeypatch.setattr(
        "app.state.get_plan",
        lambda: [{"item_id": 42, "action": "buy_order", "status": "open", "target_price": 7.0, "quantity": 3}],
    )

    holding = estimate_holding_state(42, "AK-47 | Slate (Field-Tested)")

    assert holding.quantity == 6
    assert holding.capital_cny == 49.0


def test_save_action_decisions_does_not_reopen_terminal_before_expiry():
    existing = ActionDecision(
        opportunity_id=10,
        item_id=42,
        action=ACTION_DIRECT_BUY,
        target_platform="buff",
        sell_platform="steam",
        target_price=50.0,
        reference_price=100.0,
        quantity=1,
        score=1.0,
        expected_profit_cny=20.0,
        expected_profit_rate=0.4,
        requires_jit=True,
        status="failed",
        reason="executor=jit_validation_failed",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    rows: list[ActionDecision] = [existing]

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return self.rows[0] if self.rows else None

        def first(self):
            return self.rows[0] if self.rows else None

    class FakeSession:
        def __init__(self):
            self.added = []
            self.committed = False

        def query(self, model):
            return Query(rows)

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            self.committed = True

    decision = ActionPolicyDecision(
        opportunity_id=10,
        item_id=42,
        action=ACTION_DIRECT_BUY,
        target_platform="buff",
        sell_platform="steam",
        target_price=50.2,
        reference_price=100.0,
        quantity=1,
        score=2.0,
        expected_profit_cny=21.0,
        expected_profit_rate=0.42,
        requires_jit=True,
        status="open",
        reason="new",
        expires_at=datetime.now() + timedelta(minutes=15),
    )

    saved = save_action_decisions(
        FakeSession(),
        [decision],
        config={"action_policy": {"decision_reopen_price_change_rate": 0.02}},
    )

    assert saved == 1
    assert existing.status == "failed"
    assert existing.target_price == 50.2
    assert existing.expires_at is not None
    assert existing.expires_at < decision.expires_at
