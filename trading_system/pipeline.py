"""流水线引擎 — v5.0 全链路（搜索→清洗→科技链子集群→六层决策），
结构化上下文传递（禁止字符串解析），全环节红线打点（严禁跳过/降级）。

执行顺序（与 redline.STEP_REGISTRY 一致）：
  search.collect → clean.rule_base → clean.llm_semantic
  → tech.monitor / cycle_linkage / sentiment / risk / fusion（科技股专项子集群）
  → sector.narrative（SHS 叙事 LLM 化）
  → MRS(L1) → SHS(L2) → ICS(L2.5) → 全市场扫描(L0) → TSS(L3) → 风控(L4)
  → report.emit（结果封装）
"""

from __future__ import annotations

import logging
import math
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from . import config
from .agents import (
    ChainCycleAgent, MRSAgent, RiskManagerAgent, SectorAgent, TSSAgent,
    UniverseScannerAgent,
)
from .agents.narrative_agent import NarrativeAgent
from .cleaning.pipeline import llm_semantic_clean, rule_base_clean, unwrap_cleaned
from .data_models import PipelineResult
from .events import EventCalendar
from .llm.client import LLMClient, default_client
from .providers import get_provider
from .parameters import GateParams, risk_limits_for, tighten_risk_limits
from .redline import ExecutionTracer, Passthrough
from .search.hub import SearchHub
from .tech_chain.agents import (
    ChainRiskAgent, ChainSentimentAgent, CycleLinkageAgent, TechChainMonitorAgent,
)
from .tech_chain.fusion import TechChainFusionAgent, signals_to_bonus_map
from .tech_chain.universe import CHAIN_TOPICS, SECTOR_TOPICS
from .universe import load_universe

logger = logging.getLogger(__name__)


_LINEAGE: list[tuple[str, str]] = []   # 本轮行情来源血缘（对象, 实际源），每轮清空
# v6.0 provider 健康度（白皮书§12 披露文化的延伸）：本轮内按源记录调用
# 成功/失败/时延；连续失败 ≥2 次的源在本轮降级顺序中沉底；摘要写入报告。
_HEALTH: dict[str, dict] = {}


@dataclass
class _Diagnostics:
    lineage: list = field(default_factory=list)
    health: dict = field(default_factory=dict)
    as_of: str | None = None


_RUN_DIAGNOSTICS: ContextVar[_Diagnostics | None] = ContextVar("tiger_run_diagnostics", default=None)


def _lineage():
    run = _RUN_DIAGNOSTICS.get()
    return run.lineage if run is not None else _LINEAGE


def _health():
    run = _RUN_DIAGNOSTICS.get()
    return run.health if run is not None else _HEALTH


