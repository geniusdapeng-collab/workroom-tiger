"""组合层资产管理（v3.5）— 资产管理团队的内核模块。

团队四个角色（presets 见 governance/bundles/trading/presets/）：
  - 全球资产配置官 GlobalAllocator：三市仓位分布（MRS*/TOS 加权 + 组合 Gross Cap 截断）
  - 全球宏观哨兵 GlobalSentinel：全球动态快照（三市基准 + FRED 宏观包 + 时段接力）
  - 组合风险官 PortfolioRiskOfficer：跨市场敞口聚合与组合上限校验
  - 收益稳定官 ReturnSteward：组合净值曲线稳定性评估（回撤/夏普/连续性）

纪律（与全系统一致）：
  - 组合层只做预算与披露，不做分数与预测（docs/MULTI_MARKET.md）；
  - 配置方案在模拟盘阶段 auto（R-T0 编排自治），实盘走 review 审批；
  - 输入只读三市产物（result_*.json / sim_portfolio.json / FRED 序列），
    绝不改写任何单市场决策（白皮书十条铁律逐市场独立生效）。
"""

from .allocator import AllocationPlan, GlobalAllocator
from .sentinel import GlobalSentinel, GlobalSnapshot
from .steward import (PortfolioRiskOfficer, PortfolioRiskView, ReturnSteward,
                      StabilityReport)

__all__ = [
    "AllocationPlan", "GlobalAllocator",
    "GlobalSentinel", "GlobalSnapshot",
    "ReturnSteward", "StabilityReport",
    "PortfolioRiskOfficer", "PortfolioRiskView",
]
