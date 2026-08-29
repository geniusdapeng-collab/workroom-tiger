# WorkLoom 治理外壳接入设计（GOVERNANCE）

> 本文档定义财神爷交易内核（Python，仓库根目录）与 WorkLoom IM 治理外壳（`governance/`，TypeScript）的接入关系。依据：[research/UPGRADE_PLAN_v3.md](../research/UPGRADE_PLAN_v3.md) §2。

## 一、分工原则

| 原则 | 含义 |
|---|---|
| **内核零改写红线** | 决策栈、阈值、风控公式以 `trading_system/config.py` 为 single source of truth（白皮书附录"单一口径"）。治理外壳不得另立第二套阈值 |
| **外壳只做治理不做决策** | WorkLoom 不产出任何交易评分；负责把交易动作变成可追责事件（五元化留痕）、把白皮书红线变成机器级保险的第二道锁（围栏 block 级基线） |
| **双留痕互验** | 内核 17 环节 ExecutionTracer 点名不变；外壳每个动作记 RuleImpact；两本账可互相验真 |

## 二、底座能力复用清单（WorkLoom 代码快照 2026-08-21 main，已核验）

| 能力 | 代码位置 | 在老虎交易中的用途 |
|---|---|---|
| 五元事件库（哈希链） | `governance/packages/base/workdata/` | 交易动作/审批/围栏命中的 append-only 留痕 |
| 围栏引擎 auto/review/block | `governance/packages/base/fence-engine/` | 白皮书阈值 → R-T1~R-T15 block 级基线（从 config 生成） |
| Quest 任务循环 | `governance/packages/runtime/src/loop.ts` | 盘前/盘中/盘后/周末工作流编排，断点重放幂等 |
| night-shift 夜班班组 | `governance/packages/base/night-shift/` | 美股时段（北京夜间）无人值守守夜 + 清晨决策包 |
| 审批卡片 | `governance/packages/base/review-console/` | review 级动作（实盘阶段启用阻塞式） |
| 组织记忆 | `governance/packages/base/workdata/`（pgvector） | 复盘结论/驳回样本的沉淀与检索 |
| model-router | `governance/packages/base/model-router/` | LLM 调用谷时调度与逐事件计量 |
| site 官网 | `governance/apps/site/` | 责任界面（清晨决策包/决策回放/围栏日志） |
| bundles 六装配槽 | `governance/bundles/hotel/`（先例） | `bundles/trading/` 角色包的规范来源 |

## 三、内核 ↔ 外壳衔接点

```
内核 main.py 各环节 ──事件钩子──▶ 外壳事件适配器（Quest 工具，registerWriteActions 注册）
                                      │
                                      ├─ 五元化：Who(内核Agent) × Context(市场/时段) × Object(标的/信号/订单)
                                      │   × Decision(action/before/after/basis=五段依据) × RuleImpact(命中围栏)
                                      ├─ 围栏求值：白皮书阈值从 config.py 生成的围栏包（生成记录进事件）
                                      └─ 回写：auto 放行 / review 挂起进清晨决策包 / block 拒绝+P0告警
```

## 四、围栏三层结构（S5 落地）

1. **基线层（block，不可改）**：R-T1 止损必执行；R-T9 单票>20%；R-T10 MRS*<4 开新仓；R-T13 ≥2R 未保护；R-T14 数据全断仍产出；R-T15 环节缺失……（完整清单见升级方案 §3.3）
2. **客户 patch 层（review 可改，只许收紧）**：Gross Cap、单票上限、r 值、日开仓笔数
3. **策略快照层**：每张交易卡片的 Pin/Psl/分批/时间止损快照，随信号审批绑定

## 五、模拟盘/实盘两阶段（冲突裁决 R1）

- **模拟盘阶段（当前）**：遵守白皮书第15章"小G 全 AI 掌控"——review 级规则仅推送清晨决策包供人观察，**不阻塞执行**
- **实盘阶段（未来）**：review 级规则启用阻塞式审批，人三手势裁决（采纳/编辑后采纳/驳回）

## 六、当前状态（S1 完成）

- [x] WorkLoom 底座代码接入 `governance/`（快照 2026-08-21 main）
- [x] 财神爷内核原样保留并跑通（148 测试全绿 + demo 端到端）
- [ ] 事件适配器（内核→五元事件）（S5）
- [ ] 围栏包从 config.py 生成器（S5）
- [ ] Quest 三市编排（S5）
- [ ] site 责任界面（S5）
