# Tiger 研究执行 API：CLI 与 stdio MCP

本接口让桌面 Agent 调用实际 Python 交易内核，产出可校验的研究报告、模拟账本和本地事件。它面向研究员、风控人员和接入 Tiger 的 Agent。每次调用都明确记录数据来源、实际执行步骤、降级原因和文件 SHA256。

行业入口为 `scripts/tiger-agent.mjs`、`scripts/tiger-agent-mcp.mjs`，两者调用同一个 `trading_system/agent_api.py`。公共 `workloom-agent` 入口继续提供治理读取与行业目录概览。治理岗位 preset 是岗位声明，其数量以实际 bundle 目录为准；此执行 API 当前接通四个真实 Python 员工实现。

## 可调用的行为

| 调用 | 实际行为 | 主要产物与边界 |
|---|---|---|
| pipeline `daily` | 原 `run_pipeline` 的 21 个注册步骤，再持久化信号日记、模拟盘、校准、`ReviewChief.daily` 与本地治理链 | 结构化结果、原生 Markdown/HTML 日报、复盘、账本、调用轨迹；当天收盘信号不能在当天更早的开盘成交 |
| pipeline `premarket` | 日频完整执行，加原 `premarket_plan` | 增加盘前计划 Markdown |
| pipeline `intraday` | 从已验证日频作业还原 watch，调用原 `monitor_loop` | JSON/Markdown 警报、报价健康与来源；全缺报价失败，部分覆盖降级，健康零警报可成功 |
| pipeline `backtest` | 原历史 `collect_day_frames` 和 `run_backtest` | JSON/Markdown/HTML 统计、交易记录；当前只支持 US，缓存关闭，披露历史期权和股票池偏差 |
| pipeline `tune` | 原滚动 `run_wfa` | WFA 报告及 `pending_research_review` 研究提案；不批准、不写活跃参数 |
| pipeline `review` | 将已验证日频信号日记复制到本作业，再调用 `ReviewChief.daily` 或 `weekly` | 复盘产物；源账本前后重新验 SHA，不执行审批 |
| employee `scanner` | 准备当前有效行情，调用 `UniverseScannerAgent.execute` | 实际扫描结果、数据覆盖与来源 |
| employee `mrs` | 准备基准/宏观/股票数据，调用 `MRSAgent.execute` | 实际市场状态；缺维度显式降级 |
| employee `risk` | 完整日频管线准备依赖，再调用 `RiskManagerAgent.execute` | 风控输出，轨迹明确列出前置管线与复盘 |
| employee `review` | 同只读复盘流程 | 必须提供验证过的 `sourceJob` |

所有调用只接受 `environment=simulation` 或 `paper`。这两个标签都不会发券商订单，不代表真实资金成交。每个新作业的账本和模拟盘独立，不能把多次单日调用当成连续投资业绩。

## 启动路径由调用方固定

安装包的启动器应传入自己的 Node、Python 和内核绝对路径。源码开发环境也可以显式指定已装依赖的 Python。以下 `/abs/...` 都是需要替换的路径；执行工作区须为新空目录或同身份已初始化的 Tiger 专有工作区。

```bash
node /abs/tiger/scripts/tiger-agent.mjs \
  --kernel /abs/tiger --python /abs/python/bin/python3 \
  --workspace /abs/tiger-research catalog

node /abs/tiger/scripts/tiger-agent.mjs \
  --kernel /abs/tiger --python /abs/python/bin/python3 \
  --workspace /abs/tiger-research run --json \
  '{"operation":"pipeline","mode":"daily","environment":"simulation","provider":"demo","idempotencyKey":"daily_demo_001","topN":5,"maxPicks":3,"llmMode":"disabled"}'
```

也可使用 `run --input-file request.json` 或 `run --input-file -` 从 UTF-8 文件/stdin 读取请求。输入上限 64 KiB。stdout 只有一行 JSON；默认关闭模型的日频调用会诚实返回 `degraded`，CLI 退出码为 **10**，不能据此声称真实模型全链成功。`demo` 数据始终标为 `synthetic`；配置真实模型也不会将合成行情改标为真实行情。

启动选项为 `--workspace`、`--tenant`、`--kernel`、`--python`，均不属于工具请求输入。`TIGER_KERNEL_ROOT`、`TIGER_PYTHON_EXE`、`TIGER_TENANT_ID` 可由可信启动器提供；`TIGER_SOURCE_COMMIT` 可提供实际构建提交。API 不读取调用者传入的任意命令、模块名或输出路径。

## 请求与幂等

CLI 请求必须包含 `operation`、`environment` 和 `idempotencyKey`。MCP 根据工具名选择 operation，不允许再在工具参数中传 `operation`。`idempotencyKey` 为 8–80 个 ASCII 字符，首字母/数字，其余限字母、数字、下划线、连字符。

