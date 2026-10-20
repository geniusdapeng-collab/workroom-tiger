"""MC-008: failed approval persistence must not report a completed approval."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from trading_system import ledger_io
from trading_system.review import chief as chief_module, monthly
from trading_system.review.chief import ReviewChief


@pytest.fixture
def pending(tmp_path):
    proposals = tmp_path / "proposals"
    p = monthly.Proposal(
        proposal_id="PROP-故障恢复", created_at="2026-10-20T09:00:00",
        grid_result={"recommended_params": {"mrs_gate": 6.5}},
        dsr=0.97, oos_expectancy=0.2, verdict="pending_review",
        oos_aggregate={"expectancy_r": 0.2, "trades": 20},
    )
    proposal_path = Path(monthly.save_proposal(p, str(proposals)))
    tuned = tmp_path / "tuned_params.json"
    tuned.write_bytes(b'{ "proposal_id": "previous", "params": {} }\n')
    chief = ReviewChief(out_dir=str(tmp_path), proposals_dir=str(proposals))
    return chief, p, proposal_path, tuned


def _assert_pending(chief, p, proposal_path, tuned, before_proposal, before_tuned):
    assert proposal_path.read_bytes() == before_proposal
    assert tuned.read_bytes() == before_tuned
    assert monthly.load_proposal(chief.proposals_dir, p.proposal_id).status == "pending_review"


def test_directory_target_leaves_pending_and_can_retry(pending, tmp_path, caplog):
    chief, p, proposal_path, tuned = pending
    before = proposal_path.read_bytes()
    with pytest.raises(IsADirectoryError):
        chief.approve(p.proposal_id, tuned_path=str(tmp_path))
    assert proposal_path.read_bytes() == before
    assert "审批" in caplog.text
    approved = chief.approve(p.proposal_id, tuned_path=str(tuned))
    assert approved.status == "approved"
    assert json.loads(tuned.read_bytes())["proposal_id"] == p.proposal_id


@pytest.mark.parametrize("failure_target", ["proposal", "tuned"])
def test_partial_write_keeps_both_old_files(pending, monkeypatch, failure_target, caplog):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()
    original_dump = json.dump

    def fail_dump(value, handle, *args, **kwargs):
        is_target = ((failure_target == "tuned" and "params" in value)
                     or (failure_target == "proposal" and value.get("status") == "approved"))
        if is_target:
            handle.write('{"partial":')
            raise OSError("injected partial write")
        return original_dump(value, handle, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(json, "dump", fail_dump)
        with pytest.raises(OSError, match="injected partial write"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)
    assert "审批" in caplog.text
    assert chief.approve(p.proposal_id, tuned_path=str(tuned)).status == "approved"


@pytest.mark.parametrize("failure_target", ["proposal", "tuned"])
def test_replace_failure_restores_both_files(pending, monkeypatch, failure_target):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()
    target = proposal_path if failure_target == "proposal" else tuned
    original_replace = os.replace

    def fail_replace(source, destination):
        if Path(destination) == target:
            raise PermissionError("injected replacement denied")
        return original_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_replace)
        with pytest.raises(PermissionError, match="injected replacement denied"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)
    assert chief.approve(p.proposal_id, tuned_path=str(tuned)).status == "approved"


def test_fsync_failure_cannot_commit_approval(pending, monkeypatch):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()

    def fail_sync(_fd):
        raise OSError("injected flush failure")

    with monkeypatch.context() as fault:
        fault.setattr(ledger_io.os, "fsync", fail_sync)
        with pytest.raises(OSError, match="injected flush failure"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)


def test_proposal_serialization_failure_preserves_previous_bytes(pending):
    chief, p, proposal_path, _ = pending
    before = proposal_path.read_bytes()
    p.grid_result["invalid"] = object()
    with pytest.raises(TypeError):
        monthly.save_proposal(p, chief.proposals_dir)
    assert proposal_path.read_bytes() == before


def test_unwritable_target_directory_leaves_pending(pending, tmp_path):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("POSIX non-root permission semantics required")
    chief, p, proposal_path, _ = pending
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    before = proposal_path.read_bytes()
    blocked.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            chief.approve(p.proposal_id, tuned_path=str(blocked / "tuned.json"))
    finally:
        blocked.chmod(0o700)
    assert proposal_path.read_bytes() == before


def test_target_cannot_alias_proposal_file(pending):
    chief, p, proposal_path, _ = pending
    before = proposal_path.read_bytes()
    with pytest.raises(ValueError, match="同一|重合"):
        chief.approve(p.proposal_id, tuned_path=str(proposal_path))
    assert proposal_path.read_bytes() == before


def test_rollback_failure_is_visible_and_retry_recovers(pending, monkeypatch, caplog):
    chief, p, proposal_path, tuned = pending
    before_t = tuned.read_bytes()
    original_replace = os.replace

    def fail_replace(source, destination):
        destination = Path(destination)
        if destination == tuned:
            raise PermissionError("injected tuned replacement denied")
        if destination == proposal_path and json.loads(Path(source).read_bytes())["status"] == "pending_review":
            raise PermissionError("injected rollback denied")
        return original_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_replace)
        with pytest.raises(RuntimeError, match="恢复"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    assert tuned.read_bytes() == before_t
    with pytest.raises(RuntimeError, match="恢复"):
        monthly.load_proposal(chief.proposals_dir, p.proposal_id)
    with pytest.raises(RuntimeError, match="恢复"):
        monthly.list_proposals(chief.proposals_dir)
    assert "恢复" in caplog.text
    assert chief.approve(p.proposal_id, tuned_path=str(tuned)).status == "approved"
    assert json.loads(tuned.read_bytes())["proposal_id"] == p.proposal_id


def test_process_exit_before_tuned_publish_can_be_recovered(pending):
    chief, p, proposal_path, tuned = pending
    before_t = tuned.read_bytes()
    script = """
