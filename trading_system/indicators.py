"""指标计算与理论固定映射表（single source of truth，可回测）。

本模块逐条实现《核心指标及计算公式》中的分段映射表：
- MRS 五维：macro / flow / sent / tech / micro（理论 §1）
- SHS 四因子：macro / flow / narr / micro（理论 §2）
- TSS 三组件：structure / momentum / options（理论 §3）

设计约定（理论约定）：
1. 所有指标默认使用前一交易日收盘数据。
2. 分数为 0-10 离散整数（组件聚合用 round 后保持 0-10）。
3. 指标缺失 → 该子项用中性分 5，并在 evidence 记录缺失。
4. 分位数滚动窗口 252 个交易日。

注意（公式勘误，详见 docs/UPGRADE_REPORT.md）：
- 一致性 Δ 为五维评分的【极差】(max-min)，不是标准差（旧代码用错）。
- TNX 数据源若为 Yahoo ^TNX（收益率×10 的指数点位），需先 ÷10 还原为
  百分数收益率，再计算 bp 变化；本模块统一约定输入为百分数收益率。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import config

# ============================================================
# 基础技术指标（纯函数）
# ============================================================

def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    return true_range(df).rolling(window).mean()


def adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder ADX。"""
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = true_range(df)
    atr_w = tr.ewm(alpha=1 / window, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / window, adjust=False).mean()


def rs_line(close: pd.Series, bench_close: pd.Series) -> pd.Series:
    """相对强度线：标的/基准（对齐索引后）。"""
    c, b = close.align(bench_close, join="inner")
    return c / b


def slope_pct(series: pd.Series, window: int) -> float:
    """近 window 日斜率（区间%）：series[-1]/series[-window-1] - 1。

    v5.4 修复 off-by-one：旧实现 iloc[-window] 只有 window-1 个间隔，
    却按 window 日斜率口径使用（与 pct_change_n 的 iloc[-1-n] 不一致）。
    """
    s = series.dropna()
    if len(s) < window + 1:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[-window - 1] - 1.0)


def percentile_rank(series: pd.Series, window: int = config.PERCENTILE_WINDOW) -> float:
    """最新值在过去 window 日的分位数 q ∈ [0,1]（理论约定 252）。"""
    s = series.dropna().tail(window)
    if len(s) < 20:
        return float("nan")
    return float((s < s.iloc[-1]).mean())


def last(series: pd.Series) -> float:
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else float("nan")


def pct_change_n(series: pd.Series, n: int) -> float:
    s = series.dropna()
    if len(s) < n + 1:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[-1 - n] - 1.0)


# ============================================================
# 分位 → 分数 通用映射（理论多处复用）
# ============================================================

def score_from_quantile(q: float) -> int:
    """一年分位 → 分数（固定表）。"""
    if math.isnan(q):
        return int(config.NEUTRAL_SCORE)
    if q >= 0.90: return 10
    if q >= 0.75: return 8
    if q >= 0.60: return 7
    if q >= 0.40: return 5
    if q >= 0.25: return 3
    if q >= 0.10: return 1
    return 0


def score_crowding_neutral_best(q: float) -> int:
    """“中性最好、极端扣分”映射（skew / Put-Call 分位，理论 §2.4/§3.3）。"""
    if math.isnan(q):
        return int(config.NEUTRAL_SCORE)
    if 0.40 <= q <= 0.60: return 8
    if 0.25 <= q < 0.40 or 0.60 < q <= 0.75: return 6
    if 0.10 <= q < 0.25 or 0.75 < q < 0.90: return 4
    return 2


# ============================================================
# MRS 子项映射（理论 §1.1 - §1.5）
# ============================================================

def score_tnx_20d_chg_bp(chg_bp: float) -> int:
    """子项A：TNX 20 日变化（bp）。"""
    if chg_bp <= -40: return 10
    if chg_bp <= -25: return 9
    if chg_bp <= -10: return 7
    if chg_bp <= 10: return 5
    if chg_bp <= 25: return 3
    if chg_bp <= 40: return 1
    return 0


