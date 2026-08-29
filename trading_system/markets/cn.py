"""CN 市场规格（v6.3 S4）— 沪深 A 股。

- 日历：周末 + config.CN_HOLIDAYS 内置节假日表（规则实现，可维护）
- 结算：T+1（当日买入次交易日起可卖）
- 涨跌停：主板 ±10% / 创业板(300)·科创板(688) ±20% / ST ±5%（config 单一口径）
- 最小价位：0.01 元
- 基准组：沪深300 / 中债10Y / 50ETF波指 iVIX（免费源不可得维度记缺失，
  走"剔除再归一化"纪律——见 config.MARKET_BENCHMARKS["cn"]）
- 合规：①T+1 校验 ②涨跌停追单校验（围栏前置）
"""

from __future__ import annotations

from .. import config
from .base import ComplianceVerdict, MarketSpec, _d, _parse_dates


class CNMarket(MarketSpec):
    market_id = "cn"
    short_name = "A股"
    name = "中国 A 股（沪深）"
    timezone = "Asia/Shanghai"
    currency = "CNY"
    settlement = "T+1"
    limit_note = "主板 ±10%｜创业板(300)/科创板(688) ±20%｜ST ±5%"
    holidays = _parse_dates(config.CN_HOLIDAYS)

    # ---------------------------------------------------------------- 交易规则

    def price_limit_pct(self, ticker: str, name: str = "") -> float:
        """个股涨跌停幅度：ST ±5%；300/688 开头 ±20%；其余主板 ±10%。

        ticker 形如 "300750.SZ" / "688981.SS" / "600519.SS"；名称含 "ST" 优先。
        """
        if "ST" in (name or "").upper():
            return config.CN_LIMIT_ST
        code = ticker.split(".")[0]
        if code.startswith(("300", "688")):
            return config.CN_LIMIT_STAR_CHINEXT
        return config.CN_LIMIT_MAIN

    # ---------------------------------------------------------------- 合规校验

    def check_order(self, side, ticker, price, prev_close, trade_date,
                    buy_date=None, name="", vcm_cooling=False) -> ComplianceVerdict:
        side = (side or "").lower()
        # ① T+1：当日买入标的当日卖出 → 拒绝并记录
        if side == "sell" and buy_date is not None \
                and _d(buy_date) == _d(trade_date):
            return ComplianceVerdict(
                False,
                f"T+1 结算规则：{ticker} 当日（{_d(trade_date)}）买入，当日不得卖出",
                "CN_T1")
        # ② 涨跌停追单：触及涨停价买入 / 触及跌停价卖出 → 拒绝
        lim = self.limit_prices(ticker, prev_close, name)
        if lim is not None:
            up, down = lim
            if side == "buy" and price >= up - 1e-9:
                return ComplianceVerdict(
                    False,
                    f"涨停追买禁止：{ticker} 价格 {price} 触及涨停价 {up}"
                    f"（幅度 {self.price_limit_pct(ticker, name):.0%}）",
                    "CN_LIMIT_UP_CHASE")
            if side == "sell" and price <= down + 1e-9:
                return ComplianceVerdict(
                    False,
                    f"跌停追卖禁止：{ticker} 价格 {price} 触及跌停价 {down}"
                    f"（幅度 {self.price_limit_pct(ticker, name):.0%}）",
                    "CN_LIMIT_DOWN_CHASE")
        return ComplianceVerdict(True, "T+1/涨跌停校验通过", "CN_OK")

    def compliance_rules(self) -> list[str]:
        return ["CN_T1（当日买入不得当日卖出）",
                "CN_LIMIT_UP_CHASE（涨停价追买拒绝）",
                "CN_LIMIT_DOWN_CHASE（跌停价追卖拒绝）"]
