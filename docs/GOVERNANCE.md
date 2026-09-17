# WorkLoom 治理外壳接入设计（GOVERNANCE）

> 本文档定义财神爷交易内核（Python，仓库根目录）与 WorkLoom IM 治理外壳（`governance/`，TypeScript）的接入关系。依据：[research/UPGRADE_PLAN_v3.md](../research/UPGRADE_PLAN_v3.md) §2。

## 一、分工原则

| 原则 | 含义 |
|---|---|
| **内核零改写红线** | 决策栈、阈值、风控公式以 `trading_system/config.py` 为 single source of truth（白皮书附录"单一口径"）。治理外壳不得另立第二套阈值 |
| **外壳只做治理不做决策** | WorkLoom 不产出任何交易评分；负责把交易动作变成可追责事件（五元化留痕）、把白皮书红线变成机器级保险的第二道锁（围栏 block 级基线） |
| **双留痕互验** | 内核 17 环节 ExecutionTracer 点名不变；外壳每个动作记 RuleImpact；两本账可互相验真 |

## 二、底座能力复用清单（WorkLoom 代码快照 2026-08-21 main，已核验）

| 能力 | 代码位置 | 在老虎全球资产管理中的用途 |
|---|---|---|
| 五元事件库（哈希链） | `governance/packages/base/workdata/` | 交易动作/审批/围栏命中的 append-only 留痕 |
| 围栏引擎 auto/review/block | `governance/packages/base/fence-engine/` | 白皮书阈值 → R-T1~R-T15 block 级基线（从 config 生成） |
| Quest 任务循环 | `governance/packages/runtime/src/loop.ts` | 盘前/盘中/盘后/周末工作流编排，断点重放幂等 |
| night-shift 夜班班组 | `governance/packages/base/night-shift/` | 美股时段（北京夜间）无人夜班值守 + 清晨决策包 |
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

## 六、当前状态（S6 完成）

- [x] WorkLoom 底座代码接入 `governance/`（快照 2026-08-21 main）
- [x] 财神爷内核原样保留并跑通（252 测试全绿 + demo 端到端）
- [x] 事件适配器（内核→五元事件）（S5：`trading_system/governance_bridge.py`，pipeline 环节/L4 闸门/模拟盘成交/合规命中 → 五元事件 + SHA-256 哈希链）
- [x] 围栏包从 config.py 生成器（S5：`scripts/gen_fences.py` → `governance/bundles/trading/fences/trading-baseline.yml`，R-T1~R-T15）
- [x] bundles/trading 角色包（S5：`governance/bundles/trading/` 六装配槽；根目录 `bundles/trading/` 保留指向性说明）
- [x] 复盘闭环（S6：`trading_system/review/` 诸葛团队——日度归因+违规六条（附录D）/周度体检三档/月度 WFA 提案（DSR 不显著自动 reject）；`review.daily` 注册进 STEP_REGISTRY 延迟环节；审批流 `--review-list/--review-approve/--review-reject`（驳回原因必填），approve 次日生效并披露（复用 --use-tuned 纪律）；纪要 `reports/复盘_<日期>.md` 进治理事件）
- [x] 双语文档（S6：`README_EN.md`、`docs/PROJECT_INTRO.md` / `docs/PROJECT_INTRO_EN.md`）
- [x] site 责任界面（S5：`site/governance.html`——决策包/回放验真/围栏日志）
- [x] 全栈落地（2026-08-30）：`governance/scripts/seed-trading.ts`（33 Agent/18 围栏/6 技能/7 触发器落库）+ `scripts/stack_setup.sh` 一键全栈
- [x] 事件入库适配器（2026-08-30）：`governance/scripts/ingest-tiger-events.ts`（幂等，`decision.kernel_hash` 双链互验；全库 130 事件验链一致）
- [x] **Quest 夜班编排**（2026-08-30）：`governance/scripts/quest-trading-nightly.ts`——自检→内核全链路→事件入库→官网发布，走 runQuest 围栏瀑布（R-T0 模拟盘自治窗口）+ 回执实证（无产物=未核实不得转完成）+ replay 幂等续跑；E2E：completed 4/4、replay 0.0 分钟全跳过、全库 173 事件验链一致；cron 已登记（scripts/install_cron.sh，22:00）
- [ ] Quest 驱动实盘券商下单（实盘阶段，审批流阻塞式启用）