def score_tnx_vs200(pct: float) -> int:
    """子项B：TNX 相对 200 日均线（%，如 -0.05 表示 -5%）。"""
    p = pct * 100
    if p <= -8: return 10
    if p <= -4: return 8
    if p <= -1: return 6
    if p <= 1: return 5
    if p <= 4: return 4
    if p <= 8: return 2
    return 0


def score_breadth200(breadth: float) -> int:
    """SPX 成分股在 200D 上方占比（0-100）。"""
    if breadth >= 75: return 10
    if breadth >= 65: return 9
    if breadth >= 55: return 7
    if breadth >= 45: return 5
    if breadth >= 35: return 3
    if breadth >= 25: return 1
    return 0


def score_vix_level(vix: float) -> int:
    """VIX 水平。极低 VIX 非满分（脆弱性），边界采用 (low, high] 左开右闭。"""
    if vix <= 13: return 7
    if vix <= 16: return 10
    if vix <= 20: return 7
    if vix <= 25: return 4
    if vix <= 30: return 2
    return 0


def score_vix_term_structure(ts: float) -> int:
    """TS = VIX9D / VIX。"""
    if ts <= 0.85: return 10
    if ts <= 0.95: return 7
    if ts <= 1.05: return 5
    if ts <= 1.15: return 2
    return 0


def score_spy_position(close: float, sma50: float, sma200: float) -> int:
    """子项A：价格相对均线位置。"""
    if close > sma50 > sma200: return 10
    if close > sma50 and close > sma200: return 7      # 两均线之上但 SMA50≤SMA200
    if close < sma50 and close > sma200: return 3      # 多头序中跌破 SMA50、仍守 SMA200
    if (sma50 <= close <= sma200) or (sma200 <= close <= sma50): return 5  # 空头序中夹于两均线
    if close < sma50 and close < sma200: return 0
    return 5


def score_sma50_slope(slope: float) -> int:
    """子项B：SMA50 二十日斜率（小数，如 0.015=1.5%）。"""
    p = slope * 100
    if p >= 1.5: return 10
    if p >= 0.5: return 8
    if p >= 0.0: return 6
    if p >= -0.5: return 4
    if p >= -1.5: return 2
    return 0


def score_drawdown_from_high(dd: float) -> int:
    """子项C：距 252 日高点回撤（负值小数）。"""
    p = dd * 100
    if p >= -2: return 10
    if p > -5: return 8
    if p > -10: return 6
    if p > -15: return 4
    if p > -20: return 2
    return 0


def score_gex(gex_billions: float) -> int:
    """GEX（$B）。免费数据源通常缺失 → 调用方用中性分 5 并记录。"""
    if gex_billions >= 20: return 10
    if gex_billions >= 10: return 8
    if gex_billions >= 0: return 6
    if gex_billions >= -10: return 4
    if gex_billions >= -20: return 2
    return 0


def score_0dte_share(share: float) -> int:
    if share <= 0.25: return 8
    if share <= 0.40: return 6
    if share <= 0.55: return 4
    if share <= 0.70: return 2
    return 0


# ============================================================
# SHS 子项映射（理论 §2.1 - §2.4）
# ============================================================

def tnx_trend_label(tnx_20d_chg_bp: float) -> str:
    if tnx_20d_chg_bp > 10: return "Up"
    if tnx_20d_chg_bp < -10: return "Down"
    return "Flat"


def score_sector_macro(sector_etf: str, tnx_label: str) -> int:
    """利率趋势 → 板块适配固定查表（理论 §2.1）。未知板块给中性 5。"""
    table = config.SECTOR_MACRO_MAP.get(sector_etf.upper())
    if table is None:
        return int(config.NEUTRAL_SCORE)
    return table[tnx_label]


def score_sector_r20(r20: float) -> int:
    """板块 ETF 20 日收益率。"""
    p = r20 * 100
    if p >= 8: return 10
    if p >= 5: return 8
    if p >= 2: return 7
    if p >= -2: return 5
    if p >= -5: return 3
    if p >= -8: return 1
    return 0