| 参数 | 支持值/范围 | 默认 |
|---|---|---|
| `provider` | demo / yahoo / stooq / tencent / sina / eastmoney | demo |
| `market` | us / cn / hk；backtest/tune 限 us | us |
| `universe` | core / extended | core |
| `topN` / `maxPicks` | 整数 1–100 / 1–25 | 20 / 5 |
| `account` | 有限数值 100–100000000；按所选市场账户币种解释 | 100000 |
| `timeoutSeconds` | 整数 1–900 | 300 |
| `llmMode` | disabled / configured | disabled |
| `riskLimits` | 完整对象 `{risk_r_pct,max_single_position_pct,gross_cap}`；每项有限正值，分别不大于 0.008 / 0.20 / 0.90 | 内核三项上限 |
| backtest/tune `btDays` | 整数 5–490 | 260 / 380 |
| tune `trainDays` / `testDays` / `stepDays` | 整数 20–252 / 5–126 / 5–126；btDays 至少覆盖训练加测试 | 126 / 63 / 63 |
| intraday `cycles` / `intervalSeconds` | 整数 1–100 / 0–3600 | 1 / 0 |
| review `reviewFrequency` | daily / weekly | daily |

多余字段、错误模式的字段、null、非有限数字、实盘、审批、参数生效、租户或路径字段均拒绝。同 tenant/workspace 下，同键同规范化输入重放同一终态回执，并重新校验产物；同键异参返回 `IDEMPOTENCY_CONFLICT`，并发同键只执行一份。失败、超时和取消也是终态，重试执行须换新键。崩溃作业返回 `ORPHANED_JOB`，保留其现场，不伪造完成状态。

客户档案可传 `"riskLimits":{"risk_r_pct":0.004,"max_single_position_pct":0.10,"gross_cap":0.50}`。三项约束进入实际 MRS、风控、整数股数、模拟成交、信号结算、回测与 WFA 参数；与市场档位、原计划和既有更紧参数取更紧者。输入不能放宽内核上限，也不能在旧计划回放时扩大原始授权。scanner 没有仓位输出，盘中和复盘读取已绑定的源作业；如果该源作业的风险快照超过本次声明，返回 `RISK_LIMIT_MISMATCH`。历史研究可能需要数分钟采集逐日帧；`timeoutSeconds` 是调用方的硬预算，预算不足返回持久化的 `timed_out`，不会改成成功。

## 使用日频源作业

从 daily/premarket 回执的 `artifacts` 找到 `role=pipeline-result` 条目，复制该条目的 SHA，而不是对文件自行改内容后继续调用：

```json
{
  "operation": "pipeline",
  "mode": "intraday",
  "environment": "simulation",
  "provider": "demo",
  "idempotencyKey": "intraday_demo_001",
  "sourceJob": {
    "jobId": "daily_demo_001",
    "resultSha256": "替换为源回执里的64位小写SHA256"
  },
  "cycles": 1,
  "intervalSeconds": 0
}
```

源作业须在同一个 tenant/workspace，且 provider、market、environment 相同。sourceJob 只支持已完成的 daily/premarket；API 对源回执和每个产物在使用前后重新验证。盘中调用还验证源交易日期；过旧或未来日频快照返回 `STALE_SOURCE_JOB`。

盘中 `quoteCoverage` 按实际报价观察记录 requested、ready、missing、stale、invalid、metadataUnverified、errors。价格必须为有限正值，报价类型和时间必须可验证。realtime 最大 15 分钟、realtime_delayed 最大 60 分钟；允许最多 5 分钟未来时钟偏差；日线收盘价须不早于所选市场最近交易日。外部无时区的盘中时间戳无法证明时效，会记为 metadataUnverified 并弃用；Demo 自带的本机时间只在明确的合成数据模式中解释。健康报价未触发价格条件与根本拿不到报价有不同终态。

## 回执、读取与取消

```bash
node /abs/tiger/scripts/tiger-agent.mjs \
  --kernel /abs/tiger --python /abs/python/bin/python3 \
  --workspace /abs/tiger-research get daily_demo_001

node /abs/tiger/scripts/tiger-agent.mjs \
  --kernel /abs/tiger --python /abs/python/bin/python3 \
  --workspace /abs/tiger-research artifacts daily_demo_001 execution_trace.json
```

回执包含 `schemaVersion=tiger.agent-receipt/v1`、jobId、规范化输入 SHA、开始/完成时间、sourceCommit、kernelDigest、configDigest、stepTrace、degradedSteps 和产物 `{name,role,bytes,sha256,mediaType}`。Python 验证输入/完成回执/文件；Node 启动器再次独立读取完成校验、文件 SHA 与本地治理链，成功后才添加 `launcherIntegrityVerified=true`。

成功和降级回执还包含 `requestedRiskLimits`（本次声明）、`riskLimits`（内核实际三值）、`gateParams`（完整不可变参数快照）、`resultSha256` 和 `resultArtifact={name,role}`。日频/盘前的 canonical role 是 `pipeline-result`；scanner/MRS/risk 是 `employee-result`；backtest/WFA 是 `research-result`；盘中/复盘是 `source-result`。`resultSha256` 是该结果文件的真实字节哈希。日频结果里的实际快照为 `raw.risk_limits`、`raw.gate_params`；其余 canonical 结果为顶层 `riskLimits`、`gateParams`。两层校验都要求实际三值为正、有限且不超过声明，并且与完整参数及结果文件一致；更紧的实际三值可以小于声明。`configDigest` 绑定内核配置摘要、声明上限和完整参数快照。风险快照缺失或不一致时不能取得成功回执。

