"""CN 市场规格（v6.3 S4）— 沪深 A 股。

- 日历：周末 + config.CN_HOLIDAYS 内置节假日表（规则实现，可维护）
- 结算：T+1（当日买入次交易日起可卖）
- 涨跌停：主板 ±10% / 创业板(30号段)·科创板(688) ±20%；
  主板 ST 在 2026-07-06 前 ±5%，此后 ±10%（config 单一口径）
- 最小价位：0.01 元
- 基准组：沪深300 / 中债10Y / 50ETF波指 iVIX（免费源不可得维度记缺失，
  走"剔除再归一化"纪律——见 config.MARKET_BENCHMARKS["cn"]）
- 合规：①T+1 校验 ②涨跌停追单校验（围栏前置）
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .. import config
from .base import ComplianceVerdict, MarketSpec, _d, _parse_dates


class CNMarket(MarketSpec):
    market_id = "cn"
    short_name = "A股"
    name = "中国 A 股（沪深）"
    timezone = "Asia/Shanghai"
    currency = "CNY"
    settlement = "T+1"
    limit_note = ("主板 ±10%｜创业板(30号段)/科创板(688) ±20%（含风险警示股票）｜"
                  "主板ST：2026-07-06起 ±10%，此前 ±5%")
    holidays = _parse_dates(config.CN_HOLIDAYS)

    # ---------------------------------------------------------------- 交易规则

    def price_limit_pct(self, ticker: str, name: str = "", trade_date=None) -> float:
        """Choose the board rule first, then the dated mainboard ST rule.

        The current rule is the default; replay callers supply their execution date.
        """
        code = ticker.split(".")[0]
        if len(code) == 6 and code.isdigit() and code.startswith(("30", "688")):
            return config.CN_LIMIT_STAR_CHINEXT
        if "ST" in (name or "").upper():
            day = date.today() if trade_date is None else _d(trade_date)
            if not isinstance(day, date):
                raise ValueError("The price-limit date must be a valid calendar date")
            return (config.CN_LIMIT_ST if day >= _d(config.CN_LIMIT_ST_EFFECTIVE_FROM)
                    else config.CN_LIMIT_ST_LEGACY)
        return config.CN_LIMIT_MAIN

    def limit_prices(self, ticker: str, prev_close: float, name: str = "",
                     trade_date=None) -> tuple[float, float] | None:
        """Exchange tick rounding, including the one-tick minimum price movement.

        SSE 2026 rule 3.3.17 requires half-up rounding, at least one tick of
        movement and a one-tick price floor. Decimal avoids binary half-cent drift.
        """
        if (isinstance(prev_close, bool) or not isinstance(prev_close, (int, float))
                or not math.isfinite(prev_close) or prev_close <= 0):
            return None
        previous = Decimal(str(prev_close))
        pct = Decimal(str(self.price_limit_pct(ticker, name, trade_date)))
        tick = Decimal("0.01")
        limits = []
        for direction in (1, -1):
            limit = (previous * (1 + direction * pct)).quantize(tick, rounding=ROUND_HALF_UP)
            if abs(limit - previous) < tick:
                limit = (previous + direction * tick).quantize(tick, rounding=ROUND_HALF_UP)
            limits.append(float(max(tick, limit)))
        return tuple(limits)

    # ---------------------------------------------------------------- 合规校验

    def check_order(self, side, ticker, price, prev_close, trade_date,
                    buy_date=None, name="", vcm_cooling=False) -> ComplianceVerdict:
        side = side.lower() if isinstance(side, str) else ""
        common = super().check_order(side, ticker, price, prev_close, trade_date,
                                     buy_date, name, vcm_cooling)
        if not common.allowed:
            return common
        if _d(trade_date).year >= 2027:
            return ComplianceVerdict(False, "该年份交易所日历尚未核实，停止模拟成交", "CN_CALENDAR_UNVERIFIED")
        # ① T+1：当日买入标的当日卖出 → 拒绝并记录
        if side == "sell":
            try:
                buy_day = _d(buy_date)
                if buy_day is None or not self.is_trading_day(buy_day):
                    raise ValueError("Purchase day must be an actual exchange session")
                if buy_day >= _d(trade_date):
                    return ComplianceVerdict(
                        False, f"T+1 结算规则：{ticker} 当日或未来日期买入，当前不得卖出", "CN_T1")
            except (TypeError, ValueError, AttributeError):
                return ComplianceVerdict(False, "买入日期缺失或无效，T+1无法核实，停止卖出", "CN_BUY_DATE_MISSING")
        if (isinstance(prev_close, bool) or not isinstance(prev_close, (int, float))
                or not math.isfinite(prev_close) or prev_close <= 0):
            return ComplianceVerdict(False, "前收价缺失，涨跌停无法核实，停止成交", "CN_PREV_CLOSE_MISSING")
        # ② 涨跌停追单：触及涨停价买入 / 触及跌停价卖出 → 拒绝
        lim = self.limit_prices(ticker, prev_close, name, trade_date)
        if lim is not None:
            up, down = lim
            if side == "buy" and price >= up - 1e-9:
                return ComplianceVerdict(
                    False,
                    f"涨停追买禁止：{ticker} 价格 {price} 触及涨停价 {up}"
                    f"（幅度 {self.price_limit_pct(ticker, name, trade_date):.0%}）",
                    "CN_LIMIT_UP_CHASE")
            if side == "sell" and price <= down + 1e-9:
                return ComplianceVerdict(
                    False,
                    f"跌停追卖禁止：{ticker} 价格 {price} 触及跌停价 {down}"
                    f"（幅度 {self.price_limit_pct(ticker, name, trade_date):.0%}）",
                    "CN_LIMIT_DOWN_CHASE")
        return ComplianceVerdict(True, "T+1/涨跌停校验通过", "CN_OK")

    def compliance_rules(self) -> list[str]:
        return ["CN_T1（当日买入不得当日卖出）",
                "CN_LIMIT_UP_CHASE（涨停价追买拒绝）",
                "CN_LIMIT_DOWN_CHASE（跌停价追卖拒绝）"]
