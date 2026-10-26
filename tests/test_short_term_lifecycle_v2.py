"""Whitepaper lifecycle and business-goal contracts; no model/network/broker I/O.

Initial red phase intentionally targets the not-yet-created v2 modules. These
cases assert business boundaries, not a second copy of their implementation.
"""
from copy import deepcopy
import json

import pytest

NOW = "2026-10-27T14:00:00+00:00"
LATER = "2026-10-27T14:01:00+00:00"
EXPIRES = "2026-10-27T20:00:00+00:00"


def result(*, approved=False):
    candidate = {"ticker": "AAA", "price": 100.0, "key_level": 100.0,
                 "stop_price": 95.0, "entry_template": "A", "tss_final": 8.0,
                 "sector_etf": "XLK", "chain_id": "ai_cloud"}
    pick = {"ticker": "AAA", "entry_price": 100.0, "stop_price": 95.0,
            "shares": 100, "position_pct": .1, "risk_usd": 500.0,
            "entry_template": "A", "tss_final": 8.0, "tos": 4.48}
    return {"trade_date": "2026-10-27", "provider": "real-test-fixture",
            "action": "BUY" if approved else "WAIT", "watchlist": [candidate],
            "picks": [pick] if approved else [], "raw": {
                "scan_stats": {"total": 100, "pass_hard": 80, "top_n": 1},
                "execution_decisions": [{"symbol": "AAA", "action": "buy" if approved else "watch",
                    "source_ids": ["md_AAA_fixture", "ds_issuer_fixture"],
                    "entry_condition": "回踩100并完成本轮风险复核", "invalidation": "跌破95"}],
                "rejections": {"schema": "tiger.rejections.v1", "candidate_count": 1,
                    "candidates": [{"ticker": "AAA", "status": "approved" if approved else "rejected",
                                    "reasons": [] if approved else ["mrs_below_standard", "no_legal_probe_channel"]}]}}}


def account():
    return {"known_at": "2026-10-27", "frozen_at": NOW, "nav": 100000.0, "cash": 100000.0, "gross": 0.0,
            "reserved_cash": 0.0, "reserved_risk": 0.0, "group_risk": {},
            "committed_symbols": [], "state_hash": "a" * 64}


def plan():
    return {"ticker": "AAA", "entry_price": 100.0, "stop_price": 95.0,
            "shares": 100, "entry_template": "A", "risk_usd": 500.0}


def quote(price=100.0, **fields):
    return {"AAA": {"price": price, "ts": NOW, "kind": "realtime", "stale": False,
                    "evidence_id": "q_1", **fields}}


def ledger(tmp_path, *, approved=False):
    from trading_system.opportunity_lifecycle import OpportunityLedger
    book = OpportunityLedger(tmp_path / "opportunity_ledger.json")
    book.record_research(result(approved=approved), run_id="run-source-1",
                         observed_at=NOW, expires_at=EXPIRES)
    return book


def row(book, ticker="AAA"):
    return next(item for item in book.snapshot()["opportunities"] if item["ticker"] == ticker)


def evaluation(book, *, state=None, outcomes=None, as_of=LATER):
    from trading_system.business_evaluation import build_business_evaluation
    return build_business_evaluation(result(), lifecycle=book.snapshot(),
        account_state=state or {"cash": 100000.0, "initial_cash": 100000.0,
                               "positions": [], "pending": [], "closed": [], "equity_curve": []},
        as_of=as_of, run_id="run-source-1", outcomes=outcomes)


def test_watch_with_zero_picks_is_recorded_and_only_requests_requalification(tmp_path):
    book = ledger(tmp_path)
    assert row(book)["state"] == "WATCH"
    alerts = book.observe_quotes(quote(), observed_at=LATER)
    assert [a["kind"] for a in alerts] == ["REQUALIFY"]
    assert row(book)["state"] == "WATCH"
    assert book.snapshot()["intents"] == []
    assert row(book)["rejection_reasons"] == ["mrs_below_standard", "no_legal_probe_channel"]


def test_missing_watch_price_levels_remain_unknown(tmp_path):
    from trading_system.opportunity_lifecycle import OpportunityLedger
    book = OpportunityLedger(tmp_path / "l.json")
    source = result()
    source["watchlist"][0].update(key_level=0.0, price=0.0, stop_price=0.0)
    book.record_research(source, run_id="run-1", observed_at=NOW, expires_at=EXPIRES)
    assert row(book)["levels"] == {"entry": None, "stop": None, "protect": None}
    assert book.observe_quotes(quote(), observed_at=LATER) == []


