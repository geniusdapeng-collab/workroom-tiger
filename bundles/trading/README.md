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
| 围栏包 | 三层结构（监管基线/客户 patch/策略快照），R-T1~R-T15 由 `scripts/gen_fences.py` 从内核 config 生成 |
| Agent presets | 财神爷 20 个 Agent 原职责包装 + 新增 6 个（Bull/Bear/Coordinator 辩论组、校准官、数据质量官、合规官）；复盘条线：诸葛（复盘主持）、归因分析师、统计员、策略优化师（S6 复盘闭环内核侧已实现，见 `trading_system/review/`） |
| 工作台 UI | 清晨决策包三栏、组合仪表盘、决策回放、围栏命中日志 |

## 纪律

- 任何 preset 未声明 `fence_bindings` 禁止写动作
- 执行员是唯一可写订单的 preset（实盘阶段）
- 白皮书阈值不得在此另立第二套口径——围栏规则从 `trading_system/config.py` 生成
