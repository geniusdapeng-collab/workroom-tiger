---
name: daily-attribution
description: 日度归因专家。复盘条线核心技能（随 bundles/trading 分发）：把每笔已结算信号逐笔归因到 MRS/SHS/ICS/TSS 快照与违规六条，日报附录 D 口径。安装后被 attribution-analyst / review-chief 调用；只读台账，不产出任何分数。
---

# 日度归因专家

## 适用场景
- 每日收盘后：journal 已结算信号逐笔归因
- 违规六条（追涨/扛单/破格/频繁/重仓/无计划）必标记

## 方法
1. 只读 journal.json 与当日日报快照：入场时 MRS*/SHS/ICS/TSS_final 四分数留痕是归因唯一依据。
2. 盈亏拆解：市场（MRS 档位）/主线（SHS 池）/个股（TSS 与执行力）三层拆开，不混为一谈。
3. 违规判定对照白皮书附录 D 六条，命中即标记并写入复盘事件（risk_event）。
4. 归因结论进组织记忆；样本 <50 时只用区间表述，不下胜率结论（校准层同款诚实口径）。

## 输出契约
- 每笔输出：结算 R、三层归因、违规标记、快照引用（memory_refs）。
- 归因不得回写任何决策输入（会计账白名单纪律，与 journal 同级隔离）。
