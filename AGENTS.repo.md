# Tiger 仓库专属 Agent 规则

本文件由根 `AGENTS.md` 显式要求阅读，补充其受控规则；不替代根指引和 `WORKLOOM_PRODUCT_CONTEXT.md`。根目录为 Python 交易实验引擎，`governance/` 为 WorkLoom 治理壳，行业资产在 `governance/bundles/trading/`。

## 受控执行入口

- Agent 运行完整业务只使用 `scripts/tiger-agent.mjs` 或独立 `scripts/tiger-agent-mcp.mjs`；它们共同调用 `trading_system.agent_runner`。操作限 doctor/describe/run/status/receipt/verify。
- 先 doctor 和 describe。默认 readonly，paper 权限只能由操作者在可信启动配置中显式选定；工具请求不能修改权限、Python、state-root、策略或阶段。
- 新的 AI 研究与官网更新默认必须显式选择 `us-public-model-required-paper-v1`、`public-usd` 本地 paper 账户和公开美股数据。CLI 的兼容函数默认 `us-public-paper-v1` 只保留旧调用/历史语义，不作为新任务完成；`us-public-active-paper-v1` 是另行显式选择的实验配置，不升级为操作默认。各受控配置均执行信号时冻结预算、次日开盘组合限额及 A/B/C 入场模板门禁。不得连接真实券商、真实资金或复用旧账户；不得注入演示数据掩盖公开源失败。
- 不开放自由 CLI argv、skip、tune、score、approve、live 或单阶段执行。不得把原通用能力 loader 的 local+execute 限制改成例外，不得绕过 Python 的顺序、锁、幂等和证据校验。
- Node 只负责边界校验、进程/协议传输和等待；Python 是阶段、账户、持久幂等、worker 生命周期和独立证据验证的唯一执行方。外层宿主模型不等于内层模型；内层禁用/合法 passthrough 必须如实披露。

## 新研究必须真实调用内层模型

- 对每个新的 AI 研究和官网更新任务，默认必须明确选择 `us-public-model-required-paper-v1`，使用独立 state-root 和新逻辑任务幂等键；无需等操作者再次强调“真实模型”。该配置基于 `reviewed_source_defaults`，不启用 `us-public-active-paper-v1` 板块代理或主线溢出实验，不降低数据、MRS/SHS/TSS 或风险门槛。原两个 profile 只保留其明确语义和历史兼容，disabled/passthrough 结果不能充当新研究完成。
- 固定 `LLM_BACKEND=api`、`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-flash`，仅从可信启动环境读取非空 `LLM_API_KEY`。根 `.env.example` 只是模板，不会自动加载。真实 key 放在仓库外受保护文件，由操作者的可信启动器 export 后执行 CLI/MCP；不得写进请求 JSON、源码、报告、命令参数、日志或提交。
- `deepseek-flash` 是官方 API 别名；2026-10-26 观察的官方映射为 `DeepSeek-V4.1-Flash-1017`，不是不可变快照 ID。不得造出带日期的 API ID，亦不得替换成 Pro、chat、reasoner 或本地模型。官方依据：[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)。
- 搜索使用同提供方原生 `POST /v1/tools/search`，将全部 14 个既有主题得到的净化真实资料供给内层 Flash。适配器支持原生 Fetch，但固定 collector 不调用 Fetch。Flash 的 Responses `web_search` 按官方矩阵会被忽略，不得将该参数当成搜索成功证据。依据：[Search & Fetch](https://api-docs.deepseek.com/guides/search/)、[Responses API](https://api-docs.deepseek.com/guides/responses_api/)。
- `clean.llm_semantic`、`tech.sentiment`、`tech.risk`、`sector.narrative`、`decision.debate` 五阶段必须各有真实、完整、schema 通过的 Flash 回复；模型最后决定选择或否决，程序仅绑定/校验既有数值与风险上限。无候选时也必须真实完成 WAIT/AVOID 判断。任何 disabled、passthrough、空答案、替代模型、规则语义回退、无效或缺失调用证据均是 `MODEL_REQUIRED_FAILED`，不得发布账户或网站新研究成功。
- 只在独立 receipt/verify 成功且状态为 `completed` 时报告本配置完成；不得接受 `completed_degraded`。回执包含精确 response IDs、模型/发布映射、五阶段计数、原生搜索计数、可读理由和人工执行条件。不可只看保存的成功 flag 或“发过 API 请求”。
- 公开报告给人读，说明关注谁、现在为什么、入场条件、失效条件、风险与来源；数值股数/价格/止损/风险必须来自已批准的 TradePick，不让模型编数。纸面资金、信号及模拟成交均不等于富途/老虎真实执行或收益；真实交易仍由用户手动操作并以真实成交证据回报。

## 完成与恢复

- accepted/running 不是完成；status 查询成功也可能显示业务 failed。只在 receipt 独立校验成功、summary 非空时报告业务结果，并引用原 run_id 和 receipt hash。原两个 profile 接受 completed/completed_degraded；model-required 只接受 completed。
- 丢失提交响应时，用原四字段请求和原幂等键恢复身份。不得自动生成新键来重跑失败任务或隐藏部分效果。已知 run_id 后只查该身份。
- CLI/MCP 取消、断连和等待超时停止观察；已经 accepted 的独立 worker 继续执行。不得把“客户端已退出”描述成“业务没有启动/已经撤销”。
- receipt/verify 只读取和验证证据，不恢复、补跑、改写内部阶段。证据缺失、哈希不符、来源或账户提交不一致必须失败，不能读取未验证摘要充当成功。

## 配置与验证

- 使用 `scripts/tiger-agent-config.mjs` 生成项目级配置；默认 readonly。保留其他 server/plugin 的原文，不修改用户全局配置，不在提交中保存本机绝对路径或凭据。
- `AGENTS.repo.md` 的发现依赖根指引显式要求；不要创建 `AGENTS.override.md` 来遮蔽根规则。
- 区分配置文件校验、原始 MCP 协议测试和实际 Codex/DSH 宿主挂载。没有宿主回执就不能声称本轮会话已加载，更不能承诺自动热更新。
- Node 夹具测试只能证明传输契约。完整业务验收必须另有真实 Python 流程、公开数据 paper 执行及独立 receipt/verify 证据。
- 操作命令、配置模板、错误码和测试入口详见 `docs/AGENT-CLIENTS.md`；接口字段以 `bundles/trading/agent-workflow.schema.json` 与 Python runner 的实际行为共同校验。