`receipt.synced=true` 的 `scope` 固定为 **local-kernel**：它证明本地文件与本地事件链校验通过。`governanceSynced=false` 单独披露，不能据此声称外部治理数据库已入库。原始治理链损坏、缺必需步骤事件、报告/账本缺失或磁盘失败均不能获得成功回执。

| 状态 | CLI 退出码 | 含义 |
|---|---|---|
| succeeded | 0 | 本次实际调用及其必需产物完成并校验 |
| degraded | 10 | 必需执行/产物完成，但模型透传或数据覆盖缺失已明确披露 |
| failed / timed_out / cancelled | 1 | 不具备成功完成证据，`integrityVerified=false` |
| running（仅 get） | 0 | 拥有该作业的内核仍在运行，未具备完成证据 |

run 等待最终回执。MCP 每个启动器最多同时执行两个内核作业，可以并行调用 get 查看 running。`tiger.job.cancel` 仅取消当前 MCP 启动器拥有的活跃作业；CLI 的 SIGINT/SIGTERM 也会停止本次内核并持久化取消回执。超时会停止进程树并等待退出；若平台终止过程无法验证，回执明确失败。另一个启动器的 running 作业不能被此取消工具杀掉。

产物读取只允许已经列在验证清单里的名称，inline 内容上限 512 KiB；更大文件从回执的本地 `artifactRoot` 查看。不能借此读取任意客户文件。

## 显式模型设置

默认 `llmMode=disabled` 不构造自动发现客户端、不读宿主密钥文件、不探测本地模型。`configured` 只采用启动器显式提供的一组兼容 API 配置：

| 选择 | 必需环境变量 | 同组可选凭据 |
|---|---|---|
| `LLM_BACKEND=api` | LLM_BASE_URL + LLM_MODEL | LLM_API_KEY |
| `LLM_BACKEND=local` | LLM_LOCAL_URL + LLM_LOCAL_MODEL | LLM_LOCAL_API_KEY |
| api/local/auto 的显式 OPENAI 端点 | OPENAI_BASE_URL + OPENAI_MODEL | OPENAI_API_KEY |

auto 按上表顺序选择完整配置；选定组的凭据不借用另一组的 key。configured 没有完整端点、连接失败或不返回有效模型结果时，实际模型步骤透传并列入 degradedSteps。此 facade 不沿用 `main.py` 的 Kimi SDK/keyfile 自动发现方式。

密钥从获授权的进程环境进入子进程，不放到请求 JSON、CLI argv、报告或回执。上游 stdout/stderr 不对外转发；JSON、Markdown、HTML、治理事件和错误信息在发布前脱敏。启用配置过的外部模型可能产生用量；它的费用与治理审批仍由可信宿主控制，研究回执不构成费用审批。

## Codex 接入示例

以下字段来自 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。使用安装包自己的绝对可执行路径；长期运行工具的客户端超时须比请求 `timeoutSeconds` 留出终止和校验时间。

```toml
[mcp_servers.tiger-research]
command = "/abs/embedded-node"
args = ["/abs/tiger/scripts/tiger-agent-mcp.mjs", "--workspace", "/abs/tiger-research", "--tenant", "local"]
cwd = "/abs/tiger"
startup_timeout_sec = 60
tool_timeout_sec = 930
env_vars = ["LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"]

[mcp_servers.tiger-research.env]
TIGER_KERNEL_ROOT = "/abs/tiger"
TIGER_PYTHON_EXE = "/abs/embedded-python"
```

Windows TOML 路径可用正斜杠的绝对路径。`tools/list` 返回五个工具：`tiger.pipeline.run`、`tiger.employee.run`、`tiger.job.get`、`tiger.job.artifacts`、`tiger.job.cancel`。stdio 的 stdout 全为逐行 JSON-RPC，单帧上限 128 KiB。客户端应完成 initialize → notifications/initialized → tools/list，按照握手协商版本；取消使用 notifications/cancelled。EOF 会等待已经接受的工作返回最终回执。

## 本地安全边界与验收

工作区身份由可信调用方固定；新目录必须为空，已有目录须匹配产品/tenant/workspace marker。拒绝仓库根目录、源台账目录和任意层软链，runtime、cache、临时文件、校准、提案、模拟账本均隔离在各作业目录。read-only 行业目录读取也拒绝普通、断链及父目录软链，不读取用户 keyfile。

回归入口为 `node --test scripts/tiger-agent.test.mjs` 和 `python -m pytest tests/test_agent_api.py`。覆盖六模式、四员工、21 步、完整 SHA、源账本不变、同键并发、非法参数、篡改、磁盘/账本/事件故障、模型失败脱敏、超时、取消、崩溃、stdio 真实握手与调用。开发机执行通过不能替代 Mac/Windows 安装包中嵌入运行时的实跑，不能替代真实模型或外部治理入库验收。
