"""Whitepaper execution-v2 failure assertions before the production repair.

All prices and accounts here are synthetic in-memory fixtures.  No provider,
model, broker, network or persistent user account is used.  These assert the
business contract, including the original drift and risk/rounding boundaries.
"""

from copy import deepcopy
from importlib import import_module

import pandas as pd
import pytest

from trading_system.backtest import CandidateSnap, DayFrame, GateParams, SectorSnap, _gate_day
from trading_system.exit_engine import evaluate_day, simulate_trade
from trading_system.data_models import MRSResult, PipelineResult, TradePick
from trading_system.journal import Journal
from trading_system.simulator import Bar, SimEngine


POLICY = "whitepaper-execution-v2"
DAY = "2026-10-26"


def _policy():
    # The implementation is intentionally absent in the committed red baseline.
    # Import inside each test keeps existing drift assertions independently run.
    return import_module("trading_system.execution_policy")


def _state(cash=100_000.0, *, positions=(), pending=(), plans=None):
    return {
        "started": DAY,
        "initial_cash": 100_000.0,
        "account_currency": "USD",
        "cash": cash,
        "positions": list(positions),
        "pending": list(pending),
        "entry_plans": plans or {},
        "closed": [],
        "equity_curve": [],
        "ops_log": [],
    }


def _setup(symbol="TEST", **changes):
    row = {
        "schema": "tiger.entry-setup.v2",
        "symbol": symbol,
        "known_at": DAY,
        "entry_template": "A",
        "entry_price": 100.0,
        "stop_price": 95.0,
        "key_level": 100.0,
        "mrs_star": 8.0,
        "shs": 8.0,
        "tss_final": 8.0,
        "in_main": True,
        "in_sub": False,
        "chain_hot": True,
        "breadth": 80.0,
        "allow_new_positions": True,
        "shock": False,
        "risk_group": "ai_compute",
        "sector": "XLK",
        "size_ratio": 1.0,
        "signal_adv_usd": 200_000_000.0,
        "execution_mode": "paper",
    }
    return row | changes


def _budget(snapshot):
    return {
        "schema": "tiger.entry-budget.v2",
        "execution_policy": POLICY,
        "known_at": DAY,
        "snapshot_hash": snapshot["state_hash"],
        "gross_cap": 0.25,
        "single_cap": 0.20,
        "risk_pct": 0.008,
        "group_risk_pct": 0.03,
        "portfolio_risk_pct": 0.03,
        "max_positions": 10,
    }


def _day(*args, **kwargs):
    return evaluate_day(*args, policy_id=POLICY, initial_stop=95.0,
                        protected=False, **kwargs)


def test_daily_high_alone_triggers_next_bar_half_r_protection():
    """Entry100/stop95/high111/close105: preserve profit after HIGH >=2R."""
    action = _day(100.0, 111.0, 99.0, 105.0, 100.0, 95.0, 1)
    assert action.action == "protect"
    assert action.next_stop == pytest.approx(102.5)


def test_stop_precedes_same_bar_high_protection():
    """Unknown intrabar path must not manufacture the favorable high-first exit."""
    action = _day(100.0, 111.0, 94.0, 105.0, 100.0, 95.0, 1)
    assert action.action == "exit"
    assert action.exit_price == 95.0


def test_day_seven_closes_a_profitable_position():
    action = _day(102.0, 103.0, 101.0, 102.0, 100.0, 95.0, 7)
    assert action.action == "exit"
    assert action.exit_price == 102.0


def test_day_five_exits_without_net_price_progress():
    action = _day(100.0, 101.0, 99.0, 100.0, 100.0, 95.0, 5)
    assert action.action == "exit"
    assert action.exit_price == 100.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_invalid_bar_cannot_be_silently_reported_as_hold(bad):
    with pytest.raises(ValueError):
        _day(100.0, 101.0, 99.0, bad, 100.0, 95.0, 2)


def test_v2_backtest_rejects_a_missing_entry_template():
    """The formerly accepted TOS5.12 empty-template case must stop passing."""
    frame = DayFrame(
        date=pd.Timestamp(DAY),
        mrs_star=8.0,
        regime="黄金共振",
        sectors=[SectorSnap("XLK", 8.0, 80.0)],
        candidates=[CandidateSnap("TEST", 8.0, 1.0, 1.0, "XLK", "ai_compute", True, "", 100.0, 95.0)],
    )
    assert _gate_day(frame, GateParams(execution_policy=POLICY)) == []


@pytest.mark.parametrize("cash, expected", [(50_000.0, 80), (100_000.0, 160), (200_000.0, 320)])
def test_signal_quantity_uses_current_nav_instead_of_fixed_reference(cash, expected):
    policy = _policy()
    snapshot = policy.portfolio_snapshot(_state(cash), {}, DAY)
    allocation = policy.allocate_entry(snapshot, _setup(), _budget(snapshot))
    assert snapshot["nav"] == cash
    assert allocation["shares"] == expected
    assert allocation["risk_usd"] <= cash * 0.008


@pytest.mark.parametrize("mark", [None, {"date": "2026-10-23", "price": 100.0}, {"date": "2026-10-27", "price": 100.0}])
def test_missing_stale_or_future_holdings_valuation_fails_without_mutation(mark):
    policy = _policy()
    state = _state(90_000.0, positions=[{
        "ticker": "HELD", "shares": 100, "entry_price": 95.0,
        "initial_stop": 90.0, "stop": 90.0, "risk_group": "energy",
        "entry_date": "2026-10-23",
    }])
    before = deepcopy(state)
    with pytest.raises(policy.ExecutionPolicyError):
        policy.portfolio_snapshot(state, {} if mark is None else {"HELD": mark}, DAY)
    assert state == before


def test_allocation_rechecks_hard_risk_and_never_accepts_real_broker_mode():
    policy = _policy()
    snapshot = policy.portfolio_snapshot(_state(), {}, DAY)
    budget = _budget(snapshot)
    for setup in [_setup(entry_template=""), _setup(shock=True), _setup(execution_mode="live")]:
        denied = policy.allocate_entry(snapshot, setup, budget)
        assert denied["shares"] == 0 and denied["reason"]


