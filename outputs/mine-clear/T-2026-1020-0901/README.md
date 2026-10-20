# Tiger 审计问题与可执行断言

本目录随任务 `T-2026-1020-0901` 的 PR 25 提交，保存审计时的问题、原始证据和可执行断言。它是审计规范快照，不是修复或独立验收的执行回执。

| 快照 | 问题卡 | 审计提交 |
|---|---|---|
| `audit-initial/ledger.json` | MC-001 至 MC-035 | `5fc58513363f535de66ba7c4f4b7df1d7daa5578` |
| `audit-candidate/ledger.json` | MC-036、MC-037 | `34de5b08c80b08c75c9d336cb3b5e64c2c481b20` |
| `audit-interface/ledger.json` | MC-038 至 MC-040 | `e750605583a4b41bce459ec97215c8cfdddfec91` |
| `audit-plan-visibility/ledger.json` | MC-041 | `e530d4a18c2a77cfe720a33c6b001a0ece1dde1e` |

卡片保留 `open`、断言保留 `not-run`，修复与验收基线为空。这些字段明确表示本快照没有执行断言，不能作为交付通过凭证。代码证据仍绑定原审计提交及文件 SHA256；随附的非代码审计证据按原字节复制。`packaging.json` 绑定输入台账摘要、卡片清单和证据摘要。

实际修复和独立验收分别使用干净提交执行，保存于本次交付附件的 `outputs/tiger-audit/` 中。附件记录完整源提交、实际命令、退出码、测试数、执行角色、证据摘要和门禁结果。最终报告分别列出候选验收与合并后 main 核验；历史回执不改写为新提交或 main 的执行记录。将最终候选提交自身的验收回执再次提交会改变候选 SHA，因此此处不复制或伪造这种自引用回执。

从仓库根目录检查快照：

```bash
node scripts/delivery/mine-clear.mjs plan --ledger outputs/mine-clear/T-2026-1020-0901/audit-initial/ledger.json --repo .
node scripts/delivery/mine-clear.mjs plan --ledger outputs/mine-clear/T-2026-1020-0901/audit-candidate/ledger.json --repo .
node scripts/delivery/mine-clear.mjs plan --ledger outputs/mine-clear/T-2026-1020-0901/audit-interface/ledger.json --repo .
node scripts/delivery/mine-clear.mjs plan --ledger outputs/mine-clear/T-2026-1020-0901/audit-plan-visibility/ledger.json --repo .
```

断言的声明使用环境中的 `python3`、`node` 和 `pnpm`，没有嵌入审计机器的绝对可执行文件路径。运行前按仓库声明准备 Python 依赖和 governance 工具链，并复制台账及其相对证据目录到独立输出目录。实际执行器会记录解析后使用的命令和提交；声明本身不证明执行成功。

数据库断言必须使用专属测试 PostgreSQL，按相应测试文件要求显式启用 `RUN_*_PG_TESTS`／`RUN_DB_TESTS`，不得把跳过计为通过。种子导入测试独占另一测试库。需要服务端或浏览器的断言串行运行，独占其测试端口和浏览器资源；不与正在运行的产品预览共用生命周期。浏览器夹具必须明确标为本地合成数据，不能当作生产市场数据、真实后端或真实模型调用。

MC-037 的普通指针遮挡与颜色检测分开记证：`bgOverlap` 表示颜色证据未验证，不能计为颜色通过。MC-034 的专门颜色回归仍需独立通过。所有最终状态以实际执行附件为准。

MC-038、MC-039 的原始红测绑定被测文件在审计提交中的字节，并逐项保存运行源前后摘要；它们是在并行修复工作区中取得的局部失败证据，不能冒充整个干净提交的验收。MC-039 的通知聚合交互增加了显式展开入口，MC-034、MC-035、MC-037 的既有通知操作只增加普通展开步骤，保留全部原断言与阈值。MC-040 同时校验两份验收 profile 和实际 JSX 路由，避免不存在的夜班地址被非空错误页伪装成通过。

MC-041 来自独立验收中的真实数据库、详情接口和页面证据。详情接口仅增加只读计划投影，页面显示完整有序标签；验收观察异步更新时仍使用原始派单时刻和截止时间。旧 MC-031、MC-036 的 CLI 夹具同时补入新静态依赖，保留原有失败、超时及可见性断言。交付附件分别记录 MC-001 至 MC-040 的历史候选验收、MC-041 的新候选验收及受影响旧断言的回归，不把不同提交的运行合称为同一提交的全量执行。
