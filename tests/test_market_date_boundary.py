"""Regression for market-aware dates, actual source SHA bindings and safe guards."""
from __future__ import annotations

import datetime as datetime_module
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_system import agent_api as api, config, pipeline
from trading_system.markets import get_market
from trading_system.providers import demo as demo_module


BOUNDARY = "2026-10-04T17:11:00+00:00"


class FrozenDateTime(datetime):
    instant = datetime.fromisoformat(BOUNDARY)

    @classmethod
    def now(cls, tz=None):
        current = cls.instant.astimezone(tz) if tz is not None else cls.instant.astimezone().replace(tzinfo=None)
        return cls(current.year, current.month, current.day, current.hour,
                   current.minute, current.second, current.microsecond,
                   tzinfo=current.tzinfo, fold=current.fold)


class TimestampProxy:
    def __call__(self, *args, **kwargs):
        return pd.Timestamp(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(pd.Timestamp, name)

    @staticmethod
    def now(tz=None):
        return pd.Timestamp(FrozenDateTime.now(ZoneInfo(tz) if isinstance(tz, str) else tz))

    today = now


class PandasClockProxy:
    Timestamp = TimestampProxy()

    def __getattr__(self, name):
        return getattr(pd, name)


def freeze_clock(patch, instant=BOUNDARY):
    patch.setattr(FrozenDateTime, "instant", datetime.fromisoformat(instant))
    patch.setattr(pipeline, "datetime", FrozenDateTime)
    patch.setattr(api, "datetime", FrozenDateTime)
    patch.setattr(demo_module, "pd", PandasClockProxy())


def record(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_default_ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv("TS_OPTIONS_HIST_DIR", str(tmp_path / "options"))
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(tmp_path / "journal.json"))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(tmp_path / "calibration_samples.json"))


class DateResolved(RuntimeError):
    """A deliberate checkpoint after the actual pipeline date is resolved."""


def resolved_default_date(monkeypatch, tmp_path, market, instant, explicit=None, *, check_cutoff=False):
    freeze_clock(monkeypatch, instant)
    spec = get_market(market)
    observed = {}

    def stop_at_first_search(*args, **kwargs):
        observed.update(utcInstant=instant, hostDate=FrozenDateTime.now().date().isoformat(),
                        market=market, marketTimezone=spec.timezone,
                        marketDate=FrozenDateTime.now(ZoneInfo(spec.timezone)).date().isoformat(),
                        resolvedDate=pipeline._RUN_DIAGNOSTICS.get().as_of,
                        explicitTradeDate=explicit)
        if check_cutoff:
            raw = pd.DataFrame({"Open": [10., 11., 12.], "High": [12., 13., 14.],
                                "Low": [9., 10., 11.], "Close": [11., 12., 13.]},
                               index=pd.to_datetime(["2026-10-01", "2026-10-02", "2026-10-05"]))
            visible = pipeline._visible_data(raw)
            observed["visibleDates"] = [day.date().isoformat() for day in visible.index]
        record(tmp_path / "resolved-date.json", observed)
        raise DateResolved("Stop after the real production default and as-of cutoff, before any search")

    monkeypatch.setattr(pipeline, "SearchHub", stop_at_first_search)
    with pytest.raises(DateResolved):
        pipeline.run_pipeline(provider_name="demo", universe_mode="core", market=market,
                              llm_client=api.DisabledModelClient(), trade_date=explicit,
                              run_state_dir=str(tmp_path / "runtime"), ledger_dir=str(tmp_path / "ledgers"))
    return observed


