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

### LLM 配置（点亮全部能力）

无 LLM 也能跑（语义环节透传），但板块叙事、多空辩论、舆情/风险推断会缺失。配置任一：

```bash
export KIMI_API_KEY=sk-...          # Kimi（默认网关 kimi-k2.5）
# 或放置 ~/.kimi/agent-gw.json      # agent-gw SDK 凭据
```

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

## B. 全栈（内核 + WorkLoom 治理底座）

> 现状诚实说明：治理底座（governance/，TypeScript）代码随仓库分发，内核已把每个动作写成五元事件流（`reports/governance_events.jsonl`，SHA-256 哈希链），`site/governance.html` 可直接读取展示。**TS 底座的完整接线（事件入库、审批卡片、夜班编排）是下一阶段工作**，当前请勿在生产依赖它。

全栈路径（预览）：需要 Node ≥24、pnpm 10、PostgreSQL 17（docker-compose 已带）：

```bash
cd governance
docker compose up -d          # PostgreSQL 17 + pgvector
pnpm install && pnpm db:migrate && pnpm db:seed
pnpm dev                      # server :8787 + web :5173
```

行业角色包在 `governance/bundles/trading/`（33 个数字员工 presets、三层围栏包、6 个 skills）。

## 合规声明

本系统仅运行于模拟盘，不构成投资建议，不承诺收益。实盘交易需另行接入持牌券商并满足各市场监管要求。
