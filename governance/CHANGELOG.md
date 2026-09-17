# Changelog

本文件记录 WorkLoom IM 底座的变更历史。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

## [base-sync-1.12.0] - 2026-09-02 · 基座同步：技能保鲜环 P1（自动同步 + 上行回流 + 官方运营台）

> 自 workloom-im@1.12.0 同步（vendored 基座公共段一致）：skill-ops P1 完全体（autosync/reflux/console）+ 迁移 0019/0020 + SkillDistBanner 通栏 + skillOps.reflux/console 路由 + 官方端点 + suite Y 域 P1 用例与 R-26 加固 + L2 审批 tier 红线修复（l4_chairman）。
> 门禁：typecheck 全绿 · vitest skill-ops 28/28 · suite 全绿（干净库口径）。

## [0.1.7] - 2026-08-21 · 用例集扩充批次（审计第 6 轮）

### 安全修复

- **#41 审批手势类型白名单（P1）**：非法手势（bogus）此前穿透校验被静默当作「驳回」写库（绕过 L5.2 原因必填）。validateGesture 入口白名单，非法类型抛 INVALID_GESTURE。

### 测试基建

- **suite 326→371 用例**：新增 O 店长日常场景（16）/ P 系统层（14）/ Q 异常压测（15）三域；suite 命令加 TOOL_UNVERIFIED_RATE=0 确定性执行。

### 门禁验证

- ✅ suite 371/371 ×2 连跑 · base 152/152 · runtime 12/12 · typecheck 全绿

## [0.1.6] - 2026-08-21 · CI 门禁 + 决策记录批次（审计第 5 轮）

### 基建

- **CI 质量门禁**（.github/workflows/ci.yml）：push/PR to main 触发——PG17+pgvector service、迁移种子幂等双跑、verify-chain、typecheck、三包测试（DB 集成全开）、suite 326 用例、web build、dsh-gate E6。GitHub 实测 success。
- **docs/DECISIONS.md 补建**：ADR 从此入库；D13 登记事件编号锁与哈希链粒度决策（tenant 锁 + workspace 链为有意设计，附三方案否决论证）。

### 修复

- **#40** uninstallSkill 撤销清单读安装时快照（与 #17 口径对齐，作者改绑定后留痕不再失真）。

### 门禁验证

- ✅ CI ci-gate success ×3（GitHub 实测）· base 152/152 · runtime 12/12 · suite 326/326 · typecheck 全绿

## [0.1.5] - 2026-08-21 · 全场景测试套件批次（审计第 4 轮）

### 测试基建

- **`pnpm suite` 全场景套件**（scripts/suite.ts）：14 域 326 条场景用例逐条执行——三模式意图路由/网关瀑布/围栏判定/事件检索/审批流/IM 通道/夜班/技能/记忆/巡检/模型路由/desktop 高危与多模态/注入边界/并发压测 + HTTP E2E 权限矩阵（spawn 真实 server）。用例前缀隔离、可重跑、失败汇总报告。

### 套件暴露修复

- **#36** 检索时间白名单补 `+`（东八区 ISO 格式此前被误拒）。
- **#37** 意图路由疑问词增强（句中/句尾疑问词 + 动作词优先——「房价是多少」不再误判 quest）。
- **#38** 巡检派单事件挂 sessionId=threadId（P2 线程事件流完整）。
- **#39** recall NL 用例时间窗解耦（跨天运行假红消除）。

### 门禁验证

- ✅ suite 326/326（复跑稳定）· base 152/152 · runtime 12/12 · shared 4/4 · typecheck 全绿 · web build 绿 · verify-chain 100/100

## [0.1.4] - 2026-08-20 · 深度对抗测试批次（审计第 3 轮）

### 安全修复

- **#32 种子哈希链与生产口径不一致（P1）**：seed 用 JSON.stringify 键序算哈希 vs 生产 canonicalJson——种子 100 条用生产口径重算全部不符（同链两种算法混杂）。seed 统一导入 eventHash（zod parse 后对象），新增 `pnpm db:verify-chain` 全库链验证工具。
- **#33 写操作统一角色守卫（P1）**：readonly 实测可派遣 Quest 等 14 个写操作无服务端校验（前端隐藏未配服务端强制）。新增 writeProcedure / capabilityWriteProcedure 统一接入。
- **#35 网关 actor/who 身份一致性**：分叉伪造归因留痕无机制兜底，段①新增一致性校验。

