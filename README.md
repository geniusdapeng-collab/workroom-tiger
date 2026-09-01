<div align="center">

# 老虎全球资产管理（Tiger Global Asset Management）

**由 AI 基金经理统筹的全自动化交易系统——覆盖美股 / A股 / 港股，24 小时运行。**

**No prediction. Process, discipline, audit.（别人预测市场，我们执行纪律。）**

[![License](https://img.shields.io/badge/license-Apache--2.0-9A7B2D)](LICENSE)
[![GitHub](https://img.shields.io/badge/repo-workroom--tiger-1B2A4E)](https://github.com/geniusdapeng-collab/workroom-tiger)

</div>

> **本系统当前仅运行于模拟盘（paper trading），不构成任何投资建议，不承诺收益。**

---

## 产品定位

老虎交易是一套**由 AI 基金经理统筹的全自动化交易系统**：以自研 21 环节管线与六层决策栈为交易内核，以 [WorkLoom IM](governance/README.md) 为治理外壳，严格遵守[《AI 短线美股交易（1–15 天波段版）白皮书》](reports/AI短线美股交易白皮书_20260730.pdf)的全部交易纪律。

大多数 AI 炒股项目死在同一个地方：**让模型既当裁判又当运动员**——预测不可证、过程不可审、亏损不可追。老虎交易的答案是分层：**语义归 LLM，数值归规则，闸门必须确定**——LLM 只负责读新闻、评叙事、参与多空辩论；开仓、仓位、止损、熔断全部是确定性规则，同一输入永远同一输出。治理外壳再把每个交易动作变成可追责事件，把白皮书红线变成机器级保险的第二道锁。

```
┌──────────── WorkLoom IM 治理外壳（governance/，TypeScript）────────────┐
│ 五元事件库(哈希链留痕) · 围栏引擎(白皮书阈值→block级基线) · Quest 编排    │
│ night-shift 夜班值守 · 审批卡片 · 组织记忆 · site 官网(责任界面)          │
│ ┌────────── 交易内核（仓库根目录，Python）─────────────────────────────┐ │
│ │ 21 环节管线 · 六层决策栈(L0扫描/L1 MRS/L2 SHS/L2b ICS/L3 TSS/L4风控) │ │
│ │ SearchHub 6源 · 七环行情降级链 · 零基线纪律 · journal→WFA→DSR        │ │
│ │ 小G模拟盘 · 双页签HTML日报 · 复盘闭环(诸葛:归因/体检/提案审批)        │ │
│ │ 工程红线(LLM不可逆/透传留痕/环节点名)                                │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

- **交易内核**：仓库根目录即交易内核（[完整文档](docs/CAISHEN_README.md)）。决策栈、阈值、风控公式以 `trading_system/config.py` 为 single source of truth。
- **治理外壳**：`governance/` 为 WorkLoom IM 底座，只做治理不做决策——把每个交易动作变成可追责事件。接入设计见 [docs/GOVERNANCE.md](docs/GOVERNANCE.md)。
- **行业角色包**：`governance/bundles/trading/`——AI 基金经理班组 presets、三层围栏包、投资 skills（根目录 `bundles/trading/` 为指向性说明）。

---

## 核心能力

### 交易内核：21 环节管线与六层决策栈

- **六层决策栈**：L0 全市场扫描 → L1 MRS（市场许可）→ L2 SHS（主线确认）→ L2b ICS（链景气确认）→ L3 TSS（买点）→ L4 风控——先许可、再主线、确认链景气、最后买点，顺序不可颠倒；
- **SearchHub 6 源 + 七环行情降级链**：多源采集带降级，但**绝不回退合成数据**——数据地基不实立即诚实失败；
- **零基线纪律**：每轮从零开始，不路径依赖；
- **开仓硬逻辑**：标准做多 MRS*≥6 且 SHS≥7.5 且 TSS_final≥7.2；轻仓通道仓位 ×0.30–0.40；MRS*<4.0 禁开仓；
- **风控机械化**：1R=净值 0.8%；单票 ≤20%；结构/时间止损；≥2R 盈利保护；Kill Switch；
- **迭代诚实**：journal → WFA → DSR 校正闭环，不显著保持默认参数；
- **工程红线**：21 环节点名缺一即事故；LLM 不可逆（无规则回退分支）；异常透传留痕——`main.py --demo` 实测可见 LLM 环节全部「🔁透传」披露而非伪造分数。

### 治理外壳：只做治理，不做决策

- **五元事件库**：每个交易动作（谁 / 何时 / 对什么 / 做了什么 / 规则影响）append-only 落库，SHA-256 哈希链逐条锁定，`pnpm db:verify-chain` 可现场逐条重算；
- **围栏引擎**：白皮书阈值编译为 block 级基线——突破红线的动作系统物理上无法执行；基线只可收紧不可放宽；
- **Quest 编排与审批卡片**：复盘提案、参数调整、授权变更全部以审批卡呈现，人 30 秒批完；
- **night-shift 夜班值守**：盘后批量作业（复盘 / 归因 / 体检）落在免打扰时段，清晨交付决策包；
- **组织记忆**：每轮决策的依据、每次驳回的理由沉淀为可检索记忆。

### AI 基金经理与数字 CEO

治理外壳内置 AI 基金经理班组（37 名数字员工：多方/空方研究员、链景气/链舆情分析官、数据质量官、辩论主持官、全球宏观哨兵、盘中交易员、组合风险官、校准官、合规官……）。数字 CEO 统领班组但**出厂默认关闭**：启用须在「董事长视图」完成六步深度授权（风险揭示 / 逐条确认 / 边界三滑杆 / 试用计划 / 身份核验 / 签署留痕），先当 3 天「影子」只模拟不执行，再 7 天试用（权限减半），到期不自动续期；五级权限，禁区物理熔断，每个决策都进不可篡改的事件账。

### 通用模型路由：金融降级铁律

金融版接入底座通用模型路由，并落地金融特有的降级铁律：**语义归 LLM，数值归规则；LLM 失败唯一出路是透传披露，禁止降档重答——宁可不答，不可错答。**

- **金融场景路由表**（`governance/bundles/trading/model-policy.yml`）：新闻标注 / 科技舆情 L2 盘前谷时批量；板块叙事评分 / 多空辩论 L3 旗舰档（`noDowngrade` + `fallback: passthrough-disclose`）
- **内核场景策略桥**（`trading_system/llm/scene_policy.py`）：交易内核 5 个 LLM 环节（clean.llm_semantic / tech.sentiment / tech.risk / sector.narrative / decision.debate）一一登记映射；`validate_coverage()` 启动期守卫——环节未登记即配置事故，不进运行期
- **盘中零 LLM 不变**：全部秒级交易决策保持纯规则确定性，LLM 负载全部在盘前 / 盘后批量窗口（天然谷时 ×0.2）

### 白皮书十条铁律（全系统最高纪律，任何升级不得违反）

1. **Edge 不是预测是流程**：先许可(MRS)→再主线(SHS)→确认链景气(ICS)→最后买点(TSS)
2. **刻意不赚的钱**：不赌财报；MRS*<4 不硬做；数据地基不实立即诚实失败
3. **开仓硬逻辑**：标准做多 MRS*≥6 且 SHS≥7.5 且 TSS_final≥7.2；轻仓通道仓位 ×0.30–0.40；MRS*<4.0 禁开仓
4. **风控机械化**：1R=净值 0.8%；单票≤20%；结构/时间止损；≥2R 盈利保护；Kill Switch
5. **数据工程**：多源降级链绝不回退合成数据；零基线纪律每轮从零开始
6. **工程红线**：21 环节点名缺一即事故；LLM 不可逆（无规则回退分支）；异常透传留痕
7. **迭代诚实**：WFA+DSR 校正，不显著保持默认参数
8. **模拟盘公开验证**：全 AI 掌控、赚亏如实、禁止粉饰
9. **AI 分工**：语义归 LLM，数值归规则，闸门必须确定
10. **一致性**：同输入同输出，回测/复盘/迭代的前提

---

## 系统截图（模拟运行态实拍）

以下截图均来自系统**模拟运行态**：治理外壳为种子演示数据 + 离线确定性模型（页面顶部琥珀色横幅「当前为全模拟运行态」为系统原生标识）；交易内核日报为 `main.py --demo`（离线合成演示数据，端到端 21 环节）真实运行产物。

### PC 端 · 治理外壳与交易内核

| 经营剧场 · 老虎交易工作台（默认首页） | 工作台 · 总览 |
|---|---|
| ![经营剧场](docs/images/shots/pc-home.png) | ![工作台总览](docs/images/shots/pc-workbench.png) |

| 审批中心 | 规则与权限 |
|---|---|
| ![审批中心](docs/images/shots/pc-approval.png) | ![规则与权限](docs/images/shots/pc-rules.png) |

| 夜班中心频道 | 董事长视图 · 数字 CEO |
|---|---|
| ![夜班中心](docs/images/shots/pc-night.png) | ![董事长视图](docs/images/shots/pc-chairman.png) |

| 落地向导（接入真实数据） | 交易内核 · 双页签决策日报 |
|---|---|
| ![落地向导](docs/images/shots/pc-onboarding.png) | ![决策日报](docs/images/shots/pc-daily-report.png) |

### 移动端

| 交易内核 · 决策日报（移动视口） | 治理外壳 · 掌上日报（手机壳页） | C 端 · AI 服务前台（对话） | C 端 · 服务大厅 |
|---|---|---|---|
| ![决策日报移动版](docs/images/shots/mb-daily-report.png) | ![掌上日报](docs/images/shots/mb-handoff.png) | ![AI服务前台](docs/images/shots/mc-chat.png) | ![服务大厅](docs/images/shots/mc-service.png) |

---

## 使用方式

### 快速开始（交易内核）

```bash
bash scripts/setup.sh                # 一键安装+自检（或 pip install -r requirements.txt）
python3 -m pytest tests/ -q          # 272 项测试
python3 main.py --demo               # 离线演示（合成数据，端到端 21 环节）
python3 main.py --universe full --top 30 --picks 8   # 生产模式（每日全市场）
bash scripts/stack_setup.sh          # 全栈：+ WorkLoom 治理底座（Node≥24/Docker）

# S6 复盘闭环与审批流
python3 main.py --review-list                            # 查看 WFA 参数提案
python3 main.py --review-approve <id>                    # 批准 → 次日生效并披露
python3 main.py --review-reject <id> --reason "..."      # 驳回（原因必填）
```

LLM 双模驱动：自有 API（`LLM_BACKEND=api`，任何 OpenAI 兼容端点）或本地 Agent 模型（`LLM_BACKEND=local`，自动探测 Ollama / LM Studio）；不配则红线透传、绝不伪造语义分。详见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

### 治理外壳三端（模拟运行态）

```bash
cd governance
pnpm setup && pnpm preview:all
```

| 端 | 地址 | 说明 |
|---|---|---|
| PC 端 · 治理工作台 | http://localhost:3000 | 经营剧场（老虎交易工作台）/ 审批中心 / 夜班中心 / 董事长视图 |
| 移动端 · C 端 | http://localhost:3002 | 老虎交易 AI 服务前台 H5（查净值 / 数据服务 / 异常申报，演示直登） |

> 治理外壳默认进入 `yunqi-hotel` 演示工作区；切换老虎交易工作区：启动 web 时设置 `VITE_DEMO_WORKSPACE=tiger-trading`（成员 `VITE_DEMO_MEMBER=MEM-T001`）。

### 从模拟运行态到真实数据

治理外壳种子完成后即进入**全模拟运行态**（演示数据 + 内置确定性模型，零外部依赖），页面顶部常显琥珀色横幅提示。点击「接入真实数据 →」进入**落地向导**（`/onboarding`）：① 环境自检（自动）→ ② 接入真实大模型（DeepSeek / Kimi / 智谱 / OpenAI 预设一键填，真实试调通过才落盘，保存即全链生效免重启）→ ③ 登记经营主体 → ④ 启用真实模式（横幅熄灭，全程五元事件留痕）。

---

## 面向 AI Coding Agent

治理外壳对 AI Coding Agent 原生友好（继承 WorkLoom IM 底座约定）：

1. 进仓先读 [`governance/README.md`](governance/README.md)；全量能力清单见 `governance/docs/capability-map.md`；
2. **首启必跑 `pnpm preview:all`**（governance 目录下），未完成视为环境初始化未完成；
3. **一键能力巡游 `pnpm agent:tour`**：环境 + 数据 + 服务 + 浏览器四层自检；
4. **本仓自带「操作电脑」能力**（`governance/packages/base/computer-use/`，65 动作三层感知：L1 浏览器 DOM 级 / L2 全 GUI 语义树 / L3 截图像素级，不依赖任何沙箱）：`pnpm computer:preflight && pnpm computer:smoke` 即验；可装到生产专用工作站，HTTP / MCP 远程驱动见 `governance/docs/computer-use-production.md`；
5. **验证纪律**：改完代码必跑 `pnpm suite`；发布前必跑 `pnpm release:gate`；改事件 / 号源后跑 `pnpm db:verify-chain`；UI 改动必须用浏览器能力实际打开页面截图核对。

---

## 文档地图

| 文档 | 说明 |
|---|---|
| [docs/CAISHEN_README.md](docs/CAISHEN_README.md) | 交易内核完整文档 |
| [reports/AI短线美股交易白皮书_20260730.pdf](reports/AI短线美股交易白皮书_20260730.pdf) | 交易理念白皮书（最高纪律） |
| [docs/GOVERNANCE.md](docs/GOVERNANCE.md) | WorkLoom 治理外壳接入设计 |
| [docs/PROJECT_INTRO.md](docs/PROJECT_INTRO.md) / [docs/PROJECT_INTRO_EN.md](docs/PROJECT_INTRO_EN.md) | 项目介绍（中文详细版 / English） |
| [README_EN.md](README_EN.md) | English README |
| [research/UPGRADE_PLAN_v3.md](research/UPGRADE_PLAN_v3.md) | 升级方案（已确认） |
| [research/01_benchmark.html](research/01_benchmark.html) | 调研一：行业标杆竞品深度调研（桥水 AIA 等 9 家） |
| [research/02_frontline.html](research/02_frontline.html) | 调研二：投资团队一线作业与 know-how 调研 |
| [docs/AGENT_CENSUS.md](docs/AGENT_CENSUS.md) | Agent 普查（20 个 Agent/模块分类） |
| [docs/DATA_HYGIENE.md](docs/DATA_HYGIENE.md) | 数据卫生纪律 |
| [docs/PUBLIC_VERIFICATION.md](docs/PUBLIC_VERIFICATION.md) | 模拟盘公开验证章程 |

## 合规声明

本系统为技术研究与模拟验证项目：**仅模拟盘，不做真实下单**；不构成投资建议；免费行情源存在延时（已在报告中标注）。实盘交易需另行接入持牌券商并满足各市场监管要求（美股 SEC 15c3-5、A股程序化交易报备、港股 SFC）。

## 许可证

Apache-2.0。
