"""产业链图谱（Industry Chain Map）— 新增大模块：产业链周期。

每条产业链拆为 上游 / 中游 / 下游 三个环节，各环节挂代表性标的与 ETF。
产业链周期 Agent 据此计算：
  - 链整体相对强度（chain_rs）
  - 链内广度（link_breadth，上中下游联动健康度）
  - 环节轮动信号（rotation：领涨环节是否从上游向下游传导）
  - 周期阶段（cycle_stage：复苏 / 扩张 / 过热 / 衰退）

业务含义（为什么 SHS 之外还需要 ICS）：
  SHS 回答“哪个板块热”，ICS 回答“这条产业链的钱流到哪一环、还能流多久”。
  同一板块内部，上游先动、中游确认、下游扩散是典型主升节奏；
  若龙头滞涨而垃圾股补涨，对应“过热/衰退”——SHS 的排雷规则在链维度落地。
"""

from __future__ import annotations

CHAINS: dict[str, dict] = {
    "semis": {
        "name": "半导体产业链",
        "etf": "SMH",
        "upstream": {"name": "设备/材料/EDA",
                     "tickers": ["ASML", "AMAT", "LRCX", "KLAC", "SNPS", "CDNS", "TER", "MKSI", "ENTG", "FORM", "ACLS", "UCTT", "NOVT", "LSCC", "MPWR", "MCHP", "NXPI", "ADI", "TXN", "ON", "SWKS", "GLW"]},
        "midstream": {"name": "设计/制造/代工",
                      "tickers": ["NVDA", "AMD", "AVGO", "QCOM", "MRVL", "TSM", "INTC", "MU", "ARM"]},
        "downstream": {"name": "服务器/整机/存储",
                       "tickers": ["SMCI", "DELL", "HPE", "NTAP", "WDC", "STX", "ANET"]},
    },
    "ai_compute": {
        "name": "AI 算力产业链",
        "etf": "XLK",
        "upstream": {"name": "芯片/网络/电力",
                     "tickers": ["NVDA", "AMD", "AVGO", "ANET", "VST", "CEG", "GEV", "PWR"]},
        "midstream": {"name": "云/数据中心/模型",
                      "tickers": ["MSFT", "GOOGL", "AMZN", "ORCL", "META", "IBM", "EQIX", "DLR"]},
        "downstream": {"name": "AI 应用/SaaS/终端",
                       "tickers": ["PLTR", "CRM", "NOW", "SNOW", "DDOG", "CRWD", "AI", "PATH", "SOUN", "ADBE", "INTU", "WDAY", "TEAM", "MNDY", "HUBS", "VEEV", "OKTA", "NET", "MDB", "ESTC", "GTLB", "CFLT", "BILL", "SMAR", "PCTY", "PAYX", "PAYC", "APP", "TTD", "U", "RBLX"]},
    },
    "ev": {
        "name": "电动车产业链",
        "etf": "XLY",
        "upstream": {"name": "锂矿/电池材料",
                     "tickers": ["ALB", "SQM", "LAC", "PLL", "MP"]},
        "midstream": {"name": "整车制造",
                      "tickers": ["TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM"]},
        "downstream": {"name": "充电/后市场/经销商",
                       "tickers": ["CHPT", "EVGO", "BLNK", "KMX", "AZO", "ORLY"]},
    },
    "fintech": {
        "name": "金融科技产业链",
        "etf": "XLF",
        "upstream": {"name": "交易所/数据/评级",
                     "tickers": ["CME", "ICE", "NDAQ", "SPGI", "MCO", "MSCI"]},
        "midstream": {"name": "银行/券商/资管",
                      "tickers": ["JPM", "BAC", "GS", "MS", "SCHW", "BLK", "HOOD", "WFC", "C", "USB", "PNC", "TFC", "KEY", "MTB", "ZION", "CB", "TRV", "AIG", "MET", "ALL", "AON", "WTW"]},
        "downstream": {"name": "支付/借贷/加密",
                       "tickers": ["V", "MA", "PYPL", "SQ", "SOFI", "AFRM", "UPST", "COIN", "AXP", "COF", "DFS", "SYF", "FIS", "FI", "NU", "TOST", "FOUR", "PAGS", "MQ", "LMND", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "CORZ", "IREN", "WULF"]},
    },
    "energy": {
        "name": "能源产业链",
        "etf": "XLE",
        "upstream": {"name": "勘探开采/油服",
                     "tickers": ["XOM", "CVX", "EOG", "OXY", "SLB", "HAL", "BKR", "FANG", "COP", "APA", "DVN", "CTRA", "EQT", "HES", "MRO"]},
        "midstream": {"name": "管道/储运",
                      "tickers": ["OKE", "WMB", "ET", "KMI", "MPLX"]},
        "downstream": {"name": "炼化/销售/公用",
                       "tickers": ["VLO", "PSX", "MPC", "NEE", "SO", "DUK", "CEG", "VST"]},
    },
    "biotech": {
        "name": "医药生物产业链",
        "etf": "IBB",
        "upstream": {"name": "CXO/器械/设备",
                     "tickers": ["TMO", "DHR", "A", "WAT", "MTD", "IQV", "SYK", "BSX", "ABT", "MDT", "BDX", "ZBH", "ISRG", "IDXX", "ILMN", "RMD", "ALGN", "EW", "DXCM", "BAX"]},
        "midstream": {"name": "创新药/生物制药",
                      "tickers": ["LLY", "VRTX", "REGN", "GILD", "AMGN", "MRNA", "BIIB", "MRK", "JNJ", "PFE", "ABBV", "BMY", "INCY"]},
        "downstream": {"name": "流通/药房/医保",
                       "tickers": ["MCK", "CVS", "WBA", "UNH", "ELV", "CI", "HCA", "HUM", "MOH"]},
    },
    "nuclear": {
        "name": "核能/新能源链",
        "etf": "XLU",
        "upstream": {"name": "铀矿/燃料",
                     "tickers": ["CCJ", "UUUU", "DNN", "LEU"]},
        "midstream": {"name": "堆型/设备/工程",
                      "tickers": ["OKLO", "SMR", "NNE", "BWXT", "GEV", "EME"]},
        "downstream": {"name": "电力运营/数据中心用电",
                       "tickers": ["VST", "CEG", "NRG", "EXC", "PPL", "AEP", "XEL", "WEC", "D", "DTE", "CMS", "CNP", "NI", "PNW", "PEG", "EIX", "FE", "ETR", "ES", "SRE", "ED", "AWK"]},
    },
    "consumer": {
        "name": "消费产业链",
        "etf": "XLP",
        "upstream": {"name": "原料/农业/包装",
                     "tickers": ["TSN", "ADM", "BG", "CF", "MOS"]},
        "midstream": {"name": "品牌/制造",
                      "tickers": ["PG", "KO", "PEP", "NKE", "EL", "CL", "MDLZ", "MNST", "HSY", "GIS", "CPB", "SJM", "K", "KHC", "STZ", "TAP", "CHD", "CLX", "KMB", "KVUE", "MKC", "NWL", "ENR", "BBWI", "COTY", "VFC", "GPS", "DECK"]},
        "downstream": {"name": "零售/餐饮/渠道",
                       "tickers": ["WMT", "COST", "TJX", "TGT", "SBUX", "MCD", "CMG", "HD", "LOW", "DG", "DLTR", "BBY", "YUM", "DPZ", "DRI", "ROST", "BURL", "AEO", "ANF", "ULTA", "KR", "BKNG", "ABNB", "EXPE", "MAR", "HLT", "RCL", "CCL", "NCLH", "LVS", "WYNN", "DAL", "UAL", "AAL", "LUV"]},
    },
    # 第九链：工业制造（覆盖 XLI）——此前 XLI 无链，主线 ETF 命中时定向补扫恒为空
    "industrials": {
        "name": "工业制造产业链",
        "etf": "XLI",
        "upstream": {"name": "机械/自动化/零部件",
                     "tickers": ["CAT", "DE", "ETN", "EMR", "HON", "ROK", "MMM", "HWM", "ITT", "TT", "WAB", "XYL", "DOV", "AOS", "FAST", "PCAR", "PH", "SWK", "OTIS", "IR"]},
        "midstream": {"name": "航空航天/国防/整机",
                      "tickers": ["GE", "BA", "RTX", "LMT", "NOC", "GD", "LHX", "TXT", "LDOS", "SAIC", "CACI", "MRCY", "AVAV", "KTOS", "RCAT", "ONDS", "JOBY", "ACHR", "RKLB", "LUNR", "ASTS", "IRDM", "GSAT", "VSAT"]},
        "downstream": {"name": "物流/运输/工业服务",
                       "tickers": ["UPS", "FDX", "UNP", "CSX", "WM", "RSG"]},
    },
}