### 功能正确性修复

- **#34 Quest 挂起审批通过后可恢复执行（P1）**：此前审批通过线程永卡 pending_review（replay 死循环）。runQuest 加载已批准挂起步骤映射，批准即带 approvalRef 执行（basis 留痕「经审批 \<id\> 批准执行」），Quest 生命周期闭环。

### 门禁验证

- ✅ base 152/152 · runtime 12/12 · shared 4/4 · typecheck 全绿 · web build 绿 · E6 dsh-gate 全绿
- ✅ verify-chain 100/100 一致 · 权限实测 readonly 全 403 / manager 正常 · PII/DSL 对抗 20 项全过

## [0.1.3] - 2026-08-20 · dsh rc.8 升级 + 安全加固批次（审计第 2 轮）

### 变更

- **dsh 升级 0.1.0-rc.6 → 0.1.0-rc.8**：vendor/dsh 全量替换（integrity 与 registry 逐字符一致）；dsh-gate pin rc.8 + node-pty rebuild。rc.8 新能力：Codex / Claude Code 作为按需安装的 subagent Profile Bundle（web profile 已装 `@deepseek-ai/dsh-subagent-claude-code` / `@deepseek-ai/dsh-subagent-codex`）；SQLite 新存储格式不向下兼容（升级前备份 DSH_HOME 数据目录）。**升级策略变更（项目所有者 2026-08-20 决策）：官方任何新版本（含 rc/beta/alpha）即升，不再等稳定版。**

### 安全加固

- **#30 biz_events TRUNCATE 触发器**（0004 迁移）：行级触发器不拦 TRUNCATE，表 owner 此前可清空事件库；语句级触发器对全角色生效，清库只能 DROP+重迁移。
- **#31 fence_rules 全局基线写入收口**（0005 迁移）：app/gateway 角色禁写 `workspace_id='*'` 行（原 RLS WITH CHECK 放行，任何工作区上下文可污染全租户基线），仅 owner 可写。

### 门禁验证

- ✅ base 150/150 · runtime 11/11 · shared 4/4 · typecheck 全绿 · web build 绿 · E6 dsh-gate 全绿（rc.8）
- ✅ 安全门禁 7/7（新增 TRUNCATE 拒 / fence `*` 写拒）· 迁移 0001–0005 + seed 幂等复跑

## [0.1.2] - 2026-08-20 · 审计修复批次（首轮独立审计，详见 docs/AUDIT.md）

### 安全修复

- **#22 RLS 事务级上下文失效（P0）**：#2/#20 把 `set_config(...,false)` 改事务级 `true`，但 15 个文件 40+ 封装无显式事务，autocommit 下设置语句结束即失效 → RLS 恒 NULL → 登录/审批/夜班/巡检/技能/召回 fail-closed 全不可用（此前 DB 集成测试全部 skip 未暴露，实测 37/144 红）。统一改为 BEGIN→set_config→fn→COMMIT/ROLLBACK；decide() 重构消除事务嵌套；测试断言池直查统一事务封装（原断言恒 0 行假绿/假红）。
- **#23 team 技能跨工作区互覆盖（P1）**：skills 全局表无 RLS，teamSkillId 仅名称派生，同名技能 ON CONFLICT 互覆盖。ID 内嵌 workspaceId（`skill-t-<ws>-<slug>`）；listSkills 按 scope 隔离；installSkill 追加本工作区归属校验（他区按 NOT_SIGNED 拦截留痕）。

### 功能正确性修复

- **#24 技能围栏绑定运行时不生效（P1）**：resolveAgentFenceBindings 无消费点，装配只读 preset 声明。assemblePreset 同事务并入 skill_installs 安装时快照（安装即生效、卸载即收缩）。
- **#26 appendEvent 幂等丢弃返回错误 hash/seq**：#4 只修了 appendEventIdempotent，主路径同根残留；去重时同事务回读 DB 真实值。
- **#27 routeIntent 超时未取消 LLM 调用**：classify 签名无 signal，AbortController 只赢 race；signal 接线到分类器（#7 名不副实补正）。
- **#28 冲突审批 approval_id 同毫秒碰撞**：makeReadableId("AP", Date.now()%100000) 熵不足，改事件派生 apr-e-\<eventId\>（同 loop.ts 口径）。
- **#29 IM 入站并发重推双写（TOCTOU）**：查重与写事件非原子；新增 im_inbound_dedupe 幂等键表（0003 迁移）原子占位，事件写失败补偿删占位。
- **顺带**：withObjectLock 的 SET LOCAL statement_timeout 挪到 BEGIN 后（事务外无效果，锁等待无超时兜底）；dispatch 并发上限检查移入事务（原池直查 fail-open 恒 0 行）。

