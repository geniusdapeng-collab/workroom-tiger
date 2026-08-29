"""Agent 层 — v4.0 六层流水线。

Layer 0  UniverseScanner   全市场扫描 → 动态股票池（替代硬编码 watchlist）
Layer 1  MRSAgent          市场共振五维评分 + 一致性修正（真实数据）
Layer 2  SectorAgent       SHS 板块热度 + 主线池
Layer 2.5 ChainCycleAgent  产业链周期 ICS（新增大模块）
Layer 3  TSSAgent          个股 TSS 三组件 + 入场模板检测
Layer 4  RiskManagerAgent  闸门 / R 仓位反推 / 事件折扣 / 交易卡片
"""

from .base import BaseAgent
from .mrs_agent import MRSAgent
from .sector_agent import SectorAgent
from .chain_cycle_agent import ChainCycleAgent
from .universe_scanner_agent import UniverseScannerAgent
from .tss_agent import TSSAgent
from .risk_manager_agent import RiskManagerAgent

__all__ = [
    "BaseAgent", "MRSAgent", "SectorAgent", "ChainCycleAgent",
    "UniverseScannerAgent", "TSSAgent", "RiskManagerAgent",
]
