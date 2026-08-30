---
name: steady-return-review
description: 收益稳定性评估方法论——用可验证统计口径守护"持续良好的稳定收益"。

---

# 收益稳定性评估（Steady Return Review）

## 触发（何时用）
每日组合净值更新后；周度随诸葛团队体检联动。

## 步骤（怎么做）
1. 组合日净值 = 三市净值按日对齐加总（缺失沿用前值）。
2. 三项核心指标：最大回撤（关注线 8% / 告警线 15%）、滚动夏普（样本 ≥10）、
   连续未创新高天数（警戒线 20 日）。
3. 三档结论：稳定 / 关注 / 告警；告警时向复盘负责人诸葛提交深度归因请求。
4. 样本 <20 日只给区间表述并标注"积累中"。

## 边界（什么不做）
- 绝不承诺收益；只用已发生的净值曲线说话；
- 不做收益预测，不做市场择时建议。

## 输出契约
StabilityReport JSON：days / total_return / max_drawdown / sharpe_rolling /
days_below_high / verdict / notes[]。
