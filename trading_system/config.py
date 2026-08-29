"""全局配置 — 与《核心指标及计算公式》波段版逐条对齐。

所有权重 / 阈值 / 档位表的 single source of truth。
修改必须同步更新 docs/UPGRADE_REPORT.md 的版本记录。
"""

# ============================================================
# MRS（市场共振评分）— 波段版权重（理论 §2.2）
# ============================================================
MRS_WEIGHTS = {
    "macro": 0.30,   # 宏观/利率水龙头
    "tech": 0.30,    # 技术结构
    "flow": 0.20,    # 资金广度
    "sent": 0.10,    # 情绪
    "micro": 0.10,   # 微观结构
}

# 一致性修正（理论 §2.3）：Δ = max(S_i) - min(S_i)，五维极差
# Δ < 4 → k=1.0；4 ≤ Δ ≤ 6 → k=0.8；Δ > 6 → k=0.5
def consistency_k(delta: float) -> float:
    if delta < 4.0:
        return 1.0
    if delta <= 6.0:
        return 0.8
    return 0.5

# MRS* → 总仓位上限（理论 §2.4）：(下限%, 上限%)
MRS_POSITION_CAP = [
    (8.0, 10.01, 0.70, 0.90),
    (6.0, 8.0, 0.40, 0.70),
    (4.0, 6.0, 0.10, 0.25),
    (0.0, 4.0, 0.00, 0.10),
]

MRS_GATE_BLOCK = 4.0          # MRS* < 4.0 禁止新开波段仓（除对冲白名单）
MRS_GATE_LIGHT = 6.0          # MRS* < 6.0 轻仓试探
EVENT_OVERNIGHT_DISCOUNT = (0.5, 0.7)  # 事件日前隔夜仓位折扣区间
# v6.0 事件风险管理（白皮书§11 + §4.5 三重现实折扣，机器执行）
EVENT_MACRO_DISCOUNT = 0.8      # CPI/FOMC/非农当日 Gross Cap ×0.8 + 加仓闸门延后30分钟
EVENT_OPEX_DISCOUNT = 0.9       # 期权到期周（每月第三个周五所在周）×0.9，偏模板A
EVENT_EARNINGS_SIZE = 0.5       # 标的 1-2 天内财报：仓位 ×0.5（或回避）
# v6.0 冲击折扣（Kill Switch，白皮书§4.5/§10.4）与流动性折扣阈值
SHOCK_SPY_1D_DROP = -0.04       # SPY 单日跌幅 ≤ -4%：逻辑断裂级冲击
SHOCK_VIX_1D_SPIKE = 0.30       # VIX 单日涨幅 ≥ 30%：恐慌冲击
LIQ_STRESS_ADV_RATIO = 0.65     # 池内 ADV20/ADV60 中位比 < 0.65：成交显著低于常态
LIQ_STRESS_DISCOUNT = 0.8       # 流动性折扣 Gross Cap ×0.8（理论区间 0.7-0.9 取中）

# ============================================================
# SHS（板块热度评分）— 波段版权重（理论 §3.2）
# ============================================================
SHS_WEIGHTS = {
    "macro": 0.25,   # 宏观适配（天时）
    "flow": 0.35,    # 资金动量（地利，最关键）
    "narr": 0.25,    # 叙事兑现/预期差（人和）
    "micro": 0.15,   # 微观结构催化（加速器）
}

SHS_MAIN_POOL = 7.5          # 主线入池：SHS ≥ 7.5 且广度 ≥ 60%
SHS_SUB_POOL = 7.0           # 次主线备选：7.0 ≤ SHS < 7.5
MAIN_POOL_MAX = 2            # 主线池最多 1-2 条（强制聚焦）
BREADTH_HEALTHY = 60.0       # 板块内部广度健康线（%）
BREADTH_NO_CHASE = 40.0      # 广度 < 40% 且仅权重拉动：不做追高

# 利率趋势 → 板块宏观适配固定映射表（理论 §2.1）
# TNX 20 日变化：Up(>+10bp) / Flat([-10,+10]bp) / Down(<-10bp)
SECTOR_MACRO_MAP = {
    # sector_etf: {Down, Flat, Up}
    "XLK":  {"Down": 9, "Flat": 6, "Up": 2},   # 长久期成长（软件/云）
    "SMH":  {"Down": 8, "Flat": 6, "Up": 3},   # 半导体
    "IWM":  {"Down": 7, "Flat": 6, "Up": 4},   # 小盘
    "XLF":  {"Down": 4, "Flat": 6, "Up": 8},   # 金融
    "XLU":  {"Down": 9, "Flat": 6, "Up": 2},   # 公用事业
    "XLRE": {"Down": 9, "Flat": 6, "Up": 2},   # REIT
    "XLE":  {"Down": 5, "Flat": 6, "Up": 6},   # 能源
    "XLP":  {"Down": 6, "Flat": 6, "Up": 5},   # 必选消费
    "XLV":  {"Down": 6, "Flat": 6, "Up": 5},   # 医疗
    "XLI":  {"Down": 6, "Flat": 6, "Up": 6},   # 工业
    "XLY":  {"Down": 7, "Flat": 6, "Up": 4},   # 可选消费
    "IBB":  {"Down": 8, "Flat": 6, "Up": 3},   # 生物科技
}

# 板块资金动量聚合：S_flow = round(0.45*A + 0.25*B + 0.30*C)
# A=RS斜率分位 B=R20 C=板块广度；叙事：A=EPS修正 B=指引 C=IV压缩；微观：A=CallOI B=Skew
SHS_FLOW_AGG = {"A": 0.45, "B": 0.25, "C": 0.30}
SHS_NARR_AGG = {"A": 0.45, "B": 0.35, "C": 0.20}
SHS_MICRO_AGG = {"A": 0.60, "B": 0.40}

