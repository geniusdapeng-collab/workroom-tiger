"""MC-001: accounting uses observed trading dates, never the run's wall clock."""

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system.data_models import MRSResult, PipelineResult
from trading_system.journal import Journal
from trading_system.simulator import Bar, SimEngine


def _pick(ticker="AAA"):
    return SimpleNamespace(
        ticker=ticker, tss_final=8.0, entry_template="A", stop_price=90.0,
        shares=10, risk_usd=100.0, chain="", sector="", time_stop_days=7,
    )


def _result(*, picks=(), action="BUY", allow=True, cap=1.0):
    return SimpleNamespace(
        action=action, picks=list(picks), provider="demo",
        mrs=SimpleNamespace(position_cap=(0.0, cap), allow_new_positions=allow),
    )


def _bar(day, *, open=100.0, high=102.0, low=98.0, close=101.0):
    # Assign separately so the pre-fix failure demonstrates wrong accounting,
    # rather than failing at the old dataclass's constructor signature.
    bar = Bar(open=open, high=high, low=low, close=close)
    bar.date = day
    return bar


def _engine(tmp_path):
    return SimEngine(str(tmp_path / "sim.json"))


def _queue(engine, signal_date="2026-10-19"):
    engine.step(signal_date, _result(picks=[_pick()]), lambda _: None)


def _position():
    return {
        "ticker": "HELD", "shares": 10, "entry_price": 100.0, "stop": 90.0,
        "entry_date": "2026-10-16", "chain": "", "sector": "",
        "risk_usd": 100.0, "time_stop_days": 7, "peak_price": 100.0, "note": "",
    }


@pytest.mark.parametrize("bar_date", ["2026-10-18", "2026-10-19"])
def test_pending_never_fills_on_or_before_signal_date(tmp_path, bar_date):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.step("2026-10-20", _result(), lambda _: _bar(bar_date))
    assert engine.state["positions"] == []
    assert len(engine.state["pending"]) == 1
    assert engine.state["cash"] == 100_000.0


@pytest.mark.parametrize("bar_date", [None, "", "not-a-date", "2026-10-21"])
def test_unknown_invalid_or_future_bar_cannot_execute(tmp_path, bar_date):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.state["positions"].append(_position())
    engine.step("2026-10-20", _result(), lambda _: _bar(bar_date, low=85.0))
    assert [p["ticker"] for p in engine.state["positions"]] == ["HELD"]
    assert len(engine.state["pending"]) == 1
    assert engine.state["closed"] == []
    assert engine.state["cash"] == 100_000.0


def test_signal_day_gap_does_not_void_tomorrows_pending(tmp_path):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.step("2026-10-20", _result(),
                lambda _: _bar("2026-10-19", open=85.0, low=84.0))
    assert len(engine.state["pending"]) == 1
    assert engine.state["closed"] == []


def test_delayed_run_records_actual_entry_and_exit_dates(tmp_path):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.step("2026-10-22", _result(), lambda _: _bar("2026-10-20"))
    assert engine.state["positions"][0]["entry_date"] == "2026-10-20"
    assert engine.state["positions"][0]["entry_price"] == 100.1
    engine.step("2026-10-23", _result(),
                lambda _: _bar("2026-10-21", open=95.0, low=89.0, close=94.0))
    closed = engine.state["closed"]
    assert len(closed) == 1
    assert closed[0]["entry_date"] == "2026-10-20"
    assert closed[0]["exit_date"] == "2026-10-21"
    assert closed[0]["exit"] == 89.91


@pytest.mark.parametrize("repeat_run_date", ["2026-10-20", "2026-10-21"])
def test_repeated_bar_cannot_hit_a_stop_raised_after_its_low(tmp_path, repeat_run_date):
    engine = _engine(tmp_path)
    _queue(engine)
    protection_bar = _bar("2026-10-20", high=122.0, low=95.0, close=121.0)
    engine.step("2026-10-20", _result(), lambda _: protection_bar)
    position = deepcopy(engine.state["positions"][0])
    cash = engine.state["cash"]
    assert position["stop"] == position["entry_price"]

    # A process restart must not make an already-accounted low executable again.
    engine = _engine(tmp_path)
    engine.step(repeat_run_date, _result(), lambda _: protection_bar)
    assert engine.state["closed"] == []
    assert engine.state["positions"] == [position]
    assert engine.state["cash"] == cash

    engine.step("2026-10-22", _result(),
                lambda _: _bar("2026-10-21", open=101.0, low=99.0))
    assert len(engine.state["closed"]) == 1
    assert engine.state["closed"][0]["exit_date"] == "2026-10-21"