@pytest.mark.parametrize("shares", [1, 2, 3, 7, 100])
def test_three_tranches_conserve_integer_shares_without_fake_small_lots(shares):
    policy = _policy()
    snapshot = policy.portfolio_snapshot(_state(), {}, DAY)
    pick = {
        "ticker": "TEST", "entry_template": "A", "entry_price": 100.0,
        "stop_price": 95.0, "shares": shares, "risk_usd": shares * 5.0,
        "chain": "ai_compute", "sector": "XLK", "time_stop_days": 7,
    }
    plan = policy.create_tranche_plan(pick, _budget(snapshot), DAY, setup=_setup())
    quantities = [row["shares"] for row in plan["tranches"]]
    assert len(quantities) == 3
    assert all(type(n) is int and n >= 0 for n in quantities)
    assert sum(quantities) == shares
    assert quantities[0] > 0
    if shares == 100:
        assert quantities == [40, 40, 20]


def test_full_unfilled_tranche_plan_reserves_budget_once():
    policy = _policy()
    empty = policy.portfolio_snapshot(_state(), {}, DAY)
    pick = {
        "ticker": "TEST", "entry_template": "A", "entry_price": 100.0,
        "stop_price": 95.0, "shares": 100, "risk_usd": 500.0,
        "chain": "ai_compute", "sector": "XLK", "time_stop_days": 7,
    }
    plan = policy.create_tranche_plan(pick, _budget(empty), DAY, setup=_setup())
    state = _state(plans={plan["plan_id"]: plan}, pending=[{
        "ticker": "TEST", "shares": 40, "entry_ref": 100.0,
        "stop_price": 95.0, "risk_usd": 200.0, "risk_group": "ai_compute",
        "signal_date": DAY, "plan_id": plan["plan_id"],
        "entry_budget": _budget(empty),
    }])
    snapshot = policy.portfolio_snapshot(state, {"TEST": {"date": DAY, "price": 100.0}}, DAY)
    assert snapshot["reserved_cash"] == pytest.approx(10_000.0)
    assert snapshot["reserved_risk"] == pytest.approx(500.0)
    assert snapshot["committed_symbols"] == ["TEST"]
    other = policy.allocate_entry(snapshot, _setup("OTHER"), _budget(snapshot))
    assert other["shares"] <= 150  # Gross25k minus existing full-plan10k, not just first4k.



def _pending(symbol="PENDING", *, shares=100, group="energy"):
    return {"ticker": symbol, "shares": shares, "entry_ref": 100.,
            "stop_price": 95., "risk_usd": shares * 5., "risk_group": group,
            "signal_date": DAY, "entry_budget": {"execution_policy": POLICY}}


def _plan(shares=100):
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(), {}, DAY)
    pick = {"ticker": "TEST", "entry_template": "A", "entry_price": 100.,
            "stop_price": 95., "shares": shares, "risk_usd": shares * 5.,
            "chain": "ai_compute", "sector": "XLK", "time_stop_days": 7}
    return policy.create_tranche_plan(pick, _budget(snap), DAY, setup=_setup())


def _plan_snapshot(plan, as_of="2026-10-27", *, price=100., cash=100_000., positions=()):
    return _policy().portfolio_snapshot(_state(cash, positions=positions,
        plans={plan["plan_id"]: plan}), {"TEST": {"date": as_of, "price": price}}, as_of)


def _quote(as_of="2026-10-27", *, price=100., kind="open"):
    return {"known_at": as_of, "date": as_of, "price": price, "kind": kind,
            "evidence_id": "observed-" + as_of + "-" + kind}


def _fresh(as_of="2026-10-27", **changes):
    return _setup(known_at=as_of, **changes)


def _first_intent(plan):
    return _policy().next_tranche(plan, _plan_snapshot(plan), _quote(), _fresh())


def _receipt(intent, **changes):
    return {"fill_id": "paper-fill-" + intent["intent_id"],
            "intent_id": intent["intent_id"], "plan_id": intent["plan_id"],
            "date": intent["known_at"], "shares": intent["shares"],
            "price": intent["price_limit"], "friction": 0., "execution_mode": "paper"} | changes


@pytest.mark.parametrize("cash", [-1., float("nan"), float("inf"), True])
def test_malformed_cash_fails_before_an_allocation_can_be_reported(cash):
    policy = _policy()
    state = _state(cash)
    before = deepcopy(state)
    with pytest.raises(policy.ExecutionPolicyError): policy.portfolio_snapshot(state, {}, DAY)
    # NaN is unequal to itself; object identity also proves this scalar was untouched.
    assert state["cash"] is before["cash"] or state["cash"] == before["cash"]
    assert state["positions"] == []


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1., 0., True])
def test_nonfinite_or_nonpositive_mark_cannot_value_a_position(price):
    policy = _policy()
    state = _state(90_000., positions=[{"ticker": "HELD", "shares": 100,
        "entry_price": 100., "initial_stop": 95., "stop": 95., "risk_group": "energy"}])
    with pytest.raises(policy.ExecutionPolicyError):
        policy.portfolio_snapshot(state, {"HELD": {"date": DAY, "price": price}}, DAY)


def test_snapshot_hash_changes_with_account_or_same_day_verified_mark():
    policy = _policy()
    state = _state(90_000., positions=[{"ticker": "HELD", "shares": 100,
        "entry_price": 100., "initial_stop": 95., "stop": 95., "risk_group": "energy"}])
    first = policy.portfolio_snapshot(state, {"HELD": {"date": DAY, "price": 100.}}, DAY)
    repeat = policy.portfolio_snapshot(deepcopy(state), {"HELD": {"date": DAY, "price": 100.}}, DAY)
    changed = policy.portfolio_snapshot(state, {"HELD": {"date": DAY, "price": 101.}}, DAY)
    assert first == repeat
    assert first["state_hash"] != changed["state_hash"]
    assert first["nav"] == 100_000.
    assert changed["nav"] == 100_100.


def test_pending_commitment_consumes_a_slot_and_cash_before_new_signal():
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(pending=[_pending()]),
                                    {"PENDING": {"date": DAY, "price": 100.}}, DAY)
    assert snap["reserved_cash"] == 10_000.
    assert snap["reserved_risk"] == 500.
    budget = _budget(snap) | {"max_positions": 1}
    denied = policy.allocate_entry(snap, _setup(), budget)
    assert denied["shares"] == 0
    assert "slot" in denied["reason"]