# ============================================================
# TSS（交易建仓评分）— 波段版权重（理论 §4.1）
# ============================================================
TSS_WEIGHTS = {"structure": 0.40, "momentum": 0.40, "options": 0.20}
# 组件内聚合键统一为 A/B/C：structure=距关键位/突破回踩质量/流动性真空
TSS_STRUCTURE_AGG = {"A": 0.40, "B": 0.40, "C": 0.20}
# momentum=均线结构/波动收缩/ADX；options=CallOI分位/PC分位/IV分位
TSS_MOMENTUM_AGG = {"A": 0.40, "B": 0.35, "C": 0.25}
TSS_OPTIONS_AGG = {"A": 0.45, "B": 0.30, "C": 0.25}

# 三分数联动开仓标准（理论 §5）
OPEN_LONG = {"mrs": 6.0, "shs": 7.5, "tss": 7.2}
# v6.0：mrs_lo 收编 config（此前 5.5 硬编码在 risk_manager_agent）
LIGHT_PROBE = {"mrs_lo": 5.5, "tss": 7.8, "size_ratio": (0.30, 0.40)}  # 轻仓试错

# ============================================================
# 风控（理论 §6）
# ============================================================
RISK_R_PCT = 0.008           # 单笔最大风险 r = 账户净值 0.8%（0.5%-1.0% 区间中值）
# v6.0 组合层风控（白皮书§9/§10："仓位是风险预算"的机器执行）
MAX_CHAIN_RISK_PCT = 0.030   # 单条产业链累计风险 ≤ 账户 3%（防"N 个独立 R 实为同一个 R"）
ENFORCE_GROSS_CAP = True     # 计划总敞口不得超过 MRS* 仓位上限（截断多余 picks）
# v6.0 交易成本模型（白皮书§14 净口径调参）：单边 bps，作用于成交/结算价
COST_BPS = 10.0              # 单边 10bp（佣金+滑点合并估计，可按券商实际调整）
# v6.0 ATR 档位化时间止损（白皮书§10.3 "资金效率是硬指标"）：
# (ATR%上限, 时间止损交易日)——低波动给足 7 日，高波动 5 日快进快出
TIME_STOP_BY_ATR = [(0.04, 7), (0.07, 6), (9.9, 5)]
POSITION_FIRST = (0.40, 0.50)   # 首仓比例
POSITION_SECOND = (0.30, 0.40)  # 二仓比例
POSITION_THIRD = (0.10, 0.20)   # 三仓比例
PROFIT_PROTECT_R = 2.0          # 浮盈 ≥ 2R 启动盈利保护
TIME_STOP_DAYS = (5, 7)         # 时间止损窗口
MAX_SINGLE_POSITION_PCT = 0.20  # 单票仓位上限（行为风控）
MAX_PICKS_DEFAULT = 7

# ============================================================
# 全市场扫描器（Universe Scanner）硬过滤
# ============================================================
SCAN_MIN_PRICE = 5.0           # 最低价 $5（排除仙股）
SCAN_MIN_ADV_USD = 20_000_000  # 20 日平均成交额 ≥ $20M
SCAN_MIN_HISTORY_DAYS = 260    # 至少一年数据
SCAN_MAX_ATR_PCT = 0.12        # ATR14/价格 ≤ 12%（排除病态波动）
SCAN_TOP_N = 30                # 扫描产出候选数（进 TSS 精评）
SCAN_MAINLINE_BOOST = 5        # 主线定向补扫：主线池内额外带入的候选数

# full 模式（真实全市场 6000+ 只）两级拉取：先短历史预筛流动性，再拉全历史
FULL_PREFILTER_THRESHOLD = 1000   # 池规模超过此值启用预筛
FULL_PREFILTER_DAYS = 30          # 预筛用短历史长度
FULL_HEAVY_CAP = 800              # 预筛后进入重量级拉取的上限（按 ADV 排序）

# 扫描排序分（量化初排，非 TSS）
SCAN_RANK_WEIGHTS = {
    "rs_63": 0.30,     # 63 日相对 SPY 强度（一年分位）
    "rs_20": 0.25,     # 20 日相对强度
    "ma_align": 0.20,  # 均线多头结构
    "contraction": 0.15,  # 波动率收缩（蓄势）
    "near_high": 0.10, # 接近 52 周高点（强者恒强）
}

# ============================================================
# 产业链周期（ICS）— 新增大模块
# ============================================================
ICS_WEIGHTS = {
    "chain_rs": 0.30,       # 链整体相对强度
    "link_breadth": 0.25,   # 链内各环节广度（上中下游联动健康度）
    "rotation": 0.25,       # 环节轮动信号（领涨环节是否向中下游传导）
    "cycle_stage": 0.20,    # 周期阶段评分
}
ICS_HOT = 7.0               # ICS ≥ 7.0：产业链处于可交易热区
# 产业链加成：处于扩张期链条内的候选股 TSS 排序加成（乘性，≤1.15）
CHAIN_BONUS_MAX = 1.15

# ============================================================
# 数据与运行
# ============================================================
BENCHMARK = "SPY"
NEUTRAL_SCORE = 5.0          # 指标缺失时中性分（理论约定），并记录 evidence
PERCENTILE_WINDOW = 252      # 分位数滚动窗口（理论约定，约一年）
SECTOR_ETFS = ["XLK", "SMH", "XLF", "XLE", "XLV", "XLP", "XLY", "XLI", "XLU", "XLRE", "IBB", "IWM"]

DATA_PROVIDER = "yahoo"      # yahoo | stooq | demo
CACHE_DIR = "cache"
REPORTS_DIR = "reports"
