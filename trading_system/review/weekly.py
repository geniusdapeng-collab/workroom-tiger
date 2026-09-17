"""周度统计体检（统计员）— 胜率/期望/分模板/R 分布 + 校准层联动 + 三档结论。

三档结论（阈值集中在 config.REVIEW_*，single source of truth）：
  符合预期  样本充足且期望 R、胜率均在警戒线之上
  关注      样本积累中（< REVIEW_WEEKLY_MIN_TRADES）或接近期望/胜率警告线
  告警      期望 R ≤ 告警线或胜率低于告警线——触发复盘深挖，不改任何闸门

隔离性：本模块只读 journal 台账与校准层摘要（会计账白名单），
输出只进复盘纪要与治理事件，绝不进入决策输入。
"""

from __future__ import annotations

import logging

from .. import config

logger = logging.getLogger(__name__)

# R 分布直方图分桶（附录D 口径：流程分布，不评判单笔运气）
_R_BUCKETS = ((-9e9, -1.0, "≤-1R"), (-1.0, 0.0, "-1~0R"),
              (0.0, 1.0, "0~1R"), (1.0, 2.0, "1~2R"), (2.0, 9e9, "≥2R"))


def r_distribution(rs: list[float]) -> dict[str, int]:
    """已结算 R 值分布（含边界：-1R 归 -1~0R，2R 归 ≥2R）。"""
    dist = {label: 0 for _, _, label in _R_BUCKETS}
    for r in rs:
        for lo, hi, label in _R_BUCKETS:
            if lo < r <= hi or (label == "≤-1R" and r <= -1.0):
                dist[label] += 1
                break
    return dist


def verdict_of(stats: dict) -> tuple[str, list[str]]:
    """三档体检结论。返回 (verdict, reasons)。

    verdict ∈ {"符合预期", "关注", "告警"}；reasons 为触达阈值的可读说明。
    """
    n = stats.get("closed", 0)
    exp_r = stats.get("expectancy_r", 0.0)
    win = stats.get("win_rate", 0.0)
    reasons: list[str] = []

    if n < config.REVIEW_WEEKLY_MIN_TRADES:
        return "关注", [f"已结算样本 {n} 笔 < {config.REVIEW_WEEKLY_MIN_TRADES}，"
                        "统计口径尚未稳定（样本积累中，不作结论性判断）"]
    if exp_r <= config.REVIEW_ALERT_EXPECTANCY_R:
        reasons.append(f"期望 {exp_r}R ≤ 告警线 {config.REVIEW_ALERT_EXPECTANCY_R}R")
    if win < config.REVIEW_ALERT_WIN_RATE:
        reasons.append(f"胜率 {win:.1%} < 告警线 {config.REVIEW_ALERT_WIN_RATE:.0%}")
    if reasons:
        return "告警", reasons
    if exp_r < config.REVIEW_WARN_EXPECTANCY_R:
        reasons.append(f"期望 {exp_r}R < 关注线 {config.REVIEW_WARN_EXPECTANCY_R}R")
    if win < config.REVIEW_WARN_WIN_RATE:
        reasons.append(f"胜率 {win:.1%} < 关注线 {config.REVIEW_WARN_WIN_RATE:.0%}")
    if reasons:
        return "关注", reasons
    return "符合预期", [f"样本 {n} 笔，期望 {exp_r}R、胜率 {win:.1%} 均在阈值之上"]


def weekly_checkup(records: list[dict],
                   calibration_summary: dict | None = None) -> dict:
    """周度体检主入口。

    records: journal 记录（只读已结算的做统计）；
    calibration_summary: 校准层 run() 摘要（联动更新样本状态 n/50）。
    """
    closed = [r for r in records
              if r.get("status") == "closed" and r.get("r") is not None]
    rs = [float(r["r"]) for r in closed]
    wins = [r for r in rs if r > 0]

    def agg(sub: list[float]) -> dict:
        if not sub:
            return {"n": 0, "win_rate": 0.0, "avg_r": 0.0}
        w = sum(1 for x in sub if x > 0)
        return {"n": len(sub), "win_rate": round(w / len(sub), 3),
                "avg_r": round(sum(sub) / len(sub), 3)}

    by_template: dict[str, list[float]] = {}
    for r in closed:
        by_template.setdefault(r.get("template") or "无", []).append(float(r["r"]))

    stats = {
        "closed": len(rs),
        "win_rate": round(len(wins) / len(rs), 3) if rs else 0.0,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "total_r": round(sum(rs), 2),
        "last20": agg(rs[-20:]),
        "last50": agg(rs[-50:]),
        "by_template": {k: agg(v) for k, v in sorted(by_template.items())},
    }
    verdict, reasons = verdict_of(stats)

    # 校准层联动：样本状态 n/50 如实带进体检结论（校准口径不变，只引用）
    cal = calibration_summary or {}
    calibration_status = {
        "status": cal.get("status", "unknown"),
        "n": cal.get("n", 0),
        "min_samples": cal.get("min_samples", config.CALIBRATION_MIN_SAMPLES),
        "disclosure": cal.get("disclosure", "校准摘要未提供"),
    }

    return {
        "verdict": verdict, "reasons": reasons,
        "stats": stats, "r_distribution": r_distribution(rs),
        "calibration": calibration_status,
        "thresholds": {
            "min_trades": config.REVIEW_WEEKLY_MIN_TRADES,
            "warn_expectancy_r": config.REVIEW_WARN_EXPECTANCY_R,
            "alert_expectancy_r": config.REVIEW_ALERT_EXPECTANCY_R,
            "warn_win_rate": config.REVIEW_WARN_WIN_RATE,
            "alert_win_rate": config.REVIEW_ALERT_WIN_RATE,
        },
    }
