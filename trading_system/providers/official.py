"""官方宏观数据源供应器（T0 级）— FRED 利率 + CBOE 波动率。

定位：降级链中的【真实官方源】，只承接宏观基准序列，不承接个股 OHLCV：
  - FRED（美联储圣路易斯联储）DGS10：10 年期美债收益率（官方发布，百分数）
  - CBOE（芝交所官网）VIX / VIX9D 历史日线 CSV（官方发布）

纪律（与白皮书一致）：
  - 真实源之间降级，绝不回退合成数据；本供应器本身就是真实官方源。
  - 数据不可得时抛异常，由调用方继续降级或诚实失败，绝不编造。
  - FRED DGS10 有约 1 个交易日的官方发布滞后（周末/假日顺延），
    新鲜度闸门（>3 天滞后诚实失败）不受影响。
"""

from __future__ import annotations

import io
import logging
import time

import pandas as pd
import requests

from .base import DataProvider

log = logging.getLogger("providers.official")

_FRED_DGS10 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
_CBOE_HIST = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"

_TIMEOUT = 20
_avail_cache: tuple[float, bool] | None = None


class OfficialMacroProvider(DataProvider):
    """FRED + CBOE 官方宏观基准源（name='official'）。"""

    name = "official"

    # 能力声明（v3 能力感知降级）：本源仅承接宏观基准序列；
    # 个股 OHLCV 等方法在降级链中直接跳过，不再产生必然失败的调用。
    CAPABILITIES = frozenset({
        "rate_yield_for", "vol_index_for", "tnx_yield", "vix", "vix9d", "fred_series",
    })

    # ---- 可用性探测（降级链注册用，30 分钟缓存）----
    @classmethod
    def available(cls) -> bool:
        global _avail_cache
        now = time.time()
        if _avail_cache and now - _avail_cache[0] < 1800:
            return _avail_cache[1]
        ok = False
        try:
            r = requests.get(_FRED_DGS10, timeout=8)
            ok = r.status_code == 200 and "DATE" in r.text[:200].upper()
        except Exception:
            ok = False
        _avail_cache = (now, ok)
        return ok

    # ---- 个股 OHLCV：本源不承接（明确抛错，交由其他源）----
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        raise RuntimeError("official 源仅承接宏观基准（利率/波动率），不承接个股 OHLCV")

    # ---- 利率：FRED DGS10（百分数，如 4.67）----
    def tnx_yield(self, days: int = 400) -> pd.Series:
        r = requests.get(_FRED_DGS10, timeout=_TIMEOUT)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        date_col, val_col = df.columns[0], df.columns[1]
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")  # FRED 缺失为 "."
        df = df.dropna(subset=[val_col])
        if df.empty:
            raise RuntimeError("FRED DGS10 返回空序列")
        idx = pd.to_datetime(df[date_col])
        s = pd.Series(df[val_col].to_numpy(dtype=float), index=idx).sort_index()
        s = s.tail(days)
        if len(s) < 30:
            raise RuntimeError(f"FRED DGS10 行数不足（{len(s)}<30）")
        return s

    # ---- 通用 FRED 序列（宏观补充包：DGS2/T10Y2Y/DFF 等，证据展示用）----
    def fred_series(self, series_id: str, days: int = 400) -> pd.Series:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = requests.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        date_col, val_col = df.columns[0], df.columns[1]
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
        df = df.dropna(subset=[val_col])
        if df.empty:
            raise RuntimeError(f"FRED {series_id} 返回空序列")
        idx = pd.to_datetime(df[date_col])
        s = pd.Series(df[val_col].to_numpy(dtype=float), index=idx).sort_index()
        return s.tail(days)

    # ---- 波动率：CBOE 官方历史 CSV ----
    def _cboe(self, name: str, days: int) -> pd.Series:
        url = _CBOE_HIST.format(name=name)
        r = requests.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text), header=None,
                         names=["date", "open", "high", "low", "close"])
        df = df[pd.to_datetime(df["date"], errors="coerce", format="%m/%d/%Y").notna()]
        if df.empty:
            raise RuntimeError(f"CBOE {name} 返回空序列")
        idx = pd.to_datetime(df["date"], format="%m/%d/%Y")
        s = pd.Series(pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float),
                      index=idx).dropna().sort_index()
        s = s.tail(days)
        if len(s) < 30:
            raise RuntimeError(f"CBOE {name} 行数不足（{len(s)}<30）")
        return s

    def vix(self, days: int = 400) -> pd.Series:
        return self._cboe("VIX", days)

    def vix9d(self, days: int = 400) -> pd.Series | None:
        try:
            return self._cboe("VIX9D", days)
        except Exception as e:
            log.warning("CBOE VIX9D 不可得（%s）→ None（期限结构子项按口径中性处理）", e)
            return None
