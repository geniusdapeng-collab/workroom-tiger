"""v6.0 架构与投资逻辑增强的回归锁定。

覆盖：
1. 统一出场引擎 exit_engine（journal/回测/模拟盘同一实现 + 成本净口径）
2. 统一闸门 gate.py（生产/回测同一实现）
3. 事件日历 events.py（FOMC/CPI/非农/OPEX/财报折扣）
4. 组合层风控（链级敞口上限 / 总敞口执行）
5. MRS 冲击与流动性折扣（Kill Switch 机器执行）
6. 结构化止损价（消灭正则解析）
7. 交易日历 calendar.py
8. demo 剧本化（riskoff → AVOID）
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import config


# ---------------------------------------------------------------- 1. 统一出场引擎

def test_exit_engine_stop_priority_and_gap():
    from trading_system.exit_engine import simulate_trade
    # 入场 100（索引0开盘），止损 95；索引2 跳空 94 开盘 → 按开盘价成交（净口径）
    o = [100.0, 99.0, 94.0, 96.0]
    h = [101.0, 100.0, 95.0, 97.0]
    l = [99.0, 98.0, 93.0, 95.0]
    c = [100.0, 99.0, 94.5, 96.5]
    res = simulate_trade(o, h, l, c, 0, 95.0, time_stop=7, cost_bps=0)
    assert res.exit_i == 2 and res.exit_price == 94.0
    assert res.r == pytest.approx((94.0 - 100.0) / 5.0)


def test_exit_engine_cost_model():
    from trading_system.exit_engine import simulate_trade
    o = [100.0] + [102.0] * 9
    h = [101.0] + [103.0] * 9
    l = [99.0] + [101.0] * 9
    c = [100.5] + [102.5] * 9
    gross = simulate_trade(o, h, l, c, 0, 95.0, time_stop=7, cost_bps=0)
    net = simulate_trade(o, h, l, c, 0, 95.0, time_stop=7, cost_bps=10)
    assert net.r < gross.r                       # 成本必然侵蚀 R
    entry_net, exit_net = 100 * 1.001, 102.5 * 0.999
    assert net.r == pytest.approx((exit_net - entry_net) / (entry_net - 95.0))


def test_exit_engine_void_on_gap_through_stop():
    from trading_system.exit_engine import simulate_trade
    res = simulate_trade([94.0, 95.0], [95.0, 96.0], [93.0, 94.0], [94.0, 95.5],
                         0, 95.0, cost_bps=0)
    assert res.void and res.reason.startswith("开盘即破止损")


def test_journal_and_backtest_share_engine():
    """journal 结算与回测必须走同一个出场引擎（架构红线：禁止第三套实现）。"""
    import inspect

    from trading_system import backtest, journal
    assert "exit_engine" in inspect.getsource(journal.Journal._settle_one)
    assert "exit_engine" in inspect.getsource(backtest._simulate_trade)


# ---------------------------------------------------------------- 2. 统一闸门

def test_gate_pass_gates_matches_whitepaper():
    from trading_system.gate import main_pool_eligible, pass_gates
    # 标准做多：MRS≥6 且主线 且 TSS≥7.2
    d = pass_gates(6.5, 8.0, 7.5, in_main=True, in_sub=False, chain_hot=False)
    assert d.passed and d.standard
    # 轻仓：MRS 5.5-6.0 且 TSS≥7.8
    d = pass_gates(5.7, 6.0, 7.9, in_main=False, in_sub=False, chain_hot=False)
    assert d.passed and not d.standard
    # TSS 不足：拒绝
    d = pass_gates(6.5, 8.0, 7.0, in_main=True, in_sub=False, chain_hot=False)
    assert not d.passed
    # 广度缺失不得进主线池
    assert not main_pool_eligible(8.0, float("nan"))
    assert main_pool_eligible(8.0, 65.0)
    assert not main_pool_eligible(8.0, 45.0)


def test_risk_manager_uses_shared_gate():
    import inspect

    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    assert "pass_gates" in inspect.getsource(RiskManagerAgent.execute)


# ---------------------------------------------------------------- 3. 事件日历

def test_event_calendar_macro_and_opex():
    from trading_system.events import EventCalendar
    cal = EventCalendar(override_path="/nonexistent.json")
    assert "FOMC" in cal.macro_events("2026-06-17")
    assert "CPI" in cal.macro_events("2026-08-12")
    assert "NFP" in cal.macro_events("2026-08-07")      # 8 月第一个周五
    assert cal.macro_events("2026-08-29") == []
    # OPEX：2026-08-21 是第三个周五
    assert cal.is_opex_week("2026-08-19")
    assert not cal.is_opex_week("2026-08-26")
    f1, notes1 = cal.day_discount("2026-06-17")
    assert f1 < 1.0 and notes1
    f2, _ = cal.day_discount("2026-08-26")
    assert f2 == 1.0


def test_event_calendar_earnings_discount():
    from trading_system.events import EventCalendar
    cal = EventCalendar(override_path="/nonexistent.json")
    cal.register_earnings("NVDA", "2026-08-30")
    factor, note = cal.pick_adjustment("NVDA", "2026-08-29")
    assert factor == config.EVENT_EARNINGS_SIZE and "财报" in note
    factor, note = cal.pick_adjustment("NVDA", "2026-08-20")
    assert factor == 1.0 and note == ""


# ---------------------------------------------------------------- 4. 组合层风控

def _ctx(mrs_star=8.0, cap=(0.7, 0.9), shock=False):
    from trading_system.data_models import MRSResult
    mrs = MRSResult(mrs_raw=mrs_star, delta=2.0, k=1.0, mrs_star=mrs_star,
                    regime="黄金共振", position_cap=cap,
                    allow_new_positions=not shock, shock=shock,
                    shock_reason="测试冲击" if shock else "")
    return mrs


def _cand(ticker, chain, tss=8.0, price=100.0, stop=95.0):
    from trading_system.data_models import StockCandidate
    c = StockCandidate(ticker=ticker, chain_id=chain, price=price,
                       adv_usd=1e8, atr_pct=0.03, tss_final=tss, tss=tss,
                       c_liq=1.0, key_level=99.0, stop_price=stop,
                       entry_template="A", stop_plan="测试")
    return c


def test_chain_exposure_cap():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    from trading_system.data_models import SectorScore

    mrs = _ctx()
    # 6 只同链候选（semis→SMH），全部过闸
    cands = [_cand(f"T{i}", "semis") for i in range(6)]
    sector = SectorScore(etf="SMH", shs=8.0, breadth=70.0, in_main_pool=True)
    ctx = {"mrs": mrs, "sectors": [sector], "watchlist": cands,
           "chain_map": {}, "event_calendar": None, "trade_date": "2026-08-28"}
    agent = RiskManagerAgent(None, account_usd=100_000, max_picks=7)
    picks = agent.execute(ctx)
    # 单链风险预算 3% × 100k = 3000；每笔 r=0.8%×100k=800 → 最多 3 只
    assert len(picks) == 3
    assert any("产业链敞口上限" in n for n in ctx["notes"])


def test_gross_cap_enforced():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    from trading_system.data_models import SectorScore

    # MRS* 8.5 → cap (0.7,0.9)，但构造 position 20%×n → 总敞口 ≤ 90%
    mrs = _ctx(cap=(0.30, 0.40))           # 人为压低上限验证执行
    cands = [_cand(f"T{i}", "semis", price=100.0, stop=99.0) for i in range(6)]
    sector = SectorScore(etf="SMH", shs=8.0, breadth=70.0, in_main_pool=True)
    ctx = {"mrs": mrs, "sectors": [sector], "watchlist": cands,
           "chain_map": {}, "event_calendar": None, "trade_date": "2026-08-28"}
    agent = RiskManagerAgent(None, account_usd=100_000, max_picks=7)
    # 链预算 3000 / 每股风险 1 → 每股 800 股×100 = 80000... 先过链 cap：800/笔 → 3 只
    picks = agent.execute(ctx)
    total = sum(p.position_pct for p in picks)
    assert total <= 0.40 + 1e-9


def test_shock_blocks_new_positions():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent

    ctx = {"mrs": _ctx(shock=True), "sectors": [], "watchlist": [],
           "chain_map": {}, "event_calendar": None, "trade_date": "2026-08-28"}
    picks = RiskManagerAgent(None).execute(ctx)
    assert picks == [] and ctx["action"] == "AVOID"
    assert "Kill Switch" in ctx["market_view"]


def test_mrs_shock_detection():
    import numpy as np

    from trading_system.agents.mrs_agent import MRSAgent
    idx = pd.bdate_range("2026-08-24", periods=5)
    spy = pd.DataFrame({"Open": [100]*5, "High": [101]*5, "Low": [94]*5,
                        "Close": [100.0, 100.0, 100.0, 100.0, 95.5],  # -4.5%
                        "Volume": [1]*5}, index=idx)
    vix = pd.Series([15.0]*5, index=idx)
    shock, reason = MRSAgent._detect_shock(spy, vix)
    assert shock and "SPY" in reason
    vix_spike = pd.Series([15.0, 15.0, 15.0, 15.0, 20.0], index=idx)  # +33%
    spy_ok = spy.copy(); spy_ok["Close"] = 100.0
    shock2, reason2 = MRSAgent._detect_shock(spy_ok, vix_spike)
    assert shock2 and "VIX" in reason2


# ---------------------------------------------------------------- 5. 结构化止损

def test_tss_sets_structured_stop_price():
    from trading_system.agents.tss_agent import TSSAgent
    from trading_system.data_models import StockCandidate

    f = {"atr14": 2.0, "key_level": 100.0, "close": 102.0, "sma20": 98.0}
    c = StockCandidate(ticker="X", entry_template="A")
    assert TSSAgent._stop_price(c, f) == 99.0          # key - 0.5ATR
    c.entry_template = "B"
    assert TSSAgent._stop_price(c, f) == 98.0          # key - 1.0ATR
    c.entry_template = "C"
    assert TSSAgent._stop_price(c, f) == 98.0          # min(sma20, close-1.5ATR)=min(98,99)
    c.entry_template = ""
    assert TSSAgent._stop_price(c, f) == 98.0          # close - 2ATR


# ---------------------------------------------------------------- 6. 交易日历

def test_market_calendar():
    from trading_system.calendar import (
        is_trading_day, next_trading_day, trading_days_between,
    )
    assert is_trading_day("2026-08-28")                 # 周五
    assert not is_trading_day("2026-08-29")             # 周六
    assert not is_trading_day("2026-11-26")             # 感恩节
    assert not is_trading_day("2026-07-03")             # 独立日（补休）
    assert str(next_trading_day("2026-11-26")) == "2026-11-27"
    assert trading_days_between("2026-08-24", "2026-08-28") == 4


# ---------------------------------------------------------------- 7. demo 剧本化

def test_demo_riskoff_scenario_avoids():
    from trading_system.pipeline import run_pipeline
    from trading_system.providers import get_provider

    prov = get_provider("demo")
    prov.scenario = "riskoff"
    import trading_system.pipeline as pl
    orig = pl.get_provider
    pl.get_provider = lambda name=None: prov
    try:
        r = run_pipeline(provider_name="demo", universe_mode="core",
                         top_n=8, max_picks=3)
    finally:
        pl.get_provider = orig
    assert r.action in ("AVOID", "WAIT", "HOLD")
    assert r.mrs.mrs_star < 6.0                         # 负向共振环境
