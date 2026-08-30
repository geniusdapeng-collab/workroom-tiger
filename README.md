# 老虎全球资产管理（Tiger Global Asset Management）

> 由 AI 基金经理统筹的全自动化交易系统 —— 覆盖美股 / A股 / 港股，24 小时运行。
>
> **No prediction. Process, discipline, audit.**

老虎交易以[财神爷 AI 炒股系统](docs/CAISHEN_README.md)（v6.3）为交易内核，以 [WorkLoom IM](governance/README.md) 为治理外壳，严格遵守[《AI 短线美股交易（1–15 天波段版）白皮书》](reports/AI短线美股交易白皮书_20260730.pdf)的全部交易纪律。

**本系统当前仅运行于模拟盘（paper trading），不构成任何投资建议，不承诺收益。**

## 架构：交易内核 + 治理外壳

```
┌──────────── WorkLoom IM 治理外壳（governance/，TypeScript）────────────┐
│ 五元事件库(哈希链留痕) · 围栏引擎(白皮书阈值→block级基线) · Quest 编排    │
│ night-shift 夜间守夜 · 审批卡片 · 组织记忆 · site 官网(责任界面)          │
│ ┌────────── 财神爷交易内核（仓库根目录，Python，完整保留）──────────────┐ │
│ │ 21 环节管线 · 六层决策栈(L0扫描/L1 MRS/L2 SHS/L2b ICS/L3 TSS/L4风控) │ │
│ │ SearchHub 6源 · 七环行情降级链 · 零基线纪律 · journal→WFA→DSR        │ │
│ │ 小G模拟盘 · 双页签HTML日报 · 复盘闭环(诸葛:归因/体检/提案审批)        │ │
│ │ 工程红线(LLM不可逆/透传留痕/环节点名)                                │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

- **交易内核**：仓库根目录即财神爷系统，[原 README 见此](docs/CAISHEN_README.md)。决策栈、阈值、风控公式以 `trading_system/config.py` 为 single source of truth。
- **治理外壳**：`governance/` 为 WorkLoom IM 底座，只做治理不做决策——把每个交易动作变成可追责事件，把白皮书红线变成机器级保险的第二道锁。接入设计见 [docs/GOVERNANCE.md](docs/GOVERNANCE.md)。
- **行业角色包**：`governance/bundles/trading/`——AI 基金经理班组 presets、三层围栏包、投资 skills（根目录 `bundles/trading/` 为指向性说明）。

## 快速开始（交易内核）

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


LLM 双模驱动：自有 API（`LLM_BACKEND=api`，任何 OpenAI 兼容端点）或本地 Agent 模型（`LLM_BACKEND=local`，自动探测 Ollama/LM Studio）；不配则红线透传、绝不伪造语义分。详见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 白皮书十条铁律（全系统最高纪律，任何升级不得违反）

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

## 文档地图

| 文档 | 说明 |
|---|---|
| [docs/CAISHEN_README.md](docs/CAISHEN_README.md) | 财神爷交易内核完整文档（v6.3） |
| [reports/AI短线美股交易白皮书_20260730.pdf](reports/AI短线美股交易白皮书_20260730.pdf) | 交易理念白皮书（最高纪律） |
| [docs/GOVERNANCE.md](docs/GOVERNANCE.md) | WorkLoom 治理外壳接入设计 |
| [docs/PROJECT_INTRO.md](docs/PROJECT_INTRO.md) / [docs/PROJECT_INTRO_EN.md](docs/PROJECT_INTRO_EN.md) | 项目介绍（中文详细版 / English） |
| [README_EN.md](README_EN.md) | English README |
| [research/UPGRADE_PLAN_v3.md](research/UPGRADE_PLAN_v3.md) | 升级方案 v3（财神爷主体版，已确认） |
| [research/01_benchmark.html](research/01_benchmark.html) | 调研一：行业标杆竞品深度调研（桥水 AIA 等 9 家） |
| [research/02_frontline.html](research/02_frontline.html) | 调研二：投资团队一线作业与 know-how 调研 |
| [docs/AGENT_CENSUS.md](docs/AGENT_CENSUS.md) | Agent 普查（20 个 Agent/模块分类） |
| [docs/DATA_HYGIENE.md](docs/DATA_HYGIENE.md) | 数据卫生纪律 |
| [docs/PUBLIC_VERIFICATION.md](docs/PUBLIC_VERIFICATION.md) | 模拟盘公开验证章程 |

## 合规声明

本系统为技术研究与模拟验证项目：**仅模拟盘，不做真实下单**；不构成投资建议；免费行情源存在延时（已在报告中标注）。实盘交易需另行接入持牌券商并满足各市场监管要求（美股 SEC 15c3-5、A股程序化交易报备、港股 SFC）。
