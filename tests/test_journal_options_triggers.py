"""信号日记 / 期权指标 / 触发器 / 全市场清单解析 / 再归一化 测试。"""

import math
from dataclasses import asdict

import pytest

from trading_system import config
from trading_system.indicators import aggregate


# ---------- 再归一化 ----------

def test_aggregate_renormalize():
    """缺失子项剔除后权重再归一化（v4.1 校准修复）。"""
    # 缺失 options(0.2) → 按 0.4/0.4 归一化：均值 8
    assert aggregate({"structure": 8, "momentum": 8, "options": None},
                     config.TSS_WEIGHTS) == 8.0
    # 全缺失 → 中性 5
    assert aggregate({"structure": None, "momentum": None, "options": None},
                     config.TSS_WEIGHTS) == config.NEUTRAL_SCORE
    # SHS：narr/micro 缺失 → (0.25m+0.35f)/0.6
    v = aggregate({"macro": 8, "flow": 8, "narr": None, "micro": None},
                  config.SHS_WEIGHTS)
    assert v == 8.0
    # 旧模式（renormalize=False）缺失当中性 5
    v2 = aggregate({"macro": 8, "flow": 8, "narr": None, "micro": None},
                   config.SHS_WEIGHTS, renormalize=False)
    assert math.isclose(v2, 6.8, abs_tol=0.01)   # 2+2.8+1.25+0.75


# ---------- 期权指标 ----------

def test_options_scores_with_history(tmp_path):
    from trading_system.options_metrics import OptionsHistoryStore, score_options
    from trading_system.providers.demo import DemoProvider

    store = OptionsHistoryStore(tmp_path / "oh")
    p = DemoProvider()
    # 预填 15 天历史（call_oi 递增 → 今日变化分位应高）
    snap = p.options_chain_snapshot("AAPL")
    hist = []
    for i in range(15):
        hist.append({"date": f"2026-07-{10 + i:02d}",
                     "pcr_oi": 0.8 + i * 0.01, "atm_iv": 0.3 + i * 0.005,
                     "call_oi": 100000.0 * (1 + i * 0.01)})
    (tmp_path / "oh").mkdir(parents=True)
    import json
    (tmp_path / "oh" / "AAPL.json").write_text(json.dumps(hist))

    res = score_options("AAPL", p, store=store)
    assert res["s_options"] is not None
    assert 0 <= res["s_options"] <= 10
    assert res["A"] is not None and res["B"] is not None and res["C"] is not None


def test_options_missing_returns_none():
    from trading_system.options_metrics import score_options
    from trading_system.providers.stooq import StooqProvider
    res = score_options("AAPL", StooqProvider())   # stooq 无期权链
    assert res["s_options"] is None
    assert "options_chain" in res["missing"]


def test_demo_options_deterministic():
    from trading_system.providers.demo import DemoProvider
    p = DemoProvider()
    s1 = p.options_chain_snapshot("NVDA")
    s2 = p.options_chain_snapshot("NVDA")
    assert s1 == s2
    assert 0.4 < s1["pcr_oi"] < 1.7
    assert 0.1 < s1["atm_iv"] < 1.0


# ---------- 信号日记 ----------

def test_journal_log_settle_stats(tmp_path):
    from trading_system.data_models import MRSResult, PipelineResult, TradePick
    from trading_system.journal import Journal
    from trading_system.providers.demo import DemoProvider

    j = Journal(tmp_path / "journal.json")
    pick = TradePick(ticker="NVDA", tss_final=7.8, tos=4.0, entry_template="A",
                     entry_price=100.0, stop_price=96.0, shares=200,
                     position_pct=0.2, risk_usd=800.0, card="标准做多")
    mrs = MRSResult(mrs_raw=7.0, delta=3.0, k=1.0, mrs_star=7.0)
    # 用一个过去的信号日，确保 settle 能入场
    result = PipelineResult(trade_date="2026-07-20", provider="demo",
                            mrs=mrs, picks=[pick], action="BUY")
    assert j.log_picks(result) == 1
    assert j.log_picks(result) == 0          # 同日同票去重

    settled = j.settle(DemoProvider())
    stats = j.stats()
    assert stats["closed"] + stats["open"] == 1
    if stats["closed"]:
        rec = j.records[0]
        assert rec["entry"] is not None and rec["exit"] is not None
        assert rec["entry_date"] > rec["date"]
        assert isinstance(rec["win"], bool)
        assert -3 < rec["r"] < 10


def test_journal_empty_stats(tmp_path):
    from trading_system.journal import Journal
    j = Journal(tmp_path / "j.json")
    s = j.stats()
    assert s["closed"] == 0 and s["win_rate"] == 0.0


# ---------- 触发器 ----------

class _StubProvider:
    name = "stub"

    def __init__(self, prices):
        self.prices = prices

    def quote(self, ticker):
        import pandas as pd
        return {"price": self.prices.get(ticker),
                "ts": pd.Timestamp.now().isoformat(), "kind": "realtime"}


def test_trigger_alerts():
    from trading_system.triggers import monitor_once
    watch = [{"ticker": "AAA", "entry": 100.0, "stop": 95.0, "protect": 110.0,
              "template": "A", "tss_final": 7.8}]
    # 保护位触发
    p = _StubProvider({"AAA": 110.5})
    alerts = monitor_once(watch, p)
    assert any(a["kind"] == "PROTECT" for a in alerts)
    # 止损触发
    p = _StubProvider({"AAA": 94.0})
    alerts = monitor_once(watch, p)
    assert any(a["kind"] == "STOP" for a in alerts)
    # 入场区触发（±0.5%）
    p = _StubProvider({"AAA": 100.3})
    alerts = monitor_once(watch, p)
    assert any(a["kind"] == "ENTRY" for a in alerts)
    # 去重：同轮同价位不再重复
    trig = set()
    a1 = monitor_once(watch, _StubProvider({"AAA": 94.0}), trig)
    a2 = monitor_once(watch, _StubProvider({"AAA": 94.0}), trig)
    assert len(a1) == 1 and len(a2) == 0


# ---------- 全市场清单解析 ----------

def test_symdir_parsing():
    from trading_system.universe import _clean_symbol, _parse_symdir
    nasdaq = "\n".join([
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares",
        "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
        "TESTX|Test Issue Corp|Q|Y|N|100|N|N",
        "ETFX|Some ETF Trust|Q|N|N|100|Y|N",
        "WRNT|Foo Warrant Merger|Q|N|N|100|N|N",
        "File Creation Time: x",
    ])
    out = _parse_symdir(nasdaq, 0, 3, 7, 1, include_etfs=False)
    assert out == ["AAPL"]                       # 测试股/ETF/权证全部被滤
    assert _clean_symbol("brk/b") == "BRK-B"
    assert _clean_symbol("WEIRD$$") is None
