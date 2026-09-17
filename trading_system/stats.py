"""策略评估统计 — Sharpe / 回撤 / 偏度峰度 / PSR / DSR。

DSR（Deflated Sharpe Ratio，Bailey & López de Prado 2014）完整实现：
  PSR(SR*) = Φ( (SR̂ − SR*)·√(T−1) / √(1 − γ3·SR̂ + (γ4−1)/4·SR̂²) )
  DSR = PSR(SR* = E[max_N SR])，其中
  E[max_N SR] ≈ √V·[ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ]
  N = 试验（参数组合）次数，V = 各试验 SR 的方差（回退 1/(T−1)），γ = Euler–Mascheroni。

只用标准库（statistics.NormalDist），不引入 scipy。
"""

from __future__ import annotations

import math
from statistics import NormalDist

_NORM = NormalDist()
_EULER_GAMMA = 0.5772156649015329


def annualized_sharpe(returns: list[float], periods_per_year: int = 252) -> float:
    """日收益序列 → 年化夏普（无风险利率按 0）。"""
    rs = [r for r in returns if r is not None and not math.isnan(r)]
    if len(rs) < 3:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def skewness(returns: list[float]) -> float:
    rs = [r for r in returns if not math.isnan(r)]
    n = len(rs)
    if n < 3:
        return 0.0
    m = sum(rs) / n
    m2 = sum((r - m) ** 2 for r in rs) / n
    if m2 <= 0:
        return 0.0
    m3 = sum((r - m) ** 3 for r in rs) / n
    return m3 / (m2 ** 1.5)


def kurtosis(returns: list[float]) -> float:
    """非超额峰度（正态 = 3）——DSR 公式用的正是这个口径。"""
    rs = [r for r in returns if not math.isnan(r)]
    n = len(rs)
    if n < 4:
        return 3.0
    m = sum(rs) / n
    m2 = sum((r - m) ** 2 for r in rs) / n
    if m2 <= 0:
        return 3.0
    m4 = sum((r - m) ** 4 for r in rs) / n
    return m4 / (m2 * m2)


def max_drawdown(equity_curve: list[float]) -> float:
    """净值曲线的最大回撤（正数，如 0.15 = 15%）。"""
    peak, mdd = -float("inf"), 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1 - v / peak)
    return mdd


def probabilistic_sharpe_ratio(sr_hat: float, sr_star: float, t: int,
                               skew: float, kurt: float) -> float:
    """PSR：真实夏普超过基准 SR* 的概率。"""
    if t < 2:
        return 0.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr_hat + (kurt - 1) / 4.0 * sr_hat ** 2))
    z = (sr_hat - sr_star) * math.sqrt(t - 1) / denom
    return _NORM.cdf(z)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """零假设（SR=0）下 N 次独立试验的最大夏普期望。"""
    if n_trials <= 1:
        return 0.0
    z1 = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(max(var_sr, 1e-12)) * ((1 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe_ratio(sr_hat: float, t: int, skew: float, kurt: float,
                          n_trials: int, trial_srs: list[float] | None = None) -> float:
    """DSR：扣除多重检验（试参次数）后，策略夏普仍显著为正的概率。

    n_trials 必须诚实填报（调参试了多少组合就报多少，WFA 下
    保守取 网格大小 × 折数）。trial_srs 为各试验的样本内夏普，
    用于估计 V[SR]；缺省回退 1/(T-1)。
    """
    if trial_srs and len(trial_srs) >= 2:
        m = sum(trial_srs) / len(trial_srs)
        var_sr = sum((s - m) ** 2 for s in trial_srs) / (len(trial_srs) - 1)
    else:
        var_sr = 1.0 / max(t - 1, 1)
    sr_star = expected_max_sharpe(max(n_trials, 1), var_sr)
    return probabilistic_sharpe_ratio(sr_hat, sr_star, t, skew, kurt)