def test_portfolio_stop_risk_caps_new_entry_across_distinct_groups():
    policy = _policy()
    state = _state(90_000., positions=[{"ticker": "HELD", "shares": 100,
        "entry_price": 100., "initial_stop": 72., "stop": 72., "risk_group": "energy"}])
    snap = policy.portfolio_snapshot(state, {"HELD": {"date": DAY, "price": 100.}}, DAY)
    allocation = policy.allocate_entry(snap, _setup(), _budget(snap))
    assert allocation["shares"] == 40
    assert allocation["portfolio_risk_after"] <= 0.03 + 1e-12


def test_group_risk_includes_prior_pending_reservations():
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(pending=[_pending(shares=590, group="ai_compute")]),
                                    {"PENDING": {"date": DAY, "price": 100.}}, DAY)
    allocation = policy.allocate_entry(snap, _setup(), _budget(snap) | {"gross_cap": .9})
    assert allocation["shares"] == 10
    assert allocation["group_risk_after"] <= 0.03 + 1e-12


@pytest.mark.parametrize("change", [{"risk_pct": .009}, {"single_cap": .21},
    {"gross_cap": 1.}, {"group_risk_pct": .031}, {"portfolio_risk_pct": .04},
    {"max_positions": 11}, {"max_positions": True}, {"snapshot_hash": "f" * 64},
    {"known_at": "2026-10-23"}])
def test_client_cannot_enlarge_risk_or_reuse_a_different_account_snapshot(change):
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(), {}, DAY)
    denied = policy.allocate_entry(snap, _setup(), _budget(snap) | change)
    assert denied["shares"] == 0 and denied["reason"]


@pytest.mark.parametrize("change", [{"breadth": 30., "in_main": False, "in_sub": True,
        "entry_price": 102., "key_level": 100.}, {"mrs_star": 3.9},
        {"allow_new_positions": False}, {"entry_price": float("nan")},
        {"stop_price": 101.}, {"tss_final": float("inf")},
        {"breadth": None, "in_main": True}])
def test_shared_gate_rejects_chasing_and_invalid_current_numeric_evidence(change):
    assert _policy().entry_gate_reasons(_setup(**change))


def test_first_tranche_is_exact_next_open_not_later_recovery_fill():
    policy, plan = _policy(), _plan()
    intent = _first_intent(plan)
    assert intent["shares"] == 40
    assert intent["tranche_index"] == 0
    assert intent["execution_mode"] == "paper"
    late = policy.next_tranche(plan, _plan_snapshot(plan, "2026-10-28"),
                              _quote("2026-10-28"), _fresh("2026-10-28"))
    assert late["shares"] == 0 and late["reason"] == "execution_window_missed"
    assert plan["filled_shares"] == 0


def test_plan_and_intent_ids_are_deterministic_and_account_bound():
    first, second = _plan(), _plan()
    assert first == second
    assert _first_intent(first) == _first_intent(second)
    assert _plan(101)["plan_id"] != first["plan_id"]


def test_paper_fill_is_idempotent_and_conflicting_duplicate_rejects():
    policy, plan = _policy(), _plan()
    intent = _first_intent(plan)
    receipt = _receipt(intent)
    before = deepcopy(plan)
    filled = policy.apply_paper_fill(plan, intent, receipt)
    assert plan == before
    assert filled["filled_shares"] == 40
    assert filled["remaining_shares"] == 60
    assert policy.apply_paper_fill(filled, intent, receipt) == filled
    with pytest.raises(policy.ExecutionPolicyError):
        policy.apply_paper_fill(filled, intent, receipt | {"price": receipt["price"] - .01})


@pytest.mark.parametrize("change", [{"shares": 41}, {"shares": True},
    {"intent_id": "foreign"}, {"plan_id": "foreign"}, {"execution_mode": "live"},
    {"date": "2026-10-28"}, {"price": float("nan")}])
def test_untrusted_or_overfilled_receipt_cannot_change_a_plan(change):
    policy, plan = _policy(), _plan()
    intent = _first_intent(plan)
    before = deepcopy(plan)
    with pytest.raises(policy.ExecutionPolicyError):
        policy.apply_paper_fill(plan, intent, _receipt(intent, **change))
    assert plan == before


def test_later_tranche_needs_later_dated_confirmation_and_never_averages_down():
    policy, plan = _policy(), _plan()
    intent = _first_intent(plan)
    filled = policy.apply_paper_fill(plan, intent, _receipt(intent))
    for as_of, price, kind, setup in [
        ("2026-10-27", 101., "confirmation", _fresh(entry_price=101.)),
        ("2026-10-28", 99., "confirmation", _fresh("2026-10-28", entry_price=99.)),
        ("2026-10-28", 101., "open", _fresh("2026-10-28", entry_price=101.)),
        ("2026-10-28", 101., "confirmation", _fresh("2026-10-28", entry_price=101., mrs_star=7.)),
    ]:
        denied = policy.next_tranche(filled, _plan_snapshot(filled, as_of, price=price),
                                    _quote(as_of, price=price, kind=kind), setup)
        assert denied["shares"] == 0 and denied["reason"]
    approved = policy.next_tranche(filled, _plan_snapshot(filled, "2026-10-28", price=101.),
        _quote("2026-10-28", price=101., kind="confirmation"),
        _fresh("2026-10-28", entry_price=101.))
    assert approved["shares"] == 40 and approved["tranche_index"] == 1


def test_shared_exit_loop_equals_single_day_actions_with_costs():
    # Protection on first high, then the next bar trades through +0.5R.
    batch = simulate_trade([100., 104.], [111., 105.], [99., 101.], [105., 103.],
                           0, 95., cost_bps=10., policy_id=POLICY)
    net_entry = 100.1
    first = evaluate_day(100., 111., 99., 105., net_entry, 95., 1,
                        policy_id=POLICY, initial_stop=95., protected=False)
    second = evaluate_day(104., 105., 101., 103., net_entry, first.next_stop, 2,
                         policy_id=POLICY, initial_stop=95., protected=True)
    assert batch.exit_i == 1
    assert batch.exit_price == pytest.approx(second.exit_price * .999)
    assert batch.r == pytest.approx((batch.exit_price - net_entry) / (net_entry - 95.))


