"""统一闸门引擎（v6.0）— 生产（RiskManagerAgent）与回测（_gate_day）共用。

背景（架构债）：主线池判定与三分数联动开仓规则曾分别写在
agents/risk_manager_agent.py 和 backtest.py 的 _gate_day 里——v5.4 审计
发现两处对"广度缺失"的处理已经漂移（生产改了回测没改就是这类 bug 的温床）。
本模块是唯一实现，所有阈值只从 config 读。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


def main_pool_eligible(shs: float, breadth: float,
                       shs_main: float | None = None) -> bool:
    """主线入池（白皮书§5.3 硬规则）：SHS ≥ 阈值 且 广度 ≥ 60%（有证据）。

    广度缺失（NaN）不得入主线池——"主线是还在持续被资金推进"，
    无广度证据 = 无推进证据。
    """
    th = shs_main if shs_main is not None else config.SHS_MAIN_POOL
    return (shs >= th and not math.isnan(breadth)
            and breadth >= config.BREADTH_HEALTHY)


def sub_pool_eligible(shs: float, shs_sub: float | None = None) -> bool:
    th = shs_sub if shs_sub is not None else config.SHS_SUB_POOL
    return shs >= th


@dataclass
class GateDecision:
    passed: bool
    standard: bool                # True=标准做多 / False=轻仓试错
    reason: str = ""


def pass_gates(mrs_star: float, shs: float, tss_final: float,
               in_main: bool, in_sub: bool, chain_hot: bool,
               mrs_gate: float | None = None, shs_sub: float | None = None,
               tss_gate: float | None = None, light_tss: float | None = None,
               mrs_light_lo: float | None = None) -> GateDecision:
    """三分数联动开仓判定（白皮书§9.2 / 附录 B.1，单一口径）：

    标准做多：MRS* ≥ mrs_gate 且（入主线池，或链处热区且 SHS ≥ 次主线）且 TSS ≥ tss_gate
    轻仓试错：（MRS* ∈ [mrs_light_lo, mrs_gate) 或 仅在次主线池）且 TSS ≥ light_tss
    """
    mrs_gate = mrs_gate if mrs_gate is not None else config.OPEN_LONG["mrs"]
    shs_sub = shs_sub if shs_sub is not None else config.SHS_SUB_POOL
    tss_gate = tss_gate if tss_gate is not None else config.OPEN_LONG["tss"]
    light_tss = light_tss if light_tss is not None else config.LIGHT_PROBE["tss"]
    mrs_light_lo = (mrs_light_lo if mrs_light_lo is not None
                    else config.LIGHT_PROBE["mrs_lo"])

    mrs_light = mrs_light_lo <= mrs_star < mrs_gate
    standard = (mrs_star >= mrs_gate
                and (in_main or (chain_hot and shs >= shs_sub))
                and tss_final >= tss_gate)
    if standard:
        return GateDecision(True, True)
    probe = ((mrs_light or (in_sub and not in_main)) and tss_final >= light_tss)
    if probe:
        return GateDecision(True, False)
    return GateDecision(False, False)
