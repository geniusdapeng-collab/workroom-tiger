"""Demo 数据供应商 — 离线演示与测试。

生成【确定性】合成 OHLCV：每个标的按代码哈希分配“性格”（强趋势/趋势/
震荡/下跌/高波动），保证扫描器能产生差异化排序，用于验证流水线正确性。
不用于实盘决策。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from .base import DataProvider

_PERSONALITIES = {
    "strong":  {"drift": 0.0035, "vol": 0.016},   # 强趋势（候选股）
    "up":      {"drift": 0.0016, "vol": 0.014},
    "range":   {"drift": 0.0001, "vol": 0.011},
    "down":    {"drift": -0.0018, "vol": 0.015},
    "choppy":  {"drift": 0.0004, "vol": 0.028},   # 高波动噪声
}
_P_KEYS = list(_PERSONALITIES)


def _seed(ticker: str, salt: str = "") -> int:
    return int(hashlib.md5(f"{ticker}:{salt}".encode()).hexdigest()[:8], 16)


def _bday_index(n: int) -> pd.DatetimeIndex:
    """最近 n 个交易日索引（截止到最后一个 ≤ 今天的工作日）。

    v5.4 修复：pd.bdate_range(end=非工作日, periods=n) 在 pandas 3.0 下会
    少给一根（419≠420），DataFrame 构造直接 ValueError——周末/节假日跑
    demo 必崩。改为 start→end 正向生成后取尾部 n 根，长度恒定。
    """
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=int(n * 1.5) + 10)
    return pd.bdate_range(start=start, end=end)[-n:]


class DemoProvider(DataProvider):
    """scenario 剧本（v6.0）：测试/演示不再依赖随机种子运气——
      "normal"  默认：科技主线强势（末段确定性走强）
      "riskoff" 负向共振剧本：SPY 末段 120 日趋势下行、VIX 中枢 28 → MRS* 应
                触发禁开仓/AVOID（用于闸门行为测试的确定性夹具）
    """

    name = "demo"

    def __init__(self, scenario: str = "normal"):
        assert scenario in ("normal", "riskoff"), f"未知 demo 剧本: {scenario}"
        self.scenario = scenario

    def _personality(self, ticker: str) -> dict:
        # 板块 ETF 与大盘固定为偏多，个股按哈希均匀分布
        fixed = {"SPY": "up", "QQQ": "up", "XLK": "strong", "SMH": "strong",
                 "XLF": "up", "XLE": "range", "XLV": "range", "XLP": "range",
                 "XLY": "up", "XLI": "range", "XLU": "down", "XLRE": "down",
                 "IBB": "up", "IWM": "range",
                 # S4 多市场基准/板块代理（demo 剧情：与 US 同向的健康趋势）
                 "000300.SS": "up", "HSI.HK": "up",
                 "000928.SS": "range", "000929.SS": "up", "000930.SS": "up",
                 "000931.SS": "range", "000932.SS": "up", "000933.SS": "range",
                 "000934.SS": "up", "000935.SS": "strong", "000936.SS": "range",
                 "000937.SS": "down",
                 "HSTECH.HK": "strong", "HSF.HK": "up", "HSP.HK": "range",
                 "HSU.HK": "down", "HSC.HK": "up"}
        if ticker in fixed:
            return _PERSONALITIES[fixed[ticker]]
        return _PERSONALITIES[_P_KEYS[_seed(ticker) % len(_P_KEYS)]]

    # 价格路径的固定生成长度：无论调用方请求多少天，都先生成 N_REF 根再取
    # 尾部——保证 ohlcv(t, 5) 的最后一根与 ohlcv(t, 420) 完全一致（v5.4 修复：
    # 旧实现路径随 n 变化，quote 的"现价"与日报的入场/止损位对不上，
    # 盘中触发器在 demo 下全部误报）。
    N_REF = 800

    def _gen_path(self, ticker: str, n: int, drift: float, vol: float) -> np.ndarray:
        m = self.N_REF
        rng = np.random.default_rng(_seed(ticker, "px"))
        rets = rng.normal(drift, vol, m)
        gaps = rng.choice([0, 1], size=m, p=[0.985, 0.015])
        rets += gaps * rng.normal(0, 0.03, m)
        # v5.4：强趋势性格末段 40 日的日收益噪声压至 30%（保留纹理、去掉运气）——
        # demo 剧情要求"近期强势主线"确定性成立，不能靠种子运气（实测某种子下
        # SMH 末段噪声均值 -0.7%/日，+0.35% 漂移被完全淹没）。
        if drift >= 0.003:
            rets[-40:] = drift + (rets[-40:] - drift) * 0.3
        log_trend = np.cumsum(rets)
        # OU 环绕项：制造回撤/反弹/收缩等可识别技术结构（模板 A/B/C 训练场）
        ou = np.zeros(m)
        lam = 0.06 + (_seed(ticker, "ou") % 50) / 1000.0
        ou_vol = vol * 0.9
        for i in range(1, m):
            ou[i] = ou[i - 1] * (1 - lam) + rng.normal(0, ou_vol)
        # 随机 10-22 日收缩窗口（波动压缩蓄势 → 模板 B 素材）
        for k in range(m // 60 + 1):
            s0 = _seed(ticker, f"pl{k}") % max(1, m - 25)
            ln = 10 + _seed(ticker, f"pll{k}") % 12
            ou[s0:s0 + ln] *= 0.3
        # v5.4：强趋势性格的【末段 40 日】OU 平滑归零 + 确定性上行倾斜——
        # demo 的剧情设定是"近期存在强势主线"，纯随机 OU 尾段可能把强性格
        # 标的走成大跌，破坏演示与测试场景。段首连续（ou[-40]=原 ou[-41]），
        # 段内 OU 线性归零，近期走势完全由 drift + 倾斜主导。调整施加在
        # 固定 N_REF 路径上（取尾前），与请求长度无关，跨调用一致。
        if drift >= 0.003 and m > 41:
            ou[-40:] = ou[-41] * np.linspace(1.0, 0.0, 40)
        path = np.exp(log_trend + ou)
        if drift >= 0.003:
            tilt = np.ones(m)
            tilt[-40:] = np.exp(np.linspace(0, 0.26, 40))
            path = path * tilt
        return path[-n:]

    def _spy_path(self, m: int) -> np.ndarray:
        """SPY/QQQ 基准路径（固定长度生成，调用方取尾部）。"""
        t = np.arange(m)
        drift = 0.0014 + 0.0022 * np.sin(2 * np.pi * (t - m + 30) / 220.0)
        rng = np.random.default_rng(_seed("SPY", "px2"))
        rets = drift + rng.normal(0, 0.006, m)
        if self.scenario == "riskoff":
            # 负向共振剧本：末段 120 日确定性趋势下行（噪声压至 30%）
            rets[-120:] = -0.004 + (rets[-120:] - rets[-120:].mean()) * 0.3
        return 100 * np.exp(np.cumsum(rets))

    def ohlcv(self, ticker: str, days: int = 400) -> pd.DataFrame:
        p = self._personality(ticker)
        rng = np.random.default_rng(_seed(ticker, "px2"))
        n = days
        idx = _bday_index(n)

        # 固定 N_REF 长度生成完整路径，再取尾部 n 根、以全路径首根为定价锚
        # —— 任何 days 请求下同一日期的价格完全一致（v5.4 一致性修复）。
        if ticker in ("SPY", "QQQ", "000300.SS", "HSI.HK"):
            full = self._spy_path(self.N_REF)
        else:
            full = 100 * self._gen_path(ticker, self.N_REF, p["drift"], p["vol"])
        base_price = 8 + (_seed(ticker, "base") % 39200) / 100.0
        close = (full / full[0] * base_price)[-n:]

        intraday = np.abs(rng.normal(0, p["vol"] * 0.8, n))
        high = close * (1 + intraday)
        low = close * (1 - np.abs(rng.normal(0, p["vol"] * 0.8, n)))
        open_ = np.roll(close, 1) * (1 + rng.normal(0, p["vol"] * 0.3, n))
        open_[0] = close[0]
        base_vol = 2_000_000 + _seed(ticker, "vol") % 40_000_000
        volume = (base_vol * (1 + rng.normal(0, 0.25, n))).clip(min=200_000)

        df = pd.DataFrame({"Open": open_, "High": high, "Low": low,
                           "Close": close, "Volume": volume}, index=idx)
        return self._normalize_ohlcv(df)

    def _yield_series(self, symbol: str, days: int) -> pd.Series:
        # US 回归纪律：TNX 种子保持 _seed("TNX") 不变（序列与改造前逐点一致）
        rng = np.random.default_rng(_seed("TNX") if symbol == "TNX"
                                    else _seed(f"TNX:{symbol}"))
        idx = _bday_index(days)
        # 从 4.6% 缓慢下行到 4.25%（利率顺风场景）；riskoff 剧本转为上行施压
        drift = 0.002 if self.scenario == "riskoff" else -0.001
        series = 4.6 + np.cumsum(rng.normal(drift, 0.02, days))
        return pd.Series(series, index=idx, name=symbol)

    def tnx_yield(self, days: int = 400) -> pd.Series:
        return self._yield_series("TNX", days)

    def rate_yield_for(self, symbol: str, days: int = 400) -> pd.Series:
        # S4：CN10Y/US10Y 等市场利率基准走同一合成生成器（符号级确定性）
        if symbol.upper() in ("TNX", "CN10Y", "US10Y"):
            return self._yield_series(symbol.upper(), days)
        return super().rate_yield_for(symbol, days)

    def _vol_series(self, symbol: str, days: int) -> pd.Series:
        # US 回归纪律：VIX 种子保持 _seed("VIX") 不变
        rng = np.random.default_rng(_seed("VIX") if symbol == "VIX"
                                    else _seed(f"VIX:{symbol}"))
        idx = _bday_index(days)
        # OU 均值回归：围绕 15.5 波动，间或冲上 20+（情绪分有真实起伏）
        center = 28.0 if self.scenario == "riskoff" else 15.5
        series = np.empty(days)
        series[0] = center
        for i in range(1, days):
            series[i] = series[i - 1] + 0.06 * (center - series[i - 1]) + rng.normal(0, 0.55)
        return pd.Series(np.clip(series, 10, 45), index=idx, name=symbol)

    def vix(self, days: int = 400) -> pd.Series:
        return self._vol_series("VIX", days)

    def vol_index_for(self, symbol: str, days: int = 400) -> pd.Series | None:
        # S4：IVIX50/VHSI 等市场波指基准走同一合成生成器
        s = symbol.upper()
        if s == "VIX":
            return self.vix(days)
        if s == "VIX9D":
            return self.vix9d(days)
        if s in ("IVIX50", "VHSI"):
            return self._vol_series(s, days)
        return super().vol_index_for(symbol, days)

    def vix9d(self, days: int = 400) -> pd.Series | None:
        vix = self.vix(days)
        return vix * 0.92  # 轻度 contango

    def options_chain_snapshot(self, ticker: str) -> dict | None:
        """确定性合成期权快照：PCR / IV 由代码与当日日期哈希驱动（当日固定）。"""
        day = pd.Timestamp.today().strftime("%Y%m%d")
        r1 = _seed(ticker, f"pcr{day}")
        r2 = _seed(ticker, f"iv{day}")
        r3 = _seed(ticker, f"coi{day}")
        pcr = 0.5 + (r1 % 1100) / 1000.0                 # 0.50 - 1.60
        iv = 0.15 + (r2 % 750) / 1000.0                  # 0.15 - 0.90
        call_oi = 20_000 + r3 % 400_000
        return {
            "pcr_oi": round(pcr, 3),
            "pcr_vol": round(pcr * (0.8 + (r1 % 40) / 100.0), 3),
            "atm_iv": round(iv, 3),
            "call_oi": float(call_oi),
            "put_oi": float(int(call_oi * pcr)),
            "expiry": "demo",
        }

    def quote(self, ticker: str) -> dict | None:
        """模拟盘中报价：在最近收盘价上加确定性抖动（kind=realtime 模拟）。"""
        df = self.ohlcv(ticker, days=5)
        minute = pd.Timestamp.now().minute
        jitter = ((_seed(ticker, f"q{minute}") % 200) - 100) / 10000.0  # ±1%
        return {"price": round(float(df["Close"].iloc[-1]) * (1 + jitter), 2),
                "ts": pd.Timestamp.now().isoformat(),
                "kind": "realtime"}