def score_eps_revision_up_pct(pct: float) -> int:
    if pct >= 70: return 10
    if pct >= 60: return 8
    if pct >= 50: return 7
    if pct >= 40: return 5
    if pct >= 30: return 3
    if pct >= 20: return 1
    return 0


def score_guidance_up_pct(pct: float) -> int:
    if pct >= 50: return 10
    if pct >= 40: return 8
    if pct >= 30: return 6
    if pct >= 20: return 4
    if pct >= 10: return 2
    return 0


def score_iv_pct_sector(iv_pct: float) -> int:
    """SHS 叙事子项C：IV 分位（越低越压缩，潜在预期差空间越大）。"""
    if iv_pct <= 0.20: return 9
    if iv_pct <= 0.40: return 7
    if iv_pct <= 0.60: return 5
    if iv_pct <= 0.80: return 3
    return 1


def score_iv_pct_tss(iv_pct: float) -> int:
    """TSS 期权子项C：IV 分位（过高 = 无边际优势，扣分）。"""
    if iv_pct <= 0.30: return 8
    if iv_pct <= 0.60: return 6
    if iv_pct <= 0.80: return 4
    return 2


# ============================================================
# TSS 子项映射（理论 §3.1 - §3.3）
# ============================================================

def score_dist_to_key(d_atr: float) -> int:
    """子项A：距关键位距离（ATR14 标准化）。"""
    if d_atr <= 0.5: return 10
    if d_atr <= 1.0: return 8
    if d_atr <= 1.5: return 6
    if d_atr <= 2.0: return 4
    return 2


def score_breakout_quality(flags: dict) -> int:
    """子项B：突破/回踩质量（规则化判定）。

    flags:
      breakout:        C > HHV20（发生过放量突破）
      volume_burst:    突破日 Vol/Vol20 ≥ 1.5
      weak_volume:     突破日 Vol/Vol20 < 1.2
      retest_ok:       回踩缩量（≤0.9）且最低不破 key
      retest_shabby:   回踩一般但未破位
      reclose_high:    回踩后再次收高
      false_breakouts: 近期多次假突破/回踩破位后拉回
    """
    if flags.get("false_breakouts"):
        return 3
    if flags.get("breakout") and flags.get("volume_burst"):
        if flags.get("retest_ok") and flags.get("reclose_high"):
            return 10
        if flags.get("retest_ok") or flags.get("retest_shabby"):
            return 8
        return 8
    if flags.get("breakout") and flags.get("weak_volume"):
        return 6
    if flags.get("breakout"):
        return 6
    return 0  # 关键位下方无结构、噪声区


def score_resistance_touches(touch: int) -> int:
    """子项C：流动性真空（上方 0-5% 阻力触点数，越少越真空）。"""
    if touch <= 1: return 10
    if touch <= 3: return 7
    if touch <= 5: return 4
    return 1


def score_ma_alignment_tss(close: float, sma10: float, sma20: float, sma50: float,
                           sma50_rising: bool) -> int:
    """子项A：均线结构（SMA10/20/50）。"""
    if close > sma10 > sma20 > sma50: return 10
    if close > sma20 > sma50: return 8
    if abs(close / sma20 - 1) <= 0.02 and sma50_rising: return 6
    if close < sma20 and sma50_rising: return 4
    if not sma50_rising and close < sma50: return 0
    return 4


def score_vol_contraction(vc: float) -> int:
    """子项B：vc = ATR14 / ATR50。"""
    if vc <= 0.70: return 10
    if vc <= 0.85: return 8
    if vc <= 1.00: return 6
    if vc <= 1.15: return 4
    return 2


def score_adx(adx_val: float) -> int:
    """子项C：ADX14。"""
    if adx_val >= 30: return 10
    if adx_val >= 25: return 8
    if adx_val >= 20: return 6
    if adx_val >= 15: return 4
    return 2


# ============================================================
# 组件聚合（理论固定权重）
# ============================================================

