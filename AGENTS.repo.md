# Tiger 仓库专属 Agent 规则

本文件由根 `AGENTS.md` 显式要求阅读，补充其受控规则；不替代根指引和 `WORKLOOM_PRODUCT_CONTEXT.md`。根目录为 Python 交易实验引擎，`governance/` 为 WorkLoom 治理壳，行业资产在 `governance/bundles/trading/`。

## 受控执行入口

- Agent 运行完整业务只使用 `scripts/tiger-agent.mjs` 或独立 `scripts/tiger-agent-mcp.mjs`；它们共同调用 `trading_system.agent_runner`。操作限 doctor/describe/run/status/receipt/verify。
- 先 doctor 和 describe。默认 readonly，paper 权限只能由操作者在可信启动配置中显式选定；工具请求不能修改权限、Python、state-root、策略或阶段。
- 默认固定 `us-public-paper-v1`、`public-usd` 本地 paper 账户和公开美股数据；`us-public-active-paper-v1` 是必须显式选择的板块证据实验配置，不因可运行而自动升级为默认。两个配置均执行信号时冻结预算、次日开盘组合限额及 A/B/C 入场模板门禁。不得连接真实券商、真实资金或复用旧账户；不得注入演示数据掩盖公开源失败。
- 不开放自由 CLI argv、skip、tune、score、approve、live 或单阶段执行。不得把原通用能力 loader 的 local+execute 限制改成例外，不得绕过 Python 的顺序、锁、幂等和证据校验。
- Node 只负责边界校验、进程/协议传输和等待；Python 是阶段、账户、持久幂等、worker 生命周期和独立证据验证的唯一执行方。外层宿主模型不等于内层模型；内层禁用/合法 passthrough 必须如实披露。

## 完成与恢复

- accepted/running 不是完成；status 查询成功也可能显示业务 failed。只在 receipt 独立校验成功、summary 非空且 status 为 completed/completed_degraded 时报告业务结果，并引用原 run_id 和 receipt hash。
- 丢失提交响应时，用原四字段请求和原幂等键恢复身份。不得自动生成新键来重跑失败任务或隐藏部分效果。已知 run_id 后只查该身份。
- CLI/MCP 取消、断连和等待超时停止观察；已经 accepted 的独立 worker 继续执行。不得把“客户端已退出”描述成“业务没有启动/已经撤销”。
- receipt/verify 只读取和验证证据，不恢复、补跑、改写内部阶段。证据缺失、哈希不符、来源或账户提交不一致必须失败，不能读取未验证摘要充当成功。

## 配置与验证

- 使用 `scripts/tiger-agent-config.mjs` 生成项目级配置；默认 readonly。保留其他 server/plugin 的原文，不修改用户全局配置，不在提交中保存本机绝对路径或凭据。
- `AGENTS.repo.md` 的发现依赖根指引显式要求；不要创建 `AGENTS.override.md` 来遮蔽根规则。
- 区分配置文件校验、原始 MCP 协议测试和实际 Codex/DSH 宿主挂载。没有宿主回执就不能声称本轮会话已加载，更不能承诺自动热更新。
- Node 夹具测试只能证明传输契约。完整业务验收必须另有真实 Python 流程、公开数据 paper 执行及独立 receipt/verify 证据。
- 操作命令、配置模板、错误码和测试入口详见 `docs/AGENT-CLIENTS.md`；接口字段以 `bundles/trading/agent-workflow.schema.json` 与 Python runner 的实际行为共同校验。