def test_day_five_relative_underperformance_requires_real_sector_reference():
    action = _day(102., 103., 101., 102., 100., 95., 5, sector_return=.06)
    assert action.action == "exit"
    assert "sector" in action.reason or "板块" in action.reason
    missing = _day(102., 103., 101., 102., 100., 95., 5, sector_return=None)
    assert missing.action == "hold"


def _result(day=DAY, *, picks=True):
    result = PipelineResult(day, "synthetic-fixture", mrs=MRSResult(8., 1., 1., 8.,
        regime="黄金共振", position_cap=(.5, .7)), action="BUY")
    result.raw = {"execution_policy": POLICY, "account_currency": "USD"}
    if picks:
        result.picks = [TradePick("TEST", 8., 5.12, "A", 100., 95., 100, .1, 500.,
                                 chain="ai_compute", sector="XLK", time_stop_days=7)]
        snap = _policy().portfolio_snapshot(_state(), {}, day)
        result.raw["entry_budget"] = _budget(snap) | {"known_at": day}
        result.raw["pick_rationale"] = {"TEST": {"size_ratio": 1., "signal_adv_usd": 200_000_000.,
            "entry_setup": _setup(known_at=day), "execution_setup": _setup(known_at=day)}}
    return result


def test_journal_v2_signal_stays_pending_until_paper_fill_exists(tmp_path):
    book = Journal(tmp_path / "journal.json")
    result = _result()
    assert book.log_picks(result) == 1
    assert book.records[0]["status"] == "pending"
    assert book.records[0]["execution_policy"] == POLICY
    class ForbiddenProvider:
        def ohlcv(self, *args, **kwargs):
            raise AssertionError("v2 journal must not invent independent entry fills")
    assert book.settle(ForbiddenProvider(), as_of_date=pd.Timestamp("2026-11-10").date()) == 0
    assert book.records[0]["entry"] is None


