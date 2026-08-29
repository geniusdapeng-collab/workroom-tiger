"""腾讯行情数据供应商（v6.2 多源扩展 · 中国免费源，无需 key）。

接口（已实测全通）：
  日 K  web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=us{代码}{后缀},day,,,{n},qfq
        后缀：纳斯达克 .OQ ｜ 纽交所 .N ｜ ETF 可无后缀（自动探测并本轮缓存）
        返回 bar = [date, open, close, high, low, volume]（注意开收顺序）
  实时  qt.gtimg.cn/q=us{代码}（GBK，~ 分隔，第 4 字段为现价）

重要：腾讯美股 K 线【未复权】（实测 AAPL 2020 拆股为硬跳变）——本 provider
对每段历史调用 _adjust_splits 做拆股检测+前复权，调整事件记日志披露。

定位：降级链中国免费环（yahoo → stooq → tencent → sina → eastmoney →
agentgw → ifind_gw → tiingo）。TNX/VIX 本通道不承接（腾讯 VIX 数据陈旧，
实测时间戳停滞），显式抛错走降级。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)

_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
          "?param=us{sym},day,,,{n},qfq")
_QUOTE = "https://qt.gtimg.cn/q=us{ticker}"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
_SUFFIXES = (".OQ", ".N", ".AM", "")   # 探测：纳斯达克 → 纽交所 → 美交所/ARCA(ETF) → 无后缀
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


class TencentProvider(DataProvider):
    name = "tencent"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._suffix: dict[str, str] = {}

    @staticmethod
    def available() -> bool:
        return True            # 无需 key；调用失败由降级链处理

    # ---------------------------------------------------------------- 内部
    def _get(self, url: str, encoding: str = "utf-8") -> str:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode(encoding, errors="replace")

    def _fetch_kline(self, sym: str, days: int) -> pd.DataFrame:
        text = self._get(_KLINE.format(sym=sym, n=min(days + 20, 800)))
        blob = json.loads(text)
        if blob.get("code") != 0:
            raise RuntimeError(f"腾讯 K 线错误: {blob.get('msg', '')[:80]}")
        data = blob.get("data") or {}
        node = data.get(f"us{sym}") or (list(data.values())[0] if data else None)
        if not node:
            raise RuntimeError(f"腾讯无数据: us{sym}")
        bars = node.get("qfqday") or node.get("day") or []
        if not bars:
            raise RuntimeError(f"腾讯 K 线为空: us{sym}")
        # bar = [date, open, close, high, low, volume]（腾讯开/收顺序与常规相反）
        df = pd.DataFrame({
            "Date": [b[0] for b in bars],
            "Open": [float(b[1]) for b in bars],
            "Close": [float(b[2]) for b in bars],
            "High": [float(b[3]) for b in bars],
            "Low": [float(b[4]) for b in bars],
            "Volume": [float(b[5]) if len(b) > 5 else 0.0 for b in bars],
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df, events = self._adjust_splits(df)     # 未复权源：拆股硬跳变必须处理
        if events:
            logger.warning("腾讯 %s 拆股复权: %s", sym, events)
        return self._normalize_ohlcv(df).tail(days)

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        cands = ([self._suffix[ticker]] if ticker in self._suffix
                 else [f"{ticker}{s}" for s in _SUFFIXES])
        last_err: Exception | None = None
        for cand in cands:
            try:
                df = self._fetch_kline(cand, days)
                # 腾讯对【错误后缀】不报错而是返回仅当天 1 行的占位数据
                # （实测 usCAT.OQ 如此）——探测必须要求行数充足，
                # 否则会把 1 行占位当成功并缓存错误后缀
                if len(df) < min(days, 50):
                    raise RuntimeError(f"占位数据（{len(df)} 行），后缀错误")
                self._suffix[ticker] = cand
                return df
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"腾讯全后缀探测失败 {ticker}: {last_err}")

    # ---------------------------------------------------------------- 接口
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"腾讯 {ticker} 历史不足: {len(df)} 行")
        return df

    def quote(self, ticker: str) -> dict | None:
        """腾讯实时行情（美股交易时段实时，kind=realtime）。"""
        try:
            text = self._get(_QUOTE.format(ticker=ticker), encoding="gbk")
            m = re.search(r'="(.*)"', text)
            if not m:
                raise ValueError("empty")
            f = m.group(1).split("~")
            if len(f) < 6 or f[0] == "pv_none_match":
                raise ValueError(f"腾讯无报价: {ticker}")
            price = float(f[3])
            if price <= 0:
                raise ValueError("price=0")
            ts_m = _TS_RE.search(m.group(1))
            return {"price": price,
                    "ts": ts_m.group(0) if ts_m else "",
                    "kind": "realtime"}
        except Exception as exc:
            logger.debug("腾讯报价失败 %s: %s", ticker, exc)
            return super().quote(ticker)

    # 利率/波动率本通道不承接（腾讯 VIX 数据陈旧，实测时间戳停滞）——显式抛错走降级
    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tencent 不承接利率序列（^TNX），请走 yahoo/stooq/agentgw")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("tencent 的 VIX 数据陈旧不可用（实测时间戳停滞），请走降级链")
