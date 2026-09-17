---
name: portfolio-risk-budget
description: 组合风险预算口径——跨市场敞口聚合、组合 Gross Cap 校验、集中度管理。
---

# 组合风险预算（Portfolio Risk Budget）

## 触发（何时用）
每日配置方案产出后复核；持仓变动时即时重算。

## 步骤（怎么做）
1. 三市模拟盘台账聚合：总净值、市场分布、货币分布。
2. 组合 Gross Cap 校验（当前披露口径；M2 转真实截断执行）。
3. 集中度提示：单一市场占比 >60% 显性标注（提示非否决）。
4. 与配置官联动：超限时要求配置官重出截断方案。

## 边界（什么不做）
- 单市场风控归风控官（保守派一票否决权不变）；
- 组合层只加总与校验，不越权改单市场决策。

## 输出契约
PortfolioRiskView JSON：total_equity / by_market{} / concentration{} / gross_cap / notes[]。
