"""映射表单元测试 — 逐条对照《核心指标及计算公式》。"""

import math

from trading_system import config
from trading_system.indicators import (
    aggregate, score_adx, score_breadth200, score_breakout_quality,
    score_dist_to_key, score_drawdown_from_high, score_from_quantile,
    score_ma_alignment_tss, score_resistance_touches, score_sector_macro,
    score_sector_r20, score_sma50_slope, score_spy_position,
    score_tnx_20d_chg_bp, score_tnx_vs200, score_vix_level,
    score_vix_term_structure, score_vol_contraction, tnx_trend_label,
)


def test_tnx_chg_table():
    assert score_tnx_20d_chg_bp(-50) == 10
    assert score_tnx_20d_chg_bp(-30) == 9
    assert score_tnx_20d_chg_bp(-15) == 7
    assert score_tnx_20d_chg_bp(0) == 5
    assert score_tnx_20d_chg_bp(15) == 3
    assert score_tnx_20d_chg_bp(30) == 1
    assert score_tnx_20d_chg_bp(50) == 0


def test_tnx_vs200_table():
    assert score_tnx_vs200(-0.10) == 10
    assert score_tnx_vs200(-0.05) == 8
    assert score_tnx_vs200(-0.02) == 6
    assert score_tnx_vs200(0.0) == 5
    assert score_tnx_vs200(0.02) == 4
    assert score_tnx_vs200(0.06) == 2
    assert score_tnx_vs200(0.10) == 0


def test_breadth200_table():
    assert score_breadth200(80) == 10
    assert score_breadth200(66) == 9
    assert score_breadth200(55) == 7
    assert score_breadth200(50) == 5
    assert score_breadth200(40) == 3
    assert score_breadth200(30) == 1
    assert score_breadth200(10) == 0


def test_vix_table_fragile_calm():
    """极低 VIX 不给满分（脆弱性设计）。"""
    assert score_vix_level(12) == 7
    assert score_vix_level(14.5) == 10
    assert score_vix_level(18) == 7
    assert score_vix_level(22) == 4
    assert score_vix_level(28) == 2
    assert score_vix_level(35) == 0


def test_vix_ts_table():
    assert score_vix_term_structure(0.80) == 10
    assert score_vix_term_structure(0.90) == 7
    assert score_vix_term_structure(1.00) == 5
    assert score_vix_term_structure(1.10) == 2
    assert score_vix_term_structure(1.20) == 0


def test_spy_position_table():
    assert score_spy_position(600, 590, 580) == 10   # C>SMA50>SMA200
    assert score_spy_position(600, 590, 595) == 7    # C>SMA50且C>SMA200，但SMA50≤SMA200
    assert score_spy_position(585, 580, 590) == 5    # 空头序(SMA50<SMA200)中C夹于两均线
    assert score_spy_position(585, 590, 570) == 3    # 多头序中C<SMA50但C>SMA200
    assert score_spy_position(560, 590, 580) == 0    # C<SMA50且C<SMA200


def test_slope_table():
    assert score_sma50_slope(0.02) == 10
    assert score_sma50_slope(0.01) == 8
    assert score_sma50_slope(0.001) == 6
    assert score_sma50_slope(-0.002) == 4
    assert score_sma50_slope(-0.01) == 2
    assert score_sma50_slope(-0.02) == 0


def test_drawdown_table():
    assert score_drawdown_from_high(-0.01) == 10
    assert score_drawdown_from_high(-0.04) == 8
    assert score_drawdown_from_high(-0.08) == 6
    assert score_drawdown_from_high(-0.12) == 4
    assert score_drawdown_from_high(-0.18) == 2
    assert score_drawdown_from_high(-0.25) == 0


def test_consistency_k_theory():
    """Δ 为极差：Δ<4 → 1.0；4≤Δ≤6 → 0.8；Δ>6 → 0.5。"""
    assert config.consistency_k(3.9) == 1.0
    assert config.consistency_k(4.0) == 0.8
    assert config.consistency_k(6.0) == 0.8
    assert config.consistency_k(6.1) == 0.5


def test_sector_macro_map():
    assert score_sector_macro("XLK", "Down") == 9
    assert score_sector_macro("XLK", "Up") == 2
    assert score_sector_macro("XLF", "Up") == 8
    assert score_sector_macro("UNKNOWN", "Flat") == 5


def test_tnx_trend_label():
    assert tnx_trend_label(15) == "Up"
    assert tnx_trend_label(-15) == "Down"
    assert tnx_trend_label(0) == "Flat"


def test_sector_r20():
    assert score_sector_r20(0.09) == 10
    assert score_sector_r20(0.06) == 8
    assert score_sector_r20(0.03) == 7
    assert score_sector_r20(0.0) == 5
    assert score_sector_r20(-0.03) == 3
    assert score_sector_r20(-0.07) == 1
    assert score_sector_r20(-0.10) == 0


def test_quantile_score():
    assert score_from_quantile(0.95) == 10
    assert score_from_quantile(0.80) == 8
    assert score_from_quantile(0.65) == 7
    assert score_from_quantile(0.50) == 5
    assert score_from_quantile(0.30) == 3
    assert score_from_quantile(0.15) == 1
    assert score_from_quantile(0.05) == 0
    assert score_from_quantile(math.nan) == 5  # 缺失 → 中性 5


def test_tss_structure_subitems():
    assert score_dist_to_key(0.3) == 10
    assert score_dist_to_key(2.5) == 2
    assert score_breakout_quality({"breakout": True, "volume_burst": True,
                                   "retest_ok": True, "reclose_high": True}) == 10
    assert score_breakout_quality({"false_breakouts": True, "breakout": True,
                                   "volume_burst": True}) == 3
    assert score_breakout_quality({}) == 0
    assert score_resistance_touches(1) == 10
    assert score_resistance_touches(6) == 1


def test_tss_momentum_subitems():
    assert score_ma_alignment_tss(100, 99, 98, 97, True) == 10
    assert score_ma_alignment_tss(100, 101, 99, 98, True) == 8
    assert score_vol_contraction(0.65) == 10
    assert score_vol_contraction(1.2) == 2
    assert score_adx(35) == 10
    assert score_adx(12) == 2


def test_aggregate_weights():
    # MRS 波段版权重 0.30/0.30/0.20/0.10/0.10
    val = aggregate({"macro": 8, "tech": 8, "flow": 6, "sent": 6, "micro": 6},
                    config.MRS_WEIGHTS)
    assert abs(val - 7.2) < 1e-6


def test_resistance_touch_dedup():
    """阻力触点去重：相邻 5 个交易日内的落入归并为一个触点。"""
    import numpy as np
    import pandas as pd
    from trading_system.indicators import extract_structure_features
    n = 130
    close = np.full(n, 100.0)
    high = np.full(n, 100.05)          # 常态最高在触点区间 [100.1, 105] 之下
    low = np.full(n, 99.0)
    high[70] = high[71] = high[72] = 102.0   # 触点簇 1（连续 3 日 → 1 次）
    high[110] = 102.0                        # 触点簇 2（间隔 >5 日 → +1）
    df = pd.DataFrame({"Open": close, "High": high, "Low": low,
                       "Close": close, "Volume": np.full(n, 1e6)})
    f = extract_structure_features(df)
    assert f["touch"] == 2
