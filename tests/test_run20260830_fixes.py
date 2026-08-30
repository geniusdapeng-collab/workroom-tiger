"""v3 真实运行暴露问题修复的回归测试（2026-08-30）。

三个根因：
  R1 降级协议"None 视同成功"——yahoo.vix9d 失败返回 None，吞掉后续真实可用源
     （CBOE VIX9D 官方数据可达却未被使用）；
  R2 宏观序列无陈旧检测——tencent VIX 返回 4 个月前陈旧序列，旧协议照单全收；
  R3 模拟盘与 journal 镜像断裂——HOLD（轻仓试探区）日 journal 落账而 sim 按兵不动
     （白皮书 §9.1 HOLD=轻仓试探区允许轻仓出战；§15.2 两本账互为镜像）。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import pipeline as pl
from trading_system.simulator import Bar, SimEngine


# ---------------------------------------------------------------- R1/R2 降级协议
class _FakeProvider:
    def __init__(self, name, result=None, exc=None, caps=None):
        self.name = name
        self._result = result
        self._exc = exc
        if caps is not None:
            self.CAPABILITIES = caps
        self.calls: list[str] = []

    def vol_index_for(self, symbol, days=400):
        self.calls.append(symbol)
        if self._exc:
            raise self._exc
        return self._result

    def ohlcv(self, ticker, days=400):
        self.calls.append(ticker)
        if self._exc:
            raise self._exc
        return self._result


def _fresh_series(v=14.5):
    idx = pd.date_range(end=datetime.now().date(), periods=40, freq="B")
    return pd.Series([v] * len(idx), index=idx)


def _stale_series(v=21.0):
    idx = pd.date_range(end=datetime.now().date() - timedelta(days=120), periods=40, freq="B")
    return pd.Series([v] * len(idx), index=idx)


def test_none_result_falls_through_to_real_source(monkeypatch):
    """None 视同失败：首源返回 None 时必须继续降级到真实可用源。"""
    bad = _FakeProvider("bad", result=None)
    good = _FakeProvider("good", result=_fresh_series())
    monkeypatch.setattr(pl, "_channel_chain", lambda: [good])
    monkeypatch.setattr(pl, "_LINEAGE", [])
    out = pl._single_with_fallback(bad, "vol_index_for", "VIX9D", days=400)
    assert isinstance(out, pd.Series) and float(out.iloc[-1]) == 14.5
    assert ("vol_index_for(VIX9D)", "good") in pl._LINEAGE


def test_stale_series_falls_through(monkeypatch):
    """序列末根陈旧（>5 自然日）视同失败，继续降级到新鲜源。"""
    stale = _FakeProvider("stale_src", result=_stale_series())
    fresh = _FakeProvider("fresh_src", result=_fresh_series())
    monkeypatch.setattr(pl, "_channel_chain", lambda: [fresh])
    out = pl._single_with_fallback(stale, "vol_index_for", "VIX", days=400)
    assert float(out.iloc[-1]) == 14.5


def test_all_sources_none_raises(monkeypatch):
    """全链 None → 抛错（由调用方按口径记缺失，绝不静默成功）。"""
    bad = _FakeProvider("bad", result=None)
    monkeypatch.setattr(pl, "_channel_chain", lambda: [])
    with pytest.raises(RuntimeError, match="None"):
        pl._single_with_fallback(bad, "vol_index_for", "VIX9D", days=400)


def test_capability_aware_skip(monkeypatch):
    """声明 CAPABILITIES 且不含该方法的 provider 直接跳过，不产生必然失败的调用。"""
    macro_only = _FakeProvider("official", result=_fresh_series(),
                               caps={"rate_yield_for", "vol_index_for"})
    equity_src = _FakeProvider("tencent", result=pd.DataFrame(
        {"Close": [1.0]}, index=pd.date_range(end=datetime.now().date(), periods=1)))
    monkeypatch.setattr(pl, "_channel_chain", lambda: [macro_only, equity_src])
    bad = _FakeProvider("bad", exc=RuntimeError("down"))
    pl._single_with_fallback(bad, "ohlcv", "SPY", days=400)
    assert macro_only.calls == []          # official 未被无意义调用
    assert equity_src.calls == ["SPY"]


def test_official_capabilities_declared():
    from trading_system.providers.official import OfficialMacroProvider
    assert "rate_yield_for" in OfficialMacroProvider.CAPABILITIES
    assert "ohlcv" not in OfficialMacroProvider.CAPABILITIES


# ---------------------------------------------------------------- R3 模拟盘镜像
def _pick(ticker="AAA", shares=100, stop=90.0, risk=1000.0):
    return SimpleNamespace(ticker=ticker, tss_final=8.0, entry_template="A",
                           stop_price=stop, shares=shares, risk_usd=risk,
                           chain="biotech", sector="IBB",
                           time_stop_days=0, event_note="")


def _result(action="BUY", picks=(), cap=0.25):
    mrs = SimpleNamespace(position_cap=(0.0, cap))
    return SimpleNamespace(action=action, picks=list(picks), mrs=mrs)


def test_hold_day_light_signals_registered(tmp_path):
    """HOLD（轻仓试探区）日出战的轻仓信号必须登记——与 journal 落账互为镜像。"""
    eng = SimEngine(str(tmp_path / "sim.json"))
    eng.step("2026-08-30", _result("HOLD", [_pick("BDX")]), lambda t: None)
    assert len(eng.state["pending"]) == 1
    assert eng.state["pending"][0]["ticker"] == "BDX"


def test_avoid_day_still_no_new_signals(tmp_path):
    """AVOID 是白皮书绝对禁止项：即使构造出 picks 也防御性拒绝登记。"""
    eng = SimEngine(str(tmp_path / "sim.json"))
    eng.step("2026-08-30", _result("AVOID", [_pick("XXX")]), lambda t: None)
    assert not eng.state["pending"] and not eng.state["positions"]


def test_hold_day_signal_fills_next_open(tmp_path):
    """HOLD 日登记的信号 T+1 开盘价成交（无未来函数节拍不变）。"""
    eng = SimEngine(str(tmp_path / "sim.json"))
    eng.step("2026-08-30", _result("HOLD", [_pick("BDX", shares=100, stop=90.0)]),
             lambda t: None)
    eng.step("2026-08-31", _result("HOLD"),
             lambda t: Bar(open=100.0, high=101.0, low=99.0, close=100.5))
    assert len(eng.state["positions"]) == 1
    assert eng.state["positions"][0]["ticker"] == "BDX"
