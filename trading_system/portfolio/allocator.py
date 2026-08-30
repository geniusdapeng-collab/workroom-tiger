"""全球资产配置官（GlobalAllocator）— 三市仓位分布。

输入：三市当日 result_*.json（MRS*、position_cap、picks、TOS 摘要）
输出：AllocationPlan——各市场目标额度权重与截断说明（预算，不是观点）。

配置方法论（v1 口径，config.PORTFOLIO_* 单一口径）：
  1. 各市场基础额度 = 本市场 MRS* 档位上限（白皮书附录 B，不变）；
  2. 组合闸门：Σ 目标额度 ≤ 组合 Gross Cap（默认 0.90，客户 patch 只可加严）；
  3. 超额截断：按各市场"质量分"（MRS* × 放行标的平均 TOS 归一化）从高到低
     保留，低质量市场被截断的部分如实记录（可审计）；
  4. 验证期市场（graduation=False）额度独立按轻仓系数封顶，不占标准额度；
  5. 无有效数据的市场权重为 0 并如实标注（D2 延伸到组合层）。
"""

from __future__ import annotations

import glob
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

log = logging.getLogger("portfolio.allocator")


@dataclass
class MarketAllocation:
    market: str
    available: bool                 # 当日是否有有效数据
    mrs_star: float | None = None
    local_cap: float = 0.0          # 本市场档位上限（上界）
    quality: float = 0.0            # 质量分（截断排序依据）
    weight: float = 0.0             # 配置权重（截断后）
    note: str = ""


@dataclass
class AllocationPlan:
    date: str
    gross_cap: float
    markets: list[MarketAllocation] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)   # 被组合闸门截断的市场
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "date": self.date, "gross_cap": self.gross_cap,
            "markets": [vars(m) for m in self.markets],
            "truncated": self.truncated, "note": self.note,
        }


class GlobalAllocator:
    """读取三市产物，产出配置方案（预算层，不产生任何交易动作）。"""

    def __init__(self, gross_cap: float | None = None):
        self.gross_cap = (gross_cap if gross_cap is not None
                          else float(getattr(config, "PORTFOLIO_GROSS_CAP", 0.90)))
        sr = getattr(config, "LIGHT_PROBE", {}).get("size_ratio", 0.35)
        # size_ratio 可能为区间（如 (0.30, 0.40)）——取中值
        self.light_ratio = (float(sr[0] + sr[1]) / 2.0
                            if isinstance(sr, (list, tuple)) else float(sr))

    @staticmethod
    def _load_result(out_dir: str) -> dict | None:
        files = sorted(glob.glob(str(Path(out_dir) / "result_*.json")))
        if not files:
            return None
        try:
            return json.loads(Path(files[-1]).read_text())
        except Exception as e:
            log.warning("读取 %s 失败: %s", files[-1], e)
            return None

    def _market_alloc(self, mid: str, result: dict | None,
                      graduated: bool) -> MarketAllocation:
        if not result:
            return MarketAllocation(market=mid, available=False,
                                    note="当日无有效数据（独立诚实失败）")
        mrs = (result.get("mrs") or {})
        mrs_star = mrs.get("mrs_star")
        cap = mrs.get("position_cap")
        local_cap = float(cap[1]) if isinstance(cap, (list, tuple)) and len(cap) == 2 else 0.0
        picks = result.get("picks") or []
        tos_vals = [float(getattr(p, "tos", 0) or (p.get("tos") or 0))
                    for p in picks] if picks and isinstance(picks[0], dict) else []
        avg_tos = (sum(tos_vals) / len(tos_vals)) if tos_vals else 0.0
        ms = float(mrs_star) if isinstance(mrs_star, (int, float)) else 0.0
        # 质量分：市场环境 × 标的质量（无量纲，仅用于截断排序）
        quality = round(ms * max(avg_tos, ms) / 100.0, 4)
        alloc = MarketAllocation(market=mid, available=True, mrs_star=ms,
                                 local_cap=local_cap, quality=quality)
        if not graduated:
            alloc.local_cap = round(local_cap * self.light_ratio, 4)
            alloc.note = f"新市场验证期：轻仓通道 ×{self.light_ratio:.2f}"
        return alloc

    def plan(self, results: dict[str, dict | None],
             graduated: dict[str, bool] | None = None,
             date: str = "") -> AllocationPlan:
        """results: {"us": result|None, "cn": ..., "hk": ...}"""
        graduated = graduated or {}
        allocs = [self._market_alloc(mid, results.get(mid),
                                     graduated.get(mid, mid == "us"))
                  for mid in ("us", "cn", "hk")]
        plan = AllocationPlan(date=date, gross_cap=self.gross_cap)
        active = [a for a in allocs if a.available]
        # 组合闸门：Σ local_cap ≤ gross_cap，超出按质量分从高到低截断
        total = sum(a.local_cap for a in active)
        if total > self.gross_cap and active:
            ranked = sorted(active, key=lambda a: a.quality, reverse=True)
            budget = self.gross_cap
            for a in ranked:
                take = min(a.local_cap, budget)
                if take < a.local_cap:
                    plan.truncated.append(
                        f"{a.market}: {a.local_cap:.0%}→{take:.0%}（组合闸门截断）")
                    a.local_cap = take
                budget -= take
                if budget <= 0:
                    for rest in ranked[ranked.index(a) + 1:]:
                        if rest.local_cap > 0:
                            plan.truncated.append(f"{rest.market}: {rest.local_cap:.0%}→0%（组合闸门截断）")
                        rest.local_cap = 0.0
                    break
        # 权重归一（以截断后额度占比表示配置重心；总和可能 < 100%，即现金位）
        denom = sum(a.local_cap for a in active)
        for a in allocs:
            a.weight = round(a.local_cap / self.gross_cap, 4) if self.gross_cap else 0.0
        plan.markets = allocs
        cash = max(0.0, self.gross_cap - denom)
        plan.note = (f"组合额度使用 {denom:.0%}/{self.gross_cap:.0%}，预留现金位 {cash:.0%}；"
                     "配置方案为预算口径，不产生任何交易动作。")
        return plan