def test_out_of_order_bar_cannot_exit_existing_position(tmp_path):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.step("2026-10-20", _result(), lambda _: _bar("2026-10-20"))
    position = deepcopy(engine.state["positions"][0])
    engine.step("2026-10-21", _result(),
                lambda _: _bar("2026-10-19", open=85.0, low=84.0))
    assert engine.state["closed"] == []
    assert engine.state["positions"] == [position]


def test_legacy_position_keeps_current_mark_without_replaying_its_bar(tmp_path):
    engine = _engine(tmp_path)
    position = _position()
    position.update(stop=100.0, peak_price=122.0)
    engine.state["positions"] = [position]
    engine.state["cash"] = 99_000.0
    engine.state["equity_curve"] = [{"date": "2026-10-19", "equity": 100_210.0}]
    # A legacy row may already have raised the stop after this day's low. It
    # still needs its observed closing mark when no mark was saved in the row.
    snapshot = engine.step("2026-10-20", _result(),
                           lambda _: _bar("2026-10-19", high=122.0, low=95.0, close=121.0))
    assert engine.state["closed"] == []
    assert snapshot["marks"]["HELD"] == 121.0
    assert snapshot["equity"] == 100_210.0


@pytest.mark.parametrize("first_quote_available", [True, False])
def test_legacy_migration_does_not_move_its_watermark_with_each_run(tmp_path, first_quote_available):
    engine = _engine(tmp_path)
    engine.state["positions"] = [_position()]
    engine.state["equity_curve"] = [{"date": "2026-10-19", "equity": 100_000.0}]
    engine.step("2026-10-20", _result(),
                lambda _: _bar("2026-10-19") if first_quote_available else None)
    assert engine.state["closed"] == []
    engine = _engine(tmp_path)
    # The next observed day must be managed even when it arrives the following
    # morning and yesterday's wall-clock accounting row has the same date.
    engine.step("2026-10-21", _result(),
                lambda _: _bar("2026-10-20", open=95.0, low=89.0, close=94.0))
    assert len(engine.state["closed"]) == 1
    assert engine.state["closed"][0]["exit_date"] == "2026-10-20"


def test_repeated_stale_bars_do_not_advance_time_stop(tmp_path):
    engine = _engine(tmp_path)
    _queue(engine)
    first_bar = _bar("2026-10-20", close=100.5)
    engine.step("2026-10-20", _result(), lambda _: first_bar)
    for run_day in range(21, 30):
        engine.step(f"2026-10-{run_day}", _result(), lambda _: first_bar)
    assert engine.state["closed"] == []
    engine.step("2026-10-30", _result(),
                lambda _: _bar("2026-10-21", close=100.5))
    assert len(engine.state["positions"]) == 1
    assert engine.state["closed"] == []


def test_seven_distinct_bars_keep_existing_time_stop_threshold(tmp_path):
    engine = _engine(tmp_path)
    _queue(engine)
    days = pd.bdate_range("2026-10-20", periods=7)
    for index, timestamp in enumerate(days):
        day = str(timestamp.date())
        engine.step(day, _result(), lambda _: _bar(day, close=100.5))
        assert len(engine.state["closed"]) == (1 if index == 6 else 0)
    assert engine.state["closed"][0]["days"] == 7
    assert engine.state["closed"][0]["reason"].startswith("时间止损")


@pytest.mark.parametrize("action,allow", [("AVOID", True), ("BUY", False)])
def test_new_position_gate_also_blocks_existing_pending(tmp_path, action, allow):
    engine = _engine(tmp_path)
    _queue(engine)
    # This fill is far below even the AVOID regime's existing-exposure cap.
    engine.step("2026-10-20", _result(action=action, allow=allow, cap=0.1),
                lambda _: _bar("2026-10-20"))
    assert engine.state["positions"] == []
    assert len(engine.state["pending"]) == 1
    assert engine.state["cash"] == 100_000.0


