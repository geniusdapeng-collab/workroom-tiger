# bundles/trading —— 老虎交易行业角色包

> **实际产物已迁移至 [`governance/bundles/trading/`](../../governance/bundles/trading/)**
> （S5 落地：bundle.json / schemas / fences / presets / skills / ui 六装配槽）。
> 本目录保留为指向性说明，避免与 governance 内产物出现两份漂移。

规范来源：`governance/bundles/hotel/`（WorkLoom 六装配槽先例）。
设计依据：[../../research/UPGRADE_PLAN_v3.md](../../research/UPGRADE_PLAN_v3.md) §3.5。

## 装配槽（实际位置 governance/bundles/trading/）

| 槽 | 内容 |
|---|---|
| 档案 Schema | 一标的/账户一档：基本面快照、所属产业链、风险预算、合规市场权限 |
| 对象与阶段枚举 | 对象 8 类：标的/信号/研报/仓位/订单/风控事件/组合/报告；阶段 5 个：观察期/模拟盘/小额实盘/扩量期/回撤管制期 |
| 工具集 | 行情读取（降级链接入）、交易内核管线调用、回测引擎、公告抓取；写动作一律注册并绑围栏 |
| 围栏包 | 三层结构（监管基线/客户 patch/策略快照）：R-T0~R-T15 基线 + R-P1~R-P3 patch 示例共 19 条，全部由 `scripts/gen_fences.py` 从内核 config 生成（`--check` 比对漂移） |
| Agent presets | 岗位定义清单（数量以 `governance/bundles/trading/presets/` 实际目录为准，含执行/辩论/复盘/组合层岗位）；岗位与技能引用须与 `bundle.json#provides` 对齐，由 `tests/test_bundle_manifest_consistency.py` 锁定。研究 API 的四个定向员工为 scanner/mrs/risk/review；完整日频管线执行 21 个注册步骤 |
| 工作台 UI | 清晨决策包三栏、组合仪表盘、决策回放、围栏命中日志 |

## 纪律

- 任何 preset 未声明 `fence_bindings` 禁止写动作
- 执行员是唯一可写订单的 preset（实盘阶段）
- 白皮书阈值不得在此另立第二套口径——围栏规则从 `trading_system/config.py` 生成

## 桌面 Agent 入口

本目录的 `agent-capabilities.json` / `capabilities/repo.mjs` 提供行业只读概览，并读取 `governance/bundles/trading` 的真实产物；治理目录也登记同一组四个只读行业入口。受控本地研究执行使用独立的 Tiger CLI/MCP，见 [执行合同](../../docs/TIGER-AGENT-API.md)。岗位 preset 清单不等于可独立调用的 Python 员工清单；当前实际执行员工为 scanner/mrs/risk/review。
