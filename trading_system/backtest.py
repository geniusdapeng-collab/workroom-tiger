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
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from . import config
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
class GateParams:
    """闸门参数（WFA 调优对象）。"""
    mrs_block: float = config.MRS_GATE_BLOCK
    mrs_gate: float = config.OPEN_LONG["mrs"]
    mrs_light_lo: float = config.LIGHT_PROBE["mrs_lo"]
    shs_main: float = config.SHS_MAIN_POOL
    shs_sub: float = config.SHS_SUB_POOL
    tss_gate: float = config.OPEN_LONG["tss"]
    light_tss: float = config.LIGHT_PROBE["tss"]
    light_size: float = sum(config.LIGHT_PROBE["size_ratio"]) / 2
    max_picks: int = config.MAX_PICKS_DEFAULT
    time_stop: int = config.TIME_STOP_DAYS[1]
    profit_protect_r: float = config.PROFIT_PROTECT_R
    cost_bps: float = config.COST_BPS    # v6.0：交易成本（净口径回测/调参）
    use_ics: bool = True                 # False → 产业链加成消融


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
                      signal_days: int, top_n: int):
    import hashlib
    from pathlib import Path
    key = hashlib.md5("|".join(sorted(universe)).encode()).hexdigest()[:10]
    today = pd.Timestamp.today().strftime("%Y%m%d")
    return Path(config.CACHE_DIR) / f"frames_{provider_name}_{key}_{days}_{signal_days}_{top_n}_{today}.pkl"


