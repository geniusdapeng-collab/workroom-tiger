"""组合风险官（PortfolioRiskOfficer）+ 收益稳定官（ReturnSteward）。

PortfolioRiskOfficer：跨市场敞口聚合视图（总敞口/集中度/验证期标识/货币分布），
  组合 Gross Cap 校验（当前为披露层，M2 阶段转为真实截断执行）。
ReturnSteward：组合净值曲线稳定性评估（最大回撤/滚动夏普/连续不创新高的天数），
  目标："持续良好的稳定收益"——用可验证的统计口径说话，不承诺收益。
"""

from __future__ import annotations

import glob
import json
import math
import logging
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path

from ..ledger_io import read_json_strict

log = logging.getLogger(__name__)


def _finite(value, *, positive=False):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _state(out_dir: str):
    path = Path(out_dir) / "sim_portfolio.json"
    if not path.exists():
        return None
    value = read_json_strict(path)
    if not isinstance(value, dict) or not isinstance(value.get("equity_curve", []), list):
        raise ValueError("Invalid portfolio ledger shape")
    curve = value.get("equity_curve", [])
    for point in curve:
        if not isinstance(point, dict) or not _finite(point.get("equity"), positive=True):
            raise ValueError("Invalid portfolio NAV")
        date.fromisoformat(point["date"])
    dates = [point["date"] for point in curve]
    if dates != sorted(set(dates)):
        raise ValueError("Portfolio dates must be unique and strictly increasing")
    if not _finite(value.get("cash", 0)):
        raise ValueError("Invalid portfolio cash")
    return value


# ---------------------------------------------------------------- 组合风险官
@dataclass
class PortfolioRiskView:
    total_equity: float | None
    by_market: dict = field(default_factory=dict)      # mid -> equity
    concentration: dict = field(default_factory=dict)  # mid -> 占比
    gross_cap: float = 0.90
    over_cap: bool | None = None
    gross_exposure: float | None = None
    by_currency: dict = field(default_factory=dict)
    market_currency: dict = field(default_factory=dict)
    market_gross: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total_equity": self.total_equity, "by_market": self.by_market,
                "concentration": self.concentration, "gross_cap": self.gross_cap,
                "over_cap": self.over_cap, "gross_exposure": self.gross_exposure,
                "by_currency": self.by_currency, "market_currency": self.market_currency,
                "market_gross": self.market_gross, "notes": self.notes}


class PortfolioRiskOfficer:
    def __init__(self, gross_cap: float = 0.90):
        if not _finite(gross_cap) or gross_cap > 1:
            raise ValueError("Portfolio gross cap must be a finite fraction")
        self.gross_cap = gross_cap

    @staticmethod
    def _equity(out_dir: str) -> float | None:
        try:
            state = _state(out_dir)
            if state is None:
                return None
            curve = state.get("equity_curve", [])
            return curve[-1]["equity"] if curve else state.get("cash", 0)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("Invalid portfolio ledger %s: %s", out_dir, exc)
            return None

    def view(self, out_dirs: dict[str, str]) -> PortfolioRiskView:
        by, notes, currencies, market_gross, gross_amounts = {}, [], {}, {}, {}
        for mid, directory in out_dirs.items():
            try:
                state = _state(directory)
                if state is None:
                    raise ValueError("净值数据缺失")
                curve = state.get("equity_curve", [])
                equity = curve[-1]["equity"] if curve else state.get("cash", 0)
                if not _finite(equity, positive=True) or state.get("cash", 0) > equity:
                    raise ValueError("净值或现金关系无效")
                currency = state.get("currency") or {"us": "USD", "cn": "CNY", "hk": "HKD"}.get(mid)
                if not isinstance(currency, str) or currency not in ("USD", "CNY", "HKD"):
                    raise ValueError("账户本币缺失或无效")
                if not state.get("currency"):
                    notes.append(f"{mid}: 旧账无货币字段，按市场本币 {currency} 披露")
                by[mid], currencies[mid] = round(equity, 4), currency
                gross = equity - state.get("cash", 0)
                gross_amounts[mid], market_gross[mid] = gross, round(gross / equity, 6)
                if market_gross[mid] > self.gross_cap + 1e-9:
                    notes.append(f"{mid}: 已观察到本市场总敞口 {market_gross[mid]:.1%} 超上限 {self.gross_cap:.1%}")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                notes.append(f"{mid}: {exc}（如实披露，未参与合计）")
        by_currency = {}
        for mid, equity in by.items():
            currency = currencies[mid]
            by_currency[currency] = round(by_currency.get(currency, 0) + equity, 4)
        same_currency = len(by_currency) == 1
        total = sum(by.values()) if same_currency else None
        gross = sum(gross_amounts.values()) / total if total else None
        concentration = {mid: round(equity / total, 4) for mid, equity in by.items()} if total else {}
        if len(by_currency) > 1:
            notes.append("跨币种账户没有同一时点的 FX 证据：仅按本币分账，组合金额、集中度与组合总敞口均未核算")
        for mid, fraction in concentration.items():
            if fraction > .60:
                notes.append(f"{mid} 配置占比 {fraction:.0%} >60%——集中度提示（非否决）")
        return PortfolioRiskView(total_equity=round(total, 4) if total is not None else None,
                                 by_market=by, concentration=concentration, gross_cap=self.gross_cap,
                                 over_cap=(gross > self.gross_cap + 1e-9) if gross is not None else None,
                                 gross_exposure=round(gross, 6) if gross is not None else None,
                                 by_currency=by_currency, market_currency=currencies,
                                 market_gross=market_gross, notes=notes)


