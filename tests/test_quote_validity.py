"""MC-006: only valid, dated quotes may reach current-price alerts.

Providers and clocks are isolated so no market endpoint, account, or model API
is contacted. The three-calendar-day allowance remains the existing policy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_system import pipeline
from trading_system.providers.yahoo import YahooProvider
from trading_system.triggers import monitor_once


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
WATCH = [{"ticker": "XYZ", "entry": 100.0, "stop": 95.0, "protect": 110.0}]


class _Clock(datetime):
    value = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz is not None else cls.value.replace(tzinfo=None)


class _Provider:
    synthetic = False

    def __init__(self, name="stooq", quote=None, *, calls=None, error=None):
        self.name = name
        self.result = quote
        self.calls = calls if calls is not None else []
        self.error = error

    def quote(self, ticker):
        self.calls.append((self.name, ticker))
        if self.error is not None:
            raise self.error
        return self.result


def _quote(**fields):
    return {"price": 100.3, "ts": "2026-10-20T11:00:00+00:00",
            "kind": "realtime", **fields}


@pytest.fixture(autouse=True)
def _isolated_quote_sources(monkeypatch):
    monkeypatch.setattr(_Clock, "value", NOW)
    monkeypatch.setattr(pipeline, "datetime", _Clock)
    monkeypatch.setattr(pipeline, "_LINEAGE", [])
    monkeypatch.setattr(pipeline, "_HEALTH", {})
    monkeypatch.setattr(pipeline, "_DATA_ISSUES", [])
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider",
                        lambda: _Provider())


@pytest.mark.parametrize("kind", ["realtime", "realtime_delayed", "eod_close"])
def test_old_quote_of_every_kind_falls_through(monkeypatch, kind):
    calls = []
    primary = _Provider(quote=_quote(kind=kind, ts="2026-10-09 16:00:00"), calls=calls)
    fresh = _quote(price=101.25)
    fallback = _Provider("tencent", fresh, calls=calls)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fallback])

    assert pipeline.quote_with_fallback(primary, "XYZ") is fresh
    assert calls == [("stooq", "XYZ"), ("tencent", "XYZ")]
    assert any(i["issue"] == "stale_or_unverifiable_quote" for i in pipeline._DATA_ISSUES)


@pytest.mark.parametrize("kind", ["realtime", "realtime_delayed", "eod_close"])
@pytest.mark.parametrize("ts", [None, "", "N/D", "12:00:00", "2026-02-30",
                               "2026-10-20junk", "2026-10-20 99:00:00",
                               True, 1792494000, pd.NaT])
def test_unverifiable_timestamp_cannot_be_accepted(monkeypatch, kind, ts):
    fresh = _quote(price=102.0)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [_Provider("tencent", fresh)])
    assert pipeline.quote_with_fallback(_Provider(quote=_quote(kind=kind, ts=ts)), "XYZ") is fresh


def test_missing_timestamp_is_not_current():
    q = _quote()
    del q["ts"]
    result = pipeline.quote_with_fallback(_Provider(quote=q), "XYZ")
    assert result is None or result.get("stale") is True
    assert monitor_once(WATCH, _Provider(quote=q)) == []


@pytest.mark.parametrize("ts", ["2026-10-21", "2026-10-20T12:00:01",
                               "2026-10-20T11:30:00-04:00",
                               "2026-10-21T05:00:00+02:00"])
def test_future_quotes_are_unusable(ts):
    q = _quote(ts=ts)
    assert pipeline._quote_stale(q) is True
    assert monitor_once(WATCH, _Provider(quote=q)) == []


def test_explicit_stale_flag_cannot_short_circuit_fallback(monkeypatch):
    stale = _quote(stale=True)
    fresh = _quote(price=99.75)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [_Provider("sina", fresh)])
    assert pipeline.quote_with_fallback(_Provider(quote=stale), "XYZ") is fresh


@pytest.mark.parametrize("price", [None, 0, -100.0, float("nan"), float("inf"),
                                  -float("inf"), True, False, np.bool_(True),
                                  "nan", "inf", "100.0", [], {}, 1 + 2j])
def test_invalid_price_falls_through_without_reaching_alerts(monkeypatch, price):
    bad = _quote(price=price)
    primary = _Provider(quote=bad)
    assert pipeline.quote_with_fallback(primary, "XYZ") is None
    assert monitor_once(WATCH, primary) == []
    fresh = _quote(price=105.0)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [_Provider("sina", fresh)])
    assert pipeline.quote_with_fallback(primary, "XYZ") is fresh
    assert any(i["issue"] == "invalid_price" for i in pipeline._DATA_ISSUES)


@pytest.mark.parametrize("payload", [None, {}, [], [1], "bad", 42,
                                    pd.DataFrame({"price": [100]})])
def test_invalid_quote_container_does_not_break_fallback(monkeypatch, payload):
    fresh = _quote()
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [_Provider("sina", fresh)])
    assert pipeline.quote_with_fallback(_Provider(quote=payload), "XYZ") is fresh


@pytest.mark.parametrize("ts,kind", [
    ("2026-10-19", "eod_close"),
    ("2026-10-20 11:00:00", "realtime"),
    ("2026-10-20 11:00", "realtime_delayed"),
    ("2026-10-20T11:00:00.123456", "realtime"),
    ("2026-10-20T11:00:00Z", "realtime"),
    ("2026-10-20T13:00:00+02:00", "realtime"),
    ("2026-10-20T08:00:00-04:00", "realtime"),
])
@pytest.mark.parametrize("price", [100, 100.25, np.float64(100.5)])
def test_normal_dated_quote_is_preserved(ts, kind, price):
    q = _quote(ts=ts, kind=kind, price=price)
    assert pipeline.quote_with_fallback(_Provider(quote=q), "XYZ") is q
    assert pipeline._LINEAGE == [(f"quote(XYZ)[{kind}]", "stooq")]
    assert pipeline._DATA_ISSUES == []


@pytest.mark.parametrize("kind", ["realtime", "realtime_delayed", "eod_close"])
def test_three_calendar_day_boundary_remains_unchanged(kind):
    assert pipeline._quote_stale(_quote(kind=kind, ts="2026-10-17")) is False
    assert pipeline._quote_stale(_quote(kind=kind, ts="2026-10-16")) is True


def test_weekend_uses_existing_previous_trading_day_allowance(monkeypatch):
    monkeypatch.setattr(_Clock, "value", datetime(2026, 10, 25, 12, tzinfo=timezone.utc))
    assert pipeline._quote_stale(_quote(kind="eod_close", ts="2026-10-20")) is False
    assert pipeline._quote_stale(_quote(kind="eod_close", ts="2026-10-19")) is True


def test_timezone_does_not_change_existing_calendar_reference(monkeypatch):
    monkeypatch.setattr(_Clock, "value", datetime(2026, 10, 20, 1, tzinfo=timezone.utc))
    # Source-local clock is still 10/19, but the pre-existing reference is the
    # host's previous trading date (10/20). The same dated EOD stays four days old.
    q = _quote(kind="eod_close", ts="2026-10-16T16:00:00-04:00")
    assert pipeline._quote_stale(q) is True


@pytest.mark.parametrize("flag", [pd.NA, np.array([False, True])])
def test_malformed_stale_flag_fails_closed(flag):
    assert monitor_once(WATCH, _Provider(quote=_quote(stale=flag))) == []


def test_stale_result_is_disclosed_without_mutating_source_payload(monkeypatch):
    first = _quote(ts="2026-10-09", kind="eod_close")
    second = _quote(ts="2026-10-08", price=88.0, kind="eod_close")
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [_Provider("sina", second)])

    result = pipeline.quote_with_fallback(_Provider(quote=first), "XYZ")

    assert result == {**first, "stale": True}
    assert "stale" not in first and "stale" not in second
    assert pipeline._LINEAGE == [("quote(XYZ)[stale]", "STALE")]


def test_primary_success_does_not_probe_other_providers(monkeypatch):
    def unexpected_chain():
        pytest.fail("valid primary quote must not probe unused providers")

    monkeypatch.setattr(pipeline, "_channel_chain", unexpected_chain)
    q = _quote()
    assert pipeline.quote_with_fallback(_Provider("yahoo", q), "XYZ") is q


def test_fallback_order_survives_exceptions_and_invalid_middle_sources(monkeypatch):
    calls = []
    primary = _Provider("yahoo", calls=calls)
    stooq = _Provider("stooq", calls=calls, error=TimeoutError("unavailable"))
    stale = _Provider("tencent", _quote(ts="2026-10-01"), calls=calls)
    fresh = _quote(price=123.25)
    final = _Provider("sina", fresh, calls=calls)
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", lambda: stooq)
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [primary, stale, final])

    assert pipeline.quote_with_fallback(primary, "XYZ") is fresh
    assert calls == [("yahoo", "XYZ"), ("stooq", "XYZ"),
                     ("tencent", "XYZ"), ("sina", "XYZ")]
    assert any(i["issue"] == "provider_error" and i["provider"] == "stooq"
               for i in pipeline._DATA_ISSUES)


def test_real_request_rejects_renamed_synthetic_fallback(monkeypatch):
    fake = _Provider("renamed", _quote(price=1.0))
    fake.synthetic = True
    fresh = _quote()
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fake, _Provider("sina", fresh)])
    assert pipeline.quote_with_fallback(_Provider(), "XYZ") is fresh
    assert fake.calls == []
    assert any(i["issue"] == "synthetic_fallback_rejected" for i in pipeline._DATA_ISSUES)

    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [fake])
    assert pipeline.quote_with_fallback(_Provider(), "XYZ") is None
    assert monitor_once(WATCH, _Provider()) == []


def test_explicit_synthetic_primary_remains_labeled_demo():
    q = _quote()
    demo = _Provider("renamed", q)
    demo.synthetic = True
    assert pipeline.quote_with_fallback(demo, "XYZ") is q
    assert pipeline._LINEAGE == [("quote(XYZ)[realtime]", "demo")]


@pytest.mark.parametrize("price,kind", [(100.3, "ENTRY"), (94.0, "STOP"), (110.5, "PROTECT")])
def test_current_quotes_preserve_alert_policy_and_deduplication(price, kind):
    provider = _Provider(quote=_quote(price=price))
    triggered = set()
    alerts = monitor_once(WATCH, provider, triggered)
    assert [a["kind"] for a in alerts] == [kind]
    assert alerts[0]["price"] == price
    assert monitor_once(WATCH, provider, triggered) == []


def _yahoo_fixture(monkeypatch, metadata, *, actual_fast_info=False, price=123.45):
    import yfinance as yf

    # The real lazy mapping locks down its public key contract without network.
    if actual_fast_info:
        from yfinance.scrapers.quote import FastInfo
        fast_info = FastInfo(None)
        fast_info._last_price = price
    else:
        fast_info = {"last_price": price, "lastPrice": price}

    def get_metadata():
        if isinstance(metadata, Exception):
            raise metadata
        return metadata

    ticker = SimpleNamespace(fast_info=fast_info, get_history_metadata=get_metadata)
    seen = []

    def get_ticker(symbol):
        seen.append(symbol)
        return ticker

    monkeypatch.setattr(yf, "Ticker", get_ticker)
    provider = YahooProvider()
    bars = pd.DataFrame({"Close": [98.0]}, index=pd.DatetimeIndex(["2026-10-19"]))
    monkeypatch.setattr(provider, "ohlcv", lambda ticker, days=400: bars)
    return provider, seen


@pytest.mark.parametrize("source_time", [
    1792438200,  # 2026-10-19T19:30:00Z, raw Yahoo epoch seconds
    pd.Timestamp("2026-10-19T16:50:00-04:00"),
    datetime(2026, 10, 19, 20, 50, tzinfo=timezone.utc),
])
def test_yahoo_preserves_source_market_timestamp(monkeypatch, source_time):
    provider, seen = _yahoo_fixture(monkeypatch, {"regularMarketTime": source_time})

    q = provider.quote("XYZ")

    expected = (pd.Timestamp(source_time, unit="s", tz="UTC")
                if isinstance(source_time, int) else pd.Timestamp(source_time))
    assert q["price"] == 123.45 and q["kind"] == "realtime"
    assert pd.Timestamp(q["ts"]) == expected
    assert seen == ["XYZ"]


def test_yahoo_uses_real_fast_info_public_price_key(monkeypatch):
    provider, _ = _yahoo_fixture(monkeypatch, {"regularMarketTime": 1792438200},
                                 actual_fast_info=True)
    q = provider.quote("XYZ")
    assert q["price"] == 123.45 and q["kind"] == "realtime"


@pytest.mark.parametrize("source_time", [None, "", "bad", True, 0, -1, float("nan"),
                                        float("inf"), pd.NaT, pd.Timestamp("2026-10-19"),
                                        "2026-10-19"])
def test_yahoo_missing_or_invalid_market_time_uses_dated_eod(monkeypatch, source_time):
    provider, _ = _yahoo_fixture(monkeypatch, {"regularMarketTime": source_time})
    assert provider.quote("XYZ") == {"price": 98.0, "ts": "2026-10-19", "kind": "eod_close"}


@pytest.mark.parametrize("metadata", [{}, None, TimeoutError("metadata unavailable")])
def test_yahoo_unavailable_metadata_uses_existing_eod_fallback(monkeypatch, metadata):
    provider, _ = _yahoo_fixture(monkeypatch, metadata)
    assert provider.quote("XYZ") == {"price": 98.0, "ts": "2026-10-19", "kind": "eod_close"}


@pytest.mark.parametrize("price", [True, np.bool_(True), float("nan"), float("inf"), -1, 0])
def test_yahoo_invalid_price_cannot_be_normalized_into_valid_quote(monkeypatch, price):
    provider, _ = _yahoo_fixture(monkeypatch, {"regularMarketTime": 1792438200}, price=price)
    assert provider.quote("XYZ") == {"price": 98.0, "ts": "2026-10-19", "kind": "eod_close"}


def test_yahoo_without_verifiable_time_or_eod_returns_none(monkeypatch):
    provider, _ = _yahoo_fixture(monkeypatch, {})
    monkeypatch.setattr(provider, "ohlcv", lambda ticker, days=400: pd.DataFrame())
    assert provider.quote("XYZ") is None


def test_yahoo_old_market_time_cannot_be_restamped_into_current_alert(monkeypatch):
    provider, _ = _yahoo_fixture(monkeypatch, {
        "regularMarketTime": pd.Timestamp("2026-10-01T15:00:00-04:00")}, price=94.0)
    q = pipeline.quote_with_fallback(provider, "XYZ")
    assert q["stale"] is True
    assert pd.Timestamp(q["ts"]) == pd.Timestamp("2026-10-01T15:00:00-04:00")
    assert monitor_once(WATCH, provider) == []


def test_yahoo_options_use_the_public_fast_price_for_atm_selection(monkeypatch):
    import yfinance as yf
    from yfinance.scrapers.quote import FastInfo

    fast_info = FastInfo(None)
    fast_info._last_price = 100.0
    calls = pd.DataFrame({
        "strike": [90.0, 100.0, 110.0],
        "impliedVolatility": [0.9, 0.2, 0.8],
        "openInterest": [10.0, 10.0, 10.0],
        "volume": [5.0, 5.0, 5.0],
    })
    ticker = SimpleNamespace(
        fast_info=fast_info, options=("2026-10-23",),
        option_chain=lambda expiry: SimpleNamespace(calls=calls, puts=calls.copy()),
    )
    monkeypatch.setattr(yf, "Ticker", lambda symbol: ticker)
    provider = YahooProvider()
    eod_requests = []

    def previous_close(symbol, days=400):
        eod_requests.append((symbol, days))
        return pd.DataFrame({"Close": [110.0]})

    monkeypatch.setattr(provider, "ohlcv", previous_close)
    snapshot = provider.options_chain_snapshot("XYZ")

    assert snapshot["atm_iv"] == pytest.approx(0.2)
    assert eod_requests == []
