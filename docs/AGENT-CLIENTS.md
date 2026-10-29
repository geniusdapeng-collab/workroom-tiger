# Tiger 接入 Codex / DeepSeek Harness

本指南面向用 Agent 调用 Tiger 的操作者和维护者。Tiger 有两条独立入口：原有 WorkLoom 能力目录提供仓库只读信息；新增 Tiger CLI/MCP 调用同一个 Python 严格工作流。根目录是 Python 引擎，治理壳在 `governance/`。不要把根目录当成 Node 工作区，也不要修改通用能力加载器来开放本地 execute。

## 1. 先检查，再选择访问模式

从仓库根目录运行。Node 要求由仓库现行运行时约束决定；新 Tiger 入口的 Python 最低要求为 3.11。推荐显式传入已安装本仓依赖的 Python；省略时先找仓库 `.venv/bin/python`，再找 PATH 中的 `python3`。系统 Python 版本不满足时，doctor 会失败，不会假装就绪。

```bash
node scripts/tiger-agent.mjs doctor --python /absolute/path/to/python
printf '%s\n' '{"profile":"us-public-model-required-paper-v2"}' | \
  node scripts/tiger-agent.mjs describe --python /absolute/path/to/python --input-file -
```

这两条命令不调用行情或模型、不创建账户、不启动业务 worker。doctor 检查实际解释器和依赖；describe 返回固定 profile、阶段顺序和限制。`trading.repo.readiness` 只检查文件布局，不能代替 doctor。

默认 `--mode readonly`。执行完整 paper 工作流必须由操作者在启动参数或项目部署配置中显式选择 `--mode paper`。请求 JSON 不能改变 mode、解释器、state-root、阶段或策略。

当前开放六个固定 profile。新 AI 研究和官网更新默认必须显式选择 `us-public-model-required-paper-v2`；盘前和盘中分别选择 `us-public-model-required-premarket-paper-v2`、`us-public-model-required-intraday-paper-v2`。三个 v2 profile 共用 `whitepaper-execution-v2`，研究时段不同但均执行相同完整 43 阶段，使用 `full` 股票池，最多 10 个持仓名额，不把上限当每日配额。三者在同一独立 v2 state-root 内保持连续 paper 账户。

旧 `us-public-model-required-paper-v1` 保留原五阶段模型证据和验证语义；旧 `us-public-paper-v1`、`us-public-active-paper-v1` 保留禁用/透传及实验语义，不能创建新官网研究。CLI 兼容函数默认仍是旧 `us-public-paper-v1`，所以新任务必须明确写 v2。首次账户 seed 为 100000 USD；后续仓位使用真实保存的 paper NAV、现金、持仓和整笔未完成计划预约，不再反复使用固定 100000 参考资金。外层宿主模型不等于内层模型，没有真实券商自动下单。

可选 active 实验的回退板块是冻结的 NASDAQ 供应商分类代理，不是 GICS、真实 ETF 成分或历史时点分类；第三条达标主线保留为次池。更高资金使用率会改变实际风险，不能把“上限不变”解释为“收益或回撤不变”。该实验不会随 model-required 配置启用。运行和评测边界见 [TIGER-ACTIVITY-EVALUATION.md](TIGER-ACTIVITY-EVALUATION.md)。

### 真实 DeepSeek 内层模型与原生搜索

固定配置见根 `.env.example`，实际 key 存在仓库外受保护文件。运行器不自动寻找或加载 `.env`，也不读取 Codex/DSH 的登录缓存。可信启动器先将 `LLM_BACKEND=api`、`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash` 与非空 `LLM_API_KEY` 导出到进程环境，再执行 CLI/MCP。四项仅为 model-required 的业务子进程保留；PnL token、其他模型 key、任意 Python hook 不随之传入。请求 JSON 无权设置这些值。

```bash
# protected-deepseek.env 的实际内容由操作者在仓库外管理。
set -a
. /absolute/protected/path/deepseek.env
set +a
node scripts/tiger-agent.mjs run --python /absolute/path/to/python \
  --mode paper --state-root /absolute/path/to/model-required-state \
  --input-file model-required-request.json --wait
```

`model-required-request.json` 使用下文四字段结构。v2 基于 `reviewed_source_defaults`，不启用 active 板块代理或主线溢出实验。模型只能选择或否决数值风险已批准的买入，不得新增或扩大。没有合格买入时也必须完成真实 WAIT/AVOID 推理，并研究已有持仓。

