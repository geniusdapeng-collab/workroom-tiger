"""Accounting ledgers must never turn a damaged file into an empty history."""

import os

import pytest

from trading_system.journal import Journal
from trading_system.simulator import SimEngine


@pytest.mark.parametrize("content", ["{broken", "null", "{}", "[{}]"])
def test_journal_rejects_damaged_existing_file(tmp_path, content):
    path = tmp_path / "journal.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="journal.json"):
        Journal(path)

    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", ["{broken", "null", "{}", "[]"])
def test_simulator_rejects_damaged_existing_file(tmp_path, content):
    path = tmp_path / "sim_portfolio.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="sim_portfolio.json"):
        SimEngine(str(path))

    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("ledger", ["journal", "simulator"])
def test_failed_replace_preserves_previous_ledger(tmp_path, monkeypatch, ledger):
    path = tmp_path / f"{ledger}.json"
    if ledger == "journal":
        instance = Journal(path)
        instance.records = [{"date": "2026-09-26", "ticker": "AAA", "status": "open"}]
    else:
        instance = SimEngine(str(path))
    instance.save()
    previous = path.read_bytes()

    if ledger == "journal":
        instance.records.append({"date": "2026-09-27", "ticker": "BBB", "status": "open"})
    else:
        instance.state["cash"] = 123.45

    def replace_fails(_source, _destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", replace_fails)
    with pytest.raises(OSError, match="simulated replacement failure"):
        instance.save()

    assert path.read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_non_finite_value_does_not_replace_simulator_ledger(tmp_path):
    path = tmp_path / "sim_portfolio.json"
    instance = SimEngine(str(path))
    instance.save()
    previous = path.read_bytes()

    instance.state["cash"] = float("nan")
    with pytest.raises(ValueError, match="Out of range float"):
        instance.save()

    assert path.read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
