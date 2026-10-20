"""Diagnostic limits and real process-group ownership/reaping assertions."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent_workflow_fixtures import REQUEST, fixture_source, wait_terminal
from trading_system.agent_evidence import read_json
from trading_system.agent_runner import submit
from trading_system.agent_runtime import pointer_read
from trading_system.agent_supervisor import (
    BoundedLog, SupervisorInterrupted, _darwin_zombie_group, _kill_group,
    _safe_log_line, error_record,
)
from trading_system.redline.execution import StrictObservationError


@pytest.mark.parametrize("line", [
    b"Authorization: Bearer CANARY_SECRET",
    b'{"api_key":"CANARY_SECRET"}',
    b"{'password': 'CANARY_SECRET'}",
    b"api-key=CANARY_SECRET",
    b"https://user:" + b"CANARY_SECRET" + b"@example.test/a",
    b"https://example.test/a?token=CANARY_SECRET",
])
def test_diagnostics_redact_complete_credential_values(line):
    assert b"CANARY_SECRET" not in _safe_log_line(line)


def test_log_budget_and_oversized_lines_have_explicit_truncation_flags(tmp_path):
    # Tiny empty lines expand with labels, so raw input can be much smaller
    # than retained formatted output even though the budget was exhausted.
    log = BoundedLog(tmp_path, limit=64)
    log.consume("stdout", b"\n" * 20)
    summary = log.finish()
    assert summary["received_bytes"] < summary["retained_bytes"]
    assert summary["truncated"] and summary["budget_truncated"]
    assert (tmp_path / "engine.log").stat().st_size == 64
    another = tmp_path / "oversize"
    another.mkdir()
    log = BoundedLog(another)
    log.consume("stderr", b"a" * 20000)
    log.consume("stderr", b"CANARY_SECRET\nnormal\n")
    summary = log.finish()
    assert summary["oversized_lines"] == 1 and summary["truncated"]
    assert b"CANARY_SECRET" not in (another / "engine.log").read_bytes()
    assert b"normal" in (another / "engine.log").read_bytes()


def test_redaction_alone_does_not_claim_budget_truncation(tmp_path):
    log = BoundedLog(tmp_path)
    log.consume("stderr", b"Authorization: Bearer " + b"x" * 3000 + b"\n")
    summary = log.finish()
    assert summary["received_bytes"] > summary["retained_bytes"]
    assert not summary["truncated"] and not summary["budget_truncated"]


@pytest.mark.parametrize("exception,code", [
    (TimeoutError(), "TIMEOUT"), (InterruptedError(), "INTERRUPTED"),
    (SupervisorInterrupted(), "INTERRUPTED"), (PermissionError(), "ARTIFACT_WRITE_FAILED"),
])
def test_terminal_error_code_precedes_oserror_parent_class(exception, code):
    assert error_record(exception, run_id="run-" + "9" * 32)["code"] == code


def test_observation_boundary_has_controlled_data_error_without_arbitrary_codes():
    run_id = "run-" + "8" * 32
    value = error_record(StrictObservationError("CANARY_PRIVATE_DETAIL"),
                         run_id=run_id, stage="data.prepare")
    assert value["code"] == "DATA_UNAVAILABLE"
    assert value["stage"] == "data.prepare"
    assert value["run_id"] == run_id
    assert "CANARY_PRIVATE_DETAIL" not in value["message"]

    class ArbitraryProviderError(RuntimeError):
        code = "DATA_UNAVAILABLE"

    assert error_record(ArbitraryProviderError(), run_id=run_id)["code"] == "RUNNER_FAILED"


def test_group_cleanup_precedes_reap_and_never_targets_own_group():
    with pytest.raises(RuntimeError):
        _kill_group(os.getpgrp())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        _kill_group(child.pid)
        assert child.returncode is None  # Cleanup did not poll/reap the retained leader.
        assert child.wait(timeout=3) < 0
    finally:
        if child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin zombie-group EPERM semantics")
def test_darwin_zombie_only_permission_error_is_proven_before_reap():
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not _darwin_zombie_group(child.pid):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(PermissionError):
            os.killpg(child.pid, signal.SIGTERM)
        _kill_group(child.pid)
        assert child.returncode is None
        assert child.wait(timeout=3) == 0
    finally:
        if child.returncode is None:
            child.wait(timeout=3)


def test_live_permission_failure_is_never_ignored(monkeypatch):
    import trading_system.agent_supervisor as supervisor
    monkeypatch.setattr(supervisor.os, "killpg", lambda *_: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(supervisor, "_darwin_zombie_group", lambda _pgid: False)
    with pytest.raises(PermissionError):
        _kill_group(os.getpid() + 100000)


@pytest.mark.parametrize("mode", ["fds", "descendant", "flood"])
def test_engine_fd_isolation_descendant_cleanup_and_large_log_draining(tmp_path, mode):
    source = fixture_source(tmp_path / "source", mode=mode)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    run = state / "runs" / accepted["run_id"]
    observed = wait_terminal(state, run.name, seconds=25)
    assert observed["status"] == "completed_degraded", observed
    if mode == "fds":
        assert read_json(run, "work/descriptor-observation.json")["inherited_lock_fds"] == []
    elif mode == "descendant":
        descendant = read_json(run, "work/descendant-observation.json")
        state_text = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(descendant["pid"])],
            capture_output=True, text=True, check=False, timeout=3).stdout.strip()
        assert not state_text or state_text.startswith("Z"), state_text
        process = read_json(run, "process-exit.json")
        assert process["guardian_pid"] == descendant["pgid"]
    else:
        summary = read_json(run, "log-summary.json")
        assert summary["received_bytes"] > 8 * 1024 * 1024
        assert summary["budget_truncated"] and summary["truncated"]
        assert (run / "engine.log").stat().st_size == 8 * 1024 * 1024


def test_guardian_startup_failure_releases_locks_and_never_publishes(tmp_path):
    source = fixture_source(tmp_path / "source")
    path = source / "trading_system/agent_supervisor.py"
    code = path.read_text()
    marker = '    if os.getpgrp() != os.getpid():\n'
    assert code.count(marker) == 1
    code = code.replace(marker, '    raise RuntimeError("TEST_FIXTURE guardian startup failed")\n' + marker)
    path.write_text(code)
    state = tmp_path / "state"
    accepted = submit(state, REQUEST, source=source)
    assert accepted["operation_ok"], accepted
    observed = wait_terminal(state, accepted["run_id"])
    assert observed["status"] == "failed"
    assert pointer_read(state) is None
    from trading_system.agent_runner import _lock_fd
    fd = _lock_fd(state, "account.lock", create=False, nonblocking=True)
    os.close(fd)