def aggregate(scores: dict[str, float | None], weights: dict[str, float],
              renormalize: bool = True) -> float:
    """加权聚合子项分数 → 0-10（round 到 2 位）。

    renormalize=True（v4.1 起默认）：缺失子项（None/NaN）被剔除、
    剩余权重按比例再归一化 —— 避免缺失数据把复合分钉向中性 5、
    在暗中收紧开仓闸门（v4.0 校准缺陷：SHS 主线池因此永不触发）。
    全缺失时回退中性 5 分。
    """
    avail = {k: v for k, v in scores.items()
             if v is not None and not (isinstance(v, float) and math.isnan(v))}
    if not avail:
        return config.NEUTRAL_SCORE
    if renormalize:
        wsum = sum(weights[k] for k in avail)
        val = sum(weights[k] * avail[k] for k in avail) / wsum
    else:
        total_w = sum(weights.values())
        val = sum(avail.get(k, config.NEUTRAL_SCORE) * w
                  for k, w in weights.items()) / total_w
    return round(max(0.0, min(10.0, val)), 2)


# ============================================================
# 结构特征提取（TSS 用，真实 OHLCV 计算）
# ============================================================

def extract_structure_features(df: pd.DataFrame) -> dict:
    """从 OHLCV 提取 TSS 结构子项所需特征（numpy 加速，语义不变）。

    返回:
      key_level:      关键位（20 日高点 HHV20 的前值，即突破触发位）
      close, atr14, atr50, d_atr, touch, flags(breakout 系列),
      sma10/20/50, sma50_rising, vc, adx14, vol_ratio, hhv252, dd_252
    """
    df = df.dropna()
    n = len(df)
    c_arr = df["Close"].values.astype(float)
    h_arr = df["High"].values.astype(float)
    l_arr = df["Low"].values.astype(float)
    v_arr = df["Volume"].values.astype(float)

    # TR 一次计算 → ATR14/ATR50（避免两次 true_range）
    prev_c = np.roll(c_arr, 1)
    tr = np.maximum(h_arr - l_arr,
                    np.maximum(np.abs(h_arr - prev_c), np.abs(l_arr - prev_c)))
    tr_s = pd.Series(tr, index=df.index)
    a14 = tr_s.rolling(14).mean()
    a50 = tr_s.rolling(50).mean()
    adx14 = adx(df, 14)

    close, high, vol = df["Close"], df["High"], df["Volume"]
    sma10 = last(sma(close, 10))
    sma20 = last(sma(close, 20))
    sma50 = last(sma(close, 50))
    sma50_series = sma(close, 50).dropna()
    sma50_rising = bool(len(sma50_series) > 20 and sma50_series.iloc[-1] > sma50_series.iloc[-20])

    c = float(c_arr[-1])
    a14_last = float(a14.iloc[-1]) if not math.isnan(a14.iloc[-1]) else float("nan")
    a50_last = float(a50.iloc[-1]) if not math.isnan(a50.iloc[-1]) else float("nan")

    # HHV20 滚动窗口（向量化）：hhv_end[j] = max(h[j-19:j+1])
    if n >= 20:
        hhv_end = np.lib.stride_tricks.sliding_window_view(h_arr, 20).max(axis=1)
    else:
        hhv_end = np.maximum.accumulate(h_arr)

    def prev_hhv(idx: int) -> float:
        """max(h[idx-20:idx]) — 不含当日的 20 日高点。

        hhv_end[k] = max(h[k:k+20])，故窗口 h[idx-20:idx]（终于 idx-1）
        对应 hhv_end[idx-20]，严禁用 idx-1 起点窗口（那是未来数据）。
        """
        j = idx - 20
        if j >= 0:
            return float(hhv_end[j]) if n >= 20 else float(hhv_end[idx - 1])
        return float(h_arr[:idx].max()) if idx > 0 else float("nan")

    # 关键位：不含当日的 20 日最高价（突破锚）
    key_level = float(h_arr[-21:-1].max()) if n > 21 else float(h_arr.max())

    # 突破检测：近 5 日内是否有放量突破（收盘站上此前 HHV20）
    breakout = False
    volume_burst = False
    weak_volume = False
    vol20 = vol.rolling(20).mean().values
    lookback = min(5, n - 21)
    breakout_day_idx: Optional[int] = None
    for i in range(1, lookback + 1):
        idx = n - i
        ph = prev_hhv(idx)
        if c_arr[idx] > ph:
            breakout = True
            breakout_day_idx = idx
            vr = float(v_arr[idx] / vol20[idx]) if vol20[idx] > 0 else 1.0
            if vr >= 1.5:
                volume_burst = True
            elif vr < 1.2:
                weak_volume = True
            break

    # 回踩检测：突破日之后曾回落至 key 附近（±1 ATR）
    retest_ok = False
    retest_shabby = False
    reclose_high = False
    if breakout_day_idx is not None and breakout_day_idx < n - 1:
        for j in range(breakout_day_idx + 1, n):
            near_key = abs(float(l_arr[j]) - key_level) <= a14_last
            if near_key:
                vr = float(v_arr[j] / vol20[j]) if vol20[j] > 0 else 1.0
                if vr <= 0.9 and l_arr[j] >= key_level * 0.99:
                    retest_ok = True
                elif l_arr[j] >= key_level * 0.98:
                    retest_shabby = True
        if (retest_ok or retest_shabby) and c > key_level:
            reclose_high = True

    # 假突破计数：近 60 日收盘站上 HHV20 后 5 日内跌回 key 下方的次数
    false_breakouts = False
    if n > 80:
        idxs = np.arange(n - 60, n - 5)
        ph_arr = np.array([prev_hhv(i) for i in idxs])
        hit = c_arr[idxs] > ph_arr
        if hit.any():
            fwd_min = np.array([c_arr[i + 1:i + 6].min() for i in idxs[hit]])
            false_breakouts = bool((fwd_min < ph_arr[hit] * 0.99).sum() >= 2)

    # 上方阻力触点：收盘价上方 0-5% 区间内，过去 120 日最高价落入的触点簇数。
    # 同一阻力带会被反复试探：相邻 5 个交易日内的落入归并为一个触点
    # （以最近一次落入日为锚滑动归并），替代旧版"按日计数后 min(8) 封顶"的近似。
    touch = 0
    if n > 120:
        w_highs = h_arr[-120:]
        hit_idx = np.flatnonzero((w_highs >= c * 1.001) & (w_highs <= c * 1.05))
        last_hit = -6
        for i in hit_idx:
            if i - last_hit > 5:
                touch += 1
            last_hit = int(i)

    d_atr = abs(c - key_level) / a14_last if a14_last and not math.isnan(a14_last) else 9.9

    hhv252 = float(h_arr[-252:].max())
    dd_252 = c / hhv252 - 1.0 if hhv252 > 0 else 0.0

    vc = a14_last / a50_last if a50_last and not math.isnan(a50_last) else float("nan")

    flags = {
        "breakout": breakout,
        "volume_burst": volume_burst,
        "weak_volume": weak_volume,
        "retest_ok": retest_ok,
        "retest_shabby": retest_shabby,
        "reclose_high": reclose_high,
        "false_breakouts": false_breakouts,
    }
    return {
        "close": c, "key_level": key_level,
        "atr14": a14_last, "atr50": a50_last, "d_atr": d_atr,
        "touch": touch, "flags": flags,
        "sma10": sma10, "sma20": sma20, "sma50": sma50,
        "sma50_rising": sma50_rising, "vc": vc, "adx14": last(adx14),
        "hhv252": hhv252, "dd_252": dd_252,
    }


def breadth_above_sma(universe_closes: dict[str, pd.Series], window: int) -> float:
    """一篮子股票收盘价在 SMA(window) 上方的占比（0-100）。"""
    above, total = 0, 0
    for s in universe_closes.values():
        s = s.dropna()
        if len(s) < window + 1:
            continue
        total += 1
        if s.iloc[-1] > s.iloc[-window:].mean():
            above += 1
    return round(above / total * 100, 1) if total else float("nan")
