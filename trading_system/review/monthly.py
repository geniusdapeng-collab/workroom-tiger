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
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import config
from ..ledger_io import file_transaction, read_json_strict, write_json_atomic
from ..parameters import validate_tuned_params

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
    expires_at: str | None = None
    execution_id: str | None = None
    preimage_sha256: str | None = None
    parameters_sha256: str | None = None
    tuned_sha256: str | None = None
    tuned_path: str | None = None

    def __post_init__(self):
        if not self.status:
            self.status = self.verdict
        safe_identifier(self.proposal_id)
        if (not isinstance(self.grid_result, dict) or not isinstance(self.oos_aggregate, dict)
                or self.status not in ("pending_review", "reject", "approved", "rejected")
                or isinstance(self.dsr, bool) or not isinstance(self.dsr, (int, float))
                or not math.isfinite(self.dsr) or not 0 <= self.dsr <= 1
                or isinstance(self.oos_expectancy, bool)
                or not isinstance(self.oos_expectancy, (int, float))
                or not math.isfinite(self.oos_expectancy)):
            raise ValueError("Invalid proposal schema or finite statistical evidence")
        created = datetime.fromisoformat(self.created_at)
        if self.expires_at is None:
            self.expires_at = (created + timedelta(days=7)).isoformat(timespec="seconds")
        datetime.fromisoformat(self.expires_at)


def safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("Invalid proposal or execution identifier")
    return value


def _proposal_path(proposals_dir: str, proposal_id: str) -> str:
    safe_identifier(proposal_id)
    root = Path(proposals_dir)
    target = root / f"{proposal_id}.json"
    if root.is_symlink() or target.is_symlink() or target.resolve().parent != root.resolve():
        raise ValueError("Proposal path must stay inside a non-symlink root")
    return str(target)


def save_proposal(p: Proposal, proposals_dir: str | None = None) -> str:
    """提案落盘（会计账，跨轮累计）。"""
    d = proposals_dir or config.REVIEW_PROPOSALS_DIR
    path = _proposal_path(d, p.proposal_id)
    with file_transaction(path):
        write_json_atomic(path, asdict(p))
    return path


def load_proposal(proposals_dir: str, proposal_id: str) -> Proposal:
    path = _proposal_path(proposals_dir, proposal_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"提案不存在: {proposal_id}（{path}）")
    data = read_json_strict(path)
    if not isinstance(data, dict) or data.get("proposal_id") != proposal_id:
        raise ValueError("Proposal identity differs from its filename")
    return Proposal(**data)


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
            out.append(load_proposal(d, name[:-5]))
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
    proposal_id = f"PROP-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
    d = proposals_dir or config.REVIEW_PROPOSALS_DIR

    dsr = float(wfa.get("dsr") or 0.0)
    oos = wfa.get("oos_aggregate") or {}
    oos_exp = float(oos.get("expectancy_r") or 0.0)
    rec = wfa.get("recommended_params") or {}
    if not math.isfinite(dsr) or not 0 <= dsr <= 1 or not math.isfinite(oos_exp):
        raise ValueError("WFA statistical evidence must be finite")
    invalid_params = ""
    if rec:
        try:
            rec = validate_tuned_params(rec)
        except ValueError as exc:
            invalid_params, rec = str(exc), {}

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
        if invalid_params:
            why.append(f"参数无效：{invalid_params}")
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
