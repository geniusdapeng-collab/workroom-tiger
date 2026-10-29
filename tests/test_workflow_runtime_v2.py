"""Actual orchestration over isolated synthetic ledgers; no network/model calls."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from trading_system.data_models import PipelineResult, StockCandidate
from trading_system.workflow_runtime_v2 import opportunity_stage, evaluation_stage


NOW = "2026-10-27T14:01:00+00:00"


class Execution:
    run_id = "run-test-only-v2"
    run_date = date(2026, 10, 27)

    def __init__(self, root):
        self.paths = SimpleNamespace(reports_dir=root, work_dir=root)
        self.calls = []

    def call_io(self, method, inputs, operation):
        self.calls.append((method, deepcopy(inputs)))
        return operation()

    def summary(self):
        return [{"stage": stage, "status": "completed", "output_ref": {"sha256": "1" * 64}}
                for stage in ("layer4.risk", "decision.debate")]


def result():
    return PipelineResult("2026-10-27", "SYNTHETIC_TEST", action="WAIT",
        watchlist=[StockCandidate("AAA", price=100., key_level=100., stop_price=95.)],
        raw={"execution_policy": "whitepaper-execution-v2", "execution_decisions": [],
             "rejections": {"candidates": [{"ticker": "AAA", "reasons": ["numeric_gates_failed"]}]},
             "portfolio_marks": {}, "portfolio_snapshot": {"known_at": "2026-10-26"},
             "execution_setups": {}, "numeric_approved_count": 0})


def state():
    return {"cash": 100000., "initial_cash": 100000., "account_currency": "USD",
        "positions": [], "pending": [], "closed": [], "entry_plans": {},
        "equity_curve": [{"date": "2026-10-27", "valuation_date": "2026-10-26", "equity": 100000.}]}


def test_watch_zero_picks_still_fetches_quotes_and_records_fresh_requalification_request(tmp_path, monkeypatch):
    import trading_system.workflow_runtime_v2 as runtime
    monkeypatch.setattr(runtime, "utc_now", lambda: NOW)
    requested = []
    def quote(symbol):
        requested.append(symbol)
        return {"price": 100., "ts": "2026-10-27T14:00:59+00:00", "kind": "realtime"}
    execution, current = Execution(tmp_path), result()
    value = opportunity_stage(execution, current, state(), quote)
    assert requested == ["AAA"]
    assert [alert["kind"] for alert in value["alerts"]] == ["REQUALIFY"]
    assert value["lifecycle"]["intents"] == []
    assert value["requalifications"][0]["accepted"] is False
    assert current.raw["opportunity_lifecycle"]["opportunities"][0]["state"] == "WATCH"
    assert (tmp_path / "opportunity_ledger.json").exists()
    assert (tmp_path / "opportunity-lifecycle.json").exists()


def test_eod_quote_does_not_create_executable_intent_and_zero_trades_does_not_claim_profit(tmp_path, monkeypatch):
    import trading_system.workflow_runtime_v2 as runtime
    monkeypatch.setattr(runtime, "utc_now", lambda: NOW)
    execution, current = Execution(tmp_path), result()
    opportunity_stage(execution, current, state(), lambda _: {
        "price": 100., "ts": "2026-10-26T20:00:00+00:00", "kind": "eod_close"})
    evaluated = evaluation_stage(execution, current, state(), lambda _: pytest.fail("Current immature cohort needs no historical fetch"))
    assert evaluated["evaluation"]["account"]["n_closed"] == 0
    assert evaluated["evaluation"]["account"]["profitability_established"] is False
    assert evaluated["evaluation"]["opportunities"]["capture_rate"] is None
    assert evaluated["evaluation"]["account"]["win_rate"] is None
    assert current.raw["opportunity_lifecycle"]["intents"] == []
    assert (tmp_path / "business-evaluation.json").exists()


def test_bounded_quote_stage_uses_current_research_scope_and_per_quote_observation(tmp_path, monkeypatch):
    import trading_system.workflow_runtime_v2 as runtime
    monkeypatch.setattr(runtime, "utc_now", lambda: NOW)
    execution, current = Execution(tmp_path), result()
    current.watchlist.append(StockCandidate("BBB", price=100., key_level=100., stop_price=95.))
    current.raw["execution_decisions"].append({"symbol": "BBB", "action": "watch"})
    current.raw["rejections"]["candidates"].append({"ticker": "BBB", "reasons": ["numeric_gates_failed"]})
    current.raw["candidate_research"] = {
        "coverage": {"deferred": [{"symbol": "AAA", "reason": "candidate_limit"}]},
    }
    requested = []
    def quotes(symbols):
        requested.append(list(symbols))
        return {
            "quotes": {"BBB": {"price": 100., "ts": "2026-10-27T14:00:59+00:00",
                               "observed_at": "2026-10-27T14:00:59+00:00",
                               "kind": "realtime", "source": "fixture"}},
            "attempts": {"BBB": [{"provider": "fixture", "ok": True}]},
            "workers": 1,
        }
    value = opportunity_stage(execution, current, state(),
        lambda _: pytest.fail("Per-symbol quote path must not run"),
        get_quotes=quotes)
    assert requested == [["BBB"]]
    assert current.raw["lifecycle_observation"]["scope"] == ["BBB"]
    assert current.raw["lifecycle_observation"]["quote_workers"] == 1
    assert current.raw["lifecycle_observation"]["quote_attempts"]["BBB"][0]["provider"] == "fixture"
    assert [alert["ticker"] for alert in value["alerts"]] == ["BBB"]


def test_corrupt_ledger_fails_before_quotes_or_overwrite(tmp_path, monkeypatch):
    import trading_system.workflow_runtime_v2 as runtime
    monkeypatch.setattr(runtime, "utc_now", lambda: NOW)
    target = tmp_path / "opportunity_ledger.json"
    target.write_text('{"schema":"broken"}')
    with pytest.raises(ValueError):
        opportunity_stage(Execution(tmp_path), result(), state(), lambda _: pytest.fail("No quote before ledger validation"))
    assert target.read_text() == '{"schema":"broken"}'


def test_v2_paper_observation_scope_and_complete_bar_projection_are_shared():
    import pandas as pd
    from trading_system.simulator import bar_from_frame, paper_symbols
    current = result()
    current.picks = [SimpleNamespace(ticker="PICK")]
    current.raw["execution_setups"] = {"SETUP": {"entry_price": 100.0}}
    account = {
        "positions": [{"ticker": "HELD", "sector": "XLK"}],
        "pending": [{"ticker": "PENDING"}],
        "entry_plans": {
            "p1": {"ticker": "PLAN", "status": "partial"},
            "p2": {"ticker": "DONE", "status": "filled"},
        },
    }
    assert paper_symbols(account, current) == [
        "HELD", "PENDING", "PICK", "PLAN", "SETUP", "XLK"]

    frame = pd.DataFrame(
        {"Open": [100., 101.], "High": [101., 102.], "Low": [99., 100.],
         "Close": [100., 101.], "Volume": [1000., 1000.]},
        index=pd.to_datetime(["2026-10-26", "2026-10-27"]),
    )
    bar = bar_from_frame(frame, date(2026, 10, 26))
    assert bar.date == "2026-10-26" and bar.close == 100.0
