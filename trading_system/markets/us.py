"""US 市场规格（v6.3 S4）— 现有行为原样抽取，零变化。

日历直接复用 trading_system/calendar.py（NYSE 2024-2027 休市表），
基准组 SPY/TNX/VIX，板块 SECTOR_ETFS，扫描过滤全局 SCAN_* 常量，
合规保持现有 Kill Switch/事件折扣（L4 既有机制），无个股级围栏。
"""

from __future__ import annotations

from .. import calendar as _us_cal
from .base import ComplianceVerdict, MarketSpec


class USMarket(MarketSpec):
    market_id = "us"
    short_name = "美股"
    name = "美国股市（NYSE/NASDAQ）"
    timezone = "America/New_York"
    currency = "USD"
    settlement = "T+1"               # 2024-05 起美股 T+1 结算（交易层面 T+0 可回转）
    limit_note = "无个股涨跌停；熔断 LULD 标记（披露用，不作追单拒绝）"

    # ---- 日历：原样委托 trading_system.calendar（单一权威来源不变）----
    def is_trading_day(self, d) -> bool:
        return _us_cal.is_trading_day(d)

    def next_trading_day(self, d):
        return _us_cal.next_trading_day(d)

    def prev_trading_day(self, d):
        return _us_cal.prev_trading_day(d)

    # ---- 合规：无个股涨跌停追单限制；Kill Switch/事件折扣由 L4 既有机制执行 ----
    def check_order(self, side, ticker, price, prev_close, trade_date,
                    buy_date=None, name="", vcm_cooling=False) -> ComplianceVerdict:
        return ComplianceVerdict(
            True, "US 无个股涨跌停（LULD 熔断标记披露）；Kill Switch/事件折扣走 L4 既有机制",
            "US_OK")

    def compliance_rules(self) -> list[str]:
        return ["US_OK（无个股涨跌停，LULD 标记）", "L4 Kill Switch/事件折扣（既有）"]