# 股票 → (chain_id, link) 反查表
TICKER_TO_CHAIN: dict[str, tuple[str, str]] = {}
for _cid, _c in CHAINS.items():
    for _link in ("upstream", "midstream", "downstream"):
        for _t in _c[_link]["tickers"]:
            TICKER_TO_CHAIN.setdefault(_t, (_cid, _link))

# 板块 ETF → 关联产业链（SHS × ICS 共振用）
SECTOR_TO_CHAINS: dict[str, list[str]] = {
    "SMH": ["semis"],
    "XLK": ["ai_compute", "semis"],
    "XLY": ["ev", "consumer"],
    "XLF": ["fintech"],
    "XLE": ["energy"],
    "IBB": ["biotech"],
    "XLV": ["biotech"],
    "XLP": ["consumer"],
    "XLU": ["nuclear"],
    "IWM": [],
    "XLI": ["industrials"],
    "XLRE": [],
}


def chain_of(ticker: str) -> tuple[str, str] | tuple[None, None]:
    """查询股票所属产业链与环节。"""
    return TICKER_TO_CHAIN.get(ticker.upper(), (None, None))


def mainline_tickers(sector_etfs: list[str]) -> set[str]:
    """主线板块 ETF → 其产业链上的全部成分股（主线定向补扫用）。"""
    out: set[str] = set()
    for etf in sector_etfs:
        for cid in SECTOR_TO_CHAINS.get(etf, []):
            for link in ("upstream", "midstream", "downstream"):
                out.update(CHAINS[cid][link]["tickers"])
    return out


def chain_name_zh(chain_id: str | None) -> str:
    """产业链 ID → 中文名（投资人视角口径）。

    先查科技子链注册表，再查主链注册表，都没有则回退原 ID。
    与报告层口径保持一致；Agent 层证据/交易卡片统一走这里，
    禁止把英文 chain_id 直接写进面向投资人的文本。
    """
    if not chain_id:
        return "-"
    try:
        from .tech_chain.universe import TECH_SUBCHAINS
        node = TECH_SUBCHAINS.get(chain_id)
        if node and node.get("name"):
            return node["name"]
    except Exception:
        pass
    node = CHAINS.get(chain_id)
    if node and node.get("name"):
        return node["name"]
    return chain_id
