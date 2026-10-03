"""统计校准层（v6.3 S2）— 信号分数 → 实际结果的校准摘要。

定位（调研一亮点：校准层 + 样本门槛）：
  从 journal 已结算记录提取"信号分数快照（TSS_final / MRS*）→ 实际结果（R）"
  样本，分桶统计实际胜率 / 期望 R / 样本数，并检测分数与结果的单调性。

样本门槛（迭代诚实 D7 的机器执行）：
  总样本 < CALIBRATION_MIN_SAMPLES（默认 50）时只输出区间表述
  （桶胜率 ± Wilson 95% 区间），报告固定披露"校准样本积累中（n/50）"，
  不产出校准曲线——样本不足时沉默比宣传更专业；≥50 才输出校准曲线点。

隔离性声明（会计账白名单，与 journal 同级）：
  - 本模块【只读】已结算台账（status=="closed" 且 r 非空），
    样本落盘 CALIBRATION_SAMPLES_PATH（零基线白名单，见 state.py）；
  - 输出【只进报告层】（report.py "统计校准"小节），
    绝不进入决策输入——本模块不 import gate/agents/pipeline 决策链路，
    pipeline 中仅以 iteration.calibration 环节调用并把摘要写入 raw 披露，
    任何分数/闸门计算都不得读取本模块（pytest 静态审查锁定）。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path

from . import config
from .ledger_io import file_transaction, read_json_strict, write_json_atomic

logger = logging.getLogger(__name__)

_WILSON_Z = 1.96     # 95% 置信区间


def validate_accounting_rows(records, *, journal: bool = True) -> None:
    """Reject corrupt accounting evidence before deriving a report or sample file."""
    if not isinstance(records, list):
        raise ValueError("Accounting records must be an array")
    for row in records:
        if (not isinstance(row, dict) or not isinstance(row.get("date"), str)
                or not isinstance(row.get("ticker"), str) or not row["ticker"]):
            raise ValueError("Accounting record identity is invalid")
        date.fromisoformat(row["date"])
        if journal and row.get("status") not in ("open", "closed", "void"):
            raise ValueError("Accounting record status is invalid")
        for name in ("r", "tss_final", "mrs_star"):
            value = row.get(name)
            if value is None:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or name != "r" and not 0 <= value <= 10):
                raise ValueError(f"Accounting record {name} must be a finite valid number")
        if not journal and not isinstance(row.get("win"), bool):
            raise ValueError("Calibration sample outcome must be boolean")


def wilson_interval(wins: int, n: int, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score 区间（小样本下比正态近似稳健的胜率置信区间）。"""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


class CalibrationLayer:
    """校准样本库 + 校准摘要（只读台账、只进报告）。"""

    def __init__(self, journal_path: str | None = None,
                 samples_path: str | None = None,
                 min_samples: int | None = None,
                 buckets: list[tuple[float, float]] | None = None):
        self.journal_path = Path(journal_path or config.CALIBRATION_JOURNAL_PATH)
        self.samples_path = Path(samples_path or config.CALIBRATION_SAMPLES_PATH)
        self.min_samples = (config.CALIBRATION_MIN_SAMPLES
                            if min_samples is None else min_samples)
        self.buckets = buckets or list(config.CALIBRATION_BUCKETS)
        if (isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int)
                or self.min_samples < 1):
            raise ValueError("Calibration minimum samples must be a positive integer")
        if (not isinstance(self.buckets, (list, tuple)) or any(
                not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in pair)
                or not pair[0] < pair[1] for pair in self.buckets)):
            raise ValueError("Calibration buckets must have finite increasing boundaries")

    # ------------------------------------------------------------ 样本库

    def collect_samples(self, records: list[dict]) -> list[dict]:
        """从 journal 记录提取校准样本。【只读已结算记录】：
        status=="closed" 且 r 非空（未结算/作废/悬挂一律不进入样本库）。"""
        validate_accounting_rows(records)
        samples: list[dict] = []
        seen: set[tuple] = set()
        for rec in records:
            if rec.get("status") != "closed" or rec.get("r") is None:
                continue
            key = (rec.get("date"), rec.get("ticker"))
            if key in seen:
                continue
            seen.add(key)
            samples.append({
                "date": rec.get("date"), "ticker": rec.get("ticker"),
                "tss_final": rec.get("tss_final"),
                "mrs_star": rec.get("mrs_star"),
                "r": rec.get("r"), "win": bool(rec.get("r", 0) > 0),
            })
        return samples

    def save_samples(self, samples: list[dict]) -> None:
        validate_accounting_rows(samples, journal=False)
        with file_transaction(self.samples_path):
            write_json_atomic(self.samples_path, samples, indent=1)

    # ------------------------------------------------------------ 摘要

    def run(self) -> dict:
        """读 journal → 更新样本库（落盘）→ 输出校准摘要。"""
        with file_transaction(self.journal_path), file_transaction(self.samples_path):
            try:
                records = read_json_strict(self.journal_path)
            except FileNotFoundError:
                records = []
            samples = self.collect_samples(records)
            summary = self.summarize(samples)
            write_json_atomic(self.samples_path, samples, indent=1)
            return summary

    def summarize(self, samples: list[dict]) -> dict:
        """分桶统计 + 样本门槛降级 + 单调性检测。"""
        validate_accounting_rows(samples, journal=False)
        n = len(samples)
        bucket_stats = []
        for lo, hi in self.buckets:
            sub = [s for s in samples
                   if s.get("tss_final") is not None
                   and lo <= float(s["tss_final"]) < hi]
            wins = sum(1 for s in sub if s["win"])
            wl, wh = wilson_interval(wins, len(sub))
            bucket_stats.append({
                "bucket": f"{lo}-{hi if hi <= 10 else '10+'}".replace("-10.01", "+"),
                "lo": lo, "hi": hi, "n": len(sub), "wins": wins,
                "win_rate": round(wins / len(sub), 4) if sub else None,
                "wilson": [wl, wh],
                "avg_r": round(sum(float(s["r"]) for s in sub) / len(sub), 3) if sub else None,
            })

        if n < self.min_samples:
            # 样本门槛：只输出区间表述，固定披露积累进度，不产出校准曲线
            return {
                "status": "accumulating", "n": n, "min_samples": self.min_samples,
                "disclosure": f"校准样本积累中（{n}/{self.min_samples}）——"
                              "以下为区间表述（Wilson 95%），不产出校准曲线，不作任何结论",
                "buckets": bucket_stats, "curve": None, "monotonic": None,
            }

        # ≥ 门槛：输出校准曲线点（桶中位数分数 → 实际胜率/期望R）与单调性
        curve = [{
            "bucket": b["bucket"], "n": b["n"],
            "win_rate": b["win_rate"], "avg_r": b["avg_r"],
        } for b in bucket_stats]
        rates = [b["win_rate"] for b in bucket_stats
                 if b["n"] > 0 and b["win_rate"] is not None]
        monotonic = bool(rates) and all(b >= a - 1e-9 for a, b in zip(rates, rates[1:]))
        return {
            "status": "calibrated", "n": n, "min_samples": self.min_samples,
            "disclosure": f"校准样本 {n} 条（≥{self.min_samples}），输出校准曲线点；"
                          f"分数-结果单调性：{'成立' if monotonic else '不成立（需复盘）'}",
            "buckets": bucket_stats, "curve": curve, "monotonic": monotonic,
        }
