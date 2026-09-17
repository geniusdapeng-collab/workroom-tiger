"""official 官方宏观源（FRED DGS10 / CBOE VIX·VIX9D）测试。

纪律：真实源、不编造——数据异常必须抛错（由降级链处理），绝不出假数。
网络相关用例全部 mock；真实连通性仅做可选 smoke（CI 无外网时跳过）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from trading_system.providers.official import OfficialMacroProvider


_DAYS = [f"2026-07-{d:02d}" for d in range(1, 32)] + [f"2026-08-{d:02d}" for d in range(1, 29)]
FRED_CSV = "DATE,DGS10\n" + "\n".join(
    f"{dt},4.6{i % 10}" for i, dt in enumerate(_DAYS)
) + "\n2026-08-28,.\n"  # 末尾一个官方缺失点，应被剔除

_MD = [f"07/{d:02d}/2026" for d in range(1, 32)] + [f"08/{d:02d}/2026" for d in range(1, 29)]
CBOE_CSV = "\n".join(
    f"{dt},15.0,15.5,14.5,14.{i % 10}" for i, dt in enumerate(_MD)
)


def _mock_get(url, timeout=0, **kw):
    class R:
        status_code = 200
        text = FRED_CSV if "fredgraph" in url else CBOE_CSV

        def raise_for_status(self):
            return None

    return R()


def test_tnx_yield_series(monkeypatch):
    monkeypatch.setattr("requests.get", _mock_get)
    s = OfficialMacroProvider().tnx_yield(days=400)
    assert isinstance(s, pd.Series) and len(s) == len(_DAYS)  # 59 天 - 0（缺失点在 days 窗口外亦被剔除）
    assert float(s.iloc[-1]) == pytest.approx(4.68)  # i=58 → 4.6(58%10=8)
    assert s.index.is_monotonic_increasing
    assert 3.0 < float(s.mean()) < 6.0  # 百分数口径（4.x 表示 4.x%）


def test_vix_and_vix9d(monkeypatch):
    monkeypatch.setattr("requests.get", _mock_get)
    p = OfficialMacroProvider()
    v = p.vix(days=400)
    assert len(v) == len(_MD)
    v9 = p.vix9d(days=400)
    assert v9 is not None and len(v9) == len(_MD)


def test_short_series_raises(monkeypatch):
    """行数不足必须抛错（诚实失败纪律），不得返回残缺序列。"""
    class R:
        status_code = 200
        text = "DATE,DGS10\n2026-08-28,4.67\n"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="行数不足"):
        OfficialMacroProvider().tnx_yield()


def test_ohlcv_rejected():
    """official 不承接个股 OHLCV（职责边界清晰，防止误用）。"""
    with pytest.raises(RuntimeError, match="不承接个股"):
        OfficialMacroProvider().ohlcv("AAPL")


def test_base_dispatch_rate_and_vol(monkeypatch):
    """经 base 的 rate_yield_for/vol_index_for 通用分发仍走官方实现。"""
    monkeypatch.setattr("requests.get", _mock_get)
    p = OfficialMacroProvider()
    s1 = p.rate_yield_for("TNX", days=400)
    s2 = p.vol_index_for("VIX", days=400)
    s3 = p.vol_index_for("VIX9D", days=400)
    assert len(s1) == len(_DAYS) and len(s2) == len(_MD) and s3 is not None
    with pytest.raises(RuntimeError):
        p.rate_yield_for("CN10Y")  # 非美符号→走 OHLCV→明确抛错


@pytest.mark.skipif(not OfficialMacroProvider.available(), reason="无外网")
def test_smoke_real_fred_cboe():
    p = OfficialMacroProvider()
    assert len(p.tnx_yield()) >= 30
    assert len(p.vix()) >= 30
