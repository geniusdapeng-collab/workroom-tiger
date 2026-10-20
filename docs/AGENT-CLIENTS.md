# Tiger 接入 Codex / DeepSeek Harness

本指南面向用 Agent 调用 Tiger 的操作者和维护者。Tiger 有两条独立入口：原有 WorkLoom 能力目录提供仓库只读信息；新增 Tiger CLI/MCP 调用同一个 Python 严格工作流。根目录是 Python 引擎，治理壳在 `governance/`。不要把根目录当成 Node 工作区，也不要修改通用能力加载器来开放本地 execute。

## 1. 先检查，再选择访问模式

从仓库根目录运行。Node 要求由仓库现行运行时约束决定；新 Tiger 入口的 Python 最低要求为 3.11。推荐显式传入已安装本仓依赖的 Python；省略时先找仓库 `.venv/bin/python`，再找 PATH 中的 `python3`。系统 Python 版本不满足时，doctor 会失败，不会假装就绪。

```bash
node scripts/tiger-agent.mjs doctor --python /absolute/path/to/python
node scripts/tiger-agent.mjs describe --python /absolute/path/to/python
```

这两条命令不调用行情或模型、不创建账户、不启动业务 worker。doctor 检查实际解释器和依赖；describe 返回固定 profile、阶段顺序和限制。`trading.repo.readiness` 只检查文件布局，不能代替 doctor。

默认 `--mode readonly`。执行完整 paper 工作流必须由操作者在启动参数或项目部署配置中显式选择 `--mode paper`。请求 JSON 不能改变 mode、解释器、state-root、阶段或策略。

当前只开放 `us-public-paper-v1`：美股公开数据、USD 本地 paper 账户 `public-usd`、独立初始资金 100000 USD；不继承旧账户、不连接真实券商、不执行真实资金订单。内置语义模型关闭，合法 passthrough 及原因会进入证据和回执。外层 Codex/DSH 的模型不会自动接入内层模型。

## 2. CLI：提交一次，持续查询同一个身份

六个操作为 `doctor`、`describe`、`run`、`status`、`receipt`、`verify`。业务输入使用 `--input-file path.json` 或 `--input-file -`；stdin 必须是一个 UTF-8 JSON 对象，最大 64 KiB。重复键、未知字段、非有限数值、任意命令和路径覆写都会被拒绝。

请求文件只含以下四个字段；为一次逻辑任务分配一个持久、可恢复的幂等键：

```json
{
  "schema_version": "tiger.agent-request/v1",
  "profile": "us-public-paper-v1",
  "paper_account": "public-usd",
  "idempotency_key": "operator-task-20261021-001"
}
```

```bash
node scripts/tiger-agent.mjs run \
  --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state \
  --mode paper --input-file request.json
```

成功提交返回 `run_id` 和 `status=accepted` 或 `running`。这只表示请求身份被接纳，不能报告分析或账户处理完成。将实际 `run_id` 写入查询文件：

```json
{"run_id":"run-0123456789abcdef0123456789abcdef"}
```

```bash
node scripts/tiger-agent.mjs status --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
node scripts/tiger-agent.mjs receipt --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
node scripts/tiger-agent.mjs verify --python /absolute/path/to/python \
  --state-root /absolute/path/to/tiger-state --input-file run-id.json
```

查询可以保持 readonly，但必须使用提交时同一个 state-root。`receipt` 独立验证证据和账户提交后才提供选股、持仓、挂单、现金、权益、日期及数据/模型限制；`verify` 提供验证结果而不重复业务动作。CLI 每次仅输出一个 JSON envelope；所有返回字段的契约在 `bundles/trading/agent-workflow.schema.json`。

需要持续等待时，原 `run` 命令可加 `--wait`，可选 `--wait-timeout-seconds 1..3600`。默认观察窗口 2460 秒；Python 业务 deadline 独立为 2400 秒。Node 单次提交/查询上限 60 秒，等待循环只轮询已有 `run_id`，不会重新提交。

| 场景 | 正确行为 |
|---|---|
| 已拿到 run_id 后关闭终端/MCP、超时或取消 | 停止观察；accepted worker 继续独立执行。使用原 run_id 查询 |
| 提交响应丢失，未拿到 run_id | 用完全相同的四字段请求及原幂等键恢复身份；不得自动换新键 |
| 同一键已有 completed/completed_degraded | run 返回原身份及 `data.observed_status`，仍需 receipt 验证 |
| 同一键已有 failed/blocked/interrupted | 返回原失败，不重跑内部阶段 |
| 新请求遇到 ACCOUNT_BUSY | 新键尚未被消费；之后可重试同一个请求和键 |
| status 调用成功但 status=failed | 只证明查询成功，不证明业务成功 |
| receipt/verify 发现证据缺失或损坏 | 返回失败，不读取未验证摘要，不补写或重跑阶段 |