import os
import sys
from pathlib import Path
from trading_system.review.chief import ReviewChief
original = os.replace
def exit_before_tuned(source, destination):
    if Path(destination) == Path(sys.argv[3]):
        os._exit(91)
    return original(source, destination)
os.replace = exit_before_tuned
ReviewChief(proposals_dir=sys.argv[1]).approve(sys.argv[2], tuned_path=sys.argv[3])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, chief.proposals_dir, p.proposal_id, str(tuned)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 91, result.stderr
    assert tuned.read_bytes() == before_t
    with pytest.raises(RuntimeError, match="恢复"):
        monthly.load_proposal(chief.proposals_dir, p.proposal_id)
    # Recovery is not an approval: a subsequent explicit rejection must succeed.
    rejected = chief.reject(p.proposal_id, "恢复后保留默认参数")
    assert rejected.status == "rejected"
    assert json.loads(proposal_path.read_bytes())["status"] == "rejected"
    assert tuned.read_bytes() == before_t


def test_rejected_proposal_cannot_be_approved(pending):
    chief, p, _, tuned = pending
    chief.reject(p.proposal_id, "需要继续观察")
    before = tuned.read_bytes()
    with pytest.raises(ValueError, match="pending_review"):
        chief.approve(p.proposal_id, tuned_path=str(tuned))
    assert tuned.read_bytes() == before


def test_success_preserves_next_day_and_explicit_activation(pending):
    chief, p, proposal_path, tuned = pending
    approved = chief.approve(p.proposal_id, tuned_path=str(tuned))
    saved = json.loads(proposal_path.read_bytes())
    blob = json.loads(tuned.read_bytes())
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    assert saved["status"] == approved.status == "approved"
    assert saved["effective_from"] == blob["effective_from"] == tomorrow
    assert blob["params"] == p.grid_result["recommended_params"]
    assert "--use-tuned" in blob["disclosure"]
    with pytest.raises(ValueError, match="pending_review"):
        chief.approve(p.proposal_id, tuned_path=str(tuned))


def test_concurrent_approve_and_reject_do_not_both_succeed(pending, monkeypatch):
    chief, p, _, tuned = pending
    started, release = Event(), Event()
    original_dump = json.dump

    def pause_approved(value, handle, *args, **kwargs):
        if value.get("status") == "approved":
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release approval")
        return original_dump(value, handle, *args, **kwargs)

    monkeypatch.setattr(json, "dump", pause_approved)
    with ThreadPoolExecutor(max_workers=1) as executor:
        approving = executor.submit(chief.approve, p.proposal_id, str(tuned))
        assert started.wait(timeout=5)
        try:
            with pytest.raises((OSError, RuntimeError, ValueError)):
                chief.reject(p.proposal_id, "并发驳回")
        finally:
            release.set()
        assert approving.result(timeout=5).status == "approved"
    assert monthly.load_proposal(chief.proposals_dir, p.proposal_id).status == "approved"


