"""MC027: customer overlays constrain actual execution, not only receipts."""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import config
from trading_system.agents.mrs_agent import MRSAgent
from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.backtest import CandidateSnap, DayFrame, GateParams, SectorSnap, _Panel, run_backtest, run_wfa
from trading_system.data_models import DimensionScore, MRSResult, PipelineResult, SectorScore, StockCandidate, TradePick
from trading_system.journal import Journal
from trading_system.simulator import Bar, SimEngine

LIMITS = {"risk_r_pct": .004, "max_single_position_pct": .10, "gross_cap": .50}
BASELINE = {"risk_r_pct": .008, "max_single_position_pct": .20, "gross_cap": .90}


def _result(*, limits=None):
    return PipelineResult(trade_date="2026-09-28", provider="recorded-fixture", action="BUY",
        mrs=MRSResult(8, 0, 1, 8, position_cap=(.7, .9), allow_new_positions=True),
        raw={"risk_limits": dict(LIMITS) if limits is None else limits, "gross_cap": .9,
             "market": {"market_id": "us", "currency": "USD"}})


def _risk(stop=90, count=1, limits=None):
    candidates = [StockCandidate(f"T{index}", chain_id="semis", price=100,
        stop_price=stop, tss_final=8, c_liq=1, atr_pct=.02) for index in range(count)]
    context = {"risk_limits": dict(LIMITS) if limits is None else limits, "mrs": _result().mrs,
               "sectors": [SectorScore("SMH", 8, breadth=70, in_main_pool=True)],
               "watchlist": candidates, "trade_date": "2026-09-28"}
    return RiskManagerAgent(None, max_picks=10).execute(context), context


def _pending(engine, ticker="AAPL", *, shares=80, stop=90, risk=800, original=None):
    item = {"ticker": ticker, "shares": shares, "stop_price": stop, "risk_usd": risk,
            "entry_ref": 100, "signal_date": "2026-09-28", "time_stop_days": 7}
    if original:
        item["risk_limits"] = dict(original)
    engine.state["pending"].append(item)


def _panel():
    dates = pd.bdate_range("2026-09-01", periods=30)
    ticks = tuple(f"T{index}" for index in range(8))
    data = pd.DataFrame({"Open": 100., "High": 100.01, "Low": 99.99,
                         "Close": 100., "Volume": 1e6}, index=dates)
    panel = _Panel({t: data.copy() for t in ticks}, data)
    frames = [DayFrame(day, 8, "neutral", [SectorSnap("SMH", 8, 70)],
        [CandidateSnap(t, 8, 1, 1, "SMH", "semis", False, "A", 100, 99.9)
         for t in ticks]) for day in dates]
    return frames, panel


def test_mc027_sample_customer_risk_budget_reduces_actual_live_shares():
    picks, context = _risk()
    assert len(picks) == 1 and picks[0].shares == 40 and picks[0].risk_usd == 400
    assert context["pick_rationale"]["T0"]["risk_budget_usd"] == 400
    assert context["risk_limits"] == LIMITS and context["gross_cap"] == .5


def test_mc027_sample_customer_single_position_and_gross_limits_are_calculated():
    picks, context = _risk(stop=99.9, count=10)
    assert len(picks) == 5
    assert all(p.shares == 100 and p.position_pct == .1 for p in picks)
    assert sum(p.position_pct for p in picks) == .5 and context["gross_cap"] == .5


def test_mc027_sample_current_overlay_restricts_a_legacy_pending_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    engine = SimEngine(str(tmp_path / "sim.json"))
    _pending(engine)
    engine.step("2026-09-29", _result(), lambda _: Bar(100, 101, 99, 100))
    position = engine.state["positions"][0]
    assert position["shares"] == 40 and position["risk_usd"] == 400
    assert position["risk_limits"] == LIMITS


def test_mc027_property_old_and_current_pending_constraints_take_the_tighter_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    original = {"risk_r_pct": .002, "max_single_position_pct": .05, "gross_cap": .25}
    engine = SimEngine(str(tmp_path / "sim.json"))
    _pending(engine, original=original)
    engine.step("2026-09-29", _result(), lambda _: Bar(100, 101, 99, 100))
    position = engine.state["positions"][0]
    assert position["shares"] == 20 and position["risk_usd"] == 200
    assert position["risk_limits"] == original


def test_mc027_property_paper_total_exposure_is_bounded_by_customer_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    engine = SimEngine(str(tmp_path / "sim.json"))
    for index in range(8):
        _pending(engine, f"T{index}", shares=200, stop=99.9, risk=20)
    result = _result()
    result.raw["gate_params"] = asdict(GateParams(max_picks=10))
    engine.step("2026-09-29", result, lambda _: Bar(100, 100.01, 99.99, 100))
    assert len(engine.state["positions"]) == 5 and len(engine.state["pending"]) == 3
    assert engine.state["cash"] == 50_000
    assert all(p["shares"] == 100 for p in engine.state["positions"])