退出码：0 表示当前操作成功；2 输入无效；3 访问拒绝；5 幂等冲突/账户占用；6 数据拒绝；7 runner/证据/持久化错误；8 阶段失败或未完整完成；9 超时/中断；130 取消。退出 0、JSON 合法、存在文件或状态为 accepted 均不能单独证明完整业务交付。

## 3. 独立 Tiger MCP

入口 `scripts/tiger-agent-mcp.mjs` 使用相同 Python 契约。STDIO stdout 只承载逐行 JSON-RPC。初始化顺序为 `initialize` → `notifications/initialized` → `tools/list`；初始化、发现和临时探测均不创建 state-root、不启动 Python 或业务 worker。

| 原始工具名 | 行为 | readonly 可见 |
|---|---|---|
| `tiger_doctor` | 检查解释器、依赖和运行条件 | 是 |
| `tiger_workflow_describe` | 返回固定 profile 和阶段契约 | 是 |
| `tiger_workflow_run` | 提交完整公开美股 paper 流程 | 否，直接调用也在启动 Python 前拒绝 |
| `tiger_workflow_status` | 读取既有运行状态 | 是 |
| `tiger_workflow_receipt` | 独立校验证据后读取完成摘要 | 是 |
| `tiger_workflow_verify` | 独立校验证据和账户提交 | 是 |

工具结果同时包含相同 envelope 的 `structuredContent` 和 JSON 文本；`isError` 表示操作失败。未知工具为 JSON-RPC 参数错误。MCP 取消只停止对应提交/查询的观察，不是业务撤销 API。帧最大 128 KiB，业务输入仍为 64 KiB；最多四个并发请求，重复在途 ID、畸形 JSON、过量输出和断开的流均有有界退出路径。

