"""回测引擎 — 无未来函数的信号回放 + 交易仿真 + 真实滚动 WFA。

三层结构：
  1. collect_day_frames()  重型一遍（与闸门参数无关，只算一次）：
     逐日驱动真实 MRS/SHS/ICS Agent（输入为 ≤t 的切片）+
     面板向量化全市场扫描 + 真实 TSS 评分，产出 DayFrame 序列。
  2. run_backtest()        轻型回放：按 GateParams 闸门过滤 DayFrame →
     交易仿真（次日开盘入场 / 止损 / 2R 盈利保护 / 时间止损）→
     交易层 + 组合层指标（胜率 / 期望值 / 利润因子 / 夏普 / 最大回撤）。
  3. run_wfa()             真实滚动 Walk-Forward：折内选参、折外验证，
     汇总样本外表现 + DSR 多重检验校正，产出参数推荐。

无未来函数保证：
  - 第 t 日全部评分只使用 ≤ t 的数据（Agent 输入为 iloc[i-W:i+1] 切片，
    面板指标全部基于 rolling/shift，无任何后视）；
  - 入场在信号日【次日开盘】，止损/保护/时间止损逐日向后仿真；
  - 止损优先于盈利保护判定（保守假设，避免日内路径乐观偏差）。

期权维度说明：历史期权链不可回溯，回测中期权组件走中性 5 分
（与实盘初期一致），实盘期权分位通过 journal 逐日积累后生效。
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from . import config
from .parameters import (GateParams, risk_limits_for, tighten_risk_limits,
                         validate_tuned_params)
from .agents import ChainCycleAgent, MRSAgent, SectorAgent, TSSAgent
from .agents.chain_cycle_agent import chain_bonus
from .agents.risk_manager_agent import RiskManagerAgent
from .chains import CHAINS, chain_of
from .stats import (
    annualized_sharpe, deflated_sharpe_ratio, kurtosis, max_drawdown, skewness,
)

logger = logging.getLogger(__name__)

_SLICE = 310          # 喂给 Agent 的切片长度（≥252 最长窗口，保真）
_MIN_HISTORY = int(config.SCAN_MIN_HISTORY_DAYS * 0.6)   # 与扫描器一致 156


# ============================================================
# 参数与帧数据结构
# ============================================================

@dataclass
class CandidateSnap:
    ticker: str
    tss_raw: float            # 加成前 TSS
    bonus: float              # 产业链乘性加成
    c_liq: float
    sector_etf: str
    chain_id: str
    chain_hot: bool
    template: str
    entry_ref: float
    stop: float
    atr_pct: float = 0.0


@dataclass
class SectorSnap:
    etf: str
    shs: float
    breadth: float


@dataclass
class DayFrame:
    date: pd.Timestamp
    mrs_star: float
    regime: str
    sectors: list[SectorSnap] = field(default_factory=list)
    candidates: list[CandidateSnap] = field(default_factory=list)
    gross_cap: float | None = None


# ============================================================
# 期权屏蔽包装（历史期权不可得 → 诚实中性降级）
# ============================================================

class _BtProvider:
    """回测包装：屏蔽期权/微观实时接口，其余透传。"""

    def __init__(self, base):
        self._base = base
        self.name = f"{base.name}-bt"

    def __getattr__(self, k):
        return getattr(self._base, k)

    def options_chain_snapshot(self, ticker):
        return None

    def gex_billions(self):
        return None

    def zero_dte_share(self):
        return None


# ============================================================
# 数据准备 + 面板指标（一次性向量化）
# ============================================================

class _Panel:
    """全池对齐面板 + 扫描/广度所需的全部预计算指标。"""

    def __init__(self, stock_data: dict[str, pd.DataFrame], spy: pd.DataFrame):
        self.dates = spy.index
        tickers = sorted(stock_data.keys())
        self.tickers = tickers
        self.c = stock_data  # 保留原始 df（TSS 切片用）

        def mk(col: str) -> pd.DataFrame:
            df = pd.DataFrame({t: stock_data[t][col] for t in tickers})
            return df.reindex(self.dates)

        self.close = mk("Close")
        self.open = mk("Open")
        self.high = mk("High")
        self.low = mk("Low")
        self.vol = mk("Volume")

        spy_close = spy["Close"].reindex(self.dates).ffill()
        rs = self.close.div(spy_close, axis=0)

        self.sma10 = self.close.rolling(10).mean()
        self.sma20 = self.close.rolling(20).mean()
        self.sma50 = self.close.rolling(50).mean()
        self.sma200 = self.close.rolling(200).mean()

        prev = self.close.shift(1)
        tr = pd.concat([self.high - self.low,
                        (self.high - prev).abs(),
                        (self.low - prev).abs()]).groupby(level=0).max()
        self.atr14 = tr.rolling(14).mean()
        self.atr50 = tr.rolling(50).mean()

        # RS 分位（时间轴滚动 rank，与 percentile_rank 定义一致：严格 <）
        self.q63 = self._rolling_rank(rs / rs.shift(63) - 1.0)
        self.q20 = self._rolling_rank(rs / rs.shift(20) - 1.0)

        self.hhv252 = self.high.rolling(252).max()
        self.adv20 = (self.close * self.vol).rolling(20).mean()

        # 广度面板
        valid201 = self.close.notna().rolling(201).sum() == 201
        above = (self.close > self.sma200) & valid201
        self.breadth200 = (above.sum(axis=1) / valid201.sum(axis=1)
                           .replace(0, np.nan) * 100)

        # AD20 分位（v4.1 修复口径：lag=0 为最近窗口）
        valid273 = self.close.notna().rolling(273).sum() == 273
        r20 = self.close / self.close.shift(20) - 1.0
        up = ((r20 > 0) & valid273).sum(axis=1)
        tot = valid273.sum(axis=1).replace(0, np.nan)
        upfrac = up / tot
        self.ad20_q = upfrac.rolling(233).apply(
            lambda w: float((w < w[-1]).mean()) if len(w) == 233 else np.nan,
            raw=True)
        self.ad20_q = self.ad20_q.where(tot >= 30)

    def _rolling_rank(self, panel: pd.DataFrame, window: int = 252) -> pd.DataFrame:
        """每列在时间窗内最新值的严格小于分位（复刻 percentile_rank）。"""
        vals = panel.values
        t_n, n = vals.shape
        out = np.full((t_n, n), np.nan)
        for i in range(t_n):
            lo = max(0, i - window + 1)
            w = vals[lo: i + 1]
            cur = w[-1]
            valid = ~np.isnan(w)
            cnt = valid.sum(axis=0)
            cur_ok = ~np.isnan(cur)
            less = np.where(valid & (w < cur), 1.0, 0.0).sum(axis=0)
            q = np.where(cur_ok & (cnt >= 20), less / np.where(cnt == 0, 1, cnt), np.nan)
            out[i] = q
        return pd.DataFrame(out, index=panel.index, columns=panel.columns)

    # ---- 扫描器单日截面（复刻 UniverseScannerAgent 逻辑）----

    def scan_day(self, i: int) -> list[dict]:
        """当日全部通过硬过滤的标的（按初排降序）。"""
        c = self.close.iloc[i]
        adv = self.adv20.iloc[i]
        atr_pct = (self.atr14.iloc[i] / c)
        hist = self.close.notna().iloc[max(0, i - _MIN_HISTORY + 1): i + 1].sum()

        eligible = ((c >= config.SCAN_MIN_PRICE)
                    & (adv >= config.SCAN_MIN_ADV_USD)
                    & (atr_pct <= config.SCAN_MAX_ATR_PCT)
                    & (hist >= _MIN_HISTORY))
        idx = np.flatnonzero(eligible.values)
        if len(idx) == 0:
            return []

        cv, s10, s20, s50 = (c.values[idx], self.sma10.iloc[i].values[idx],
                             self.sma20.iloc[i].values[idx], self.sma50.iloc[i].values[idx])
        ma_score = np.select(
            [(cv > s10) & (s10 > s20) & (s20 > s50), (cv > s20) & (s20 > s50), cv > s50],
            [10.0, 8.0, 5.0], default=2.0)
        vc = (self.atr14.iloc[i].values[idx] / self.atr50.iloc[i].values[idx])
        contraction = np.select([vc <= 0.7, vc <= 0.85, vc <= 1.0, vc <= 1.15],
                                [10.0, 8.0, 6.0, 4.0], default=2.0)
        near_high = np.clip((cv / self.hhv252.iloc[i].values[idx] - 0.75) / 0.25 * 10, 0, 10)
        # v5.4 修复：上市 156–251 日的股票 hhv252 为 NaN，旧代码让 NaN 传播进
        # rank（argsort 沉底隐形淘汰），与同式中 q63/q20 的 NaN→0.5 中性口径矛盾。
        near_high = np.where(np.isnan(near_high), 5.0, near_high)
        q63 = np.where(np.isnan(self.q63.iloc[i].values[idx]), 0.5, self.q63.iloc[i].values[idx]) * 10
        q20 = np.where(np.isnan(self.q20.iloc[i].values[idx]), 0.5, self.q20.iloc[i].values[idx]) * 10
        w = config.SCAN_RANK_WEIGHTS
        rank = (q63 * w["rs_63"] + q20 * w["rs_20"] + ma_score * w["ma_align"]
                + contraction * w["contraction"] + near_high * w["near_high"])

        order = np.argsort(-rank)
        cols = self.close.columns
        return [{"ticker": cols[idx[j]], "rank": float(rank[j]), "price": float(cv[j])}
                for j in order]


# ============================================================
# 第一层：重型采集（参数无关，只算一次）
# ============================================================

def _frame_cache_path(provider_name: str, universe: list[str], days: int,
                      signal_days: int, top_n: int, *, cache_dir=None):
    import hashlib
    from pathlib import Path
    key = hashlib.md5("|".join(sorted(universe)).encode()).hexdigest()[:10]
    today = pd.Timestamp.today().strftime("%Y%m%d")
    name = f"frames_{provider_name}_{key}_{days}_{signal_days}_{top_n}_{today}.pkl"
    from .state import run_path
    return run_path(cache_dir if cache_dir is not None else config.CACHE_DIR, name)


def collect_day_frames(provider, universe: list[str], days: int = 460,
                       signal_days: int = 260, top_n: int = config.SCAN_TOP_N,
                       use_cache: bool = True, *, cache_dir=None
                       ) -> tuple[list[DayFrame], "_Panel", dict]:
    """逐日驱动真实 Agent，产出 DayFrame 序列。

    provider 为真实数据供应商（demo/yahoo/stooq），数据只下载一次。
    同日同参数命中帧缓存则秒回（WFA/调参/测试复用）。
    """
    from .pipeline import _batch_with_fallback

    cache_path = _frame_cache_path(provider.name, universe, days, signal_days, top_n, cache_dir=cache_dir)
    if use_cache and cache_path.exists():
        import pickle
        try:
            with open(cache_path, "rb") as f:
                frames, panel = pickle.load(f)
            logger.info("命中帧缓存: %s（%d 帧）", cache_path, len(frames))
            return frames, panel, {"spy": None, "stock_data": None}
        except Exception:
            pass

    bt = _BtProvider(provider)
    spy = provider.ohlcv(config.BENCHMARK, days=days)
    tnx = provider.tnx_yield(days=days)
    vix = provider.vix(days=days)
    try:
        vix9d = provider.vix9d(days=days)
    except Exception:
        vix9d = None
    sector_etfs = _batch_with_fallback(provider, config.SECTOR_ETFS, days)

    chain_tickers: set[str] = set()
    for cdef in CHAINS.values():
        for link in ("upstream", "midstream", "downstream"):
            chain_tickers.update(cdef[link]["tickers"])
    all_tickers = sorted(set(universe) | chain_tickers)
    stock_data = _batch_with_fallback(provider, all_tickers, days)
    logger.info("回测数据就绪: 股票 %d/%d", len(stock_data), len(all_tickers))

    panel = _Panel(stock_data, spy)
    tnx = tnx.reindex(panel.dates).ffill()
    vix = vix.reindex(panel.dates).ffill()
    vix9d = vix9d.reindex(panel.dates).ffill() if vix9d is not None else None

    mrs_agent = MRSAgent(bt)
    sector_agent = SectorAgent(bt)
    chain_agent = ChainCycleAgent(bt)
    tss_agent = TSSAgent(bt)
    stop_extract = RiskManagerAgent._stop_price

    n_days = len(panel.dates)
    start_i = max(_MIN_HISTORY + 100, n_days - signal_days)
    frames: list[DayFrame] = []

    for i in range(start_i, n_days):
        date = panel.dates[i]
        all_rows = panel.scan_day(i)
        if not all_rows:
            continue

        # ---- 当日切片 market_data（只含 ≤t 数据；先跑市场/板块/产业链）----
        sliced_chain = {t: stock_data[t][stock_data[t].index <= date].tail(_SLICE)
                        for t in sorted(chain_tickers) if t in stock_data}
        md = {
            "spy": spy[spy.index <= date].tail(_SLICE),
            "tnx": tnx[tnx.index <= date].tail(_SLICE),
            "vix": vix[vix.index <= date].tail(_SLICE),
            "vix9d": vix9d[vix9d.index <= date].tail(_SLICE) if vix9d is not None else None,
            "sector_etfs": {e: df[df.index <= date].tail(_SLICE)
                            for e, df in sector_etfs.items()},
            "stock_ohlcv": sliced_chain,
            "universe_closes": {t: df["Close"] for t, df in sliced_chain.items()},
            "precomputed": {
                "breadth200": float(panel.breadth200.iloc[i])
                    if not math.isnan(panel.breadth200.iloc[i]) else float("nan"),
                "ad20_q": float(panel.ad20_q.iloc[i])
                    if not math.isnan(panel.ad20_q.iloc[i]) else None,
            },
        }
        context = {"market_data": md, "trade_date": str(date.date())}

        mrs = mrs_agent.execute(context)
        sectors = sector_agent.execute(context)
        chain_agent.execute(context)
        chain_map = context.get("chain_map", {})

        # ---- 候选 = 全局 Top N + 主线定向补扫（与实盘扫描器同规则）----
        cand_rows = all_rows[:top_n]
        if config.SCAN_MAINLINE_BOOST > 0:
            from .chains import mainline_tickers
            hot_etfs = [s.etf for s in sectors
                        if s.in_main_pool
                        or (s.shs >= config.SHS_SUB_POOL
                            and (math.isnan(s.breadth) or s.breadth >= config.BREADTH_HEALTHY))]
            ml = mainline_tickers(hot_etfs) if hot_etfs else set()
            chosen = {r["ticker"] for r in cand_rows}
            boost = 0
            for row in all_rows[top_n:]:
                if boost >= config.SCAN_MAINLINE_BOOST:
                    break
                if row["ticker"] in ml and row["ticker"] not in chosen:
                    cand_rows.append(row)
                    chosen.add(row["ticker"])
                    boost += 1

        # ---- TSS（真实评分，期权中性）----
        snaps: list[CandidateSnap] = []
        from .data_models import StockCandidate
        for row in cand_rows:
            t = row["ticker"]
            if t not in stock_data:
                continue
            df_t = stock_data[t][stock_data[t].index <= date].tail(_SLICE)
            if df_t is None or len(df_t) < 130:
                continue
            cid, link = chain_of(t)
            c = StockCandidate(ticker=t, rank_score=row["rank"], price=row["price"],
                               chain_id=cid or "", chain_link=link or "")
            c.adv_usd = float(panel.adv20[t].iloc[i]) if not math.isnan(
                panel.adv20[t].iloc[i]) else 0.0
            tss_agent._score(c, df_t)
            bonus = chain_bonus(chain_map.get(c.chain_id)) if chain_map.get(c.chain_id) else 1.0
            etf = CHAINS[cid]["etf"] if cid in CHAINS else ""
            ch = chain_map.get(cid)
            snaps.append(CandidateSnap(
                ticker=t, tss_raw=c.tss, bonus=bonus, c_liq=c.c_liq,
                sector_etf=etf, chain_id=cid or "", chain_hot=bool(ch and ch.hot),
                template=c.entry_template, entry_ref=row["price"],
                stop=stop_extract(c),
                atr_pct=c.atr_pct,
            ))

        frames.append(DayFrame(
            date=date, mrs_star=mrs.mrs_star, regime=mrs.regime,
            sectors=[SectorSnap(etf=s.etf, shs=s.shs, breadth=s.breadth)
                     for s in sectors],
            candidates=snaps,
            gross_cap=mrs.position_cap[1],
        ))

    logger.info("DayFrame 采集完成: %d 个信号日", len(frames))
    if use_cache:
        import pickle
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump((frames, panel), f)
        except Exception as exc:
            logger.debug("帧缓存写入失败: %s", exc)
    return frames, panel, {"spy": spy, "stock_data": stock_data}


# ============================================================
# 第二层：闸门回放 + 交易仿真
# ============================================================

@dataclass
class Trade:
    signal_date: str
    entry_date: str
    exit_date: str
    ticker: str
    template: str
    mode: str                 # 标准做多 / 轻仓试错
    entry: float
    stop0: float
    exit_price: float
    r: float
    weight: float
    entry_i: int = -1
    exit_i: int = -1
    day_returns: dict = field(default_factory=dict)   # master_i → 当日组合收益贡献
    shares: int = 0
    risk: float = 0.0
    chain_id: str = ""

    @property
    def win(self) -> bool:
        return self.r > 0


def _simulate_trade(panel: _Panel, col: int, i_sig: int, stop0: float,
                    params: GateParams, *, sample_end=None, market=None
                    ) -> tuple[int, int, float, float, float, dict] | None:
    """从信号次日开盘仿真到止损/保护/时间止损。

    v6.0：出场判定统一调用 exit_engine.simulate_trade（与 journal 结算同一实现，
    含 COST_BPS 交易成本净口径）；本函数额外维护组合日收益贡献序列。

    返回 (entry_i, exit_i, entry_raw, exit_price_net, r_net, day_returns) 或 None。
    """
    from .exit_engine import cost_adj_buy, cost_adj_sell, simulate_trade
    from .markets import get_market

    market = market or get_market("us")
    if not math.isfinite(stop0) or stop0 <= 0:
        return None

    o, h, l_, c = (panel.open.values[:, col], panel.high.values[:, col],
                   panel.low.values[:, col], panel.close.values[:, col])
    n = len(panel.dates)
    if sample_end is not None:
        n = int(panel.dates.searchsorted(pd.Timestamp(sample_end), side="right"))
    o, h, l_, c = (values[:n] for values in (o, h, l_, c))

    # 入场：信号日后第一个有效交易日开盘（最多等 3 日）
    entry_i, entry_raw = -1, float("nan")
    for j in range(i_sig + 1, min(i_sig + 4, n)):
        if math.isfinite(o[j]) and o[j] > 0 and market.is_trading_day(panel.dates[j]):
            entry_i, entry_raw = j, float(o[j])
            break
    if entry_i < 0 or entry_raw <= stop0:
        return None

    res = simulate_trade(o, h, l_, c, entry_i, stop0,
                         time_stop=params.time_stop,
                         protect_r=params.profit_protect_r,
                         cost_bps=params.cost_bps, dates=panel.dates[:n],
                         market=market, ticker=panel.tickers[col])
    if res is None or res.void:
        return None
    if res.exit_i >= 0:
        exit_i, exit_price_net = res.exit_i, res.exit_price
    else:
        # 数据耗尽未出场：引擎的 exit_price 是未扣成本的最新收盘，
        # 这里统一为净价，保证 r 与 exit_price 严格自洽
        exit_i = res.last_i
        exit_price_net = cost_adj_sell(res.exit_price, params.cost_bps)

    # 组合日收益贡献（净口径：入场按含成本价）
    entry_net = cost_adj_buy(entry_raw, params.cost_bps)
    day_ret: dict[int, float] = {}
    prev = entry_net
    for j in range(entry_i, exit_i + 1):
        cj = float(c[j])
        if not math.isfinite(cj) or cj <= 0:
            continue                                   # 停牌日：持仓不动
        if j == exit_i:
            day_ret[j] = exit_price_net / prev - 1.0
        else:
            day_ret[j] = cj / prev - 1.0
            prev = cj
    return entry_i, exit_i, entry_raw, exit_price_net, res.r, day_ret


def _gate_day(frame: DayFrame, params: GateParams) -> list[tuple[CandidateSnap, str, float]]:
    """单日闸门（v6.0：与生产 RiskManagerAgent 共用 gate.py 单一实现，
    参数化阈值）——v5.4 前此处与生产各自维护一份判定，已经发生漂移。
    返回 [(snap, mode, tos)]。"""
    from .gate import classify_sector_pools, finite_score, pass_gates

    if not finite_score(frame.mrs_star) or frame.mrs_star < params.mrs_block:
        return []
    # 主线/次主线池（参数化阈值，最多 2 条主线；广度缺失不得进主线池）
    main, sub = classify_sector_pools(frame.sectors, shs_main=params.shs_main,
                                      shs_sub=params.shs_sub)
    shs_map = {s.etf: s.shs for s in frame.sectors}

    out = []
    for snap in frame.candidates:
        if (not finite_score(snap.tss_raw) or not math.isfinite(snap.bonus)
                or snap.bonus <= 0 or not math.isfinite(snap.c_liq)
                or not 0 < snap.c_liq <= 1):
            continue
        tss_final = min(10.0, snap.tss_raw * (snap.bonus if params.use_ics else 1.0))
        shs = shs_map.get(snap.sector_etf, config.NEUTRAL_SCORE) if snap.sector_etf else config.NEUTRAL_SCORE
        decision = pass_gates(
            frame.mrs_star, shs, tss_final,
            in_main=snap.sector_etf in main, in_sub=snap.sector_etf in sub,
            chain_hot=snap.chain_hot,
            mrs_gate=params.mrs_gate, shs_sub=params.shs_sub,
            tss_gate=params.tss_gate, light_tss=params.light_tss,
            mrs_light_lo=params.mrs_light_lo, mrs_block=params.mrs_block)
        if not decision.passed:
            continue
        tos = frame.mrs_star * shs * tss_final * snap.c_liq / 100
        out.append((snap, "标准做多" if decision.standard else "轻仓试错", tos))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def run_backtest(frames: list[DayFrame], panel: _Panel,
                 params: GateParams | None = None, *, sample_end=None,
                 account_usd: float = 100_000, market=None,
                 risk_limits: dict | None = None) -> dict:
    """Replay inside an explicit sample, using integer shares and marked NAV.

    The default sample ends on the last signal frame. A caller may extend it
    explicitly, but WFA always supplies each fold's own final day. Unclosed
    positions are valued at the last sample Close less liquidation cost.
    """
    from .exit_engine import cost_adj_buy
    from .markets import get_market
    from .position_sizing import integer_capacity, size_position

    params = tighten_risk_limits(params, risk_limits)
    market = market or get_market("us")
    if isinstance(account_usd, bool) or not math.isfinite(account_usd) or account_usd <= 0:
        raise ValueError("Account equity must be finite and positive")
    ordered = sorted(frames, key=lambda f: f.date)
    if len({f.date for f in ordered}) != len(ordered):
        raise ValueError("Signal dates must be unique")
    end = pd.Timestamp(sample_end) if sample_end is not None else (ordered[-1].date if ordered else None)
    ordered = [f for f in ordered if end is not None and f.date <= end]
    start = ordered[0].date if ordered else None
    col_of = {t: j for j, t in enumerate(panel.tickers)}
    date_i = {d: i for i, d in enumerate(panel.dates)}
    trades: list[Trade] = []

    for frame in ordered:
        i_sig = date_i.get(frame.date)
        if i_sig is None or not market.is_trading_day(frame.date):
            continue
        active = [trade for trade in trades if trade.exit_i >= i_sig]
        slots = params.max_picks - len(active)
        if slots <= 0:
            continue
        gross_cap = frame.gross_cap
        if gross_cap is None:
            gross_cap = MRSAgent._position_cap(frame.mrs_star)[1]
        if isinstance(gross_cap, bool) or not math.isfinite(gross_cap) or not 0 <= gross_cap <= 1:
            raise ValueError("Frame gross cap must be finite and in [0,1]")
        gross_cap = min(gross_cap, params.gross_cap)
        for snap, mode, _ in _gate_day(frame, params):
            if slots <= 0:
                break
            col = col_of.get(snap.ticker)
            if (col is None or any(t.ticker == snap.ticker for t in active)
                    or not math.isfinite(snap.entry_ref) or not math.isfinite(snap.stop)
                    or not 0 < snap.stop < snap.entry_ref):
                continue
            from .parameters import holding_limit
            execution_params = replace(params, time_stop=holding_limit(snap.atr_pct, params))
            sim = _simulate_trade(panel, col, i_sig, snap.stop, execution_params,
                                  sample_end=end, market=market)
            if sim is None:
                continue
            entry_i, exit_i, entry, exit_price, r_net, day_ret = sim
            ratio = 1.0 if mode == "标准做多" else params.light_size
            plan = size_position(account_usd, snap.entry_ref, snap.stop, ratio,
                                 risk_r_pct=params.risk_r_pct,
                                 max_single_position_pct=params.max_single_position_pct)
            entry_net = cost_adj_buy(entry, params.cost_bps)
            shares = min(plan.shares, integer_capacity(plan.risk, entry_net - snap.stop),
                         integer_capacity(account_usd * params.max_single_position_pct, entry_net))
            from .position_sizing import PositionSize
            size = PositionSize(shares, shares * entry_net, shares * entry_net / account_usd,
                                shares * (entry_net - snap.stop), plan.budget, shares < plan.shares)
            if size.shares <= 0:
                continue
            if sum(t.weight for t in active) + size.position_pct > gross_cap + 1e-9:
                continue
            chain_used = sum(t.risk for t in active if t.chain_id == snap.chain_id)
            if snap.chain_id and chain_used + size.risk > account_usd * config.MAX_CHAIN_RISK_PCT + 1e-9:
                continue
            trade = Trade(
                signal_date=str(frame.date.date()),
                entry_date=str(panel.dates[entry_i].date()),
                exit_date=str(panel.dates[exit_i].date()),
                ticker=snap.ticker, template=snap.template or "无", mode=mode,
                entry=entry, stop0=snap.stop, exit_price=exit_price,
                r=r_net, weight=size.position_pct, entry_i=entry_i, exit_i=exit_i,
                day_returns=day_ret, shares=size.shares, risk=size.risk,
                chain_id=snap.chain_id,
            )
            trades.append(trade)
            active.append(trade)
            slots -= 1

    # Accounting NAV covers every available date in the sample, including days
    # without a signal frame. Cash/share flows include both one-sided costs.
    cash, previous_equity = account_usd, account_usd
    held: list[Trade] = []
    marks: dict[str, float] = {}
    port_ret, port_dates, equity = [], [], [1.0]
    for i, day in enumerate(panel.dates):
        if start is None or day < start or day > end:
            continue
        for trade in trades:
            if trade.entry_i == i:
                cash -= trade.shares * cost_adj_buy(trade.entry, params.cost_bps)
                marks[trade.ticker] = trade.entry
                held.append(trade)
        for trade in held:
            value = float(panel.close.iloc[i, col_of[trade.ticker]])
            if math.isfinite(value) and value > 0:
                marks[trade.ticker] = value
        exiting = [trade for trade in held if trade.exit_i == i]
        for trade in exiting:
            cash += trade.shares * trade.exit_price
        held = [trade for trade in held if trade not in exiting]
        current_equity = cash + sum(trade.shares * marks[trade.ticker] for trade in held)
        port_ret.append(current_equity / previous_equity - 1.0)
        port_dates.append(str(day.date()))
        equity.append(current_equity / account_usd)
        previous_equity = current_equity

    rs = [trade.r for trade in trades]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    by_template: dict[str, list[float]] = {}
    for trade in trades:
        by_template.setdefault(trade.template, []).append(trade.r)
    return {
        "params": params, "n_days": len(ordered), "n_trades": len(trades), "trades": trades,
        "sample_start": str(start.date()) if start is not None else None,
        "sample_end": str(end.date()) if end is not None else None,
        "account_usd": account_usd,
        "risk_limits": risk_limits_for(params),
        "win_rate": round(len(wins) / len(rs), 4) if rs else 0.0,
        "avg_r": round(float(np.mean(rs)), 3) if rs else 0.0,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2) if sum(losses) else None) if wins else 0.0,
        "total_r": round(sum(rs), 2), "port_total_return": round(equity[-1] - 1, 4),
        "port_sharpe": round(annualized_sharpe(port_ret), 2),
        "port_max_dd": round(max_drawdown(equity), 4),
        "port_returns": port_ret, "port_dates": port_dates,
        "by_template": {k: {"n": len(v), "win_rate": round(sum(x > 0 for x in v) / len(v), 3),
                              "avg_r": round(float(np.mean(v)), 3)}
                        for k, v in sorted(by_template.items())},
        "valuation_note": "样本末仍持有的仓位按末个有效收盘减卖出成本估值；历史期权缺失使用中性维度。",
    }


# ============================================================
# 第三层：真实滚动 Walk-Forward Analysis + DSR
# ============================================================

DEFAULT_GRID: list[dict] = [
    {"mrs_gate": m, "shs_main": s, "tss_gate": t}
    for m in (5.5, 6.0, 6.5)
    for s in (7.0, 7.5, 8.0)
    for t in (7.0, 7.2, 7.5)
]                                                     # 27 组合


def make_folds(frames: list[DayFrame], train: int = 126, test: int = 63,
               step: int = 63) -> list[tuple[list[DayFrame], list[DayFrame]]]:
    """滚动窗口折（非锚定）：[train 126d][test 63d]，每次前移 63d。"""
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (train, test, step)):
        raise ValueError("WFA train/test/step must be positive integers")
    if any(a.date >= b.date for a, b in zip(frames, frames[1:])):
        raise ValueError("WFA signal dates must be strictly increasing")
    folds = []
    i = 0
    while i + train + test <= len(frames):
        folds.append((frames[i: i + train], frames[i + train: i + train + test]))
        i += step
    return folds


def run_wfa(frames: list[DayFrame], panel: _Panel,
            grid: list[dict] | None = None,
            train: int = 126, test: int = 63, step: int = 63,
            min_trades: int = 5, *, base_params: GateParams | None = None,
            account_usd: float = 100_000, risk_limits: dict | None = None) -> dict:
    """真实滚动 WFA：每折在样本内选参，样本外验证，汇总 OOS + DSR。

    返回 {folds, oos_aggregate, dsr, recommended_params, grid_size, detail}。
    """
    import json
    base_params = tighten_risk_limits(base_params, risk_limits)
    if isinstance(account_usd, bool) or not isinstance(account_usd, (int, float)) or not math.isfinite(account_usd) or account_usd <= 0:
        raise ValueError("WFA account equity must be finite and positive")
    grid = DEFAULT_GRID if grid is None else grid
    if not grid:
        raise ValueError("WFA grid cannot be empty")
    for override in grid:
        if override:
            validate_tuned_params(override)
        elif not isinstance(override, dict):
            raise ValueError("WFA grid entries must be parameter objects")
        tighten_risk_limits(replace(base_params, **override), risk_limits_for(base_params))
    folds = make_folds(frames, train, test, step)
    if not folds:
        return {"error": f"信号日不足（{len(frames)} < {train + test}），无法 WFA",
                "base_params": asdict(base_params), "risk_limits": risk_limits_for(base_params),
                "account_usd": account_usd}

    fold_rows = []
    daily_oos: dict[str, float] = {}
    trade_oos: dict[tuple[str, str, str], float] = {}
    chosen: list[dict] = []
    trial_srs: list[float] = []

    for fi, (train_frames, test_frames) in enumerate(folds):
        best_params, best_sr, best_row = None, -9.0, None
        for g in grid:
            p = tighten_risk_limits(replace(base_params, **g), risk_limits_for(base_params))
            res = run_backtest(train_frames, panel, p, sample_end=train_frames[-1].date,
                               account_usd=account_usd)
            sr = res["port_sharpe"] if res["n_trades"] >= min_trades else -9.0
            trial_srs.append(sr if sr > -9 else 0.0)
            if sr > best_sr or (sr == best_sr and best_row is not None
                                and res["expectancy_r"] > best_row["expectancy_r"]):
                best_params = ({**g, **risk_limits_for(p)} if g else {})
                best_sr, best_row = sr, res
        # v5.4 修复：整折所有网格组合交易数都不足 min_trades 时，best_params
        # 保持 None，旧代码 replace(GateParams(), **None) 直接 TypeError 崩溃。
        # 诚实做法：该折回退理论默认参数并在折明细中披露，绝不硬造"最优"。
        fold_fallback = best_params is None
        if fold_fallback:
            best_params, best_sr = {}, None
            logger.warning("WFA fold %d: 样本内全部网格组合交易数不足 %d，"
                           "回退默认参数（已披露）", fi + 1, min_trades)
        execution_params = tighten_risk_limits(replace(base_params, **best_params),
                                               risk_limits_for(base_params))
        oos = run_backtest(test_frames, panel, execution_params,
                           sample_end=test_frames[-1].date, account_usd=account_usd)
        daily_oos.update(zip(oos["port_dates"], oos["port_returns"]))
        for trade in oos["trades"]:
            trade_oos[(trade.ticker, trade.signal_date, trade.entry_date)] = trade.r
        chosen.append(best_params)
        fold_rows.append({
            "fold": fi + 1,
            "train": f"{train_frames[0].date.date()}~{train_frames[-1].date.date()}",
            "test": f"{test_frames[0].date.date()}~{test_frames[-1].date.date()}",
            "is_params": best_params, "is_sharpe": best_sr,
            "gate_params": asdict(execution_params),
            "risk_limits": risk_limits_for(execution_params),
            "is_trades": best_row["n_trades"] if best_row else 0,
            "is_expectancy": best_row["expectancy_r"] if best_row else None,
            "is_fallback_default": fold_fallback,
            "oos_trades": oos["n_trades"], "oos_win_rate": oos["win_rate"],
            "oos_expectancy": oos["expectancy_r"], "oos_sharpe": oos["port_sharpe"],
            "oos_max_dd": oos["port_max_dd"],
        })
        logger.info("WFA fold %d: IS %s (SR=%s, %d笔%s) → OOS 胜率%.1f%% 期望%.2fR",
                    fi + 1, best_params, f"{best_sr:.2f}" if best_sr is not None else "N/A",
                    best_row["n_trades"] if best_row else 0,
                    "，回退默认" if fold_fallback else "",
                    oos["win_rate"] * 100, oos["expectancy_r"])

    # ---- OOS 汇总 ----
    oos_dates = sorted(daily_oos)
    oos_returns = [daily_oos[day] for day in oos_dates]
    oos_trade_rs = list(trade_oos.values())
    n = len(oos_trade_rs)
    wins = [r for r in oos_trade_rs if r > 0]
    losses = [r for r in oos_trade_rs if r <= 0]
    equity = list(np.cumprod([1 + r for r in oos_returns])) if oos_returns else [1.0]
    oos_sharpe = annualized_sharpe(oos_returns)
    # v5.4 修复 DSR 量纲错误：PSR/DSR 要求 SR̂ 与 T 同频。旧代码把【年化】夏普
    # （已乘 √252）配【日频】T 与偏度峰度，z 值被高估约 √252≈16 倍，DSR 几乎
    # 恒为 1.0——"不显著则回退默认参数"的保险丝形同虚设（折内夏普 3.92 的
    # 过拟合参数会被错误放行）。正确口径：日频 SR + 日频 T + 日频试验 SR 方差。
    rs_clean = [r for r in oos_returns if not math.isnan(r)]
    if len(rs_clean) >= 3:
        _m = sum(rs_clean) / len(rs_clean)
        _v = sum((r - _m) ** 2 for r in rs_clean) / (len(rs_clean) - 1)
        sr_daily = _m / math.sqrt(_v) if _v > 0 else 0.0
    else:
        sr_daily = 0.0
    trial_srs_daily = [s / math.sqrt(252) for s in trial_srs]
    dsr = deflated_sharpe_ratio(
        sr_hat=sr_daily, t=max(len(rs_clean), 2),
        skew=skewness(oos_returns), kurt=kurtosis(oos_returns),
        n_trials=len(grid) * len(folds),          # 保守：网格 × 折数
        trial_srs=trial_srs_daily,
    )
    oos_agg = {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "expectancy_r": round(float(np.mean(oos_trade_rs)), 3) if n else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
            if losses and sum(losses) != 0 else (None if wins else 0.0),
        "sharpe": round(oos_sharpe, 2),
        "max_dd": round(max_drawdown(equity), 4),
    }

    # ---- 参数推荐：被选折中 OOS 期望最高者；OOS 期望 ≤0 则保守回退默认 ----
    rec = dict(chosen[-1]) if chosen else {}
    scored: dict[str, list[float]] = {}
    for row in fold_rows:
        key = json.dumps(row["is_params"], sort_keys=True)
        scored.setdefault(key, []).append(row["oos_expectancy"])
    if scored:
        best_key = max(scored, key=lambda k: (sum(scored[k]) / len(scored[k])))
        rec = json.loads(best_key)
    if oos_agg["expectancy_r"] <= 0 or dsr < 0.5:
        rec = {}                                 # 样本外不显著 → 不覆盖默认

    return {
        "folds": fold_rows,
        "n_folds": len(folds),
        "grid_size": len(grid),
        "account_usd": account_usd,
        "base_params": asdict(base_params),
        "risk_limits": risk_limits_for(base_params),
        "oos_aggregate": oos_agg,
        "oos_dates": oos_dates,
        "oos_returns": oos_returns,
        "overlap_note": "重叠测试窗同一日期只计一次，采用当日最新已完成训练折的回放值。",
        "dsr": round(dsr, 4),
        "dsr_note": (f"DSR={dsr:.3f}（N={len(grid)}×{len(folds)}={len(grid) * len(folds)} 次试验校正）"
                     + (" ≥0.95 统计显著" if dsr >= 0.95 else
                        " 0.5~0.95 弱显著" if dsr >= 0.5 else " <0.5 不显著，建议保持默认参数")),
        "recommended_params": rec,
        "recommended_note": ("样本外期望为正且通过校正 → 生成待审批参数提案"
                             if rec else "样本外不显著 → 保持理论默认参数"),
    }


def save_tuned_params(wfa: dict, path: str = "tuned_params.json") -> str | None:
    """Create a review proposal; research never activates tuned parameters."""
    from pathlib import Path
    from .review.monthly import generate_proposal
    if not wfa.get("recommended_params"):
        return None
    destination = Path(path).absolute().parent / "review_proposals"
    proposal = generate_proposal(wfa, str(destination))
    return str(destination / f"{proposal.proposal_id}.json")


def apply_tuned_params(path: str = "tuned_params.json", *, as_of: str | None = None) -> dict | None:
    """Return validated overrides from an approved effect without global writes.

    Active copy, immutable effect, execution receipt and approved proposal must
    agree. Orphan effects, edited snapshots and pre-effective runs fail closed.
    """
    import hashlib
    import json
    from datetime import date
    from pathlib import Path
    from .ledger_io import file_digest, read_json_strict

    active = Path(path).absolute()
    if not active.exists():
        return None
    try:
        if active.is_symlink():
            raise ValueError("Tuned parameters cannot be a symlink")
        blob = read_json_strict(active)
        if not isinstance(blob, dict) or blob.get("status") != "approved":
            return None
        effective = date.fromisoformat(blob["effective_from"])
        run_date = date.fromisoformat(as_of) if as_of is not None else date.today()
        if run_date < effective:
            return None
        params = validate_tuned_params(blob.get("params"))
        proposal_id, execution_id = blob["proposal_id"], blob["execution_id"]
        # Validate filenames before following references stored in an artifact.
        import re
        for identifier in (proposal_id, execution_id):
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", identifier):
                raise ValueError("Invalid reviewed effect identity")
        root = active.parent.resolve()
        receipt_path = root / "review_executions" / f"{execution_id}.json"
        effect_path = root / "review_executions" / f"{execution_id}.effect.json"
        intent_path = root / "review_executions" / f"{execution_id}.intent.json"
        proposal_path = Path(blob["proposal_path"])
        if (str(receipt_path) != blob.get("receipt_path")
                or proposal_path.name != f"{proposal_id}.json"
                or not proposal_path.resolve().is_relative_to(root)
                or any(p.is_symlink() for p in (receipt_path, effect_path, intent_path, proposal_path))
                or receipt_path.parent.is_symlink()):
            raise ValueError("Reviewed artifacts must remain in the same output directory")
        receipt, proposal = read_json_strict(receipt_path), read_json_strict(proposal_path)
        intent = read_json_strict(intent_path)
        if (not isinstance(intent, dict) or receipt != intent.get("receipt")
                or file_digest(receipt_path) != hashlib.sha256(json.dumps(
                    intent["receipt"], ensure_ascii=False, indent=2,
                    allow_nan=False).encode("utf-8")).hexdigest()):
            raise ValueError("Approval receipt differs from its prepared execution")
        for key in ("proposal_id", "execution_id", "preimage_sha256", "parameters_sha256", "status", "effective_from"):
            if receipt.get(key) != blob.get(key) or proposal.get(key) != blob.get(key):
                raise ValueError("Approval receipt does not match its effect and proposal")
        digest = receipt.get("tuned_sha256")
        if (file_digest(active) != digest or file_digest(effect_path) != digest
                or file_digest(proposal_path) != receipt.get("proposal_sha256")
                or proposal.get("tuned_sha256") != digest
                or receipt.get("tuned_path") != str(effect_path)
                or proposal.get("tuned_path") != str(effect_path)
                or params != proposal.get("grid_result", {}).get("recommended_params")):
            raise ValueError("Approved immutable effect or parameter bytes changed")
        raw = json.dumps(params, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != blob.get("parameters_sha256"):
            raise ValueError("Approved parameters differ from the reviewed digest")
        return params
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Rejected tuned parameters %s: %s", active, exc)
        return None