def test_mc027_property_journal_replay_records_actual_tighter_filled_quantity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    result = _result(limits=BASELINE)
    result.picks = [TradePick("AAPL", 8, 8, "A", 100, 90, 80, .08, 800,
                             chain="semis", sector="SMH", card="标准做多", time_stop_days=7)]
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(result)
    dates = pd.DatetimeIndex(["2026-09-28", "2026-09-29", "2026-09-30"])
    frame = pd.DataFrame({"Open": 100, "High": 101, "Low": [99, 99, 89],
                          "Close": [100, 100, 90]}, index=dates)
    provider = SimpleNamespace(ohlcv=lambda *a, **kw: frame)
    journal.settle(provider, as_of="2026-09-29", risk_limits=LIMITS)
    row = journal.records[0]
    assert row["filled_shares"] == 40 and row["filled_risk_usd"] == 400
    assert row["execution_risk_limits"] == LIMITS
    journal.settle(provider, as_of="2026-09-30", risk_limits=BASELINE)
    row = journal.records[0]
    assert row["filled_shares"] == 40 and row["filled_risk_usd"] == 400


def test_mc027_property_backtest_and_wfa_use_actual_customer_caps():
    frames, panel = _panel()
    result = run_backtest(frames, panel, GateParams(max_picks=10, cost_bps=0), risk_limits=LIMITS)
    assert result["trades"] and result["params"].risk_r_pct == .004
    assert result["risk_limits"] == LIMITS
    assert all(t.weight <= .1 and t.risk <= 400 for t in result["trades"])
    for index in range(len(panel.dates)):
        active = [t for t in result["trades"] if t.entry_i <= index <= t.exit_i]
        assert sum(t.weight for t in active) <= .5 + 1e-9
    wfa = run_wfa(frames, panel, grid=[{}], train=8, test=8, step=8, min_trades=1,
                  base_params=GateParams(max_picks=10, cost_bps=0), risk_limits=LIMITS)
    assert wfa["base_params"]["risk_r_pct"] == .004 and wfa["risk_limits"] == LIMITS


def test_mc027_property_mrs_reports_the_effective_customer_cap(monkeypatch):
    agent = MRSAgent(None)
    for name in ("_macro", "_flow", "_sentiment", "_technical", "_micro"):
        monkeypatch.setattr(agent, name, lambda *a, **kw: DimensionScore("fixture", 9))
    monkeypatch.setattr(agent, "_detect_shock", lambda *a: (False, ""))
    monkeypatch.setattr(agent, "_liquidity_discount", lambda *a: (1, ""))
    context = {"risk_limits": LIMITS, "market_data": {"spy": None, "universe_closes": {}}}
    result = agent.execute(context)
    assert result.position_cap == (.5, .5)


@pytest.mark.parametrize("value", [[], True, {"risk_r_pct": 0}, {"risk_r_pct": -.001},
    {"risk_r_pct": True}, {"risk_r_pct": float("nan")}, {"risk_r_pct": float("inf")},
    {"risk_r_pct": .0081}, {"max_single_position_pct": .21}, {"gross_cap": .9001},
    {"gross_cap": "0.5"}, {"unknown": .1}])
def test_mc027_property_invalid_or_widening_limits_fail_before_any_execution(value):
    with pytest.raises((ValueError, TypeError)):
        _risk(limits=value)


def test_mc027_property_overlay_never_mutates_global_configuration():
    before = (config.RISK_R_PCT, config.MAX_SINGLE_POSITION_PCT, list(config.MRS_POSITION_CAP))
    narrower, _ = _risk()
    later, _ = _risk(limits=BASELINE)
    assert narrower[0].risk_usd == 400 and later[0].risk_usd == 800
    assert (config.RISK_R_PCT, config.MAX_SINGLE_POSITION_PCT, config.MRS_POSITION_CAP) == before


def test_mc027_linked_markdown_discloses_actual_policy_snapshot():
    from trading_system.report import render_markdown
    from trading_system.markets import get_market
    result = _result()
    result.raw["market"] = get_market("us").to_dict()
    result.raw["gate_params"] = asdict(GateParams(**LIMITS))
    text = render_markdown(result)
    assert "单笔计划风险不超过 0.4%" in text
    assert "单票市值不超过 10%" in text and "政策总仓位不超过 50%" in text


def test_mc202_linked_markdown_discloses_actual_calibration_namespace():
    from trading_system.report import render_markdown
    from trading_system.markets import get_market
    result = _result()
    result.raw["market"] = get_market("us").to_dict()
    path = "/isolated/client-27/artifacts/calibration_samples.json"
    result.raw["calibration"] = {"status": "accumulating", "buckets": [],
                                  "samples_path": path, "monotonic": None}
    assert path in render_markdown(result)


