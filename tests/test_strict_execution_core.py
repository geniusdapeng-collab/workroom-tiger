"""Strict core boundaries: durable order, actual IO, immutable observations and failures."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from trading_system.agent_evidence import EvidenceStore, read_snapshot, verify_events
from trading_system.redline.execution import (
    EvidenceWriteError, ExecutionContext, ExecutionContractError, ExecutionPolicy,
    RuntimePaths, daily_stage_contract, daily_stage_specs, record_provider,
)


def make_execution(tmp_path):
    root = (tmp_path / "run").resolve()
    work = root / "work"
    reports = work / "reports"
    reports.mkdir(parents=True)
    paths = RuntimePaths(
        work_dir=work, reports_dir=reports,
        journal_path=reports / "journal.json", sim_path=reports / "sim_portfolio.json",
        calibration_path=reports / "calibration_samples.json",
        options_history_dir=reports / "options_hist",
        governance_path=reports / "governance_events.jsonl",
        proposals_dir=reports / "review_proposals",
        calendar_override_path=work / "events_calendar.json",
    )
    execution = ExecutionContext(run_id="strict-test", run_date=date(2026, 10, 20),
                                 paths=paths, policy=ExecutionPolicy("public-paper-v1"),
                                 store=EvidenceStore(root, "strict-test"))
    return execution, root


def advance_to(execution, target):
    for spec in daily_stage_specs():
        if spec.name == target:
            return
        with execution.step(spec.name, {"test_input": spec.name}) as scope:
            scope.output({"test_output": spec.name})
    raise AssertionError(target)


def test_exact_39_stages_emit_78_events_and_verify(tmp_path):
    execution, root = make_execution(tmp_path)
    for spec in daily_stage_specs():
        with execution.step(spec.name, {"stage": spec.name}) as scope:
            if spec.allow_passthrough:
                scope.passthrough("Disabled by the trusted public profile")
            scope.output({"actual": spec.name})
    execution.assert_complete()
    events = [json.loads(row) for row in (root / "events.jsonl").read_text().splitlines()]
    assert len(events) == 78
    assert [row["stage"] for row in events[::2]] == [s.name for s in daily_stage_specs()]
    assert [row["sequence"] for row in events] == list(range(1, 79))
    assert events[2]["previous_stage_ref"] == events[1]["output_ref"]
    verify_events(root, execution.run_id, daily_stage_contract())


def test_input_and_output_are_frozen_at_call_time(tmp_path):
    execution, root = make_execution(tmp_path)
    inputs = {"values": [1]}
    output = {"values": [2]}
    with execution.step("daily.preflight", inputs) as stage:
        inputs["values"].append(3)
        stage.output(output)
        output["values"].append(4)
    record = execution.summary()[0]
    first = read_snapshot(root, record["input_ref"], run_id=execution.run_id,
                          stage="daily.preflight", role="input")
    second = read_snapshot(root, record["output_ref"], run_id=execution.run_id,
                           stage="daily.preflight", role="output")
    assert first == {"values": [1]}
    assert second["value"] == {"values": [2]}


@pytest.mark.parametrize("violation", ["wrong_first", "overlap", "duplicate_output", "illegal_passthrough",
                                        "missing_output", "exception_after_output", "unfinished_io"])
def test_violations_stop_pipeline_even_when_inner_exception_is_caught(tmp_path, violation):
    execution, _ = make_execution(tmp_path)
    if violation == "wrong_first":
        with pytest.raises(ExecutionContractError):
            with execution.step("state.reset", {}):
                pytest.fail("must not enter")
    else:
        with pytest.raises((ExecutionContractError, ValueError)):
            with execution.step("daily.preflight", {}) as stage:
                if violation == "missing_output":
                    return
                if violation == "unfinished_io":
                    execution.begin_io("provider.yahoo.ohlcv", {"ticker": "AAPL"})
                    stage.output({})
                elif violation == "exception_after_output":
                    stage.output({})
                    raise ValueError("business error after persisted output")
                else:
                    if violation == "duplicate_output":
                        stage.output({})
                    with pytest.raises(ExecutionContractError):
                        if violation == "overlap":
                            with execution.step("state.reset", {}):
                                pytest.fail("nested stage entered")
                        elif violation == "illegal_passthrough":
                            stage.passthrough("not allowed")
                        else:
                            stage.output({"second": True})
                    if stage.output_ref is None:
                        stage.output({})
    with pytest.raises(ExecutionContractError):
        with execution.step("daily.preflight" if violation == "wrong_first" else "state.reset", {}):
            pytest.fail("poisoned execution continued")


def test_io_snapshots_precede_call_and_preserve_none_vs_failure(tmp_path):
    execution, root = make_execution(tmp_path)
    values = [1]
    with execution.step("daily.preflight", {}) as stage:
        def operation():
            values.append(2)
            return None
        assert execution.call_io("provider.yahoo.ohlcv", {"values": values}, operation) is None
        with pytest.raises(ValueError):
            execution.call_io("provider.yahoo.quote", {"ticker": "AAPL"},
                              lambda: (_ for _ in ()).throw(ValueError("secret-provider-body")))
        stage.output({"done": True})
    value = read_snapshot(root, execution.summary()[0]["output_ref"],
                          run_id=execution.run_id, stage="daily.preflight", role="output")
    calls = value["io_calls"]
    assert [call["index"] for call in calls] == [1, 2]
    assert "output_ref" in calls[0] and "error_ref" in calls[1]
    read = lambda ref, role: read_snapshot(root, ref, run_id=execution.run_id,
                                          stage="daily.preflight", role=role)
    assert read(calls[0]["input_ref"], "input") == {"values": [1]}
    assert read(calls[0]["output_ref"], "output") is None
    assert read(calls[1]["error_ref"], "output")["error_type"] == "ValueError"
    assert "secret-provider-body" not in "".join(p.read_text() for p in (root / "evidence").glob("*.json"))


@pytest.mark.parametrize("failure_point", ["input", "begin", "io_input", "io_output", "output", "end"])
def test_storage_failure_is_terminal_and_not_a_business_fallback(tmp_path, failure_point, monkeypatch):
    execution, root = make_execution(tmp_path)
    snapshot = execution.store.snapshot
    append = execution.store.append_event
    counter = {"snapshot": 0}
    fail_at = {"input": 1, "io_input": 2, "io_output": 3, "output": 4}.get(failure_point)
    def broken_snapshot(**kwargs):
        counter["snapshot"] += 1
        if counter["snapshot"] == fail_at:
            raise OSError("simulated evidence storage failure")
        return snapshot(**kwargs)
    def broken_event(event):
        if event["kind"] == failure_point:
            raise OSError("simulated event fsync failure")
        return append(event)
    monkeypatch.setattr(execution.store, "snapshot", broken_snapshot)
    monkeypatch.setattr(execution.store, "append_event", broken_event)
    with pytest.raises(EvidenceWriteError):
        with execution.step("daily.preflight", {}) as stage:
            execution.call_io("provider.yahoo.ohlcv", {"ticker": "AAPL"}, lambda: [1])
            stage.output({})
    with pytest.raises(ExecutionContractError):
        with execution.step("state.reset", {}):
            pytest.fail("storage failure continued")
    assert all(row["status"] != "completed" for row in execution.summary())


def test_cross_thread_evidence_rejected_and_poisoned(tmp_path):
    execution, _ = make_execution(tmp_path)
    failures = []
    with pytest.raises(ExecutionContractError):
        with execution.step("daily.preflight", {}) as stage:
            def worker():
                try:
                    execution.begin_io("search.google_news.search", {})
                except ExecutionContractError as error:
                    failures.append(error)
            thread = Thread(target=worker)
            thread.start(); thread.join()
            stage.output({})
    assert len(failures) == 1


def test_public_policy_rejects_paid_and_synthetic_before_provider_work(tmp_path):
    execution, _ = make_execution(tmp_path)
    for name, synthetic in [("agentgw", False), ("ifind_gw", False), ("tiingo", False), ("yahoo", True)]:
        with pytest.raises(ExecutionContractError):
            record_provider(SimpleNamespace(name=name, synthetic=synthetic), execution)
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy("bad", provider_names=("yahoo", "agentgw"))
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy("bad", search_source_names=("kimi_search",))


def test_disabled_llm_records_attempt_without_calling_any_transport(tmp_path, monkeypatch):
    from trading_system.llm.client import DisabledLLMClient
    from trading_system.redline import LLMUnavailable
    execution, _ = make_execution(tmp_path)
    advance_to(execution, "clean.llm_semantic")
    with execution.step("clean.llm_semantic", {}) as stage:
        client = DisabledLLMClient(execution)
        with pytest.raises(LLMUnavailable):
            client.complete_json(system="test", user="untrusted document", schema_hint={})
        stage.passthrough("trusted model disabled")
        stage.output(client.describe())
    assert client.describe()["attempts"] == 1


def test_stooq_strict_csv_does_not_solve_or_verify_access_challenge(monkeypatch):
    from trading_system.providers.stooq import _StooqSession
    session = _StooqSession(allow_challenge=False)
    monkeypatch.setattr(session, "_verify", lambda *a, **k: pytest.fail("challenge verify executed"))
    monkeypatch.setattr(session, "_solve_pow", lambda *a, **k: pytest.fail("proof of work executed"))
    monkeypatch.setattr(session, "_request", lambda *a, **k:
                        "Date,Open,High,Low,Close,Volume\n2026-10-20,1,2,1,2,100")
    assert session.get_text("https://stooq.com/q/d/l/?s=aapl.us&i=d").startswith("Date,")
    monkeypatch.setattr(session, "_request", lambda *a, **k: 'const c="YQ==",d=3')
    with pytest.raises(Exception):
        session.get_text("https://stooq.com/q/d/l/?s=aapl.us&i=d")


@pytest.mark.parametrize("finish_during_cleanup", [True, False])
def test_search_late_results_are_not_consumed_and_unsettled_workers_fail(tmp_path, finish_during_cleanup):
    from trading_system.search.hub import SearchHub, SearchWorkersUnsettled
    from trading_system.search.models import RawDocument
    execution, root = make_execution(tmp_path)
    advance_to(execution, "search.collect")
    release = Event()
    started = Event()
    class Source:
        name = "google_news"
        def search(self, query, limit):
            started.set()
            release.wait(3)
            return [RawDocument("late", "google_news", "late", "https://example.test/late", "late", "2026-10-20")]
    hub = SearchHub([Source()], execution=execution, budget=0.01,
                    cleanup_budget=0.2 if finish_during_cleanup else 0.01,
                    use_disk_cache=False)
    try:
        if finish_during_cleanup:
            with execution.step("search.collect", {}) as stage:
                batch = hub.search("query")
                assert started.is_set() and batch.docs == []
                release.set()
                cleanup = hub.settle_workers()
                assert cleanup["settled"] and not cleanup["late_results_consumed"]
                stage.output({"batch": batch, "cleanup": cleanup})
            assert batch.source_stats["google_news"]["outcome_observed"] is False
            assert batch.docs == []
        else:
            with pytest.raises(SearchWorkersUnsettled):
                with execution.step("search.collect", {}) as stage:
                    batch = hub.search("query")
                    assert batch.docs == []
                    hub.settle_workers()
                    stage.output({})
            assert execution.summary()[-1]["status"] == "failed"
    finally:
        release.set()
        for future in hub._started_futures:
            future.result(timeout=3)


def test_search_no_workers_requires_no_cleanup_wait(tmp_path):
    from trading_system.search.hub import SearchHub
    execution, _ = make_execution(tmp_path)
    advance_to(execution, "search.collect")
    with execution.step("search.collect", {}) as stage:
        hub = SearchHub([], execution=execution, cleanup_budget=15, use_disk_cache=False)
        cleanup = hub.settle_workers()
        assert cleanup["started"] == 0 and cleanup["cleanup_elapsed_s"] < 0.1
        stage.output(cleanup)


def test_public_factories_skip_paid_probes_and_ignore_ambient_provider(tmp_path, monkeypatch):
    from trading_system.pipeline import _channel_chain
    from trading_system.providers import get_provider
    import trading_system.search.sources as sources
    execution, _ = make_execution(tmp_path)
    execution.policy = ExecutionPolicy("public-only-test", provider_names=("yahoo",))
    monkeypatch.setenv("TS_PROVIDER", "agentgw")
    with execution.step("daily.preflight", {}) as stage:
        assert get_provider(None, execution=execution).name == "yahoo"
        assert _channel_chain(execution) == []
        monkeypatch.setattr(sources, "KimiSearchSource", lambda *a, **k: pytest.fail("paid source constructed"))
        actual = sources.default_sources(allowed_names=execution.policy.search_source_names)
        assert {source.name for source in actual} == set(execution.policy.search_source_names)
        stage.output({"sources": [source.name for source in actual]})
