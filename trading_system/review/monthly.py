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
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .. import config
from ..ledger_io import write_json_atomic

logger = logging.getLogger(__name__)

# Approval recovery records are deliberately not .json proposal entries.
APPROVAL_TRANSACTION = ".approval-transaction"


@contextmanager
def _approval_lock(path: Path):
    """Serialize cooperating proposal readers/writers; contention fails closed.

    Keep the lock file in place: unlinking it would let a new writer lock a
    different inode while the previous writer still holds the old one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _ensure_no_approval_transaction(proposals_dir: str) -> None:
    """Incomplete two-file commits must not be presented as approved proposals."""
    path = os.path.join(proposals_dir, APPROVAL_TRANSACTION)
    try:
        os.stat(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("审批恢复记录检查失败: %s", path)
        raise
    logger.error("审批事务待恢复，暂不读取或修改提案: %s", path)
    raise RuntimeError(f"审批事务待恢复: {path}；请重试原审批或驳回操作")


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
    try:
        with _approval_lock(Path(d).resolve() / ".approval.lock"):
            _ensure_no_approval_transaction(d)
            return _save_proposal_unlocked(p, d)
    except Exception:
        logger.exception("提案 %s 保存失败: %s", p.proposal_id, d)
        raise


def _save_proposal_unlocked(p: Proposal, proposals_dir: str) -> str:
    """Caller must hold the proposal-directory lock."""
    path = _proposal_path(proposals_dir, p.proposal_id)
    write_json_atomic(path, asdict(p))
    return path


def _create_proposal_unlocked(p: Proposal, proposals_dir: str) -> None:
    """Choose and publish a new ID while holding the proposal-directory lock.

    Every existing directory entry reserves its ID, including corrupt JSON,
    directories and dangling symlinks. Publish complete bytes with a hard link
    so an unexpected target created after selection also cannot be replaced.
    Filesystems without hard-link support fail explicitly.
    """
    base_id = p.proposal_id
    sequence = 1
    while True:
        target = Path(_proposal_path(proposals_dir, p.proposal_id))
        try:
            target.lstat()
        except FileNotFoundError:
            break
        sequence += 1
        p.proposal_id = f"{base_id}-{sequence}"

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(asdict(p), handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_proposal(proposals_dir: str, proposal_id: str) -> Proposal:
    try:
        with _approval_lock(Path(proposals_dir).resolve() / ".approval.lock"):
            return _load_proposal_unlocked(proposals_dir, proposal_id)
    except Exception:
        logger.exception("提案 %s 读取失败", proposal_id)
        raise


def _load_proposal_unlocked(proposals_dir: str, proposal_id: str) -> Proposal:
    """Caller must hold the proposal-directory lock."""
    _ensure_no_approval_transaction(proposals_dir)
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
    try:
        with _approval_lock(Path(d).resolve() / ".approval.lock"):
            _ensure_no_approval_transaction(d)
            for name in sorted(os.listdir(d)):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        out.append(Proposal(**json.load(f)))
                except Exception as exc:
                    logger.warning("提案文件损坏跳过 %s: %s", name, exc)
    except Exception:
        logger.exception("提案列表读取失败: %s", d)
        raise
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
    try:
        root = Path(d).resolve()
        with _approval_lock(root / ".approval.lock"):
            _ensure_no_approval_transaction(str(root))
            _create_proposal_unlocked(p, str(root))
    except Exception:
        logger.exception("提案 %s 创建失败: %s", p.proposal_id, d)
        raise
    logger.info("月度提案 %s: verdict=%s（DSR=%.3f, OOS期望=%.3fR）",
                p.proposal_id, p.verdict, p.dsr, p.oos_expectancy)
    return p