@pytest.mark.parametrize("fields", [
    {"stale": True}, {"kind": "eod_close"}, {"ts": "2026-10-26T14:00:00+00:00"},
    {"ts": "2026-10-27T14:02:00+00:00"}, {"ts": None}, {"ts": "2026-10-27T14:00:00"},
    {"price": float("nan")}, {"price": True}, {"price": -1.0},
])
def test_unusable_quotes_do_not_create_intents_or_trigger_rechecks(tmp_path, fields):
    book = ledger(tmp_path)
    assert book.observe_quotes(quote(**fields), observed_at=LATER) == []
    assert row(book)["state"] == "WATCH"
    assert book.snapshot()["intents"] == []
    assert book.snapshot()["observation_refusals"][-1]["reason"]


def test_current_risk_rejection_never_promotes_watch(tmp_path):
    book = ledger(tmp_path)
    update = book.requalify(row(book)["opportunity_id"], run_id="run-recheck-1", observed_at=LATER,
        accepted=False, reasons=["market_block"], risk_evidence_ids=["risk_recheck_1"],
        account_snapshot=account())
    assert update["state"] == "WATCH"
    assert book.snapshot()["intents"] == []
    assert row(book)["rejection_reasons"] == ["market_block"]


def test_fresh_evidence_bound_recheck_creates_intent_but_no_fill(tmp_path):
    book = ledger(tmp_path)
    new_account = account()
    update = book.requalify(row(book)["opportunity_id"], run_id="run-recheck-1", observed_at=LATER,
        accepted=True, reasons=[], risk_evidence_ids=["risk_recheck_1"],
        account_snapshot=new_account, plan=plan())
    assert update["state"] == "INTENT_PENDING"
    intent = book.snapshot()["intents"][0]
    assert intent["account_state_hash"] == new_account["state_hash"]
    assert intent["account_kind"] == "paper"
    assert intent["shares"] == 100
    assert intent["requires_execution_recheck"] is True
    assert row(book).get("filled_shares", 0) == 0


@pytest.mark.parametrize("change", ["no_refs", "future_account", "bad_quantity", "bad_stop", "no_template"])
def test_requalification_fails_closed_without_current_risk_and_executable_plan(tmp_path, change):
    book = ledger(tmp_path)
    refs, snap, proposed = ["risk_1"], account(), plan()
    if change == "no_refs": refs = []
    if change == "future_account": snap["known_at"] = "2026-10-28"
    if change == "bad_quantity": proposed["shares"] = True
    if change == "bad_stop": proposed["stop_price"] = 101.0
    if change == "no_template": proposed["entry_template"] = ""
    with pytest.raises(ValueError):
        book.requalify(row(book)["opportunity_id"], run_id="run-new", observed_at=LATER,
            accepted=True, reasons=[], risk_evidence_ids=refs, account_snapshot=snap, plan=proposed)
    assert book.snapshot()["intents"] == []


def test_repeated_source_is_idempotent_but_changed_research_collision_errors(tmp_path):
    book = ledger(tmp_path)
    before = book.snapshot()
    book.record_research(result(), run_id="run-source-1", observed_at=NOW, expires_at=EXPIRES)
    assert book.snapshot() == before
    changed = result()
    changed["watchlist"][0]["key_level"] = 101.0
    with pytest.raises(ValueError, match="collision|conflict|changed"):
        book.record_research(changed, run_id="run-source-1", observed_at=NOW, expires_at=EXPIRES)
    assert book.snapshot() == before


def test_quote_alerts_idempotent_per_evidence_and_expired_plan_cannot_enter(tmp_path):
    book = ledger(tmp_path)
    first = book.observe_quotes(quote(), observed_at=LATER)
    assert first
    assert book.observe_quotes(quote(), observed_at=LATER) == []
    assert book.observe_quotes(quote(ts="2026-10-27T21:00:00+00:00"),
                               observed_at="2026-10-27T21:00:00+00:00") == []
    assert row(book)["state"] == "EXPIRED"
    assert book.snapshot()["intents"] == []


def test_held_stop_and_protection_survive_zero_pick_daily_refresh(tmp_path):
    book = ledger(tmp_path)
    state = {"positions": [{"ticker": "AAA", "shares": 40, "entry_price": 100.0,
                            "initial_stop": 95.0, "stop": 95.0, "entry_date": "2026-10-26"}],
             "pending": [], "closed": []}
    book.sync_account(state, snapshot_id="paper-snapshot-1", observed_at=NOW)
    assert row(book)["state"] == "HELD"
    alerts = book.observe_quotes(quote(111.0), observed_at=LATER)
    assert [a["kind"] for a in alerts] == ["PROTECT"]
    assert alerts[0]["proposed_stop"] == 102.5
    assert row(book)["levels"]["stop"] == 95.0
    assert row(book)["pending_protection_stop"] == 102.5
    state_before = book.snapshot()
    book.record_research(result(), run_id="run-source-2", observed_at=LATER, expires_at=EXPIRES)
    held = next(item for item in book.snapshot()["opportunities"] if item["state"] == "HELD")
    assert held["levels"]["stop"] == 95.0
    assert held["pending_protection_stop"] == 102.5
    assert held["filled_shares"] == 40
    assert len(book.snapshot()["intents"]) == len(state_before["intents"])


