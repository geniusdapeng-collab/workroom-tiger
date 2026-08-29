"""Tiingo 数据供应商（官方 API，需 TIINGO_API_KEY 环境变量）—— 可选强化环。

官方 REST API（非抓取），带 key 后不受共享出口 IP 限流影响。
未配置 key 时 available()=False，自动从降级链剔除，不影响任何现有流程。

官网免费档：每日 500 请求、EOD 日线，足以覆盖 core/extended 池每日扫描；
付费档可支撑 full 池。配置方式：export TIINGO_API_KEY=xxxx
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

from .base import DataProvider

log = logging.getLogger(__name__)

_BASE = "https://api.tiingo.com/tiingo/daily/{t}/prices"


class TiingoProvider(DataProvider):
    name = "tiingo"

    def __init__(self, batch_budget_s: float = 240.0):
        self.key = os.environ.get("TIINGO_API_KEY", "")
        if not self.key:
            raise RuntimeError("未配置 TIINGO_API_KEY")
        self.batch_budget_s = batch_budget_s
        self._cache: dict[str, pd.DataFrame] = {}

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("TIINGO_API_KEY"))

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        # 缓存充足性按【本次请求的 days】判定（v5.4 修复：旧代码不看长度，
        # full 模式预筛的 30 日短历史会被重量级拉取直接复用，指标全面失真）。
        if ticker in self._cache and len(self._cache[ticker]) >= days:
            return self._cache[ticker]
        start = (datetime.now() - timedelta(days=min(int(days * 1.6) + 30, 1000))
                 ).strftime("%Y-%m-%d")
        url = f"{_BASE.format(t=ticker)}?startDate={start}&token={self.key}"
        req = urllib.request.Request(url, headers={"User-Agent": "caishen-ai/5.4"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not rows:
            raise RuntimeError(f"tiingo 空数据: {ticker}")
        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        df = df.set_index("Date")
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        out = self._normalize_ohlcv(df[["Open", "High", "Low", "Close", "Volume"]])
        self._cache[ticker] = out
        return out

    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"tiingo {ticker} 历史不足: {len(df)} 行")
        return df

    def ohlcv_batch(self, tickers: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        started = time.time()
        for t in tickers:
            if time.time() - started > self.batch_budget_s:
                log.warning("tiingo 批量墙钟耗尽 %d/%d", len(out), len(tickers))
                break
            try:
                df = self._fetch(t, days)
                if len(df):
                    out[t] = df
            except Exception as e:
                log.info("tiingo 拉取 %s 失败: %s", t, e)
            time.sleep(0.15)   # 免费档频率保护
        return out

    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tiingo 不提供利率序列，走 agentgw 通道")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tiingo 不提供 VIX，走 agentgw 通道")