@pytest.mark.parametrize("action,allow", [("AVOID", True), ("BUY", False)])
def test_blocked_opening_still_manages_existing_positions(tmp_path, action, allow):
    engine = _engine(tmp_path)
    _queue(engine)
    engine.state["positions"].append(_position())
    engine.step("2026-10-20", _result(action=action, allow=allow),
                lambda ticker: _bar("2026-10-20", low=89.0 if ticker == "HELD" else 98.0))
    assert [p["ticker"] for p in engine.state["closed"]] == ["HELD"]
    assert engine.state["positions"] == []
    assert len(engine.state["pending"]) == 1


def test_mrs_no_new_position_flag_also_blocks_signal_registration(tmp_path):
    engine = _engine(tmp_path)
    engine.step("2026-10-19", _result(picks=[_pick()], allow=False), lambda _: None)
    assert engine.state["pending"] == []


def _record(day="2026-10-19"):
    return {
        "date": day, "ticker": "AAA", "status": "open", "stop": 90.0,
        "entry_ref": 100.0, "entry": None, "entry_date": None,
    }


def _frame(days, opens=None):
    prices = opens if opens is not None else [100.0] * len(days)
    return pd.DataFrame({
        "Open": prices, "High": [p + 2.0 for p in prices],
        "Low": [p - 2.0 for p in prices], "Close": [p + 1.0 for p in prices],
        "Volume": [100_000] * len(days),
    }, index=pd.to_datetime(days))


@pytest.mark.parametrize("days", [[], ["2026-10-16"], ["2026-10-19"]])
def test_journal_waits_until_a_later_bar_exists(days):
    rec = _record()
    before = deepcopy(rec)
    Journal._settle_one(rec, _frame(days))
    assert rec == before


@pytest.mark.parametrize("days", [
    ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"],
    ["2026-10-21", "2026-10-16", "2026-10-20", "2026-10-19"],
])
def test_journal_chooses_first_actual_date_after_signal(days):
    rec = _record()
    prices = [105.0 if d == "2026-10-20" else 100.0 for d in days]
    Journal._settle_one(rec, _frame(days, prices))
    assert rec["entry_date"] == "2026-10-20"
    assert rec["entry"] == 105.0


def test_journal_missing_signal_row_uses_covered_history_and_entry_reference():
    # Prior history proves coverage across the signal date. Its first close is
    # unrelated to the signal's price and must not cause a false corporate action.
    rec = _record()
    Journal._settle_one(rec, _frame(
        ["2026-10-16", "2026-10-20", "2026-10-21"], [50.0, 100.0, 101.0]))
    assert rec["status"] == "open"
    assert rec["entry_date"] == "2026-10-20"
    assert rec["entry"] == 100.0


def test_journal_does_not_invent_entry_from_truncated_history():
    rec = _record()
    before = deepcopy(rec)
    Journal._settle_one(rec, _frame(["2026-10-27", "2026-10-28"]))
    assert rec == before


def test_journal_preserves_original_entry_when_history_window_moves():
    rec = _record()
    Journal._settle_one(rec, _frame(["2026-10-19", "2026-10-20"]))
    before = deepcopy(rec)
    Journal._settle_one(rec, _frame(["2026-10-27", "2026-10-28"]))
    assert rec == before


