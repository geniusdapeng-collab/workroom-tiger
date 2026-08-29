"""科技股产业链扩展图谱 — 专项子集群的标的地基。

六条子链（上游材料 → 制造/代工 → 模型厂商 → 应用端 全覆盖）：
  memory   存储链：三星/海力士/美光 三角 + 模组/服务器下游
  logic    逻辑芯片：设计/EDA
  foundry  代工链：台积电双挂牌（2330.TW 与 TSM ADR 互相印证）
  equipment 设备链：ASML/AMAT/LRCX/KLAC
  ai_model AI 模型： hyperscaler 资本开支
  ai_app   AI 应用：软件/SaaS 变现端

全球化联动（用户指令三）：韩国双雄（005930.KS / 000660.KS）与台积电
（2330.TW / TSM）的相对强度是存储与代工产能/库存周期的先行映射；
它们与美股对应标的的价差/动量背离本身就是信号。
"""

from __future__ import annotations

TECH_SUBCHAINS: dict[str, dict] = {
    "memory": {
        "name": "存储产业链",
        "links": {
            "upstream": ["AMAT", "LRCX", "ASML", "KLAC"],
            "midstream": ["MU", "005930.KS", "000660.KS", "WDC", "STX"],
            "downstream": ["SMCI", "DELL", "HPE", "NTAP"],
        },
        "global_leaders": ["005930.KS", "000660.KS", "MU"],
        "cycle_note": "韩国双雄相对强度 + 美光动量 = 存储价格/库存周期先行映射",
    },
    "logic": {
        "name": "逻辑芯片链",
        "links": {
            "upstream": ["SNPS", "CDNS", "ARM"],
            "midstream": ["NVDA", "AMD", "QCOM", "AVGO", "INTC", "MRVL"],
            "downstream": ["DELL", "SMCI", "AAPL"],
        },
        "global_leaders": ["NVDA", "AMD", "QCOM"],
        "cycle_note": "设计端先行于代工投片 1-2 个季度",
    },
    "foundry": {
        "name": "晶圆代工链",
        "links": {
            "upstream": ["ASML", "AMAT", "LRCX"],
            "midstream": ["TSM", "2330.TW", "GFS", "UMC"],
            "downstream": ["NVDA", "AMD", "QCOM", "AAPL"],
        },
        "global_leaders": ["2330.TW", "TSM"],
        "cycle_note": "代工利用率=全行业景气总闸门；2330.TW 与 TSM ADR 互为印证",
    },
    "equipment": {
        "name": "半导体设备链",
        "links": {
            "upstream": ["ASML", "AMAT", "LRCX", "KLAC", "TER"],
            "midstream": ["TSM", "INTC", "MU", "005930.KS"],
            "downstream": [],
        },
        "global_leaders": ["ASML", "AMAT"],
        "cycle_note": "设备订单是产能周期的最前端（book-to-bill 先行）",
    },
    "ai_model": {
        "name": "AI 模型/云链",
        "links": {
            "upstream": ["NVDA", "AMD", "AVGO", "ANET"],
            "midstream": ["MSFT", "GOOGL", "AMZN", "META", "ORCL"],
            "downstream": ["PLTR", "CRM", "NOW"],
        },
        "global_leaders": ["MSFT", "GOOGL", "META"],
        "cycle_note": "hyperscaler 资本开支指引决定上游 2-4 个季度订单",
    },
    "ai_app": {
        "name": "AI 应用链",
        "links": {
            "upstream": ["MSFT", "GOOGL", "AMZN"],
            "midstream": ["PLTR", "CRM", "NOW", "SNOW", "DDOG", "CRWD"],
            "downstream": [],
        },
        "global_leaders": ["PLTR", "CRM"],
        "cycle_note": "应用端 ARR 兑现速度决定叙事能否闭环",
    },
}

# 搜索主题词（SearchHub 查询规划用；规则只负责"拼接"这一确定性操作）
CHAIN_TOPICS: dict[str, str] = {
    "memory": "DRAM NAND memory chip prices inventory cycle Samsung SK Hynix Micron",
    "logic": "GPU AI chip design Nvidia AMD Broadcom",
    "foundry": "TSMC foundry utilization wafer capacity CoWoS advanced packaging",
    "equipment": "semiconductor equipment orders ASML Applied Materials book-to-bill",
    "ai_model": "hyperscaler AI capex Microsoft Google Meta Amazon data center",
    "ai_app": "AI software monetization Palantir enterprise AI adoption ARR",
}

# v6.3 全领域情报主题（修复"搜索主题 6/6 全科技"的偏科）：
# NarrativeAgent 给全部 12 个板块 ETF 打叙事分，但此前喂给 LLM 的文档
# 100% 是科技新闻——非科技板块的叙事维度实质被架空。以下主题与
# SECTOR_ETFS 全领域一一对应，保证每个板块都有自己的情报输入。
SECTOR_TOPICS: dict[str, str] = {
    "energy": "crude oil OPEC natural gas prices energy stocks Chevron ExxonMobil E&P",
    "financials": "banks net interest margin credit quality JPMorgan financials payments Visa Mastercard",
    "healthcare": "pharma biotech FDA approval drug pipeline Eli Lilly Novo healthcare earnings",
    "consumer": "consumer spending retail earnings Walmart Costco restaurants discretionary staples",
    "industrials": "manufacturing PMI industrial production aerospace defense Boeing Caterpillar logistics",
    "utilities": "utilities electricity demand power grid nuclear natural gas power prices",
    "reits": "REIT commercial real estate office occupancy property REIT dividends rates",
    "macro": "Federal Reserve FOMC CPI inflation jobs report Treasury yields S&P 500 outlook",
}

# 链 → 主决策引擎既有 CHAINS（chains.py）的映射（融合用）
SUBCHAIN_TO_MAIN: dict[str, str] = {
    "memory": "semis",
    "logic": "semis",
    "foundry": "semis",
    "equipment": "semis",
    "ai_model": "ai_compute",
    "ai_app": "ai_compute",
}

ALL_TECH_TICKERS: list[str] = sorted({
    t for c in TECH_SUBCHAINS.values() for link in c["links"].values() for t in link
})