def test_mc027_property_journal_defers_then_fills_within_actual_total_exposure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    result = _result(limits=BASELINE)
    result.picks = [TradePick(f"T{i}", 8, 8, "A", 100, 99.9, 200, .2, 20,
                             chain="semis", sector="SMH", card="标准做多") for i in range(8)]
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(result)
    dates = pd.bdate_range("2026-09-28", "2026-10-08")
    frame = pd.DataFrame({"Open": 100, "High": 100.01, "Low": 99.99, "Close": 100}, index=dates)
    provider = SimpleNamespace(ohlcv=lambda *a, **kw: frame)
    journal.settle(provider, as_of="2026-09-29", risk_limits=LIMITS)
    filled = [r for r in journal.records if r.get("entry_date")]
    assert len(filled) == 5 and all(r["filled_shares"] == 100 for r in filled)
    assert sum(r["filled_shares"] * r["entry"] for r in filled) == 50_000
    pending = [r for r in journal.records if not r.get("entry_date")]
    assert len(pending) == 3 and all(r.get("entry_blocked") for r in pending)
    journal.settle(provider, as_of="2026-10-08", risk_limits=BASELINE)
    assert all(r.get("entry_date") for r in journal.records)
    assert {r["entry_date"] for r in journal.records[5:]} == {"2026-10-08"}
    assert all(r["execution_risk_limits"] == LIMITS for r in journal.records)
    for day in dates.date:
        active = [r for r in journal.records if r["entry_date"] <= day.isoformat()
                  and (r.get("exit_date") is None or r["exit_date"] >= day.isoformat())]
        assert sum(r["filled_shares"] * 100 for r in active) <= 50_000


def test_mc027_property_paper_existing_positions_are_preserved_while_new_fills_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    engine = SimEngine(str(tmp_path / "sim.json"))
    engine.state["cash"] = 40_000
    engine.state["positions"] = [{"ticker": "HELD", "shares": 600, "stop": 90,
        "risk_usd": 6000, "entry_price": 100, "entry_date": "2026-09-28", "time_stop_days": 7}]
    _pending(engine, ticker="NEW")
    engine.step("2026-09-29", _result(), lambda _: Bar(100, 101, 99, 100))
    assert [(p["ticker"], p["shares"]) for p in engine.state["positions"]] == [("HELD", 600)]
    assert [p["ticker"] for p in engine.state["pending"]] == ["NEW"]


@pytest.mark.parametrize("limits", [{"risk_r_pct": .009, "max_single_position_pct": .1, "gross_cap": .5},
    {"risk_r_pct": .004, "max_single_position_pct": True, "gross_cap": .5},
    {"risk_r_pct": .004, "max_single_position_pct": .1},
    {"risk_r_pct": .004, "max_single_position_pct": .1, "gross_cap": .5, "extra": 1}])
def test_mc027_property_corrupt_saved_pending_policy_preserves_ledger(tmp_path, limits):
    import json
    path = tmp_path / "sim.json"
    engine = SimEngine(str(path))
    _pending(engine)
    engine.save()
    payload = json.loads(path.read_text())
    payload["pending"][0]["risk_limits"] = limits
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    with pytest.raises((ValueError, TypeError)):
        SimEngine(str(path))
    assert path.read_bytes() == raw


def test_mc027_property_wfa_grid_cannot_widen_customer_policy():
    frames, panel = _panel()
    wfa = run_wfa(frames, panel, grid=[{**BASELINE, "mrs_gate": 6.1}],
                  train=8, test=8, step=8, min_trades=1,
                  base_params=GateParams(max_picks=10, cost_bps=0), risk_limits=LIMITS)
    assert wfa["folds"] and wfa["base_params"]["risk_r_pct"] == .004
    assert all(row["risk_limits"] == LIMITS for row in wfa["folds"])
    assert all(row["gate_params"]["risk_r_pct"] == .004 for row in wfa["folds"])
    assert not wfa["recommended_params"]  # zero-return OOS cannot invent a recommendation


def test_mc027_property_riskoff_keeps_positive_policy_separate_from_zero_market_capacity():
    mrs = _result().mrs
    mrs.allow_new_positions = False
    context = {"mrs": mrs, "risk_limits": LIMITS}
    assert RiskManagerAgent(None).execute(context) == []
    assert context["risk_limits"] == LIMITS and context["gate_params"].gross_cap == .5
    assert context["gross_cap"] == 0 and context["action"] == "AVOID"


def test_mc027_property_concurrent_customers_do_not_share_risk_parameters():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        narrow = pool.submit(_risk, limits=LIMITS)
        broad = pool.submit(_risk, limits=BASELINE)
    assert narrow.result()[0][0].risk_usd == 400
    assert broad.result()[0][0].risk_usd == 800