def collect_day_frames(provider, universe: list[str], days: int = 460,
                       signal_days: int = 260, top_n: int = config.SCAN_TOP_N,
                       use_cache: bool = True
                       ) -> tuple[list[DayFrame], "_Panel", dict]:
    """逐日驱动真实 Agent，产出 DayFrame 序列。

    provider 为真实数据供应商（demo/yahoo/stooq），数据只下载一次。
    同日同参数命中帧缓存则秒回（WFA/调参/测试复用）。
    """
    from .pipeline import _batch_with_fallback

    cache_path = _frame_cache_path(provider.name, universe, days, signal_days, top_n)
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
            ))

        frames.append(DayFrame(
            date=date, mrs_star=mrs.mrs_star, regime=mrs.regime,
            sectors=[SectorSnap(etf=s.etf, shs=s.shs, breadth=s.breadth)
                     for s in sectors],
            candidates=snaps,
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

    @property
    def win(self) -> bool:
        return self.r > 0


def _simulate_trade(panel: _Panel, col: int, i_sig: int, stop0: float,
                    params: GateParams) -> tuple[int, int, float, float, float, dict] | None:
    """从信号次日开盘仿真到止损/保护/时间止损。

    v6.0：出场判定统一调用 exit_engine.simulate_trade（与 journal 结算同一实现，
    含 COST_BPS 交易成本净口径）；本函数额外维护组合日收益贡献序列。

    返回 (entry_i, exit_i, entry_raw, exit_price_net, r_net, day_returns) 或 None。
    """
    from .exit_engine import cost_adj_buy, simulate_trade

    o, h, l_, c = (panel.open.values[:, col], panel.high.values[:, col],
                   panel.low.values[:, col], panel.close.values[:, col])
    n = len(panel.dates)

    # 入场：信号日后第一个有效交易日开盘（最多等 3 日）
    entry_i, entry_raw = -1, float("nan")
    for j in range(i_sig + 1, min(i_sig + 4, n)):
        if not math.isnan(o[j]):
            entry_i, entry_raw = j, float(o[j])
            break
    if entry_i < 0 or entry_raw <= stop0:
        return None

    res = simulate_trade(o, h, l_, c, entry_i, stop0,
                         time_stop=params.time_stop,
                         protect_r=params.profit_protect_r,
                         cost_bps=params.cost_bps)
    if res is None or res.void:
        return None
    if res.exit_i >= 0:
        exit_i, exit_price_net = res.exit_i, res.exit_price
    else:
        # 数据耗尽未出场：引擎的 exit_price 是未扣成本的最新收盘，
        # 这里统一为净价，保证 r 与 exit_price 严格自洽
        from .exit_engine import cost_adj_sell
        exit_i = min(entry_i + params.time_stop - 1, n - 1)
        exit_price_net = cost_adj_sell(res.exit_price, params.cost_bps)

    # 组合日收益贡献（净口径：入场按含成本价）
    entry_net = cost_adj_buy(entry_raw, params.cost_bps)
    day_ret: dict[int, float] = {}
    prev = entry_net
    for j in range(entry_i, exit_i + 1):
        cj = float(c[j])
        if math.isnan(cj):
            continue                                   # 停牌日：持仓不动
        if j == exit_i and res.exit_i >= 0:
            day_ret[j] = res.exit_price / prev - 1.0
        else:
            day_ret[j] = cj / prev - 1.0
            prev = cj
    return entry_i, exit_i, entry_raw, exit_price_net, res.r, day_ret


def _gate_day(frame: DayFrame, params: GateParams) -> list[tuple[CandidateSnap, str, float]]:
    """单日闸门（v6.0：与生产 RiskManagerAgent 共用 gate.py 单一实现，
    参数化阈值）——v5.4 前此处与生产各自维护一份判定，已经发生漂移。
    返回 [(snap, mode, tos)]。"""
    from .gate import main_pool_eligible, pass_gates, sub_pool_eligible

    if frame.mrs_star < params.mrs_block:
        return []
    # 主线/次主线池（参数化阈值，最多 2 条主线；广度缺失不得进主线池）
    by_shs = sorted(frame.sectors, key=lambda s: s.shs, reverse=True)
    main: set[str] = set()
    for s in by_shs:
        if len(main) >= config.MAIN_POOL_MAX:
            break
        if main_pool_eligible(s.shs, s.breadth, shs_main=params.shs_main):
            main.add(s.etf)
    sub = {s.etf for s in frame.sectors
           if s.etf not in main and sub_pool_eligible(s.shs, shs_sub=params.shs_sub)}
    shs_map = {s.etf: s.shs for s in frame.sectors}

    out = []
    for snap in frame.candidates:
        tss_final = min(10.0, snap.tss_raw * (snap.bonus if params.use_ics else 1.0))
        shs = shs_map.get(snap.sector_etf, config.NEUTRAL_SCORE) if snap.sector_etf else config.NEUTRAL_SCORE
        decision = pass_gates(
            frame.mrs_star, shs, tss_final,
            in_main=snap.sector_etf in main, in_sub=snap.sector_etf in sub,
            chain_hot=snap.chain_hot,
            mrs_gate=params.mrs_gate, shs_sub=params.shs_sub,
            tss_gate=params.tss_gate, light_tss=params.light_tss,
            mrs_light_lo=params.mrs_light_lo)
        if not decision.passed:
            continue
        tos = frame.mrs_star * shs * tss_final * snap.c_liq / 100
        out.append((snap, "标准做多" if decision.standard else "轻仓试错", tos))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def run_backtest(frames: list[DayFrame], panel: _Panel,
                 params: GateParams | None = None) -> dict:
    """闸门回放 + 交易仿真 + 指标汇总。"""
    params = params or GateParams()
    col_of = {t: j for j, t in enumerate(panel.tickers)}
    date_i = {d: i for i, d in enumerate(panel.dates)}

    trades: list[Trade] = []
    open_until: list[int] = []        # 未平仓交易的 exit_i（并发控制）

    for frame in frames:
        i_sig = date_i.get(frame.date)
        if i_sig is None:
            continue
        open_until = [x for x in open_until if x >= i_sig]
        slots = params.max_picks - len(open_until)
        if slots <= 0:
            continue
        for snap, mode, tos in _gate_day(frame, params)[:slots]:
            col = col_of.get(snap.ticker)
            if col is None or snap.stop >= snap.entry_ref:
                continue
            sim = _simulate_trade(panel, col, i_sig, snap.stop, params)
            if sim is None:
                continue
            entry_i, exit_i, entry, exit_price, r_net, day_ret = sim
            size_ratio = 1.0 if mode == "标准做多" else params.light_size
            risk_pct = (entry - snap.stop) / entry
            weight = min(config.RISK_R_PCT / risk_pct,
                         config.MAX_SINGLE_POSITION_PCT) * size_ratio
            trades.append(Trade(
                signal_date=str(frame.date.date()),
                entry_date=str(panel.dates[entry_i].date()),
                exit_date=str(panel.dates[exit_i].date()),
                ticker=snap.ticker, template=snap.template or "无", mode=mode,
                entry=round(entry, 2), stop0=round(snap.stop, 2),
                exit_price=round(exit_price, 2),
                r=round(r_net, 3),
                weight=round(weight, 4), entry_i=entry_i, exit_i=exit_i,
                day_returns=day_ret,
            ))
            open_until.append(exit_i)

    # ---- 组合日收益 ----
    frame_dates = {f.date for f in frames}
    port_ret = []
    for i, d in enumerate(panel.dates):
        if d not in frame_dates:
            continue
        r_day = 0.0
        for t in trades:
            if t.entry_i <= i <= t.exit_i:
                r_day += t.weight * t.day_returns.get(i, 0.0)
        port_ret.append(r_day)

    rs = [t.r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    equity = list(np.cumprod([1 + r for r in port_ret])) if port_ret else [1.0]
    by_template: dict[str, list[float]] = {}
    for t in trades:
        by_template.setdefault(t.template, []).append(t.r)

    return {
        "params": params,
        "n_days": len(frames),
        "n_trades": len(trades),
        "trades": trades,
        "win_rate": round(len(wins) / len(rs), 4) if rs else 0.0,
        "avg_r": round(float(np.mean(rs)), 3) if rs else 0.0,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else (float("inf") if wins else 0.0),
        "total_r": round(sum(rs), 2),
        "port_total_return": round(equity[-1] - 1, 4),
        "port_sharpe": round(annualized_sharpe(port_ret), 2),
        "port_max_dd": round(max_drawdown(equity), 4),
        "port_returns": port_ret,
        "by_template": {k: {"n": len(v), "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3),
                            "avg_r": round(float(np.mean(v)), 3)}
                        for k, v in sorted(by_template.items())},
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
    folds = []
    i = 0
    while i + train + test <= len(frames):
        folds.append((frames[i: i + train], frames[i + train: i + train + test]))
        i += step
    return folds


def run_wfa(frames: list[DayFrame], panel: _Panel,
            grid: list[dict] | None = None,
            train: int = 126, test: int = 63, step: int = 63,
            min_trades: int = 5) -> dict:
    """真实滚动 WFA：每折在样本内选参，样本外验证，汇总 OOS + DSR。

    返回 {folds, oos_aggregate, dsr, recommended_params, grid_size, detail}。
    """
    grid = grid or DEFAULT_GRID
    folds = make_folds(frames, train, test, step)
    if not folds:
        return {"error": f"信号日不足（{len(frames)} < {train + test}），无法 WFA"}

    fold_rows = []
    oos_returns: list[float] = []
    oos_trade_rs: list[float] = []
    chosen: list[dict] = []
    trial_srs: list[float] = []

    for fi, (train_frames, test_frames) in enumerate(folds):
        best_params, best_sr, best_row = None, -9.0, None
        for g in grid:
            p = replace(GateParams(), **g)
            res = run_backtest(train_frames, panel, p)
            sr = res["port_sharpe"] if res["n_trades"] >= min_trades else -9.0
            trial_srs.append(sr if sr > -9 else 0.0)
            if sr > best_sr or (sr == best_sr and best_row is not None
                                and res["expectancy_r"] > best_row["expectancy_r"]):
                best_params, best_sr, best_row = g, sr, res
        # v5.4 修复：整折所有网格组合交易数都不足 min_trades 时，best_params
        # 保持 None，旧代码 replace(GateParams(), **None) 直接 TypeError 崩溃。
        # 诚实做法：该折回退理论默认参数并在折明细中披露，绝不硬造"最优"。
        fold_fallback = best_params is None
        if fold_fallback:
            best_params, best_sr = {}, None
            logger.warning("WFA fold %d: 样本内全部网格组合交易数不足 %d，"
                           "回退默认参数（已披露）", fi + 1, min_trades)
        oos = run_backtest(test_frames, panel, replace(GateParams(), **best_params))
        oos_returns.extend(oos["port_returns"])
        oos_trade_rs.extend([t.r for t in oos["trades"]])
        chosen.append(best_params)
        fold_rows.append({
            "fold": fi + 1,
            "train": f"{train_frames[0].date.date()}~{train_frames[-1].date.date()}",
            "test": f"{test_frames[0].date.date()}~{test_frames[-1].date.date()}",
            "is_params": best_params, "is_sharpe": best_sr,
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
            if losses and sum(losses) != 0 else (float("inf") if wins else 0.0),
        "sharpe": round(oos_sharpe, 2),
        "max_dd": round(max_drawdown(equity), 4),
    }

    # ---- 参数推荐：被选折中 OOS 期望最高者；OOS 期望 ≤0 则保守回退默认 ----
    rec = dict(chosen[-1]) if chosen else {}
    scored: dict[str, list[float]] = {}
    for row in fold_rows:
        key = str(sorted(row["is_params"].items()))
        scored.setdefault(key, []).append(row["oos_expectancy"])
    if scored:
        best_key = max(scored, key=lambda k: (sum(scored[k]) / len(scored[k])))
        rec = dict(eval(best_key))
    if oos_agg["expectancy_r"] <= 0 or dsr < 0.5:
        rec = {}                                 # 样本外不显著 → 不覆盖默认

    return {
        "folds": fold_rows,
        "n_folds": len(folds),
        "grid_size": len(grid),
        "oos_aggregate": oos_agg,
        "dsr": round(dsr, 4),
        "dsr_note": (f"DSR={dsr:.3f}（N={len(grid)}×{len(folds)}={len(grid) * len(folds)} 次试验校正）"
                     + (" ≥0.95 统计显著" if dsr >= 0.95 else
                        " 0.5~0.95 弱显著" if dsr >= 0.5 else " <0.5 不显著，建议保持默认参数")),
        "recommended_params": rec,
        "recommended_note": ("样本外期望为正且通过校正 → 写入 tuned_params.json"
                             if rec else "样本外不显著 → 保持理论默认参数"),
    }


def save_tuned_params(wfa: dict, path: str = "tuned_params.json") -> str | None:
    """WFA 推荐参数落盘（pipeline 启动时加载覆盖默认闸门）。"""
    import json
    from datetime import datetime
    if not wfa.get("recommended_params"):
        return None
    blob = {
        "tuned_at": datetime.now().isoformat(timespec="seconds"),
        "params": wfa["recommended_params"],
        "oos_aggregate": wfa["oos_aggregate"],
        "dsr": wfa["dsr"],
        "n_folds": wfa["n_folds"],
        "grid_size": wfa["grid_size"],
    }
    with open(path, "w") as f:
        json.dump(blob, f, ensure_ascii=False, indent=2)
    return path


def apply_tuned_params(path: str = "tuned_params.json") -> dict | None:
    """读取调优参数并覆盖 config 闸门（返回应用的参数，无文件/无效返回 None）。"""
    import json
    import os
    if not os.path.exists(path):
        return None
    try:
        blob = json.load(open(path))
        p = blob.get("params", {})
        applied = {}
        if "mrs_gate" in p:
            config.OPEN_LONG["mrs"] = float(p["mrs_gate"]); applied["mrs_gate"] = p["mrs_gate"]
        if "shs_main" in p:
            config.SHS_MAIN_POOL = float(p["shs_main"]); applied["shs_main"] = p["shs_main"]
        if "tss_gate" in p:
            config.OPEN_LONG["tss"] = float(p["tss_gate"]); applied["tss_gate"] = p["tss_gate"]
        return applied or None
    except Exception:
        return None
