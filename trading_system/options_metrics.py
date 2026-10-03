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
import re
from datetime import date, datetime
from pathlib import Path

from . import config
from .ledger_io import file_transaction, read_json_strict, write_json_atomic
from .indicators import (
    aggregate, score_crowding_neutral_best, score_from_quantile,
    score_iv_pct_tss,
)

logger = logging.getLogger(__name__)

MIN_OBS = 10          # 分位数最小历史样本数
KEEP_OBS = 126        # 历史库保留长度（约半年交易日）


def _pct_rank(values: list[float], x: float) -> float:
    """x 在 values 中的分位（≤x 的占比）。values 含 x 自身。"""
    vals = [v for v in values if math.isfinite(v)]
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
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^_-]{0,47}", ticker):
            raise ValueError("Invalid options-history ticker identifier")
        if self.root.is_symlink():
            raise ValueError("Options-history root cannot be a symlink")
        target = self.root / f"{ticker}.json"
        if target.is_symlink():
            raise ValueError("Options-history file cannot be a symlink")
        return target

    @staticmethod
    def _validate(hist: object) -> list[dict]:
        if not isinstance(hist, list):
            raise ValueError("Options history must be an array")
        seen = set()
        previous = ""
        for rec in hist:
            if not isinstance(rec, dict) or not isinstance(rec.get("date"), str):
                raise ValueError("Options history record must contain an ISO date")
            date.fromisoformat(rec["date"])
            if rec["date"] in seen or rec["date"] < previous:
                raise ValueError("Options history dates must be unique and increasing")
            seen.add(rec["date"])
            previous = rec["date"]
            for key in ("pcr_oi", "atm_iv", "call_oi"):
                value = rec.get(key)
                if value is not None and (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0):
                    raise ValueError(f"Options history {key} must be finite and non-negative, or null")
        return hist

    def load(self, ticker: str) -> list[dict]:
        target = self._path(ticker)
        try:
            hist = read_json_strict(target)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Options history {target} is damaged; original evidence preserved") from exc
        return self._validate(hist)

    def append(self, ticker: str, snap: dict, *, as_of: str | None = None) -> list[dict]:
        """Lock the complete RMW and publish only a successfully persisted history."""
        if not isinstance(snap, dict):
            raise ValueError("Options snapshot must be an object")
        target = self._path(ticker)
        today = as_of or datetime.now().strftime("%Y-%m-%d")
        rec = {"date": today, "pcr_oi": snap.get("pcr_oi"),
               "atm_iv": snap.get("atm_iv"), "call_oi": snap.get("call_oi")}
        self._validate([rec])
        with file_transaction(target):
            hist = self.load(ticker)
            if hist and hist[-1]["date"] > today:
                raise ValueError("Refusing a historical append behind the stored options sample")
            if hist and hist[-1]["date"] == today:
                hist[-1] = rec
            else:
                hist.append(rec)
            hist = hist[-KEEP_OBS:]
            write_json_atomic(target, hist)
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
    # v6.5：判定改用 providers.base.is_synthetic（按 synthetic 标记，不再按 name
    # 字符串）——原先 `name == "demo"` 会被 DemoProvider 子类（测试夹具改名如
    # "onestale-demo"）绕过，合成快照照样落进 reports/options_hist/。
    from .providers.base import is_synthetic
    if is_synthetic(provider):
        persist = False
    store = store or OptionsHistoryStore()
    hist = store.append(ticker, snap) if persist else (store.load(ticker) + [snap])

    def _num(v) -> float:
        """缺失/None/NaN 统一归一为 NaN（v5.4：旧代码遇显式 None 直接 TypeError）。"""
        try:
            f = float(v)
            return f if math.isfinite(f) and not isinstance(v, bool) else float("nan")
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