def test_stop_is_exclusive_and_no_quote_manufactures_closed_trade(tmp_path):
    book = ledger(tmp_path)
    book.sync_account({"positions": [{"ticker": "AAA", "shares": 40, "entry_price": 100.0,
        "initial_stop": 95.0, "stop": 95.0, "entry_date": "2026-10-26"}], "pending": [], "closed": []},
        snapshot_id="paper-snapshot-1", observed_at=NOW)
    alerts = book.observe_quotes(quote(94.0), observed_at=LATER)
    assert [a["kind"] for a in alerts] == ["STOP"]
    assert row(book)["state"] == "EXIT_PENDING"
    assert row(book)["filled_shares"] == 40
    assert not any(item["state"] == "CLOSED" for item in book.snapshot()["opportunities"])


def test_manual_actual_holdings_need_execution_evidence(tmp_path):
    from trading_system.opportunity_lifecycle import OpportunityLedger
    book = OpportunityLedger(tmp_path / "manual.json", account_kind="manual")
    state = {"positions": [{"ticker": "AAA", "shares": 40, "entry_price": 100.0,
                            "initial_stop": 95.0, "stop": 95.0}], "pending": [], "closed": []}
    with pytest.raises(ValueError, match="evidence"):
        book.sync_account(state, snapshot_id="manual-1", observed_at=NOW, account_kind="manual")
    assert book.snapshot()["opportunities"] == []


@pytest.mark.parametrize("raw", ['{"schema":"unexpected"}', '{"schema":NaN}',
                                  '{"schema":"a","schema":"b"}', '{'])
def test_bad_ledger_is_never_reinitialized(tmp_path, raw):
    from trading_system.opportunity_lifecycle import OpportunityLedger
    path = tmp_path / "opportunity_ledger.json"
    path.write_text(raw)
    with pytest.raises(ValueError): OpportunityLedger(path)
    assert path.read_text() == raw


def test_atomic_storage_failure_preserves_original_ledger(tmp_path, monkeypatch):
    import trading_system.opportunity_lifecycle as module
    book = ledger(tmp_path)
    book.save()
    path = tmp_path / "opportunity_ledger.json"
    before = path.read_bytes()
    book.observe_quotes(quote(), observed_at=LATER)
    def fail(*args, **kwargs): raise OSError("disk unavailable")
    monkeypatch.setattr(module, "write_json_atomic", fail)
    with pytest.raises(OSError): book.save()
    assert path.read_bytes() == before


def test_concurrent_writer_cannot_silently_overwrite_newer_head(tmp_path):
    from trading_system.opportunity_lifecycle import OpportunityLedger
    first = ledger(tmp_path)
    first.save()
    second = OpportunityLedger(tmp_path / "opportunity_ledger.json")
    first.observe_quotes(quote(), observed_at=LATER)
    first.save()
    second.record_research(result(), run_id="run-source-2", observed_at=LATER, expires_at=EXPIRES)
    with pytest.raises(ValueError, match="conflict|changed|revision"): second.save()


def test_unmatured_business_metrics_are_unknown_not_fabricated_zero(tmp_path):
    book = ledger(tmp_path)
    report = evaluation(book)
    assert report["schema"] == "tiger.business-evaluation.v2"
    assert report["opportunities"]["capture_rate"] is None
    assert report["opportunities"]["unavailable_reason"] == "NO_MATURED_INDEPENDENT_LABELS"
    assert report["account"]["n_closed"] == 0
    assert report["account"]["win_rate"] is None
    assert report["account"]["expectancy_r"] is None
    assert report["account"]["profitability_established"] is False
    assert report["funnel"]["tss_candidates"] == 1
    assert report["rejections"]["multi_label"] is True


def test_evaluation_does_not_mutate_decisions_or_account(tmp_path):
    from trading_system.business_evaluation import build_business_evaluation
    book = ledger(tmp_path)
    source, life, state = result(), book.snapshot(), {"positions": [], "pending": [], "closed": [],
                "cash": 100000., "initial_cash": 100000., "equity_curve": []}
    expected = deepcopy((source, life, state))
    build_business_evaluation(source, lifecycle=life, account_state=state, as_of=LATER, run_id="run-1")
    assert (source, life, state) == expected


