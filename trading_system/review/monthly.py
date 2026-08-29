"""月度 WFA 提案（策略优化师）— 只提案不生效。

D7 迭代诚实的机器执行：
  - 调用既有 backtest/WFA 能力产出网格结果与 DSR；
  - DSR < REVIEW_DSR_SIGNIFICANT（0.95）或 OOS 期望非正或无推荐参数
    → 自动 verdict="reject"（不显著保持默认，提案不进入待审批）；
  - 显著 → verdict="pending_review"，落盘 reports/review_proposals/<id>.json
    等待三手势审批（--review-approve / --review-reject）；
  - 无论何种 verdict，本模块【绝不】写 tuned_params.json——生效动作
    只发生在 approve（chief.py），且次日生效并披露。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .. import config

logger = logging.getLogger(__name__)


@dataclass
class Proposal:
    """参数提案（WFA 产物 + DSR 校正结论 + 审批状态）。"""
    proposal_id: str
    created_at: str
    grid_result: dict          # 推荐参数 + 网格/折数摘要
    dsr: float
    oos_expectancy: float
    verdict: str               # pending_review | reject | approved | rejected
    status: str = ""           # 镜像 verdict（审批流改写此字段）
    reason: str = ""           # 自动 reject / 人工驳回原因
    oos_aggregate: dict = field(default_factory=dict)
    approved_at: str | None = None
    rejected_at: str | None = None
    effective_from: str | None = None   # approve 后次日生效日期（披露用）

    def __post_init__(self):
        if not self.status:
            self.status = self.verdict


def _proposal_path(proposals_dir: str, proposal_id: str) -> str:
    return os.path.join(proposals_dir, f"{proposal_id}.json")


def save_proposal(p: Proposal, proposals_dir: str | None = None) -> str:
    """提案落盘（会计账，跨轮累计）。"""
    d = proposals_dir or config.REVIEW_PROPOSALS_DIR
    os.makedirs(d, exist_ok=True)
    path = _proposal_path(d, p.proposal_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(p), f, ensure_ascii=False, indent=2)
    return path


def load_proposal(proposals_dir: str, proposal_id: str) -> Proposal:
    path = _proposal_path(proposals_dir, proposal_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"提案不存在: {proposal_id}（{path}）")
    with open(path, encoding="utf-8") as f:
        return Proposal(**json.load(f))


def list_proposals(proposals_dir: str | None = None) -> list[Proposal]:
    """全部提案（按创建时间升序）。"""
    d = proposals_dir or config.REVIEW_PROPOSALS_DIR
    out: list[Proposal] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out.append(Proposal(**json.load(f)))
        except Exception as exc:
            logger.warning("提案文件损坏跳过 %s: %s", name, exc)
    out.sort(key=lambda p: p.created_at)
    return out


def generate_proposal(wfa: dict,
                      proposals_dir: str | None = None) -> Proposal:
    """从 WFA 结果生成提案并落盘。

    wfa: backtest.run_wfa() 的返回（folds / oos_aggregate / dsr /
         recommended_params / n_folds / grid_size）。
    """
    now = datetime.now()
    proposal_id = f"PROP-{now.strftime('%Y%m%d-%H%M%S')}"
    d = proposals_dir or config.REVIEW_PROPOSALS_DIR
    if os.path.isdir(d):            # 同秒幂等：追加序号防撞
        seq = sum(1 for n in os.listdir(d)
                  if n.startswith(f"{proposal_id}")) 
        if seq:
            proposal_id = f"{proposal_id}-{seq + 1}"

    dsr = float(wfa.get("dsr") or 0.0)
    oos = wfa.get("oos_aggregate") or {}
    oos_exp = float(oos.get("expectancy_r") or 0.0)
    rec = wfa.get("recommended_params") or {}

    significant = (dsr >= config.REVIEW_DSR_SIGNIFICANT
                   and oos_exp > 0 and bool(rec))
    if significant:
        verdict, reason = "pending_review", ""
    else:
        verdict = "reject"
        why = []
        if dsr < config.REVIEW_DSR_SIGNIFICANT:
            why.append(f"DSR={dsr:.3f} < {config.REVIEW_DSR_SIGNIFICANT} 统计不显著")
        if oos_exp <= 0:
            why.append(f"OOS 期望 {oos_exp}R 非正")
        if not rec:
            why.append("无推荐参数（WFA 回退默认）")
        reason = "；".join(why) + "——D7 迭代诚实：保持默认参数，提案不进入待审批"

    p = Proposal(
        proposal_id=proposal_id,
        created_at=now.isoformat(timespec="seconds"),
        grid_result={
            "recommended_params": rec,
            "n_folds": wfa.get("n_folds"),
            "grid_size": wfa.get("grid_size"),
            "dsr_note": wfa.get("dsr_note", ""),
        },
        dsr=round(dsr, 4),
        oos_expectancy=round(oos_exp, 3),
        verdict=verdict,
        reason=reason,
        oos_aggregate=oos,
    )
    save_proposal(p, d)
    logger.info("月度提案 %s: verdict=%s（DSR=%.3f, OOS期望=%.3fR）",
                p.proposal_id, p.verdict, p.dsr, p.oos_expectancy)
    return p
