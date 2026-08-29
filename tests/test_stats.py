"""PSR/DSR 统计模块测试（理论 §评估，Bailey & López de Prado 2014）。"""

import math

from trading_system.stats import (
    annualized_sharpe, deflated_sharpe_ratio, expected_max_sharpe, kurtosis,
    max_drawdown, probabilistic_sharpe_ratio, skewness,
)


def test_psr_baseline():
    """sr_hat == sr_star 时 PSR = 0.5；sr_hat 更高时 > 0.5。"""
    assert probabilistic_sharpe_ratio(1.0, 1.0, 100, 0.0, 3.0) == 0.5
    assert probabilistic_sharpe_ratio(1.5, 1.0, 100, 0.0, 3.0) > 0.5
    assert probabilistic_sharpe_ratio(0.5, 1.0, 100, 0.0, 3.0) < 0.5


def test_psr_sample_size():
    """样本越长，同样的 SR 差异越显著。"""
    p_short = probabilistic_sharpe_ratio(1.0, 0.0, 20, 0.0, 3.0)
    p_long = probabilistic_sharpe_ratio(1.0, 0.0, 500, 0.0, 3.0)
    assert p_long > p_short


def test_expected_max_sharpe():
    """N=1 → 0；N 越大期望最大夏普越高（多重检验惩罚）。"""
    assert expected_max_sharpe(1, 0.01) == 0.0
    e10 = expected_max_sharpe(10, 0.01)
    e100 = expected_max_sharpe(100, 0.01)
    assert e100 > e10 > 0


def test_dsr_multiple_testing_penalty():
    """同样的样本内夏普，试参次数越多 DSR 越低。"""
    d1 = deflated_sharpe_ratio(1.0, 100, 0.0, 3.0, n_trials=1)
    d10 = deflated_sharpe_ratio(1.0, 100, 0.0, 3.0, n_trials=10)
    d100 = deflated_sharpe_ratio(1.0, 100, 0.0, 3.0, n_trials=100)
    assert d1 > d10 > d100
    # N=1 时 DSR 退化为 PSR(0)
    assert math.isclose(d1, probabilistic_sharpe_ratio(1.0, 0.0, 100, 0.0, 3.0),
                        rel_tol=1e-9)


def test_dsr_bounds():
    d = deflated_sharpe_ratio(0.8, 250, -0.2, 3.5, n_trials=27,
                              trial_srs=[0.5, 0.8, 1.1, 0.2])
    assert 0.0 <= d <= 1.0


def test_moments_and_drawdown():
    rs = [0.01, -0.02, 0.03, 0.005, -0.01] * 20
    assert abs(skewness(rs)) < 5
    assert kurtosis(rs) > 0
    assert max_drawdown([1.0, 1.2, 1.0, 1.1, 0.9]) > 0.24  # 1.2→0.9 = 25%
    assert annualized_sharpe([0.001, 0.002, -0.0005, 0.0015] * 25) > 0