def test_realized_net_cost_identity_and_no_actual_profit_from_paper(tmp_path):
    book = ledger(tmp_path)
    state = {"cash": 100090., "initial_cash": 100000., "positions": [], "pending": [],
        "equity_curve": [{"date": "2026-10-26", "equity": 100000.}, {"date": "2026-10-27", "equity": 100090.}],
        "closed": [{"ticker": "AAA", "entry_date": "2026-10-26", "exit_date": "2026-10-27",
            "entry": 100., "exit": 109., "shares": 10, "pnl_usd": 90., "gross_pnl": 100.,
            "friction_cost": 10., "net_r": 1.8, "r_multiple": 1.8}]}
    report = evaluation(book, state=state)
    assert report["account"]["pnl_net"] == 90.0
    assert report["account"]["pnl_gross"] == 100.0
    assert report["account"]["friction_total"] == 10.0
    assert report["account"]["account_kind"] == "paper"
    assert report["account"]["profitability_established"] is False
    state["closed"][0]["friction_cost"] = 11.
    with pytest.raises(ValueError, match="cost|identity|friction"): evaluation(book, state=state)


def test_future_independent_outcome_cannot_contaminate_present_evaluation(tmp_path):
    book = ledger(tmp_path)
    outcome = {"opportunity_id": row(book)["opportunity_id"], "outcome_as_of": "2026-11-06T21:00:00+00:00",
               "horizon_complete": True, "method": "frozen-policy-v2", "policy_hash": "b" * 64,
               "tradeable": True, "eligible_at_signal": True, "net_return": .05, "evidence_ids": ["independent_prices_1"]}
    with pytest.raises(ValueError, match="future|as.of"):
        evaluation(book, outcomes=[outcome])


def test_legacy_watch_builder_includes_watchlist_and_account_holdings_without_picks(tmp_path):
    from trading_system.triggers import watch_from_result
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result()))
    state = {"positions": [{"ticker": "HELD", "shares": 20, "entry_price": 50.0,
        "initial_stop": 47.0, "stop": 47.0}], "pending": [], "closed": []}
    watch = watch_from_result(path, account_state=state)
    assert {item["ticker"] for item in watch} == {"AAA", "HELD"}
    assert next(item for item in watch if item["ticker"] == "AAA")["role"] == "watch"
    assert next(item for item in watch if item["ticker"] == "HELD")["role"] == "held"


def test_explicit_held_trigger_never_emits_entry(monkeypatch):
    from trading_system.triggers import monitor_once
    monkeypatch.setattr("trading_system.pipeline.quote_with_fallback", lambda provider, ticker:
        {"price": 100.0, "ts": NOW, "kind": "realtime", "stale": False})
    watch = [{"ticker": "AAA", "entry": 100., "stop": 95., "protect": 110., "role": "held"}]
    assert monitor_once(watch, object()) == []


def test_matured_independent_cohort_reports_capture_and_actual_missed_gate(tmp_path):
    book = ledger(tmp_path)
    book.requalify(row(book)["opportunity_id"], run_id="coverage-recheck", observed_at=LATER,
        accepted=False, reasons=["research_budget_exhausted"], risk_evidence_ids=["risk_coverage"],
        account_snapshot=account())
    outcome = {"opportunity_id": row(book)["opportunity_id"], "outcome_as_of": "2026-11-06T21:00:00+00:00",
        "horizon_complete": True, "method": "frozen-policy-v2", "policy_hash": "b" * 64,
        "tradeable": True, "eligible_at_signal": True, "net_return": .05, "entry_price": 100., "exit_price": 105.,
        "cost_usd": 0., "evidence_ids": ["independent_prices_1"]}
    report = evaluation(book, outcomes=[outcome], as_of="2026-11-07T00:00:00+00:00")
    assert report["opportunities"]["labeled_tradeable"] == 1
    assert report["opportunities"]["capture_rate"] == 0.0
    assert report["opportunities"]["missed"][0]["reasons"] == ["research_budget_exhausted"]
    assert report["opportunities"]["scope"] == "recorded_candidate_cohort"


def test_valuation_date_can_precede_research_date_but_explicit_binding_must_match(tmp_path):
    book = ledger(tmp_path)
    prior_close = {**account(), "known_at": "2026-10-26"}
    oid = row(book)["opportunity_id"]
    with pytest.raises(ValueError, match="valuation|assessment|date"):
        book.requalify(oid, run_id="recheck-date-error", observed_at=LATER,
            accepted=True, reasons=[], risk_evidence_ids=["risk_1"],
            account_snapshot=prior_close, plan=plan(), assessment_date="2026-10-27")
    book.requalify(oid, run_id="recheck-date-valid", observed_at=LATER,
        accepted=True, reasons=[], risk_evidence_ids=["risk_1"],
        account_snapshot=prior_close, plan=plan(), assessment_date="2026-10-26")
    intent = book.snapshot()["intents"][0]
    assert intent["valuation_date"] == "2026-10-26"
    assert intent["requires_execution_recheck"] is True
