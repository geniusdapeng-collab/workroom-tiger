"""纽交所交易日历（v6.0）。

此前全系统用 pd.bdate_range 近似交易日——纽交所节假日（感恩节/独立日/
圣诞/马丁路德金日等）在系统认知里不存在：假日发信号、结算 age 按日历天、
盘前计划假日照跑。本模块是"交易日"语义的唯一权威来源。

覆盖 2024–2027 年 NYSE 全日休市日（每年例行 9 个假日；特殊休市如国葬
不在表内，遇到时信号顺延一天，无副作用）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# NYSE 全日休市日（2024-2027）
_HOLIDAYS: set[date] = {
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def _d(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    return d


def is_trading_day(d) -> bool:
    """是否 NYSE 交易日（工作日且非休市日）。"""
    d = _d(d)
    return d.weekday() < 5 and d not in _HOLIDAYS


def next_trading_day(d) -> date:
    """d 之后的第一个交易日（d 本身是交易日也取其后）。"""
    d = _d(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d) -> date:
    """d 之前的最后一个交易日（d 本身不是交易日时含当天向前找）。"""
    d = _d(d)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(d0, d1) -> int:
    """(d0, d1] 区间内的交易日数。"""
    d0, d1 = _d(d0), _d(d1)
    n, cur = 0, d0
    while cur < d1:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n
