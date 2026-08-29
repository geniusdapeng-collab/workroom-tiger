"""AgentGW 服务端行情通道测试（限流根修）。

不访问真实网络：monkeypatch SDK 的 call_data_source_tool。
锁定：CSV 解析契约、TNX 单位自适配、批量墙钟预算、降级链末环接入。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from trading_system.providers.agentgw import AgentGwProvider

import pandas as _pd

_dates = _pd.date_range("2026-05-20", periods=60, freq="B").strftime("%Y-%m-%dT04:00:00.000Z")
_rows = "\n".join(
    f"{dt},{100+i},{102+i},{99+i},{101+i},1000000,0,0,0,FAKE,NA,NA,USD"
    for i, dt in enumerate(_dates))
_CSV = ("Date,Open,High,Low,Close,Volume,Dividends,Stock Splits,Capital Gains,"
        "thscode,thsname_cn,thsname_en,currency\n" + _rows + "\n")

_TNX_CSV_HIGH = ("Date,Open,High,Low,Close,Volume\n"
                 "2026-07-28T05:00:00.000Z,46.1,46.5,46.0,46.2,0\n"
                 "2026-07-29T05:00:00.000Z,46.2,46.6,46.1,46.4,0\n")
_TNX_CSV_PCT = ("Date,Open,High,Low,Close,Volume\n"
                "2026-07-28T05:00:00.000Z,4.61,4.65,4.60,4.62,0\n"
                "2026-07-29T05:00:00.000Z,4.62,4.66,4.61,4.64,0\n")


class _Resp:
    def __init__(self, csv_text: str):
        self.is_success = bool(csv_text)
        self.text = json.dumps({"data_preview": csv_text}) if csv_text else "EMPTY"


@pytest.fixture
def provider(monkeypatch):
    p = AgentGwProvider.__new__(AgentGwProvider)   # 跳过 __init__ 的 SDK 导入
    p.per_call_timeout, p.batch_budget_s, p.pause_s = 40.0, 240.0, 0.0
    p._cache = {}
    calls = []

    def fake_call(params):
        import time as _t
        _t.sleep(0.05)                      # 模拟网络延迟，避免瞬时完成的竞态
        calls.append(params["params"]["ticker"])
        tk = params["params"]["ticker"]
        if tk == "^TNX":
            return _Resp(_TNX_CSV_PCT)
        if tk == "^VIX":
            return _Resp(_CSV)
        return _Resp(_CSV)

    p._api = type("T", (), {"call_data_source_tool": staticmethod(fake_call)})()
    p._calls = calls
    return p


def test_ohlcv_parse_contract(provider):
    df = provider.ohlcv("FAKE", days=30)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 60
    assert float(df["Close"].iloc[-1]) == 160.0
    assert df.index.is_monotonic_increasing


def test_tnx_unit_autodetect(provider, monkeypatch):
    s = provider.tnx_yield(days=30)
    assert 4.0 < float(s.iloc[-1]) < 6.0, "真实收益率口径不得再除 10"
    # ×10 口径必须还原
    provider._api = type("T", (), {"call_data_source_tool":
                         staticmethod(lambda p: _Resp(_TNX_CSV_HIGH))})()
    provider._cache.clear()
    s2 = provider.tnx_yield(days=30)
    assert 4.0 < float(s2.iloc[-1]) < 6.0, "×10 口径必须还原为百分数"


def test_batch_budget_and_partial(provider, monkeypatch):
    provider.batch_budget_s = -1   # 预算立即耗尽：至多放走已完成的个别任务
    out = provider.ohlcv_batch(["A", "B", "C", "D", "E"], days=30)
    assert len(out) <= 1, f"墙钟预算必须截断批量，实际完成 {len(out)}"
    provider.batch_budget_s = 240
    out = provider.ohlcv_batch(["A", "B"], days=30)
    assert set(out) == {"A", "B"}


def test_inrun_cache(provider):
    provider.ohlcv("FAKE", days=30)
    provider.ohlcv("FAKE", days=30)
    assert provider._calls.count("FAKE") == 1, "本轮内必须复用缓存（零基线：不落盘、不跨轮）"


def test_fallback_chain_ends_at_agentgw(monkeypatch):
    """yahoo/stooq 双败时，单只降级链必须落到 agentgw 服务端通道。"""
    from trading_system import pipeline as pl

    class _Broken:
        name = "broken"

        def ohlcv(self, *a, **k):
            raise RuntimeError("rate limited")

    monkeypatch.setattr("trading_system.providers.stooq.StooqProvider.ohlcv",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    sentinel = pd.DataFrame({"Open": [1], "High": [1], "Low": [1],
                             "Close": [1], "Volume": [1]})
    fake_ag = type("AG", (), {"name": "agentgw", "ohlcv": lambda self, *a, **k: sentinel})()
    monkeypatch.setattr(pl, "_channel_chain", lambda: [fake_ag])
    out = pl._single_with_fallback(_Broken(), "ohlcv", "SPY", days=5)
    assert out is sentinel