def test_journal_settle_keeps_pre_signal_coverage_and_persists_open_entry(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    journal.records = [_record("2026-07-20")]
    journal.save()
    provider = SimpleNamespace(ohlcv=lambda *_args, **_kwargs: _frame(
        ["2026-07-17", "2026-07-21", "2026-07-22"], [50.0, 100.0, 101.0]))
    assert journal.settle(provider) == 0
    reloaded = Journal(journal.path)
    assert reloaded.records[0]["entry_date"] == "2026-07-21"
    assert reloaded.records[0]["entry"] == 100.0


def test_journal_discloses_history_that_no_longer_covers_signal(tmp_path):
    journal = Journal(tmp_path / "journal.json")
    journal.records = [_record("2026-07-20")]
    provider = SimpleNamespace(ohlcv=lambda *_args, **_kwargs: _frame(
        ["2026-08-03", "2026-08-04"]))
    assert journal.settle(provider) == 0
    assert journal.records[0]["entry_date"] is None
    assert journal.last_failed == ["AAA"]


def test_journal_can_record_the_next_day_without_waiting_two_calendar_days(tmp_path, monkeypatch):
    class NextDay(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 7, 21)

    monkeypatch.setattr("trading_system.journal.datetime", NextDay)
    journal = Journal(tmp_path / "journal.json")
    journal.records = [_record("2026-07-20")]
    provider = SimpleNamespace(ohlcv=lambda *_args, **_kwargs: _frame(
        ["2026-07-20", "2026-07-21"]))
    journal.settle(provider)
    assert journal.records[0]["entry_date"] == "2026-07-21"


def test_journal_ignores_future_rows_at_settlement(tmp_path, monkeypatch):
    class Today(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 7, 22)

    monkeypatch.setattr("trading_system.journal.datetime", Today)
    journal = Journal(tmp_path / "journal.json")
    journal.records = [_record("2026-07-20")]
    frame = _frame(["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"])
    frame.loc[pd.Timestamp("2026-07-23"), "Low"] = 85.0
    provider = SimpleNamespace(ohlcv=lambda *_args, **_kwargs: frame)
    assert journal.settle(provider) == 0
    assert journal.records[0]["status"] == "open"
    assert journal.records[0]["entry_date"] == "2026-07-21"


def _run_daily(tmp_path, monkeypatch, *, account=100_000.0, observe_bar=False):
    import main
    from trading_system import pipeline, providers, state
    from trading_system.review.chief import ReviewChief

    result = PipelineResult(
        trade_date="2026-10-20", provider="demo", action="BUY",
        mrs=MRSResult(7.0, 1.0, 1.0, 7.0, position_cap=(0.0, 1.0)),
    )
    monkeypatch.setattr(main, "run_pipeline", lambda **_: result)
    monkeypatch.setattr(providers, "get_provider", lambda _: SimpleNamespace(
        name="demo", synthetic=True))
    monkeypatch.setattr(state, "purge_run_state", lambda: {})
    monkeypatch.setattr(pipeline, "_single_with_fallback", lambda *_args, **_kwargs:
                        _frame(["2026-10-19"]))
    monkeypatch.setattr(ReviewChief, "daily", lambda *_args: {
        "memo_path": None, "ms": 0, "attribution": {"total_violations": 0}})
    observed = []
    original_step = SimEngine.step

    def capture_step(self, day, today_result, get_bar, **kwargs):
        if observe_bar:
            observed.append(get_bar("AAA"))
        return original_step(self, day, today_result, get_bar, **kwargs)

    monkeypatch.setattr(SimEngine, "step", capture_step)
    args = SimpleNamespace(
        out=str(tmp_path), universe="core", universe_file=None, top=1, picks=1,
        account=account, use_tuned=False, market="us", demo=True,
        mode="daily", html=False, quiet=True,
    )
    main._daily(args, "demo")
    return SimEngine(str(tmp_path / "sim_portfolio.json")), observed


def test_daily_adapter_retains_actual_ohlcv_date(tmp_path, monkeypatch):
    _, observed = _run_daily(tmp_path, monkeypatch, observe_bar=True)
    assert len(observed) == 1 and observed[0] is not None
    assert getattr(observed[0], "date", None) == "2026-10-19"


def test_daily_new_simulator_uses_requested_account_size(tmp_path, monkeypatch):
    engine, _ = _run_daily(tmp_path, monkeypatch, account=25_000.0)
    assert engine.state["initial_cash"] == 25_000.0
    assert engine.state["cash"] == 25_000.0


def test_restart_keeps_saved_initial_cash_for_performance_stats(tmp_path):
    path = str(tmp_path / "sim.json")
    engine = SimEngine(path, initial_cash=25_000.0)
    engine.step("2026-10-19", _result(), lambda _: None)
    reloaded = SimEngine(path)
    assert reloaded.initial_cash == 25_000.0
    assert reloaded.stats()["equity"] == 25_000.0
    assert reloaded.stats()["cum_return"] == 0.0
    assert reloaded.stats()["max_drawdown"] == 0.0
