"""HK 市场规格（v6.3 S4）— 港股主板。

- 日历：周末 + config.HK_HOLIDAYS 内置节假日表；HK_HALF_DAYS 半日市标记
  （平安夜/新年前夕/农历新年前夕仅上午交易）
- 结算：T+0 回转（交收 T+2，交易层面当日可回转）
- 涨跌停：无；但 VCM（市调机制）冷静期适用标的集合见 config.HK_VCM_SYMBOLS
- 最小价位：按港交所价位表分档
- 基准组：恒指 HSI / 美债10Y（+HIBOR 占位）/ 恒指波指 VHSI（占位；
  免费源不可得维度记缺失走"剔除再归一化"）
- 合规：VCM 冷静期标的追单 → 拒绝
"""

from __future__ import annotations

from .. import config
from .base import ComplianceVerdict, MarketSpec, _parse_dates

# 港交所最小价位表（价位下限, 最小价位）
_HK_TICK_TABLE = [
    (0.25, 0.001), (0.50, 0.005), (10.00, 0.010), (20.00, 0.020),
    (100.00, 0.050), (200.00, 0.100), (500.00, 0.200),
    (1000.00, 0.500), (2000.00, 1.000), (5000.00, 2.000),
    (9995.00, 5.000),
]


class HKMarket(MarketSpec):
    market_id = "hk"
    short_name = "港股"
    name = "中国香港股市（HKEX 主板）"
    timezone = "Asia/Hong_Kong"
    currency = "HKD"
    settlement = "T+0"
    limit_note = "无涨跌停；VCM 市调机制冷静期标的集合（恒指+国指成分）"
    holidays = _parse_dates(config.HK_HOLIDAYS)
    half_days = _parse_dates(config.HK_HALF_DAYS)

    # ---------------------------------------------------------------- 交易规则

    def min_tick(self, price: float) -> float:
        """港交所价位表：价格越低最小价位越小。"""
        for cap, tick in _HK_TICK_TABLE:
            if price < cap:
                return tick
        return 5.0

    def in_vcm(self, ticker: str) -> bool:
        return ticker.upper() in set(config.HK_VCM_SYMBOLS)

    # ---------------------------------------------------------------- 合规校验

    def check_order(self, side, ticker, price, prev_close, trade_date,
                    buy_date=None, name="", vcm_cooling=False) -> ComplianceVerdict:
        # ③ VCM 冷静期标的追单 → 拒绝
        if vcm_cooling and self.in_vcm(ticker):
            return ComplianceVerdict(
                False,
                f"VCM 冷静期：{ticker} 属市调机制适用标的，冷静期内追单拒绝",
                "HK_VCM_COOLING")
        note = "无涨跌停" + ("（VCM 适用标的，当前非冷静期）" if self.in_vcm(ticker)
                             else "")
        return ComplianceVerdict(True, f"HK 合规校验通过：{note}", "HK_OK")

    def compliance_rules(self) -> list[str]:
        return ["HK_VCM_COOLING（VCM 冷静期标的追单拒绝）",
                "HK 无涨跌停（披露）"]
