"""MC-026: every successful proposal creation must retain its own record."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event, local

import pytest

from trading_system.review import monthly
from trading_system.review.chief import ReviewChief


BASE_ID = "PROP-20261020-090000"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 20, 9, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(monthly, "datetime", FrozenDatetime)


def _wfa(marker="新提案"):
    return {
        "dsr": 0.97654,
        "oos_aggregate": {"expectancy_r": 0.2468, "trades": 20},
        "recommended_params": {"mrs_gate": 6.5, "marker": marker},
        "n_folds": 3,
        "grid_size": 27,
        "dsr_note": "保持原始统计说明",
    }


def _old_proposal(directory: Path, suffix="") -> Path:
    proposal = monthly.Proposal(
        proposal_id=BASE_ID + suffix,
        created_at="2026-10-20T09:00:00",
        grid_result={"recommended_params": {"marker": "原提案" + suffix}},
        dsr=0.99, oos_expectancy=0.2, verdict="pending_review",
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{proposal.proposal_id}.json"
    # Non-canonical formatting detects even a semantically equivalent rewrite.
    path.write_text(json.dumps(asdict(proposal), ensure_ascii=False, indent=4) + "\n")
    return path


def _assert_saved(directory, returned):
    stored = monthly.list_proposals(str(directory))
    assert len(stored) == len(returned)
    assert {p.proposal_id for p in stored} == {p.proposal_id for p in returned}
    assert {p.proposal_id: asdict(p) for p in stored} == {
        p.proposal_id: asdict(p) for p in returned
    }


def test_two_successful_concurrent_creations_retain_two_records(tmp_path, monkeypatch):
    """Both calls reach the lock boundary before either is allowed to write."""
    original_lock = monthly._approval_lock
    entered = Barrier(2)
    first_finished = Event()
    worker = local()

    @contextmanager
    def scheduled_lock(path):
        entered.wait(timeout=5)
        if worker.marker == "second":
            assert first_finished.wait(timeout=5)
        try:
            with original_lock(path):
                yield
        finally:
            if worker.marker == "first":
                first_finished.set()

    def create(marker):
        worker.marker = marker
        return monthly.generate_proposal(_wfa(marker), str(tmp_path))

    with monkeypatch.context() as scheduling:
        scheduling.setattr(monthly, "_approval_lock", scheduled_lock)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(create, marker) for marker in ("first", "second")]
            returned = [future.result(timeout=10) for future in futures]
    assert len({p.proposal_id for p in returned}) == 2
    _assert_saved(tmp_path, returned)


def test_serial_same_second_creations_keep_every_payload(tmp_path):
    returned = [
        monthly.generate_proposal(_wfa(f"serial-{index}"), str(tmp_path))
        for index in range(12)
    ]
    assert [p.proposal_id for p in returned] == (
        [BASE_ID] + [f"{BASE_ID}-{index}" for index in range(2, 13)]
    )
    _assert_saved(tmp_path, returned)


def test_numbering_hole_never_rewrites_legal_proposal_bytes(tmp_path):
    paths = [_old_proposal(tmp_path, suffix) for suffix in ("", "-3")]
    before = {path: path.read_bytes() for path in paths}
    created = monthly.generate_proposal(_wfa(), str(tmp_path))
    assert created.proposal_id not in {path.stem for path in paths}
    assert {path: path.read_bytes() for path in paths} == before
    assert len(monthly.list_proposals(str(tmp_path))) == 3


@pytest.mark.parametrize("kind", ["invalid-json", "directory", "symlink", "broken-symlink"])
def test_occupied_paths_are_not_available_ids(tmp_path, kind):
    directory = tmp_path / "proposals"
    directory.mkdir()
    occupied = directory / f"{BASE_ID}.json"
    later = _old_proposal(directory, "-3")
    before_later = later.read_bytes()
    outside = tmp_path / "outside"
    if kind == "invalid-json":
        occupied.write_bytes(b'{"interrupted":')
    elif kind == "directory":
        occupied.mkdir()
        (occupied / "keep").write_bytes(b"directory payload")
    else:
        if kind == "symlink":
            outside.write_bytes(b"linked payload")
        occupied.symlink_to(outside)
    before_stat = occupied.lstat()
    created = monthly.generate_proposal(_wfa(), str(directory))
    assert created.proposal_id not in (BASE_ID, BASE_ID + "-3")
    assert later.read_bytes() == before_later
    assert occupied.lstat().st_ino == before_stat.st_ino
    if kind == "invalid-json":
        assert occupied.read_bytes() == b'{"interrupted":'
    elif kind == "directory":
        assert (occupied / "keep").read_bytes() == b"directory payload"
    else:
        assert occupied.is_symlink() and occupied.readlink() == outside
        if kind == "symlink":
            assert outside.read_bytes() == b"linked payload"
        else:
            assert not outside.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_publish_time_collision_cannot_overwrite_target(tmp_path, monkeypatch, kind):
    """An uncoordinated filesystem writer may occupy the chosen name."""
    target = tmp_path / f"{BASE_ID}.json"
    outside = tmp_path / "outside"
    original_dump = json.dump

    def occupy_target(value, handle, *args, **kwargs):
        result = original_dump(value, handle, *args, **kwargs)
        assert value["proposal_id"] == BASE_ID
        if kind == "file":
            target.write_bytes(b"externally created")
        elif kind == "directory":
            target.mkdir()
            (target / "keep").write_bytes(b"keep")
        else:
            if kind == "symlink":
                outside.write_bytes(b"outside")
            target.symlink_to(outside)
        return result

    monkeypatch.setattr(json, "dump", occupy_target)
    with pytest.raises(OSError):
        monthly.generate_proposal(_wfa(), str(tmp_path))
    if kind == "file":
        assert target.read_bytes() == b"externally created"
    elif kind == "directory":
        assert (target / "keep").read_bytes() == b"keep"
    else:
        assert target.is_symlink() and target.readlink() == outside
        if kind == "symlink":
            assert outside.read_bytes() == b"outside"
        else:
            assert not outside.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_nested_directory_is_created(tmp_path):
    directory = tmp_path / "new" / "nested" / "proposals"
    proposal = monthly.generate_proposal(_wfa(), str(directory))
    assert proposal.proposal_id == BASE_ID
    _assert_saved(directory, [proposal])
    assert (directory / ".approval.lock").exists()


@pytest.mark.parametrize("failure", ["serialization", "partial-write", "fsync"])
def test_write_failure_preserves_old_bytes_and_can_retry(tmp_path, monkeypatch, failure, caplog):
    original = _old_proposal(tmp_path)
    before = original.read_bytes()
    wfa = _wfa()
    chief = ReviewChief(proposals_dir=str(tmp_path))
    emitted = []
    monkeypatch.setattr(chief, "_emit", lambda **event: emitted.append(event))

    def fail_dump(_value, handle, *_args, **_kwargs):
        handle.write('{"partial":')
        raise OSError("injected incomplete proposal write")

    def fail_sync(_fd):
        raise OSError("injected proposal fsync failure")

    with monkeypatch.context() as failure_patch:
        if failure == "serialization":
            wfa["recommended_params"]["invalid"] = object()
            expected = TypeError
        elif failure == "partial-write":
            failure_patch.setattr(json, "dump", fail_dump)
            expected = OSError
        else:
            failure_patch.setattr(os, "fsync", fail_sync)
            expected = OSError
        with pytest.raises(expected):
            chief.monthly(wfa)
    assert original.read_bytes() == before
    assert list(tmp_path.glob("*.json")) == [original]
    assert not list(tmp_path.glob("*.tmp"))
    assert emitted == []
    assert "失败" in caplog.text
    created = chief.monthly(_wfa("retry"))
    assert created.proposal_id != original.stem
    assert original.read_bytes() == before
    assert len(emitted) == 1


def test_unwritable_directory_cannot_report_success(tmp_path):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("POSIX non-root permission semantics required")
    original = _old_proposal(tmp_path)
    before = original.read_bytes()
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            monthly.generate_proposal(_wfa(), str(tmp_path))
    finally:
        tmp_path.chmod(0o700)
    assert original.read_bytes() == before
    assert list(tmp_path.glob("*.json")) == [original]


def test_unfinished_approval_transaction_blocks_new_creation(tmp_path):
    original = _old_proposal(tmp_path)
    before = original.read_bytes()
    journal = tmp_path / monthly.APPROVAL_TRANSACTION
    journal.write_bytes(b'{"incomplete":')
    with pytest.raises(RuntimeError, match="审批事务待恢复"):
        monthly.generate_proposal(_wfa(), str(tmp_path))
    assert journal.read_bytes() == b'{"incomplete":'
    assert original.read_bytes() == before
    assert list(tmp_path.glob("*.json")) == [original]


@pytest.mark.parametrize("alias_kind", ["canonical", "dotdot", "symlink"])
def test_existing_directory_lock_covers_path_aliases(tmp_path, alias_kind):
    directory = tmp_path / "proposals"
    original = _old_proposal(directory)
    before = original.read_bytes()
    if alias_kind == "canonical":
        alias = directory
    elif alias_kind == "dotdot":
        (tmp_path / "extra").mkdir()
        alias = tmp_path / "extra" / ".." / "proposals"
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(directory, target_is_directory=True)
    with monthly._approval_lock(directory / ".approval.lock"):
        with pytest.raises(OSError):
            monthly.generate_proposal(_wfa(), str(alias))
    assert original.read_bytes() == before
    assert list(directory.glob("*.json")) == [original]
    created = monthly.generate_proposal(_wfa(), str(alias))
    assert monthly.load_proposal(str(directory), created.proposal_id) == created


def test_separate_process_respects_existing_approval_lock(tmp_path):
    original = _old_proposal(tmp_path)
    before = original.read_bytes()
    script = """
