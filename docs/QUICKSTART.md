# 老虎交易 · 快速上手（QUICKSTART）

> 两条使用路径：**A. 交易内核单用**（Python，开箱即用）｜**B. 全栈**（内核 + WorkLoom 治理底座，适合团队/机构）。

## A. 交易内核单用（5 分钟）

```bash
git clone https://github.com/geniusdapeng-collab/workroom-tiger.git
cd workroom-tiger
bash scripts/setup.sh        # 装依赖 + 冒烟测试 + 环境自检
```

三条命令验证：

```bash
python3 main.py --demo                                    # 离线演示（合成数据，确定性）
bash scripts/doctor.sh                                    # 你的网络下哪些数据源可用
python3 main.py --mode daily --universe extended \
       --top 30 --picks 8 --html                          # 真实数据全链路（约 30 分钟）
```

产物在 `reports/`（或 `--out` 指定目录）：双页签日报 HTML、journal 台账、小虎模拟盘台账、复盘纪要、治理事件流。

### 数据源说明（无需任何付费 API）

| 数据 | 来源（降级链顺序） |
|---|---|
| 美股行情 | yahoo → stooq → tencent → sina → eastmoney → agentgw → ifind → tiingo |
| 利率/波动率 | yahoo → stooq → **FRED（美联储官方）/ CBOE（官方）** → ... |
| 新闻情报 | agent-gw 全网 / Google News / Reddit / **东财资讯 / 新浪快讯** / SEC EDGAR / PatentsView / Federal Register |
| A股/港股 | tencent / sina / eastmoney（内置前复权） |

系统对你的网络环境自适应：`doctor.sh` 会给出源可达性画像；不可达源自动降级/熔断，**拿不到真数据的环节诚实透传，绝不编造**。

### LLM 配置（双模驱动，点亮全部能力）

无 LLM 也能跑（语义环节透传），但板块叙事、多空辩论、舆情/风险推断会缺失。支持两种方式：

**方式 a：自有 API（任何 OpenAI 兼容端点）**

```bash
export LLM_BACKEND=api
export LLM_BASE_URL=https://api.deepseek.com/v1   # DeepSeek/Kimi/Qwen/GLM/自部署 vLLM 均可
export LLM_API_KEY=sk-...
export LLM_MODEL=deepseek-chat
# 或 Kimi 原通道：export KIMI_API_KEY=sk-...（或 ~/.kimi/agent-gw.json）
```

**方式 b：本地 AI Coding Agent 主力模型（开发者本地零配置）**

开发者在本地用 AI Coding Agent 运行时，系统自动探测本地模型端点：

```bash
export LLM_BACKEND=local      # 或 auto（默认，自动探测）
# 探测顺序：LLM_LOCAL_URL（显式指定）→ Ollama localhost:11434 → LM Studio localhost:1234 → OPENAI_BASE_URL
# 例：显式指定：export LLM_LOCAL_URL=http://localhost:11434/v1 LLM_LOCAL_MODEL=qwen3:32b
```

默认 `LLM_BACKEND=auto`：kimi → api → local 依次探测；全部不可用时按红线透传（报告如实标注），**系统绝不用规则伪造语义分**。

### 每日自动运行（cron）

```bash
bash scripts/install_cron.sh        # 周一至周五 06:00（北京时间）跑美股日报
# 官网发布环：daily 运行后执行
bash scripts/publish_site.sh
```

### 三市运行

```bash
python3 main.py --market us --mode daily --html     # 美股（默认）
python3 main.py --market cn --mode daily --html     # A股（轻仓验证期 0/50）
python3 main.py --market hk --mode daily --html     # 港股（轻仓验证期 0/50）
```

## B. 全栈（内核 + WorkLoom 治理底座）✅ 已落地

一条命令起全栈（需要 Node ≥24、pnpm 10、Docker）：

```bash
bash scripts/stack_setup.sh
```

脚本完成：依赖安装 → PostgreSQL 17 容器 → 迁移 → 演示种子 + **trading bundle 种子**（33 数字员工 presets、18 条三层围栏、6 个官方技能、7 个三市/夜班/WFA 触发器）→ 内核事件入库 → 启动指引。

落地核验（2026-08-30 实测）：

| 项 | 结果 |
|---|---|
| 底座测试套件 | 全绿（Node 24 + pnpm 10） |
| 内核事件入库 | `governance/scripts/ingest-tiger-events.ts`：30 条五元事件幂等入库（重复执行全量去重） |
| 哈希链 | `verify-chain.ts`：130 条事件逐条重算一致（双链：内核 JSONL 链 + 底座 DB 链，事件内 `decision.kernel_hash` 互验） |
| trading bundle | 33 agents / 18 fence_rules / 6 skills / 7 triggers 落库可查 |
| 服务 | server :8787（/health ✓）+ web :5173 |

日常衔接：内核每次运行后执行 `cd governance && pnpm tsx --env-file=.env scripts/ingest-tiger-events.ts` 增量入库（幂等，可入 cron）。

> 诚实边界：内核 → 底座的事件流已双向可验；底座的审批卡片/夜班自治执行**编排**（Quest 驱动内核 pipeline）是下一阶段，当前三市日程由 cron 触发器登记 + `scripts/install_cron.sh` 驱动。

## 合规声明

本系统仅运行于模拟盘，不构成投资建议，不承诺收益。实盘交易需另行接入持牌券商并满足各市场监管要求。
