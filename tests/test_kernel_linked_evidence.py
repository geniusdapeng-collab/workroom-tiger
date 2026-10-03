"""MC003/202: synthetic routing and corrupt evidence remain explicit."""
import json
import os
from pathlib import Path

import pytest

from trading_system import pipeline
from trading_system.calibration import CalibrationLayer
from trading_system.providers.demo import DemoProvider
from trading_system.review.chief import ReviewChief


def _record(**values):
    return {"date": "2026-09-28", "ticker": "AAPL", "status": "closed",
            "r": 1., "tss_final": 8., "mrs_star": 8., **values}


INVALID_BYTES = [b"[broken", b'{"unexpected":"root"}',
    b'[{"date":"2026-09-28","ticker":"AAPL","status":"closed","r":NaN,"tss_final":8}]']


def test_mc202_sample_renamed_synthetic_source_cannot_start_real_search(monkeypatch):
    class RenamedDemo(DemoProvider):
        name = "renamed-fixture"
    class HubCheckpoint:
        def __init__(self, *, demo, use_disk_cache):
            assert demo is True, "Synthetic source selected real search"
            assert use_disk_cache is False
            raise RuntimeError("synthetic search checkpoint")
    monkeypatch.setattr(pipeline, "get_provider", lambda *args: RenamedDemo())
    monkeypatch.setattr(pipeline, "SearchHub", HubCheckpoint)
    with pytest.raises(RuntimeError, match="synthetic search checkpoint"):
        pipeline.run_pipeline(provider_name="renamed-fixture", universe_mode="core", top_n=1, max_picks=1)


@pytest.mark.parametrize("raw", INVALID_BYTES)
def test_mc003_sample_calibration_corrupt_journal_preserves_prior_samples(tmp_path, raw):
    journal, samples = tmp_path / "journal.json", tmp_path / "calibration_samples.json"
    journal.write_bytes(raw)
    previous = json.dumps([_record()]).encode()
    samples.write_bytes(previous)
    layer = CalibrationLayer(str(journal), str(samples))
    with pytest.raises(ValueError):
        layer.run()
    assert journal.read_bytes() == raw and samples.read_bytes() == previous


@pytest.mark.parametrize("raw", INVALID_BYTES)
def test_mc003_property_review_corrupt_journal_cannot_emit_an_empty_success_memo(tmp_path, raw):
    journal = tmp_path / "journal.json"
    journal.write_bytes(raw)
    samples = tmp_path / "calibration_samples.json"
    previous = json.dumps([_record()]).encode()
    samples.write_bytes(previous)
    with pytest.raises(ValueError):
        ReviewChief(str(tmp_path)).daily("2026-09-29")
    assert journal.read_bytes() == raw and samples.read_bytes() == previous
    assert not list(tmp_path.glob("复盘_*.md"))


@pytest.mark.parametrize("changes", [{"r": True}, {"r": "1"}, {"r": float("inf")},
    {"tss_final": "NaN"}, {"tss_final": 11}, {"mrs_star": True}, {"status": "unexpected"}])
def test_mc003_property_invalid_calibration_records_cannot_commit(tmp_path, changes):
    journal, samples = tmp_path / "journal.json", tmp_path / "calibration_samples.json"
    journal.write_text(json.dumps([_record(**changes)]))
    previous = b"[]"
    samples.write_bytes(previous)
    with pytest.raises(ValueError):
        CalibrationLayer(str(journal), str(samples)).run()
    assert samples.read_bytes() == previous


def test_mc003_property_calibration_real_atomic_write_failure_preserves_samples(tmp_path, monkeypatch):
    journal, samples = tmp_path / "journal.json", tmp_path / "calibration_samples.json"
    journal.write_text(json.dumps([_record()]))
    previous = b"[]"
    samples.write_bytes(previous)
    replace = os.replace
    def fail(source, target):
        if Path(target) == samples:
            raise OSError("calibration disk failure")
        return replace(source, target)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="calibration disk failure"):
        CalibrationLayer(str(journal), str(samples)).run()
    assert samples.read_bytes() == previous


def test_mc003_property_missing_journal_is_a_valid_empty_baseline(tmp_path):
    journal, samples = tmp_path / "journal.json", tmp_path / "calibration_samples.json"
    outcome = CalibrationLayer(str(journal), str(samples)).run()
    assert outcome["status"] == "accumulating" and outcome["n"] == 0
    assert json.loads(samples.read_text()) == []
