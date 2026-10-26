"""Independent v2 sealed-IO replay contracts; all data is SYNTHETIC_TEST.

No provider, model, broker or network operation is permitted. Rehashed semantic
mutations must fail even when every serialized reference remains internally valid.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.llm.deepseek_required import MODEL, RELEASE, SEARCH_MODE, build_chat_request
from trading_system.llm.request_budget import BudgetGuard, LIMITS

ADMITTED = "2026-10-27T14:00:00+00:00"
START = "2026-10-27T14:00:01+00:00"
FINISH = "2026-10-27T14:00:02+00:00"


def h(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def native_record(*, kind="search", call_id="1" * 32, guard=None, query="SYNTHETIC_TEST issuer"):
    guard = guard or BudgetGuard(**LIMITS, clock=lambda: 0.)
    body = {"query": query, "count": 3, "need_content": True} if kind == "search" else {"url": "https://issuer.example/release"}
    reservation = guard.reserve(kind, call_id, body)
    guard.settle(call_id, None)
    identity = {"url": "https://issuer.example/release", "title": "SYNTHETIC_TEST issuer release",
        "content": "SYNTHETIC_TEST source content only; no actual company or trading claim.",
        "published": "2026-10-26T14:00:00+00:00" if kind == "search" else None}
    document = {"id": "ds_" + h(identity)[:24], **identity, "source": "deepseek_" + kind,
        "fetched_at": "2026-10-27T14:00:01.500000+00:00",
        "content_hash": hashlib.sha256(identity["content"].encode()).hexdigest()}
    request = {"schema": "tiger.deepseek-" + kind + "-request.v1", "call_id": call_id,
        "stage": "search.collect", "provider": "deepseek", "method": "POST",
        "endpoint": "https://api.deepseek.com/v1/tools/" + kind,
        "request": body, "request_sha256": h(body), "search_mode": SEARCH_MODE,
        "budget_reservation": reservation, "started_at_utc": START}
    proof = {"schema": "tiger.deepseek-" + kind + ".v1", "call_id": call_id,
        "stage": "search.collect", "provider": "deepseek", "method": "POST",
        "search_mode": SEARCH_MODE, "request_sha256": h(body), "transport_response_sha256": "a" * 64,
        "budget_reservation": reservation, "budget_after": guard.snapshot(),
        "started_at_utc": START, "finished_at_utc": FINISH, "elapsed_ms": 1000.}
    if kind == "search":
        request["query_sha256"] = hashlib.sha256(query.encode()).hexdigest()
        proof.update(query_sha256=request["query_sha256"], result_count=1, usable_result_count=1,
            results=[document], response_sha256=h({"results": [document]}))
    else:
        proof.update(page=document, response_sha256=h({"page": document}))
    return {"method": "model.deepseek." + kind, "request": request, "proof": proof}


def client(records):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    execution = SimpleNamespace(run_date=date(2026, 10, 27),
        policy=SimpleNamespace(market_timezone="America/New_York"), admitted_at_utc=ADMITTED)
    value = module.ReplayClient(execution, {"search.collect": records})
    value.set_stage("search.collect")
    return value


def test_pure_replay_returns_exact_recorded_native_source_without_network():
    record = native_record()
    replay = client([record])
    assert replay.search("SYNTHETIC_TEST issuer", count=3, need_content=True) == record["proof"]["results"]
    replay.finish_stage()
    assert replay.budget.snapshot() == record["proof"]["budget_after"]


@pytest.mark.parametrize("tamper", ["query", "document_body", "document_identity", "budget", "usage", "future_clock", "extra_field"])
def test_rehashed_native_mutations_are_rejected_by_actual_protocol(tamper):
    record = native_record()
    if tamper == "query":
        record["request"]["request"]["query"] = "UNBOUND_TOPIC"
        record["request"]["request_sha256"] = h(record["request"]["request"])
        record["proof"]["request_sha256"] = record["request"]["request_sha256"]
    elif tamper == "document_body":
        record["proof"]["results"][0]["content"] = "SYNTHETIC_TEST unbound changed source body"
        record["proof"]["response_sha256"] = h({"results": record["proof"]["results"]})
    elif tamper == "document_identity": record["proof"]["results"][0]["id"] = "ds_invented"
    elif tamper == "budget": record["proof"]["budget_after"]["counts"]["search"] = 0
    elif tamper == "usage": record["proof"]["budget_after"]["actual_tokens"] = 50
    elif tamper == "future_clock": record["proof"]["finished_at_utc"] = "2026-10-27T13:59:00+00:00"
    else: record["request"]["Authorization"] = "SYNTHETIC_TEST forbidden extra field"
    with pytest.raises(EvidenceInvalid): client([record]).search("SYNTHETIC_TEST issuer", count=3, need_content=True)


def test_replay_rejects_unconsumed_or_missing_calls():
    replay = client([native_record()])
    with pytest.raises(EvidenceInvalid): replay.finish_stage()
    replay = client([])
    with pytest.raises(EvidenceInvalid): replay.search("SYNTHETIC_TEST issuer", count=3, need_content=True)


def test_native_fetch_cannot_acquire_invented_publication_date():
    record = native_record(kind="fetch")
    record["proof"]["page"]["published"] = "2026-10-27T12:00:00+00:00"
    identity = {key: record["proof"]["page"][key] for key in ("url", "title", "content", "published")}
    record["proof"]["page"]["id"] = "ds_" + h(identity)[:24]
    record["proof"]["response_sha256"] = h({"page": record["proof"]["page"]})
    with pytest.raises(EvidenceInvalid): client([record]).fetch("https://issuer.example/release")


def reasoning_record(stage, effort):
    """Explicit synthetic Chat evidence; never constructs a real API client."""
    schema = {"type": "object", "properties": {"status": {"const": "SYNTHETIC_TEST"}},
              "required": ["status"], "additionalProperties": False}
    kwargs = {"system": "SYNTHETIC_TEST system", "user": "SYNTHETIC_TEST input",
              "schema_hint": {}, "schema": schema, "max_tokens": 256, "temperature": .2}
    body = build_chat_request(**kwargs)
    body["reasoning_effort"] = effort
    identity = "2" * 32
    guard = BudgetGuard(**LIMITS, clock=lambda: 0.)
    reservation = guard.reserve("chat", identity, body)
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    guard.settle(identity, usage)
    parsed = {"status": "SYNTHETIC_TEST"}
    response = {"id": "SYNTHETIC_TEST-replay-response", "model": MODEL, "system_fingerprint": None,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
            "content": json.dumps(parsed, sort_keys=True, separators=(",", ":"))}}], "usage": usage}
    request = {"schema": "tiger.deepseek-request.v1", "call_id": identity, "stage": stage,
        "provider": "deepseek", "method": "POST", "endpoint": "https://api.deepseek.com/chat/completions",
        "request": body, "request_sha256": h(body), "output_schema": schema,
        "schema_sha256": h(schema), "release": RELEASE, "budget_reservation": reservation,
        "started_at_utc": START}
    proof = {"schema": "tiger.deepseek-call.v1", "call_id": identity, "stage": stage,
        "provider": "deepseek", "requested_model": MODEL, "response_id": response["id"], "model": MODEL,
        "release": RELEASE, "usage": usage, "finish_reason": "stop", "request_sha256": h(body),
        "transport_response_sha256": "a" * 64, "response_sha256": h(response),
        "schema_sha256": h(schema), "response": response, "parsed": parsed,
        "system_fingerprint": None, "search_mode": SEARCH_MODE, "budget_reservation": reservation,
        "budget_after": guard.snapshot(), "started_at_utc": START, "finished_at_utc": FINISH,
        "elapsed_ms": 1000.}
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    execution = SimpleNamespace(run_date=date(2026, 10, 27),
        policy=SimpleNamespace(market_timezone="America/New_York"), admitted_at_utc=ADMITTED)
    replay = module.ReplayClient(execution, {stage: [{"method": "model.deepseek.chat",
                                                    "request": request, "proof": proof}]})
    replay.set_stage(stage)
    return replay, kwargs, proof


def test_offline_clean_replay_uses_exact_low_effort_request_and_settled_budget():
    replay, kwargs, proof = reasoning_record("clean.llm_semantic", "low")
    assert replay.complete_json(**kwargs, reasoning_effort="low") == {"status": "SYNTHETIC_TEST"}
    replay.finish()
    assert replay.budget.snapshot() == proof["budget_after"]


@pytest.mark.parametrize("stage,effort", [("clean.llm_semantic", "high"),
    ("tech.risk", "low"), ("decision.debate", "low")])
def test_offline_replay_rejects_effort_outside_the_same_production_stage_policy(stage, effort):
    replay, kwargs, _ = reasoning_record(stage, effort)
    with pytest.raises(EvidenceInvalid):
        replay.complete_json(**kwargs, reasoning_effort=effort)


def test_offline_other_stage_keeps_the_default_high_effort_request():
    replay, kwargs, _ = reasoning_record("decision.debate", "high")
    assert replay.complete_json(**kwargs) == {"status": "SYNTHETIC_TEST"}
    replay.finish()


def complete_fixture(tmp_path, monkeypatch, *, held=False, empty_candidates=False,
                     optional_missing=False, old_cohort=False, paper_fallback=False, **options):
    """Run the actual 43 stages, then expose only their sealed local evidence."""
    from pathlib import Path
    import shutil
    import main
    from test_short_term_core_v2 import upgrade_fixture
    from trading_system.ledger_io import read_json_strict, write_json_atomic
    execution, root, args, provider, denied, attempts = upgrade_fixture(tmp_path, monkeypatch, **options)
    if paper_fallback:
        from dataclasses import replace
        from trading_system.providers.tencent import TencentProvider
        execution.policy = replace(execution.policy, provider_names=("yahoo", "tencent"))
        original_history = provider.ohlcv
        def unavailable_in_paper(symbol, days=400):
            if execution._active is not None and execution._active.spec.name in {"paper.step", "business.evaluate"}:
                raise RuntimeError("SYNTHETIC_TEST ordinary primary source unavailable")
            return original_history(symbol, days)
        def unavailable_quote(symbol):
            raise RuntimeError("SYNTHETIC_TEST ordinary primary quote unavailable")
        monkeypatch.setattr(provider, "ohlcv", unavailable_in_paper)
        monkeypatch.setattr(provider, "quote", unavailable_quote)
        monkeypatch.setattr(TencentProvider, "available", staticmethod(lambda: True))
        monkeypatch.setattr(TencentProvider, "ohlcv", lambda self, symbol, days=400: original_history(symbol, days))
        monkeypatch.setattr(TencentProvider, "ohlcv_batch", lambda self, symbols, days=400: {})
        monkeypatch.setattr(TencentProvider, "quote", lambda self, symbol: {
            "price": 100., "kind": "realtime", "ts": "2026-10-20T20:06:00+00:00"})
    if old_cohort:
        from trading_system.data_models import PipelineResult, StockCandidate, TradePick
        from trading_system.execution_policy import portfolio_snapshot
        from trading_system.opportunity_lifecycle import OpportunityLedger
        from trading_system.workflow_v2 import execution_policy_identity
        day = "2026-10-08"
        history = provider.ohlcv("AAA", 420)
        price = float(history.loc[day, "Close"])
        state = read_json_strict(execution.paths.sim_path)
        snapshot = portfolio_snapshot(state, {}, day)
        setup = {"schema": "tiger.entry-setup.v2", "symbol": "AAA", "known_at": day,
            "entry_template": "A", "entry_price": price, "stop_price": price * .95,
            "key_level": price, "mrs_star": 7., "shs": 8., "tss_final": 8.,
            "in_main": True, "in_sub": False, "chain_hot": True, "breadth": 80.,
            "allow_new_positions": True, "shock": False, "risk_group": "SYNTHETIC_TEST",
            "sector": "XLK", "size_ratio": 1., "signal_adv_usd": 500_000_000., "execution_mode": "paper"}
        budget = {"schema": "tiger.entry-budget.v2", "execution_policy": "whitepaper-execution-v2",
            "known_at": day, "snapshot_hash": snapshot["state_hash"], "gross_cap": .7,
            "single_cap": .2, "risk_pct": .008, "group_risk_pct": .03, "portfolio_risk_pct": .03,
            "max_positions": 10, "source_snapshot": snapshot, "source_account_state": state}
        pick = TradePick("AAA", 8., 4.48, "A", price, price * .95, 100, 100 * price / 100_000., 5 * price,
                         chain="SYNTHETIC_TEST", sector="XLK")
        previous = PipelineResult(day, "SYNTHETIC_TEST", action="BUY", picks=[pick],
            watchlist=[StockCandidate("AAA", price=price, key_level=price, stop_price=price * .95)],
            raw={"execution_decisions": [{"symbol": "AAA", "action": "buy"}],
                 "cohort_inputs": {"schema": "tiger.cohort-inputs.v2", "policy_id": "whitepaper-execution-v2",
                    "policy_hash": execution_policy_identity()["sha256"], "setups": {"AAA": setup},
                    "budget": budget, "portfolio_snapshot": snapshot, "portfolio_state": state}})
        ledger = OpportunityLedger(execution.paths.reports_dir / "opportunity_ledger.json")
        ledger.record_research(previous, run_id="SYNTHETIC_TEST_prior_cohort", observed_at=day + "T20:00:00+00:00",
                               expires_at="2026-10-09T20:00:00+00:00")
        ledger.save()
        (root / "account-before").mkdir()
        shutil.copy2(ledger.path, root / "account-before/opportunity_ledger.json")
    if held:
        state = read_json_strict(execution.paths.sim_path)
        state.update(cash=90_000., positions=[{"ticker": "AAA", "shares": 40,
            "entry_price": 90., "initial_stop": 85., "stop": 85.,
            "entry_date": "2026-10-19", "last_bar_date": "2026-10-19", "bars_held": 0,
            "risk_usd": 200., "initial_risk_usd": 200.,
            "risk_group": "XLK", "sector": "XLK", "chain": "ai_cloud", "friction_bps": 10.}])
        write_json_atomic(execution.paths.sim_path, state)
    if empty_candidates:
        from trading_system.agents.universe_scanner_agent import UniverseScannerAgent
        original = UniverseScannerAgent.execute
        def empty(scanner, context):
            original(scanner, context)
            context["watchlist"] = []
            return []
        monkeypatch.setattr(UniverseScannerAgent, "execute", empty)
    if optional_missing:
        from trading_system.agents.sector_agent import SectorAgent
        from trading_system.agents.tss_agent import TSSAgent
        original_sector, original_tss = SectorAgent.execute, TSSAgent.execute
        def missing_sector(agent, context):
            rows = original_sector(agent, context)
            for row in rows:
                row.breadth, row.rs_slope_q, row.r20 = float("nan"), float("nan"), float("nan")
            return rows
        def missing_candidate(agent, context):
            rows = original_tss(agent, context)
            for row in rows:
                row.atr_pct = float("nan")
            return rows
        monkeypatch.setattr(SectorAgent, "execute", missing_sector)
        monkeypatch.setattr(TSSAgent, "execute", missing_candidate)
    source = Path(__file__).resolve().parents[1]
    destination = root / "source" / "trading_system"
    destination.mkdir(parents=True)
    for name in ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py"):
        shutil.copy2(source / "trading_system" / name, destination / name)
    output = main._daily(args, "yahoo", execution=execution)
    result = read_json_strict(output.report_paths["json"])
    admission = {"clock": {"run_date": execution.run_date.isoformat(),
        "market_timezone": execution.policy.market_timezone,
        "admitted_at_utc": execution.admitted_at_utc,
        "deadline_at_utc": (datetime.fromisoformat(execution.admitted_at_utc)
                            + timedelta(minutes=40)).isoformat()}}
    return execution, root, result, admission, denied, attempts


@pytest.mark.parametrize("phase", ["daily", "premarket", "intraday"])
def test_complete_semantic_replay_of_all43_stages_without_network(tmp_path, monkeypatch, phase):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    _, root, result, admission, denied, attempts = complete_fixture(tmp_path, monkeypatch, phase=phase)
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert denied == []
    assert any(path.endswith("fetch") for _, path in attempts)


@pytest.mark.parametrize("scenario", ["held_current_NAV", "held_premarket", "no_followup", "empty_targets"])
def test_complete_replay_preserves_account_and_empty_research_boundaries(tmp_path, monkeypatch, scenario):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    phase_options = ({"phase": "premarket", "admitted": "2026-10-20T13:00:00+00:00",
                      "ready": "2026-10-20T13:06:00+00:00"} if scenario == "held_premarket" else {})
    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch,
        held=scenario.startswith("held_"), empty_candidates=scenario == "empty_targets",
        followup=scenario != "no_followup", **phase_options)
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    research = result["raw"]["candidate_research"]
    if scenario.startswith("held_"):
        assert "AAA" in research["coverage"]["selected_symbols"]
        assert result["raw"]["portfolio_snapshot"]["nav"] != 100_000.
        if scenario == "held_premarket":
            assert result["raw"]["daily_bars_cutoff"] == "2026-10-19"
            assert all(row["as_of"] <= "2026-10-19" for row in research["market_evidence"])
    elif scenario == "empty_targets":
        assert research["theses"] == [] and research["plan"]["targets"] == []
    else:
        assert research["counters"]["fetch"] == 0
    assert denied == []


def test_typed_missing_scalars_retain_numeric_semantics_without_hydrating_frames():
    import math
    from trading_system.agent_evidence import normalize
    from trading_system.data_models import SectorScore, StockCandidate
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    for original in (SectorScore("XLK", 8., breadth=float("nan")), StockCandidate("AAA", atr_pct=float("nan"))):
        restored = module._restore(normalize(original))
        field = "breadth" if isinstance(restored, SectorScore) else "atr_pct"
        assert math.isnan(getattr(restored, field))
        assert normalize(restored) == normalize(original)


def test_complete43_stage_replay_preserves_optional_nan_dataclass_fields(tmp_path, monkeypatch):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch, optional_missing=True)
    assert any(row["breadth"] is None for row in result["sectors"])
    assert any(row["atr_pct"] is None for row in result["watchlist"])
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert denied == []


def test_tail_replay_matures_bound_old_cohort_without_making_it_actual_account_profit(tmp_path, monkeypatch):
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch, old_cohort=True)
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    report = result["raw"]["business_evaluation"]
    assert report["opportunities"]["matured_labels"] == 1
    assert report["account"]["n_closed"] == 0
    assert report["account"]["pnl_net"] == 0
    assert report["account"]["profitability_established"] is False
    assert denied == []


def test_coherent_paper_cash_invention_requires_canonical_account_step(tmp_path, monkeypatch):
    """All recorded copies agree; the actual policy must still reject the lie."""
    from trading_system import execution_policy
    from trading_system.agent_evidence import verify_events
    from trading_system.redline.execution import daily_stage_contract
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    original = execution_policy.step_paper_account
    def invent_cash(*args, **kwargs):
        report = original(*args, **kwargs)
        report["state"]["cash"] += 10_000.
        report["state"]["equity_curve"][-1]["equity"] += 10_000.
        report["equity"] += 10_000.
        return report
    with monkeypatch.context() as patched:
        patched.setattr(execution_policy, "step_paper_account", invent_cash)
        _, root, result, admission, denied, _ = complete_fixture(tmp_path, patched)
    assert result["raw"]["business_evaluation"]["account"]["cash"] == 110_000.
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid) as exc:
        module.verify_required_model_v2(root, result, admission)
    assert exc.value.code == "MODEL_REQUIRED_FAILED"
    assert denied == []


def test_coherent_legal_buy_cannot_drop_its_canonical_pending_plan(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    import main
    from test_root_integration_review_v2 import legal_buy_fixture
    from trading_system import execution_policy
    from trading_system.agent_evidence import verify_events
    from trading_system.ledger_io import read_json_strict
    from trading_system.redline.execution import daily_stage_contract
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    original = execution_policy.step_paper_account
    def drop_legal_plan(*args, **kwargs):
        report = original(*args, **kwargs)
        assert report["state"]["pending"]
        report["state"]["pending"] = []
        report["state"]["entry_plans"] = {}
        return report
    with monkeypatch.context() as patched:
        patched.setattr(execution_policy, "step_paper_account", drop_legal_plan)
        execution, root, args, denied, _ = legal_buy_fixture(tmp_path, patched)
        source = Path(__file__).resolve().parents[1] / "trading_system"
        destination = root / "source/trading_system"
        destination.mkdir(parents=True)
        for name in ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py"):
            shutil.copy2(source / name, destination / name)
        output = main._daily(args, "yahoo", execution=execution)
        result = read_json_strict(output.report_paths["json"])
    assert output.result.action == "BUY" and output.sim_state["pending"] == []
    admission = {"clock": {"run_date": "2026-10-20", "market_timezone": "America/New_York",
        "admitted_at_utc": "2026-10-20T13:45:00+00:00", "deadline_at_utc": "2026-10-20T14:25:00+00:00"}}
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid) as exc:
        module.verify_required_model_v2(root, result, admission)
    assert exc.value.stage == "paper.step"
    assert denied == []


def test_paper_replay_accepts_actual_available_probe_and_recorded_fallback_bars(tmp_path, monkeypatch):
    from trading_system.agent_evidence import read_snapshot
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    execution, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch,
        paper_fallback=True, old_cohort=True)
    end = next(row for row in execution.summary() if row["stage"] == "paper.step")
    calls = read_snapshot(root, end["output_ref"], run_id=root.name, stage="paper.step", role="output")["io_calls"]
    assert any(row["method"] == "provider.tencent.available" for row in calls)
    assert any(row["method"] == "provider.yahoo.ohlcv" and "error_ref" in row for row in calls)
    assert any(row["method"] == "provider.tencent.ohlcv" and "output_ref" in row for row in calls)
    for name, method in (("opportunity.lifecycle", "quote"), ("business.evaluate", "ohlcv")):
        end = next(row for row in execution.summary() if row["stage"] == name)
        nested = read_snapshot(root, end["output_ref"], run_id=root.name, stage=name, role="output")["io_calls"]
        assert any(row["method"] == "provider.tencent.available" for row in nested)
        assert any(row["method"] == "provider.tencent." + method and "output_ref" in row for row in nested)
    assert module.verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert denied == []


def rewrite_stage(root, stage, mutate):
    """Deliberately recompute all affected file/event hashes after corruption."""
    from trading_system.agent_evidence import (atomic_bytes, atomic_json, digest,
        file_ref, json_bytes, read_bytes, read_json, SNAPSHOT_SCHEMA)
    events = [json.loads(line) for line in read_bytes(root, "events.jsonl").splitlines()]
    end = next(row for row in events if row["stage"] == stage and row["kind"] == "end")
    old_input, old_output = end["input_ref"], end["output_ref"]
    input_blob = read_json(root, old_input["path"])
    output_blob = read_json(root, old_output["path"])
    mutate(input_blob["value"], output_blob["value"])
    atomic_json(root, old_input["path"], input_blob)
    atomic_json(root, old_output["path"], output_blob)
    replacements = [(old_input, file_ref(root, old_input["path"], SNAPSHOT_SCHEMA)),
                    (old_output, file_ref(root, old_output["path"], SNAPSHOT_SCHEMA))]
    previous = None
    for row in events:
        for key in ("input_ref", "output_ref", "previous_stage_ref"):
            for before, after in replacements:
                if row[key] == before:
                    row[key] = after
                    break
        row["previous_event_sha256"] = previous
        row["event_sha256"] = digest(json_bytes({key: val for key, val in row.items() if key != "event_sha256"}))
        previous = row["event_sha256"]
    atomic_bytes(root, "events.jsonl", b"".join(json_bytes(row) for row in events))


def rewrite_child(root, call, role, transform):
    from trading_system.agent_evidence import atomic_json, digest, file_ref, json_bytes, read_json, SNAPSHOT_SCHEMA
    ref = call[role + "_ref"]
    blob = read_json(root, ref["path"])
    changed = transform(blob["value"])
    if changed is not None:
        blob["value"] = changed
    # Equal clocks can intentionally share one content-addressed snapshot. A
    # mutation creates a new blob instead of corrupting the other observation.
    target = "evidence/" + digest(json_bytes(blob)) + ".json"
    atomic_json(root, target, blob)
    call[role + "_ref"] = file_ref(root, target, SNAPSHOT_SCHEMA)


@pytest.mark.parametrize("tamper", ["lifecycle_state", "invented_quote", "clock", "observation_summary",
    "current_NAV", "profits_coherent", "maturity", "missing_history", "invented_history",
    "paper_bar", "missing_paper_provider"])
def test_rehashed_tail_requires_actual_account_observations_and_pure_business_replay(tmp_path, monkeypatch, tamper):
    from trading_system.agent_evidence import verify_events
    from trading_system.ledger_io import write_json_atomic
    from trading_system.redline.execution import daily_stage_contract
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch,
        old_cohort=tamper in {"maturity", "missing_history", "invented_history"})
    if tamper == "paper_bar":
        def change(_, out):
            symbol = next(iter(out["value"]["bars"]))
            out["value"]["bars"][symbol]["fields"]["close"] += 100.
        rewrite_stage(root, "paper.step", change)
    elif tamper == "missing_paper_provider":
        rewrite_stage(root, "paper.step", lambda _, out: out.update(io_calls=[]))
    elif tamper == "lifecycle_state":
        rewrite_stage(root, "opportunity.lifecycle", lambda _, out:
            out["value"]["lifecycle"]["opportunities"][0].update(state="QUALIFIED"))
    elif tamper == "invented_quote":
        def change(_, out):
            call = next(row for row in out["io_calls"] if row["method"] == "opportunity.quote")
            invented = {"price": 999., "kind": "realtime", "ts": "2026-10-20T20:06:00+00:00"}
            rewrite_child(root, call, "output", lambda _: invented)
            symbol = next(iter(out["value"]["quotes"]))
            out["value"]["quotes"][symbol] = invented
        rewrite_stage(root, "opportunity.lifecycle", change)
    elif tamper == "clock":
        def change(_, out):
            call = next(row for row in out["io_calls"] if row["method"] == "clock.observe")
            rewrite_child(root, call, "output", lambda _: "2026-10-20T19:59:00+00:00")
        rewrite_stage(root, "opportunity.lifecycle", change)
    elif tamper == "observation_summary":
        result["raw"]["lifecycle_observation"]["alerts"] = [{"kind": "BUY", "ticker": "ZZZ"}]
    elif tamper == "current_NAV":
        result["raw"]["current_portfolio_snapshot"]["nav"] = 999_999.
    elif tamper == "profits_coherent":
        def change(_, out):
            evaluation = out["value"]["evaluation"]
            evaluation["account"]["profitability_established"] = True
            for call in out["io_calls"]:
                if call["method"] == "workflow.artifact.write":
                    def request(value):
                        if "account" in value["value"]:
                            value["value"]["account"]["profitability_established"] = True
                    def response(value):
                        if "account" in value:
                            value["account"]["profitability_established"] = True
                    rewrite_child(root, call, "input", request)
                    rewrite_child(root, call, "output", response)
            result["raw"]["business_evaluation"] = deepcopy(evaluation)
            write_json_atomic(root / "work/reports/business-evaluation.json", evaluation)
        rewrite_stage(root, "business.evaluate", change)
    elif tamper == "maturity":
        rewrite_stage(root, "business.evaluate", lambda _, out: out["value"]["outcomes"][0].update(net_return=1.))
    elif tamper == "missing_history":
        def change(_, out):
            out["io_calls"] = [call for call in out["io_calls"] if call["method"] != "evaluation.history"]
            for index, call in enumerate(out["io_calls"], 1): call["index"] = index
        rewrite_stage(root, "business.evaluate", change)
    else:
        def change(_, out):
            call = next(row for row in out["io_calls"] if row["method"] == "evaluation.history")
            def frame(value):
                column = value["columns"].index("Close")
                value["values"][-1][column] += 100.
            rewrite_child(root, call, "output", frame)
        rewrite_stage(root, "business.evaluate", change)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid) as exc:
        module.verify_required_model_v2(root, result, admission)
    assert exc.value.code == "MODEL_REQUIRED_FAILED"
    assert denied == []


@pytest.mark.parametrize("tamper", ["risk_assessment", "research_context", "research_plan",
    "source_index", "research_counters", "research_thesis", "numeric_budget", "native_IO_order",
    "native_IO_omission", "prompt_documents", "reasoning_effort", "final_action", "final_picks", "final_model", "policy_source"])
def test_rehashed_semantic_mutations_fail_even_with_valid_event_chain(tmp_path, monkeypatch, tamper):
    from trading_system.agent_evidence import (atomic_json, file_ref, read_json,
        SNAPSHOT_SCHEMA, verify_events)
    from trading_system.redline.execution import daily_stage_contract
    module = importlib.import_module("trading_system.llm.required_proof_v2")
    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch)
    if tamper == "risk_assessment":
        rewrite_stage(root, "tech.risk", lambda _, out: out["value"]["fields"].update(assessment="SYNTHETIC_TEST rehashed invented assessment"))
    elif tamper == "research_context":
        rewrite_stage(root, "research.candidates", lambda inp, _: inp["context"].update(account_usd=1.))
    elif tamper == "research_plan":
        rewrite_stage(root, "research.candidates", lambda _, out: out["value"]["plan"].update(requested_symbols=["ZZZ"]))
    elif tamper == "source_index":
        rewrite_stage(root, "research.candidates", lambda _, out: out["value"]["source_index"].clear())
    elif tamper == "research_counters":
        rewrite_stage(root, "research.candidates", lambda _, out: out["value"]["counters"].update(search=0))
    elif tamper == "research_thesis":
        rewrite_stage(root, "research.candidates", lambda _, out: out["value"]["theses"][0].update(status="sufficient"))
    elif tamper == "numeric_budget":
        rewrite_stage(root, "layer4.risk", lambda _, out: out["value"]["entry_budget"].update(risk_pct=.5))
    elif tamper == "native_IO_order":
        def reverse(_, out):
            out["io_calls"].reverse()
            for index, call in enumerate(out["io_calls"], 1):
                call["index"] = index
        rewrite_stage(root, "search.collect", reverse)
    elif tamper == "native_IO_omission":
        rewrite_stage(root, "search.collect", lambda _, out: out["io_calls"].pop())
    elif tamper in {"prompt_documents", "reasoning_effort"}:
        def change(_, out):
            call = out["io_calls"][0]
            request_blob = read_json(root, call["input_ref"]["path"])
            request = request_blob["value"]
            if tamper == "reasoning_effort":
                assert request["request"]["reasoning_effort"] == "low"
                request["request"]["reasoning_effort"] = "high"
            else:
                request["request"]["messages"][1]["content"] = request["request"]["messages"][1]["content"].replace('"documents":[', '"documents_omitted":[', 1)
            request["request_sha256"] = h(request["request"])
            atomic_json(root, call["input_ref"]["path"], request_blob)
            call["input_ref"] = file_ref(root, call["input_ref"]["path"], SNAPSHOT_SCHEMA)
            proof_blob = read_json(root, call["output_ref"]["path"])
            proof_blob["value"]["request_sha256"] = request["request_sha256"]
            atomic_json(root, call["output_ref"]["path"], proof_blob)
            call["output_ref"] = file_ref(root, call["output_ref"]["path"], SNAPSHOT_SCHEMA)
        rewrite_stage(root, "clean.llm_semantic", change)
    elif tamper == "final_action": result["action"] = "BUY"
    elif tamper == "final_picks": result["picks"] = [{"ticker": "ZZZ", "shares": 100}]
    elif tamper == "final_model": result["raw"]["model"]["all_required_completed"] = False
    else:
        (root / "source" / "trading_system" / "execution_policy.py").write_text("SYNTHETIC_TEST changed policy source\n")
    # This assertion distinguishes semantic replay from ordinary hash rejection.
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid) as exc:
        module.verify_required_model_v2(root, result, admission)
    assert exc.value.code == "MODEL_REQUIRED_FAILED"
    assert denied == []
