"""全球宏观哨兵（GlobalSentinel）— 24h 全球动态监控快照。

监控面（全部真实数据，缺失如实标注，绝不编造）：
  - 三市基准指数（SPY / 沪深300 / 恒指）与各自 20 日动量
  - FRED 宏观包（config.FRED_MACRO_SERIES：2Y/利差/联邦基金利率）——证据展示
  - 时段接力：当前处于哪个市场的交易窗口（Asia → US 的 24h 接力）

产出 GlobalSnapshot：喂给组合总览「全球动态」区与夜班决策包。
本模块不产出任何评分——MRS 宏观维的评分权仍归各市场 mrs_agent（框架不变）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from .. import config

log = logging.getLogger("portfolio.sentinel")

_BMK = {"us": "SPY", "cn": "000300.SS", "hk": "HSI.HK"}
_BMK_LABEL = {"us": "标普500", "cn": "沪深300", "hk": "恒生指数"}


@dataclass
class GlobalSnapshot:
    ts: str
    session: str                    # 当前交易窗口（asia/europe/us/closed）
    benchmarks: dict = field(default_factory=dict)   # mid -> {label, last, mom20, source}
    fred: dict = field(default_factory=dict)         # series_id -> {label, last}
    missing: list = field(default_factory=list)      # 缺失项（如实披露）

    def to_dict(self) -> dict:
        return {"ts": self.ts, "session": self.session,
                "benchmarks": self.benchmarks, "fred": self.fred,
                "missing": self.missing}


def _session(now: datetime) -> str:
    """北京时间口径的三市接力窗口。"""
    bj = now.astimezone(timezone(timedelta(hours=8)))
    hm = bj.hour * 60 + bj.minute
    if 9 * 60 + 15 <= hm < 16 * 60 + 30:
        return "asia"
    if 16 * 60 + 30 <= hm < 21 * 60 + 30:
        return "europe"
    if hm >= 21 * 60 + 30 or hm < 4 * 60 + 30:
        return "us"
    return "closed"


class GlobalSentinel:
    """全球动态快照采集（best-effort：任何一项失败只记 missing，不阻塞）。"""

    def __init__(self, provider=None):
        # 延迟构造：优先腾讯（实测可达），调用方可注入任意 DataProvider
        self._provider = provider

    @property
    def provider(self):
        if self._provider is None:
            from ..providers.tencent import TencentProvider
            self._provider = TencentProvider()
        return self._provider

    def _benchmark(self, mid: str) -> dict | None:
        sym = _BMK[mid]
        try:
            df = self.provider.ohlcv(sym, days=40)
            if df is None or not len(df):
                return None
            closes = df["Close"]
            last = float(closes.iloc[-1])
            mom20 = (float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1.0) \
                if len(closes) > 21 else None
            return {"label": _BMK_LABEL[mid], "symbol": sym, "last": round(last, 2),
                    "mom20": round(mom20, 4) if mom20 is not None else None,
                    "source": getattr(self.provider, "name", "?")}
        except Exception as e:
            log.warning("哨兵基准 %s(%s) 获取失败: %s", mid, sym, e)
            return None

    def _fred(self) -> dict:
        out = {}
        try:
            from ..providers.official import OfficialMacroProvider
            p = OfficialMacroProvider()
        except Exception as e:
            log.warning("哨兵 FRED 源初始化失败: %s", e)
            return out
        for sid, label in (getattr(config, "FRED_MACRO_SERIES", {}) or {}).items():
            try:
                s = p.fred_series(sid, days=10)
                out[sid] = {"label": label, "last": round(float(s.iloc[-1]), 3)}
            except Exception as e:
                log.warning("哨兵 FRED %s 失败: %s", sid, e)
        return out

    def snapshot(self, now: datetime | None = None) -> GlobalSnapshot:
        now = now or datetime.now(timezone.utc)
        snap = GlobalSnapshot(ts=now.isoformat(), session=_session(now))
        for mid in ("us", "cn", "hk"):
            b = self._benchmark(mid)
            if b:
                snap.benchmarks[mid] = b
            else:
                snap.missing.append(f"benchmark:{mid}")
        snap.fred = self._fred()
        for sid in (getattr(config, "FRED_MACRO_SERIES", {}) or {}):
            if sid not in snap.fred:
                snap.missing.append(f"fred:{sid}")
        return snap
