"""v5.4 深度审计修复的回归锁定。

覆盖：
1. 当前价格链路：quote 降级链（None 视为失败继续降级）+ 时效披露
2. stooq 反爬：非 CSV 响应（质询页/封禁页）必须抛清晰错误，绝不 KeyError
3. 期权维度：缺失子项 None 剔除（不再被极端打分 / TypeError）
4. universe：United 系不误杀 + nasdaqlisted ETF 列过滤
5. journal：开盘破止损信号作废（void），不污染胜率
6. MRS：仓位上限档位表单一口径（档内不插值、边界不跳变）
7. backtest：真实入场日披露 + WFA 整折无效回退默认参数（不崩溃）
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trading_system import pipeline
from trading_system.providers.base import DataProvider


# ---------------------------------------------------------------- 1. quote 降级链

class _NoneQuoteProvider:
    """quote 恒返回 None 的主源（模拟 yahoo 限流）。"""
    name = "yahoo"

    def quote(self, ticker):
        return None


class _FakeStooqQuote:
    name = "stooq"

    def quote(self, ticker):
        from datetime import datetime
        return {"price": 123.45, "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "kind": "realtime_delayed"}


def test_quote_fallback_treats_none_as_failure(monkeypatch):
    """主源 quote=None 时必须降级到下一环，而不是静默拿不到价。"""
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", _FakeStooqQuote)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])
    q = pipeline.quote_with_fallback(_NoneQuoteProvider(), "AAPL")
    assert q and q["price"] == 123.45
    assert q["kind"] == "realtime_delayed"


def test_quote_fallback_all_dead_returns_none(monkeypatch):
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", _NoneQuoteProvider)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])
    assert pipeline.quote_with_fallback(_NoneQuoteProvider(), "AAPL") is None


def test_base_quote_discloses_eod_kind():
    """基类 quote 必须标注 kind=eod_close（收盘价不得冒充实时价）。"""
    class _P(DataProvider):
        name = "fake"

        def ohlcv(self, ticker, days=400):
            idx = pd.bdate_range("2026-08-24", periods=5)
            return pd.DataFrame({"Open": [1] * 5, "High": [1] * 5, "Low": [1] * 5,
                                 "Close": [10.0, 11, 12, 13, 14.5],
                                 "Volume": [1] * 5}, index=idx)

        def tnx_yield(self, days=400):
            raise NotImplementedError

        def vix(self, days=400):
            raise NotImplementedError

    q = _P().quote("X")
    assert q["price"] == 14.5 and q["kind"] == "eod_close"


# ---------------------------------------------------------------- 2. stooq 反爬

def test_stooq_rejects_non_csv():
    """质询页/封禁页必须抛 RuntimeError（降级链接管），不允许 KeyError。"""
    from trading_system.providers.stooq import StooqProvider

    p = StooqProvider()
    p._http.get_text = lambda url: (_ for _ in ()).throw(
        RuntimeError("stooq 反爬质询未能解除（数据中心 IP 可能被拒），请走降级链下一环"))
    with pytest.raises(RuntimeError, match="反爬|非 CSV|网络"):
        p.ohlcv("AAPL", days=10)


def test_stooq_session_detects_challenge_page():
    from trading_system.providers.stooq import _StooqSession

    sess = _StooqSession()
    sess._request = lambda *a, **k: "<html>Access denied</html>"
    with pytest.raises(RuntimeError, match="非 CSV"):
        sess.get_text("https://stooq.com/q/d/l/?s=aapl.us&i=d")


# ---------------------------------------------------------------- 3. 期权缺失子项

def test_options_none_subfields_excluded_not_scored():
    """snap 子项为 None/NaN 时该子项剔除（None），不得按分位 0.0 极端打分。"""
    from trading_system.options_metrics import OptionsHistoryStore, score_options

    class _Snap:
        name = "fake"

        def options_chain_snapshot(self, ticker):
            return {"pcr_oi": None, "atm_iv": float("nan"), "call_oi": 100_000.0}

    class _Store(OptionsHistoryStore):
        def __init__(self):
            pass

        def load(self, ticker):
            # 12 条历史（≥MIN_OBS），call_oi 稳定、pcr/iv 有值
            return [{"date": f"2026-08-{d:02d}", "pcr_oi": 0.9, "atm_iv": 0.3,
                     "call_oi": 100_000.0} for d in range(1, 13)]

        def append(self, ticker, snap):
            return self.load(ticker) + [snap]

    out = score_options("XYZ", _Snap(), store=_Store(), persist=False)
    assert out["B"] is None and out["C"] is None          # 缺失子项剔除
    assert out["A"] is not None                            # call_oi 有效
    assert "pcr_oi" in out["missing"] and "atm_iv" in out["missing"]


def test_options_all_subfields_missing_whole_dim_none():
    from trading_system.options_metrics import OptionsHistoryStore, score_options

    class _Snap:
        name = "fake"

        def options_chain_snapshot(self, ticker):
            return {"pcr_oi": None, "atm_iv": None, "call_oi": None}

    class _Store(OptionsHistoryStore):
        def __init__(self):
            pass

        def load(self, ticker):
            return [{"date": f"2026-08-{d:02d}", "pcr_oi": None, "atm_iv": None,
                     "call_oi": None} for d in range(1, 13)]

        def append(self, ticker, snap):
            return self.load(ticker) + [snap]

    out = score_options("XYZ", _Snap(), store=_Store(), persist=False)
    assert out["s_options"] is None                        # 整维剔除，不钉中性 5


def test_options_call_oi_gap_no_fake_crash():
    """call_oi 历史有缺失时不得制造 -100% 假跳变。"""
    from trading_system.options_metrics import OptionsHistoryStore, score_options

    class _Snap:
        name = "fake"

        def options_chain_snapshot(self, ticker):
            return {"pcr_oi": 0.9, "atm_iv": 0.3, "call_oi": 100_000.0}

    class _Store(OptionsHistoryStore):
        def __init__(self):
            pass

        def load(self, ticker):
            hist = [{"date": f"2026-08-{d:02d}", "pcr_oi": 0.9, "atm_iv": 0.3,
                     "call_oi": 100_000.0} for d in range(1, 11)]
            hist.append({"date": "2026-08-11", "pcr_oi": 0.9, "atm_iv": 0.3,
                         "call_oi": None})                # 缺失一日
            hist.append({"date": "2026-08-12", "pcr_oi": 0.9, "atm_iv": 0.3,
                         "call_oi": 100_000.0})
            return hist

        def append(self, ticker, snap):
            return self.load(ticker) + [snap]

    out = score_options("XYZ", _Snap(), store=_Store(), persist=False)
    # 所有有效对变化率都是 0 → 分位 1.0 → 高分；若混入 -100% 假跳变则分位骤降
    assert out["raw"]["call_oi_chg"] == pytest.approx(0.0)


# ---------------------------------------------------------------- 4. universe

def test_united_family_not_killed():
    from trading_system.universe import _bad_name
    for name in ("unitedhealth group incorporated common stock",
                 "united parcel service, inc. common stock",
                 "united airlines holdings, inc. common stock"):
        assert not _bad_name(name)
    assert _bad_name("spac acquisition corp. unit")
    assert _bad_name("some company units each consisting of")
    assert _bad_name("abc inc. warrant")
    assert _bad_name("xyz preferred stock")


def test_nasdaqlisted_etf_column_filtered():
    """nasdaqlisted.txt ETF 标志在第 6 列（v5.4 前误用第 7 列 NextShares）。"""
    from trading_system.universe import _parse_symdir

    header = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"
    rows = [
        header,
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|N",
        "QQQ|Invesco QQQ Trust, Series 1|Q|N|N|100|Y|N",   # 名称无 "etf" 字样
        "TESTX|Test Stock|Q|Y|N|100|N|N",
        "File Creation Time: xxx",
    ]
    out = _parse_symdir("\n".join(rows), 0, 3, 6, 1, include_etfs=False)
    assert "AAPL" in out and "QQQ" not in out and "TESTX" not in out


# ---------------------------------------------------------------- 5. journal 作废口径

def test_journal_gap_through_stop_is_void_not_counted(tmp_path):
    from trading_system.journal import Journal

    j = Journal(tmp_path / "journal.json")
    j.records.append({
        "date": "2026-08-20", "ticker": "XYZ", "mode": "标准做多", "template": "A",
        "sector": "", "chain": "", "entry_ref": 100.0, "stop": 95.0,
        "tss_final": 8.0, "tos": 5.0, "mrs_star": 7.0, "action": "BUY",
        "status": "open", "entry": None, "entry_date": None,
        "exit": None, "exit_date": None, "r": None, "win": None,
    })
    # 信号日次日开盘 94（直接破止损 95）
    idx = pd.bdate_range("2026-08-20", periods=10)
    df = pd.DataFrame({
        "Open": [100.0, 94.0] + [93.0] * 8,
        "High": [101.0, 95.0] + [94.0] * 8,
        "Low": [99.0, 93.0] + [92.0] * 8,
        "Close": [100.0, 93.5] + [93.0] * 8,
    }, index=idx)
    rec = j.records[0]
    j._settle_one(rec, df[df.index.date >= pd.Timestamp("2026-08-20").date()])
    assert rec["status"] == "void"
    assert rec["r"] is None
    stats = j.stats()
    assert stats["closed"] == 0                        # 不污染胜率样本


# ---------------------------------------------------------------- 6. MRS 仓位上限

def test_position_cap_band_table_no_interpolation():
    from trading_system.agents.mrs_agent import MRSAgent

    assert MRSAgent._position_cap(8.5) == (0.70, 0.90)
    assert MRSAgent._position_cap(8.0) == (0.70, 0.90)   # 档底不再塌缩
    assert MRSAgent._position_cap(7.99) == (0.40, 0.70)  # 边界无跳变
    assert MRSAgent._position_cap(6.0) == (0.40, 0.70)
    assert MRSAgent._position_cap(5.99) == (0.10, 0.25)
    assert MRSAgent._position_cap(3.5) == (0.00, 0.10)


# ---------------------------------------------------------------- 7. backtest

def test_simulate_trade_reports_true_entry_index():
    """停牌导致延后入场时，entry_i/entry_date 必须是真实成交日。"""
    import numpy as np
    from trading_system.backtest import GateParams, _Panel, _simulate_trade

    dates = pd.bdate_range("2026-08-03", periods=15)
    n = len(dates)
    data = {
        "XYZ": pd.DataFrame({
            "Open": [100.0] * 3 + [np.nan] + [98.0] * (n - 4),   # i=3 停牌
            "High": [101.0] * n, "Low": [97.0] * n,
            "Close": [100.0] * n, "Volume": [1e6] * n,
        }, index=dates)
    }
    spy = pd.DataFrame({"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
                        "Close": [1.0] * n, "Volume": [1] * n}, index=dates)
    panel = _Panel(data, spy)
    sim = _simulate_trade(panel, 0, i_sig=2, stop0=90.0, params=GateParams())
    assert sim is not None
    entry_i = sim[0]
    assert entry_i == 4                                  # 真实入场是第 4 根，不是 i_sig+1=3
    assert str(panel.dates[entry_i].date()) == str(dates[4].date())


def test_wfa_fold_all_invalid_falls_back_to_default():
    """整折网格全部交易数不足时回退默认参数，不崩溃。"""
    from dataclasses import replace

    from trading_system import backtest

    class _FakePanel:
        dates = pd.bdate_range("2026-01-05", periods=400)

    frames = []
    for d in _FakePanel.dates:
        f = type("F", (), {})()
        f.date = d
        frames.append(f)

    def fake_run_backtest(frames, panel, params=None):
        return {"n_trades": 0, "port_sharpe": 0.0, "expectancy_r": 0.0,
                "port_returns": [0.0] * len(frames), "trades": [],
                "win_rate": 0.0, "port_max_dd": 0.0}

    import pytest as _pytest
    with _pytest.MonkeyPatch().context() as mp:
        mp.setattr(backtest, "run_backtest", fake_run_backtest)
        out = backtest.run_wfa(frames, _FakePanel(), min_trades=5)
    assert "folds" in out
    assert all(row["is_fallback_default"] for row in out["folds"])
    assert all(row["is_params"] == {} for row in out["folds"])


def test_dsr_uses_daily_frequency():
    """DSR 口径：日频 SR 配日频 T（v5.4 前年化 SR 配日频 T，量纲混乱）。"""
    from trading_system.stats import deflated_sharpe_ratio

    sr_daily = 0.15                                    # 日频夏普
    sr_annual = sr_daily * (252 ** 0.5)                # 同一策略的年化口径
    # 混合口径（年化 SR 配日频 T）把中等显著性虚报为 ≈1.0，
    # "不显著则回退默认参数"的保险丝因此失效——这正是被修复的 bug。
    dsr_daily = deflated_sharpe_ratio(sr_hat=sr_daily, t=126, skew=-1.0,
                                      kurt=3.0, n_trials=27, trial_srs=[0.01] * 27)
    dsr_mixed = deflated_sharpe_ratio(sr_hat=sr_annual, t=126, skew=-1.0,
                                      kurt=3.0, n_trials=27, trial_srs=[0.01] * 27)
    assert 0.80 < dsr_daily < 0.99                     # 正确口径：中等显著
    assert dsr_mixed > 0.99                            # 混合口径：虚报显著
    assert dsr_mixed > dsr_daily
