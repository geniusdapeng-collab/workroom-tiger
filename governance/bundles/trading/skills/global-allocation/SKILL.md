---
name: global-allocation
description: 全球资产配置方法论——三市仓位分布的完整 SOP（额度计算/组合闸门/截断审计）。
---

# 全球资产配置（Global Allocation）

## 触发（何时用）
每日三市全部收盘后（北京时间次日凌晨），或组合层配置复核请求。

## 步骤（怎么做）
1. 读取三市当日 result（MRS*、position_cap、picks、TOS）。
2. 各市场基础额度 = 本市场 MRS* 档位上限（白皮书附录 B）；验证期市场额外乘轻仓系数。
3. 组合闸门：Σ 额度 ≤ 组合 Gross Cap（默认 90%，客户 patch 只可加严）。
4. 超额时按质量分（MRS* × 标的 TOS 归一）从高到低截断，逐条记录截断明细。
5. 产出配置方案（预算口径）→ 组合总览披露 + 治理事件留痕。

## 边界（什么不做）
- 不产出市场分数与预测（组合层没有观点）；不产生任何交易动作；
- 不改写任何单市场决策；缺数据市场权重为 0 并如实标注。

## 输出契约
AllocationPlan JSON：date / gross_cap / markets[]（weight/local_cap/quality/note）/ truncated[] / note。
