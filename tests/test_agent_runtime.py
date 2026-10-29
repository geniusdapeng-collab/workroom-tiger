"""Receipt verification, immutable account history and atomic publication faults."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
from trading_system import agent_evidence as evidence
from trading_system import agent_runtime as runtime
from trading_system.agent_evidence import (
    EvidenceInvalid, atomic_json, digest, file_ref, json_bytes, read_bytes, read_json,
)
from trading_system.agent_runner import execute, submit


@pytest.fixture(scope="module")
def completed_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("completed-offline-fixture")
    source = fixture_source(root / "source")
    state = root / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run_id = accepted["run_id"]
    observed = wait_terminal(state, run_id)
    assert observed["status"] == "completed_degraded", observed
    assert runtime.verify_run(state, run_id, current_source=source)["verification"]["ok"]
    return state, run_id, source


@pytest.fixture
def completed(completed_fixture, tmp_path):
    original, run_id, source = completed_fixture
    state = tmp_path / "state"
    shutil.copytree(original, state)
    return state, state / "runs" / run_id, source


def unpublished(completed):
    state, run, source = completed
    account = state / "accounts" / "public-usd"
    (account / "current.json").unlink()
    shutil.rmtree(account / "versions" / run.name)
    for name in ("account-version.json", "receipt.json", "publication-durability.json"):
        (run / name).unlink()
    return state, run, source


def artifacts(run):
    rows = read_json(run, "engine-result.json")["artifacts"]
    return [file_ref(run, row["path"], row["schema"]) for row in rows]


def result_path(run):
    clock = read_json(run, "admission.json")["clock"]
    return "work/reports/result_" + clock["run_date"].replace("-", "") + ".json"


def rebound_receipt(state, run):
    """Rehash temp evidence to test semantic checks beyond a byte-hash mismatch."""
    receipt = read_json(run, "receipt.json")
    engine = read_json(run, "engine-result.json")
    engine["artifacts"] = artifacts(run)
    atomic_json(run, "engine-result.json", engine)
    receipt["artifacts"] = engine["artifacts"]
    for name, ref in list(receipt.items()):
        if isinstance(ref, dict) and set(ref) == {"path", "sha256", "bytes", "schema"}:
            receipt[name] = file_ref(run, ref["path"], ref["schema"])
    atomic_json(run, "receipt.json", receipt)
    pointer = runtime.pointer_read(state)
    pointer["receipt_sha256"] = digest(read_bytes(run, "receipt.json"))
    atomic_json(state / "accounts/public-usd", "current.json", pointer)
    # This post-publication observation is not commitment authority.
    (run / "publication-durability.json").unlink(missing_ok=True)


@pytest.mark.parametrize("field,value", [
    ("currency_policy", []), ("accounting", []), ("freshness", {}),
    ("source_lineage", []), ("redline", [None]), ("data_coverage", "unknown"),
])
def test_rehashed_malformed_business_metadata_fails_without_protocol_crash(completed, field, value):
    state, run, _ = completed
    path = result_path(run)
    result = read_json(run, path)
    result["raw"][field] = value
    atomic_json(run, path, result)
    rebound_receipt(state, run)
    response = execute("receipt", {"run_id": run.name}, mode="readonly", state=state)
    assert not response["operation_ok"] and response["status"] == "invalid"
    assert response["summary"] is None and response["verification"] is None
    assert response["data"]["observed_status"] == "completed_degraded"
    assert response["error"]["code"] == "RESULT_INVALID"


@pytest.mark.parametrize("field,value", [
    ("positions", [None]), ("equity_curve", [None]), ("ops_log", [None]),
    ("cash", 10 ** 400),
])
def test_invalid_sim_shapes_are_explicit_evidence_errors(completed, field, value):
    _state, run, _ = completed
    state = read_json(run, "work/reports/sim_portfolio.json")
    state[field] = value
    atomic_json(run, "work/reports/sim_portfolio.json", state)
    with pytest.raises(EvidenceInvalid):
        runtime.business_summary(run, read_json(run, "admission.json"), artifacts(run))


@pytest.mark.parametrize("field,value", [
    ("source_tree_sha256", "0" * 64),
    ("stage_count", 39.0), ("stage_count", True),
    ("module_origins", {}), ("success", 1),
])
def test_rehashed_engine_identity_is_still_validated(completed, field, value):
    state, run, _ = completed
    engine = read_json(run, "engine-result.json")
    engine[field] = value
    atomic_json(run, "engine-result.json", engine)
    rebound_receipt(state, run)
    with pytest.raises(EvidenceInvalid):
        runtime.verify_run(state, run.name)


@pytest.mark.parametrize("field,value", [
    ("engine_exit", False), ("guardian_exit", 0.0),
    ("group_cleanup_before_reap", False), ("run_id", "run-" + "0" * 32),
])
def test_process_exit_is_bound_and_semantically_checked(completed, field, value):
    state, run, _ = completed
    process = read_json(run, "process-exit.json")
    process[field] = value
    atomic_json(run, "process-exit.json", process)
    rebound_receipt(state, run)
    with pytest.raises(EvidenceInvalid):
        runtime.verify_run(state, run.name)


def test_missing_process_exit_cannot_reuse_saved_verified_flag(completed):
    state, run, _ = completed
    (run / "process-exit.json").unlink()
    with pytest.raises(EvidenceInvalid, match="missing"):
        runtime.verify_run(state, run.name)


@pytest.mark.parametrize("extension", [".md", ".html"])
def test_empty_but_rehashed_report_is_not_a_finished_report(completed, extension):
    _state, run, _ = completed
    day = read_json(run, "admission.json")["clock"]["run_date"].replace("-", "")
    (run / f"work/reports/日报_{day}{extension}").write_bytes(b"")
    with pytest.raises(EvidenceInvalid, match="report"):
        runtime.business_summary(run, read_json(run, "admission.json"), artifacts(run))


def test_stale_date_cannot_claim_zero_lag(completed):
    _state, run, _ = completed
    path = result_path(run)
    result = read_json(run, path)
    result["raw"]["freshness"] = {"benchmark_last_bar": "2000-01-03", "benchmark_lag_days": 0}
    atomic_json(run, path, result)
    with pytest.raises(EvidenceInvalid, match="lag"):
        runtime.business_summary(run, read_json(run, "admission.json"), artifacts(run))


@pytest.mark.parametrize("reported_lag", [-7, 0])
def test_future_benchmark_never_passes_final_business_verification(completed, reported_lag):
    _state, run, _ = completed
    admission = read_json(run, "admission.json")
    future = datetime.fromisoformat(admission["clock"]["run_date"]) + timedelta(days=7)
    path = result_path(run)
    result = read_json(run, path)
    result["raw"]["freshness"] = {
        "benchmark_last_bar": future.date().isoformat(), "benchmark_lag_days": reported_lag,
    }
    atomic_json(run, path, result)
    with pytest.raises(EvidenceInvalid, match="freshness"):
        runtime.business_summary(run, admission, artifacts(run))


@pytest.mark.parametrize("field,value", [
    ("source", "frozen_overlay"), ("source_path", "source/not-the-calendar.json"),
    ("work_path", "work/other.json"), ("sha256", "0" * 64),
    ("unrecognized", True),
])
def test_calendar_metadata_is_recomputed_from_frozen_source(completed, field, value):
    state, run, _ = completed
    record = read_json(run, "calendar-input.json")
    record[field] = value
    atomic_json(run, "calendar-input.json", record)
    # Rehashing a receipt cannot legitimize a different input source or path.
    rebound_receipt(state, run)
    with pytest.raises(EvidenceInvalid, match="Calendar"):
        runtime.verify_run(state, run.name)


def test_missing_account_file_and_unbound_report_are_rejected(completed):
    state, run, _ = completed
    (run / "work/reports/unbound.md").write_text("TEST_FIXTURE unbound")
    with pytest.raises(EvidenceInvalid, match="inventory"):
        runtime.verify_run(state, run.name)
    (run / "work/reports/unbound.md").unlink()
    version = state / "accounts/public-usd/versions" / run.name
    (version / "journal.json").unlink()
    with pytest.raises(EvidenceInvalid, match="missing"):
        runtime.verify_run(state, run.name)


@pytest.mark.parametrize("fault_path", [
    "account-version.json", "receipt.json", "current.json",
])
def test_failure_before_publication_keeps_account_uncommitted(completed, monkeypatch, fault_path):
    state, run, _ = unpublished(completed)
    original = runtime.atomic_json
    def fail(root, relative, value, **kwargs):
        if relative == fault_path:
            raise OSError("TEST_FIXTURE storage failure")
        return original(root, relative, value, **kwargs)
    monkeypatch.setattr(runtime, "atomic_json", fail)
    with pytest.raises(OSError):
        runtime.seal_and_publish(state, run)
    assert runtime.pointer_read(state) is None
    assert runtime.committed_pointer(state, run.name) is None


def test_fsync_error_after_visible_pointer_is_disclosed_on_later_read(completed, monkeypatch):
    state, run, _ = unpublished(completed)
    original = runtime.atomic_json
    def fail_after_replace(root, relative, value, **kwargs):
        result = original(root, relative, value, **kwargs)
        if relative == "current.json":
            raise OSError("TEST_FIXTURE directory fsync failed after replace")
        return result
    monkeypatch.setattr(runtime, "atomic_json", fail_after_replace)
    value = runtime.seal_and_publish(state, run)
    assert value["verification"]["ok"]
    assert any("fsync reported an error" in note for note in value["limitations"])
    later = runtime.verify_run(state, run.name)
    assert later["verification"]["ok"]
    assert any("fsync reported an error" in note for note in later["limitations"])


def test_publication_uses_prepared_verification_without_postcommit_replay(completed, monkeypatch):
    state, run, _ = unpublished(completed)
    calls = []
    original = runtime.verify_run

    def counted(*args, **kwargs):
        calls.append(kwargs.get("require_committed", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "verify_run", counted)
    value = runtime.seal_and_publish(state, run)
    assert calls == [False]
    assert value["verification"]["ok"]
    assert "published-account-pointer-readback" in value["verification"]["checks"]
    assert "prepared-unpublished-account" not in value["verification"]["checks"]
    assert runtime.committed_pointer(state, run.name) == value["pointer"]


def test_commit_survives_postpublication_status_failure_and_missing_durability_record(completed):
    state, run, _ = completed
    atomic_json(run, "status.json", {
        "schema": "tiger.status.v1", "run_id": run.name, "status": "running", "error": None,
    })
    (run / "publication-durability.json").unlink()
    value = execute("receipt", {"run_id": run.name}, mode="readonly", state=state)
    assert value["operation_ok"] and value["verification"]["ok"]
    assert any("confirmation is unavailable" in note for note in value["limitations"])
    assert read_json(run, "status.json")["status"] == "running"


def test_publication_checks_monotonic_deadline_after_temp_fsync(completed, monkeypatch):
    state, run, _ = unpublished(completed)
    original = runtime.atomic_json
    marker = {"entered": False}
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 11.0 if marker["entered"] else 1.0)
    def delayed(root, relative, value, **kwargs):
        if relative == "current.json":
            original_guard = kwargs["before_publish"]
            def at_replace():
                marker["entered"] = True
                original_guard()
            kwargs["before_publish"] = at_replace
        return original(root, relative, value, **kwargs)
    monkeypatch.setattr(runtime, "atomic_json", delayed)
    # All evidence preparation is allowed; the injected guard delay crosses the
    # cutoff immediately before the authoritative replace.
    with pytest.raises(TimeoutError):
        runtime.seal_and_publish(state, run, deadline=10.0)
    assert marker["entered"], "The actual after-fsync publication guard must execute"
    assert runtime.pointer_read(state) is None


def test_old_receipt_after_second_commit_and_corrupted_older_ancestry(tmp_path):
    source = fixture_source(tmp_path / "source")
    state = tmp_path / "state"
    first = submit(state, REQUEST, source=source)
    assert wait_terminal(state, first["run_id"])["status"] == "completed_degraded"
    second = submit(state, REQUEST | {"idempotency_key": "fixture-second-0002"}, source=source)
    assert wait_terminal(state, second["run_id"])["status"] == "completed_degraded"
    assert runtime.verify_run(state, first["run_id"])["verification"]["ok"]
    assert runtime.verify_run(state, second["run_id"])["verification"]["ok"]
    old = state / "accounts/public-usd/versions" / first["run_id"] / "version.json"
    old.write_bytes(old.read_bytes() + b" ")
    # Finding the current run must not stop ancestry verification prematurely.
    with pytest.raises(EvidenceInvalid, match="version hash"):
        runtime.committed_pointer(state, second["run_id"])


def test_source_changes_are_historical_disclosure_not_current_run_claim(completed):
    state, run, source = completed
    frozen = runtime.verify_run(state, run.name, current_source=source)
    assert frozen["current_source_matches"] is True
    changed = source.parent / ("changed-" + run.name)
    shutil.copytree(source, changed)
    with (changed / "main.py").open("a") as handle:
        handle.write("\n# TEST_FIXTURE current source changed\n")
    value = runtime.verify_run(state, run.name, current_source=changed)
    assert value["verification"]["ok"] and value["current_source_matches"] is False
    (run / "source/main.py").write_text("modified frozen source")
    with pytest.raises(EvidenceInvalid, match="hash mismatch"):
        runtime.verify_run(state, run.name, current_source=changed)


def test_conflict_and_broken_replay_never_rewrite_existing_status(completed):
    state, run, source = completed
    before = read_bytes(run, "status.json")
    mapping = next((state / "keys").iterdir())
    record = json.loads(mapping.read_bytes())
    record["request_sha256"] = "0" * 64
    mapping.write_bytes(json_bytes(record))
    result = submit(state, REQUEST, source=source)
    assert result["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert read_bytes(run, "status.json") == before
    record["request_sha256"] = digest(json_bytes(REQUEST))
    mapping.write_bytes(json_bytes(record))
    (run / "receipt.json").write_bytes(b"{}\n")
    result = submit(state, REQUEST, source=source)
    assert not result["operation_ok"]
    assert read_bytes(run, "status.json") == before


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-21T04:00:00+08:00", "2026-10-20"),
    ("2026-11-01T05:30:00+00:00", "2026-11-01"),
    ("2026-11-01T06:30:00+00:00", "2026-11-01"),
])
def test_market_admission_date_is_frozen_with_offset_and_dst(instant, expected):
    clock = runtime.frozen_clock(datetime.fromisoformat(instant))
    runtime.validate_clock(clock)
    assert clock["run_date"] == expected
    assert clock["market_timezone"] == "America/New_York"
    assert datetime.fromisoformat(clock["deadline_at_utc"]) - datetime.fromisoformat(
        clock["admitted_at_utc"]) == timedelta(seconds=2400)
