# 自我进化飞轮（D24）移植说明 · workroom-tiger/governance

> 日期：2026-08-31 · 状态：**✅ 已完成（同日闭环）——底座整体升级至 workloom-im@28c2d91 并回灌飞轮 P0，门禁全绿**

## 结论

workloom-im 底座已于 v1.10.0 落地「自我进化飞轮 P0」（D24：偏好注入主链路 /
记忆提炼与生命周期 / 反馈枚举第⑧槽 / 组织记忆中心 P23 / 进化积分卡），
并已移植到 workloom-hotel、hyperreality-system、workloom、panda-cineforge 四仓（全量门禁绿）。

**本仓 governance/ 治理壳原钉在 pre-D16 老底座**——三方合并不可行，改为整体替换升级
（packages/{shared,db,base,runtime} + apps/{server,web,webc} + scripts + vendor + bundles/hotel
以底座为准整体替换；trading bundle、交易脚本、本说明文档保留），随后回灌飞轮 P0 全部内容
（evolve 包 / 偏好注入 / P23 组织记忆中心 / RejectDialog / 第⑧槽注册）。

## 版本差距实证（governance vs workloom-im@fb96ffb）

| 依赖项 | governance 现状 | 飞轮 P0 要求 |
|---|---|---|
| 数据库迁移 | 仅到 `0005_fence_baseline_guard.sql` | 0017（含 `append_event_insert` 特权函数、双角色、`memory_usage` RLS 入列、`org_memory.subject_id`） |
| 网关写入 API | 仅旧版 `gatewayAppend`（无 `gatewayAppendOnClient`） | D16 单事务 `gatewayAppendOnClient`（记忆/校准事件与业务写同一 COMMIT） |
| 记忆 API | 无 `upsertMemoryInTx` | 提炼器/手势回写依赖事务内写入变体 |
| C 端服务网关 | `apps/server/src/service/` 不存在 | server index.ts 已含 serviceGateway 挂载 |
| workdata RLS | `memory_usage` 无 workspace_id 列 | 0013 迁移的 RLS 口径 |

## 升级路径（建议顺序）

1. 以 workloom-im@fb96ffb 为准，整体替换 `governance/packages/{shared,db,base,runtime}`；
2. 重放迁移 0006–0017 到治理库（含双角色授权与 `append_event_insert`）；
3. apps/{server,web} 按新底座 API 对账（gatewayAppend 旧签名仍兼容，但建议统一切换）；
4. 回灌飞轮 P0（本次已备好三方合并基线：base=843f0e0，冲突面见本仓 stash 记录）；
5. 写交易行业反馈枚举表 `governance/bundles/trading/feedback-enums.yml`
   （建议词表：risk.over_limit 超风控阈值 / compliance.restricted 合规限制标的 /
   data.stale 行情数据陈旧 / amount.too_high 单笔金额过高 / other）；
6. 全量门禁（typecheck + vitest + suite + verify-chain）后方可合入。

## 参考

- 底座决策：workloom-im `docs/DECISIONS.md` D24
- 变更集：workloom-im CHANGELOG [1.10.0]（commits 4addfc5..fde32c2）
