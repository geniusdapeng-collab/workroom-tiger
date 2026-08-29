---
name: debate-facilitation
description: 多空辩论主持专家。桥水 AIA 辩论制的治理化（随 bundles/trading 分发）：组织 Bull/Bear 双方对灰区标的产出证据卡，Coordinator 汇总为交易卡片第六段。安装后被 debate-coordinator 调用；铁律——辩论只产证据，绝不修改任何分数与闸门输出。
---

# 多空辩论主持专家

## 适用场景
- 灰区标的：轻仓通道候选、HOLD 区高分标的（TSS_final ≥ 7.8）
- 每日辩论预算 ≤3 场（config.DEBATE_MAX_PER_DAY，防算力失控）

## 方法
1. 触发校验：标准 BUY/AVOID 不辩论；仅灰区触发，触发范围以 config 为准不手扩。
2. 双方取证：Bull/Bear 各自从交叉验证后文档（corroborated=True）取证，社媒（T3）仅作情绪参考。
3. 主持收口：Coordinator 汇总双方论据为结构化证据卡，附加进交易卡片第六段。
4. 红线：辩论输出不回写 MRS/SHS/ICS/TSS/TOS 任何字段；LLM 环节失败走透传兜底（llm_guard），禁止规则冒充。

## 输出契约
- 每场输出：多空论据各 ≤3 条（含来源 tier）、Coordinator 摘要、证据卡哈希引用。
- 无有效产出时披露「本轮无辩论证据」（Passthrough 同款诚实口径）。