@pytest.mark.parametrize("market,instant,expected", [
    ("us", BOUNDARY, "2026-10-02"),
    ("us", "2026-10-05T01:11:00+00:00", "2026-10-02"),
    ("us", "2026-10-05T04:01:00+00:00", "2026-10-05"),
    ("us", "2026-02-02T01:01:00+00:00", "2026-01-30"),
    ("us", "2027-01-01T01:01:00+00:00", "2026-12-31"),
    ("us", "2026-07-03T16:00:00+00:00", "2026-07-02"),
    ("us", "2026-03-08T06:30:00+00:00", "2026-03-06"),
    ("us", "2026-03-08T07:30:00+00:00", "2026-03-06"),
    ("us", "2026-11-01T05:30:00+00:00", "2026-10-30"),
    ("us", "2026-11-01T06:30:00+00:00", "2026-10-30"),
    ("cn", "2026-10-13T17:11:00+00:00", "2026-10-14"),
    ("hk", "2026-10-13T17:11:00+00:00", "2026-10-14"),
    ("cn", "2026-10-05T03:11:00+00:00", "2026-09-30"),
    ("hk", "2026-10-01T02:11:00+00:00", "2026-09-30"),
])
def test_production_default_uses_target_market_date(monkeypatch, tmp_path, market, instant, expected):
    observed = resolved_default_date(monkeypatch, tmp_path, market, instant)
    assert observed["resolvedDate"] == expected, observed


@pytest.mark.parametrize("explicit", ["2026-10-02", "2026-10-04"])
def test_explicit_trade_date_remains_explicit(monkeypatch, tmp_path, explicit):
    observed = resolved_default_date(monkeypatch, tmp_path, "us", BOUNDARY, explicit)
    assert observed["resolvedDate"] == explicit


def test_market_default_is_also_the_future_bar_cutoff(monkeypatch, tmp_path):
    observed = resolved_default_date(monkeypatch, tmp_path, "us", BOUNDARY, check_cutoff=True)
    assert observed["visibleDates"] == ["2026-10-01", "2026-10-02"], observed


@pytest.fixture(scope="module")
def source_corpus(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    kernel = Path(api.__file__).resolve().parent.parent
    patch.chdir(kernel)
    for name in ("CACHE_DIR", "REPORTS_DIR", "CALIBRATION_SAMPLES_PATH",
                 "CALIBRATION_JOURNAL_PATH", "REVIEW_PROPOSALS_DIR"):
        patch.setattr(config, name, getattr(config, name))
    workspace = api.workspace_at(str(tmp_path_factory.mktemp("mc201-actual-sources").resolve() / "workspace"),
                                 "local", kernel, create=True)
    corpus = {}
    try:
        for label, instant in (("boundary", BOUNDARY),
                               ("future", "2026-10-05T17:11:00+00:00"),
                               ("stale", "2026-09-29T17:11:00+00:00"),
                               ("legacy-fresh", "2026-10-02T10:11:00+00:00")):
            with patch.context() as clock_patch:
                freeze_clock(clock_patch, instant)
                selected = api.normalize_request({"operation": "pipeline", "mode": "daily",
                    "environment": "simulation", "provider": "demo", "llmMode": "disabled",
                    "idempotencyKey": "mc201" + label.replace("-", "") + "001", "topN": 10, "maxPicks": 3})
                receipt = api.run(selected, workspace, "local", kernel)
                assert receipt["status"] == "degraded", receipt.get("error")
                item = next(entry for entry in receipt["artifacts"] if entry["role"] == "pipeline-result")
                path = api.artifact_path(api.job_at(workspace, "local", receipt["jobId"]), item["name"])
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                assert actual_hash == item["sha256"] == receipt["resultSha256"]
                result = api.read_json(path)
                assert len(result["picks"]) >= 2, (label, result["picks"])
                corpus[label] = {"sourceJob": {"jobId": receipt["jobId"], "resultSha256": item["sha256"]},
                                 "path": str(path), "blob": result, "instant": instant}
        record(workspace.parent / "source-corpus.json", {
            label: {"sourceJob": value["sourceJob"], "path": value["path"],
                    "tradeDate": value["blob"]["trade_date"], "picks": len(value["blob"]["picks"]),
                    "generationInstant": value["instant"]} for label, value in corpus.items()})
        yield workspace, kernel, corpus
    finally:
        patch.undo()


def source_request(key, source):
    return api.normalize_request({"operation": "pipeline", "mode": "intraday", "environment": "simulation",
                                  "provider": "demo", "llmMode": "disabled", "idempotencyKey": key,
                                  "sourceJob": source, "topN": 10, "maxPicks": 3})


def healthy_quote(picks, calls):
    def quote(provider, ticker):
        calls.append(ticker)
        pick = next(item for item in picks if item["ticker"] == ticker)
        return {"price": pick["entry_price"] + pick["entry_price"] - pick["stop_price"],
                "ts": FrozenDateTime.now(timezone.utc).isoformat(), "kind": "realtime"}
    return quote


def test_actual_default_daily_source_sha_reaches_intraday_quotes(source_corpus, monkeypatch, tmp_path):
    workspace, kernel, corpus = source_corpus
    source = corpus["boundary"]
    freeze_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline, "quote_with_fallback", healthy_quote(source["blob"]["picks"], calls))
    receipt = api.run(source_request("mc201healthy001", source["sourceJob"]), workspace, "local", kernel)
    record(tmp_path / "actual-boundary-receipt.json", {"receipt": receipt, "quoteCalls": calls,
                                                      "sourceDate": source["blob"]["trade_date"],
                                                      "sourceJob": source["sourceJob"]})
    assert receipt["status"] == "succeeded", receipt.get("error")
    assert calls == [pick["ticker"] for pick in source["blob"]["picks"]]
    item = next(item for item in receipt["artifacts"] if item["name"] == "intraday.json")
    data = api.read_json(api.artifact_path(api.job_at(workspace, "local", receipt["jobId"]), item["name"]))
    assert data["alerts"] == []
    assert data["quoteCoverage"]["requested"] == data["quoteCoverage"]["ready"] == len(calls)
    assert data["quoteCoverage"]["missing"] == 0


