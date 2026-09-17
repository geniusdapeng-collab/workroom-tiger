# 全系统 Agent 智能化普查（v5.0）

日期：2026-07-30
依据：用户指令二·3 —— 逐一盘点所有 Agent，区分"规则驱动"与"LLM 驱动"；
规则仅保留在"高度确定、逻辑固定、无歧义"的低层操作；凡涉及语义理解、
推理判断、动态决策的环节一律 LLM 化。

## 分类总表

| # | Agent / 模块 | 驱动 | 判定依据 |
|---|---|---|---|
| 1 | UniverseScannerAgent（L0 扫描） | 规则 | 硬过滤阈值（价/ADV/历史/ATR）+ 量化初排，纯数值比较，无歧义 |
| 2 | MRSAgent（L1 大盘） | 规则 | 五维映射表（VIX/利率/广度数值 → 分数），阈值触发型 |
| 3 | SectorAgent（L2 板块） | **混合** | 宏观查表/资金动量=规则；**叙事兑现 → v5.0 起 LLM**（NarrativeAgent 注入） |
| 4 | ChainCycleAgent（L2b ICS） | 规则 | 链 RS/广度/轮动/阶段，行情数值计算 |
| 5 | TSSAgent（L3 个股） | **混合** | 结构/动能=规则量化；期权维度=规则分位；缺失因子再归一化 |
| 6 | RiskManagerAgent（L4 风控） | 规则 | 三层闸门阈值 + R 仓位反推，逻辑固定 |
| 7 | **NarrativeAgent（叙事）** | **LLM** | EPS 修正方向/指引语气 = 语义推理，规则做不了也不许做 |
| 8 | **ChainSentimentAgent（链舆情）** | **LLM** | 情感分析/热度聚合，语义理解 |
| 9 | **ChainRiskAgent（链风险）** | **LLM** | 供应链扰动/政策/专利的事件抽取与传导推断 |
| 10 | TechChainMonitorAgent（链监测） | 规则 | 环节动量/广度，行情数值计算 |
| 11 | CycleLinkageAgent（全球联动） | 规则 | 跨市场相对强度/背离，行情数值计算 |
| 12 | TechChainFusionAgent（链融合） | 规则 | 固定权重合成，无歧义聚合 |
| 13 | 数据清洗 · rule_base | 规则 | 仅去重 + 格式校验（用户指令二·2 划定的规则保留区） |
| 14 | 数据清洗 · llm_semantic | **LLM** | 实体消歧/情感/事件抽取/关联推断——全部 LLM |
| 15 | SearchHub（搜索集成） | 规则 | 并发/缓存/超时/熔断调度；查询词拼接为确定性操作 |
| 16 | 搜索源集群（6 源） | 规则 | 接口适配与解析（格式转换型） |
| 17 | backtest / WFA / DSR | 规则 | 历史模拟与统计检验，确定性计算 |
| 18 | journal / triggers | 规则 | 落账/结算/阈值警报，逻辑固定 |
| 19 | options_metrics | 规则 | 期权链快照 → 分位评分，数值计算 |
| 20 | report / main | 规则 | 格式化输出与流程编排 |

**LLM 驱动环节共 4 个**（clean.llm_semantic、tech.sentiment、tech.risk、
sector.narrative），全部注册在 `redline.STEP_REGISTRY` 并受 `llm_guard` 保护：
LLM 不可用时唯一合法出路是**透传兜底 + 日志**（Passthrough），
代码库中不存在任何"LLM 失败 → 改用规则计算"的回退路径
（由 pytest 行为测试 + 源码静态审查双重锁定）。

## 升级记录（规则 → LLM）

| 环节 | v4.1 状态 | v5.0 处置 |
|---|---|---|
| 板块叙事兑现（SHS·S_narr） | 免费源缺失 → None → 再归一化 | **LLM 推理**（财报电话会/新闻 → EPS 修正方向 × 指引语气 → 0..10 分） |
| 文档情感分析 | 无此环节 | **LLM**（清洗阶段逐篇标注 sentiment/score/events） |
| 实体消歧 | 无（正则匹配 ticker） | **LLM**（模糊实体 → 确认相关代码，杜绝"AAPL=苹果?"式歧义） |
| 产业链风险识别 | 无 | **LLM**（8-K/公报/专利/新闻 → 分级预警 + 传导路径） |
| 产业链舆情温度 | 无 | **LLM**（六子链 heat × sentiment × 叙事变化） |

## 保留规则的边界（为什么不 LLM 化）

- **行情数值映射**（MRS 技术/广度、SHS 资金、TSS 结构/动能、ICS）：
  输入是 OHLCV 数值，输出按理论映射表查表——"阈值触发"同级，零歧义，
  规则不仅更快更稳，而且可回测、可复现（LLM 化反而会引入不可回测性）；
- **闸门与仓位**（RiskManager）：风控必须确定性，同输入同输出；
- **去重/格式校验**：用户指令明确划给规则。

## 红线执行机制

1. **全链路完整**：`ExecutionTracer` 逐环节打点，运行结束 `assert_complete()`
   校验 17 个注册环节无一遗漏，缺失即抛 `RedlineViolation`（系统性事故，停止）；
2. **LLM 不可逆**：`llm_guard()` 是 LLM 环节的唯一执行方式，只有两个出口
   （LLM 真实产出 / Passthrough 透传），测试注入故障锁定行为；
3. **异常透传**：环节无产出 → `Passthrough(payload 原样)` + WARNING 日志，
   日报"红线执行轨迹"章节逐环节披露 ✅/🔁透传 与原因——
   **跳过环节在架构上不存在，降级为规则在代码里不存在**。
