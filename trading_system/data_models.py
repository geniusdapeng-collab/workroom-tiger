"""结构化数据模型 — 各层之间用 dataclass 传递，禁止字符串解析（v3 根因修复）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DimensionScore:
    """单个维度/因子评分。score=None 表示整维缺失（聚合时再归一化剔除）。"""
    name: str
    score: Optional[float]            # 0-10 或 None（缺失）
    sub_scores: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 缺失子项记录（理论约定）


@dataclass
class MRSResult:
    mrs_raw: float
    delta: float                      # 五维极差 (max-min)
    k: float
    mrs_star: float
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)
    regime: str = "neutral"
    position_cap: tuple[float, float] = (0.0, 0.0)
    allow_new_positions: bool = True
    evidence: list[str] = field(default_factory=list)
    # v6.0（白皮书§4.5 三重现实折扣）：冲击折扣与流动性折扣的机器执行
    shock: bool = False               # Kill Switch：逻辑断裂级冲击，停新开仓
    shock_reason: str = ""
    liq_discount: float = 1.0         # 流动性折扣系数（0.7-1.0）


@dataclass
class SectorScore:
    etf: str
    shs: float
    factors: dict[str, float] = field(default_factory=dict)   # macro/flow/narr/micro
    breadth: float = float("nan")
    rs_slope_q: float = float("nan")
    r20: float = float("nan")
    in_main_pool: bool = False
    in_sub_pool: bool = False
    evidence: list[str] = field(default_factory=list)


@dataclass
class ChainLink:
    """产业链环节（上游/中游/下游）。"""
    name: str
    tickers: list[str]
    rs_20: float = float("nan")       # 环节 20 日等权相对强度
    momentum: float = float("nan")    # 环节 20 日等权收益


@dataclass
class ChainState:
    """产业链周期状态。"""
    chain_id: str
    name: str
    ics: float                        # 产业链周期评分 0-10
    stage: str                        # 复苏 / 扩张 / 过热 / 衰退
    stage_score: float = 5.0
    leading_link: str = ""            # 当前领涨环节
    links: dict[str, ChainLink] = field(default_factory=dict)
    breadth: float = float("nan")     # 链内股票 50D 上方占比
    rotation_signal: str = ""         # 传导中 / 龙头滞涨 / 无序
    hot: bool = False
    evidence: list[str] = field(default_factory=list)


@dataclass
class StockCandidate:
    ticker: str
    sector_etf: str = ""
    chain_id: str = ""
    chain_link: str = ""              # upstream / midstream / downstream
    rank_score: float = 0.0           # 扫描初排分
    # 过滤相关
    price: float = 0.0
    adv_usd: float = 0.0
    atr_pct: float = 0.0
    # TSS（s_options=None 表示期权维度缺失，聚合时再归一化剔除）
    s_structure: float = 0.0
    s_momentum: float = 0.0
    s_options: Optional[float] = 0.0
    tss: float = 0.0
    tss_final: float = 0.0            # 产业链加成后
    c_liq: float = 1.0
    tos: float = 0.0
    entry_template: str = ""          # A / B / C / ""
    key_level: float = 0.0
    stop_plan: str = ""
    stop_price: float = 0.0           # v6.0：结构化止损价（消灭正则解析文本）
    evidence: list[str] = field(default_factory=list)


@dataclass
class TradePick:
    ticker: str
    tss_final: float
    tos: float
    entry_template: str
    entry_price: float
    stop_price: float
    shares: int
    position_pct: float
    risk_usd: float
    chain: str = ""
    sector: str = ""
    card: str = ""                    # 交易卡片（白皮书 §5.10 模板）
    time_stop_days: int = 0           # v6.0：ATR 档位化时间止损（0=用全局默认）
    event_note: str = ""              # v6.0：事件折扣说明（财报/CPI/FOMC/OPEX）


@dataclass
class PipelineResult:
    trade_date: str
    provider: str
    mrs: Optional[MRSResult] = None
    sectors: list[SectorScore] = field(default_factory=list)
    chains: list[ChainState] = field(default_factory=list)
    watchlist: list[StockCandidate] = field(default_factory=list)
    picks: list[TradePick] = field(default_factory=list)
    action: str = "HOLD"
    market_view: str = ""
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
