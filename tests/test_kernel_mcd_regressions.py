"""MC-120: concrete samples and invariant properties for all 19 kernel cards.

Each test's setup is its precondition, actions are explicit, and the assertions
are its expected result. Fixtures use isolated files and deterministic bars;
no network, live money or product accounting ledgers are touched.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_system import config
from trading_system.agents.narrative_agent import NarrativeAgent
from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.agents.sector_agent import SectorAgent
from trading_system.backtest import (CandidateSnap, DayFrame, GateParams, SectorSnap,
                                     _Panel, _gate_day, _simulate_trade,
                                     apply_tuned_params, run_backtest, run_wfa,
                                     save_tuned_params)
from trading_system.cleaning.pipeline import llm_semantic_clean, unwrap_cleaned
from trading_system.data_models import MRSResult, PipelineResult, SectorScore, StockCandidate, TradePick
from trading_system.exit_engine import cost_adj_buy, evaluate_day, simulate_trade, trading_days_held
from trading_system.journal import Journal
from trading_system.ledger_io import ConcurrentUpdateError, file_digest, read_json_strict, write_json_atomic
from trading_system.markets import get_market
from trading_system.options_metrics import OptionsHistoryStore
from trading_system.portfolio import GlobalAllocator, PortfolioRiskOfficer, ReturnSteward
from trading_system.providers.demo import DemoProvider
from trading_system.redline import LLMUnavailable, Passthrough
from trading_system.review.chief import ReviewChief
from trading_system.review.monthly import Proposal, generate_proposal, load_proposal, save_proposal
from trading_system.search.models import CleanDocument, RawDocument
from trading_system.simulator import Bar, SimEngine
from trading_system.tech_chain.agents import ChainRiskAgent, ChainSentimentAgent


def _pick(ticker="AAPL", shares=10, stop=90.0, risk=100.0):
    return TradePick(ticker, 8, 8, "A", 100, stop, shares, .01, risk,
                     chain="semis", sector="SMH", card="标准做多", time_stop_days=7)


def _result(ticker=None, *, action="BUY", score=8.0, allow=True, shock=False, cap=.9, market="us"):
    return PipelineResult(trade_date="2026-09-29", provider="isolated-real", action=action,
                          mrs=MRSResult(score, 0, 1, score, position_cap=(0, cap),
                                        allow_new_positions=allow, shock=shock),
                          picks=[_pick(ticker)] if ticker else [],
                          raw={"market": get_market(market).to_dict(), "gross_cap": cap})


def _pending(engine, ticker="AAPL", *, stop=90.0, risk=100, shares=10):
    engine.state["pending"] = [{"ticker": ticker, "shares": shares, "stop_price": stop,
                                "risk_usd": risk, "signal_date": "2026-09-28",
                                "entry_ref": 100.0, "time_stop_days": 7}]


def _panel(n=12, tickers=("AAPL",), price=100.0):
    dates = pd.bdate_range("2026-09-14", periods=n)
    frame = pd.DataFrame({"Open": price, "High": price * 1.002,
                          "Low": price * .998, "Close": price, "Volume": 1e6}, index=dates)
    return _Panel({ticker: frame.copy() for ticker in tickers}, frame), dates


def _frames(dates, tickers=("AAPL",), *, stop=90, score=8, all_signals=False):
    return [DayFrame(day, score, "neutral", [SectorSnap("SMH", 8, 70)],
                     [CandidateSnap(ticker, 8, 1, 1, "SMH", "semis", False, "A", 100, stop)
                      for ticker in tickers] if all_signals or i == 0 else [])
            for i, day in enumerate(dates)]


def _proposal(out, params=None):
    root = Path(out) / "review_proposals"
    proposal = generate_proposal({"dsr": .97, "recommended_params": params or {"mrs_gate": 6.5},
                                  "oos_aggregate": {"expectancy_r": .3}, "n_folds": 3,
                                  "grid_size": 4}, str(root))
    chief = ReviewChief(str(out), proposals_dir=str(root))
    path = root / f"{proposal.proposal_id}.json"
    return chief, proposal, path, file_digest(path)


def _approve(out, execution="approved-one", params=None):
    chief, proposal, path, digest = _proposal(out, params)
    done = chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id=execution)
    return chief, done, path, digest


def _sim_file(directory, curve, *, cash=None, currency=None):
    directory.mkdir(parents=True, exist_ok=True)
    value = {"equity_curve": [{"date": day, "equity": equity} for day, equity in curve],
             "cash": curve[-1][1] if cash is None else cash, "positions": [], "pending": []}
    if currency:
        value["currency"] = currency
    write_json_atomic(directory / "sim_portfolio.json", value)


# MC-001: current-day risk permission governs yesterday's pending fills.
@pytest.mark.parametrize("action,allow,shock,score", [
    ("AVOID", True, False, 8), ("BUY", False, False, 8),
    ("BUY", True, True, 8), ("BUY", True, False, 3.99),
    ("BUY", True, False, float("nan")), ("BUY", True, False, 11),
])
def test_mc001_sample_risk_block(tmp_path, action, allow, shock, score):
    engine = SimEngine(str(tmp_path / "sim.json"))
    _pending(engine)
    engine.step("2026-09-29", _result(action=action, allow=allow, shock=shock, score=score),
                lambda _: Bar(100, 101, 99, 100, date="2026-09-29"))
    assert engine.state["cash"] == 100_000
    assert not engine.state["positions"] and len(engine.state["pending"]) == 1


def test_mc001_property_buy_permission_and_per_run_block(tmp_path):
    rng = random.Random(120001)
    for index in range(20):
        score = rng.uniform(0, 10)
        engine = SimEngine(str(tmp_path / f"risk-{index}.json"))
        _pending(engine)
        result = _result(score=score)
        engine.step("2026-09-29", result, lambda _: Bar(100, 101, 99, 100))
        assert bool(engine.state["positions"]) == (score >= config.MRS_GATE_BLOCK)
    engine = SimEngine(str(tmp_path / "override.json"))
    _pending(engine)
    result = _result(score=5.4)
    result.raw["gate_params"] = asdict(GateParams(mrs_block=5.5))
    engine.step("2026-09-29", result, lambda _: Bar(100, 101, 99, 100))
    assert not engine.state["positions"]


# MC-002: transactional RMW and explicit stale-save rejection.
@pytest.mark.parametrize("kind", ["journal", "sim"])
def test_mc002_sample_cas_rejects_stale_snapshot(tmp_path, kind):
    path = tmp_path / f"{kind}.json"
    first = Journal(path) if kind == "journal" else SimEngine(str(path))
    first.save()
    stale = Journal(path) if kind == "journal" else SimEngine(str(path))
    if kind == "journal":
        first.log_picks(_result("AAPL"))
        stale.records.append({"date": "2026-09-29", "ticker": "MSFT", "status": "open"})
    else:
        first.step("2026-09-29", _result("AAPL"), lambda _: None)
        stale.state["cash"] = 80_000
    committed = path.read_bytes()
    with pytest.raises(ConcurrentUpdateError):
        stale.save()
    assert path.read_bytes() == committed


@pytest.mark.parametrize("kind", ["journal", "sim"])
def test_mc002_property_interleaved_threads_retain_every_signal(tmp_path, kind):
    path = tmp_path / f"{kind}.json"
    seed = Journal(path) if kind == "journal" else SimEngine(str(path))
    seed.save()
    barrier = threading.Barrier(4)
    def worker(worker_id):
        ledger = Journal(path) if kind == "journal" else SimEngine(str(path))
        barrier.wait(timeout=30)
        for item in range(5):
            result = _result(f"T{worker_id}{item}")
            if kind == "journal":
                ledger.log_picks(result)
            else:
                ledger.step("2026-09-29", result, lambda _: None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))
    value = read_json_strict(path)
    rows = value if kind == "journal" else value["pending"]
    assert {row["ticker"] for row in rows} == {f"T{a}{b}" for a in range(4) for b in range(5)}
    assert len(rows) == 20


# MC-003: preserve malformed/non-finite evidence and expose persistence failure.
def test_mc003_sample_corrupt_history_bytes_are_preserved(tmp_path):
    target = tmp_path / "AAPL.json"
    original = b'[{"date":"2026-09-28","atm_iv":Infinity}]'
    target.write_bytes(original)
    with pytest.raises(ValueError):
        OptionsHistoryStore(tmp_path).append("AAPL", {"atm_iv": .2})
    assert target.read_bytes() == original


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, -1, "1"])
def test_mc003_property_invalid_numbers_cannot_commit(tmp_path, bad):
    store = OptionsHistoryStore(tmp_path)
    store.append("AAPL", {"atm_iv": .2, "pcr_oi": 1, "call_oi": 100}, as_of="2026-09-28")
    original = (tmp_path / "AAPL.json").read_bytes()
    for key in ("atm_iv", "pcr_oi", "call_oi"):
        with pytest.raises(ValueError):
            store.append("AAPL", {key: bad}, as_of="2026-09-29")
        assert (tmp_path / "AAPL.json").read_bytes() == original


def test_mc003_property_atomic_failure_and_out_of_order_history(tmp_path, monkeypatch):
    store = OptionsHistoryStore(tmp_path)
    store.append("AAPL", {"pcr_oi": 1}, as_of="2026-09-28")
    original = (tmp_path / "AAPL.json").read_bytes()
    with pytest.raises(ValueError):
        store.append("AAPL", {"pcr_oi": 2}, as_of="2026-09-25")
    replace = os.replace
    def fail(source, target):
        if Path(target) == tmp_path / "AAPL.json":
            raise OSError("genuine persistence failure")
        return replace(source, target)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="genuine persistence failure"):
        store.append("AAPL", {"pcr_oi": 2}, as_of="2026-09-29")
    assert (tmp_path / "AAPL.json").read_bytes() == original


# MC-201: array consumers cannot read beyond IS/OOS sample boundaries.
def test_mc201_sample_last_signal_cannot_fill_in_next_sample():
    panel, dates = _panel()
    frames = _frames(dates[:3])
    frames[0].candidates = []
    frames[-1].candidates = _frames(dates[:1])[0].candidates
    assert run_backtest(frames, panel)["trades"] == []
    explicit = run_backtest(frames, panel, sample_end=dates[-1])
    assert len(explicit["trades"]) == 1


def test_mc201_property_future_price_perturbation_preserves_sample_results():
    base, dates = _panel(20)
    frames = _frames(dates[:5])
    params = GateParams(time_stop=12, cost_bps=0)
    result = run_backtest(frames, base, params)
    for price in (1, 30, 103, 300, 10_000):
        changed, _ = _panel(20)
        for column, factor in (("open", 1), ("high", 1.01), ("low", .99), ("close", 1)):
            getattr(changed, column).iloc[5:] = price * factor
        other = run_backtest(frames, changed, params)
        assert other["expectancy_r"] == result["expectancy_r"]
        assert other["port_returns"] == result["port_returns"]
        assert all(trade.exit_date <= str(dates[4].date()) for trade in other["trades"])


def test_mc201_property_overlapping_wfa_dates_are_unique():
    panel, dates = _panel(30)
    output = run_wfa(_frames(dates, all_signals=True), panel,
                     grid=[{"time_stop": 2}], train=8, test=8, step=4, min_trades=1)
    assert output["oos_dates"] == sorted(set(output["oos_dates"]))
    assert len(output["oos_returns"]) == len(output["oos_dates"])
    assert len(output["oos_dates"]) < output["n_folds"] * 8
    assert all(row["train"].split("~")[1] < row["test"].split("~")[0] for row in output["folds"])


# MC-202: no partial global mutation; isolated per-run thresholds/diagnostics.
def test_mc202_sample_approved_loader_returns_without_global_mutation(tmp_path):
    before = (dict(config.OPEN_LONG), config.SHS_MAIN_POOL)
    _, proposal, _, _ = _approve(tmp_path)
    active = tmp_path / "tuned_params.json"
    assert apply_tuned_params(str(active), as_of=str(date.fromisoformat(proposal.effective_from) - timedelta(days=1))) is None
    assert apply_tuned_params(str(active), as_of=proposal.effective_from) == {"mrs_gate": 6.5}
    assert (dict(config.OPEN_LONG), config.SHS_MAIN_POOL) == before
    active.write_text('{"params":{"mrs_gate":9.5,"shs_main":"invalid"}}')
    assert apply_tuned_params(str(active), as_of=proposal.effective_from) is None
    assert (dict(config.OPEN_LONG), config.SHS_MAIN_POOL) == before


def test_mc202_property_parallel_pipelines_do_not_share_params_or_diagnostics(tmp_path, monkeypatch):
    from trading_system import pipeline
    state = threading.local()
    class TaggedDemo(DemoProvider):
        def __init__(self, tag):
            super().__init__()
            self.name = f"{tag}-demo"
    class Offline:
        def complete_json(self, **kwargs):
            raise LLMUnavailable("isolated model channel")
    monkeypatch.setattr(pipeline, "get_provider", lambda name=None: state.provider)
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(tmp_path / "samples.json"))
    paths = []
    for tag, value in (("A", 6.5), ("B", 9.5)):
        _, proposal, _, _ = _approve(tmp_path / tag, f"exec-{tag}", {"mrs_gate": value})
        paths.append((tag, value, proposal.effective_from, str(tmp_path / tag / "tuned_params.json")))
    before = dict(config.OPEN_LONG)
    def run(spec):
        tag, value, day, path = spec
        state.provider = TaggedDemo(tag)
        result = pipeline.run_pipeline(provider_name="demo", universe_mode="core", top_n=3,
                                       max_picks=1, trade_date=day, use_tuned=True,
                                       tuned_path=path, llm_client=Offline())
        assert result.raw["gate_params"]["mrs_gate"] == value
        assert set(result.raw["provider_health"]) == {f"{tag}-demo"}
        assert all(source == f"{tag}-demo" for _, source in result.raw["source_lineage"])
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, paths))
    assert len(results) == 2 and config.OPEN_LONG == before


def test_mc202_property_risk_card_and_action_follow_run_parameters():
    candidate = StockCandidate("AAPL", chain_id="semis", price=100, stop_price=90,
                               tss_final=8, c_liq=1, atr_pct=.02)
    params = GateParams(mrs_gate=7, time_stop=3, profit_protect_r=1.5)
    context = {"mrs": _result(score=6.5).mrs, "gate_params": params,
               "sectors": [SectorScore("SMH", 8, breadth=70, in_main_pool=True)],
               "watchlist": [candidate]}
    picks = RiskManagerAgent(None).execute(context)
    assert picks and context["action"] == "HOLD"
    assert "轻仓" in context["market_view"]
    assert picks[0].time_stop_days == 3
    assert "第 3 个交易日" in picks[0].card and "1.5 倍初始风险" in picks[0].card
    assert "初始 +0.5R" in picks[0].card
    rationale = context["pick_rationale"]["AAPL"]
    assert not rationale["standard"] and all(g["ok"] for g in rationale["gate"].values())


def test_mc202_property_wfa_uses_nondefault_account_and_slot_limit():
    panel, dates = _panel(30, ("AAPL", "MSFT"))
    frames = _frames(dates, ("AAPL", "MSFT"), all_signals=True)
    base = GateParams(max_picks=1, time_stop=2, cost_bps=0)
    small = run_wfa(frames, panel, grid=[{}], train=8, test=8, step=8,
                    min_trades=1, base_params=base, account_usd=1_000)
    large = run_wfa(frames, panel, grid=[{}], train=8, test=8, step=8,
                    min_trades=1, base_params=base, account_usd=100_000)
    assert small["account_usd"] == 1_000 and large["account_usd"] == 100_000
    assert small["base_params"]["max_picks"] == 1
    # $1000 has an $8 risk budget and cannot buy one $10-risk share.
    assert all(row["is_trades"] == row["oos_trades"] == 0 for row in small["folds"])
    assert all(row["oos_trades"] > 0 for row in large["folds"])
    assert all(row["oos_trades"] <= 4 for row in large["folds"])


def test_mc202_property_journal_snapshots_exit_parameters(tmp_path):
    dates = pd.DatetimeIndex(["2026-09-28", "2026-09-29", "2026-09-30"])
    df = pd.DataFrame({"Open": [100, 100, 110], "High": [101, 116, 111],
                       "Low": [99, 99, 104], "Close": [100, 110, 106]}, index=dates)
    class Provider:
        def ohlcv(self, *args, **kwargs):
            return df
    result = _result("AAPL")
    result.trade_date = "2026-09-28"
    result.raw["gate_params"] = asdict(GateParams(profit_protect_r=1.5, cost_bps=0))
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(result)
    assert journal.settle(Provider(), as_of="2026-09-30") == 1
    record = journal.records[0]
    assert record["exit"] == 105 and record["r"] == .5 and record["protected"]


def test_mc202_property_paper_position_retains_signal_exit_parameters(tmp_path):
    engine = SimEngine(str(tmp_path / "sim.json"))
    result = _result("AAPL")
    result.raw["gate_params"] = asdict(GateParams(profit_protect_r=1.5, cost_bps=0))
    engine.step("2026-09-28", result, lambda _: None)
    later = _result()
    later.raw["gate_params"] = asdict(GateParams(profit_protect_r=3, cost_bps=100))
    engine.step("2026-09-29", later, lambda _: Bar(100, 116, 99, 110))
    engine.step("2026-09-30", later, lambda _: Bar(110, 111, 104, 106))
    closed = engine.state["closed"][0]
    assert closed["entry"] == 100 and closed["exit"] == 105 and closed["r_multiple"] == .5


def test_mc202_property_main_purge_uses_only_selected_run_root(tmp_path, monkeypatch):
    import main
    roots = {name: tmp_path / name for name in ("current", "neighbor", "cwd")}
    for name, root in roots.items():
        (root / "cache" / "search").mkdir(parents=True)
        (root / "cache" / "search" / "stale.json").write_text(name)
    protected = roots["current"] / "journal.json"
    protected.write_text("[]")
    tuned = roots["current"] / "tuned_params.json"
    tuned.write_text("{}")
    args = SimpleNamespace(out=str(roots["current"]), demo=True, universe="core",
                           universe_file=None, top=3, picks=1, account=100_000,
                           use_tuned=False, market="us")
    observed = {}
    def boundary(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("pipeline checkpoint")
    monkeypatch.setattr(main, "run_pipeline", boundary)
    monkeypatch.chdir(roots["cwd"])
    with pytest.raises(RuntimeError, match="pipeline checkpoint"):
        main._daily(args, "demo")
    assert not (roots["current"] / "cache" / "search").exists()
    for name in ("neighbor", "cwd"):
        assert (roots[name] / "cache" / "search" / "stale.json").read_text() == name
    assert protected.read_text() == "[]" and tuned.read_text() == "{}"
    assert Path(observed["run_state_dir"]).resolve() == roots["current"]
    assert Path(observed["ledger_dir"]).resolve() == roots["current"]


def test_mc202_property_purge_preserves_nested_accounting_evidence(tmp_path):
    from trading_system.state import purge_run_state
    cache = tmp_path / "cache" / "search"
    cache.mkdir(parents=True)
    protected = {"journal.json": b"[]", "journal.json.lock": b"lock-sentinel",
                 "tuned_params.json": b"{}", "governance_events.jsonl": b"event-sentinel"}
    for name, value in protected.items():
        (cache / name).write_bytes(value)
    (cache / "stale.json").write_text("stale")
    executions = cache / "review_executions"
    executions.mkdir()
    (executions / "approved.json").write_text("receipt-sentinel")
    purge_run_state(str(tmp_path))
    assert not (cache / "stale.json").exists()
    assert all((cache / name).read_bytes() == value for name, value in protected.items())
    assert (executions / "approved.json").read_text() == "receipt-sentinel"


def test_mc202_property_purge_cannot_follow_a_neighbor_cache_symlink(tmp_path):
    from trading_system.state import purge_run_state
    root, neighbor = tmp_path / "current", tmp_path / "neighbor"
    root.mkdir()
    (neighbor / "search").mkdir(parents=True)
    sentinel = neighbor / "search" / "protected.json"
    sentinel.write_text("neighbor-sentinel")
    (root / "cache").symlink_to(neighbor, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        purge_run_state(str(root))
    assert sentinel.read_text() == "neighbor-sentinel"


def test_mc202_property_purge_propagates_actual_delete_failure(tmp_path, monkeypatch):
    from trading_system.state import purge_run_state
    cache = tmp_path / "cache" / "search"
    cache.mkdir(parents=True)
    sentinel = cache / "stale.json"
    sentinel.write_text("stale")
    unlink = os.unlink
    def fail(path, *args, **kwargs):
        if Path(path).name == "stale.json":
            raise PermissionError("cache deletion denied")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", fail)
    with pytest.raises(PermissionError, match="cache deletion denied"):
        purge_run_state(str(tmp_path))
    assert sentinel.read_text() == "stale"


# MC-203: model-output shape and finite ranges at consumer boundaries.
class _LLM:
    def __init__(self, value):
        self.value = value
    def complete_json(self, **kwargs):
        return self.value


def test_mc203_sample_nonfinite_narrative_is_passthrough():
    for value in ("NaN", "Infinity", "-Infinity", float("inf")):
        out = NarrativeAgent(_LLM({"sectors": [{"etf": "SMH", "narrative_score": value}]})).execute([], ["SMH"])
        assert isinstance(out, Passthrough)


@pytest.mark.parametrize("value", [None, [], True, {"sectors": {}}, {"chains": "broken"}, {"alerts": 1}, {"documents": None}])
def test_mc203_property_wrong_containers_never_create_semantic_scores(value):
    docs = [CleanDocument(RawDocument("doc", "fixture", "title", "url", "content long enough to preserve", published="2026-09-28"))]
    for output in (NarrativeAgent(_LLM(value)).execute(docs, ["SMH"]),
                   ChainSentimentAgent(_LLM(value)).execute(docs),
                   ChainRiskAgent(_LLM(value)).execute(docs)):
        assert isinstance(output, Passthrough)
    cleaned = unwrap_cleaned(llm_semantic_clean([doc.raw for doc in docs], _LLM(value)))
    assert all(doc.degraded and doc.sentiment_score is None for doc in cleaned)


def test_mc203_property_out_of_range_and_nontext_lists_are_missing():
    doc = RawDocument("doc", "fixture", "title", "url", "content long enough to preserve", published="2026-09-28")
    for score in (-1.1, 1.1, "NaN", "1e999", True, {"value": .5}):
        output = {"documents": [{"id": "doc", "tickers": ["AAPL"], "events": [],
                                  "sentiment_score": score, "summary_zh": "s", "relevance": .5}]}
        result = unwrap_cleaned(llm_semantic_clean([doc], _LLM(output)))[0]
        assert result.degraded and result.sentiment_score is None
    output = {"chains": [{"chain_id": "memory", "heat": 8, "sentiment_score": .5,
                           "key_drivers": [123]}]}
    assert isinstance(ChainSentimentAgent(_LLM(output)).execute([]), Passthrough)


# MC-204: same intraday protection, stop priority, and unconditional expiry.
def test_mc204_sample_high_trigger_and_next_bar_protected_stop():
    output = simulate_trade([100, 110], [125, 111], [99, 104], [110, 106], 0, 90, cost_bps=0)
    assert output.protected and output.exit_i == 1 and output.exit_price == 105
    assert output.r == .5
    priority = evaluate_day(100, 125, 89, 105, 100, 90, 1)
    assert priority.action == "exit" and priority.exit_price == 90
    expiry = evaluate_day(103, 104, 102, 103, 100, 90, 7)
    assert expiry.action == "exit" and expiry.reason.startswith("时间止损")


def test_mc204_property_daily_and_batch_transitions_agree():
    rng = random.Random(120204)
    for _ in range(40):
        opens, highs, lows, closes = [], [], [], []
        for index in range(10):
            opening = 100 if index == 0 else rng.uniform(90, 125)
            closing = rng.uniform(90, 125)
            opens.append(opening); closes.append(closing)
            highs.append(max(opening, closing) + rng.uniform(0, 5))
            lows.append(min(opening, closing) - rng.uniform(0, 5))
        batch = simulate_trade(opens, highs, lows, closes, 0, 90, cost_bps=0, time_stop=7)
        stop, protected, expected = 90, False, None
        for index in range(10):
            transition = evaluate_day(opens[index], highs[index], lows[index], closes[index],
                                      100, stop, index + 1, time_stop=7,
                                      initial_stop=90, initial_r=10, protected=protected)
            stop, protected = transition.new_stop, transition.protected
            if transition.action == "exit":
                expected = (index, transition.exit_price)
                break
        assert (batch.exit_i, batch.exit_price) == expected
        assert batch.protected == protected


def test_mc204_property_journal_simulator_and_batch_share_fill_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    dates = pd.DatetimeIndex(["2026-09-28", "2026-09-29", "2026-09-30"])
    df = pd.DataFrame({"Open": [100, 100, 110], "High": [101, 125, 111],
                       "Low": [99, 99, 104], "Close": [100, 110, 106]}, index=dates)
    record = {"date": "2026-09-28", "ticker": "AAPL", "status": "open", "entry_ref": 100,
              "stop": 90, "time_stop_days": 7}
    Journal._settle_one(record, df, as_of="2026-09-30")
    engine = SimEngine(str(tmp_path / "sim.json"))
    _pending(engine, shares=100, risk=1000)
    engine.step("2026-09-29", _result(), lambda _: Bar(100, 125, 99, 110))
    engine.step("2026-09-30", _result(action="AVOID"), lambda _: Bar(110, 111, 104, 106))
    batch = simulate_trade(df.Open.tolist(), df.High.tolist(), df.Low.tolist(), df.Close.tolist(), 1, 90, cost_bps=0)
    assert record["exit"] == engine.state["closed"][0]["exit"] == batch.exit_price == 105
    assert record["r"] == engine.state["closed"][0]["r_multiple"] == batch.r == .5


# MC-205: published 2026 mainland exchange calendar is the independent oracle.
def test_mc205_sample_missing_published_closures_are_closed():
    for day in ("2026-01-02", "2026-02-23", "2026-05-04", "2026-05-05"):
        assert not get_market("cn").is_trading_day(day)


def test_mc205_property_every_2026_day_matches_exchange_closures():
    closure_ranges = [("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
                      ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
                      ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
                      ("2026-10-01", "2026-10-07")]
    closures = {day.date() for first, last in closure_ranges for day in pd.date_range(first, last)}
    market = get_market("cn")
    for stamp in pd.date_range("2026-01-01", "2026-12-31"):
        day = stamp.date()
        assert market.is_trading_day(day) == (day.weekday() < 5 and day not in closures), day
    assert str(market.next_trading_day("2026-09-30")) == "2026-10-08"
    assert str(market.prev_trading_day("2026-10-07")) == "2026-09-30"
    assert not market.check_order("buy", "600000.SS", 100, 100, "2027-01-04").allowed


# MC-206: HKEX phased tick changes and inclusive upper-bound behavior.
def test_mc206_sample_current_phase_two_spreads():
    market = get_market("hk")
    for price, tick in ((.6, .005), (10, .005), (15, .01), (20, .01), (25, .02), (50, .02)):
        assert market.min_tick(price, "2026-10-02") == tick


def test_mc206_property_full_spread_boundaries_and_historical_phase():
    table = [(.25, .001), (.5, .005), (10, .005), (20, .01), (50, .02), (100, .05),
             (200, .1), (500, .2), (1000, .5), (2000, 1), (5000, 2), (9995, 5)]
    market = get_market("hk")
    for index, (cap, tick) in enumerate(table):
        assert market.min_tick(cap, "2026-10-02") == tick
        if index + 1 < len(table):
            assert market.min_tick(cap + .0001, "2026-10-02") == table[index + 1][1]
    assert market.min_tick(1, "2026-08-02") == .01
    assert market.min_tick(15, "2025-08-03") == .02
    assert market.min_tick(25, "2025-08-03") == .05
    assert market.min_tick(25, "2025-08-04") == .02
    for price in (True, float("nan"), float("inf"), 0, 10_000):
        with pytest.raises(ValueError):
            market.min_tick(price)
    with pytest.raises(ValueError):
        market.min_tick(25, security_type="etp")


# MC-207: asynchronous calendars, latest loss and native-currency scale invariance.
def test_mc207_sample_latest_loss_is_included(tmp_path):
    _sim_file(tmp_path / "us", [("2026-09-28", 100_000), ("2026-09-29", 100_000), ("2026-09-30", 50_000)], currency="USD")
    _sim_file(tmp_path / "hk", [("2026-09-28", 780_000), ("2026-09-30", 780_000)], currency="HKD")
    report = ReturnSteward().assess({m: str(tmp_path / m) for m in ("us", "hk")})
    assert report.dates[-1] == "2026-09-30" and report.total_return == -.25 and report.max_drawdown == -.25


def test_mc207_property_native_scaling_and_common_start_do_not_change_nav(tmp_path):
    outputs = []
    for scale in (1, 7.8, 1000):
        root = tmp_path / str(scale)
        _sim_file(root / "us", [("2026-09-25", 100), ("2026-09-28", 110), ("2026-09-29", 88), ("2026-09-30", 99)])
        _sim_file(root / "hk", [("2026-09-28", 200 * scale), ("2026-09-30", 180 * scale)])
        report = ReturnSteward().assess({m: str(root / m) for m in ("us", "hk")})
        assert report.dates == ["2026-09-28", "2026-09-29", "2026-09-30"]
        assert report.nav == pytest.approx([1, .9, .9])
        outputs.append((report.total_return, report.max_drawdown))
    assert len(set(outputs)) == 1


# MC-208: report observed exposure, and unknown cross-currency totals explicitly.
def test_mc208_sample_observed_gross_breach(tmp_path):
    _sim_file(tmp_path / "us", [("2026-09-29", 100_000)], cash=0, currency="USD")
    report = PortfolioRiskOfficer(.9).view({"us": str(tmp_path / "us")})
    assert report.over_cap is True and report.gross_exposure == 1


def test_mc208_property_cap_comparison_and_fx_unknown(tmp_path):
    for index, gross in enumerate((0, .1, .5, .9, .90001, 1)):
        directory = tmp_path / str(index)
        _sim_file(directory, [("2026-09-29", 100_000)], cash=100_000 * (1 - gross), currency="USD")
        report = PortfolioRiskOfficer(.9).view({"us": str(directory)})
        assert report.over_cap == (gross > .9 + 1e-9)
        assert report.gross_exposure == pytest.approx(gross)
    _sim_file(tmp_path / "usd", [("2026-09-29", 100_000)], currency="USD")
    _sim_file(tmp_path / "hkd", [("2026-09-29", 780_000)], currency="HKD")
    mixed = PortfolioRiskOfficer().view({"us": str(tmp_path / "usd"), "hk": str(tmp_path / "hkd")})
    assert mixed.total_equity is None and mixed.over_cap is None and mixed.gross_exposure is None
    assert mixed.by_currency == {"USD": 100_000, "HKD": 780_000} and mixed.concentration == {}


# MC-209: reviewed snapshots, immutable receipts, rollback and crash recovery.
@pytest.mark.parametrize("stage", ["intent", "effect", "active", "proposal", "receipt"])
def test_mc209_sample_io_fault_never_leaves_ghost_approval(tmp_path, monkeypatch, stage):
    _, old, _, _ = _approve(tmp_path, "old")
    active = tmp_path / "tuned_params.json"
    old_active = active.read_bytes()
    chief, proposal, proposal_path, digest = _proposal(tmp_path, {"mrs_gate": 7.0})
    original_proposal = proposal_path.read_bytes()
    paths = {"intent": tmp_path / "review_executions" / "new.intent.json",
             "effect": tmp_path / "review_executions" / "new.effect.json", "active": active,
             "proposal": proposal_path, "receipt": tmp_path / "review_executions" / "new.json"}
    replace, fired = os.replace, []
    def fail(source, target):
        if Path(target) == paths[stage] and not fired:
            fired.append(True)
            raise OSError(f"{stage} disk failure")
        return replace(source, target)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="new")
    assert fired and proposal_path.read_bytes() == original_proposal and active.read_bytes() == old_active
    assert load_proposal(str(proposal_path.parent), proposal.proposal_id).status == "pending_review"
    assert apply_tuned_params(str(active), as_of=old.effective_from) == {"mrs_gate": 6.5}
    done = chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="new")
    assert done.status == "approved" and apply_tuned_params(str(active), as_of=done.effective_from) == {"mrs_gate": 7.0}
    receipt = read_json_strict(paths["receipt"])
    assert receipt["tuned_path"] == str(paths["effect"])
    assert file_digest(paths["effect"]) == receipt["tuned_sha256"]


@pytest.mark.parametrize("stage", ["effect", "active", "proposal", "receipt"])
def test_mc209_property_process_crash_recovers_same_execution(tmp_path, monkeypatch, stage):
    chief, proposal, proposal_path, digest = _proposal(tmp_path)
    active = tmp_path / "tuned_params.json"
    paths = {"effect": tmp_path / "review_executions" / "crash.effect.json", "active": active,
             "proposal": proposal_path, "receipt": tmp_path / "review_executions" / "crash.json"}
    replace, fired = os.replace, []
    def crash(source, target):
        if Path(target) == paths[stage] and not fired:
            fired.append(True)
            raise SystemExit("simulated process termination")
        return replace(source, target)
    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(SystemExit):
        chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="crash")
    assert apply_tuned_params(str(active), as_of="2099-01-01") is None
    done = chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="crash")
    receipt = paths["receipt"].read_bytes()
    repeated = chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="crash")
    assert repeated.execution_id == done.execution_id and paths["receipt"].read_bytes() == receipt
    assert apply_tuned_params(str(active), as_of=done.effective_from) == {"mrs_gate": 6.5}


def test_mc209_property_snapshot_expiry_authority_and_research_only_proposes(tmp_path):
    chief, proposal, path, digest = _proposal(tmp_path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="snapshot"):
        chief.approve(proposal.proposal_id, execution_id="no-snapshot")
    with pytest.raises(ValueError, match="snapshot changed"):
        chief.approve(proposal.proposal_id, expected_sha256="0" * 64, execution_id="wrong")
    assert path.read_bytes() == original and not (tmp_path / "tuned_params.json").exists()
    proposal.expires_at = (datetime.now() - timedelta(days=1)).isoformat()
    save_proposal(proposal, str(path.parent))
    with pytest.raises(ValueError, match="expired"):
        chief.approve(proposal.proposal_id, expected_sha256=file_digest(path), execution_id="expired")
    other = tmp_path / "research"
    other.mkdir()
    saved = save_tuned_params({"dsr": .97, "recommended_params": {"mrs_gate": 6.5},
                              "oos_aggregate": {"expectancy_r": .3}}, str(other / "tuned_params.json"))
    assert Path(saved).parent == other / "review_proposals"
    assert not (other / "tuned_params.json").exists()


def test_mc209_property_two_reviewers_consume_one_snapshot_once(tmp_path):
    chief, proposal, path, digest = _proposal(tmp_path)
    barrier = threading.Barrier(2)
    def review(index):
        barrier.wait(timeout=30)
        try:
            if index:
                return chief.reject(proposal.proposal_id, "independent rejection", expected_sha256=digest, execution_id="reject-one").status
            return chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="approve-one").status
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(review, (0, 1)))
    assert statuses.count("blocked") == 1
    assert load_proposal(str(path.parent), proposal.proposal_id).status in ("approved", "rejected")
    receipts = list((tmp_path / "review_executions").glob("*.json"))
    assert len([p for p in receipts if not p.name.endswith((".effect.json", ".intent.json"))]) == 1


@pytest.mark.parametrize("artifact", ["active", "effect", "proposal", "receipt"])
def test_mc209_property_tampered_approval_artifact_never_activates(tmp_path, artifact):
    _, done, path, _ = _approve(tmp_path)
    active = tmp_path / "tuned_params.json"
    targets = {"active": active, "effect": Path(done.tuned_path), "proposal": path,
               "receipt": tmp_path / "review_executions" / "approved-one.json"}
    value = read_json_strict(targets[artifact])
    value["tampered"] = True
    write_json_atomic(targets[artifact], value)
    assert apply_tuned_params(str(active), as_of=done.effective_from) is None


# MC-210: timestamps, suspension gaps, and no future settlement.
def test_mc210_sample_first_strictly_later_observation():
    record = {"date": "2026-09-25", "ticker": "AAPL", "status": "open", "entry_ref": 100, "stop": 90}
    df = pd.DataFrame({"Open": [100, 100], "High": [101, 101], "Low": [99, 99], "Close": [100, 100]},
                      index=pd.DatetimeIndex(["2026-09-28", "2026-09-29"]))
    Journal._settle_one(record, df, as_of="2026-09-28")
    assert record["entry_date"] == "2026-09-28" and record["status"] == "open"
    assert record.get("exit_date") is None


def test_mc210_property_missing_signal_bar_and_cutoff_never_shift_entry():
    dates = pd.bdate_range("2026-09-21", periods=12)
    df = pd.DataFrame({"Open": 100, "High": 101, "Low": 99, "Close": 100}, index=dates)
    for signal_index in range(5):
        record = {"date": str(dates[signal_index].date()), "ticker": "AAPL", "status": "open", "entry_ref": 100, "stop": 90}
        data = df.drop(index=dates[signal_index])
        Journal._settle_one(record, data, as_of=str(dates[signal_index + 1].date()))
        assert record["entry_date"] == str(dates[signal_index + 1].date())
        assert record["status"] == "open"
        untouched = {"date": str(dates[signal_index].date()), "ticker": "AAPL", "status": "open", "entry_ref": 100, "stop": 90}
        Journal._settle_one(untouched, df, as_of=untouched["date"])
        assert "entry_date" not in untouched


def test_mc210_property_public_settlement_records_the_next_session(tmp_path):
    dates = pd.DatetimeIndex(["2026-09-28", "2026-09-29", "2026-09-30"])
    df = pd.DataFrame({"Open": [100, 100, 100], "High": [101, 101, 101],
                       "Low": [99, 99, 89], "Close": [100, 100, 90]}, index=dates)
    class Provider:
        def ohlcv(self, *args, **kwargs):
            return df
    result = _result("AAPL")
    result.trade_date = "2026-09-28"
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(result)
    assert journal.settle(Provider(), as_of="2026-09-29") == 0
    record = Journal(tmp_path / "journal.json").records[0]
    assert record["entry_date"] == "2026-09-29" and record["status"] == "open"
    assert record["exit_date"] is None  # supplied future stop bar is not consumed


# MC-211: market fences at actual fill and exit, including holiday deferral.
def test_mc211_sample_cn_t1_defers_real_stop_until_later_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COST_BPS", 0)
    engine = SimEngine(str(tmp_path / "cn.json"))
    _pending(engine, ticker="600000.SS", stop=98, risk=200, shares=100)
    engine.step("2026-09-29", _result(market="cn"), lambda _: Bar(100, 101, 97, 97.5, prev_close=100))
    assert len(engine.state["positions"]) == 1 and not engine.state["closed"]
    assert "T+1" in engine.state["positions"][0]["blocked_exit"]
    engine.step("2026-09-30", _result(action="AVOID", market="cn"),
                lambda _: Bar(97.5, 99, 96, 98, prev_close=97.5))
    assert not engine.state["positions"] and engine.state["closed"][0]["exit"] == 97.5


def test_mc211_property_holiday_and_missing_previous_close_cannot_fill(tmp_path):
    for day in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07"):
        engine = SimEngine(str(tmp_path / f"{day}.json"))
        _pending(engine, ticker="600000.SS", stop=90)
        engine.step(day, _result(market="cn"), lambda _: Bar(100, 101, 99, 100, prev_close=100))
        assert not engine.state["positions"] and engine.state["cash"] == 100_000
    engine = SimEngine(str(tmp_path / "missing-prev.json"))
    _pending(engine, ticker="600000.SS", stop=90)
    engine.state["pending"][0].pop("entry_ref")
    engine.step("2026-09-29", _result(market="cn"), lambda _: Bar(100, 101, 99, 100))
    assert not engine.state["positions"]
    engine.step("2026-09-30", _result(market="cn"), lambda _: Bar(100, 101, 99, 100, prev_close=100))
    assert len(engine.state["positions"]) == 1
    assert trading_days_held("2026-09-29", "2026-10-14", get_market("cn")) == 7


def test_mc211_property_entry_reference_cannot_replace_missing_previous_close(tmp_path):
    engine = SimEngine(str(tmp_path / "cn.json"))
    _pending(engine, ticker="600000.SS", stop=90)
    assert engine.state["pending"][0]["entry_ref"] == 100
    engine.step("2026-09-29", _result(market="cn"), lambda _: Bar(100, 101, 99, 100))
    assert not engine.state["positions"] and len(engine.state["pending"]) == 1
    engine.step("2026-09-30", _result(market="cn"),
                lambda _: Bar(100, 101, 99, 100, prev_close=100))
    assert len(engine.state["positions"]) == 1


def test_mc211_property_cn_sell_requires_a_valid_prior_purchase_date():
    market = get_market("cn")
    for value in (None, "invalid", "2026-09-30", "2026-10-01", True, 1):
        assert not market.check_order("sell", "600000.SS", 100, 100,
                                      "2026-09-30", buy_date=value).allowed
    assert market.check_order("sell", "600000.SS", 100, 100,
                               "2026-09-30", buy_date="2026-09-29").allowed


# MC-212: same-ticker dedup and risk caps remain true over arbitrary signals.
def test_mc212_sample_no_overlapping_same_ticker():
    panel, dates = _panel()
    result = run_backtest(_frames(dates, all_signals=True), panel, GateParams(time_stop=7, cost_bps=0))
    trades = result["trades"]
    assert trades and all(b.entry_i > a.exit_i for a, b in zip(trades, trades[1:]))


def test_mc212_property_slot_chain_and_gross_caps_hold_every_day():
    tickers = tuple(f"T{index}" for index in range(8))
    panel, dates = _panel(30, tickers)
    output = run_backtest(_frames(dates, tickers, stop=95, all_signals=True), panel,
                          GateParams(max_picks=5, time_stop=3, cost_bps=0))
    assert output["trades"]
    for day in range(len(dates)):
        active = [t for t in output["trades"] if t.entry_i <= day <= t.exit_i]
        assert len(active) <= 5 and len({t.ticker for t in active}) == len(active)
        assert sum(t.risk for t in active) <= 100_000 * config.MAX_CHAIN_RISK_PCT + 1e-6
        assert sum(t.weight for t in active) <= .9 + 1e-9


def test_mc212_property_paper_fills_respect_run_slot_limit(tmp_path):
    engine = SimEngine(str(tmp_path / "sim.json"))
    _pending(engine)
    seed = engine.state["pending"][0]
    engine.state["pending"] = [{**seed, "ticker": f"T{index}"} for index in range(5)]
    result = _result()
    result.raw["gate_params"] = asdict(GateParams(max_picks=2))
    engine.step("2026-09-29", result, lambda _: Bar(100, 101, 99, 100))
    assert len(engine.state["positions"]) == 2 and len(engine.state["pending"]) == 3


# MC-213: light risk discount before integer-share and position caps.
def test_mc213_sample_light_narrow_stop_matches_live_planner():
    panel, dates = _panel()
    frame = _frames(dates, stop=99.5, score=5.7)
    output = run_backtest(frame, panel, GateParams(time_stop=2, cost_bps=0))
    candidate = StockCandidate("AAPL", chain_id="semis", price=100, stop_price=99.5,
                               tss_final=8, c_liq=1, atr_pct=.02)
    context = {"mrs": _result(score=5.7).mrs, "sectors": [SectorScore("SMH", 8, breadth=70, in_main_pool=True)],
               "watchlist": [candidate]}
    picks = RiskManagerAgent(None).execute(context)
    assert output["trades"][0].weight == picks[0].position_pct == .2
    assert picks[0].risk_usd == 100 and context["pick_rationale"]["AAPL"]["risk_budget_usd"] == 280


def test_mc213_property_planner_replay_integer_risk_and_caps_agree():
    rng = random.Random(120213)
    for _ in range(24):
        price = rng.uniform(10, 250)
        stop = price * (1 - rng.uniform(.001, .2))
        score = rng.choice([5.7, 8])
        panel, dates = _panel(price=price)
        frames = _frames(dates, score=score)
        frames[0].candidates[0].entry_ref, frames[0].candidates[0].stop = price, stop
        result = run_backtest(frames, panel, GateParams(time_stop=2, cost_bps=0))
        candidate = StockCandidate("AAPL", chain_id="semis", price=price, stop_price=stop,
                                   tss_final=8, c_liq=1, atr_pct=.02)
        context = {"mrs": _result(score=score).mrs, "sectors": [SectorScore("SMH", 8, breadth=70, in_main_pool=True)],
                   "watchlist": [candidate]}
        pick = RiskManagerAgent(None).execute(context)[0]
        trade = result["trades"][0]
        assert trade.shares == pick.shares
        assert trade.weight == pytest.approx(pick.position_pct, abs=.00005)
        assert trade.risk == pytest.approx(pick.risk_usd, abs=.0001)
        assert trade.risk <= 100_000 * config.RISK_R_PCT * (1 if score == 8 else .35) + 1e-6
        assert trade.weight <= config.MAX_SINGLE_POSITION_PCT


# MC-214: terminal liquidation cost is present in unit returns and account NAV.
def test_mc214_sample_unclosed_mark_compounds_to_net_price_ratio():
    panel, _ = _panel(3)
    result = _simulate_trade(panel, 0, 0, 90, GateParams(time_stop=7, cost_bps=10))
    entry_i, exit_i, entry, exit_net, r, returns = result
    assert exit_i == 2 and np.prod([1 + returns[i] for i in sorted(returns)]) == pytest.approx(exit_net / cost_adj_buy(entry, 10))


@pytest.mark.parametrize("bps", [0, 1, 10, 50, 500])
def test_mc214_property_account_nav_contains_every_hold_day_and_both_costs(bps):
    panel, dates = _panel(5)
    frames = _frames(dates)
    frames = [frames[0], frames[-1]]  # no signal frames on interior holding dates
    output = run_backtest(frames, panel, GateParams(time_stop=7, cost_bps=bps))
    trade = output["trades"][0]
    assert output["port_dates"] == [str(day.date()) for day in dates]
    expected_nav = 1 + trade.shares * (trade.exit_price - cost_adj_buy(trade.entry, bps)) / 100_000
    assert np.prod([1 + r for r in output["port_returns"]]) == pytest.approx(expected_nav)
    assert output["port_total_return"] == pytest.approx(expected_nav - 1, abs=.00005)


# MC-215: third strong sector remains eligible for the sub pool in both paths.
def test_mc215_sample_third_strong_sector_can_enter_light_pool(monkeypatch):
    data = pd.DataFrame({"Close": [100] * 120})
    context = {"market_data": {"tnx": None, "spy": data, "universe_closes": {},
                                "sector_etfs": {etf: data for etf in ("S1", "S2", "S3")}}}
    agent = SectorAgent(None)
    monkeypatch.setattr(agent, "_score_sector", lambda etf, *args: SectorScore(etf, 8, breadth=70))
    sectors = agent.execute(context)
    assert next(s for s in sectors if s.etf == "S3").in_sub_pool
    frame = DayFrame(pd.Timestamp("2026-09-29"), 8, "neutral",
                     [SectorSnap(s.etf, s.shs, s.breadth) for s in sectors],
                     [CandidateSnap("AAPL", 8, 1, 1, "S3", "semis", False, "A", 100, 90)])
    assert _gate_day(frame, GateParams())[0][1] == "轻仓试错"


def test_mc215_property_sector_pool_membership_is_shared(monkeypatch):
    rng = random.Random(120215)
    for _ in range(20):
        scores = {f"S{index}": (rng.uniform(6, 10), rng.uniform(20, 100)) for index in range(6)}
        data = pd.DataFrame({"Close": [100] * 120})
        context = {"market_data": {"tnx": None, "spy": data, "universe_closes": {},
                                    "sector_etfs": {etf: data for etf in scores}}}
        agent = SectorAgent(None)
        monkeypatch.setattr(agent, "_score_sector", lambda etf, *args: SectorScore(etf, scores[etf][0], breadth=scores[etf][1]))
        sectors = agent.execute(context)
        for sector in sectors:
            frame = DayFrame(pd.Timestamp("2026-09-29"), 8, "neutral",
                             [SectorSnap(s.etf, s.shs, s.breadth) for s in sectors],
                             [CandidateSnap("AAPL", 8, 1, 1, sector.etf, "semis", False, "A", 100, 90)])
            assert bool(_gate_day(frame, GateParams())) == (sector.in_main_pool or sector.in_sub_pool)
        assert sum(s.in_main_pool for s in sectors) <= config.MAIN_POOL_MAX


# MC-216: proposal/execution identifiers and all artifact paths are confined.
def test_mc216_sample_proposal_traversal_never_writes(tmp_path):
    with pytest.raises(ValueError):
        proposal = Proposal("../escaped", datetime.now().isoformat(), {}, .97, .2, "pending_review")
        save_proposal(proposal, str(tmp_path / "proposals"))
    assert not (tmp_path / "escaped.json").exists()


@pytest.mark.parametrize("identifier", ["", "..", "../x", "..\\x", "/tmp/x", "a/b", "a:b", "a%2Fb", "a" * 97])
def test_mc216_property_invalid_ids_and_symlinks_cannot_escape(tmp_path, identifier):
    chief, proposal, path, digest = _proposal(tmp_path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id=identifier)
    assert path.read_bytes() == original and not (tmp_path / "tuned_params.json").exists()


def test_mc216_property_review_and_option_symlink_roots_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "review"
    root.mkdir()
    chief, proposal, path, digest = _proposal(root)
    (root / "review_executions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="escape")
    history = tmp_path / "history"
    history.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        OptionsHistoryStore(history).append("AAPL", {"pcr_oi": 1})
    assert list(outside.iterdir()) == []


# Authorized additions: the actual demo source remains offline and coherent.
def test_authorized_addition_demo_never_probes_real_sources(monkeypatch):
    from trading_system import pipeline
    def forbidden():
        raise AssertionError("Synthetic source probed a real channel")
    monkeypatch.setattr(pipeline, "_channel_chain", forbidden)
    assert set(pipeline._batch_with_fallback(DemoProvider(), ["SPY"], 5)) == {"SPY"}
    class Down(DemoProvider):
        def ohlcv(self, *args, **kwargs):
            raise RuntimeError("fixture failure")
        def ohlcv_batch(self, *args, **kwargs):
            raise RuntimeError("fixture failure")
    assert pipeline._batch_with_fallback(Down(), ["SPY"], 5) == {}
    with pytest.raises(RuntimeError, match="fixture failure"):
        pipeline._single_with_fallback(Down(), "ohlcv", "SPY", days=5)


def test_authorized_addition_demo_full_ohlcv_matches_every_tail():
    provider = DemoProvider()
    for ticker in ("SPY", "AAPL", "NVDA", "TSM", "MSFT"):
        long = provider.ohlcv(ticker, 420)
        assert (long.High >= long[["Open", "Close"]].max(axis=1)).all()
        assert (long.Low <= long[["Open", "Close"]].min(axis=1)).all()
        for days in (1, 5, 60, 245):
            pd.testing.assert_frame_equal(provider.ohlcv(ticker, days), long.tail(days))


@pytest.mark.parametrize("field,value", [
    ("cost_bps", -1), ("cost_bps", 10_000), ("cost_bps", True),
    ("profit_protect_r", 0), ("profit_protect_r", -2), ("profit_protect_r", True),
    ("time_stop_days", -1), ("time_stop_days", 253), ("time_stop_days", True),
])
def test_mc003_property_invalid_paper_parameter_snapshots_preserve_committed_bytes(tmp_path, field, value):
    target = tmp_path / "sim.json"
    engine = SimEngine(str(target))
    engine.save()
    committed = target.read_bytes()
    _pending(engine)
    engine.state["pending"][0][field] = value
    with pytest.raises(ValueError):
        engine.save()
    assert target.read_bytes() == committed
    # The same invalid snapshot must fail on load without resetting evidence.
    target.write_text(json.dumps(engine.state))
    corrupted = target.read_bytes()
    with pytest.raises(ValueError):
        SimEngine(str(target))
    assert target.read_bytes() == corrupted


@pytest.mark.parametrize("value", [[], True, {"cost_bps": -1}, {"profit_protect_r": 0}, {"time_stop": 0}])
def test_mc003_property_invalid_journal_parameter_snapshots_preserve_committed_bytes(tmp_path, value):
    target = tmp_path / "journal.json"
    journal = Journal(target)
    journal.log_picks(_result("AAPL"))
    committed = target.read_bytes()
    journal.records[0]["gate_params"] = value
    with pytest.raises((ValueError, TypeError)):
        journal.save()
    assert target.read_bytes() == committed
    target.write_text(json.dumps(journal.records))
    corrupted = target.read_bytes()
    with pytest.raises(ValueError):
        Journal(target)
    assert target.read_bytes() == corrupted


@pytest.mark.parametrize("dates,closes,run_day,expected", [
    (["2026-09-25", "2026-09-29"], [100, 100], "2026-09-29", 0),
    (["2026-09-30", "2026-10-07", "2026-10-08"], [100, 70, 100], "2026-10-08", 100),
    (["2026-09-28", "2026-09-29"], [100, 100], "2026-09-29", 100),
])
def test_mc211_property_main_previous_close_is_from_the_actual_previous_session(tmp_path, monkeypatch, dates, closes, run_day, expected):
    import main
    from trading_system import journal, pipeline, simulator
    frame = pd.DataFrame({"Open": closes, "High": np.array(closes) + 1,
                          "Low": np.array(closes) - 1, "Close": closes, "Volume": 1e6},
                         index=pd.DatetimeIndex(dates))
    result = _result(market="cn")
    result.trade_date = run_day
    monkeypatch.setattr(main, "run_pipeline", lambda **kwargs: result)
    monkeypatch.setattr(main, "to_json", lambda *args: str(tmp_path / "result.json"))
    monkeypatch.setattr(main, "to_markdown", lambda *args: str(tmp_path / "report.md"))
    monkeypatch.setattr(journal, "Journal", lambda *args: SimpleNamespace(
        log_picks=lambda *a: 0, settle=lambda *a, **kw: 0, stats=lambda: {"closed": 0}))
    monkeypatch.setattr(pipeline, "_single_with_fallback", lambda *a, **kw: frame)
    observed = {}
    def step(day, result, get_bar):
        observed["bar"] = get_bar("600000.SS")
        raise RuntimeError("bar checkpoint")
    monkeypatch.setattr(simulator, "SimEngine", lambda *a, **kw: SimpleNamespace(step=step))
    args = SimpleNamespace(out=str(tmp_path), demo=True, universe="core", universe_file=None,
                           top=3, picks=1, account=100_000, use_tuned=False, market="cn")
    with pytest.raises(RuntimeError, match="bar checkpoint"):
        main._daily(args, "demo")
    assert observed["bar"].date == run_day
    assert observed["bar"].prev_close == expected


def test_mc202_property_explicit_namespaces_keep_option_and_calibration_ledgers_separate(tmp_path, monkeypatch):
    from trading_system import pipeline
    class RecordedFixture(DemoProvider):
        synthetic = False  # deterministic fixture exercises the real write path
        name = "recorded-fixture"
    class Offline:
        def complete_json(self, **kwargs):
            raise LLMUnavailable("offline namespace fixture")
    monkeypatch.setattr(pipeline, "get_provider", lambda name=None: RecordedFixture())
    global_root = tmp_path / "global"
    (global_root / "cache" / "search").mkdir(parents=True)
    sentinel = global_root / "cache" / "search" / "sentinel.json"
    sentinel.write_text("global-cache-sentinel")
    global_journal = global_root / "journal.json"
    global_journal.write_text("[]")
    global_samples = global_root / "calibration_samples.json"
    global_samples.write_text("global-samples-sentinel")
    global_options = global_root / "options_hist"
    global_options.mkdir()
    marker = global_options / "AAPL.json"
    marker.write_text('[{"date":"2026-09-28","atm_iv":0.1}]')
    monkeypatch.setenv("TS_OPTIONS_HIST_DIR", str(global_options))
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(global_journal))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(global_samples))
    monkeypatch.chdir(global_root)
    before = {path: path.read_bytes() for path in (sentinel, global_journal, global_samples, marker)}
    outputs = []
    for name, native_r in (("a", 2), ("b", -1)):
        ledger = tmp_path / name / "artifacts"
        runtime = tmp_path / name / "runtime"
        ledger.mkdir(parents=True)
        write_json_atomic(ledger / "journal.json", [{"date": "2026-09-28", "ticker": "AAPL",
            "status": "closed", "tss_final": 8, "mrs_star": 8, "r": native_r}])
        result = pipeline.run_pipeline(provider_name="demo", universe_mode="core", top_n=2,
                                       max_picks=1, llm_client=Offline(),
                                       run_state_dir=str(runtime), ledger_dir=str(ledger))
        samples = read_json_strict(ledger / "calibration_samples.json")
        assert len(samples) == 1 and samples[0]["r"] == native_r
        assert result.raw["calibration"]["n"] == 1
        histories = sorted((ledger / "options_hist").glob("*.json"))
        assert histories and all(len(read_json_strict(path)) == 1 for path in histories)
        outputs.append(histories)
    assert outputs[0][0].parent != outputs[1][0].parent
    assert all(path.read_bytes() == data for path, data in before.items())
    assert list((global_root / "cache" / "search").iterdir()) == [sentinel]
    assert list(global_options.iterdir()) == [marker]


@pytest.mark.parametrize("market,count,filename", [("us", 2000, "universe_full.json"),
    ("cn", 4000, "universe_cn.json"), ("hk", 1500, "universe_hk.json")])
def test_mc202_property_universe_cache_uses_selected_runtime_directory(tmp_path, monkeypatch, market, count, filename):
    from trading_system import universe
    global_cache, current_cache = tmp_path / "global-cache", tmp_path / "current-cache"
    global_cache.mkdir()
    sentinel = global_cache / filename
    sentinel.write_text("global-universe-sentinel")
    monkeypatch.setattr(universe, "_FULL_CACHE", global_cache / "universe_full.json")
    monkeypatch.setattr(universe, "_CN_CACHE", global_cache / "universe_cn.json")
    monkeypatch.setattr(universe, "_HK_CACHE", global_cache / "universe_hk.json")
    values = [f"T{index}" for index in range(count)]
    if market == "us":
        monkeypatch.setattr(universe, "fetch_full_universe", lambda *a: values)
        observed = universe.load_full_universe(cache_dir=current_cache)[0]
    else:
        monkeypatch.setattr(universe, f"fetch_{market}_universe", lambda: values)
        observed = universe.load_market_universe(market, "full", cache_dir=current_cache)[0]
    assert observed == values
    assert read_json_strict(current_cache / filename)["tickers"] == values
    assert sentinel.read_text() == "global-universe-sentinel"


def test_mc202_property_backtest_frame_cache_uses_selected_runtime_directory(tmp_path, monkeypatch):
    from trading_system import backtest
    global_cache, current_cache = tmp_path / "global-cache", tmp_path / "current-cache"
    global_cache.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", str(global_cache))
    sentinel = backtest._frame_cache_path("demo", ["AAPL"], 10, 3, 1)
    sentinel.write_bytes(b"untrusted-global-frame-sentinel")
    frames, panel, raw = backtest.collect_day_frames(DemoProvider(), ["AAPL"], days=10,
        signal_days=3, top_n=1, cache_dir=current_cache)
    assert frames == [] and len(panel.dates) == 10 and raw["spy"] is not None
    assert len(list(current_cache.glob("frames_*.pkl"))) == 1
    assert sentinel.read_bytes() == b"untrusted-global-frame-sentinel"


@pytest.mark.parametrize("change", ["active_path", "rejection_reason"])
def test_mc209_property_completed_retry_keeps_original_execution_parameters(tmp_path, change):
    chief, proposal, path, digest = _proposal(tmp_path)
    if change == "active_path":
        done = chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="bound")
        retry = lambda: chief.approve(proposal.proposal_id, expected_sha256=digest,
                                     execution_id="bound", tuned_path=str(tmp_path / "alternate.json"))
        healthy = lambda: chief.approve(proposal.proposal_id, expected_sha256=digest, execution_id="bound")
    else:
        done = chief.reject(proposal.proposal_id, "original reason", expected_sha256=digest, execution_id="bound")
        retry = lambda: chief.reject(proposal.proposal_id, "changed reason",
                                    expected_sha256=digest, execution_id="bound")
        healthy = lambda: chief.reject(proposal.proposal_id, "original reason",
                                      expected_sha256=digest, execution_id="bound")
    committed = {file: file.read_bytes() for file in tmp_path.rglob("*.json")}
    with pytest.raises(ValueError, match="retry parameters"):
        retry()
    repeated = healthy()
    assert repeated.execution_id == done.execution_id
    assert all(file.read_bytes() == raw for file, raw in committed.items())
    assert not (tmp_path / "alternate.json").exists()


def test_mc202_property_default_unit_pipeline_keeps_calibration_in_fixture_ledger(tmp_path, monkeypatch):
    from trading_system import pipeline
    class Offline:
        def complete_json(self, **kwargs):
            raise LLMUnavailable("unit accounting isolation fixture")
    monkeypatch.chdir(tmp_path)
    result = pipeline.run_pipeline(provider_name="demo", universe_mode="core",
                                   top_n=1, max_picks=1, llm_client=Offline())
    ledger_root = Path(os.environ["TS_OPTIONS_HIST_DIR"])
    samples = Path(result.raw["calibration"]["samples_path"])
    assert samples.parent == ledger_root
    assert samples.exists() and read_json_strict(samples) == []
    assert Path(result.raw["calibration"]["journal_path"]).parent == ledger_root
    assert not (tmp_path / "reports").exists()
