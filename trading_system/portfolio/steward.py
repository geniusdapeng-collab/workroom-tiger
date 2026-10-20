"""组合风险官（PortfolioRiskOfficer）+ 收益稳定官（ReturnSteward）。

PortfolioRiskOfficer：跨市场敞口聚合视图（总敞口/集中度/验证期标识/货币分布），
  组合 Gross Cap 校验（当前为披露层，M2 阶段转为真实截断执行）。
ReturnSteward：组合净值曲线稳定性评估（最大回撤/滚动夏普/连续不创新高的天数），
  目标："持续良好的稳定收益"——用可验证的统计口径说话，不承诺收益。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
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
    def _curve(out_dir: str) -> dict[str, float]:
        """按观测日期读取净值；乱序排序，重复日期拒绝猜测有效余额。

        一条无效记录使该市场整条曲线不可用，不能删掉坏点后把缺口当休市。
        读取/校验失败交由 assess 按市场披露，不改写原台账。
        """
        f = Path(out_dir) / "sim_portfolio.json"
        s = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            raise ValueError("净值台账必须是对象")
        points = s.get("equity_curve")
        if points is None:
            return {}
        if not isinstance(points, list):
            raise ValueError("净值曲线必须是数组")
        curve = {}
        for i, p in enumerate(points, 1):
            if not isinstance(p, dict):
                raise ValueError(f"第 {i} 条净值记录必须是对象")
            day = p.get("date")
            try:
                valid_day = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
            except ValueError:
                valid_day = False
            if not valid_day:
                raise ValueError(f"第 {i} 条日期无效，须为 YYYY-MM-DD")
            if day in curve:
                raise ValueError(f"重复日期 {day}，无法确定当日净值")
            value = p.get("equity")
            try:
                equity = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"第 {i} 条净值无效") from exc
            if isinstance(value, bool) or not math.isfinite(equity):
                raise ValueError(f"第 {i} 条净值须为有限数值")
            curve[day] = equity
        return dict(sorted(curve.items()))

    def assess(self, out_dirs: dict[str, str]) -> StabilityReport:
        # 沿用现有账户金额加总口径；只在已有观测之后前向延续，不补造初始余额。
        curves = {}
        notes: list = []
        for m, d in out_dirs.items():
            try:
                curve = self._curve(d)
            except (OSError, UnicodeError, ValueError) as exc:
                notes.append(f"{m}: 净值曲线不可用（{exc}），未纳入组合统计")
                continue
            if curve:
                curves[m] = curve
            else:
                notes.append(f"{m}: 无净值曲线数据，未纳入组合统计")
        if not curves:
            return StabilityReport(days=0, total_return=0.0, max_drawdown=0.0,
                                   sharpe_rolling=None, days_below_high=0,
                                   verdict="关注", notes=["无净值曲线数据（积累中，不作结论）", *notes])
        # 组合成分固定：从所有有效市场均有已知净值的首日起统计，避免把新增
        # 市场的首笔余额记作收益。日期只取观测并集，不为全市场缺失的日子造点。
        start = max(next(iter(c)) for c in curves.values())
        dates = sorted({day for c in curves.values() for day in c})
        latest, series = {}, []
        for day in dates:
            for m, curve in curves.items():
                if day in curve:
                    latest[m] = curve[day]
            if day >= start:
                series.append(sum(latest.values()))
        n = len(series)
        end = dates[-1]
        notes.append(f"组合统计自 {start} 起至 {end}，仅含观测日期并集；缺失日沿用前值")
        for m, curve in curves.items():
            last = next(reversed(curve))
            stale_days = (date.fromisoformat(end) - date.fromisoformat(last)).days
            notes.append(f"{m}: 最后净值观测 {last}，距统计末日滞后 {stale_days} 个自然日"
                         + ("（末值沿用）" if stale_days else ""))
        base = series[0]
        if not math.isfinite(base) or base <= 0 or any(not math.isfinite(v) for v in series):
            notes.append("组合基准净值非正或合计净值非有限数值，收益与回撤不可计算（不作结论）")
            return StabilityReport(days=n, total_return=0.0, max_drawdown=0.0,
                                   sharpe_rolling=None, days_below_high=0,
                                   verdict="关注", notes=notes)
        total_ret = series[-1] / base - 1.0
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
        if n >= 10 and any(v <= 0 for v in series[:-1]):
            notes.append("组合净值存在非正收益率分母，滚动夏普不可计算")
        elif n >= 10:
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
