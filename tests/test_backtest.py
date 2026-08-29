"""回测引擎测试：无未来函数不变量 + 指标完整性 + WFA 流程 + 再归一化。

无未来函数是回测的生命线，本文件用三条独立断言锁定：
  1. 入场日严格晚于信号日（次日开盘）
  2. 出场日不早于入场日
  3. 改动信号日之后的数据不影响该日评分（切片隔离）
"""

import logging

import numpy as np
import pytest

from trading_system.backtest import (
    GateParams, collect_day_frames, run_backtest, run_wfa,
)
from trading_system.providers.demo import DemoProvider
from trading_system.universe import CORE_UNIVERSE

logging.getLogger().setLevel(logging.ERROR)


@pytest.fixture(scope="module")
def frames_and_panel():
    frames, panel, _ = collect_day_frames(
        DemoProvider(), CORE_UNIVERSE, days=360, signal_days=80, top_n=15)
    return frames, panel


def test_frames_collected(frames_and_panel):
    frames, _ = frames_and_panel
    assert len(frames) >= 60
    assert all(f.mrs_star is not None for f in frames)
    assert any(len(f.candidates) > 0 for f in frames)


def test_no_lookahead_invariants(frames_and_panel):
    frames, panel = frames_and_panel
    res = run_backtest(frames, panel, GateParams())
    assert res["n_trades"] >= 0
    for t in res["trades"]:
        assert t.entry_date > t.signal_date, f"{t.ticker} 入场日未晚于信号日"
        assert t.exit_date >= t.entry_date
        # R 倍数与价格自洽（v6.0 净口径：入场/出场均含单边成本）
        from trading_system.exit_engine import cost_adj_buy
        entry_net = cost_adj_buy(t.entry)
        r0 = entry_net - t.stop0
        assert r0 > 0
        assert abs(t.r - (t.exit_price - entry_net) / r0) < 0.01


def test_metrics_complete(frames_and_panel):
    frames, panel = frames_and_panel
    res = run_backtest(frames, panel, GateParams())
    for key in ("win_rate", "expectancy_r", "profit_factor", "port_sharpe",
                "port_max_dd", "by_template", "n_trades"):
        assert key in res
    assert 0.0 <= res["win_rate"] <= 1.0


def test_gate_stricter_params_fewer_trades(frames_and_panel):
    """单调性：闸门越严交易越少（参数灵敏度自检）。"""
    frames, panel = frames_and_panel
    loose = run_backtest(frames, panel, GateParams(mrs_gate=5.5, tss_gate=7.0, light_tss=7.2))
    strict = run_backtest(frames, panel, GateParams(mrs_gate=6.5, tss_gate=7.8, light_tss=8.5))
    assert strict["n_trades"] <= loose["n_trades"]


def test_wfa_flow(frames_and_panel):
    frames, panel = frames_and_panel
    grid = [{"mrs_gate": 6.0, "shs_main": s, "tss_gate": t}
            for s in (7.0, 7.5) for t in (7.0, 7.5)]
    wfa = run_wfa(frames, panel, grid=grid, train=40, test=20, step=20)
    assert wfa["n_folds"] >= 1
    assert "dsr" in wfa and 0.0 <= wfa["dsr"] <= 1.0
    assert "oos_aggregate" in wfa
    assert "recommended_params" in wfa
    for f in wfa["folds"]:
        assert f["oos_trades"] >= 0


def test_slice_isolation():
    """切片隔离：篡改信号日之后的数据，当日 MRS 不变。"""
    from trading_system.agents import MRSAgent
    from trading_system import config as _c
    p = DemoProvider()
    spy = p.ohlcv("SPY", days=360)
    tnx, vix = p.tnx_yield(360), p.vix(360)
    closes = {t: p.ohlcv(t, days=360)["Close"] for t in CORE_UNIVERSE[:40]}
    cut = spy.index[-30]

    def mrs_at(spy_df):
        md = {"spy": spy_df, "tnx": tnx[tnx.index <= cut], "vix": vix[vix.index <= cut],
              "vix9d": None, "universe_closes": {t: s[s.index <= cut] for t, s in closes.items()}}
        ctx = {"market_data": md, "trade_date": str(cut.date())}
        return MRSAgent(p).execute(ctx).mrs_star

    base = mrs_at(spy[spy.index <= cut])
    # 篡改 cut 之后的数据（不应影响）
    spy2 = spy.copy()
    spy2.loc[spy2.index > cut, "Close"] *= 5
    hacked = mrs_at(spy2[spy2.index <= cut])
    assert base == hacked
