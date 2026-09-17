# Tiger governance 目录指引

> 适用范围：本目录及其子目录。本文件只补充 Tiger 治理壳规则，不替代或放宽仓库根级规则。

## 必读上下文

在 governance/ 内进行任何实质性工作前，仍必须先完整阅读仓库根目录的 AGENTS.md 与 WORKLOOM_PRODUCT_CONTEXT.md；两者的产品边界、安全不变量、Git 纪律和验证要求在本目录继续有效。若根级文件缺失、损坏或无法读取，停止实质性修改并报告，不得只依赖本文件。

## 职责边界与单一事实源

- governance/ 是 WorkLoom 的治理壳：负责身份与租户上下文、围栏、人工审批、只增事件、审计回执、评测和治理界面。
- 根目录 Python trading_system/ 负责市场数据、策略研究、回测、组合与风险计算；governance/ 不计算 alpha，也不成为第二套交易策略实现。
- trading_system/config.py 是策略参数的唯一事实源。治理围栏或展示需要同一阈值时，应由受控生成或显式适配链路派生，并保留来源与版本；禁止在 TypeScript、YAML 或 UI 中另行手工维护一套可漂移的参数。
- 修改 Python 与治理壳之间的 proposal、event、receipt 或 fence 契约时，必须同时核对对象身份、幂等键、单位/币种、时间戳、参数版本和失败语义。

## 模拟与纸面交易红线

- Tiger 当前只允许研究、回测、模拟和纸面交易，不得连接真实资金、提交真实券商订单或把模拟结果表述为实盘成交。
- API、事件、回执和 UI 必须明确携带并展示 simulation/paper 环境；mock 或测试回执不得伪装成外部平台成功。
- 任何引入真实券商执行、真实账户凭证或实盘资金的工作，必须另行取得用户明确授权，并先完成独立合规审查、威胁建模、隔离账户、额度控制、kill switch、对账与回滚设计。普通功能请求不构成该授权。

## 高风险审批与证据

- 下单建议、仓位变化、风险额度调整、批量或外部发布、凭证使用及其他可能影响资金或公开信息的动作，默认进入高风险审批；未知或规则异常时 fail closed。
- 审批必须绑定租户、操作者、proposal 哈希、完整参数、策略/配置版本、模拟环境和有效期；内容变化后原审批失效。
- 批准、修改、驳回和过期都写入只增事件。驳回保留原因，修改生成新的可审计 proposal，不覆盖旧证据。
- 每个执行步骤及子调用都必须重新经过权限、PII、围栏与审批检查。没有真实且可核验的目标环境回执，不得标记完成。
- 密钥、账户凭证、客户数据和持仓隐私不得进入代码、fixture、文档、日志、事件样例或提交。

## 本目录验证

从 governance/ 目录运行与改动相匹配的验证：

- 基础静态与单测：pnpm typecheck；pnpm test
- 治理端标准套件：pnpm suite
- 事件、账本或回执变更：额外运行 pnpm db:verify-chain
- 能力面或导览变更：pnpm capabilities，再运行 pnpm capabilities:check
- UI 变更：pnpm preview:all，并实际核对模拟标识、审批状态和回执展示
- 发布候选：pnpm release:gate；未通过不得发布

若变更触及 Python ↔ governance 契约、策略参数派生或交易围栏，还要回到仓库根目录运行：

- python3 scripts/gen_fences.py 后检查生成 diff，确认没有产生第二事实源
- python3 -m pytest tests/test_gen_fences.py tests/test_governance_bridge.py -q
- 影响面较大或准备交付时运行 python3 -m pytest tests/ -q

先运行最接近改动面的检查，再按风险扩大范围。由于该仓可能采用局部检出，缺文件时不得把整仓删除纳入暂存，也不得用恢复整个工作树的方式掩盖环境问题。
