"""期权维度指标 — 真实期权链 → TSS 期权组件（理论 §4.1，TSS_OPTIONS_AGG）。

流程：
  provider.options_chain_snapshot(ticker)  →  原始快照（PCR_OI / ATM_IV / CallOI）
  → 台账历史库逐日积累（reports/options_hist/，随台账提交持久化，最多保留 126 条）
  → 分位数计算（CallOI 变化分位 / PCR 分位 / IV 分位）
  → 映射表打分（A/B/C）→ 加权聚合 S_options

诚实降级原则：
  - 期权链获取失败 → 三个子项全部中性 5 分 + missing 记录
  - 历史样本不足（< MIN_OBS 条）→ 中性 5 分 + evidence 注明"样本积累中"
  分位数只在真实积累的历史上计算，绝不伪造。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

from . import config
from .indicators import (
    aggregate, score_crowding_neutral_best, score_from_quantile,
    score_iv_pct_tss,
)

logger = logging.getLogger(__name__)

MIN_OBS = 10          # 分位数最小历史样本数
KEEP_OBS = 126        # 历史库保留长度（约半年交易日）


def _pct_rank(values: list[float], x: float) -> float:
    """x 在 values 中的分位（≤x 的占比）。values 含 x 自身。"""
    vals = [v for v in values if not math.isnan(v)]
    if not vals:
        return float("nan")
    return sum(1 for v in vals if v <= x) / len(vals)


class OptionsHistoryStore:
    """期权快照历史库（JSON 文件，每 ticker 一个文件）。

    默认落在 reports/options_hist/ 而非 cache/：cache/ 被 .gitignore 排除，
    云端沙箱每轮全新克隆后历史归零、分位数永远积累不起来；
    reports/ 随台账提交回仓库，是跨轮累计的唯一通道。
    """

    def __init__(self, root: str | Path | None = None):
        import os
        # v6.0：root 可用 TS_OPTIONS_HIST_DIR 覆盖——台账数据本质不是代码，
        # 生产部署建议指向独立数据目录/卷，代码仓保持纯。
        self.root = Path(root or os.environ.get("TS_OPTIONS_HIST_DIR")
                         or Path(config.REPORTS_DIR) / "options_hist")

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.json"

    def load(self, ticker: str) -> list[dict]:
        p = self._path(ticker)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text())
        except Exception:
            return []

    def append(self, ticker: str, snap: dict) -> list[dict]:
        """按日落账（同日重复运行不重复积累），返回完整历史。"""
        hist = self.load(ticker)
        today = datetime.now().strftime("%Y-%m-%d")
        rec = {
            "date": today,
            "pcr_oi": snap.get("pcr_oi"),
            "atm_iv": snap.get("atm_iv"),
            "call_oi": snap.get("call_oi"),
        }
        if hist and hist[-1].get("date") == today:
            hist[-1] = rec
        else:
            hist.append(rec)
        hist = hist[-KEEP_OBS:]
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(ticker).write_text(json.dumps(hist))
        except Exception as exc:
            logger.debug("期权历史写入失败 %s: %s", ticker, exc)
        return hist


def score_options(ticker: str, provider, store: OptionsHistoryStore | None = None,
                  persist: bool = True) -> dict:
    """计算单标的期权维度 A/B/C 子分与聚合分。

    s_options=None 表示缺失（上游聚合时再归一化剔除，而非钉向中性 5）。
    返回 {A, B, C, s_options, missing, evidence, raw}。
    """
    neutral = {"A": None, "B": None, "C": None, "s_options": None}
    try:
        snap = provider.options_chain_snapshot(ticker)
    except Exception:
        snap = None
    if not snap:
        return {**neutral, "missing": ["options_chain"],
                "evidence": "期权链缺失 → 维度剔除再归一化", "raw": {}}

    # v6.1：demo 合成快照【禁止写入】真实历史库——否则 demo 的编造数据会
    # 混入生产分位（"拿不到就瞎编"的典型污染链）。demo 下仅内存中计算。
    if getattr(provider, "name", "") == "demo":
        persist = False
    store = store or OptionsHistoryStore()
    hist = store.append(ticker, snap) if persist else (store.load(ticker) + [snap])

    def _num(v) -> float:
        """缺失/None/NaN 统一归一为 NaN（v5.4：旧代码遇显式 None 直接 TypeError）。"""
        try:
            f = float(v)
            return f if f == f else float("nan")
        except (TypeError, ValueError):
            return float("nan")

    pcr = _num(snap.get("pcr_oi"))
    iv = _num(snap.get("atm_iv"))
    call_oi = _num(snap.get("call_oi"))

    if len(hist) < MIN_OBS:
        return {**neutral,
                "missing": [],
                "evidence": f"期权历史样本积累中（{len(hist)}/{MIN_OBS}）→ 维度暂剔除再归一化",
                "raw": {"pcr_oi": pcr, "atm_iv": iv, "call_oi": call_oi,
                        "obs": len(hist)}}

    # A：Call OI 日变化的一年分位（突增 = 聪明钱进场信号）
    # v5.4 修复：任一端缺失即跳过该对——旧代码 `or 0` 会把缺失当 0，
    # 制造 -100% 的假跳变混入一年分位分布。
    chgs = []
    for prev, cur in zip(hist, hist[1:]):
        p, c = _num(prev.get("call_oi")), _num(cur.get("call_oi"))
        if p and p == p and c == c:         # p 非 0 且两端均非 NaN
            chgs.append(c / p - 1)
    call_oi_chg = chgs[-1] if chgs else float("nan")
    q_chg = _pct_rank(chgs, call_oi_chg) if chgs else float("nan")
    # 子项缺失 → None（聚合层剔除再归一化），不允许 NaN 分位 0.0 被极端打分
    a = score_from_quantile(q_chg) if not math.isnan(q_chg) else None

    # B：PCR_OI 分位（中性最好、极端扣分 —— 拥挤度理论）
    q_pcr = _pct_rank([_num(h.get("pcr_oi")) for h in hist], pcr) if not math.isnan(pcr) else float("nan")
    b = score_crowding_neutral_best(q_pcr) if not math.isnan(q_pcr) else None

    # C：ATM IV 分位（高分位 = 期权贵、无入场边际优势）
    q_iv = _pct_rank([_num(h.get("atm_iv")) for h in hist], iv) if not math.isnan(iv) else float("nan")
    c_score = score_iv_pct_tss(q_iv) if not math.isnan(q_iv) else None

    # 三个子项全缺失 → 整维 None（剔除再归一化），而不是钉中性 5 分
    s_opt = (None if all(v is None for v in (a, b, c_score))
             else aggregate({"A": a, "B": b, "C": c_score}, config.TSS_OPTIONS_AGG))
    missing = [k for k, v in (("call_oi_chg", a), ("pcr_oi", b), ("atm_iv", c_score))
               if v is None]
    a_txt = f"CallOI变化{call_oi_chg:+.1%}分位{q_chg:.2f}→{a}分" if a is not None else "CallOI缺失→剔除"
    b_txt = f"PCR {pcr:.2f}分位{q_pcr:.2f}→{b}分" if b is not None else "PCR缺失→剔除"
    c_txt = f"IV {iv:.0%}分位{q_iv:.2f}→{c_score}分" if c_score is not None else "IV缺失→剔除"
    return {
        "A": a, "B": b, "C": c_score, "s_options": s_opt,
        "missing": missing,
        "evidence": f"期权: {a_txt}; {b_txt}; {c_txt} ⇒ S_options={s_opt}",
        "raw": {"pcr_oi": pcr, "atm_iv": iv, "call_oi": call_oi,
                "call_oi_chg": call_oi_chg, "obs": len(hist)},
    }
