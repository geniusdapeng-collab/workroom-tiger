"""Yahoo's published ^TNX series is already a percentage yield."""

import pandas as pd
import pytest

from trading_system.indicators import score_tnx_20d_chg_bp, tnx_trend_label
from trading_system.providers.yahoo import YahooProvider


@pytest.mark.parametrize("values", [[5.098, 5.136], [0.52, 0.55], [15.0, 15.25]])
def test_tnx_preserves_published_percentage_units(monkeypatch, values):
    """Normal, low-rate and high-rate regimes share the same provider unit."""
    raw = pd.DataFrame({"Close": values}, index=pd.date_range("2026-10-16", periods=2))
    provider = YahooProvider()
    requested = []

    def download(ticker, days):
        requested.append((ticker, days))
        return raw

    monkeypatch.setattr(provider, "_download", download)
    pd.testing.assert_series_equal(provider.tnx_yield(60), raw["Close"])
    assert requested == [("^TNX", 60)]


def test_tnx_percentage_move_reaches_existing_macro_thresholds(monkeypatch):
    """4.8% to 5.1% is +30bp, not +3bp; scoring rules stay unchanged."""
    raw = pd.DataFrame({"Close": [4.8] * 20 + [5.1]},
                       index=pd.bdate_range("2026-09-21", periods=21))
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_download", lambda ticker, days: raw)
    series = provider.tnx_yield(21)
    change_bp = (series.iloc[-1] - series.iloc[-21]) * 100
    assert change_bp == pytest.approx(30.0)
    assert score_tnx_20d_chg_bp(change_bp) == 1
    assert tnx_trend_label(change_bp) == "Up"


def test_tnx_missing_values_remain_missing(monkeypatch):
    raw = pd.DataFrame({"Close": [5.098, float("nan"), 5.136]})
    provider = YahooProvider()
    monkeypatch.setattr(provider, "_download", lambda ticker, days: raw)
    pd.testing.assert_series_equal(provider.tnx_yield(3), raw["Close"].dropna())


def test_tnx_fetch_failure_is_propagated_to_fallback(monkeypatch):
    provider = YahooProvider()

    def fail(ticker, days):
        raise TimeoutError("upstream timed out")

    monkeypatch.setattr(provider, "_download", fail)
    with pytest.raises(TimeoutError, match="upstream timed out"):
        provider.tnx_yield(30)
