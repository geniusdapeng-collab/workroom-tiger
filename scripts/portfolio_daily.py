#!/usr/bin/env python3
"""资产管理团队日度任务（v3.5）— 配置官+哨兵+组合风险官+收益稳定官一轮协作。

流程（上下游协作）：
  上游：三市当日产物（result_*.json / sim_portfolio.json）+ 实时基准/FRED
  本层：配置方案 → 全球动态快照 → 组合风险视图 + 收益稳定评估
  下游：reports/portfolio/portfolio_day.json（组合总览数据源）+ 治理事件留痕

用法：
  python3 scripts/portfolio_daily.py [--us reports] [--cn reports/cn] [--hk reports/hk] \
      [--out reports/portfolio] [--events reports/governance_events.jsonl]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trading_system.governance_bridge import (  # noqa: E402
    FiveElementEvent, GovernanceBridge, RuleImpact)
from trading_system.portfolio import (  # noqa: E402
    GlobalAllocator, GlobalSentinel, PortfolioRiskOfficer, ReturnSteward)


def _load_result(out_dir: str) -> dict | None:
    fs = sorted(glob.glob(str(Path(out_dir) / "result_*.json")))
    if not fs:
        return None
    try:
        return json.loads(Path(fs[-1]).read_text())
    except Exception:
        return None


def _graduated(out_dir: str) -> bool:
    """新市场验证期判定（reports/<m>/graduation.json 存在且达标为准；默认未达标）。"""
    f = Path(out_dir) / "graduation.json"
    if f.exists():
        try:
            g = json.loads(f.read_text())
            return bool(g.get("graduated"))
        except Exception:
            pass
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--us", default="reports")
    ap.add_argument("--cn", default="reports/cn")
    ap.add_argument("--hk", default="reports/hk")
    ap.add_argument("--out", default="reports/portfolio")
    ap.add_argument("--events", default="reports/governance_events.jsonl")
    args = ap.parse_args()

    dirs = {"us": args.us, "cn": args.cn, "hk": args.hk}
    results = {m: _load_result(d) for m, d in dirs.items()}
    graduated = {m: (True if m == "us" else _graduated(d)) for m, d in dirs.items()}

    # 1. 全球资产配置官：配置方案
    allocator = GlobalAllocator()
    plan = allocator.plan(results, graduated=graduated,
                          date=datetime.now().strftime("%Y-%m-%d"))

    # 2. 全球宏观哨兵：全球动态快照（best-effort，失败记 missing）
    try:
        snapshot = GlobalSentinel().snapshot().to_dict()
    except Exception as e:
        snapshot = {"error": str(e)[:200], "missing": ["sentinel:init"]}

    # 3. 组合风险官：敞口聚合视图
    risk_view = PortfolioRiskOfficer(gross_cap=allocator.gross_cap).view(dirs).to_dict()

    # 4. 收益稳定官：稳定性三档评估
    stability = ReturnSteward().assess(dirs).to_dict()

    day = {
        "date": plan.date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "allocation": plan.to_dict(),
        "snapshot": snapshot,
        "risk": risk_view,
        "stability": stability,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    day_path = out / "portfolio_day.json"
    day_path.write_text(json.dumps(day, ensure_ascii=False, indent=2), encoding="utf-8")

    # 治理事件留痕（组合层动作入五元事件链）
    try:
        bridge = GovernanceBridge(args.events)
        bridge.emit(FiveElementEvent(
            who={"type": "agent", "id": "global-allocator", "version": "v0.1"},
            context={"channel": "portfolio", "stage": "paper", "market": "multi"},
            object={"type": "portfolio", "id": plan.date},
            decision={
                "action": "portfolio.allocate",
                "after": {"weights": {m.market: m.weight for m in plan.markets},
                          "truncated": plan.truncated,
                          "stability_verdict": stability.get("verdict")},
                "basis": [plan.note,
                          f"组合净值 {risk_view.get('total_equity')}, "
                          f"稳定性 {stability.get('verdict')}"
                          f"（回撤 {stability.get('max_drawdown')}）"],
            },
            rule_impact=[RuleImpact("R-T0", result="pass")],
        ))
    except Exception as e:
        print(f"[警告] 治理事件写入失败（不阻塞，旁路纪律）: {e}")

    print(f"✓ 资产管理团队日度任务完成 → {day_path}")
    print(f"  配置：{plan.note}")
    print(f"  稳定性：{stability.get('verdict')}（最大回撤 {stability.get('max_drawdown')}）")
    print(f"  组合净值：{risk_view.get('total_equity')}")


if __name__ == "__main__":
    main()
