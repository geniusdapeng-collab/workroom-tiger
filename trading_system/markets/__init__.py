"""多市场抽象包（v6.3 S4）— "框架不变、输入替换"的三市支持（US/CN/HK）。

D1：六层决策栈对所有市场一致，本包只提供输入替换（日历/基准/规则/过滤参数）；
D2：硬依赖全断的市场当日诚实失败，其余市场不受影响；
D3：全部可调参数在 trading_system/config.py，本包不持有阈值。
"""

from .base import ComplianceVerdict, MarketSpec
from .registry import all_markets, get_market, market_ids

__all__ = ["MarketSpec", "ComplianceVerdict", "get_market", "market_ids", "all_markets"]
