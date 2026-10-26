"""统一出场引擎（v6.0）— 一个实现，三处消费。

背景（架构债）：同一条出场纪律——次日开盘入场 / 止损优先（跳空按开盘价）/
浮盈≥2R 止损上移 +0.5R / 时间止损收盘离场——曾在 journal.py、simulator.py、
backtest.py 各写了一遍，且已经发生漂移（journal 曾把"开盘破止损的作废信号"
计入胜率，模拟盘却跳过）。本模块是唯一实现：

  simulate_trade()   批量仿真（journal 结算 / 回测共用）：给 OHLC 数组与
                     入场索引，返回 (出场索引, 出场价, R, 原因, 浮动状态)
  evaluate_day()     逐日判定（小G模拟盘共用）：给今日 Bar 与持仓状态，
                     返回当日动作（None=持有 / 止损 / 时间止损 / 保护触发）

约定（与白皮书§10/§14.1/§15 一致）：
  - 开盘价 ≤ 止损 → 信号作废（void），不是亏损交易；
  - 止损优先的保守假设：同日先判止损再判保护；
  - 跳空低开穿越止损按开盘价成交；
  - 时间止损：第 time_stop 个交易日收盘平仓；
  - 交易成本：COST_BPS 单边，买入价×(1+bps)、卖出价×(1-bps)（净口径）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


@dataclass
class ExitResult:
    exit_i: int                 # 出场 bar 索引（-1 = 数据内未出场）
    exit_price: float
    r: float                    # 以 1R = 入场价-初始止损 计
    reason: str                 # 止损离场 / 保护性止盈 / 时间止损到期平仓 / ""
    protected: bool             # 是否触发过 2R 保护
    void: bool = False          # 开盘即破止损 → 信号作废


def cost_adj_buy(price: float, cost_bps: float | None = None) -> float:
    """买入净价（含单边成本）。cost_bps=None 用全局默认。"""
    bps = config.COST_BPS if cost_bps is None else cost_bps
    return price * (1 + bps / 10_000)


def cost_adj_sell(price: float, cost_bps: float | None = None) -> float:
    """卖出净价（含单边成本）。"""
    bps = config.COST_BPS if cost_bps is None else cost_bps
    return price * (1 - bps / 10_000)


def simulate_trade(o, h, l, c, entry_i: int, stop0: float,
                   time_stop: int | None = None,
                   protect_r: float | None = None,
                   cost_bps: float | None = None, *,
                   policy_id: str = "legacy-execution-v1",
                   sector_returns=None) -> ExitResult | None:
    """从 entry_i（开盘入场 bar）仿真到止损/保护/时间止损。

    o/h/l/c：等长数值序列（numpy 或 list），entry_i 起为持仓期。
    返回 ExitResult；开盘即破止损返回 void=True 的结果；无法入场返回 None。
    R 与价格均为【含交易成本净口径】（v6.0）。
    """
    if policy_id == "whitepaper-execution-v2":
        return _simulate_v2(o, h, l, c, entry_i, stop0, time_stop, protect_r,
                            cost_bps, sector_returns)
    if policy_id != "legacy-execution-v1":
        raise ValueError("Unknown execution policy")
    time_stop = time_stop or config.TIME_STOP_DAYS[1]
    protect_r = protect_r if protect_r is not None else config.PROFIT_PROTECT_R
    n = len(c)
    if entry_i >= n:
        return None
    entry_raw = float(o[entry_i])
    if not (entry_raw > 0) or math.isnan(entry_raw):
        return None
    entry = cost_adj_buy(entry_raw, cost_bps)
    if entry_raw <= stop0:
        return ExitResult(exit_i=entry_i, exit_price=entry_raw, r=0.0,
                          reason="开盘即破止损位，信号作废", protected=False, void=True)

    r0 = entry - stop0
    stop, protected = stop0, False
    protect_level = entry + protect_r * r0
    last_i = min(entry_i + time_stop - 1, n - 1)

    for j in range(entry_i, last_i + 1):
        cj = c[j]
        if math.isnan(float(cj)):
            continue                                   # 停牌日：持仓不动
        lj, hj, oj = float(l[j]), float(h[j]), float(o[j])
        # 止损优先（保守假设）
        if not math.isnan(lj) and lj <= stop:
            gap = not math.isnan(oj) and oj < stop
            raw_exit = oj if gap else stop
            exit_p = cost_adj_sell(raw_exit, cost_bps)
            r = (exit_p - entry) / r0
            reason = "保护性止盈" if (protected and exit_p >= entry) else "止损离场"
            return ExitResult(j, exit_p, r, reason, protected)
        # 浮盈 ≥2R → 止损上移至 +0.5R
        if not protected and not math.isnan(hj) and hj >= protect_level:
            stop = max(stop, entry + 0.5 * r0)
            protected = True
        # 时间止损：第 time_stop 个交易日收盘离场
        if j - entry_i + 1 >= time_stop:
            exit_p = cost_adj_sell(float(cj), cost_bps)
            return ExitResult(j, exit_p, (exit_p - entry) / r0,
                              "时间止损到期平仓", protected)

    # 数据内未出场：返回浮动状态（exit_i=-1，r 为浮动 R）
    last_close = float(c[last_i])
    r_live = (cost_adj_sell(last_close, cost_bps) - entry) / r0
    return ExitResult(-1, last_close, r_live, "", protected)


@dataclass
class DayAction:
    """逐日判定结果（小G模拟盘用）。"""
    action: str                 # "hold" / "exit" / "protect"
    exit_price: float = 0.0
    reason: str = ""
    next_stop: float | None = None


def evaluate_day(bar_open: float, bar_high: float, bar_low: float, bar_close: float,
                 entry_price: float, stop: float, days_held: int,
                 time_stop: int | None = None,
                 protect_r: float | None = None, *,
                 policy_id: str = "legacy-execution-v1",
                 initial_stop: float | None = None,
                 protected: bool = False,
                 sector_return: float | None = None) -> DayAction:
    """单根日 K 的出场纪律判定（与 simulate_trade 同一套规则语义）：

    1. 盘中触及止损（low ≤ stop）→ 跳空按开盘价，否则按止损价；
    2. 持仓满 time_stop 且未推进（收盘 < 成本×1.01）→ 收盘时间止损；
    3. 浮盈 ≥ protect_r × R → 返回 protect（调用方上移止损至成本线）。
    """
    if policy_id == "whitepaper-execution-v2":
        return _evaluate_v2(bar_open, bar_high, bar_low, bar_close, entry_price,
                            stop, days_held, time_stop, protect_r,
                            initial_stop, protected, sector_return)
    if policy_id != "legacy-execution-v1":
        raise ValueError("Unknown execution policy")
    time_stop = time_stop or config.TIME_STOP_DAYS[1]
    protect_r = protect_r if protect_r is not None else config.PROFIT_PROTECT_R
    if bar_low <= stop:
        exit_p = round(min(stop, bar_open), 2)
        reason = "跳空低开触发止损" if bar_open < stop else "触及止损离场"
        return DayAction("exit", exit_p, reason)
    if days_held >= time_stop and bar_close < entry_price * 1.01:
        return DayAction("exit", round(bar_close, 2),
                         f"时间止损（{time_stop} 日未推进）")
    if bar_close >= entry_price + protect_r * abs(entry_price - stop) \
            and stop < entry_price:
        return DayAction("protect")
    return DayAction("hold")



def _evaluate_v2(o, h, l, c, entry, stop, days, time_stop, protect_r,
                 initial_stop, protected, sector_return):
    """Canonical v2 bar rule. A new protective stop applies on the next bar."""
    values = (o, h, l, c, entry, stop, initial_stop)
    if (any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or v <= 0 for v in values)
            or h < max(o, l, c) or l > min(o, h, c)
            or initial_stop >= entry or type(days) is not int or days < 1
            or type(protected) is not bool):
        raise ValueError("Invalid v2 OHLC or immutable entry risk")
    deadline = config.TIME_STOP_DAYS[1] if time_stop in (None, 0) else time_stop
    if type(deadline) is not int or not 5 <= deadline <= 7:
        raise ValueError("V2 time stop must be 5..7 observed trading bars")
    protect_r = config.PROFIT_PROTECT_R if protect_r is None else protect_r
    if isinstance(protect_r, bool) or not isinstance(protect_r, (int, float)) or not math.isfinite(protect_r) or protect_r <= 0:
        raise ValueError("Invalid v2 protection risk multiple")
    if sector_return is not None and (isinstance(sector_return, bool)
            or not isinstance(sector_return, (int, float)) or not math.isfinite(sector_return)):
        raise ValueError("Invalid observed sector return")
    if l <= stop:
        return DayAction("exit", min(stop, o),
                         "跳空低开触发止损" if o < stop else "保护性止盈" if protected else "触及止损离场")
    if days >= deadline:
        return DayAction("exit", c, f"时间止损到期平仓（{deadline} 日）")
    if days >= config.TIME_STOP_DAYS[0]:
        if c <= entry:
            return DayAction("exit", c, "时间止损（5日未推进）")
        if sector_return is not None and c / entry - 1 < sector_return - config.SECTOR_UNDERPERFORMANCE_V2:
            return DayAction("exit", c, "时间止损（明显跑输实际所属板块）")
    risk = entry - initial_stop
    if not protected and h >= entry + protect_r * risk:
        return DayAction("protect", next_stop=max(stop, entry + .5 * risk))
    return DayAction("hold", next_stop=stop)


def _simulate_v2(o, h, l, c, entry_i, stop0, time_stop, protect_r,
                  cost_bps, sector_returns):
    """Batch caller of evaluate_day; no second copy of the exit equations."""
    n = len(c)
    if any(len(values) != n for values in (o, h, l)) or type(entry_i) is not int or entry_i < 0:
        raise ValueError("Invalid v2 bar arrays")
    if entry_i >= n:
        return None
    raw = float(o[entry_i])
    if not math.isfinite(raw) or raw <= 0:
        return None
    if raw <= stop0:
        return ExitResult(entry_i, raw, 0., "开盘即破止损位，信号作废", False, True)
    entry, stop, protected = cost_adj_buy(raw, cost_bps), stop0, False
    days, last_close = 0, None
    for j in range(entry_i, n):
        row = tuple(float(values[j]) for values in (o, h, l, c))
        if all(math.isnan(value) for value in row):
            continue  # Explicit missing complete bar: no fabricated holding day.
        days += 1
        sector = sector_returns[j] if sector_returns is not None else None
        action = evaluate_day(*row, entry, stop, days, time_stop, protect_r,
                              policy_id="whitepaper-execution-v2", initial_stop=stop0,
                              protected=protected, sector_return=sector)
        last_close = row[-1]
        if action.action == "exit":
            net = cost_adj_sell(action.exit_price, cost_bps)
            return ExitResult(j, net, (net - entry) / (entry - stop0), action.reason, protected)
        if action.action == "protect":
            stop, protected = action.next_stop, True
    if last_close is None:
        return None
    return ExitResult(-1, last_close, (cost_adj_sell(last_close, cost_bps) - entry) / (entry - stop0), "", protected)
