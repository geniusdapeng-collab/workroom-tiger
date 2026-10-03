"""MC211/212: observed exchange sessions and already-held chain risk."""
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import config
from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.data_models import MRSResult, PipelineResult, SectorScore, StockCandidate, TradePick
from trading_system.exit_engine import simulate_trade
from trading_system.journal import Journal
from trading_system.markets import get_market
from trading_system.parameters import GateParams
from trading_system.simulator import Bar, SimEngine


LIMITS = {"risk_r_pct": .004, "max_single_position_pct": .10, "gross_cap": .50}


def _frame(dates, prices):
    return pd.DataFrame({"Open": prices, "High": [p + 1 for p in prices],
                         "Low": [p - 1 for p in prices], "Close": prices},
                        index=pd.DatetimeIndex(dates))


def _replay(frame, entry_index, stop=90):
    return simulate_trade(frame.Open.tolist(), frame.High.tolist(), frame.Low.tolist(),
                          frame.Close.tolist(), entry_index, stop, cost_bps=0,
                          dates=list(frame.index.date), market=get_market("cn"), ticker="600000.SS")


def _result():
    return PipelineResult(trade_date="2026-09-28", provider="recorded-fixture", action="BUY",
        mrs=MRSResult(8, 0, 1, 8, position_cap=(.7, .9), allow_new_positions=True),
        raw={"market": get_market("us").to_dict(), "risk_limits": dict(LIMITS),
             "gate_params": asdict(GateParams(max_picks=10, cost_bps=0)), "gross_cap": .5})


def _pending(engine, count=8):
    engine.state["pending"] = [{"ticker": f"T{i}", "chain": "semis", "shares": 80,
        "stop_price": 90., "risk_usd": 800., "entry_ref": 100.,
        "signal_date": "2026-09-28", "time_stop_days": 7} for i in range(count)]


def test_mc211_sample_replay_missing_prior_exchange_session_cannot_buy():
    frame = _frame(["2026-09-25", "2026-09-29"], [100., 100.])
    assert _replay(frame, 1) is None


def test_mc211_property_replay_holiday_observation_cannot_replace_previous_close():
    frame = _frame(["2026-09-30", "2026-10-07", "2026-10-08"], [100., 70., 100.])
    outcome = _replay(frame, 2)
    assert outcome is not None and not outcome.void and outcome.last_i == 2


def test_mc211_property_replay_sale_with_missing_prior_session_retains_position():
    frame = _frame(["2026-09-21", "2026-09-22", "2026-09-24"], [100., 100., 100.])
    frame.loc["2026-09-24", "Low"] = 94.
    outcome = _replay(frame, 1, stop=95)
    assert outcome is not None and outcome.exit_i == -1
    assert "前收价缺失" in outcome.blocked_reason


def test_mc211_property_planner_cannot_use_old_observation_for_cn_previous_close():
    frame = _frame(["2026-09-25", "2026-09-29"], [100., 100.])
    candidate = StockCandidate("600000.SS", chain_id="semis", price=100, stop_price=90,
                               tss_final=8, c_liq=1, atr_pct=.02)
    context = {"mrs": _result().mrs, "trade_date": "2026-09-29",
        "market_spec": get_market("cn"), "market_grad": {"graduated": True},
        "market_data": {"stock_ohlcv": {candidate.ticker: frame}},
        "sectors": [SectorScore("SMH", 8, breadth=70, in_main_pool=True)],
        "watchlist": [candidate]}
    assert RiskManagerAgent(None).execute(context) == []
    assert context["compliance"][0]["rule_id"] == "CN_PREV_CLOSE_MISSING"


@pytest.mark.parametrize("already_held", [False, True])
def test_mc212_property_paper_chain_budget_includes_positions_from_previous_runs(tmp_path, already_held):
    engine = SimEngine(str(tmp_path / "sim.json"))
    if already_held:
        engine.state["cash"] = 90_000
        engine.state["positions"] = [{"ticker": "HELD", "chain": "semis", "shares": 100,
            "stop": 90, "risk_usd": 1000, "entry_price": 100,
            "entry_date": "2026-09-28", "time_stop_days": 7}]
    _pending(engine)
    engine.step("2026-09-29", _result(), lambda _: Bar(100, 101, 99, 100))
    positions = engine.state["positions"]
    assert sum(p["risk_usd"] for p in positions) <= 100_000 * config.MAX_CHAIN_RISK_PCT
    expected_new = int((100_000 * config.MAX_CHAIN_RISK_PCT - (1000 if already_held else 0)) / 400)
    assert len(positions) == expected_new + int(already_held)
    assert len(engine.state["pending"]) == 8 - expected_new


def test_mc212_property_journal_chain_budget_includes_prior_fills(tmp_path):
    result = _result()
    result.picks = [TradePick(f"T{i}", 8, 8, "A", 100, 90, 80, .08, 800,
        chain="semis", sector="SMH", card="标准做多", time_stop_days=7) for i in range(8)]
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(result)
    frame = _frame(["2026-09-28", "2026-09-29", "2026-09-30"], [100., 100., 100.])
    journal.settle(SimpleNamespace(ohlcv=lambda *args, **kwargs: frame), as_of="2026-09-29")
    filled = [r for r in journal.records if r.get("entry_date")]
    assert sum(r["filled_risk_usd"] for r in filled) <= 100_000 * config.MAX_CHAIN_RISK_PCT
    expected_fills = int(100_000 * config.MAX_CHAIN_RISK_PCT / 400)
    assert len(filled) == expected_fills
    assert len([r for r in journal.records if r.get("entry_blocked")]) == 8 - expected_fills