def test_missing_old_tuned_stays_absent_after_failure(pending, monkeypatch):
    chief, p, proposal_path, tuned = pending
    tuned.unlink()
    before = proposal_path.read_bytes()
    original_replace = os.replace

    def fail_tuned(source, destination):
        if Path(destination) == tuned:
            raise OSError("injected first tuned write failure")
        return original_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_tuned)
        with pytest.raises(OSError, match="injected first tuned write failure"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    assert not tuned.exists()
    assert proposal_path.read_bytes() == before
    assert chief.approve(p.proposal_id, tuned_path=str(tuned)).status == "approved"


def test_journal_failure_cannot_publish_or_emit(pending, monkeypatch):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()
    emitted = []
    monkeypatch.setattr(chief, "_emit", lambda **event: emitted.append(event))
    original_replace = os.replace

    def fail_journal(source, destination):
        if Path(destination).name == monthly.APPROVAL_TRANSACTION:
            raise OSError("injected recovery journal failure")
        return original_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_journal)
        with pytest.raises(OSError, match="injected recovery journal failure"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)
    assert emitted == []


def test_completed_commit_cleanup_failure_is_retryable(pending, monkeypatch):
    chief, p, proposal_path, tuned = pending
    original_unlink = Path.unlink
    emitted = []
    monkeypatch.setattr(chief, "_emit", lambda **event: emitted.append(event))

    def fail_cleanup(path, *args, **kwargs):
        if path.name == monthly.APPROVAL_TRANSACTION:
            raise PermissionError("injected journal cleanup denied")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(PermissionError, match="injected journal cleanup denied"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    committed_p, committed_t = proposal_path.read_bytes(), tuned.read_bytes()
    assert json.loads(committed_p)["status"] == "approved"
    assert json.loads(committed_t)["proposal_id"] == p.proposal_id
    assert emitted == []
    with pytest.raises(RuntimeError, match="恢复"):
        monthly.load_proposal(chief.proposals_dir, p.proposal_id)
    assert chief.approve(p.proposal_id, tuned_path=str(tuned)).status == "approved"
    assert proposal_path.read_bytes() == committed_p
    assert tuned.read_bytes() == committed_t
    assert len(emitted) == 1
    with pytest.raises(ValueError, match="pending_review"):
        chief.approve(p.proposal_id, tuned_path=str(tuned))


def test_recovery_conflict_never_overwrites_external_edit(pending, monkeypatch):
    chief, p, proposal_path, tuned = pending
    original_replace = os.replace

    def fail_commit_and_rollback(source, destination):
        if Path(destination) == tuned or (
                Path(destination) == proposal_path
                and json.loads(Path(source).read_bytes())["status"] == "pending_review"):
            raise PermissionError("injected unavailable storage")
        return original_replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_commit_and_rollback)
        with pytest.raises(RuntimeError, match="恢复"):
            chief.approve(p.proposal_id, tuned_path=str(tuned))
    external = b'{"external_edit": true}\n'
    tuned.write_bytes(external)
    proposal_before_recovery = proposal_path.read_bytes()
    with pytest.raises(RuntimeError, match="外部修改"):
        chief.approve(p.proposal_id, tuned_path=str(tuned))
    assert tuned.read_bytes() == external
    assert proposal_path.read_bytes() == proposal_before_recovery
    assert (Path(chief.proposals_dir) / monthly.APPROVAL_TRANSACTION).exists()


def test_second_process_cannot_approve_while_locked(pending):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()
    script = """
import sys
from trading_system.review.chief import ReviewChief
try:
    ReviewChief(proposals_dir=sys.argv[1]).approve(sys.argv[2], tuned_path=sys.argv[3])
except OSError:
    raise SystemExit(73)
"""
    with chief_module._approval_guard(chief.proposals_dir, str(tuned)):
        result = subprocess.run(
            [sys.executable, "-c", script, chief.proposals_dir, p.proposal_id, str(tuned)],
            capture_output=True, text=True, timeout=20,
        )
    assert result.returncode == 73, result.stderr
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)


@pytest.mark.parametrize("reader", ["load", "list"])
def test_readers_hold_lock_across_snapshot(pending, monkeypatch, reader):
    chief, p, proposal_path, tuned = pending
    before_p, before_t = proposal_path.read_bytes(), tuned.read_bytes()
    started, release = Event(), Event()
    original_load = json.load

    def pause_read(handle, *args, **kwargs):
        value = original_load(handle, *args, **kwargs)
        if value.get("status") == "pending_review" and not started.is_set():
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release reader")
        return value

    monkeypatch.setattr(json, "load", pause_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        if reader == "load":
            reading = executor.submit(monthly.load_proposal, chief.proposals_dir, p.proposal_id)
        else:
            reading = executor.submit(monthly.list_proposals, chief.proposals_dir)
        assert started.wait(timeout=5)
        try:
            with pytest.raises(OSError):
                chief.approve(p.proposal_id, tuned_path=str(tuned))
        finally:
            release.set()
        result = reading.result(timeout=5)
    assert (result if reader == "load" else result[0]).status == "pending_review"
    _assert_pending(chief, p, proposal_path, tuned, before_p, before_t)
