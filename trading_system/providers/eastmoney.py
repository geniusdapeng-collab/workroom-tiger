"""东方财富数据供应商（v6.2 多源扩展 · 中国免费源，无需 key）。

接口（按东财公开 API 契约实现；实时端点已实测，K 线端点为东财多年稳定格式）：
  日 K  push2his.eastmoney.com/api/qt/stock/kline/get
        ?secid={市场}.{代码}&klt=101&fqt=1&lmt={n}&fields1=...&fields2=...
        fqt=1 = 东财服务端前复权（与 v6.0 复权口径一致，无需本地拆股处理）
        市场：105=纳斯达克 ｜ 106=纽交所 ｜ 107=美交所（自动探测并本轮缓存）
        klines 行格式：date,open,close,high,low,volume,amount,...（开/收相邻）
  实时  push2delay（延时）→ push2（实时）/api/qt/stock/get
        f43=现价×1000 ｜ f60=昨收×1000 ｜ f86=时间戳(epoch)

可用性：available() 探测 K 线端点——不通（如部分海外机房被东财拒绝）时
本 provider 自动从降级链剔除，不影响任何现有流程。

定位：降级链中国免费环（yahoo → stooq → tencent → sina → eastmoney →
agentgw → ifind_gw → tiingo）。TNX/VIX 不承接，显式抛错走降级。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime

import pandas as pd

from .base import DataProvider

logger = logging.getLogger(__name__)

_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
          "?secid={secid}&klt=101&fqt=1&lmt={n}"
          "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57")
_QUOTE_HOSTS = ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com")
_QUOTE_PATH = "/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f60,f86"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
       "Referer": "https://quote.eastmoney.com"}
_MARKETS = ("105", "106", "107")       # 纳斯达克 → 纽交所 → 美交所


class EastMoneyProvider(DataProvider):
    name = "eastmoney"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._market: dict[str, str] = {}

    @staticmethod
    def available() -> bool:
        """K 线端点连通性探测（不通则自动从降级链剔除）。"""
        try:
            req = urllib.request.Request(
                _KLINE.format(secid="105.AAPL", n=2), headers=_UA)
            with urllib.request.urlopen(req, timeout=8) as resp:
                blob = json.loads(resp.read().decode("utf-8", errors="replace"))
            return blob.get("rc") == 0 and bool((blob.get("data") or {}).get("klines"))
        except Exception:
            return False

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    # ---------------------------------------------------------------- 内部
    def _fetch_kline(self, secid: str, days: int) -> pd.DataFrame:
        blob = self._get_json(_KLINE.format(secid=secid, n=min(days + 20, 1000)))
        data = blob.get("data") or {}
        klines = data.get("klines") or []
        if blob.get("rc") != 0 or not klines:
            raise RuntimeError(f"东财 K 线为空: {secid}")
        rows = [k.split(",") for k in klines]
        # 行格式: date, open, close, high, low, volume, amount（开/收相邻）
        df = pd.DataFrame({
            "Date": [r[0] for r in rows],
            "Open": [float(r[1]) for r in rows],
            "Close": [float(r[2]) for r in rows],
            "High": [float(r[3]) for r in rows],
            "Low": [float(r[4]) for r in rows],
            "Volume": [float(r[5]) if len(r) > 5 else 0.0 for r in rows],
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        return self._normalize_ohlcv(df).tail(days)   # fqt=1 服务端已前复权

    def _fetch(self, ticker: str, days: int) -> pd.DataFrame:
        cands = ([self._market[ticker]] if ticker in self._market else list(_MARKETS))
        last_err: Exception | None = None
        for mkt in cands:
            try:
                df = self._fetch_kline(f"{mkt}.{ticker}", days)
                self._market[ticker] = mkt
                return df
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"东财全市场探测失败 {ticker}: {last_err}")

    # ---------------------------------------------------------------- 接口
    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        df = self._fetch(ticker, days)
        if len(df) < max(30, days // 3):
            raise RuntimeError(f"东财 {ticker} 历史不足: {len(df)} 行")
        return df

    def quote(self, ticker: str) -> dict | None:
        """东财实时/延时报价（push2 实时优先，push2delay 延时兜底）。"""
        cands = ([self._market[ticker]] if ticker in self._market else list(_MARKETS))
        for mkt in cands:
            for host in _QUOTE_HOSTS:
                try:
                    blob = self._get_json(host + _QUOTE_PATH.format(secid=f"{mkt}.{ticker}"))
                    data = blob.get("data") or {}
                    raw = data.get("f43")
                    if raw is None:
                        continue
                    price = float(raw) / 1000.0
                    if price <= 0:
                        continue
                    self._market[ticker] = mkt
                    ts = (datetime.fromtimestamp(int(data["f86"])).isoformat()
                          if data.get("f86") else "")
                    return {"price": price, "ts": ts,
                            "kind": "realtime" if "delay" not in host else "realtime_delayed"}
                except Exception:
                    continue
        logger.debug("东财报价失败 %s，回退基类日线收盘价", ticker)
        return super().quote(ticker)

    # 利率/波动率本通道不承接——显式抛错走降级
    def tnx_yield(self, days: int = 400) -> pd.Series:
        raise RuntimeError("eastmoney 不承接利率序列（^TNX），请走 yahoo/stooq/agentgw")

    def vix(self, days: int = 400) -> pd.Series:
        raise RuntimeError("eastmoney 不承接波动率指数（^VIX），请走 yahoo/stooq/agentgw")
