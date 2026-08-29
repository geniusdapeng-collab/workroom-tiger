"""MRS Agent — 市场共振评分（理论第一道门）。

输入（真实数据）：
  TNX 收益率序列、VIX（+VIX9D）、SPY OHLCV、股票池收盘价（广度）
输出：MRSResult（mrs_raw / Δ极差 / k / mrs_star / regime / 仓位上限 / 闸门）

理论修复点（相对 v3）：
  1. 权重改为波段版 0.30/0.30/0.20/0.10/0.10（v3 用 0.25/0.25/0.20/0.15/0.15）
  2. Δ 改为五维【极差】max-min，k ∈ {1.0, 0.8, 0.5}（v3 误用标准差与连续映射）
"""

from __future__ import annotations

import math

import pandas as pd

from .. import config
from ..data_models import DimensionScore, MRSResult
from ..indicators import (
    aggregate, breadth_above_sma, last, pct_change_n,
    score_breadth200, score_drawdown_from_high, score_from_quantile,
    score_gex, score_sma50_slope, score_spy_position, score_tnx_20d_chg_bp,
    score_tnx_vs200, score_vix_level, score_vix_term_structure, score_0dte_share,
    sma,
)
from .base import BaseAgent


class MRSAgent(BaseAgent):
    name = "MRS-Agent"
    layer = 1

    def execute(self, context: dict) -> MRSResult:
        p = self.provider

        # S4 多市场：基准组（指数/利率/波动率）由市场规格映射；CN/HK 免费源
        # 不可得维度为 None → 该维记缺失走"剔除再归一化"（D2，不钉中性分）。
        labels = context["market_data"].get(
            "benchmark_labels", {"index": "SPY", "rate": "TNX", "vol": "VIX"})
        tnx: pd.Series | None = context["market_data"].get("tnx")     # 百分数收益率
        vix: pd.Series | None = context["market_data"].get("vix")
        vix9d: pd.Series | None = context["market_data"].get("vix9d")
        spy: pd.DataFrame = context["market_data"]["spy"]
        universe_closes: dict[str, pd.Series] = context["market_data"]["universe_closes"]

        dims: dict[str, DimensionScore] = {}
        dims["macro"] = self._macro(tnx, labels["rate"])
        dims["flow"] = self._flow(universe_closes,
                                  context["market_data"].get("precomputed"))
        dims["sent"] = self._sentiment(vix, vix9d, labels["vol"])
        dims["tech"] = self._technical(spy, labels["index"])
        dims["micro"] = self._micro()

        scores = {k: d.score for k, d in dims.items()}
        mrs_raw = aggregate(scores, config.MRS_WEIGHTS)   # None 维度再归一化剔除

        # 一致性修正：Δ = 可用维度极差
        vals = [v for v in scores.values() if v is not None]
        if not vals:
            raise RuntimeError("MRS 五维全部缺失：基准组不可用，诚实失败（D2）")
        delta = round(max(vals) - min(vals), 2)
        k = config.consistency_k(delta)
        mrs_star = round(mrs_raw * k, 2)

        # 仓位上限（白皮书附录 B.2 档位表单一口径）
        cap = self._position_cap(mrs_star)
        regime = self._regime(mrs_star)
        allow = mrs_star >= config.MRS_GATE_BLOCK

        # ---- v6.0 三重现实折扣的机器执行（白皮书§4.5/§10.4，此前仅有文档）----
        shock, shock_reason = self._detect_shock(spy, vix, labels["index"], labels["vol"])
        if shock:
            allow = False                     # Kill Switch：停止新开仓，只许减仓对冲
        liq_discount, liq_note = self._liquidity_discount(
            context["market_data"].get("stock_ohlcv", {}))
        if liq_discount < 1.0:
            cap = (round(cap[0] * liq_discount, 3), round(cap[1] * liq_discount, 3))

        dim_txt = "/".join(f"{k} {v if v is not None else '缺失'}" for k, v in scores.items())
        evidence = [
            f"MRS_raw={mrs_raw}（{dim_txt}，缺失维度已再归一化）",
            f"Δ={delta} → k={k} → MRS*={mrs_star}",
            f"regime={regime}，仓位上限 {cap[0]:.0%}-{cap[1]:.0%}，允许新开仓={allow}",
        ]
        if shock:
            evidence.append(f"⚠ Kill Switch 触发：{shock_reason} → 停止新开仓"
                            "（白皮书§10.4：动作不是建议，是制度）")
        if liq_note:
            evidence.append(liq_note)
        for d in dims.values():
            evidence.extend(d.evidence)
            for m in d.missing:
                evidence.append(f"[缺失→中性5] {d.name}: {m}")

        result = MRSResult(
            mrs_raw=mrs_raw, delta=delta, k=k, mrs_star=mrs_star,
            dimensions=dims, regime=regime, position_cap=cap,
            allow_new_positions=allow, evidence=evidence,
            shock=shock, shock_reason=shock_reason, liq_discount=liq_discount,
        )
        context["mrs"] = result
        return result

    # ---------------- 现实折扣（v6.0） ----------------

    @staticmethod
    def _detect_shock(spy: pd.DataFrame, vix: pd.Series | None,
                      index_label: str = "SPY", vol_label: str = "VIX") -> tuple[bool, str]:
        """冲击折扣（Kill Switch，白皮书§4.5）：突发冲击导致逻辑断裂。

        触发任一：指数单日跌幅 ≤ SHOCK_SPY_1D_DROP（默认 -4%）；
                  波动率单日涨幅 ≥ SHOCK_VIX_1D_SPIKE（默认 +30%）。
        波动率基准缺失（CN/HK 免费源不可得）时仅执行指数腿（缺失不编造）。
        """
        close = spy["Close"].dropna()
        if len(close) >= 2:
            spy_1d = float(close.iloc[-1] / close.iloc[-2] - 1.0)
            if spy_1d <= config.SHOCK_SPY_1D_DROP:
                return True, f"{index_label} 单日 {spy_1d:+.1%}（阈值 {config.SHOCK_SPY_1D_DROP:+.0%}），逻辑断裂级冲击"
        if vix is None:
            return False, ""
        v = vix.dropna()
        if len(v) >= 2 and float(v.iloc[-2]) > 0:
            vix_1d = float(v.iloc[-1] / v.iloc[-2] - 1.0)
            if vix_1d >= config.SHOCK_VIX_1D_SPIKE:
                return True, f"{vol_label} 单日 {vix_1d:+.0%}（阈值 {config.SHOCK_VIX_1D_SPIKE:+.0%}），恐慌冲击"
        return False, ""

    @staticmethod
    def _liquidity_discount(stock_ohlcv: dict) -> tuple[float, str]:
        """流动性折扣（白皮书§4.5）：池内成交显著低于常态 → Gross Cap ×0.8。

        口径：池内各标的成交额（Close×Volume）的 ADV20/ADV60 比值的中位数 <
        LIQ_STRESS_ADV_RATIO（默认 0.65）判定为成交显著萎缩。
        """
        ratios = []
        for df in stock_ohlcv.values():
            if df is None or len(df) < 65:
                continue
            dv = (df["Close"] * df["Volume"]).dropna()
            if len(dv) < 65:
                continue
            adv20 = float(dv.iloc[-20:].mean())
            adv60 = float(dv.iloc[-60:].mean())
            if adv60 > 0:
                ratios.append(adv20 / adv60)
        if len(ratios) < 20:
            return 1.0, ""
        ratios.sort()
        med = ratios[len(ratios) // 2]
        if med < config.LIQ_STRESS_ADV_RATIO:
            return (config.LIQ_STRESS_DISCOUNT,
                    f"流动性折扣：池内 ADV20/ADV60 中位比 {med:.2f} < "
                    f"{config.LIQ_STRESS_ADV_RATIO}（成交显著低于常态）→ 总仓位上限"
                    f"×{config.LIQ_STRESS_DISCOUNT}（白皮书§4.5）")
        return 1.0, ""

    # ---------------- 五维 ----------------

    def _macro(self, tnx: pd.Series | None, rate_label: str = "TNX") -> DimensionScore:
        if tnx is None or len(tnx.dropna()) == 0:
            return DimensionScore(
                name="macro", score=None,
                evidence=[f"宏观: 利率基准（{rate_label}）缺失 → 整维剔除再归一化（D2）"],
                missing=[f"利率基准({rate_label})"])
        tnx = tnx.dropna()
        chg_bp = (tnx.iloc[-1] - tnx.iloc[-21]) * 100 if len(tnx) > 21 else 0.0
        sma200 = tnx.tail(200).mean()
        vs200 = tnx.iloc[-1] / sma200 - 1.0 if sma200 else 0.0
        a = score_tnx_20d_chg_bp(chg_bp)
        b = score_tnx_vs200(vs200)
        score = round(0.6 * a + 0.4 * b)
        return DimensionScore(
            name="macro", score=score, sub_scores={"A_tnx_chg": a, "B_tnx_vs200": b},
            evidence=[f"宏观: {rate_label}20日变化 {chg_bp:+.0f}bp→{a}分; {rate_label}相对200D {vs200:+.1%}→{b}分 ⇒ S_macro={score}"],
        )

    def _flow(self, universe_closes: dict[str, pd.Series],
              precomputed: dict | None = None) -> DimensionScore:
        pre = precomputed or {}
        if "breadth200" in pre:               # 预计算契约：提供即信任（NaN=缺失）
            breadth = pre["breadth200"]
        else:
            breadth = breadth_above_sma(universe_closes, 200)
        a = score_breadth200(breadth) if not math.isnan(breadth) else None
        missing = [] if a is not None else ["breadth_200"]
        # AD20 分位（可选子项）：免费源缺 A/D 线，用池内 20 日涨跌家数比近似
        if "ad20_q" in pre:
            ad_q = pre["ad20_q"] if pre["ad20_q"] is not None else float("nan")
        else:
            ad_q = self._ad20_quantile(universe_closes)
        b = score_from_quantile(ad_q) if not math.isnan(ad_q) else None
        if b is None:
            missing.append("AD20")
        if a is None and b is None:
            score = None
        elif a is None:
            score = b
        elif b is None:
            score = a
        else:
            score = round(0.7 * a + 0.3 * b)
        a_txt = f"{a}分" if a is not None else "缺失"
        ev = [f"资金广度: 池内200D上方占比 {breadth}%→{a_txt} ⇒ S_flow={score}"]
        return DimensionScore(name="flow", score=score,
                              sub_scores={"A_breadth200": a} if a is not None else {},
                              evidence=ev, missing=missing)

    def _sentiment(self, vix: pd.Series | None, vix9d: pd.Series | None,
                   vol_label: str = "VIX") -> DimensionScore:
        if vix is None or len(vix.dropna()) == 0:
            return DimensionScore(
                name="sent", score=None,
                evidence=[f"情绪: 波动率基准（{vol_label}）缺失 → 整维剔除再归一化（D2）"],
                missing=[f"波动率基准({vol_label})"])
        vix_last = last(vix)
        a = score_vix_level(vix_last)
        missing: list[str] = []
        if vix9d is None or len(vix9d) == 0:
            score = a
            missing.append(f"{vol_label}9D 期限结构")
            ev = [f"情绪: {vol_label}={vix_last:.1f}→{a}分 ⇒ S_sent={score}"]
        else:
            ts = last(vix9d) / vix_last
            b = score_vix_term_structure(ts)
            score = round(0.6 * a + 0.4 * b)
            ev = [f"情绪: {vol_label}={vix_last:.1f}→{a}分; {vol_label}9D/{vol_label}={ts:.2f}→{b}分 ⇒ S_sent={score}"]
        return DimensionScore(name="sent", score=score,
                              sub_scores={"A_vix": a}, evidence=ev, missing=missing)

    def _technical(self, spy: pd.DataFrame, index_label: str = "SPY") -> DimensionScore:
        close = spy["Close"]
        c = last(close)
        sma50 = last(sma(close, 50))
        sma200 = last(sma(close, 200))
        a = score_spy_position(c, sma50, sma200)

        sma50_series = sma(close, 50).dropna()
        # v5.4 修复 off-by-one：iloc[-20] 只有 19 个交易日间隔，却按"20 日斜率"
        # 打分（阈值表按 20 日校准）；正确取 iloc[-21]（20 个间隔）。
        if len(sma50_series) > 20:
            slope = sma50_series.iloc[-1] / sma50_series.iloc[-21] - 1.0
            b = score_sma50_slope(slope)
        else:
            slope, b = float("nan"), None

        # v5.4 口径统一：回撤基准用 252 日【最高价】（与 TSS 层 extract_structure_
        # features 的 hhv252 一致）。旧代码用收盘价高点，系统性低估回撤、抬高得分。
        hh252 = float(spy["High"].tail(252).max())
        dd = c / hh252 - 1.0
        d = score_drawdown_from_high(dd)

        score = aggregate({"A": a, "B": b, "C": d}, {"A": 0.5, "B": 0.3, "C": 0.2})
        slope_txt = f"{slope:+.1%}" if b is not None else "缺失"
        b_txt = f"{b}分" if b is not None else "缺失→剔除"
        ev = [f"技术: {index_label}相对50/200D→{a}分; SMA50斜率{slope_txt}→{b_txt}; 回撤{dd:.1%}→{d}分 ⇒ S_tech={score}"]
        return DimensionScore(name="tech", score=score,
                              sub_scores={"A_pos": a, "B_slope": b, "C_dd": d}, evidence=ev)

    def _micro(self) -> DimensionScore:
        p = self.provider
        missing: list[str] = []
        gex = p.gex_billions()
        a = score_gex(gex) if gex is not None else None
        if gex is None:
            missing.append("GEX")
        share = p.zero_dte_share()
        b = score_0dte_share(share) if share is not None else None
        if share is None:
            missing.append("0DTE_share")
        if a is None and b is None:
            score = None          # 整维缺失 → MRS 聚合再归一化剔除
        else:
            vals = [(v, w) for v, w in ((a, 0.7), (b, 0.3)) if v is not None]
            score = round(sum(v * w for v, w in vals) / sum(w for _, w in vals))
        return DimensionScore(name="micro", score=score,
                              sub_scores={"A_gex": a} if a is not None else {},
                              evidence=[f"微观: GEX/0DTE 数据缺失 → 整维剔除再归一化" if score is None
                                        else f"微观: ⇒ S_micro={score}"],
                              missing=missing)

    # ---------------- 工具 ----------------

    @staticmethod
    def _ad20_quantile(universe_closes: dict[str, pd.Series]) -> float:
        """近 20 日池内上涨家数占比，相对近一年该指标的分位（A/D 线代理）。

        v4.1 修复：cur 必须取 lag=0（最近 20 日窗口），v4.0 误取最老窗口，
        导致该分位滞后约一年。
        """
        series: list[float] = []
        closes = {t: s.dropna() for t, s in universe_closes.items() if len(s.dropna()) > 272}
        if len(closes) < 30:
            return float("nan")
        for lag in range(0, 233):
            up = down = 0
            for s in closes.values():
                ret = s.iloc[-1 - lag] / s.iloc[-21 - lag] - 1.0
                if ret > 0:
                    up += 1
                elif ret < 0:
                    down += 1
            total = up + down
            series.append(up / total if total else 0.5)
        if len(series) < 60:
            return float("nan")
        cur = series[0]                     # lag=0：最近 20 日窗口
        return sum(1 for v in series if v < cur) / len(series)

    @staticmethod
    def _position_cap(mrs_star: float) -> tuple[float, float]:
        """MRS* → 总仓位上限。

        v5.4 修复：旧实现档内插值且下限多乘 0.5——档位边界处下限跳变
        （7.99→0.55 而 8.00→0.70；5.99→0.175 而 6.00→0.40），且每档底部
        上下限塌缩为同一值。白皮书附录 B.2 的单一口径是档位表本身：
        落在哪档就用哪档的区间，不做档内插值。
        """
        for low, high, cmin, cmax in config.MRS_POSITION_CAP:
            if low <= mrs_star < high:
                return (cmin, cmax)
        return (0.7, 0.9)

    @staticmethod
    def _regime(mrs_star: float) -> str:
        if mrs_star >= 8.0: return "黄金共振"
        if mrs_star >= 6.0: return "健康趋势"
        if mrs_star >= 4.0: return "震荡混乱"
        return "负向共振"
