"""Offline Yahoo transport fixtures test the real strict factory and daily engine.

The same OHLCV values are returned with yfinance's daily ignore_tz semantics;
these fixtures are test data, never evidence of live market observations.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yfinance as yf

import main
from trading_system import pipeline, providers
from trading_system.agent_evidence import normalize, read_snapshot, verify_events
from trading_system.providers import get_provider as real_get_provider
from trading_system.providers.yahoo import YahooProvider
from trading_system.redline.execution import (
    RecordingProvider, StrictObservationError, daily_stage_contract,
)
from test_strict_daily_core import daily_fixture
from test_strict_execution_core import make_execution


GLOBAL_SYMBOLS = ["000660.KS", "005930.KS", "2330.TW", "GFS", "UMC"]


class YahooDownloadFixture:
    """Only replace Yahoo transport, keeping factory/provider/recording real."""

    def __init__(self, common_timezone=None):
        self.common_timezone = common_timezone
        self.frames = {}
        self.calls = []

    def frame(self, ticker):
        if ticker not in self.frames:
            timezone = ("Asia/Seoul" if ticker.endswith(".KS") else
                        "Asia/Taipei" if ticker.endswith(".TW") else "America/New_York")
            last_date = "2026-10-21" if ticker.endswith((".KS", ".TW")) else "2026-10-20"
            index = pd.bdate_range(end=last_date, periods=280, tz=timezone, name="Date")
            step = np.arange(len(index))
            close = 70 + step * (.08 + sum(map(ord, ticker)) % 7 * .005) + np.sin(step / 9) * .2
            if ticker in {"^TNX", "^VIX", "^VIX9D"}:
                close = np.full(len(index), {"^TNX": 4.1, "^VIX": 19., "^VIX9D": 18.}[ticker])
            self.frames[ticker] = pd.DataFrame({
                "Open": close * .999, "High": close * 1.01, "Low": close * .99,
                "Close": close, "Volume": np.full(len(index), 2_000_000),
            }, index=index)
        return self.frames[ticker].copy(deep=True)

    def download(self, tickers, **kwargs):
        symbols = tickers.split()
        frames = {ticker: self.frame(ticker) for ticker in symbols}
        # Day+ defaults strip each exchange's timezone before combining rows.
        # Aware results may use UTC (older yfinance) or a common exchange zone.
        if kwargs.get("ignore_tz", True):
            for frame in frames.values():
                frame.index = frame.index.tz_localize(None)
        else:
            zone = self.common_timezone or Counter(
                str(frame.index.tz) for frame in frames.values()).most_common(1)[0][0]
            for frame in frames.values():
                frame.index = frame.index.tz_convert(zone)
        raw = pd.concat(frames, axis=1, names=["Ticker", "Price"], sort=True).sort_index()
        if kwargs.get("group_by") != "ticker":
            raw = raw.swaplevel(axis=1)
        self.calls.append({"tickers": symbols, "kwargs": dict(kwargs), "raw": raw.copy(deep=True)})
        return raw

    def install(self, monkeypatch):
        monkeypatch.setattr(yf, "download", self.download)
        monkeypatch.setattr(yf, "Ticker", lambda ticker: SimpleNamespace(options=()))


def _io_output(execution, root, stage_name, method):
    record = next(row for row in execution.summary() if row["stage"] == stage_name)
    stage = read_snapshot(root, record["output_ref"], run_id=execution.run_id,
                          stage=stage_name, role="output")
    call = next(call for call in stage["io_calls"] if call["method"] == method)
    return read_snapshot(root, call["output_ref"], run_id=execution.run_id,
                         stage=stage_name, role="output")


def _assert_projection(actual, original, raw):
    assert actual.index.tz is None
    pd.testing.assert_index_equal(actual.index, raw.index.tz_convert("America/New_York").tz_localize(None))
    assert actual.to_numpy().tobytes() == original.to_numpy().tobytes()
    assert max(actual.index.date) <= date(2026, 10, 20)


@pytest.mark.parametrize("common_timezone", ["UTC", "America/New_York", "Asia/Seoul"])
def test_strict_mixed_global_batch_keeps_same_prices_and_raw_timezone(tmp_path, monkeypatch, common_timezone):
    transport = YahooDownloadFixture(common_timezone)
    transport.install(monkeypatch)
    execution, root = make_execution(tmp_path)
    provider = real_get_provider("yahoo", execution=execution)
    assert isinstance(provider, RecordingProvider) and isinstance(provider._provider, YahooProvider)
    originals = {ticker: transport.frame(ticker).tail(90) for ticker in GLOBAL_SYMBOLS}
    with execution.step("daily.preflight", {}) as stage:
        actual = provider.ohlcv_batch(GLOBAL_SYMBOLS, days=90)
        stage.output(actual)
    raw = _io_output(execution, root, "daily.preflight", "provider.yahoo.ohlcv_batch")
    for ticker, original in originals.items():
        expected_raw = original.astype(float)
        expected_raw.index = expected_raw.index.tz_convert(common_timezone)
        expected_raw.columns.name = "Price"
        assert raw[ticker] == normalize(expected_raw)
        _assert_projection(actual[ticker], original, expected_raw)
        pd.testing.assert_frame_equal(transport.frame(ticker).tail(90), original)
    assert transport.calls[0]["kwargs"]["ignore_tz"] is False
    assert actual["000660.KS"].index[-1].date() == date(2026, 10, 20)


@pytest.mark.parametrize("method,args,ticker", [
    ("ohlcv", ("2330.TW",), "2330.TW"),
    ("tnx_yield", (), "^TNX"), ("vix", (), "^VIX"), ("vix9d", (), "^VIX9D"),
    ("rate_yield_for", ("TNX",), "^TNX"),
    ("vol_index_for", ("VIX",), "^VIX"),
    ("vol_index_for", ("VIX9D",), "^VIX9D"),
])
def test_strict_single_and_macro_preserve_raw_timezone(tmp_path, monkeypatch, method, args, ticker):
    transport = YahooDownloadFixture()
    transport.install(monkeypatch)
    execution, root = make_execution(tmp_path)
    original = transport.frame(ticker).tail(90)
    expected_raw = original.copy()
    expected_raw.columns.name = None
    if method != "ohlcv":
        expected_raw = expected_raw["Close"]
    with execution.step("daily.preflight", {}) as stage:
        actual = getattr(real_get_provider("yahoo", execution=execution), method)(*args, days=90)
        stage.output(actual)
    assert _io_output(execution, root, "daily.preflight", f"provider.yahoo.{method}") == normalize(expected_raw)
    _assert_projection(actual, original if method == "ohlcv" else original["Close"], expected_raw)
    assert transport.calls[0]["kwargs"]["ignore_tz"] is False


@pytest.mark.parametrize("method,ticker", [("ohlcv", "SPY"), ("ohlcv", "2330.TW"),
    ("ohlcv_batch", "005930.KS"), ("ohlcv_batch", "GFS"), ("vix9d", "^VIX9D")])
def test_future_us_and_global_observations_still_reject_and_persist_raw(tmp_path, monkeypatch, method, ticker):
    transport = YahooDownloadFixture()
    transport.install(monkeypatch)
    original = transport.frame(ticker)
    future = original.copy()
    future.index = future.index + pd.Timedelta(days=7)
    transport.frames[ticker] = future
    execution, root = make_execution(tmp_path)
    provider = real_get_provider("yahoo", execution=execution)
    args = ([ticker],) if method == "ohlcv_batch" else () if method == "vix9d" else (ticker,)
    with pytest.raises(StrictObservationError):
        with execution.step("daily.preflight", {}) as stage:
            getattr(provider, method)(*args, days=90)
            stage.output({})
    expected_raw = future.tail(90).copy()
    expected_raw.columns.name = "Price" if method == "ohlcv_batch" else None
    if transport.calls[-1]["kwargs"].get("ignore_tz", True):
        expected_raw.index = expected_raw.index.tz_localize(None)
    expected = ({ticker: normalize(expected_raw)} if method == "ohlcv_batch" else
                normalize(expected_raw["Close"] if method == "vix9d" else expected_raw))
    snapshots = [read_snapshot(root, {"path": str(path.relative_to(root)),
        "sha256": path.stem, "bytes": path.stat().st_size, "schema": "tiger.snapshot.v1"},
        run_id=execution.run_id, stage="daily.preflight", role="output")
        for path in (root / "evidence").glob("*.json")
        if '"role":"output"' in path.read_text()]
    assert expected in snapshots
    assert execution.summary()[-1]["status"] == "failed"
    assert not (root / "receipt.json").exists()


@pytest.mark.parametrize("factory", [YahooProvider, lambda: real_get_provider("yahoo")])
def test_legacy_default_preserves_naive_index_shape_and_value_bytes(monkeypatch, factory):
    transport = YahooDownloadFixture()
    transport.install(monkeypatch)
    provider = factory()
    actual = provider.ohlcv("2330.TW", days=90)
    expected = transport.frame("2330.TW").tail(90)
    expected.index = expected.index.tz_localize(None)
    expected.columns.name = None
    pd.testing.assert_frame_equal(actual, expected, check_freq=False)
    assert actual.to_numpy().tobytes() == expected.to_numpy().tobytes()
    batch = provider.ohlcv_batch(GLOBAL_SYMBOLS, days=90)
    for ticker in GLOBAL_SYMBOLS:
        expected = transport.frame(ticker).tail(90)
        expected.index = expected.index.tz_localize(None)
        expected.columns.name = "Price"
        # Combining exchange calendars introduces NaNs, as on the legacy path.
        # OHLCV normalization keeps the historical union-frame float dtypes.
        expected = expected.astype(float)
        pd.testing.assert_frame_equal(batch[ticker], expected, check_freq=False)
        assert batch[ticker].to_numpy().tobytes() == expected.to_numpy().tobytes()
    assert all("ignore_tz" not in call["kwargs"] for call in transport.calls)


def test_real_yahoo_strict_factory_completes_all_39_daily_stages(tmp_path, monkeypatch):
    execution, root, args, _, network = daily_fixture(tmp_path, monkeypatch)
    # Restore both factory imports; replace only transport, search and temp IO.
    monkeypatch.setattr(pipeline, "get_provider", real_get_provider)
    monkeypatch.setattr(providers, "get_provider", real_get_provider)
    transport = YahooDownloadFixture()
    transport.install(monkeypatch)
    output = main._daily(args, "yahoo", execution=execution)
    execution.assert_complete()
    verify_events(root, execution.run_id, daily_stage_contract())
    assert len(execution.summary()) == 39
    assert len((root / "events.jsonl").read_text().splitlines()) == 78
    assert output.result.trade_date == "2026-10-20"
    assert output.result.raw["freshness"]["benchmark_last_bar"] == "2026-10-20"
    assert output.sim_state["equity_curve"][-1]["date"] == "2026-10-20"
    assert network == [] and all(call["kwargs"]["ignore_tz"] is False for call in transport.calls)
    monitor = _io_output(execution, root, "tech.monitor", "provider.yahoo.ohlcv_batch")
    for ticker in ("000660.KS", "005930.KS", "2330.TW"):
        frame = monitor[ticker]
        index = pd.DatetimeIndex([row["value"] for row in frame["index"]])
        assert index.tz is not None
        assert index[-1].tz_convert("America/New_York").date() == date(2026, 10, 20)
        expected = transport.frame(ticker).tail(90)
        assert frame["values"] == normalize(expected)["values"]
