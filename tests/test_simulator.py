"""小G模拟盘记账引擎测试。

覆盖公开验证台账的核心纪律：
  T+1 开盘价成交（无未来函数）/ 开盘价漂移风险重算 / 止损与跳空 /
  时间止损 / 2R 盈利保护 / AVOID 日不开新仓但继续管理持仓 /
  台账持久化往返 / 统计口径。
"""
from types import SimpleNamespace

from trading_system.simulator import Bar, SimEngine


def _pick(ticker="AAA", shares=100, stop=90.0, risk=1000.0):
    return SimpleNamespace(ticker=ticker, tss_final=8.0, entry_template="A",
                           stop_price=stop, shares=shares, risk_usd=risk,
                           chain="semis", sector="XLK",
                           time_stop_days=0, event_note="")


def _result(action="BUY", picks=(), cap=1.0):
    mrs = SimpleNamespace(position_cap=(0.0, cap))
    return SimpleNamespace(action=action, picks=list(picks), mrs=mrs)


def _pos(ticker="AAA", entry=100.0, stop=90.0, shares=100, entry_date="2026-07-01"):
    return {"ticker": ticker, "shares": shares, "entry_price": entry,
            "stop": stop, "entry_date": entry_date, "chain": "semis",
            "sector": "XLK", "risk_usd": round(shares * abs(entry - stop), 2),
            "peak_price": entry, "note": ""}


def _engine(tmp_path) -> SimEngine:
    return SimEngine(str(tmp_path / "sim_portfolio.json"))


# ---------------------------------------------------------------- T+1 成交
def test_signal_fills_next_day_open(tmp_path):
    eng = _engine(tmp_path)
    bars = {"AAA": Bar(open=100.0, high=102.0, low=98.0, close=101.0)}
    # T 日：登记信号，不成交（无未来函数）
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    assert not eng.state["positions"] and len(eng.state["pending"]) == 1
    # T+1 日：按开盘价成交
    eng.step("2026-07-29", _result("AVOID"), lambda t: bars.get(t))
    pos = eng.state["positions"]
    # v6.0 净口径：entry_price 含单边成本（100 × 1.001）
    assert len(pos) == 1 and pos[0]["entry_price"] == 100.1
    assert eng.state["cash"] == round(100_000 - 100 * 100.1, 2)
    assert any("开盘价成交" in op for op in eng.state["ops_log"][-1]["ops"])


def test_open_drift_recalculates_shares(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick(shares=100, stop=90.0, risk=1000.0)]),
             lambda t: None)
    # 开盘价从信号价漂移到 105 → 每股风险 15 → 股数收缩到 66
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=105.0, high=106.0, low=104.0, close=105.5))
    assert eng.state["positions"][0]["shares"] == 66


def test_same_day_rerun_never_fills_today_signal(tmp_path):
    eng = _engine(tmp_path)
    bar = Bar(open=100.0, high=102.0, low=98.0, close=101.0)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: bar)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: bar)  # 同日重跑
    assert not eng.state["positions"]            # T+1 铁律：当日信号绝不当日成交
    assert len(eng.state["ops_log"]) == 1        # 操作日志同日幂等覆盖
    eng.step("2026-07-29", _result("AVOID"), lambda t: bar)
    assert len(eng.state["positions"]) == 1      # 次日才按开盘价成交


def test_gap_through_stop_voids_signal(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick(stop=90.0)]), lambda t: None)
    # 次日开盘价 89 直接跌破止损 90 → 风险模型失效，信号作废
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=89.0, high=90.0, low=88.0, close=89.5))
    assert not eng.state["positions"] and not eng.state["pending"]
    assert any("信号作废" in op for op in eng.state["ops_log"][-1]["ops"])


def test_no_quote_defers_fill(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    eng.step("2026-07-29", _result("AVOID"), lambda t: None)  # 无行情
    assert not eng.state["positions"] and len(eng.state["pending"]) == 1  # 顺延


# ---------------------------------------------------------------- 出场纪律
def test_stop_loss_at_stop_price(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=95.0, high=96.0, low=89.0, close=94.0))
    closed = eng.state["closed"]
    # v6.0 净口径：卖出扣单边成本（90 × 0.999）
    assert len(closed) == 1 and closed[0]["exit"] == 89.91
    assert closed[0]["reason"] == "触及止损离场"
    assert closed[0]["pnl_usd"] == -1009.0 and closed[0]["r_multiple"] == -1.01


def test_gap_down_exits_at_open(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=85.0, high=86.0, low=84.0, close=85.5))
    closed = eng.state["closed"]
    # v6.0 净口径：85 × 0.999
    assert closed[0]["exit"] == 84.915 and closed[0]["reason"] == "跳空低开触发止损"


