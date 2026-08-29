"""全市场股票池（Universe）。

支持四种模式：
  core     — 仅大盘核心（约 120 只，快速模式）
  extended — 内嵌高流动性池（约 460 只，默认）
  full     — 真实全市场：NASDAQ Trader 官方上市清单（NASDAQ + NYSE/AMEX，
             约 6000+ 只普通股），下载后本地缓存 7 天，离线回退 extended
  file     — 从用户 CSV/TXT 自定义加载（一行一个代码）

扫描器会对池内每只票做硬过滤 + 量化排序，产出动态 watchlist。
v4.0 起系统不再内置任何“默认自选股”。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# 大盘核心（快速模式 / 冒烟测试用）
CORE_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "AMD", "NFLX",
    "COST", "LLY", "JPM", "V", "UNH", "XOM", "MA", "HD", "PG", "JNJ",
    "CRM", "BAC", "ORCL", "CVX", "MRK", "ABBV", "KO", "PEP", "WMT", "CSCO",
    "ACN", "MCD", "TMO", "ABT", "LIN", "ADBE", "DHR", "TXN", "PM", "NEE",
    "QCOM", "INTU", "AMGN", "IBM", "GS", "MS", "CAT", "GE", "BA", "HON",
    "PLTR", "UBER", "SHOP", "SQ", "SNOW", "DDOG", "CRWD", "PANW", "ANET", "SMCI",
    "MU", "LRCX", "AMAT", "KLAC", "MRVL", "ON", "ARM", "TSM", "ASML", "DELL",
    "COIN", "MARA", "RIOT", "HOOD", "SOFI", "AFRM", "RBLX", "U", "TTD", "APP",
    "ENPH", "SEDG", "FSLR", "RUN", "NOVA", "ARRY", "NXT", "CEG", "VST", "GEV",
    "OKLO", "SMR", "CCJ", "UUUU", "DNN", "LEU", "NNE", "BWXT", "PWR", "EME",
    "IONQ", "RGTI", "QBTS", "QUBT", "SOUN", "BBAI", "AI", "PATH", "UPST", "LMND",
    "RKLB", "LUNR", "ASTS", "SPCE", "JOBY", "ACHR", "EH", "ONDS", "RCAT", "KTOS",
]

# 扩展池（在核心之上追加，合计 ~460 只高流动性标的）
EXTENDED_UNIVERSE = CORE_UNIVERSE + [
    # 科技/软件
    "ADP", "ADI", "APH", "CDNS", "CDW", "CTSH", "DXCM", "EPAM", "FICO", "FTNT",
    "GDDY", "GEN", "GLW", "GRMN", "HPE", "HPQ", "INTC", "JKHY", "KEYS", "LOGI",
    "MCHP", "MPWR", "MSI", "NTAP", "NXPI", "PAYC", "PTC", "ROP", "SNPS",
    "STX", "SWKS", "TDY", "TER", "TRMB", "TYL", "UI", "VRSN", "WDC", "WDAY",
    "ZBRA", "ZM", "DOCU", "OKTA", "NET", "ESTC", "MDB", "GTLB", "CFLT", "BILL",
    "HUBS", "VEEV", "NOW", "TEAM", "MNDY", "ASAN", "SMAR", "PCTY", "PAYX", "BR",
    # 通信/消费互联网
    "ABNB", "BKNG", "CMCSA", "CHTR", "DASH", "EA", "EXPE", "LYV", "MTCH", "PINS",
    "ROKU", "SPOT", "T", "TMUS", "TRIP", "VZ", "WBD", "YELP", "ZG", "Z",
    # 金融
    "AIG", "ALL", "AON", "AXP", "BLK", "BX", "CB", "CME", "COF", "DFS",
    "FIS", "FI", "ICE", "KKR", "MCO", "MET", "NDAQ", "PNC", "PYPL", "SCHW",
    "SPGI", "SYF", "TFC", "TRV", "USB", "WFC", "WTW", "ZION", "C", "KEY", "MTB",
    # 医疗
    "AMGN", "BIIB", "BMY", "CI", "CVS", "DXCM", "ELV", "EW", "GILD", "HCA",
    "HUM", "IDXX", "ILMN", "INCY", "ISRG", "MCK", "MDT", "MOH", "MRNA", "PFE",
    "REGN", "RMD", "SYK", "VRTX", "ZBH", "BSX", "BDX", "BAX", "ALGN", "DXCM",
    # 工业/材料
    "AOS", "DOV", "EMR", "ETN", "FAST", "FDX", "GD", "HWM", "IR", "ITT",
    "LMT", "MMM", "NOC", "OTIS", "PCAR", "PH", "RTX", "SWK", "TT", "UPS",
    "URI", "VMC", "WAB", "XYL", "AA", "ALB", "APD", "CF", "CLF", "FCX",
    "MLM", "NEM", "NUE", "PPG", "SHW", "STLD", "VST", "CE", "DD", "DOW",
    # 能源
    "APA", "BKR", "COP", "CTRA", "DVN", "EOG", "EQT", "FANG", "HAL", "HES",
    "MPC", "MRO", "OKE", "OXY", "PSX", "SLB", "VLO", "WMB", "XOM", "CVX",
    # 消费
    "AEO", "ANF", "BBY", "BURL", "CCL", "CMG", "DECK", "DG", "DLTR", "DPZ",
    "DRI", "EBAY", "EL", "F", "GM", "GPS", "HAS", "HLT", "KMX", "LVS",
    "MAR", "MGM", "NCLH", "NKE", "ORLY", "RCL", "ROST", "SBUX", "TJX", "TGT",
    "ULTA", "VFC", "WYNN", "YUM", "AZO", "BBWI", "CPB", "GIS", "HSY", "K",
    "KHC", "KR", "MDLZ", "MNST", "SJM", "STZ", "TAP", "TSN", "WBA", "CL",
    "CHD", "CLX", "COTY", "EL", "ENR", "KMB", "KVUE", "MKC", "NWL", "PG",
    # 公用事业/REIT
    "AEP", "AWK", "CMS", "CNP", "D", "DTE", "DUK", "ED", "EIX", "ES",
    "ETR", "EXC", "FE", "NEE", "NI", "NRG", "PEG", "PNW", "PPL", "SO",
    "SRE", "WEC", "XEL", "AMT", "ARE", "AVB", "BXP", "CPT", "DLR", "EQIX",
    "EQR", "ESS", "EXR", "INVH", "IRM", "MAA", "O", "PLD", "PSA", "REG",
    "SBAC", "SPG", "UDR", "VICI", "VTR", "WELL", "WY",
    # 新能源/电动车/航空
    "RIVN", "LCID", "NIO", "XPEV", "LI", "FSR", "CHPT", "EVGO", "BLNK", "PLUG",
    "FCEL", "BE", "BLDP", "SPWR", "MAXN", "JKS", "CSIQ", "DQ", "DAL", "UAL",
    "AAL", "LUV", "SAVE", "ALK", "JBLU", "HA", "SKYW",
    # 稀土/材料主题
    "MP", "LAC", "ALTM", "SQM", "LTHM", "PLL", "SGML", "SLI", "TMC", "UUUU",
    # 太空/无人机/机器人
    "AVAV", "TXT", "LHX", "LDOS", "SAIC", "CACI", "MRCY", "VSAT", "IRDM", "GSAT",
    # 量子/AI 二线
    "IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "SOUN", "BBAI", "VERI", "GFAI", "AISP",
    # 加密/金融科技
    "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "BITF", "CORZ", "IREN", "WULF",
    "HOOD", "SOFI", "AFRM", "UPST", "LMND", "NU", "MQ", "FOUR", "TOST", "PAGS",
]

# 去重保序
_seen: set[str] = set()
EXTENDED_UNIVERSE = [t for t in EXTENDED_UNIVERSE if not (t in _seen or _seen.add(t))]


# ============================================================
# 真实全市场清单（NASDAQ Trader 官方 symdir，每日更新，免费无需 key）
# ============================================================
_NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
_OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
_FULL_CACHE = Path(config.CACHE_DIR) / "universe_full.json"
_FULL_TTL_S = 7 * 86400

# 证券名称中排除非普通股的关键词（权证/单位/优先股/债券类）
# 注意：纯子串匹配会误杀——"unit" 曾把 UnitedHealth/UPS/United Airlines 等
# United 系公司全部静默剔除出 full 池（v5.4 修复：单位/权证类用词边界正则）。
import re as _re
_BAD_NAME_KW = (
    "warrant", " right", "preferred", "preference", "depositary",
    "notes", "note ", "debenture", "baby bond", "%", "etn", "etf",
)
_BAD_NAME_RE = _re.compile(r"\bunits?\b")   # "Unit(s)" 独立单词才排除


def _bad_name(name: str) -> bool:
    return any(kw in name for kw in _BAD_NAME_KW) or bool(_BAD_NAME_RE.search(name))


def _clean_symbol(sym: str) -> str | None:
    """清洗代码：只保留字母数字，类别股分隔符统一为 Yahoo 风格的 '-'。"""
    s = sym.strip().upper().replace("/", "-").replace(".", "-").replace("^", "-")
    if not s or any(not ch.isalnum() and ch != "-" for ch in s):
        return None
    if len(s) > 6 or s.endswith("W") and "-" in s:   # 过滤超长与权证尾缀
        return None
    return s


def _parse_symdir(text: str, sym_col: int, test_col: int, etf_col: int,
                  name_col: int, include_etfs: bool) -> list[str]:
    out: list[str] = []
    for ln in text.splitlines()[1:]:
        if ln.startswith("File Creation Time") or "|" not in ln:
            break
        parts = ln.split("|")
        if len(parts) <= max(sym_col, test_col, etf_col, name_col):
            continue
        if parts[test_col].strip() != "N":           # 排除测试证券
            continue
        if not include_etfs and parts[etf_col].strip() == "Y":
            continue
        name = parts[name_col].lower()
        if _bad_name(name):
            continue
        s = _clean_symbol(parts[sym_col])
        if s:
            out.append(s)
    return out


def fetch_full_universe(include_etfs: bool = False, timeout: int = 25) -> list[str]:
    """下载 NASDAQ Trader 官方清单，返回过滤后的全市场普通股代码。

    nasdaqlisted.txt 列: Symbol(0)|Security Name(1)|Market Category(2)|Test Issue(3)|
                         Financial Status(4)|Round Lot Size(5)|ETF(6)|NextShares(7)
    otherlisted.txt  列: ACT Symbol(0)|Security Name(1)|Exchange(2)|CQS Symbol(3)|
                         ETF(4)|Round Lot Size(5)|Test Issue(6)|NASDAQ Symbol(7)
    v5.4 修复：nasdaqlisted 的 etf_col 曾误写 7（NextShares 列），导致
    include_etfs=False 实际不过滤 ETF——QQQ 等名称不含 "etf" 字样的 ETF
    混入全市场股票池被当个股扫描。正确列索引为 6。
    """
    tickers: list[str] = []
    for url, cols in (
        (_NASDAQ_LISTED, (0, 3, 6, 1)),
        (_OTHER_LISTED, (0, 6, 4, 1)),
    ):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        tickers += _parse_symdir(text, *cols, include_etfs)
    return sorted(set(tickers))


def load_full_universe(include_etfs: bool = False) -> tuple[list[str], str]:
    """加载全市场清单：在线下载 → 本地缓存（7天） → 回退 extended。

    返回 (tickers, source)，source ∈ {nasdaqtrader, cache, fallback}。
    """
    try:
        tickers = fetch_full_universe(include_etfs)
        if len(tickers) >= 2000:
            _FULL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _FULL_CACHE.write_text(json.dumps(
                {"ts": time.time(), "tickers": tickers}))
            logger.info("全市场清单下载成功: %d 只（已缓存）", len(tickers))
            return tickers, "nasdaqtrader"
    except Exception as exc:
        logger.warning("全市场清单下载失败: %s", exc)
    if _FULL_CACHE.exists():
        try:
            blob = json.loads(_FULL_CACHE.read_text())
            age = time.time() - blob.get("ts", 0)
            if age < _FULL_TTL_S and len(blob.get("tickers", [])) >= 2000:
                logger.info("使用缓存全市场清单: %d 只（缓存 %.1f 天）",
                            len(blob["tickers"]), age / 86400)
                return blob["tickers"], "cache"
        except Exception:
            pass
    logger.warning("全市场清单不可用，回退内嵌 extended 池（%d 只）", len(EXTENDED_UNIVERSE))
    return list(dict.fromkeys(EXTENDED_UNIVERSE)), "fallback"


def load_universe(mode: str = "extended", file_path: str | None = None) -> list[str]:
    if mode == "core":
        return list(dict.fromkeys(CORE_UNIVERSE))
    if mode == "file":
        if not file_path:
            raise ValueError("mode=file 需要提供 file_path")
        p = Path(file_path)
        tickers = [ln.strip().upper() for ln in p.read_text().splitlines()
                   if ln.strip() and not ln.startswith("#")]
        return list(dict.fromkeys(tickers))
    if mode == "full":
        tickers, _src = load_full_universe()
        return tickers
    return list(dict.fromkeys(EXTENDED_UNIVERSE))
