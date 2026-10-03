"""Shared daily exit transition for journal, backtest and paper execution.

The entry day counts as day one. Stops run before protection; High reaching
2R raises the next bar's stop to entry_net + 0.5 initial R. Time expiry always
exits at Close. A market fence may defer an otherwise required sale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from . import config


@dataclass
class ExitResult:
    exit_i: int
    exit_price: float
    r: float
    reason: str
    protected: bool
    void: bool = False
    last_i: int = -1
    stop: float = 0.0
    blocked_reason: str = ""


def _finite(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _bps(cost_bps: float | None) -> float:
    value = config.COST_BPS if cost_bps is None else cost_bps
    if not _finite(value) or not 0 <= value < 10_000:
        raise ValueError("Single-side cost must be finite and in [0, 10000) bp")
    return value


def cost_adj_buy(price: float, cost_bps: float | None = None) -> float:
    return price * (1 + _bps(cost_bps) / 10_000)


def cost_adj_sell(price: float, cost_bps: float | None = None) -> float:
    return price * (1 - _bps(cost_bps) / 10_000)


def trading_days_held(entry_date, current_date, market=None) -> int:
    """Count exchange sessions inclusively, including sessions missed by a run."""
    from .markets import get_market
    spec = market or get_market("us")
    first = date.fromisoformat(str(entry_date)[:10])
    last = date.fromisoformat(str(current_date)[:10])
    if last < first:
        raise ValueError("Current day precedes the position's entry day")
    return sum(spec.is_trading_day(first + timedelta(days=i))
               for i in range((last - first).days + 1))


def previous_session_close(dates, closes, trade_date, market) -> float:
    """Return an observed close from the actual preceding exchange session.

    A suspension gap or a holiday row cannot stand in for the exchange's
    previous close. Zero explicitly means missing, so the CN fence stops a fill.
    """
    if len(dates) != len(closes):
        raise ValueError("Previous-close dates and prices must have equal lengths")
    day = date.fromisoformat(str(trade_date)[:10])
    expected = market.prev_trading_day(day - timedelta(days=1))
    for observed, close in zip(dates, closes):
        if date.fromisoformat(str(observed)[:10]) != expected:
            continue
        if isinstance(close, bool):
            return 0.0
        try:
            value = float(close)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0
    return 0.0


@dataclass
class DayAction:
    action: str                 # hold / exit / protect
    exit_price: float = 0.0     # raw executable price; caller applies costs
    reason: str = ""
    new_stop: float = 0.0
    protected: bool = False


def evaluate_day(bar_open: float, bar_high: float, bar_low: float, bar_close: float,
                 entry_price: float, stop: float, days_held: int,
                 time_stop: int | None = None, protect_r: float | None = None,
                 *, initial_stop: float | None = None,
                 initial_r: float | None = None, protected: bool = False) -> DayAction:
    """Apply one transition; initial R never changes when the stop is raised."""
    time_stop = config.TIME_STOP_DAYS[1] if time_stop is None else time_stop
    protect_r = config.PROFIT_PROTECT_R if protect_r is None else protect_r
    if isinstance(time_stop, bool) or not isinstance(time_stop, int) or time_stop < 1:
        raise ValueError("time_stop must be a positive integer")
    if not _finite(protect_r) or protect_r <= 0:
        raise ValueError("protect_r must be finite and positive")
    values = (bar_open, bar_high, bar_low, bar_close, entry_price, stop)
    if not all(_finite(v) and v > 0 for v in values):
        raise ValueError("Exit prices must be finite and positive")
    if bar_low > min(bar_open, bar_close) or bar_high < max(bar_open, bar_close):
        raise ValueError("Invalid OHLC envelope")
    stop0 = stop if initial_stop is None else initial_stop
    r0 = entry_price - stop0 if initial_r is None else initial_r
    if not _finite(r0) or r0 <= 0:
        raise ValueError("Initial risk per share must be finite and positive")
    if isinstance(days_held, bool) or not isinstance(days_held, int) or days_held < 1:
        raise ValueError("days_held must include the entry session")

    if bar_low <= stop:
        raw_exit = min(stop, bar_open)
        reason = ("保护性止盈" if protected and raw_exit >= entry_price else
                  "跳空低开触发止损" if bar_open < stop else "止损离场")
        return DayAction("exit", raw_exit, reason, stop, protected)
    raised = not protected and bar_high >= entry_price + protect_r * r0
    new_stop = max(stop, entry_price + 0.5 * r0) if raised else stop
    protected = protected or raised
    if days_held >= time_stop:
        return DayAction("exit", bar_close, "时间止损到期平仓", new_stop, protected)
    if raised:
        return DayAction("protect", new_stop=new_stop, protected=True)
    return DayAction("hold", new_stop=stop, protected=protected)


def simulate_trade(o, h, l, c, entry_i: int, stop0: float,
                   time_stop: int | None = None, protect_r: float | None = None,
                   cost_bps: float | None = None, *, dates=None, market=None,
                   ticker: str = "") -> ExitResult | None:
    """Replay the same daily transition within the arrays supplied by the caller.

    For an open position exit_price is the last raw Close and r is its net
    liquidation value. last_i identifies that mark. It is never a future bar.
    """
    time_stop = config.TIME_STOP_DAYS[1] if time_stop is None else time_stop
    protect_r = config.PROFIT_PROTECT_R if protect_r is None else protect_r
    n = len(c)
    if any(len(v) != n for v in (o, h, l)):
        raise ValueError("OHLC arrays must have equal lengths")
    if entry_i < 0 or entry_i >= n:
        return None
    if dates is not None and (len(dates) != n or any(a >= b for a, b in zip(dates, dates[1:]))):
        raise ValueError("Dates must be unique, strictly increasing and match OHLC")
    entry_raw = float(o[entry_i])
    if not math.isfinite(entry_raw) or entry_raw <= 0 or not _finite(stop0) or stop0 <= 0:
        return None
    if entry_raw <= stop0:
        return ExitResult(entry_i, entry_raw, 0.0, "开盘即破止损位，信号作废",
                          False, void=True, last_i=entry_i, stop=stop0)
    if market is not None and dates is not None:
        prev = previous_session_close(dates, c, dates[entry_i], market)
        verdict = market.check_order("buy", ticker, entry_raw, prev, dates[entry_i])
        if not verdict.allowed:
            return None
    entry = cost_adj_buy(entry_raw, cost_bps)
    r0 = entry - stop0
    stop, protected, last_i, blocked = stop0, False, -1, ""
    for j in range(entry_i, n):
        values = [float(v[j]) for v in (o, h, l, c)]
        if not all(math.isfinite(v) and v > 0 for v in values):
            continue                    # no executable bar, retain the last valid mark
        if market is not None and dates is not None and not market.is_trading_day(dates[j]):
            continue
        days = (trading_days_held(dates[entry_i], dates[j], market)
                if dates is not None else j - entry_i + 1)
        act = evaluate_day(*values, entry, stop, days, time_stop, protect_r,
                           initial_stop=stop0, initial_r=r0, protected=protected)
        last_i = j
        if act.action == "exit":
            verdict = None
            if market is not None and dates is not None:
                prev = previous_session_close(dates, c, dates[j], market)
                verdict = market.check_order("sell", ticker, act.exit_price, prev,
                                             dates[j], buy_date=dates[entry_i])
            if verdict is None or verdict.allowed:
                exit_net = cost_adj_sell(act.exit_price, cost_bps)
                return ExitResult(j, exit_net, (exit_net - entry) / r0, act.reason,
                                  act.protected, last_i=j, stop=act.new_stop)
            blocked = verdict.reason
        stop, protected = act.new_stop, act.protected
    if last_i < 0:
        return None
    mark = float(c[last_i])
    return ExitResult(-1, mark, (cost_adj_sell(mark, cost_bps) - entry) / r0,
                      "", protected, last_i=last_i, stop=stop, blocked_reason=blocked)
