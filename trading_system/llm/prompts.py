"""LLM 任务提示词模板 — 每个 LLM 驱动环节的系统/用户提示词集中管理。

设计原则：输出强制 JSON（schema_hint 与代码内解析一一对应）；
提示词中明确"只允许基于给定文本推理，禁止编造未出现的事实"。
"""

from __future__ import annotations

SYSTEM_ANALYST = (
    "你是美股短线交易系统的语义分析引擎。你的任务是对给定的财经文本做"
    "实体消歧、情感分析、事件抽取与产业链关联推断。"
    "只允许基于输入文本中实际出现的信息推理，禁止编造；"
    "无法判断时输出 null/空数组，禁止猜测。始终输出 JSON。"
)

# ---- 清洗：实体消歧 + 情感 + 事件抽取 ----
CLEAN_SCHEMA = {
    "documents": [
        {
            "id": "原文档id",
            "tickers": ["消歧后确认相关的股票代码"],
            "sentiment": "bullish|bearish|neutral|mixed",
            "sentiment_score": "浮点 -1..1",
            "events": ["事件类型: earnings|guidance|downgrade|upgrade|lawsuit|"
                       "supply_chain|policy|patent|capacity|macro|other"],
            "summary_zh": "一句中文摘要",
            "relevance": "0..1 对短线交易的相关度",
        }
    ]
}
CLEAN_USER_TMPL = "对以下 {n} 篇文档逐篇分析：\n\n{docs}"

# ---- 产业链舆情：环节级情感聚合 ----
SENTIMENT_SCHEMA = {
    "chains": [
        {
            "chain_id": "存储|逻辑芯片|代工|设备|AI模型|AI应用 之一",
            "heat": "0..10 舆情热度（讨论量×情感强度）",
            "sentiment_score": "-1..1",
            "key_drivers": ["驱动因素短语"],
            "narrative_change": " improving|stable|deteriorating",
        }
    ]
}
SENTIMENT_USER_TMPL = (
    "以下是科技股产业链相关文档（已消歧）。按六条链分别聚合舆情：\n\n{docs}"
)

# ---- 产业链风险：供应链扰动/政策/专利 ----
RISK_SCHEMA = {
    "alerts": [
        {
            "severity": "1..10",
            "chain_id": "受影响链",
            "link": "upstream|midstream|downstream|unknown",
            "type": "supply_chain|policy|patent|capacity|other",
            "headline_zh": "一句中文预警",
            "transmission": ["可能传导到的环节/标的"],
            "evidence_ids": ["支撑文档id"],
        }
    ]
}
RISK_USER_TMPL = (
    "从以下文档中识别科技股产业链风险信号（供应链扰动、政策微调、专利诉讼、"
    "产能/库存异常）。只报告文本中有明确依据的信号：\n\n{docs}"
)

# ---- 板块叙事：EPS 修正/指引语义（SectorAgent 叙事维度 LLM 化）----
NARRATIVE_SCHEMA = {
    "sectors": [
        {
            "etf": "板块ETF代码",
            "narrative_score": "0..10（0=叙事全面恶化, 5=中性, 10=叙事强劲兑现）",
            "eps_revision_bias": "up|flat|down|unknown",
            "guidance_tone": "positive|neutral|negative|unknown",
            "evidence": ["一句依据"],
        }
    ]
}
NARRATIVE_USER_TMPL = (
    "根据以下财报电话会/研报/新闻文本，评估各板块当前的叙事兑现度"
    "（盈利修正方向与指引语气）：\n\n板块: {etfs}\n\n文档:\n{docs}"
)

# ---- 多空辩论（decision.debate：Bull/Bear/Coordinator 三角色）----
DEBATE_SCHEMA = {
    "bull_points": ["多头论据（每条一句，必须引用文档或给定依据）"],
    "bear_points": ["空头/魔鬼代言人论据（每条一句）"],
    "coordinator_verdict": "证据充分|存疑|反对",
    "verdict_reason": "裁决理由（只据双方结论与证据，不引入新事实）",
    "falsification_conditions": ["证伪条件（出现即证明多头逻辑失效，至少 1 条）"],
}
DEBATE_USER_TMPL = (
    "你主持一场美股短线标的多空辩论（桥水 AIA 辩论制），标的 {symbol}"
    "（{mode}，个股综合质量 {tss}/10，入场模板 {template}，"
    "产业链 {chain}，板块 {sector}）。\n"
    "五段依据草稿：{draft}\n\n"
    "规则：\n"
    "1. 先以 Bull 角色独立陈述多头论据（只基于给定文档与依据，禁止编造）；\n"
    "2. 再以 Bear（魔鬼代言人）角色独立陈述空头论据，必须主动寻找多头逻辑"
    "最脆弱的环节；\n"
    "3. 最后以 Coordinator 角色裁决：只根据双方已陈述的结论与证据给出"
    "「证据充分/存疑/反对」及理由，不得引入任何新事实（防回音室）；\n"
    "4. 必须给出至少 1 条可证伪条件（出现即证明多头逻辑失效的客观事件）。\n\n"
    "本轮文档证据：\n{docs}"
)