def test_paper_and_journal_share_fills_costs_closed_receipts_and_idempotence(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    book = Journal(tmp_path / "journal.json")
    book.log_picks(_result())
    no_bar = lambda ticker: None
    engine.step(DAY, _result(), no_bar)
    assert len(engine.state["entry_plans"]) == 1
    assert engine.state["positions"] == []
    next_day = "2026-10-27"
    bar = Bar(100., 101., 99., 100., 200_000_000., next_day)
    report = engine.step(next_day, _result(next_day, picks=False), lambda ticker: bar)
    assert engine.state["positions"][0]["shares"] == 40
    book.sync_execution(engine.state, as_of=next_day)
    assert book.records[0]["entry"] == engine.state["positions"][0]["entry_price"]
    before = deepcopy(engine.state)
    engine.step(next_day, _result(next_day, picks=False), lambda ticker: bar)
    assert engine.state == before
    stop_day = "2026-10-28"
    engine.step(stop_day, _result(stop_day, picks=False),
                lambda ticker: Bar(94., 96., 93., 95., 200_000_000., stop_day))
    book.sync_execution(engine.state, as_of=stop_day)
    closed = engine.state["closed"][0]
    rec = book.records[0]
    assert rec["status"] == "closed"
    assert rec["pnl_usd"] == closed["pnl_usd"]
    assert rec["friction_cost"] == closed["friction_cost"]
    assert closed["gross_pnl"] - closed["friction_cost"] == pytest.approx(closed["pnl_usd"], abs=.01)
    assert engine.state["entry_plans"][closed["plan_id"]]["remaining_shares"] == 0
    old_records = deepcopy(book.records)
    book.sync_execution(engine.state, as_of=stop_day)
    assert book.records == old_records


def test_v2_paper_fetch_failure_never_partially_mutates_or_rewrites_account(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(DAY, _result(), lambda ticker: None)
    before, stored = deepcopy(engine.state), (tmp_path / "paper.json").read_bytes()
    def unavailable(ticker): raise OSError("market source unavailable")
    with pytest.raises(OSError): engine.step("2026-10-27", _result("2026-10-27", picks=False), unavailable)
    assert engine.state == before
    assert (tmp_path / "paper.json").read_bytes() == stored


def test_legacy_exit_semantics_remain_explicitly_readable():
    legacy = evaluate_day(102., 103., 101., 102., 100., 95., 7,
                          policy_id="legacy-execution-v1")
    assert legacy.action == "hold"


def test_research_date_and_prior_close_valuation_are_not_backdated_signals():
    policy = _policy()
    setup = _setup(known_at="2026-10-23")
    snapshot = policy.portfolio_snapshot(_state(), {}, "2026-10-23")
    budget = _budget(snapshot) | {"known_at": "2026-10-23"}
    pick = {"ticker": "TEST", "entry_template": "A", "entry_price": 100.,
            "stop_price": 95., "shares": 100, "risk_usd": 500.,
            "chain": "ai_compute", "sector": "XLK", "time_stop_days": 7}
    plan = policy.create_tranche_plan(pick, budget, "2026-10-26", setup=setup)
    assert plan["signal_date"] == "2026-10-26"
    assert plan["valuation_date"] == "2026-10-23"
    assert plan["first_execution_date"] == "2026-10-27"
    with pytest.raises(policy.ExecutionPolicyError):
        policy.create_tranche_plan(pick, budget, "2026-10-22", setup=setup)


def test_three_actual_tranches_use_prior_confirmation_and_keep_original_clock(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(DAY, _result(), lambda ticker: None)
    for day, price, expected in [("2026-10-27", 100., 40),
                                 ("2026-10-28", 101., 80),
                                 ("2026-10-29", 102., 100)]:
        result = _result(day, picks=False)
        result.raw["execution_setups"] = {"TEST": _fresh(day, entry_price=price)}
        engine.step(day, result, lambda ticker: Bar(price, price + 1, price - 1, price,
                                                   200_000_000., day))
        pos = engine.state["positions"][0]
        assert pos["shares"] == expected
        assert pos["entry_date"] == "2026-10-27"
        assert pos["initial_stop"] == 95.
        assert pos["initial_risk_usd"] == pytest.approx(pos["shares"] * (pos["entry_price"] - 95.))
    plan = next(iter(engine.state["entry_plans"].values()))
    assert [receipt["shares"] for receipt in plan["fill_receipts"]] == [40, 40, 20]
    assert plan["status"] == "filled"
    assert pos["bars_held"] == 3
    assert engine.state["pending"] == []


def test_daily_high_cannot_invent_a_second_tranche_without_prior_confirmation(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(DAY, _result(), lambda ticker: None)
    for day, price in [("2026-10-27", 100.), ("2026-10-28", 105.)]:
        engine.step(day, _result(day, picks=False),
                    lambda ticker: Bar(price, 112., price - 1, price + 1, 200_000_000., day))
    assert engine.state["positions"][0]["shares"] == 40
    assert len(next(iter(engine.state["entry_plans"].values()))["fill_receipts"]) == 1


def test_paper_storage_failure_leaves_memory_and_published_account_intact(tmp_path, monkeypatch):
    import trading_system.simulator as module
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(DAY, _result(), lambda ticker: None)
    before, stored = deepcopy(engine.state), (tmp_path / "paper.json").read_bytes()
    def fail(*args, **kwargs): raise OSError("disk failure")
    monkeypatch.setattr(module, "write_json_atomic", fail)
    with pytest.raises(OSError):
        engine.step("2026-10-27", _result("2026-10-27", picks=False),
            lambda ticker: Bar(100., 101., 99., 100., 200_000_000., "2026-10-27"))
    assert engine.state == before
    assert (tmp_path / "paper.json").read_bytes() == stored


def test_numeric_backtest_and_paper_use_same_integer_fill_exit_and_net_costs(tmp_path):
    from types import SimpleNamespace
    from trading_system.backtest import run_backtest
    days = pd.to_datetime([DAY, "2026-10-27", "2026-10-28"])
    data = {"open": [100., 100., 94.], "high": [101., 101., 96.],
            "low": [99., 99., 93.], "close": [100., 100., 95.]}
    panel = SimpleNamespace(dates=days, tickers=["TEST"], sector_data={})
    for key, values in data.items(): setattr(panel, key, pd.DataFrame({"TEST": values}, index=days))
    panel.vol = pd.DataFrame({"TEST": [2_000_000.] * 3}, index=days)
    frame = DayFrame(days[0], 8., "黄金共振", [SectorSnap("XLK", 8., 80.)],
        [CandidateSnap("TEST", 8., 1., 1., "XLK", "ai_compute", True, "A", 100., 95.,
                       key_level=100., adv_usd=200_000_000.)])
    backtest = run_backtest([frame], panel, GateParams(execution_policy=POLICY))
    engine = SimEngine(str(tmp_path / "paper.json"))
    signal = _result()
    signal.picks[0].shares = 160
    signal.picks[0].risk_usd = 800.
    signal.picks[0].position_pct = .16
    signal.raw["entry_budget"]["gross_cap"] = .9
    engine.step(DAY, signal, lambda ticker: Bar(100., 101., 99., 100., 200_000_000., DAY))
    for index in (1, 2):
        day = str(days[index].date())
        engine.step(day, _result(day, picks=False), lambda ticker: Bar(
            data["open"][index], data["high"][index], data["low"][index], data["close"][index],
            float((panel.vol["TEST"] * panel.close["TEST"]).iloc[:index+1].mean()), day))
    assert backtest["n_trades"] == 1
    actual, replay = engine.state["closed"][0], backtest["paper_state"]["closed"][0]
    for key in ("shares", "entry_date", "exit_date", "entry", "exit", "pnl_usd", "gross_pnl", "friction_cost"):
        assert actual[key] == replay[key]
    assert backtest["paper_state"]["cash"] == engine.state["cash"]
    assert backtest["evidence_scope"] == "numeric-paper-replay-not-historical-required-model-alpha"


@pytest.mark.parametrize("change", [{"max_positions": 100}, {"portfolio_risk_pct": .5}, {"snapshot_hash": "bad"}])
def test_frozen_plan_cannot_bypass_the_budget_validation(change):
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(), {}, DAY)
    pick = {"ticker": "TEST", "entry_template": "A", "entry_price": 100.,
            "stop_price": 95., "shares": 100, "risk_usd": 500.}
    with pytest.raises(policy.ExecutionPolicyError):
        policy.create_tranche_plan(pick, _budget(snap) | change, DAY, setup=_setup())


def test_tampered_plan_quantity_cannot_hide_unfilled_reservations():
    policy, plan = _policy(), _plan()
    plan["status"] = "filled"  # No receipts: this is an inconsistent ledger.
    with pytest.raises(policy.ExecutionPolicyError): _plan_snapshot(plan)


def test_market_state_caps_gross_budget_even_if_client_requests_the_global_maximum():
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(), {}, DAY)
    setup = _setup(mrs_star=5.8, in_main=False, in_sub=True, size_ratio=.35)
    denied = policy.allocate_entry(snap, setup, _budget(snap) | {"gross_cap": .9})
    assert denied["shares"] == 0


def test_foreign_currency_symbol_cannot_enter_pure_usd_allocation():
    policy = _policy()
    snap = policy.portfolio_snapshot(_state(), {}, DAY)
    assert policy.allocate_entry(snap, _setup("0700.HK"), _budget(snap))["shares"] == 0


def _risk_context(cash=100_000., *, research_day=DAY, valuation_day=DAY, held=False):
    from trading_system.data_models import SectorScore, StockCandidate
    state = _state(cash)
    candidates = [StockCandidate("TEST", sector_etf="XLK", chain_id="ai_compute",
        price=100., stop_price=95., key_level=100., entry_template="A", tss_final=8.,
        adv_usd=200_000_000., atr_pct=.03, c_liq=1.)]
    if held:
        state["positions"] = [{"ticker": "HELD", "shares": 100, "entry_price": 100.,
            "initial_stop": 95., "stop": 95., "risk_group": "energy", "sector": "XLE",
            "entry_date": "2026-10-23"}]
        candidates.append(StockCandidate("HELD", sector_etf="XLE", chain_id="energy",
            price=100., stop_price=95., key_level=100., entry_template="C", tss_final=8.,
            adv_usd=200_000_000., atr_pct=.03, c_liq=1.))
    frames = {c.ticker: pd.DataFrame({"Close": [100.], "Open": [100.], "High": [101.],
        "Low": [99.], "Volume": [2_000_000.]}, index=pd.to_datetime([valuation_day])) for c in candidates}
    return {"execution_policy": POLICY, "trade_date": research_day, "portfolio_state": state,
        "portfolio_valuation_date": valuation_day, "market_data": {"stock_ohlcv": frames},
        "mrs": MRSResult(8., 0., 1., 8., position_cap=(.7, .9)),
        "sectors": [SectorScore("XLK", 8., breadth=80., in_main_pool=True)], "chain_map": {}, "watchlist": candidates}


@pytest.mark.parametrize("cash, shares", [(50_000., 80), (100_000., 160), (200_000., 320)])
def test_production_risk_v2_uses_canonical_current_nav(cash, shares):
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    context = _risk_context(cash)
    picks = RiskManagerAgent(None, account_usd=100_000., max_picks=10).execute(context)
    assert [pick.shares for pick in picks] == [shares]
    assert context["entry_budget"]["schema"] == "tiger.entry-budget.v2"
    assert context["portfolio_snapshot"]["nav"] == cash
    assert context["cohort_inputs"]["portfolio_state"] == context["portfolio_state"]
    assert context["cohort_inputs"]["portfolio_snapshot"]["state_hash"] == context["entry_budget"]["snapshot_hash"]


def test_production_risk_preserves_held_setup_and_all_overlapping_reasons():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    context = _risk_context(90_000., held=True)
    context["watchlist"][0].entry_template = ""
    context["mrs"].mrs_star = 5.17
    RiskManagerAgent(None, max_picks=10).execute(context)
    assert set(context["execution_setups"]) == {"TEST", "HELD"}
    row = next(row for row in context["rejections"]["candidates"] if row["ticker"] == "TEST")
    assert "entry_template_missing" in row["reasons"]
    assert "numeric_gates_failed" in row["reasons"]
    assert "no_legal_probe_channel" in row["reasons"]
    assert context["picks"] == []


def test_production_risk_has_no_fixed_cash_fallback_for_unknown_account():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    context = _risk_context()
    context.pop("portfolio_state")
    assert RiskManagerAgent(None, max_picks=10).execute(context) == []
    assert context["action"] == "WAIT"
    assert "portfolio_state_missing" in context["rejections"]["candidates"][0]["reasons"]


def test_production_risk_prior_close_is_disclosed_without_backdating_signal():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    context = _risk_context(research_day="2026-10-26", valuation_day="2026-10-23")
    assert RiskManagerAgent(None, max_picks=10).execute(context)
    assert context["entry_budget"]["known_at"] == "2026-10-23"
    assert context["cohort_inputs"]["signal_date"] == "2026-10-26"
    assert context["execution_setups"]["TEST"]["known_at"] == "2026-10-23"


def test_missing_template_does_not_hide_invalid_stop_in_multilabel_gates():
    reasons = _policy().entry_gate_reasons(_setup(entry_template="", stop_price=101.))
    assert "entry_template_missing" in reasons
    assert "invalid_entry_or_stop" in reasons


def test_nonnumeric_setup_fields_return_reasons_without_type_errors():
    for changes in [{"size_ratio": None}, {"entry_price": None, "breadth": 20.}]:
        assert _policy().entry_gate_reasons(_setup(**changes))


def test_v2_does_not_implicitly_promote_active_sector_proxy_experiment():
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    context = _risk_context()
    candidate = context["watchlist"][0]
    context["market_data"]["stock_ohlcv"]["CDW"] = context["market_data"]["stock_ohlcv"].pop("TEST")
    candidate.ticker, candidate.chain_id, candidate.sector_etf = "CDW", "", "XLY"
    assert RiskManagerAgent(None, max_picks=10).execute(context) == []
    row = context["rejections"]["candidates"][0]
    assert row["sector_evidence"]["source"] == "unmapped"
    assert "sector_evidence_missing" in row["reasons"]


def test_required_model_explicit_empty_confirmations_cannot_fall_back_to_numeric_setup(tmp_path):
    engine = SimEngine(str(tmp_path / "paper.json"))
    engine.step(DAY, _result(), lambda ticker: None)
    for day, price in [("2026-10-27", 100.), ("2026-10-28", 101.)]:
        result = _result(day, picks=False)
        result.raw["execution_setups"] = {"TEST": _fresh(day, entry_price=price)}
        result.raw["tranche_confirmations"] = {}
        engine.step(day, result, lambda ticker: Bar(price, price + 1, price - 1, price,
                                                   200_000_000., day))
    assert engine.state["positions"][0]["shares"] == 40
    assert "confirmation" not in next(iter(engine.state["entry_plans"].values()))


@pytest.mark.parametrize('policy', ['legacy-execution-v1', POLICY])
def test_backtest_result_explicitly_identifies_version_and_numeric_scope_without_signals(policy):
    from types import SimpleNamespace
    from trading_system.backtest import run_backtest
    panel = SimpleNamespace(dates=pd.DatetimeIndex([]), tickers=[])
    params = GateParams(execution_policy=policy, initial_cash=90000., max_positions=3)
    result = run_backtest([], panel, params)
    assert result['execution_policy'] == policy
    assert result['params'] is params and result['n_trades'] == 0
    assert 'numeric' in result['evidence_scope']
    if policy == POLICY:
        assert result['paper_state']['initial_cash'] == 90000.
        assert result['paper_state']['cash'] == 90000.
        assert result['evidence_scope'] == 'numeric-paper-replay-not-historical-required-model-alpha'


def test_long_lived_plans_bind_source_account_by_hash_without_recursive_history_growth():
    import json
    p = _policy()
    state = _state()
    sizes = []
    for number in range(6):
        setup = _setup(f'TEST{number}')
        snapshot = p.portfolio_snapshot(state, {}, DAY)
        budget = _budget(snapshot) | {'source_snapshot': deepcopy(snapshot), 'source_account_state': deepcopy(state)}
        original = deepcopy(budget)
        pick = {'ticker': setup['symbol'], 'entry_template': 'A', 'entry_price': 100., 'stop_price': 95., 'shares': 1}
        plan = p.create_tranche_plan(pick, budget, DAY, setup=setup)
        assert budget == original
        assert 'source_account_state' not in plan['budget']
        assert 'source_account_state' not in plan['frozen_contract']['budget']
        assert plan['budget']['source_account_state_sha256'] == p._hash(state)
        assert plan['budget']['source_budget_sha256'] == p._hash(original)
        assert p.create_tranche_plan(pick, plan['budget'], DAY, setup=setup)['plan_id'] == plan['plan_id']
        p._cancel(plan, 'SYNTHETIC_ONLY_frozen_plan_not_executed')
        state['entry_plans'][plan['plan_id']] = plan
        sizes.append(len(json.dumps(state)))
    assert sizes[-1] < sizes[0] * 8, sizes
    assert sizes[-1] < 100000
    assert p.portfolio_snapshot(state, {}, DAY)['nav'] == 100000.


def test_compact_plan_source_hash_tampering_is_not_a_new_admitted_contract():
    p = _policy()
    state = _state()
    snapshot = p.portfolio_snapshot(state, {}, DAY)
    budget = _budget(snapshot) | {'source_account_state': deepcopy(state), 'source_snapshot': snapshot}
    pick = {'ticker': 'TEST', 'entry_template': 'A', 'entry_price': 100., 'stop_price': 95., 'shares': 100}
    plan = p.create_tranche_plan(pick, budget, DAY, setup=_setup())
    assert 'source_budget_sha256' in plan['budget']
    for change_frozen in (False, True):
        changed = deepcopy(plan)
        changed['budget']['source_budget_sha256'] = 'd' * 64
        if change_frozen:
            changed['frozen_contract']['budget']['source_budget_sha256'] = 'd' * 64
        with pytest.raises(p.ExecutionPolicyError):
            p._validate_plan(changed)


def test_existing_legal_full_source_plan_remains_readable_after_compaction():
    p = _policy()
    state = _state()
    snapshot = p.portfolio_snapshot(state, {}, DAY)
    full = _budget(snapshot) | {'source_account_state': deepcopy(state), 'source_snapshot': snapshot}
    pick = {'ticker': 'TEST', 'entry_template': 'A', 'entry_price': 100., 'stop_price': 95., 'shares': 100}
    plan = p.create_tranche_plan(pick, full, DAY, setup=_setup())
    # Reconstruct the actual pre-compaction contract, with its original full
    # immutable budget. No receipt or historical record is rewritten.
    plan['budget'] = deepcopy(full)
    plan['frozen_contract']['budget'] = deepcopy(full)
    plan['plan_id'] = p._hash(plan['frozen_contract'])
    p._validate_plan(plan)
    state['entry_plans'][plan['plan_id']] = plan
    valued = p.portfolio_snapshot(state, {'TEST': {'date': DAY, 'price': 100.}}, DAY)
    assert valued['reserved_cash'] == 10000.
    assert valued['committed_symbols'] == ['TEST']


@pytest.mark.parametrize('opening', [102., 102.8])
def test_current_protection_stop_at_open_blocks_add_before_exiting_original_shares(tmp_path, opening):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    confirmed = _result('2026-10-27', picks=False)
    confirmed.raw['execution_setups'] = {'TEST': _fresh('2026-10-27', entry_price=105.)}
    engine.step('2026-10-27', confirmed,
        lambda ticker: Bar(100., 111., 99., 105., 200_000_000., '2026-10-27'))
    position = engine.state['positions'][0]
    assert position['shares'] == 40 and position['stop'] == pytest.approx(102.8)
    report = engine.step('2026-10-28', _result('2026-10-28', picks=False),
        lambda ticker: Bar(opening, 104., 101., 103., 200_000_000., '2026-10-28'))
    plan = next(iter(engine.state['entry_plans'].values()))
    assert report['fills'] == []
    assert len(plan['fill_receipts']) == 1 and plan['status'] == 'closed'
    assert engine.state['positions'] == [] and engine.state['pending'] == []
    assert engine.state['closed'][0]['shares'] == 40
    assert engine.state['closed'][0]['exit_raw'] == opening
    assert plan['cancelled_shares'] == 60


@pytest.mark.parametrize('opening', [94.9, 95.])
def test_first_open_at_or_below_structural_stop_is_not_repaired_by_added_friction(tmp_path, opening):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    report = engine.step('2026-10-27', _result('2026-10-27', picks=False),
        lambda ticker: Bar(opening, 97., 94., 96., 200_000_000., '2026-10-27'))
    assert report['fills'] == [] and engine.state['closed'] == []
    assert engine.state['positions'] == [] and engine.state['pending'] == []
    plan = next(iter(engine.state['entry_plans'].values()))
    assert plan['status'] == 'cancelled' and plan['fill_receipts'] == []
    assert engine.state['cash'] == 100000.


def test_consumed_confirmation_cannot_fund_a_third_tranche_after_a_later_empty_research(tmp_path):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    for day, price, confirmations, expected_shares in [
        ('2026-10-27', 100., {'TEST': _fresh('2026-10-27', entry_price=100.)}, 40),
        ('2026-10-28', 101., {}, 80),
        ('2026-10-29', 102., {}, 80),
    ]:
        result = _result(day, picks=False)
        result.raw['tranche_confirmations'] = confirmations
        engine.step(day, result, lambda ticker: Bar(price, price + 1, price - 1, price,
                                                    200_000_000., day))
        assert engine.state['positions'][0]['shares'] == expected_shares
    plan = next(iter(engine.state['entry_plans'].values()))
    assert len(plan['fill_receipts']) == 2 and 'confirmation' not in plan


def test_later_add_cannot_consume_a_confirmation_older_than_the_previous_complete_trading_day(tmp_path):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    result = _result('2026-10-27', picks=False)
    result.raw['tranche_confirmations'] = {'TEST': _fresh('2026-10-27', entry_price=100.)}
    engine.step('2026-10-27', result,
        lambda ticker: Bar(100., 101., 99., 100., 200_000_000., '2026-10-27'))
    # There is no new complete day / qualification. A missing bar cannot turn
    # Tuesday's old model confirmation into a fresh Thursday opening intent.
    before = deepcopy(engine.state)
    with pytest.raises(_policy().ExecutionPolicyError):
        engine.step('2026-10-28', _result('2026-10-28', picks=False), lambda ticker: None)
    assert engine.state == before
    report = engine.step('2026-10-29', _result('2026-10-29', picks=False),
        lambda ticker: Bar(102., 103., 101., 102., 200_000_000., '2026-10-29'))
    assert report['fills'] == [] and engine.state['positions'][0]['shares'] == 40


def test_same_complete_bar_retry_preserves_the_previously_frozen_model_confirmation(tmp_path):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    result = _result('2026-10-27', picks=False)
    result.raw['tranche_confirmations'] = {'TEST': _fresh('2026-10-27', entry_price=100.)}
    bar = Bar(100., 101., 99., 100., 200_000_000., '2026-10-27')
    engine.step('2026-10-27', result, lambda ticker: bar)
    before = deepcopy(engine.state)
    engine.step('2026-10-27', result, lambda ticker: bar)
    assert engine.state == before
    engine.step('2026-10-28', _result('2026-10-28', picks=False),
        lambda ticker: Bar(101., 102., 100., 101., 200_000_000., '2026-10-28'))
    assert engine.state['positions'][0]['shares'] == 80


def test_v2_journal_does_not_duplicate_full_account_history_inside_every_signal(tmp_path):
    book = Journal(tmp_path / 'journal.json')
    result = _result()
    source = _state()
    source['closed'] = [{'synthetic_account_history': 'TEST ONLY'}] * 20
    result.raw['entry_budget']['source_account_state'] = deepcopy(source)
    full_budget = deepcopy(result.raw['entry_budget'])
    book.log_picks(result)
    stored = book.records[0]['entry_budget']
    assert 'source_account_state' not in stored
    assert stored['source_account_state_sha256'] == _policy()._hash(source)
    assert stored['source_budget_sha256'] == _policy()._hash(full_budget)
    assert stored['risk_pct'] == full_budget['risk_pct']
    assert result.raw['entry_budget'] == full_budget
    assert book.records[0]['entry'] is None


def test_prior_close_valued_research_cannot_backdate_a_new_model_confirmation(tmp_path):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step(DAY, _result(), lambda ticker: None)
    first = _result('2026-10-27', picks=False)
    first.raw['tranche_confirmations'] = {'TEST': _fresh('2026-10-27', entry_price=100.)}
    engine.step('2026-10-27', first,
        lambda ticker: Bar(100., 101., 99., 100., 200_000_000., '2026-10-27'))
    later_research = _result('2026-10-29', picks=False)
    later_research.raw['tranche_confirmations'] = {'TEST': _fresh('2026-10-28', entry_price=101.)}
    engine.step('2026-10-29', later_research,
        lambda ticker: Bar(101., 102., 100., 101., 200_000_000., '2026-10-28'))
    assert engine.state['positions'][0]['shares'] == 80
    assert 'confirmation' not in next(iter(engine.state['entry_plans'].values()))
    report = engine.step('2026-10-29', _result('2026-10-29', picks=False),
        lambda ticker: Bar(102., 103., 101., 102., 200_000_000., '2026-10-29'))
    assert report['fills'] == [] and engine.state['positions'][0]['shares'] == 80


def test_previous_complete_trading_day_confirmation_survives_the_weekend(tmp_path):
    engine = SimEngine(str(tmp_path / 'paper.json'))
    engine.step('2026-10-22', _result('2026-10-22'), lambda ticker: None)
    first = _result('2026-10-23', picks=False)
    first.raw['tranche_confirmations'] = {'TEST': _fresh('2026-10-23', entry_price=100.)}
    engine.step('2026-10-23', first,
        lambda ticker: Bar(100., 101., 99., 100., 200_000_000., '2026-10-23'))
    report = engine.step('2026-10-26', _result('2026-10-26', picks=False),
        lambda ticker: Bar(101., 102., 100., 101., 200_000_000., '2026-10-26'))
    assert len(report['fills']) == 1 and report['fills'][0]['shares'] == 40
    assert engine.state['positions'][0]['shares'] == 80


@pytest.mark.parametrize('change', [
    {'first_execution_date': DAY}, {'valuation_date': '2026-10-23'},
    {'entry_template': 'B'}, {'risk_group': 'foreign-group'}, {'sector': 'XLF'},
    {'chain': 'foreign-chain'}, {'time_stop_days': 8}, {'status': 'partial'},
    {'tranche_field': 'index', 'value': 2}, {'tranche_field': 'shares', 'value': 90},
    {'tranche_field': 'weight', 'value': .8}, {'tranche_field': 'filled_shares', 'value': 1},
    {'tranche_field': 'status', 'value': 'filled'},
])
def test_mutable_plan_projection_cannot_change_its_frozen_execution_window_or_tranches(change):
    p, plan = _policy(), _plan()
    if 'tranche_field' in change:
        plan['tranches'][0][change['tranche_field']] = change['value']
    else:
        plan.update(change)
    with pytest.raises(p.ExecutionPolicyError):
        p._validate_plan(plan)


def test_persisted_plan_receipts_must_still_be_paper_and_bound_to_the_same_plan():
    p, plan = _policy(), _plan()
    intent = _first_intent(plan)
    filled = p.apply_paper_fill(plan, intent, _receipt(intent))
    for changes in [{'execution_mode': 'live'}, {'plan_id': 'foreign'}, {'shares': 0}, {'price': float('nan')}]:
        changed = deepcopy(filled)
        changed['fill_receipts'][0].update(changes)
        with pytest.raises(p.ExecutionPolicyError):
            p._validate_plan(changed)


@pytest.mark.parametrize('changes', [{'entry_price': 101.}, {'time_stop_days': 8}, {'time_stop_days': True}])
def test_new_plan_cannot_freeze_a_different_entry_or_an_invalid_time_deadline(changes):
    p = _policy()
    snap = p.portfolio_snapshot(_state(), {}, DAY)
    pick = {'ticker': 'TEST', 'entry_template': 'A', 'entry_price': 100., 'stop_price': 95., 'shares': 100} | changes
    with pytest.raises(p.ExecutionPolicyError):
        p.create_tranche_plan(pick, _budget(snap), DAY, setup=_setup())