### 测试健壮性

- **#25 runtime 全流程测试 flaky（~27% 失败率）**：静态 import 使 TOOL_UNVERIFIED_RATE=0 设置被击穿（模块级常量提前定型）；loop.js 改动态 import。H-15 测试 hotel 资产路径改 import.meta.url 定位（原 cwd 敏感）+ finally 还原 industry（防污染残留）。
- security-audit 增 #22 回归用例（autocommit 反例 fail-closed + 池连接卫生）。

### 数据库迁移

- 新增 `packages/db/migrations/0003_im_inbound_dedupe.sql`：`im_inbound_dedupe` 幂等键表（PK(workspace_id,channel,channel_msg_id)，RLS 同口径）。

### 门禁验证

- ✅ typecheck 全绿（6 个项目）
- ✅ shared 4/4 · **base 148/148（含 54 个原 skip 的 DB 集成测试，×3 连跑）** · runtime 11/11（×5 连跑）
- ✅ 安全门禁 6/6（append-only 双保险 / 旁路直写防控 / RLS 隔离）· seed 幂等复跑
- ✅ server `/health` + `/trpc/system.health` 200（db:up）· web build 绿 · 端到端 loginAs→members/threads/approvals 实测通过

## [0.1.1] - 2026-08-20 · Bug 修复批次

### 安全修复

- **#9 提示词注入防护**：`routeIntent` 的 LLM 分类器 prompt 用 `<user_input>` 结构化分隔符隔离用户输入，声明分隔符内为数据非指令，防止用户输入劫持分类结果绕过审批路由（F3.2）。
- **#2/#20/N RLS 配置统一**：全部非测试代码的 `set_config('app.workspace_id', ..., false)` 改为 `true`（事务级），消除会话级 RLS 变量泄漏到连接池的跨租户数据泄漏风险（F7.1/L7.1）。
- **#17 技能 fence_bindings 安装时快照**：`skill_installs` 表新增 `fence_bindings_snapshot` 列，安装时快照绑定；运行时 `resolveAgentFenceBindings` 读快照而非 `skills.fence_bindings` 实时值，防止技能作者更新绑定绕过 E8.1 冲突检测。
- **#16 isSignedSource DB 约束**：`skills` 表新增 CHECK 约束 `skills_team_id_format`，强制 `level='team'` 的技能 ID 必须以 `skill-t-` 开头，与 `isSignedSource` 逻辑一致，DB 层防伪造签名。

### 数据一致性修复

- **#10 expireSweep 写事件**：过期审批状态变更现在经网关写 `approval.expired` 事件，不再只改表不写事件，符合铁律 1（一切写入必经网关，F5.7/E5.3）。
- **#11 runQuest 重放跳过被阻塞步骤**：`existingStepIds` 只收录真正执行完成（auto）的步骤，排除 block/review 事件（按 `basis` 前缀「熔断：」「越围栏挂起：」识别），避免重放时跳过从未执行的步骤（E3.3/H-5）。
- **#4 appendEventIdempotent 去重返回正确 hash**：去重时从 DB 取回已存在事件的真实 `hash`/`seq` 返回，避免调用方拿到错误 hash 断链（L1.4）。

### 功能正确性修复

