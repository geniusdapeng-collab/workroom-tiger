"""资产管理团队内核模块测试（v3.5）：配置官/哨兵/组合风险官/收益稳定官。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from trading_system.portfolio import (GlobalAllocator, GlobalSentinel,
                                      PortfolioRiskOfficer, ReturnSteward)


def _mk_result(d: Path, mrs=7.0, cap=(0.4, 0.7), tos=(50.0, 40.0)):
    d.mkdir(parents=True, exist_ok=True)
    (d / "result_20260830.json").write_text(json.dumps({
        "trade_date": "2026-08-30", "action": "BUY",
        "mrs": {"mrs_star": mrs, "position_cap": list(cap)},
        "picks": [{"tos": tos[0]}, {"tos": tos[1]}], "raw": {}}))


def _mk_sim(d: Path, equities):
    d.mkdir(parents=True, exist_ok=True)
    (d / "sim_portfolio.json").write_text(json.dumps({
        "cash": equities[-1], "positions": [], "pending": [],
        "equity_curve": [{"date": f"2026-08-{i + 1:02d}", "equity": e}
                         for i, e in enumerate(equities)]}))


# ---------------------------------------------------------------- 配置官
def test_allocator_within_cap(tmp_path):
    _mk_result(tmp_path / "us", mrs=7.0, cap=(0.4, 0.7))
    _mk_result(tmp_path / "cn", mrs=1.5, cap=(0.0, 0.1))
    _mk_result(tmp_path / "hk", mrs=5.3, cap=(0.1, 0.25))
    plan = GlobalAllocator().plan(
        {"us": json.loads((tmp_path / "us/result_20260830.json").read_text()),
         "cn": json.loads((tmp_path / "cn/result_20260830.json").read_text()),
         "hk": json.loads((tmp_path / "hk/result_20260830.json").read_text())},
        graduated={"us": True, "cn": False, "hk": False}, date="2026-08-30")
    assert plan.gross_cap == 0.90
    assert not plan.truncated                       # 1.05 未超（cn/hk 轻仓折算后远低于 cap）
    us = next(m for m in plan.markets if m.market == "us")
    assert us.weight > 0
    cn = next(m for m in plan.markets if m.market == "cn")
    assert "轻仓" in cn.note                        # 验证期市场轻仓封顶披露


def test_allocator_truncates_by_quality(tmp_path):
    """三市共振超组合上限：按质量分从高到低截断，截断记录可审计。"""
    for mid in ("us", "cn", "hk"):
        _mk_result(tmp_path / mid, mrs=9.0, cap=(0.7, 0.9))
    results = {m: json.loads((tmp_path / m / "result_20260830.json").read_text())
               for m in ("us", "cn", "hk")}
    plan = GlobalAllocator(gross_cap=0.90).plan(results, date="2026-08-30")
    total = sum(m.local_cap for m in plan.markets)
    assert total <= 0.90 + 1e-9
    assert plan.truncated                            # 截断记录非空（可审计）


def test_allocator_missing_market_honest(tmp_path):
    _mk_result(tmp_path / "us")
    plan = GlobalAllocator().plan(
        {"us": json.loads((tmp_path / "us/result_20260830.json").read_text()),
         "cn": None, "hk": None})
    cn = next(m for m in plan.markets if m.market == "cn")
    assert not cn.available and "无有效数据" in cn.note


# ---------------------------------------------------------------- 哨兵
class _FakeProvider:
    name = "fake"

    def ohlcv(self, ticker, days=40):
        idx = pd.bdate_range("2026-07-01", periods=30)
        return pd.DataFrame({"Close": range(100, 130),
                             "Open": 1, "High": 1, "Low": 1, "Volume": 1},
                            index=idx)


def test_sentinel_snapshot(monkeypatch):
    s = GlobalSentinel(provider=_FakeProvider())
    monkeypatch.setattr(
        "trading_system.providers.official.OfficialMacroProvider.fred_series",
        lambda self, sid, days=10: pd.Series([1.0, 2.0, 0.47]))
    snap = s.snapshot()
    assert set(snap.benchmarks) == {"us", "cn", "hk"}
    assert snap.benchmarks["us"]["mom20"] == pytest.approx(129 / 109 - 1, rel=1e-3)
    assert snap.fred and not snap.missing
    assert snap.session in ("asia", "europe", "us", "closed")


def test_sentinel_missing_honest(monkeypatch):
    class _Down:
        name = "down"

        def ohlcv(self, t, days=40):
            raise RuntimeError("down")

    monkeypatch.setattr(
        "trading_system.providers.official.OfficialMacroProvider.fred_series",
        lambda self, sid, days=10: (_ for _ in ()).throw(RuntimeError("down")))
    snap = GlobalSentinel(provider=_Down()).snapshot()
    assert "benchmark:us" in snap.missing and not snap.benchmarks


# ---------------------------------------------------------------- 组合风险官 + 收益稳定官
def test_risk_view_concentration(tmp_path):
    _mk_sim(tmp_path / "us", [100000, 101000])
    _mk_sim(tmp_path / "cn", [50000, 50000])
    view = PortfolioRiskOfficer().view(
        {"us": str(tmp_path / "us"), "cn": str(tmp_path / "cn"),
         "hk": str(tmp_path / "hk")})
    assert view.total_equity == 151000
    assert view.concentration["us"] == pytest.approx(101000 / 151000, rel=1e-3)
    assert any("集中度" in n for n in view.notes)
    assert any("hk" in n for n in view.notes)        # 缺失如实披露


def test_steward_stable_curve(tmp_path):
    _mk_sim(tmp_path / "us", [100000 + i * 100 for i in range(30)])
    rep = ReturnSteward().assess({"us": str(tmp_path / "us")})
    assert rep.max_drawdown == 0.0 and rep.verdict == "稳定"
    assert rep.total_return > 0


def test_steward_alert_on_drawdown(tmp_path):
    eq = [100000] * 5 + [80000] * 5                  # -20% 回撤
    _mk_sim(tmp_path / "us", eq)
    rep = ReturnSteward().assess({"us": str(tmp_path / "us")})
    assert rep.verdict == "告警" and rep.max_drawdown == pytest.approx(-0.2)


def test_steward_no_data(tmp_path):
    rep = ReturnSteward().assess({"us": str(tmp_path / "us")})
    assert rep.verdict == "关注" and "积累中" in rep.notes[0]
