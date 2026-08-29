"""provider 降级链测试（v5.0 真实运行暴露问题修复的回归锁定）。"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_system import pipeline


class _DeadProvider:
    """模拟全面故障的主源（如 yahoo 被限流）。"""
    name = "yahoo"

    def ohlcv(self, ticker, days=400):
        raise ValueError(f"no data for {ticker}")

    def tnx_yield(self, days=400):
        raise ValueError("rate limited")

    def vix(self, days=400):
        raise ValueError("rate limited")

    def vix9d(self, days=400):
        raise ValueError("rate limited")


class _FakeStooq:
    name = "stooq"

    def ohlcv(self, ticker, days=400):
        return pd.DataFrame({"Close": [100.0, 101.0, 102.0]})

    def tnx_yield(self, days=400):
        return pd.Series([4.1, 4.2])

    def vix(self, days=400):
        return pd.Series([15.0, 16.0])

    def vix9d(self, days=400):
        return pd.Series([14.0, 15.0])


def test_single_fallback_to_stooq(monkeypatch):
    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider", _FakeStooq)
    dead = _DeadProvider()
    df = pipeline._single_with_fallback(dead, "ohlcv", "SPY", days=10)
    assert len(df) == 3
    tnx = pipeline._single_with_fallback(dead, "tnx_yield", days=10)
    assert list(tnx) == [4.1, 4.2]


def test_single_fallback_stooq_failure_propagates(monkeypatch):
    """全链皆败时异常必须上抛（数据地基缺失不能瞎跑）。"""
    monkeypatch.setattr(pipeline, "_channel_chain", lambda: [])   # 服务端通道不可用场景
    class _DeadStooq(_DeadProvider):
        name = "stooq"
    with pytest.raises(ValueError):
        pipeline._single_with_fallback(_DeadStooq(), "ohlcv", "SPY", days=10)
