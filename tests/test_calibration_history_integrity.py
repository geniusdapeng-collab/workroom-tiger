"""MC023: failed calibration must preserve the last usable sample history.

All files are temporary fixtures; the pipeline case explicitly disables paid LLM
clients and uses the synthetic provider. Calibration math and strategy stay fixed.
"""

import io
import json
from pathlib import Path

import pytest

from trading_system.calibration import CalibrationLayer


def _record(**changes):
    return {"date": "2026-08-28", "ticker": "NVDA", "status": "closed",
            "tss_final": 8.0, "mrs_star": 7.0, "r": 1.25, **changes}


@pytest.fixture
def history(tmp_path):
    layer = CalibrationLayer(str(tmp_path / "journal.json"),
                             str(tmp_path / "samples.json"))
    previous = [{"date": "2026-08-27", "ticker": "OLD", "tss_final": 7.5,
                 "mrs_star": 7.0, "r": -0.5, "win": False}]
    layer.samples_path.write_text(json.dumps(previous, indent=3) + "\n",
                                  encoding="utf-8")
    return layer, layer.samples_path.read_bytes()


def _write_journal(layer, records):
    layer.journal_path.write_text(json.dumps(records), encoding="utf-8")
    return layer.journal_path.read_bytes()


@pytest.mark.parametrize("content", [
    b"{broken", b"", b"\xff", b"null", b"{}", b"1", b"true", b'"history"',
])
def test_unreadable_or_wrong_shape_journal_preserves_samples(history, content):
    layer, previous = history
    layer.journal_path.write_bytes(content)

    with pytest.raises(ValueError, match=r"校准.*journal\.json"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == content


@pytest.mark.parametrize("record", [
    None, [], "record", 3, True, {},
    {"ticker": "NVDA", "status": "closed", "r": 1.0},
    _record(date=None), _record(ticker=[]), _record(status=None), _record(status=""),
])
def test_malformed_record_does_not_erase_usable_history(history, record):
    layer, previous = history
    source = _write_journal(layer, [record])

    with pytest.raises(ValueError, match=r"校准.*journal\.json"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source


@pytest.mark.parametrize("field", ["r", "tss_final", "mrs_star"])
@pytest.mark.parametrize("value", [
    "not-a-number", [], {}, True, float("nan"), float("inf"), float("-inf"), 10 ** 400,
], ids=["text", "list", "object", "bool", "nan", "positive-inf", "negative-inf", "overflow"])
def test_invalid_calibration_numbers_preserve_old_history(history, field, value):
    layer, previous = history
    source = _write_journal(layer, [_record(**{field: value})])

    with pytest.raises(ValueError, match=rf"校准.*{field}"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source


def test_invalid_duplicate_record_is_not_hidden_by_deduplication(history):
    layer, previous = history
    source = _write_journal(layer, [_record(), _record(r=float("nan"))])

    with pytest.raises(ValueError, match="校准.*r"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read failed")],
                         ids=["permission", "io"])
def test_source_read_error_has_reason_and_preserves_history(history, monkeypatch, error):
    layer, previous = history
    source = _write_journal(layer, [_record()])
    read_text = Path.read_text

    def fail_journal(path, *args, **kwargs):
        if path == layer.journal_path:
            raise error
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_journal)
    with pytest.raises(ValueError, match=rf"校准.*journal\.json.*{type(error).__name__}") as caught:
        layer.run()

    assert caught.value.__cause__ is error
    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source


def test_journal_directory_is_not_treated_as_empty_history(history):
    layer, previous = history
    layer.journal_path.mkdir()

    with pytest.raises(ValueError, match=r"校准.*journal\.json.*IsADirectoryError"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.is_dir()


def test_missing_initial_journal_remains_legitimate_empty_history(tmp_path):
    layer = CalibrationLayer(str(tmp_path / "journal.json"),
                             str(tmp_path / "nested/samples.json"))
    first = layer.run()
    assert first["status"] == "accumulating" and first["n"] == 0
    assert first["curve"] is None and first["monotonic"] is None
    assert json.loads(layer.samples_path.read_text(encoding="utf-8")) == []
    assert not layer.journal_path.exists()
    # Repeated first-use runs keep the empty-history contract and prior bytes.
    layer.samples_path.write_bytes(b"\n[ ]\n")
    second = layer.run()
    assert second == first
    assert layer.samples_path.read_bytes() == b"\n[ ]\n"


@pytest.mark.parametrize("existing", [b'[{"ticker":"OLD","r":1}]', b"{broken", b"{}", b""])
def test_missing_source_cannot_silently_clear_existing_history(tmp_path, existing):
    layer = CalibrationLayer(str(tmp_path / "journal.json"), str(tmp_path / "samples.json"))
    layer.samples_path.write_bytes(existing)

    with pytest.raises(ValueError, match=r"校准.*journal\.json.*缺失"):
        layer.run()

    assert layer.samples_path.read_bytes() == existing
    assert not layer.journal_path.exists()


def test_missing_source_with_unreadable_samples_fails_closed(history, monkeypatch):
    layer, previous = history
    read_text = Path.read_text

    def deny_samples(path, *args, **kwargs):
        if path == layer.samples_path:
            raise PermissionError("samples denied")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_samples)
    with pytest.raises(ValueError, match=r"校准.*journal\.json.*缺失"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous


def test_valid_empty_journal_is_authoritative(history):
    layer, _previous = history
    source = _write_journal(layer, [])

    summary = layer.run()

    assert summary["status"] == "accumulating" and summary["n"] == 0
    assert json.loads(layer.samples_path.read_text(encoding="utf-8")) == []
    assert layer.journal_path.read_bytes() == source


def test_normal_records_keep_settlement_deduplication_and_bucket_math(history):
    layer, _previous = history
    source = _write_journal(layer, [
        _record(), _record(r=-9),  # same day/ticker: the first settled record wins
        _record(ticker="LOSS", tss_final=7.5, r=-0.5),
        _record(ticker="OPEN", status="open", r=None, tss_final="not-yet-settled"),
        _record(ticker="VOID", status="void", r=None),
        _record(ticker="PENDING", r=None),
        _record(ticker="LEGACY", tss_final=None, mrs_star=None, r=0),
    ])

    summary = layer.run()

    assert summary["status"] == "accumulating" and summary["n"] == 3
    assert [(b["n"], b["wins"], b["avg_r"]) for b in summary["buckets"]] == [
        (1, 0, -0.5), (1, 1, 1.25), (0, 0, None),
    ]
    samples = json.loads(layer.samples_path.read_text(encoding="utf-8"))
    assert [s["ticker"] for s in samples] == ["NVDA", "LOSS", "LEGACY"]
    assert [s["win"] for s in samples] == [True, False, False]
    assert samples[-1]["tss_final"] is None and samples[-1]["mrs_star"] is None
    assert layer.journal_path.read_bytes() == source


def test_summary_failure_does_not_commit_replacement_samples(history, monkeypatch):
    layer, previous = history
    source = _write_journal(layer, [_record()])

    def fail_summary(_samples):
        raise ArithmeticError("summary calculation failed")

    monkeypatch.setattr(layer, "summarize", fail_summary)
    with pytest.raises(ArithmeticError, match="summary calculation failed"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source


@pytest.mark.parametrize("failure", ["write", "flush", "replace"])
def test_atomic_write_failure_keeps_complete_previous_samples(history, monkeypatch, failure):
    from trading_system import ledger_io

    layer, previous = history
    source = _write_journal(layer, [_record()])

    def fail(*_args, **_kwargs):
        raise OSError(f"injected {failure} failure")

    if failure == "write":
        real_open = io.open

        class PartialWrite:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def write(self, text):
                self.handle.write(text[:max(1, len(text) // 2)])
                self.handle.flush()
                fail()

        def partial_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return PartialWrite(handle) if "w" in mode else handle

        monkeypatch.setattr(io, "open", partial_open)
    elif failure == "flush":
        monkeypatch.setattr(ledger_io.os, "fsync", fail)
    else:
        monkeypatch.setattr(ledger_io.os, "replace", fail)

    with pytest.raises(OSError, match=f"injected {failure} failure"):
        layer.run()

    assert layer.samples_path.read_bytes() == previous
    assert layer.journal_path.read_bytes() == source
    assert sorted(p.name for p in layer.samples_path.parent.iterdir()) == ["journal.json", "samples.json"]


@pytest.mark.parametrize("bad", [float("nan"), object()], ids=["nonfinite", "unserializable"])
def test_direct_sample_serialization_failure_preserves_previous_bytes(history, bad):
    layer, previous = history
    sample = {"ticker": "NVDA", "tss_final": 8, "r": 1, "win": True, "extra": bad}

    with pytest.raises((TypeError, ValueError)):
        layer.save_samples([sample])

    assert layer.samples_path.read_bytes() == previous
    assert sorted(p.name for p in layer.samples_path.parent.iterdir()) == ["samples.json"]


def test_pipeline_discloses_failure_without_changing_strategy_or_erasing_history(tmp_path, monkeypatch):
    from trading_system import config, pipeline
    from trading_system.redline import LLMUnavailable
    from trading_system.report import render_markdown

    class NoPaidClient:
        def complete_json(self, **_kwargs):
            raise LLMUnavailable("MC023 test: paid LLM disabled")

    monkeypatch.setattr(pipeline, "default_client", lambda: NoPaidClient())
    monkeypatch.setattr(config, "DEBATE_ENABLED", False)
    journal_path, samples_path = tmp_path / "journal.json", tmp_path / "samples.json"
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(samples_path))
    journal_path.write_text(json.dumps([_record()]), encoding="utf-8")

    first = pipeline.run_pipeline(provider_name="demo", universe_mode="core", top_n=5, max_picks=3)
    previous = samples_path.read_bytes()
    journal_path.write_bytes(b"{broken")
    failed = pipeline.run_pipeline(provider_name="demo", universe_mode="core", top_n=5, max_picks=3)

    def decision(result):
        return (result.action, result.mrs.mrs_star,
                [(p.ticker, p.tss_final, p.tos, p.shares, p.card) for p in result.picks],
                [(c.ticker, c.tss_final) for c in result.watchlist])

    assert decision(failed) == decision(first)
    calibration = failed.raw["calibration"]
    assert calibration["status"] == "skipped" and "n" not in calibration
    assert "journal.json" in calibration["reason"] and "JSON" in calibration["reason"]
    report = render_markdown(failed)
    assert "本轮校准环节失败" in report and "journal.json" in report
    assert "校准样本积累中（0/" not in report
    assert samples_path.read_bytes() == previous and journal_path.read_bytes() == b"{broken"
