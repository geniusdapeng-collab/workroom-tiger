"""产业链周期 Agent（Industry Chain Cycle，新增大模块）。

回答 SHS 无法回答的问题：钱在产业链内部流到了哪一环、还能流多久。

每条链计算四个分量（config.ICS_WEIGHTS）：
  1. chain_rs      链整体相对强度（等权组合 vs SPY，63 日 RS 斜率一年分位）
  2. link_breadth  链内广度（成员 50D 上方占比 → 固定映射）
  3. rotation      环节轮动信号：
                     上游领涨且中游跟随  → 传导中（点火期）     8 分
                     中游领涨（主升确认）→ 主升                9 分
                     下游领涨而龙头滞涨  → 补涨/过热           3 分
                     各环节无序          → 混沌                5 分
  4. cycle_stage   周期阶段：复苏 7 / 扩张 9 / 过热 4 / 衰退 2

ICS ≥ 7.0 标记为可交易热区；选股阶段对热区链内标的给乘性加成（≤1.15），
衰退链内标的给 0.95 惩罚（见 tss_agent / risk_manager_agent）。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .. import config
from ..chains import CHAINS
from ..data_models import ChainLink, ChainState
from ..indicators import (
    breadth_above_sma, last, percentile_rank, pct_change_n, rs_line, sma,
)
from .base import BaseAgent

_STAGE_SCORE = {"复苏": 7.0, "扩张": 9.0, "过热": 4.0, "衰退": 2.0}


class ChainCycleAgent(BaseAgent):
    name = "Chain-Cycle-Agent"
    layer = 2.5

    def execute(self, context: dict) -> list[ChainState]:
        spy_close: pd.Series = context["market_data"]["spy"]["Close"]
        stock_data: dict[str, pd.DataFrame] = context["market_data"]["stock_ohlcv"]

        states: list[ChainState] = []
        for cid, cdef in CHAINS.items():
            state = self._score_chain(cid, cdef, spy_close, stock_data)
            if state:
                states.append(state)

        states.sort(key=lambda s: s.ics, reverse=True)
        context["chains"] = states
        context["chain_map"] = {s.chain_id: s for s in states}
        self.log.info("ICS 完成: %s",
                      [(s.chain_id, s.ics, s.stage, s.leading_link) for s in states])
        return states

    # ------------------------------------------------------------

    def _score_chain(self, cid: str, cdef: dict, spy_close: pd.Series,
                     stock_data: dict[str, pd.DataFrame]) -> ChainState | None:
        links: dict[str, ChainLink] = {}
        all_closes: dict[str, pd.Series] = {}

        for link_key in ("upstream", "midstream", "downstream"):
            ldef = cdef[link_key]
            closes = {t: stock_data[t]["Close"] for t in ldef["tickers"] if t in stock_data}
            all_closes.update(closes)
            link = ChainLink(name=ldef["name"], tickers=list(closes.keys()))
            if closes:
                rets = [pct_change_n(s, 20) for s in closes.values()]
                rets = [r for r in rets if not math.isnan(r)]
                link.momentum = float(np.mean(rets)) if rets else float("nan")
                rs_vals = []
                for s in closes.values():
                    rs = rs_line(s, spy_close)
                    q = percentile_rank(rs.pct_change(20))
                    if not math.isnan(q):
                        rs_vals.append(q)
                link.rs_20 = float(np.mean(rs_vals)) if rs_vals else float("nan")
            links[link_key] = link

        if len(all_closes) < 3:
            return None

        # 1) 链整体相对强度：等权组合指数 vs SPY
        combo = self._equal_weight_index(all_closes)
        rs = rs_line(combo, spy_close)
        rs_slope_63 = rs.pct_change(63)
        q_chain = percentile_rank(rs_slope_63)
        from ..indicators import score_from_quantile
        s_chain_rs = score_from_quantile(q_chain)

        # 2) 链内广度
        breadth = breadth_above_sma(all_closes, 50)
        if math.isnan(breadth):
            s_breadth = config.NEUTRAL_SCORE
        else:
            s_breadth = self._breadth_score(breadth)

        # 3) 环节轮动
        rotation_score, rotation_signal, leading = self._rotation(links)

        # 4) 周期阶段
        stage, stage_reason = self._classify_stage(rs, breadth, links)
        s_stage = _STAGE_SCORE[stage]

        ics = round(
            s_chain_rs * config.ICS_WEIGHTS["chain_rs"]
            + s_breadth * config.ICS_WEIGHTS["link_breadth"]
            + rotation_score * config.ICS_WEIGHTS["rotation"]
            + s_stage * config.ICS_WEIGHTS["cycle_stage"], 2)

        hot = ics >= config.ICS_HOT
        evidence = [
            f"{cdef['name']}: 链RS分位{q_chain:.2f}→{s_chain_rs}分; 链内广度{breadth:.0f}%→{s_breadth}分",
            f"轮动: {rotation_signal}（领涨环节: {leading}）→{rotation_score}分",
            f"周期阶段: {stage}（{stage_reason}）→{s_stage}分 ⇒ ICS={ics} {'[热区]' if hot else ''}",
        ]
        return ChainState(
            chain_id=cid, name=cdef["name"], ics=ics, stage=stage,
            stage_score=s_stage, leading_link=leading, links=links,
            breadth=breadth, rotation_signal=rotation_signal, hot=hot,
            evidence=evidence,
        )

    # ------------------------------------------------------------

    @staticmethod
    def _equal_weight_index(closes: dict[str, pd.Series]) -> pd.Series:
        """等权组合净值指数（对共同交易日归一化）。"""
        norm = []
        for s in closes.values():
            s = s.dropna()
            if len(s) > 130:
                norm.append(s / s.iloc[-130])
        if not norm:
            return pd.Series(dtype=float)
        df = pd.concat(norm, axis=1).dropna()
        return df.mean(axis=1)

    @staticmethod
    def _breadth_score(breadth: float) -> float:
        if breadth >= 75: return 10
        if breadth >= 65: return 8
        if breadth >= 55: return 7
        if breadth >= 45: return 5
        if breadth >= 35: return 3
        if breadth >= 25: return 1
        return 0

    @staticmethod
    def _rotation(links: dict[str, ChainLink]) -> tuple[float, str, str]:
        mom = {k: l.momentum for k, l in links.items() if not math.isnan(l.momentum)}
        if len(mom) < 3:
            return 5.0, "数据不足", ""
        leading = max(mom, key=mom.get)
        up, mid, down = mom["upstream"], mom["midstream"], mom["downstream"]
        lead_names = {"upstream": "上游", "midstream": "中游", "downstream": "下游"}

        if leading == "midstream" and mid > 0:
            return 9.0, "中游领涨·主升确认", lead_names[leading]
        if leading == "upstream" and mid >= up - 0.02 and up > 0:
            return 8.0, "上游点火·向中游传导", lead_names[leading]
        if leading == "downstream" and mid < down * 0.5:
            return 3.0, "下游补涨·龙头滞涨（过热警示）", lead_names[leading]
        if leading == "downstream" and mid > 0:
            return 6.0, "下游扩散·趋势后段", lead_names[leading]
        return 5.0, "各环节无序", lead_names[leading]

    @staticmethod
    def _classify_stage(rs: pd.Series, breadth: float, links: dict[str, ChainLink]) -> tuple[str, str]:
        rs = rs.dropna()
        if len(rs) < 70:
            return "复苏", "数据不足，默认早期"
        slope_63 = rs.iloc[-1] / rs.iloc[-63] - 1.0
        slope_20 = rs.iloc[-1] / rs.iloc[-20] - 1.0
        rs_pos = rs.iloc[-1] / rs.tail(252).max() - 1.0

        mid_mom = links["midstream"].momentum if not math.isnan(links["midstream"].momentum) else 0.0

        if slope_63 < 0 and slope_20 > 0:
            return "复苏", f"63日RS仍弱({slope_63:+.1%})但20日转正({slope_20:+.1%})"
        if slope_63 > 0 and slope_20 > 0 and (math.isnan(breadth) or breadth >= 55):
            if rs_pos > -0.02 and slope_20 < slope_63 / 3 and mid_mom <= 0:
                return "过热", f"贴近高点但动量减速且中游滞涨"
            return "扩张", f"63/20日RS共振上行({slope_63:+.1%}/{slope_20:+.1%})，广度{breadth:.0f}%"
        if slope_63 < 0 and slope_20 < 0:
            return "衰退", f"RS 63/20日共振下行({slope_63:+.1%}/{slope_20:+.1%})"
        if rs_pos > -0.03 and slope_20 < 0:
            return "过热", f"高位滞涨（RS距高点{rs_pos:.1%}，20日转弱）"
        return "复苏", "过渡态，按早期处理"


def chain_bonus(chain_state: ChainState | None) -> float:
    """选股乘性加成：热区且阶段为复苏/扩张 → 最高 1.15；衰退 → 0.95。"""
    if chain_state is None:
        return 1.0
    if chain_state.stage == "衰退":
        return 0.95
    if chain_state.hot and chain_state.stage in ("复苏", "扩张"):
        return 1.0 + min(config.CHAIN_BONUS_MAX - 1.0,
                         max(0.0, (chain_state.ics - 5.0) * 0.03))
    return 1.0
