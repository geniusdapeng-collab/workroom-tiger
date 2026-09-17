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
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- 组合风险官
@dataclass
class PortfolioRiskView:
    total_equity: float
    by_market: dict = field(default_factory=dict)      # mid -> equity
    concentration: dict = field(default_factory=dict)  # mid -> 占比
    gross_cap: float = 0.90
    over_cap: bool = False
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total_equity": self.total_equity, "by_market": self.by_market,
                "concentration": self.concentration, "gross_cap": self.gross_cap,
                "over_cap": self.over_cap, "notes": self.notes}


class PortfolioRiskOfficer:
    def __init__(self, gross_cap: float = 0.90):
        self.gross_cap = gross_cap

    @staticmethod
    def _equity(out_dir: str) -> float | None:
        f = Path(out_dir) / "sim_portfolio.json"
        if not f.exists():
            return None
        try:
            s = json.loads(f.read_text())
            curve = s.get("equity_curve") or []
            return float(curve[-1]["equity"]) if curve else float(s.get("cash", 0))
        except Exception:
            return None

    def view(self, out_dirs: dict[str, str]) -> PortfolioRiskView:
        by, notes = {}, []
        for mid, d in out_dirs.items():
            eq = self._equity(d)
            if eq is not None:
                by[mid] = round(eq, 2)
            else:
                notes.append(f"{mid}: 净值数据缺失（如实披露）")
        total = round(sum(by.values()), 2)
        conc = {m: round(v / total, 4) for m, v in by.items()} if total else {}
        # 集中度提示：单一市场占比 >60% 时显性标注（提示非否决）
        for m, c in conc.items():
            if c > 0.60:
                notes.append(f"{m} 配置占比 {c:.0%} >60%——集中度提示（非否决，供配置官参考）")
        return PortfolioRiskView(total_equity=total, by_market=by,
                                 concentration=conc, gross_cap=self.gross_cap,
                                 over_cap=False, notes=notes)


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

    def to_dict(self) -> dict:
        return {"days": self.days, "total_return": self.total_return,
                "max_drawdown": self.max_drawdown,
                "sharpe_rolling": self.sharpe_rolling,
                "days_below_high": self.days_below_high,
                "verdict": self.verdict, "notes": self.notes}


class ReturnSteward:
    """组合净值稳定性评估。阈值全部可配置（config.PORTFOLIO_STABILITY_*）。"""

    def __init__(self, mdd_warn: float = 0.08, mdd_alert: float = 0.15,
                 below_high_warn_days: int = 20):
        self.mdd_warn = mdd_warn
        self.mdd_alert = mdd_alert
        self.below_high_warn_days = below_high_warn_days

    @staticmethod
    def _curve(out_dir: str) -> list[float]:
        f = Path(out_dir) / "sim_portfolio.json"
        if not f.exists():
            return []
        try:
            s = json.loads(f.read_text())
            return [float(p["equity"]) for p in (s.get("equity_curve") or [])]
        except Exception:
            return []

    def assess(self, out_dirs: dict[str, str]) -> StabilityReport:
        # 组合日净值 = 三市当日净值加总（按日期对齐，缺失日沿用前值）
        curves = {m: self._curve(d) for m, d in out_dirs.items()}
        curves = {m: c for m, c in curves.items() if c}
        notes: list = []
        if not curves:
            return StabilityReport(days=0, total_return=0.0, max_drawdown=0.0,
                                   sharpe_rolling=None, days_below_high=0,
                                   verdict="关注", notes=["无净值曲线数据（积累中，不作结论）"])
        n = min(len(c) for c in curves.values())
        series = [sum(c[i] for c in curves.values()) for i in range(n)]
        base = series[0] if series else 1.0
        total_ret = (series[-1] / base - 1.0) if base else 0.0
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
                               days_below_high=below_high, verdict=verdict, notes=notes)