def _isolated_run(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        context_handle = _RUN_DIAGNOSTICS.set(_Diagnostics())
        try:
            return function(*args, **kwargs)
        finally:
            _RUN_DIAGNOSTICS.reset(context_handle)
    return wrapped


def _visible_data(out):
    if isinstance(out, (pd.DataFrame, pd.Series)):
        if out.empty:
            raise RuntimeError("Empty market observations")
        if not isinstance(out.index, pd.DatetimeIndex) or not out.index.is_unique or not out.index.is_monotonic_increasing:
            raise ValueError("Market dates must be unique and strictly increasing")
    run = _RUN_DIAGNOSTICS.get()
    if (run is not None and run.as_of and isinstance(out, (pd.DataFrame, pd.Series))
            and isinstance(out.index, pd.DatetimeIndex)):
        out = out[out.index.date <= datetime.strptime(run.as_of, "%Y-%m-%d").date()].copy()
        if out.empty:
            raise RuntimeError("No market observations at or before this run's date")
    if isinstance(out, pd.DataFrame) and set(("Open", "High", "Low", "Close")).issubset(out.columns):
        prices = out[["Open", "High", "Low", "Close"]].astype(float)
        observed = prices.dropna()
        if (np.isinf(prices.to_numpy()).any() or (observed <= 0).any().any()
                or (observed["Low"] > observed[["Open", "Close"]].min(axis=1)).any()
                or (observed["High"] < observed[["Open", "Close"]].max(axis=1)).any()):
            raise ValueError("OHLC prices must be finite, positive and inside the daily envelope")
        if "Volume" in out.columns:
            volume = out["Volume"].astype(float)
            if np.isinf(volume).any() or (volume.dropna() < 0).any():
                raise ValueError("Volume must be finite and non-negative")
    elif isinstance(out, pd.Series):
        if np.isinf(out.astype(float)).any():
            raise ValueError("Macro observations must be finite")
    return out


def _health_record(name: str, ok: bool, elapsed: float) -> None:
    h = _health().setdefault(name, {"ok": 0, "fail": 0, "secs": 0.0})
    h["ok" if ok else "fail"] += 1
    h["secs"] = round(h["secs"] + elapsed, 2)


def _health_score(name: str) -> float:
    """健康分：成功率优先，连续失败惩罚（用于降级顺序自适应排序）。"""
    h = _health().get(name)
    if not h:
        return 0.5
    total = h["ok"] + h["fail"]
    return (h["ok"] + 1) / (total + 2) - min(h["fail"], 4) * 0.1


def _channel_chain() -> list:
    """降级通道群（v6.2 七环制 + v3 官方宏观源），按本轮健康分自适应排序：
    official(FRED/CBOE 官方宏观基准：利率/波动率)
    → tencent(腾讯) / sina(新浪) / eastmoney(东财) 中国免费源（无需 key）
    → agentgw(yahoo_finance 服务端) → ifind_gw(同花顺服务端) → tiingo(key)。
    完整降级链：yahoo → stooq → 本通道群。"""
    chain: list = []
    for cls_path in (("official", "OfficialMacroProvider"),
                     ("tencent", "TencentProvider"),
                     ("sina", "SinaProvider"),
                     ("eastmoney", "EastMoneyProvider"),
                     ("agentgw", "AgentGwProvider"),
                     ("ifind_gw", "IfindGwProvider"),
                     ("tiingo", "TiingoProvider")):
        try:
            mod = __import__(f"trading_system.providers.{cls_path[0]}",
                             fromlist=[cls_path[1]])
            cls = getattr(mod, cls_path[1])
            if cls.available():
                chain.append(cls())
        except Exception:
            continue
    chain.sort(key=lambda p: _health_score(p.name), reverse=True)
    return chain


def _series_stale(out, max_lag_days: int = 5) -> bool:
    """宏观序列陈旧判定（v3 修复：tencent VIX 曾返回 4 个月前的陈旧序列）。
    仅对带 DatetimeIndex 的 pd.Series 生效；末根距今 >max_lag_days 个自然日
    视为陈旧（容忍官方源自然滞后：FRED ~1 个交易日、周末/假期顺延，
    5 个自然日足以覆盖，远小于"陈旧数月"的故障模式）。"""
    try:
        import pandas as pd
        if not isinstance(out, pd.Series) or out.empty:
            return False
        idx = out.index
        if not isinstance(idx, pd.DatetimeIndex):
            return False
        last = idx.max().to_pydatetime().date()
        run = _RUN_DIAGNOSTICS.get()
        reference = (datetime.strptime(run.as_of, "%Y-%m-%d").date()
                     if run is not None and run.as_of else datetime.now().date())
        lag = (reference - last).days
        return lag < 0 or lag > max_lag_days
    except Exception:
        return False


def _supports(provider, method: str) -> bool:
    """能力感知（v3 修复）：provider 声明 CAPABILITIES 时，不含该方法直接跳过，
    避免"必然失败的调用"污染健康分与降级时延（如 official 不承接个股 OHLCV）。"""
    caps = getattr(provider, "CAPABILITIES", None)
    if caps is None:
        # 未声明时按基类接口推断：rate_yield_for/vol_index_for 等非 OHLCV 方法
        # 对所有 provider 视为支持（基类有默认实现）；未声明者不做裁剪。
        return True
    return method in caps


def _single_with_fallback(provider, method: str, *args, **kwargs):
    """单只/单序列拉取：yahoo → stooq → 服务端通道群（agentgw/ifind/tiingo）。

    v3 修复两条降级协议缺陷（2026-08-30 真实运行暴露）：
      1) None 视同失败：可选序列（如 vix9d）失败时部分 provider 返回 None
         而不抛异常，旧协议把 None 当成功 → 后续真实可用源永不被尝试；
      2) 序列级陈旧检测：返回的宏观序列末根过旧时视同失败继续降级。
    """
    what = f"{method}({args[0] if args else ''})"
    t0 = time.time()
    try:
        out = _visible_data(getattr(provider, method)(*args, **kwargs))
        if out is None:
            raise RuntimeError(f"{provider.name}.{method} 返回 None（视同失败，继续降级）")
        if _series_stale(out):
            raise RuntimeError(f"{provider.name}.{method} 序列陈旧（末根滞后超限，视同失败）")
        _lineage().append((what, provider.name))
        _health_record(provider.name, True, time.time() - t0)
        return out
    except Exception as e:
        _health_record(provider.name, False, time.time() - t0)
        from .providers.base import is_synthetic
        if is_synthetic(provider):
            raise
        chain: list = []
        if provider.name == "yahoo":
            from .providers.stooq import StooqProvider
            chain.append(StooqProvider())
        chain.extend(p for p in _channel_chain()
                     if p.name != provider.name and _supports(p, method))
        last = e
        for alt in chain:
            t1 = time.time()
            try:
                logger.warning("%s.%s 失败（%s），降级 %s", provider.name, method, last, alt.name)
                out = _visible_data(getattr(alt, method)(*args, **kwargs))
                if out is None:
                    raise RuntimeError(f"{alt.name}.{method} 返回 None（视同失败，继续降级）")
                if _series_stale(out):
                    raise RuntimeError(f"{alt.name}.{method} 序列陈旧（末根滞后超限，视同失败）")
                _lineage().append((what, alt.name))
                _health_record(alt.name, True, time.time() - t1)
                return out
            except Exception as e2:
                _health_record(alt.name, False, time.time() - t1)
                last = e2
        raise last


def _quote_stale(q: dict, max_lag_days: int = 3) -> bool:
    """eod_close 类报价的陈旧判定：报价日期落后最近交易日 >max_lag_days 视为陈旧
    （停牌/源不完整的票，上周的收盘价绝不是"当前价格"）。"""
    if q.get("kind") == "realtime":
        return False
    try:
        from .calendar import prev_trading_day
        qdate = datetime.strptime(str(q.get("ts", ""))[:10], "%Y-%m-%d").date()
        return (prev_trading_day(datetime.now().date()) - qdate).days > max_lag_days
    except Exception:
        return False


def quote_with_fallback(provider, ticker: str) -> dict | None:
    """当前报价降级链：provider → stooq → 服务端通道群（agentgw/ifind/tiingo）。

    与 _single_with_fallback 的区别：quote 失败时各 provider 返回 None 而非
    抛异常，旧链路会把 None 当成功、静默拿不到价（盘中触发器因此零警报）。
    本函数把 None 视为失败继续降级，并把报价来源与时效（kind:
    realtime / realtime_delayed / eod_close）记入来源血缘如实披露。
    v6.1：eod_close 报价日期落后最近交易日 >3 天判定【陈旧】——继续降级找
    新鲜报价；全链皆陈旧时返回最后一个并标注 stale=True（触发器将弃用，
    用上周的价格触发止损/入场比不报警更危险）。
    """
    from .providers.base import is_synthetic
    def sources():
        yield provider
        if is_synthetic(provider):
            return
        if provider.name != "stooq":
            from .providers.stooq import StooqProvider
            yield StooqProvider()
        yield from (src for src in _channel_chain() if src.name != provider.name
                    and not is_synthetic(src))
    stale_q: dict | None = None
    for src in sources():
        try:
            q = src.quote(ticker)
        except Exception as e:
            logger.warning("%s.quote(%s) 异常（%s），降级下一环", src.name, ticker, e)
            continue
        if (not isinstance(q, dict) or isinstance(q.get("price"), bool)
                or not isinstance(q.get("price"), (int, float))
                or not math.isfinite(q["price"]) or q["price"] <= 0):
            logger.warning("%s.quote(%s) 无数据，降级下一环", src.name, ticker)
            continue
        if _quote_stale(q):
            logger.warning("%s.quote(%s) 报价陈旧（%s），尝试下一环",
                           src.name, ticker, q.get("ts"))
            q["stale"] = True
            stale_q = stale_q or q
            continue
        _lineage().append((f"quote({ticker})[{q.get('kind', '?')}]", src.name))
        return q
    if stale_q is not None:
        _lineage().append((f"quote({ticker})[stale]", "STALE"))
        return stale_q
    return None


def _batch_with_fallback(provider, tickers: list[str], days: int,
                         label: str = "batch") -> dict[str, pd.DataFrame]:
    """Merge successful real sources; one failed batch does not suppress fallback."""
    from .providers.base import is_synthetic
    data: dict[str, pd.DataFrame] = {}
    def sources():
        yield provider
        if is_synthetic(provider):
            return
        if provider.name != "stooq":
            from .providers.stooq import StooqProvider
            yield StooqProvider()
        yield from (src for src in _channel_chain() if src.name != provider.name
                    and _supports(src, "ohlcv_batch") and not is_synthetic(src))
    enough = max(3, len(tickers) // 3)
    for src in sources():
        missing = [ticker for ticker in tickers if ticker not in data]
        if not missing:
            break
        t0 = time.time()
        try:
            batch = src.ohlcv_batch(missing, days=days)
            if not isinstance(batch, dict):
                raise ValueError("Batch OHLCV payload must be a dictionary")
            got = {}
            for ticker, frame in batch.items():
                if ticker not in missing or frame is None or frame.empty:
                    continue
                try:
                    got[ticker] = _visible_data(frame)
                except (ValueError, RuntimeError) as exc:
                    logger.warning("%s %s OHLCV rejected: %s", src.name, ticker, exc)
            _health_record(src.name, bool(got), time.time() - t0)
            data.update(got)
            if got:
                _lineage().append((f"{label}+{len(got)}[{len(data)}/{len(tickers)}]", src.name))
            if len(data) >= enough:
                break
        except Exception as exc:
            _health_record(src.name, False, time.time() - t0)
            logger.warning("%s batch failed; proceeding to next source: %s", src.name, exc)
    return data


@_isolated_run
def run_pipeline(
    provider_name: str | None = None,
    universe_mode: str = "extended",
    universe_file: str | None = None,
    top_n: int = config.SCAN_TOP_N,
    max_picks: int = config.MAX_PICKS_DEFAULT,
    account_usd: float = 100_000,
    trade_date: str | None = None,
    use_tuned: bool = False,
    market: str = "us",
    llm_client: LLMClient | None = None,
    tuned_path: str = "tuned_params.json",
    run_state_dir: str | None = None,
    ledger_dir: str | None = None,
    risk_limits: dict | None = None,
) -> PipelineResult:
    started = time.time()
    if isinstance(account_usd, bool) or not isinstance(account_usd, (int, float)) or not math.isfinite(account_usd) or account_usd <= 0:
        raise ValueError("Account equity must be finite and positive")
    gate_params = tighten_risk_limits(GateParams(max_picks=max_picks), risk_limits)
    from .state import run_path, run_directory
    run_cache = str(run_path(run_state_dir, "cache")) if run_state_dir is not None else None
    ledger_root = run_directory(ledger_dir) if ledger_dir is not None else None
    _lineage().clear()
    _health().clear()
    provider = get_provider(provider_name)
    # S4 多市场（D1 框架不变、输入替换）：市场规格解析——日历/基准组/板块代理/
    # 扫描过滤/合规规则/轻仓毕业门槛全部来自 spec（参数 single source = config）。
    from .markets import get_market
    spec = get_market(market)
    trade_date = trade_date or spec.prev_trading_day(datetime.now(ZoneInfo(spec.timezone)).date()).isoformat()
    datetime.strptime(trade_date, "%Y-%m-%d")
    _RUN_DIAGNOSTICS.get().as_of = trade_date
    bmk = spec.benchmarks
    benchmark_labels = {"index": bmk.get("index_label", bmk["index"]),
                        "rate": bmk.get("rate_label", bmk["rate"]),
                        "vol": bmk.get("vol_label", bmk["vol"])}
    ah_enabled = bool(config.AH_TOPICS_ENABLED
                      or spec.market_id in config.AH_AUTO_MARKETS)
    market_grad = spec.graduation()
    universe_source = universe_mode
    if spec.market_id == "us":
        if universe_mode == "full":
            from .universe import load_full_universe
            universe, universe_source = load_full_universe(cache_dir=run_cache)   # nasdaqtrader/cache/fallback
        else:
            universe = load_universe(universe_mode, universe_file, cache_dir=run_cache)
    else:
        # CN/HK：full=东财全量清单（两级拉取复刻 US）；其他=内嵌池/文件
        from .universe import load_market_universe
        universe, universe_source = load_market_universe(
            spec.market_id, universe_mode, universe_file, cache_dir=run_cache)
    tracer = ExecutionTracer(run_id=trade_date)
    from .providers.base import is_synthetic
    synthetic_source = is_synthetic(provider)
    demo_mode = (provider_name == "demo") or synthetic_source

    # ================= 搜索与清洗（系统的命脉环节） =================
    llm = llm_client if llm_client is not None else default_client()

    with tracer.step("search.collect"):
        hub = SearchHub(demo=demo_mode, use_disk_cache=False)
        # 主题集注册（SearchHub 同款机制）：科技六子链 + 全领域八主题——每个板块
        # 都要有自己的情报输入，否则非科技板块的 LLM 叙事维度被架空（情报偏科即评分偏科）。
        # S3：AH（A股/港股）主题默认关闭（config.AH_TOPICS_ENABLED，S4 多市场启用），
        # 关闭时整体缺席、不产生任何网络调用，开关状态写入日报披露。
        hub.register_topic_set("chains", CHAIN_TOPICS)
        hub.register_topic_set("sectors", SECTOR_TOPICS)
        hub.register_topic_set("ah", config.AH_TOPICS, enabled=ah_enabled)
        raw_docs = hub.gather_topic_sets(limit_per_query=4, deep=True)
        all_topics = hub.active_topics()
        logger.info("search.collect: %d 篇原始文档（%d 主题；AH主题=%s）",
                    len(raw_docs), len(all_topics),
                    "启用" if ah_enabled else "关闭")

    with tracer.step("clean.rule_base"):
        base_docs = rule_base_clean(raw_docs)
        logger.info("clean.rule_base: %d→%d（去重/格式校验）", len(raw_docs), len(base_docs))

    with tracer.step("clean.llm_semantic"):
        cleaned = unwrap_cleaned(llm_semantic_clean(base_docs, llm, tracer))
        degraded_n = sum(1 for c in cleaned if c.degraded)
        logger.info("clean.llm_semantic: %d 篇（语义缺失 %d）", len(cleaned), degraded_n)

    # S3 数据层：交叉验证（规则层调度，D9 不引入 LLM）。关键事件类文档需
    # ≥2 个不同源（且至少一个 ≤T2）佐证；未达标标记 corroborated=False——
    # 不删除（透传纪律），但叙事/舆情打分输入只使用 corroborated=True 的集合；
    # 被降级文档在决策侧仅作背景参考（tech.risk / decision.debate 仍可见全集）。
    with tracer.step("clean.cross_validate"):
        from .search.credibility import CrossValidator, scoring_docs
        cv_stats = CrossValidator().validate(cleaned)
        score_docs = scoring_docs(cleaned)
        logger.info("clean.cross_validate: 总 %d 篇｜关键事件 %d｜通过 %d｜降级 %d"
                    "（LLM标注缺失 %d｜缺发布时间 %d）",
                    cv_stats["total"], cv_stats["key_event_docs"],
                    cv_stats["corroborated"], cv_stats["downgraded"],
                    cv_stats["llm_missing"], cv_stats["missing_published_at"])

    # WFA 调优参数：默认【不加载】——tuned_params.json 属于上一轮生产残留，
    # 零基线纪律下每轮从零开始；仅当调用方显式 use_tuned=True 时才覆盖默认闸门。
    tuned_note = ""
    if use_tuned:
        from .backtest import apply_tuned_params
        applied = apply_tuned_params(tuned_path, as_of=trade_date)
        if applied:
            gate_params = tighten_risk_limits(replace(gate_params, **applied),
                                              risk_limits_for(gate_params))
            tuned_note = f"本轮使用已批准 WFA 参数（显式 --use-tuned）: {applied}"
            logger.info(tuned_note)
    logger.info("=== Pipeline 启动 market=%s provider=%s universe=%s(%d) ===",
                spec.market_id, provider.name, universe_mode, len(universe))

    # ---------- 数据真实性与时效性闸门（v6.1，白皮书§12"地基不牢评分皆是沙上之塔"）----------
    # S4：日历/指数名/板块清单全部按市场规格参数化（US 行为与现状完全一致）

    def _freshness_gate(spy_df, tnx_s, vix_s, etfs: dict, stocks: dict) -> dict:
        """硬依赖充足性 + 末根日期新鲜度 + 陈旧标的剔除。

        - 指数行数不足 → 诚实失败（短数据照样能"算出分"，但口径全错）；
        - 指数末根日期落后最近交易日 >3 天 → 诚实失败（基准数据陈旧）；
        - 利率/波动率历史不足：US → 诚实失败（现状行为）；CN/HK → 记缺失
          走"剔除再归一化"（D2，不可得维度不编造）；
        - 板块覆盖率 < 2/3：US → 诚实失败；CN/HK → 按缺失披露继续（D2）；
        - 个股末根日期落后指数末根 >5 天 → 判定停牌/源不完整，【剔除】并披露
          （绝不把上一周的价格当"当前价格"参与评分）。
        """
        report: dict = {}
        idx_name = bmk["index"]
        if len(spy_df) < 260:
            raise RuntimeError(
                f"[{spec.market_id}] {idx_name} 历史不足（{len(spy_df)} < 260 行）："
                "SMA200/252 日分位口径不成立，"
                "宁可中止也不产出带污点的评分（白皮书§1.3 诚实失败）")
        tnx_ok = tnx_s is not None and len(tnx_s) >= 100
        vix_ok = vix_s is not None and len(vix_s) >= 30
        if spec.benchmark_hard_fail:
            if not tnx_ok or not vix_ok:
                raise RuntimeError(
                    f"[{spec.market_id}] {bmk['rate']}/{bmk['vol']} 历史不足"
                    f"（{0 if tnx_s is None else len(tnx_s)}/"
                    f"{0 if vix_s is None else len(vix_s)} 行），MRS 宏观/情绪维口径不成立")
        else:
            missing_bmk = [lbl for lbl, ok in ((bmk["rate"], tnx_ok), (bmk["vol"], vix_ok))
                           if not ok]
            if missing_bmk:
                report["benchmark_missing"] = missing_bmk   # 缺失维披露（再归一化）
        expect = spec.prev_trading_day(trade_date)
        spy_last = spy_df.index[-1].date()
        lag = (expect - spy_last).days
        if lag < 0:
            raise RuntimeError(f"[{spec.market_id}] 基准包含运行日期之后的未来行情，拒绝评分")
        report["benchmark_last_bar"] = str(spy_last)
        report["benchmark_lag_days"] = lag
        if lag > 3:
            raise RuntimeError(
                f"[{spec.market_id}] 基准数据陈旧：{idx_name} 末根 {spy_last}，"
                f"最近交易日 {expect}（滞后 {lag} 天）——"
                "用陈旧数据产出'今日'评分是数据造假，立即中止（白皮书§12.3）")
        if lag >= 1:
            logger.warning("数据时效披露：%s 末根 %s（最近交易日 %s，滞后 %d 天）",
                           idx_name, spy_last, expect, lag)
        sector_list = spec.sector_symbols
        need_etf = (len(sector_list) * 2 + 2) // 3     # ≥2/3
        if len(etfs) < need_etf:
            missing = [e for e in sector_list if e not in etfs]
            if spec.sector_hard_fail:
                raise RuntimeError(
                    f"[{spec.market_id}] 板块 ETF 覆盖率不足（{len(etfs)}/{len(sector_list)} < 2/3，"
                    f"缺 {missing}）：SHS 主线判定地基不全，诚实失败")
            report["sector_breadth_missing"] = (
                f"板块广度数据不足（{len(etfs)}/{len(sector_list)}，缺 {missing}）→ "
                "SHS 板块维度按缺失披露（D2：宁可缺失不可编造）")
            logger.warning("[%s] %s", spec.market_id, report["sector_breadth_missing"])
        stale = [t for t, df in stocks.items()
                 if df is not None and len(df)
                 and (spy_last - df.index[-1].date()).days > 5]
        for t in stale:
            del stocks[t]
        if stale:
            logger.warning("剔除 %d 只陈旧/停牌标的（末根滞后 %s 超 5 天）: %s%s",
                           len(stale), idx_name, stale[:10], "..." if len(stale) > 10 else "")
        report["stale_removed"] = stale
        report["etf_coverage"] = f"{len(etfs)}/{len(sector_list)}"
        return report

    # ---------- 数据准备 ----------
    days = 420
    market_data: dict = {"benchmark_labels": benchmark_labels}
    with tracer.step("data.prepare"):
        idx_sym = bmk["index"]
        try:
            spy = _single_with_fallback(provider, "ohlcv", idx_sym, days=days)
        except Exception as exc:
            # D2 诚实失败：该市场基准（指数）全断 → 当日中止，其余市场不受影响
            raise RuntimeError(
                f"[{spec.market_id}] {idx_sym} 基准数据获取失败（含降级源），"
                f"该市场当日诚实失败，MRS 无法计算: {exc}") from exc
        market_data["spy"] = spy
        # 利率基准：US 硬依赖（现状行为）；CN/HK 缺失 → 记 None 走再归一化
        try:
            market_data["tnx"] = _single_with_fallback(
                provider, "rate_yield_for", bmk["rate"], days=days)
        except Exception as exc:
            if spec.benchmark_hard_fail:
                raise RuntimeError(
                    f"[{spec.market_id}] {bmk['rate']} 数据获取失败（含降级源），"
                    f"MRS 无法计算: {exc}") from exc
            logger.warning("[%s] 利率基准 %s 不可得（%s）→ 宏观维缺失再归一化",
                           spec.market_id, bmk["rate"], exc)
            market_data["tnx"] = None
        # 波动率基准：同上
        try:
            market_data["vix"] = _single_with_fallback(
                provider, "vol_index_for", bmk["vol"], days=days)
        except Exception as exc:
            if spec.benchmark_hard_fail:
                raise RuntimeError(
                    f"[{spec.market_id}] {bmk['vol']} 数据获取失败（含降级源），"
                    f"MRS 无法计算: {exc}") from exc
            logger.warning("[%s] 波动率基准 %s 不可得（%s）→ 情绪维缺失再归一化",
                           spec.market_id, bmk["vol"], exc)
            market_data["vix"] = None
        try:
            market_data["vix9d"] = (_single_with_fallback(
                provider, "vol_index_for", bmk["vol_short"], days=days)
                if bmk.get("vol_short") else None)
        except Exception:
            market_data["vix9d"] = None

        sector_etfs = _batch_with_fallback(provider, spec.sector_symbols, days)
        market_data["sector_etfs"] = sector_etfs

        # full 模式两级拉取：先用 30 日短历史做流动性预筛，再对幸存者拉全历史
        prefilter_note = ""
        scan_filt = spec.scan_filters()
        if len(universe) > config.FULL_PREFILTER_THRESHOLD:
            logger.info("池规模 %d > %d，启动流动性预筛（%d 日短历史）",
                        len(universe), config.FULL_PREFILTER_THRESHOLD,
                        config.FULL_PREFILTER_DAYS)
            light = _batch_with_fallback(provider, universe, config.FULL_PREFILTER_DAYS)
            survivors: list[tuple[str, float]] = []
            for t, df in light.items():
                if len(df) < 15:
                    continue
                px = float(df["Close"].iloc[-1])
                adv = float((df["Close"] * df["Volume"]).tail(20).mean())
                if px >= scan_filt["min_price"] and adv >= scan_filt["min_adv"]:
                    survivors.append((t, adv))
            survivors.sort(key=lambda x: x[1], reverse=True)
            kept = survivors[: config.FULL_HEAVY_CAP]
            prefilter_note = (f"预筛 {len(universe)}→{len(light)}→流动性通过 "
                              f"{len(survivors)}→重量级拉取 {len(kept)}")
            logger.info(prefilter_note)
            universe = [t for t, _ in kept]

        # 全市场股票数据（扫描 + 产业链 + 广度共用一份）
        # S4：US 产业链为美股映射，仅 US 市场并入拉取；CN/HK 链覆盖率如实披露为 0
        chain_tickers: set[str] = set()
        if spec.market_id == "us":
            from .chains import CHAINS
            for c in CHAINS.values():
                for link in ("upstream", "midstream", "downstream"):
                    chain_tickers.update(c[link]["tickers"])
        all_tickers = sorted(set(universe) | chain_tickers)
        stock_data = _batch_with_fallback(provider, all_tickers, days)
        market_data["stock_ohlcv"] = stock_data
        # v6.1：真实性/时效性闸门——充足性、新鲜度、陈旧标的剔除（诚实失败优于脏数据）
        freshness = _freshness_gate(spy, market_data["tnx"], market_data["vix"],
                                    sector_etfs, stock_data)
        market_data["freshness"] = freshness
        market_data["universe_closes"] = {t: df["Close"] for t, df in stock_data.items()}
        logger.info("数据准备完成: 股票 %d/%d, 板块ETF %d",
                    len(stock_data), len(all_tickers), len(sector_etfs))

    # ================= 科技股产业链专项子集群 =================
    # （放在数据准备之后：monitor 复用已下载行情，仅全球联动标的单独拉取）
    with tracer.step("tech.monitor"):
        tech_monitor = TechChainMonitorAgent(provider).execute(tracer, prefetched=stock_data,
                                                                as_of=trade_date)
    with tracer.step("tech.cycle_linkage"):
        tech_linkage = CycleLinkageAgent(provider).execute(tracer, as_of=trade_date)
    with tracer.step("tech.sentiment"):
        tech_sentiment = ChainSentimentAgent(llm).execute(score_docs, tracer)
    with tracer.step("tech.risk"):
        tech_alerts = ChainRiskAgent(llm).execute(cleaned, tracer)
    with tracer.step("tech.fusion"):
        tech_signals = TechChainFusionAgent().execute(
            tech_monitor, tech_linkage, tech_sentiment, tech_alerts, tracer)
        logger.info("tech.fusion: %s",
                    [(s.chain_id, s.prosperity, s.risk_level, s.bonus_hint) for s in tech_signals])

    # ================= SHS 叙事维度（LLM 化） =================
    narrative_llm: dict = {}
    with tracer.step("sector.narrative"):
        narr_res = NarrativeAgent(llm).execute(score_docs, spec.sector_symbols, tracer)
        if not isinstance(narr_res, Passthrough):
            narrative_llm = narr_res

    # v6.3：链映射覆盖率披露——无链映射的候选在 L4 只能走轻仓通道
    # （SHS 钉 5.0），覆盖率本身就是公平性指标，必须显性化
    from .chains import TICKER_TO_CHAIN as _T2C
    _mapped = sum(1 for t in all_tickers if t in _T2C)
    chain_coverage = f"{_mapped}/{len(all_tickers)}（{_mapped / max(len(all_tickers), 1):.0%}）"
    logger.info("产业链映射覆盖率: %s", chain_coverage)

    from .options_metrics import OptionsHistoryStore
    options_store = (OptionsHistoryStore(run_path(ledger_root, "options_hist"))
                     if ledger_root is not None else None)
    context: dict = {
        "options_store": options_store,
        "gate_params": gate_params,
        "risk_limits": risk_limits_for(gate_params),
        "market_data": market_data,
        "trade_date": trade_date,
        "chain_coverage": chain_coverage,
        # S4 多市场：市场规格 / 扫描过滤 / 轻仓毕业门槛（agents 只读）
        "market_spec": spec,
        "scan_filters": scan_filt,
        "market_grad": market_grad,
        # v6.0：事件日历（白皮书§11 事件风险管理的机器执行）
        "event_calendar": EventCalendar(),
        # 科技链子集群标准化汇入（核心因子之一）
        "tech_signals": [s.to_dict() for s in tech_signals],
        "tech_bonus_map": signals_to_bonus_map(tech_signals),
        "tech_signal_map": {s.main_chain: s for s in tech_signals},
        # SHS 叙事维度 LLM 注入（空 dict = 透传，维度剔除再归一化）
        "narrative_llm": narrative_llm,
    }

    # ---------- 六层流水线 ----------
    with tracer.step("layer1.mrs"):
        mrs = MRSAgent(provider).execute(context)
    with tracer.step("layer2.sector"):
        sectors = SectorAgent(provider).execute(context)
    with tracer.step("layer2b.chain"):
        chains = ChainCycleAgent(provider).execute(context)
    with tracer.step("layer0.scan"):
        scanner = UniverseScannerAgent(provider, universe, top_n=top_n)
        candidates = scanner.execute(context)
    with tracer.step("layer3.tss"):
        candidates = TSSAgent(provider).execute(context)
    with tracer.step("layer4.risk"):
        picks = RiskManagerAgent(provider, account_usd=account_usd,
                                 max_picks=max_picks).execute(context)

    # ================= 多空辩论证据层（v6.3 S2，桥水 AIA 辩论制） =================
    # 铁律：辩论在 L4 闸门产出【之后】执行，只读 picks/rationale/watchlist，
    # 绝不回写任何分数与闸门输出；产出仅作交易卡片"第六段"进报告层。
    # 回测路径不经过本 pipeline（无未来函数：历史回放禁止注入 LLM）。
    with tracer.step("decision.debate"):
        from .agents.debate_agent import DebateAgent, select_debate_targets
        debate_raw: dict = {"status": "disabled", "triggered": [], "evidence": {}}
        if config.DEBATE_ENABLED:
            targets = select_debate_targets(
                context.get("picks", []), context.get("pick_rationale", {}),
                context.get("watchlist", []), context.get("action", "HOLD"))
            debate_raw["triggered"] = [t["symbol"] for t in targets]
            if targets:
                res = DebateAgent(llm).execute(targets, cleaned, tracer)
                if isinstance(res, Passthrough):
                    debate_raw["status"] = "passthrough"   # 报告标注"本轮无辩论证据"
                else:
                    debate_raw["status"] = "ok"
                    debate_raw["evidence"] = {s: e.to_dict() for s, e in res.items()}
            else:
                debate_raw["status"] = "no_target"         # 灰区外，本轮不辩论
        context["debate"] = debate_raw
        logger.info("decision.debate: status=%s triggered=%s",
                    debate_raw["status"], debate_raw["triggered"])

    # ================= 统计校准层（v6.3 S2，迭代层·只进报告不改闸门） =================
    # 会计账白名单（与 journal 同级）：只读已结算台账，输出仅进报告披露，
    # 绝不进入决策输入（代码层隔离，见 calibration.py 头注）。
    with tracer.step("iteration.calibration"):
        from .calibration import CalibrationLayer
        try:
            calibration = (CalibrationLayer(
                journal_path=str(run_path(ledger_root, "journal.json")),
                samples_path=str(run_path(ledger_root, "calibration_samples.json")))
                if ledger_root is not None else CalibrationLayer())
            calibration_summary = calibration.run()
            calibration_summary["samples_path"] = str(calibration.samples_path)
            calibration_summary["journal_path"] = str(calibration.journal_path)
        except Exception as exc:
            # 注册表约定：本环节不可透传——失败则跳过披露并记录（不阻塞主链路）
            logger.warning("iteration.calibration 失败，跳过披露并记录: %s", exc)
            calibration_summary = {"status": "skipped", "reason": str(exc)}
        context["calibration"] = calibration_summary
        logger.info("iteration.calibration: status=%s n=%s",
                    calibration_summary.get("status"), calibration_summary.get("n"))

    with tracer.step("report.emit"):
        notes = context.get("notes", [])
        if tuned_note:
            notes = notes + [tuned_note]
        result = PipelineResult(
            trade_date=trade_date,
            provider=provider.name,
            mrs=context.get("mrs"),
            sectors=context.get("sectors", []),
            chains=context.get("chains", []),
            watchlist=context.get("watchlist", []),
            picks=context.get("picks", []),
            action=context.get("action", "HOLD"),
            market_view=context.get("market_view", ""),
            notes=notes,
            raw={
                "scan_stats": context.get("scan_stats", {}),
                "elapsed_s": round(time.time() - started, 1),
                "universe_mode": universe_mode,
                "universe_source": universe_source,
                "account_usd": account_usd,
                "account_currency": spec.currency,
                "data_synthetic": synthetic_source,
                "gross_cap": context.get("gross_cap", 0.0),
                "pick_rationale": context.get("pick_rationale", {}),
                "source_lineage": list(_lineage()),
                "provider_health": {name: dict(value) for name, value in _health().items()},
                "gate_params": asdict(context["gate_params"]),
                "risk_limits": dict(context["risk_limits"]),
                "freshness": market_data.get("freshness", {}),
                "chain_coverage": context.get("chain_coverage", ""),
                "data_coverage": f"{len(stock_data)}/{len(all_tickers)}",
                "prefilter": prefilter_note,
                "tech_signals": context.get("tech_signals", []),
                "docs_collected": len(raw_docs),
                "docs_cleaned": len(cleaned),
                "docs_semantic_degraded": degraded_n,
                # v6.3 S3：交叉验证统计与 AH 主题开关（数据层披露）
                "cross_validation": cv_stats,
                "ah_topics_enabled": ah_enabled,
                # v6.3 S4：市场标识 / 合规校验记录 / 轻仓毕业状态（日报披露）
                "market": spec.to_dict(),
                "compliance": context.get("compliance", []),
                "market_grad": market_grad,
                # v6.3 S2：辩论证据与校准摘要（只读证据/披露，不含决策输入）
                "debate": context.get("debate", {}),
                "calibration": context.get("calibration", {}),
            },
        )
    # 红线 1：全链路环节完整性强制校验（缺环节 = 系统性事故，立即停止）
    tracer.assert_complete()
    # S6：review.daily 属延迟环节（依赖 main.daily 尾部的 journal 落账/结算），
    # 记 deferred 占位；main.daily 由 review.chief 实际执行后改写为 executed。
    tracer.mark_deferred("review.daily",
                         "复盘纪要由 main.daily 尾部 review.chief 生成并补账")
    result.raw["redline"] = tracer.summary()
    logger.info("=== Pipeline 完成 %.1fs action=%s picks=%d ===",
                time.time() - started, result.action, len(result.picks))
    return result