import sys
from trading_system.review import monthly
try:
    monthly.generate_proposal({
        "dsr": .99, "oos_aggregate": {"expectancy_r": .2},
        "recommended_params": {"marker": "child"},
    }, sys.argv[1])
except OSError:
    raise SystemExit(73)
raise SystemExit(0)
"""
    with monthly._approval_lock(tmp_path / ".approval.lock"):
        child = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True, text=True, timeout=20,
        )
    assert child.returncode == 73, child.stderr
    assert original.read_bytes() == before
    assert list(tmp_path.glob("*.json")) == [original]


def test_save_proposal_still_updates_existing_record(tmp_path):
    proposal = monthly.generate_proposal(_wfa(), str(tmp_path))
    proposal.status = "rejected"
    proposal.reason = "人工复核后继续观察"
    path = monthly.save_proposal(proposal, str(tmp_path))
    assert Path(path).name == f"{proposal.proposal_id}.json"
    _assert_saved(tmp_path, [proposal])


@pytest.mark.parametrize(
    "dsr,expectancy,params,verdict,reason_fragment",
    [
        (.95, .2, {"mrs_gate": 6.5}, "pending_review", ""),
        (.94999, .2, {"mrs_gate": 6.5}, "reject", "统计不显著"),
        (.99, 0.0, {"mrs_gate": 6.5}, "reject", "非正"),
        (.99, -.2, {"mrs_gate": 6.5}, "reject", "非正"),
        (.99, .2, {}, "reject", "无推荐参数"),
    ],
)
def test_assessment_and_proposal_fields_are_unchanged(
    tmp_path, dsr, expectancy, params, verdict, reason_fragment,
):
    wfa = _wfa()
    wfa.update(dsr=dsr, recommended_params=params)
    wfa["oos_aggregate"]["expectancy_r"] = expectancy
    proposal = monthly.generate_proposal(wfa, str(tmp_path))
    assert proposal.created_at == "2026-10-20T09:00:00"
    assert proposal.verdict == proposal.status == verdict
    assert reason_fragment in proposal.reason
    assert proposal.dsr == round(dsr, 4)
    assert proposal.oos_expectancy == round(expectancy, 3)
    assert proposal.oos_aggregate == wfa["oos_aggregate"]
    assert proposal.grid_result == {
        "recommended_params": params, "n_folds": 3,
        "grid_size": 27, "dsr_note": "保持原始统计说明",
    }
    assert proposal.approved_at is None
    assert proposal.rejected_at is None
    assert proposal.effective_from is None
    assert not (tmp_path / "tuned_params.json").exists()
    _assert_saved(tmp_path, [proposal])
