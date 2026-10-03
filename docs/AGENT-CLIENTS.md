# 桌面 Agent 接入指南：Codex / DeepSeek Harness

> 读者：要把本仓能力接进桌面 Agent 的人（或 Agent 自己）。目标是 **5 分钟能跑通**：
> 先 `list` 看到能力 → 再 `invoke` 读一条真实状态 → 最后把 stdio MCP 挂进客户端。
> 本仓 `bundle = trading`，能力前缀 `trading`。

Tiger 研究执行入口见 [TIGER-AGENT-API.md](TIGER-AGENT-API.md)：实际内核六模式、四个员工、逐产物 SHA 回执与受控工作区。下面的公共 `workloom-agent` 入口继续提供只读/预览能力。

---

## 1. 五分钟接入（四条命令，均可直接复制）

前置：Node.js ≥ 24、已 `pnpm install`（只跑只读能力不需要先起服务端）。

```bash
# ① 看能力清单
node scripts/workloom-agent.mjs list

# ② 看单项契约（输入 JSON Schema / 传输方式）
node scripts/workloom-agent.mjs describe trading.repo.status

# ③ 真跑一条只读能力（本仓自述）
printf '%s' '{}' | node scripts/workloom-agent.mjs invoke trading.repo.status --input-file -

# ④ MCP 冒烟（stdio JSON-RPC：initialize + tools/list）
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' \
  | node scripts/workloom-agent-mcp.mjs
```

成功标志：`list` 打印本仓清单；`invoke` 返回单行 JSON（`status=succeeded`）；MCP 冒烟返回 `tools/list`（禁用能力不会出现）。

---

## 2. 本仓能力（以 `list` 输出为准）

公共能力（8 条，随基座下发）：工作区档案、数字员工、任务列表/详情/事件、待审队列、巡检状态、技能目录。

本仓行业能力（只读/预览，**不执行写操作**）：

| 能力 | 类型 | 作用 |
|---|---|---|
| `trading.repo.status` | 只读 | 本仓自述：product.manifest 摘要 + bundles/docs/脚本计数 |
| `trading.repo.docs` | 只读 | `docs/*.md` 索引（文件名 + 一级标题），给 Agent 找文档 |
| `trading.bundle.summary` | 只读 | 默认行业包概览：skills / fences / pipelines / presets 计数 |
| `trading.repo.readiness` | 预览 | 接入就绪度：治理目录的 Node、依赖、运行时资产、可选模型环境存在性、MCP 入口、dsh-gate pin；不读密钥文件 |



公共 loader 的本地模块不允许 execute（硬约束）；治理写动作继续走服务端过程、幂等与围栏/审批。Tiger 自有研究 API 使用独立受控工作区，只执行研究/模拟，不批准调参、不写券商订单；六模式与四员工的实际范围见 [执行合同](TIGER-AGENT-API.md)。

---

## 3. 能力边界（用之前先看这张表）

| 类别 | 会不会动数据/花钱 | 出现在 MCP 菜单 | 例子 |
|---|---|---|---|
| 只读（`read`） | 否 | ✅ `readOnlyHint=true` | `core.threads.get`、`trading.repo.status` |
| 回执（`receipt`） | 否 | ✅ | `core.threads.events` |
| 预览（`preview`） | 否（本地校验/出计划） | ✅ | `trading.repo.readiness` |
| 执行（`execute`） | **是**（服务端过程 + 幂等键 + 围栏/审批） | 仅启用且过审后出现 | 本仓未登记 |

---

## 4. Codex 接入

```toml
[mcp_servers.workloom-workroom-tiger]
command = "node"
args = ["/abs/path/to/workroom-tiger/scripts/workloom-agent-mcp.mjs"]
cwd = "/abs/path/to/workroom-tiger"
startup_timeout_sec = 60

[mcp_servers.workloom-workroom-tiger.env]
WORKLOOM_PRODUCT_ROOT = "/abs/path/to/workroom-tiger"
# 只读能力可不配下面两项；要用服务端过程时按需配置（不要写进仓库/日志）
# WORKLOOM_BASE_URL = "http://127.0.0.1:<8787+portOffset>"   # 非回环必须 HTTPS
# WORKLOOM_ACCESS_TOKEN = "<B 端成员令牌>"
# WORKLOOM_C_TOKEN = "<C 端令牌>"                             # 仅 C 端能力，与 B 端令牌不可互换
```

`[mcp_servers.<name>]`、command/args/cwd/env/startup_timeout_sec 已对照 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。运行研究内核时还应设置工具超时，完整例子见 [Tiger 研究 API](TIGER-AGENT-API.md)。

---

## 5. DeepSeek Harness（DSH）接入

**先分清两个 DSH**（本仓只有"内置组件"，桌面端是外部客户端）：

| | 内置 DSH 组件（本仓自带） | DSH 桌面端（官方客户端） |
|---|---|---|
| 是什么 | `vendor/dsh` + `packages/runtime` 的 dsh seam（意图路由 / 任务循环 / 装配 / 工具 / 剧本）+ 围栏插件 + 事件桥 | DeepSeek 官方桌面应用（v0.2 预览版，macOS/Windows，MIT 开源） |
| 谁在用 | 产品自己（服务端派活、夜班） | 人（资深用户/内部同事的工作台） |
| 怎么接本仓 | **不需要 MCP**，进程内直接走服务端过程 + 围栏 + 账本 | 路线 A：用它的 `bash` 跑本仓 CLI；路线 B：用 MCP 插件挂 `scripts/workloom-agent-mcp.mjs` |
| 验收 | `bash scripts/dsh-gate.sh`（headless → 过围栏 → 事件哈希链 → kill -9 重放） | 客户端侧 `tools/list` 能看到本仓工具 |

DSH 官方扩展手册原话：**"MCP：每个服务器一个插件：发现工具 → `ctx.tools.register()`"**。桌面端插件页按 npm 包名安装，
把服务器命令配成 `node /abs/path/to/workroom-tiger/scripts/workloom-agent-mcp.mjs` 并注入 `WORKLOOM_PRODUCT_ROOT` 即可。

---

## 6. 安全与排错

- 令牌三类：`WORKLOOM_ACCESS_TOKEN`（B 端）、`WORKLOOM_C_TOKEN`（C 端，单独）、本地演示身份（`auth.loginAs`）；
  **令牌只走环境变量**，不得进仓库/参数/日志/提示词；
- 地址：默认 `127.0.0.1:8787+portOffset`（本仓 `product.manifest.json`）；**非回环必须 HTTPS**；
- 禁用能力：`enabled=false` 不进 MCP `tools/list`，调用返回 `DISABLED` + 原因；
- 错误：清单/契约类错误返回 `{"error":{"code":...}}`；本地能力抛错显示 `INTERNAL_ERROR` + 可读原因；
- 路径监狱：本地能力只读仓内相对路径，绝对路径与 `../` 逃逸一律拒绝；
- 本地只读能力不联网、不写盘、不上行客户数据。

---

## 7. 本仓本次落地（T-2026-1001-0011）

- 入口：`scripts/AGENT-CAPABILITIES.md`、`scripts/agent-capabilities.{mjs,core.json,test.mjs}`、
  `scripts/workloom-agent.mjs`、`scripts/workloom-agent-mcp.mjs`（与基座 main 一致，测试为清单驱动版本）；
- 行业能力模块：`bundles/trading/capabilities/repo.mjs`（只读/预览）；
- 能力清单：`bundles/trading/agent-capabilities.json`（本仓行业能力，见 §2）；
- 自检：`node --test scripts/agent-capabilities.test.mjs`。