- **#12 模型路由熔断不丢弃回答**：熔断时 `RouteResult` 新增 `budgetExceeded` 标志并仍返回 `text`，避免白烧 token（F6.5/L6.4）。
- **#13 resumeNight 区分夜班暂停与手动暂停**：`threads` 表新增 `paused_by` 列；`pauseAll` 标记 `paused_by='night-shift'`；`resumeNight` 只恢复该标记的线程，不覆盖用户手动暂停（F4.3/E4.2）。
- **#5 isWriteAction 与围栏规则同步**：网关新增 `registerWriteActions` 运行时注册接口，行业 Bundle 新增写类动作后可注册到网关，避免硬编码前缀未覆盖而放行未声明 fence_bindings 的 Agent 写动作（F2.10）。
- **#6 confirmNight 围栏快照严谨化**：围栏版本快照查询限定 `is_baseline=true` + `ORDER BY version DESC` 确定性排序，避免取到非基线规则或随机版本（F2.6）。
- **#19 currentWindow 支持非跨午夜窗口**：峰谷窗口判定支持跨午夜（`start > end`，如 22:00-08:00）和非跨午夜（`start < end`，如 09:00-17:00）两种配置（F6.3）。
- **#21 回执失败不传播**：`handleGestureCallback` 的 `driver.sendText` 失败时只记录日志，不让成功的审批操作「看起来失败」（F5.5）。
- **#18 P1 dispatchState 卡 typing**：`dispatch` 的 `finally` 块用 `text.trim()` 判断而非闭包旧值 `draft`，避免成功派遣后 DispatchBar 卡在 typing 态。

### 设计改进

- **#8 PII 银行卡加 Luhn 校验**：`BANKCARD` 规则新增 `verify` 二次校验，用 Luhn 算法过滤订单号/时间戳等非卡数字，避免误脱敏破坏业务语义（F1.10）。
- **#7 routeIntent 超时取消 LLM 调用**：超时后调用 `AbortController.abort()` 真正取消底层 LLM 请求，避免 token 浪费（F3.2）。
- **#14/#15 withObjectLock 改用阻塞锁 + 64位 key**：改用 `pg_advisory_xact_lock`（阻塞版，内核管理等待队列）+ md5 前 16 位转 bigint 的 64 位 hash key，避免轮询占用 gateway 连接 5 秒和 `hashtext` 32 位碰撞（E2.5）。

### 架构优化

- **K mock 工具随机返回 synced:false**：demo 工具通过 `TOOL_UNVERIFIED_RATE` 环境变量控制（默认 10%）随机返回 `synced:false`，让 E3.7 回执校验路径在开发阶段就被走到。
- **L 连接池扩容**：`app` 池 10→30，`gateway` 池 4→20，`owner` 池 2→5，避免并发请求耗尽连接。

### 数据库迁移

- 新增 `packages/db/migrations/0002_bugfix.sql`：
  - `threads` 表新增 `paused_by` 列 + 索引（#13）
  - `skill_installs` 表新增 `fence_bindings_snapshot` 列（#17）
  - `skills` 表新增 CHECK 约束 `skills_team_id_format`（#16）

### 门禁验证

- ✅ typecheck 全绿（13 个项目）
- ✅ shared 包测试 4/4 绿
- ✅ base 包测试 90 passed（54 skipped 为 DB 集成测试）
- ✅ runtime 包测试 5 passed（4 skipped 为 DB 集成测试）

### 未纳入本批次

- **#1/A 双池事务一致性（Outbox 方案）**：架构性大改造，影响面贯穿全栈，需单独评估，留待下个版本。

## [治理壳 2.0.0] - 2026-08-31 · 底座整体升级 + 自我进化飞轮 P0（D24）

- **底座升级**：governance 由 pre-D16 老底座整体升级至 workloom-im@28c2d91——packages/{shared,db,base,runtime}、apps/{server,web,webc}、scripts、vendor、bundles/hotel 以底座为准整体替换；迁移 0001–0017 全新重放（双角色 + append_event_insert + memory_usage RLS + org_memory.subject_id）。
- **飞轮 P0（D24）**：evolve 全域（偏好注入主链路 / 记忆提炼与生命周期 / 反馈枚举第⑧槽 / 进化积分卡）+ P23 组织记忆中心 + RejectDialog 五页接线。
- **行业实物**：bundles/trading/feedback-enums.yml（11 条：risk.over_limit/risk.concentration/compliance.restricted/compliance.window/timing.too_early/timing.too_late/style.over_trade…）；交易专属脚本（seed-trading / quest-trading-nightly / ingest-tiger-events / proposal-bridge）保留并登记 package.json。
- **门禁**：typecheck 全绿 · vitest 全量（apps/server 120/120 修正默认库指向 workroom_tiger）· suite 445/445 · verify-chain 一致 · trading 种子与三市触发器装载正常。