def test_time_stop_after_seven_days(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())
    eng.state["equity_curve"] = [
        {"date": f"2026-07-{d:02d}", "equity": 100_000.0} for d in range(2, 11)]
    # 第 9 个持仓交易日，收盘价未推进 +1%
    eng.step("2026-07-11", _result("AVOID"),
             lambda t: Bar(open=100.2, high=100.8, low=99.5, close=100.5))
    assert eng.state["closed"][0]["reason"].startswith("时间止损")


def test_time_stop_not_triggered_when_profitable(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())
    eng.state["equity_curve"] = [
        {"date": f"2026-07-{d:02d}", "equity": 100_000.0} for d in range(2, 11)]
    eng.step("2026-07-11", _result("AVOID"),
             lambda t: Bar(open=101.5, high=102.0, low=101.0, close=101.5))
    assert not eng.state["closed"] and len(eng.state["positions"]) == 1


def test_profit_protection_moves_stop_to_cost(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())  # entry 100 / stop 90 / 风险 10
    eng.step("2026-07-29", _result("AVOID"),
             lambda t: Bar(open=119.0, high=121.0, low=118.0, close=120.0))
    pos = eng.state["positions"][0]
    assert pos["stop"] == 100.0  # 浮盈 2R → 止损移成本线
    assert any("2R" in op for op in eng.state["ops_log"][-1]["ops"])


# ---------------------------------------------------------------- 纪律门
def test_avoid_day_registers_no_new_signals(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("AVOID", [_pick("BBB")]), lambda t: None)
    assert not eng.state["pending"] and not eng.state["positions"]


def test_avoid_day_still_manages_positions(tmp_path):
    eng = _engine(tmp_path)
    eng.state["positions"].append(_pos())
    eng.step("2026-07-29", _result("AVOID", [_pick("BBB")]),
             lambda t: Bar(open=95.0, high=96.0, low=89.0, close=94.0))
    assert len(eng.state["closed"]) == 1        # 持仓仍按纪律出场
    assert not eng.state["pending"]             # 但不登记新信号


# ---------------------------------------------------------------- 持久化 / 统计
def test_ledger_persistence_roundtrip(tmp_path):
    eng = _engine(tmp_path)
    eng.step("2026-07-28", _result("BUY", [_pick()]), lambda t: None)
    eng.save()
    reloaded = _engine(tmp_path)
    assert reloaded.state["cash"] == eng.state["cash"]
    assert reloaded.state["pending"] == eng.state["pending"]
    assert reloaded.state["equity_curve"] == eng.state["equity_curve"]
    assert reloaded.state["started"] == "2026-07-28"


def test_stats_win_rate_expectancy_profit_factor(tmp_path):
    eng = _engine(tmp_path)
    eng.state["closed"] = [
        {"ticker": "W", "pnl_usd": 2000.0, "r_multiple": 2.0, "days": 5,
         "entry_date": "2026-07-01", "exit_date": "2026-07-08",
         "entry": 100, "exit": 120, "shares": 100, "reason": "盈利保护"},
        {"ticker": "L", "pnl_usd": -1000.0, "r_multiple": -1.0, "days": 3,
         "entry_date": "2026-07-01", "exit_date": "2026-07-04",
         "entry": 100, "exit": 90, "shares": 100, "reason": "触及止损离场"},
    ]
    eng.state["equity_curve"] = [{"date": "2026-07-01", "equity": 100_000.0},
                                 {"date": "2026-07-04", "equity": 91_000.0},
                                 {"date": "2026-07-08", "equity": 101_000.0}]
    st = eng.stats()
    assert st["n_closed"] == 2 and st["win_rate"] == 0.5
    assert st["expectancy_r"] == 0.5 and st["profit_factor"] == 2.0
    assert st["equity"] == 101_000.0 and st["days"] == 3
    assert 0.089 < st["max_drawdown"] < 0.091   # 峰值 10 万 → 谷值 9.1 万
    assert abs(st["cum_return"] - 0.01) < 1e-9


def test_stats_profit_factor_none_without_losses(tmp_path):
    eng = _engine(tmp_path)
    eng.state["closed"] = [
        {"ticker": "W", "pnl_usd": 1500.0, "r_multiple": 1.5, "days": 4,
         "entry_date": "2026-07-01", "exit_date": "2026-07-05",
         "entry": 100, "exit": 115, "shares": 100, "reason": "时间止损（7 日未推进）"},
    ]
    st = eng.stats()
    assert st["win_rate"] == 1.0 and st["profit_factor"] is None  # 无亏损交易 → PF 记 None（展示 ∞）


def test_stats_empty_ledger(tmp_path):
    st = _engine(tmp_path).stats()
    assert st["equity"] == 100_000.0 and st["n_closed"] == 0
    assert st["win_rate"] is None and st["days"] == 0