固定 API ID 为 `deepseek-flash`，2026-10-27 复核的 [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) 映射为 `DeepSeek-V4.1-Flash-1017`。它是可变别名，回执保存实际 response.model、response IDs、release 观察和 usage，不虚构不可变日期 ID。原生 [Search & Fetch](https://api-docs.deepseek.com/guides/search/) 使用同一提供方与 key。14 个广域主题后，对最多 10 个候选及全部最多 10 个持仓的去重并集逐发行人初搜；每个发行人可按初评的真实缺口再补搜一次并 Fetch 一个已观察、归属该目标的 URL。Fetch 可为零，不为凑次数调用；空 Search 结果和未知发布日期如实保留。

六个必须真实完成的语义阶段为 `clean.llm_semantic`、`tech.sentiment`、`tech.risk`、`sector.narrative`、`research.candidates`、`decision.debate`。调用次数随文档批次和实际补证变化，不能把阶段数当成固定回复数。清洗逐篇保留完整摘要、逐字引文、来源归属和时效限定；下游使用完整语义索引，不能声称再次读了原文。发行人研究每批最多 3 个目标、每源最多展示 24000 字符、每批正文总量最多 60000 字符，先按发行人公平分配，本轮 Fetch/补搜优先。来源索引保留每个目标跨实际轮次累计见过的最长前缀字符数、完整性和摘要；没展示的正文不能用于新事实或完整新闻证据。

每次远端调用前须通过统一预算预留。当前新v2任务选择`tiger.budget-policy.v4`：最多160次chat、20000000实际加预留tokens、60次Search、20次Fetch、240次远端请求、保守估算30 USD、7200秒。旧显式v3保留600000 tokens，旧v2保留300000 tokens，其余旧限额为40 chat/120远端/1 USD/2400秒，旧回执不变。发行人子预算另限 40 次 Search、20 次 Fetch、14 次 chat。所有来源逐篇完整清洗，只有已知用量且安全的schema拒绝可完整重新生成一次，v4整轮最多追加56次；正文、来源索引和时效门槛保留，其他错误不追加尝试。全部预留及实际usage必须结算一致；未知付费结果冻结，不能规则代答。美元数是按冻结上限费率计算的估计，不是账单承诺。详见 [官方定价](https://api-docs.deepseek.com/quick_start/pricing/)。

当前v4在清洗之外的五个语义阶段（`tech.sentiment`、`tech.risk`、`sector.narrative`、`research.candidates`、`decision.debate`）统一显式请求 `max_tokens=65536`，保留 high 推理；清洗继续为 32768、disabled/none。该选择来自 `request_budget.semantic_output_tokens`，发行人初评/补证终评共同消费冻结 `plan.limits.max_output_tokens`，独立验证重建同一请求与预留。旧隐式/v2/v3 请求上限保持 8192/12288/16384 的原阶段值，不回写历史。只对上述v4普通 Chat 的高推理请求，响应窗口取 900 秒、预算单调时钟剩余、准入时刻加 7200 秒的绝对剩余三者最小值；无剩余时间时不发起 HTTP。旧政策、清洗、Search/Fetch 保留原窗口，响应正文仍限 1 MiB；未知结果冻结且不自动重试。900秒是源代码约束的网络窗口，不是从既有证明时间戳重新测出的单次耗时；独立证明继续核验整轮/绝对期限。65536 是上限，不承诺使用量、输出完整或任务成功。2026-10-28 核对的 [官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 将 thinking 默认额度列为 64K，仍须以本任务的真实完成状态、usage 和独立回执判断。

每个目标必须有市场、板块、产业链、个股、计划五段依据，并保留入场触发、反证、风险、缺口。`technical_flow_v2` 可以没有公司利好新闻，但仍需具体量价/结构/主线与事件未知边界；`news_event_v2` 的充分证据必须有冻结接纳时刻前 7 日内、归属本发行人、实际完整展示的原始资料。不同域名不自动算独立来源；模型识别原始来源，无法识别时保持未知。空风险预警也不能自动变成风险维度满分。

每个 buy/watch 恰好绑定自己的真实 `market_evidence`：`{id,symbol,as_of,close,series_sha256}`，由冻结完整日线重建；ID 为 `md_<symbol>_<YYYYMMDD>_<摘要前24位>`。`close` 仅是其日期收盘参考，不是获准入场或成交价格。盘前/盘中在纽约 16:00 之前只使用前一完整交易日日线；提前收市仍保守等待 16:00。交易日历不保证覆盖临时特殊休市；官方常规/提前时段见 [NYSE Hours & Calendars](https://www.nyse.com/markets/hours-calendars)。

最终模型的 source_ids 恰好包含自己的行情 ID，至多八篇直接文档引用；发布 registry 可扩展到十六篇，以保留 thesis 的原始/日期/身份引用。watch 的仓位为 0，执行价格、止损、股数、风险、时间止损均为 null，不能变成零价订单。buy 的数值全部绑定原 TradePick，以本轮 paper NAV 为参考；`valid_for` 是下一交易日纽约 16:00。已有机会持续记录 WATCH/QUALIFIED/INTENT_PENDING/HELD/EXIT_PENDING 等状态；触价只请求重新评估，待执行意向必须重新检查新鲜报价及当前完整风险。真实持仓、现金与成交仍来自单独实际账户证据。

模型/原生搜索不可用、401/配额错误、截断、错误模型、空输出、缺少真实调用或文档/阶段输出与调用不一致，均停止整个任务；schema拒绝只允许前述明确、有界的完整重新生成，仍不通过即停止。失败任务不透传，不规则代答，不发布新账户或网站成功。成功只允许 `completed`；`completed_degraded` 不满足此配置。真实业务成功须通过 receipt/verify 独立重算全部阶段 I/O 和最终计划。所有自动测试里的模拟 HTTP 仅证明契约，不能算作真实模型使用。

## 2. CLI：提交一次，持续查询同一个身份

六个操作为 `doctor`、`describe`、`run`、`status`、`receipt`、`verify`。业务输入使用 `--input-file path.json` 或 `--input-file -`；stdin 必须是一个 UTF-8 JSON 对象，最大 64 KiB。重复键、未知字段、非有限数值、任意命令和路径覆写都会被拒绝。

请求文件只含以下四个字段；为一次逻辑任务分配一个持久、可恢复的幂等键：

```json
{
  "schema_version": "tiger.agent-request/v1",
  "profile": "us-public-model-required-paper-v2",
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

如需单独运行已授权的 active 实验，将请求中的 `profile` 明确改为 `us-public-active-paper-v1`，使用新的逻辑幂等键和独立 state-root；兼容旧调用须明确使用 `us-public-paper-v1`，不作为新 AI 研究完成。`describe` 可用 `--input-file` 读取对应 profile 的对象返回其 policy。旧 v1 与新 v2 账户不可混用；首次建立独立 v2 state-root，不复制旧待成交/净值冒充迁移。三个 v2 阶段保留同一账户连续历史。

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

需要持续等待时，原 `run` 命令可加 `--wait`，可选 `--wait-timeout-seconds 1..3600`。默认观察窗口7260秒；Python业务deadline按可信预算版本独立执行（当前v4为7200秒，旧版本2400秒）。显式1..3600秒选项用于较短观察，不修改业务期限。Node 的提交、status 等普通操作单次观察上限为 60 秒；`receipt`/`verify` 要完整重建较大的 v2 证据图，单次上限为 300 秒。等待循环只轮询已有 `run_id`，不会重新提交。

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
  bundles/trading/capabilities/workflow-model-required.test.mjs \
  bundles/trading/capabilities/workflow-short-term-v2.test.mjs \
  bundles/trading/capabilities/workflow-clean-regeneration.test.mjs \
  bundles/trading/capabilities/repo.test.mjs \
  scripts/tiger-agent.test.mjs scripts/tiger-agent-mcp.test.mjs \
  scripts/tiger-agent-config.test.mjs \
  scripts/agent-capabilities.test.mjs governance/scripts/agent-capabilities.test.mjs
```

这些 Node 测试包含确定性传输夹具，不能证明公开行情调用、v2 的 43 阶段执行或 paper 账户提交成功。完整验收还需要真实 Python 集成、故障/并发/证据篡改测试，以及通过当前宿主 CLI 或工具取得的实际公开数据 paper 回执。任何 completion 声明须关联源代码身份、run_id、receipt hash、验证结果及限制。

| 错误或现象 | 处理 |
|---|---|
| doctor not_ready / Python 版本不符 | 指定已安装本仓依赖且满足 3.11+ 的实际 Python，再检查 doctor |
| READ_ONLY | 由操作者在部署配置中选择 paper；请求字段不能升级权限 |
| RESULT_INVALID / EVIDENCE_* | 保留原 run_id 和证据，调查格式/哈希/绑定；不得拼接成功摘要 |
| MODEL_REQUIRED_FAILED | 保留原失败身份和证据。新模型 I/O 错误可附固定安全码、HTTP 状态或 schema 路径/关键字；不保存响应正文、推理内容或任意错误字符串。旧证据没有诊断字段时，不能事后断言原始原因。定位原因并完成修复或服务恢复验证后，为新的尝试分配独立身份；原失败不改，受控入口不内部重跑或自动换键重试 |
| 配置锁存在 | 先确认写入进程是否仍活跃；不要绕过锁覆盖其他配置 |
| MCP 菜单没有 run | readonly 的正常结果；配置改动须在实际宿主重新连接后验证 |
| 公开源失败/访问挑战/数据不足 | 依据回执如实报告缺口；不注入演示行情，不绕过访问控制 |

当前 profile 不接受 skip、自由 argv、手改 score、tune、approve、live、provider 或 root 覆写。后续扩展必须修改已审查的固定 profile、Python 流程和验证契约，不能只在 Node 层放开参数。
