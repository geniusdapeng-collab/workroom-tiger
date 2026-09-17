"""事件日历与事件折扣（v6.0，白皮书§11 + §4.5 的机器执行）。

此前白皮书"事件风险管理"整章（财报降仓、CPI/FOMC/非农加仓闸门、OPEX 偏
模板 A）在代码中没有对应实现——跳空风险完全裸奔。本模块提供：

  EventCalendar.macro_events(date)      当日宏观事件（CPI/FOMC/非农）
  EventCalendar.is_opex_week(date)      是否期权到期周（每月第三个周五所在周）
  EventCalendar.earnings_within(ticker, date, days)  标的是否临近财报
  EventCalendar.day_discount(date)      当日 Gross Cap 折扣（宏观事件×0.8 / OPEX×0.9）
  EventCalendar.pick_adjustment(ticker, date)  个股级事件调整（财报临近→仓位×0.5）

宏观事件日期来源：FOMC 为美联储提前公布的全年日程（内置 2026-2027）；
CPI/非农为 BLS 例行发布日（内置 2026 主表，缺失日期自动近似：非农=每月
第一个周五，CPI=每月中旬，近似只影响折扣触发日±1-2 天，方向保守）。
用户可用 config 同级 events_calendar.json 覆盖/补充（{"macro": {"YYYY-MM-DD": "FOMC"},
"earnings": {"TICKER": "YYYY-MM-DD"}}）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# FOMC 议息会议（美联储公布日程，结果公布日）
_FOMC = {
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
}

# CPI 发布日（BLS 例行，2026）
_CPI = {
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-13", "2026-11-10", "2026-12-10",
}


def _d(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    return d


class EventCalendar:
    """事件日历（每轮运行实例化一次；用户覆盖文件可选）。"""

    def __init__(self, override_path: str | None = None):
        self._macro: dict[str, str] = {d: "FOMC" for d in _FOMC}
        self._macro.update({d: "CPI" for d in _CPI})
        self._earnings: dict[str, str] = {}
        path = override_path or os.environ.get("TS_EVENTS_CALENDAR", "events_calendar.json")
        p = Path(path)
        if p.exists():
            try:
                blob = json.loads(p.read_text())
                self._macro.update(blob.get("macro", {}))
                self._earnings.update({k.upper(): v for k, v in blob.get("earnings", {}).items()})
                log.info("事件日历覆盖文件已加载: %s（宏观 %d 条，财报 %d 条）",
                         path, len(blob.get("macro", {})), len(self._earnings))
            except Exception as exc:
                log.warning("事件日历覆盖文件解析失败（忽略）: %s", exc)

    # ------------------------------------------------------------ 宏观事件
    def macro_events(self, d) -> list[str]:
        """当日宏观事件列表（CPI/FOMC/非农）。非农=每月第一个周五（例行规则）。"""
        d = _d(d)
        out = []
        key = d.isoformat()
        if key in self._macro:
            out.append(self._macro[key])
        if d.weekday() == 4 and d.day <= 7:      # 每月第一个周五 = 非农
            out.append("NFP")
        return out

    def is_opex_week(self, d) -> bool:
        """是否期权到期周（每月第三个周五所在周，周一~周五）。"""
        d = _d(d)
        first = date(d.year, d.month, 1)
        third_fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        week_start = third_fri - timedelta(days=4)
        return week_start <= d <= third_fri

    def day_discount(self, d) -> tuple[float, list[str]]:
        """当日 Gross Cap 总折扣与说明（白皮书§4.5 事件折扣）。"""
        factor, notes = 1.0, []
        events = self.macro_events(d)
        if events:
            factor *= config.EVENT_MACRO_DISCOUNT
            notes.append(f"宏观事件日（{'/'.join(events)}）：总仓位上限×{config.EVENT_MACRO_DISCOUNT}，"
                         "加仓闸门延后开盘 30 分钟（白皮书§11.1）")
        if self.is_opex_week(d):
            factor *= config.EVENT_OPEX_DISCOUNT
            notes.append(f"期权到期周：总仓位上限×{config.EVENT_OPEX_DISCOUNT}，"
                         "关键位拉扯/假突破频发，偏模板 A 回踩确认（白皮书§11.1）")
        return round(factor, 3), notes

    # ------------------------------------------------------------ 个股事件
    def register_earnings(self, ticker: str, d) -> None:
        """登记财报日（供 provider 层获取后注入）。"""
        self._earnings[ticker.upper()] = _d(d).isoformat()

    def earnings_within(self, ticker: str, d, days: int = 2) -> date | None:
        """标的是否在 days 天内发布财报；是则返回财报日，否则 None。"""
        raw = self._earnings.get(ticker.upper())
        if not raw:
            return None
        ed = _d(raw)
        d = _d(d)
        return ed if d <= ed <= d + timedelta(days=days) else None

    def pick_adjustment(self, ticker: str, d) -> tuple[float, str]:
        """个股级事件调整：返回 (仓位系数, 说明)。白皮书§11.1：
        非事件策略仓位在财报前 1-2 天降至计划的 30%-50%（取 0.5 口径）。"""
        ed = self.earnings_within(ticker, d, days=2)
        if ed:
            return (config.EVENT_EARNINGS_SIZE,
                    f"财报临近（{ed.isoformat()}，跳空不可控）：仓位×{config.EVENT_EARNINGS_SIZE}"
                    "（白皮书§11.1 事件折扣）")
        return 1.0, ""