初始化行为和工具结果遵循已核实的 [MCP 生命周期规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 与 [工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。本服务不要求 task-based execution 扩展。

## 4. 生成 Codex 项目配置

生成器只写选中 Tiger 项目内的配置，默认位置 `.codex/config.toml`。它实际运行选定 Python 的 `tomllib` 校验 TOML，保留其他配置原文，只更新一对管理标记内的 Tiger server；遇到已有同名非托管条目、重复标记、非法 TOML、链接路径或并发修改则拒绝替换。

```bash
# 只检查是否已匹配；不会创建配置目录。需更新时退出 2。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python --check

# 默认生成 readonly。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python

# 经操作者选择后，显式启用 paper；再次运行可切回 readonly。
node scripts/tiger-agent-config.mjs --client codex \
  --python /absolute/path/to/python --mode paper
```

可选 `--project /absolute/path/to/workroom-tiger`、`--state-root /absolute/path/to/tiger-state` 和项目相对 `--config`。生成结果包含实际 Node/Python 路径、解释器版本、写入状态；不保存凭据、不编辑用户全局配置。原文件会原子替换，重复生成结果一致。`durability=unknown` 表示已发生替换，但目录同步或当前内容复核未确认；`cleanup=incomplete` 表示文件或写入锁清理未完全确认。两者都不能当成“没有写入”自动重试，应按 warning 核对现有文件和锁。

提交到仓库的是 `config/agent-clients/codex.toml.example` 可移植模板；带有本机绝对路径的实际配置留在本地并被 Git 忽略。

Codex 支持项目 `.codex/config.toml`、STDIO 的 command/args/cwd、启动与工具超时配置；项目配置受客户端信任机制约束，详见已核实的 [Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。生成配置成功不表示当前会话已加载。请在可信项目的新连接或显式刷新后的实际宿主中验证工具列表和 doctor；交付证据应分别记录“文件校验”“协议调用”“原生宿主挂载”。生成器始终返回 `host_attached=false`。

原生 `codex mcp get tiger-workflow --json` 是配置查询入口，单独的 not-found 不能判定项目文件无效。需要区分项目层发现和实际生效时，可通过已支持该接口的原生 app-server `config/read` 指定 `cwd`、检查项目层及 `disabledReason`；只输出 Tiger 字段，避免暴露其他配置。若项目层因尚未信任而禁用，应使用客户端正常的项目信任流程；生成器不会更改这项用户设置。只读配置检查不需要启动 thread/turn 或模型，接口见 [Codex App Server 文档](https://learn.chatgpt.com/docs/app-server)。

根 `AGENTS.md` 显式要求阅读 `AGENTS.repo.md`。后者补充 Tiger 用法，不创建覆盖根规则的 `AGENTS.override.md`；参见 [Codex AGENTS.md 发现规则](https://learn.chatgpt.com/docs/agent-configuration/agents-md)。

## 5. 生成 DeepSeek Harness 插件列表

本仓 `governance/` 内置 DSH 组件属于产品治理壳；外部 DSH 客户端的 MCP 插件属于调用端，两者不共享执行入口。

```bash
node scripts/tiger-agent-config.mjs --client dsh \
  --python /absolute/path/to/python
```

输出项目 `.dsh/tiger-mcp.yaml`，内容是顶层插件列表 overlay，不是完整 DSH 应用配置。将该列表按所用 DSH 部署的配置装配方式加入插件集合；生成器不启动、安装或改写 DSH 全局配置。已安装的 `governance` YAML 依赖用于解析与保留配置；缺失时明确报错，不临时下载包。

每个服务器一条 `@deepseek-ai/dsh-mcp-client`，`serverName=tiger-workflow`、`transport=stdio`，具体字段为 command/args/cwd/toolCallTimeoutMs/failOnStartupError。调用端名称形如 `mcp__tiger-workflow__tiger_doctor`。字段和临时 STDIO probe 行为来自已核实的 [官方固定标签 dsh-v0.2.1-rc.2 MCP 插件文档](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/mcp/mcp-client/README.md)；这不是对任意客户端版本的兼容声明。

已有插件及注释按原字节保留；block list、flow list 和尾逗号均支持。生成器不会执行 `!!js`，其他插件的此类标记原样保留。已有同名 server/plugin 冲突时拒绝写入。默认 readonly；`--mode paper` 显式切换。模板见 `config/agent-clients/dsh.yaml.example`。实际宿主是否装配成功须由该宿主的工具发现和调用回执证明，不能用原始 STDIO 测试冒充。

## 6. 保留原 WorkLoom 只读目录

```bash
node scripts/workloom-agent.mjs list
node scripts/workloom-agent.mjs describe trading.repo.status
printf '%s' '{}' | node scripts/workloom-agent.mjs invoke trading.repo.status --input-file -
```

`trading.repo.status`、`trading.repo.docs`、`trading.bundle.summary`、`trading.repo.readiness` 读取本仓元数据；行业 bundle 来自 `governance/bundles/trading`。仓库 provider 不读 HOME 密钥文件、不联网、不写盘。通用目录其他服务端能力沿用自己的访问机制；这些能力不因此获得 Tiger paper 执行权。原通用 loader 的 local+execute 禁止规则保留。

## 7. 可复核的验证与排错

```bash
# 实际已安装 Python 路径只通过当前进程注入，不写入提交。
TIGER_TEST_PYTHON=/absolute/path/to/python node --test \
  bundles/trading/capabilities/workflow-client.test.mjs \
  bundles/trading/capabilities/repo.test.mjs \
  scripts/tiger-agent.test.mjs scripts/tiger-agent-mcp.test.mjs \
  scripts/tiger-agent-config.test.mjs \
  scripts/agent-capabilities.test.mjs governance/scripts/agent-capabilities.test.mjs
```

这些 Node 测试包含确定性传输夹具，不能证明公开行情调用、39 阶段执行或 paper 账户提交成功。完整验收还需要真实 Python 集成、故障/并发/证据篡改测试，以及通过当前宿主 CLI 或工具取得的实际公开数据 paper 回执。任何 completion 声明须关联源代码身份、run_id、receipt hash、验证结果及限制。

| 错误或现象 | 处理 |
|---|---|
| doctor not_ready / Python 版本不符 | 指定已安装本仓依赖且满足 3.11+ 的实际 Python，再检查 doctor |
| READ_ONLY | 由操作者在部署配置中选择 paper；请求字段不能升级权限 |
| RESULT_INVALID / EVIDENCE_* | 保留原 run_id 和证据，调查格式/哈希/绑定；不得拼接成功摘要 |
| 配置锁存在 | 先确认写入进程是否仍活跃；不要绕过锁覆盖其他配置 |
| MCP 菜单没有 run | readonly 的正常结果；配置改动须在实际宿主重新连接后验证 |
| 公开源失败/访问挑战/数据不足 | 依据回执如实报告缺口；不注入演示行情，不绕过访问控制 |

当前 profile 不接受 skip、自由 argv、手改 score、tune、approve、live、provider 或 root 覆写。后续扩展必须修改已审查的固定 profile、Python 流程和验证契约，不能只在 Node 层放开参数。
