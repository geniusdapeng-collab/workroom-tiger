"""小G 摩擦成本三栏台账测试（v6.3 S2，防滑点断崖）。

覆盖：
  ① 三栏恒等式：毛收益 − 摩擦成本 = 净收益（USD 与 R 双口径）；
  ② 滑点单调：ADV 越小滑点越大；
  ③ 摩擦永不为负（盈利单同样成立）；
  ④ demo 确定性：同输入同输出；
  ⑤ 与既有口径兼容：ADV 未知（Bar 默认）回落 v6.0 合并 10bp。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_system import config
from trading_system.simulator import Bar, SimEngine, friction_bps


def _pick(ticker="AAA", shares=100, stop=90.0, risk=1000.0):
    return SimpleNamespace(ticker=ticker, tss_final=8.0, entry_template="A",
                           stop_price=stop, shares=shares, risk_usd=risk,
                           chain="semis", sector="XLK",
                           time_stop_days=0, event_note="")


def _result(action="BUY", picks=(), cap=1.0):
    mrs = SimpleNamespace(position_cap=(0.0, cap))
    return SimpleNamespace(action=action, picks=list(picks), mrs=mrs)


def _engine(tmp_path, name="sim_portfolio.json") -> SimEngine:
    return SimEngine(str(tmp_path / name))


def _run_roundtrip(eng, adv):
    """信号 → T+1 成交（含摩擦）→ 止损出场。返回 closed 记录。"""
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=100.0, high=102.0, low=98.0, close=101.0, adv=adv))
    eng.step("2026-07-30", _result("AVOID"),
             lambda t: Bar(open=95.0, high=96.0, low=89.0, close=94.0, adv=adv))
    return eng.state["closed"][0]


# ---------------------------------------------------------------- ① 三栏恒等式
def test_three_column_identity(tmp_path):
    eng = _engine(tmp_path)
    closed = _run_roundtrip(eng, adv=600_000_000)          # ≥$500M → 滑点5bp+佣金10bp
    # 入场：100 × (1+15bp) = 100.15；出场：90 × (1−15bp) = 89.865
    assert eng.state["positions"] == []
    assert closed["gross_pnl"] == pytest.approx(-1000.0)   # (90−100)×100
    assert closed["pnl_usd"] == pytest.approx(-1028.5)     # 净亏 = 毛亏 − 摩擦
    assert closed["friction_cost"] == pytest.approx(28.5)
    # 恒等式：毛 − 摩擦 = 净
    assert closed["gross_pnl"] - closed["friction_cost"] == \
        pytest.approx(closed["pnl_usd"])
    # R 双口径：risk_usd=1000 → 毛 -1.0R / 净 -1.03R（四舍五入后恒等）
    assert closed["gross_r"] == pytest.approx(-1.0)
    assert closed["net_r"] == pytest.approx(-1.03)
    assert closed["net_r"] == closed["r_multiple"]         # net_r 即原 R 口径
    # stats 三栏汇总同恒等式
    st = eng.stats()
    assert st["pnl_gross"] - st["friction_total"] == pytest.approx(st["pnl_net"])


# ---------------------------------------------------------------- ② 滑点单调
def test_slippage_monotonic_in_adv():
    assert friction_bps(600_000_000) == 15.0    # ≥$500M → 5+10
    assert friction_bps(200_000_000) == 20.0    # ≥$100M → 10+10
    assert friction_bps(50_000_000) == 35.0     # ≥$20M  → 25+10
    assert friction_bps(10_000_000) == 60.0     # 以下   → 50+10
    tiers = [friction_bps(a) for a in
             (600_000_000, 200_000_000, 50_000_000, 10_000_000)]
    assert tiers == sorted(tiers)               # ADV 越小滑点越大（严格单调）
    assert len(set(tiers)) == 4


def test_smaller_adv_costs_more_in_ledger(tmp_path):
    big = _run_roundtrip(_engine(tmp_path, "a.json"), adv=600_000_000)
    small = _run_roundtrip(_engine(tmp_path, "b.json"), adv=10_000_000)
    assert small["friction_cost"] > big["friction_cost"]     # 小 ADV 摩擦更大
    assert big["gross_pnl"] == small["gross_pnl"]            # 毛口径与 ADV 无关


# ---------------------------------------------------------------- ③ 摩擦永不为负
def test_friction_never_negative_on_winning_trade(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=100.0, high=102.0, low=98.0, close=101.0,
                           adv=50_000_000))
    # 时间止损小幅盈利出场：持仓满 7 日记账日，收盘 101.3 低于成本×1.01
    # （入场净价 100×1.0035=100.35，101.3 < 100.35×1.01=101.35 → 触发）
    eng.state["equity_curve"] = [
        {"date": f"2026-08-{d:02d}", "equity": 100_000.0} for d in range(1, 10)]
    eng.step("2026-08-10", _result("AVOID"),
             lambda t: Bar(open=100.8, high=101.6, low=100.5, close=101.3,
                           adv=50_000_000))
    closed = eng.state["closed"][0]
    assert closed["reason"].startswith("时间止损")
    assert closed["pnl_usd"] > 0                             # 盈利单
    assert closed["friction_cost"] >= 0                      # 摩擦永不为负
    assert closed["gross_pnl"] - closed["friction_cost"] == \
        pytest.approx(closed["pnl_usd"])


# ---------------------------------------------------------------- ④ demo 确定性
def test_deterministic_same_input_same_output(tmp_path):
    s1 = _run_roundtrip(_engine(tmp_path, "a.json"), adv=200_000_000)
    s2 = _run_roundtrip(_engine(tmp_path, "b.json"), adv=200_000_000)
    assert s1 == s2


# ---------------------------------------------------------------- ⑤ 既有口径兼容
def test_unknown_adv_falls_back_to_legacy_10bp(tmp_path):
    assert friction_bps(0.0) == config.COST_BPS              # ADV 未知 → v6.0 口径
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=100.0, high=102.0, low=98.0, close=101.0))
    pos = eng.state["positions"][0]
    assert pos["entry_price"] == 100.1                       # 与 v6.0 完全一致
    assert pos["entry_raw"] == 100.0 and pos["friction_bps"] == config.COST_BPS