@pytest.mark.parametrize("label", ["future", "stale"])
def test_actual_future_and_stale_source_jobs_still_stop_before_quotes(source_corpus, monkeypatch, tmp_path, label):
    workspace, kernel, corpus = source_corpus
    source = corpus[label]
    freeze_clock(monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline, "quote_with_fallback", healthy_quote(source["blob"]["picks"], calls))
    receipt = api.run(source_request("mc201guard" + label + "001", source["sourceJob"]), workspace, "local", kernel)
    record(tmp_path / "actual-source-guard.json", {"label": label, "receipt": receipt,
                                                   "sourceDate": source["blob"]["trade_date"], "quoteCalls": calls})
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "STALE_SOURCE_JOB"
    assert receipt["integrityVerified"] is False
    assert calls == []


def test_legacy_cli_accepts_an_actual_fresh_market_report(source_corpus, monkeypatch, tmp_path):
    import main as main_module
    workspace, kernel, corpus = source_corpus
    source = corpus["legacy-fresh"]
    freeze_clock(monkeypatch)
    monkeypatch.setattr(datetime_module, "datetime", FrozenDateTime)
    selected = tmp_path / ("result_" + source["blob"]["trade_date"].replace("-", "") + ".json")
    selected.write_bytes(Path(source["path"]).read_bytes())
    calls = []
    monkeypatch.setattr(pipeline, "quote_with_fallback", healthy_quote(source["blob"]["picks"], calls))
    arguments = SimpleNamespace(out=str(tmp_path), watch=None, interval=1, cycles=1, demo=True,
                                provider="demo", market="us")
    main_module._intraday(arguments, "demo")
    record(tmp_path / "legacy-fresh-observation.json", {"sourceDate": source["blob"]["trade_date"], "quoteCalls": calls})
    assert calls == [pick["ticker"] for pick in source["blob"]["picks"]]


def test_legacy_cli_still_rejects_an_actual_stale_market_report(source_corpus, monkeypatch, tmp_path):
    import main as main_module
    workspace, kernel, corpus = source_corpus
    source = corpus["stale"]
    freeze_clock(monkeypatch)
    monkeypatch.setattr(datetime_module, "datetime", FrozenDateTime)
    selected = tmp_path / ("result_" + source["blob"]["trade_date"].replace("-", "") + ".json")
    selected.write_bytes(Path(source["path"]).read_bytes())
    calls = []
    monkeypatch.setattr(pipeline, "quote_with_fallback", healthy_quote(source["blob"]["picks"], calls))
    arguments = SimpleNamespace(out=str(tmp_path), watch=None, interval=1, cycles=1, demo=True,
                                provider="demo", market="us")
    with pytest.raises(SystemExit, match="已过期|陈旧"):
        main_module._intraday(arguments, "demo")
    assert calls == []