# ---------------------------------------------------------------- 收益稳定官
@dataclass
class StabilityReport:
    days: int
    total_return: float
    max_drawdown: float
    sharpe_rolling: float | None
    days_below_high: int
    verdict: str                 # 稳定 | 关注 | 告警
    notes: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    nav: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"days": self.days, "total_return": self.total_return,
                "max_drawdown": self.max_drawdown,
                "sharpe_rolling": self.sharpe_rolling,
                "days_below_high": self.days_below_high,
                "verdict": self.verdict, "notes": self.notes, "dates": self.dates,
                "nav": self.nav, "portfolio_basis": "各市场本币净值分别归一为1、等权合成，不代表跨币种金额收益"}


class ReturnSteward:
    """组合净值稳定性评估。阈值全部可配置（config.PORTFOLIO_STABILITY_*）。"""

    def __init__(self, mdd_warn: float = 0.08, mdd_alert: float = 0.15,
                 below_high_warn_days: int = 20):
        self.mdd_warn = mdd_warn
        self.mdd_alert = mdd_alert
        self.below_high_warn_days = below_high_warn_days

    @staticmethod
    def _curve(out_dir: str) -> list[dict]:
        try:
            state = _state(out_dir)
            return state.get("equity_curve", []) if state else []
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("Invalid portfolio curve %s: %s", out_dir, exc)
            return []

    def assess(self, out_dirs: dict[str, str]) -> StabilityReport:
        curves, notes = {}, []
        for mid, directory in out_dirs.items():
            curve = self._curve(directory)
            if curve:
                curves[mid] = curve
            else:
                notes.append(f"{mid}: 无有效净值曲线，未参与稳定性统计")
        if not curves:
            return StabilityReport(days=0, total_return=0.0, max_drawdown=0.0,
                                   sharpe_rolling=None, days_below_high=0,
                                   verdict="关注", notes=["无净值曲线数据（积累中，不作结论）"] + notes)
        # Start only once every participating account has an observed NAV.
        # On asynchronous exchange sessions retain the latest observed value.
        start = max(curve[0]["date"] for curve in curves.values())
        dates = sorted({point["date"] for curve in curves.values() for point in curve if point["date"] >= start})
        cursors = {mid: 0 for mid in curves}
        latest, bases, series = {}, {}, []
        for day in dates:
            for mid, curve in curves.items():
                while cursors[mid] < len(curve) and curve[cursors[mid]]["date"] <= day:
                    latest[mid] = curve[cursors[mid]]["equity"]
                    cursors[mid] += 1
                bases.setdefault(mid, latest[mid])
            series.append(sum(latest[mid] / bases[mid] for mid in curves) / len(curves))
        n = len(series)
        notes.append("组合按日期并集对齐、缺失日沿用已观察前值；各市场本币净值分别归一后等权合成，不合计不同货币金额")
        total_ret = series[-1] / series[0] - 1.0
        # 最大回撤
        peak, mdd, below_high = series[0], 0.0, 0
        for v in series:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1.0)
        for v in reversed(series):
            if v >= peak:
                break
            below_high += 1
        # 滚动夏普（日频，年化 √252；样本 <10 不输出）
        sharpe = None
        if n >= 10:
            rets = [series[i] / series[i - 1] - 1.0 for i in range(1, n)]
            mu = sum(rets) / len(rets)
            var = sum((r - mu) ** 2 for r in rets) / len(rets)
            sd = math.sqrt(var)
            if sd > 0:
                sharpe = round(mu / sd * math.sqrt(252), 2)
        mdd_abs = abs(mdd)
        if mdd_abs >= self.mdd_alert:
            verdict = "告警"
            notes.append(f"最大回撤 {mdd_abs:.1%} ≥ 告警线 {self.mdd_alert:.0%}")
        elif mdd_abs >= self.mdd_warn or below_high >= self.below_high_warn_days:
            verdict = "关注"
            if mdd_abs >= self.mdd_warn:
                notes.append(f"最大回撤 {mdd_abs:.1%} ≥ 关注线 {self.mdd_warn:.0%}")
            if below_high >= self.below_high_warn_days:
                notes.append(f"连续 {below_high} 日未创新高 ≥ {self.below_high_warn_days} 日")
        else:
            verdict = "稳定"
        if n < 20:
            notes.append(f"样本 {n} 日 <20，统计口径尚未稳定（积累中，不作结论性判断）")
            verdict = "关注" if verdict == "稳定" else verdict
        return StabilityReport(days=n, total_return=round(total_ret, 4),
                               max_drawdown=round(mdd, 4), sharpe_rolling=sharpe,
                               days_below_high=below_high, verdict=verdict, notes=notes,
                               dates=dates, nav=series)
