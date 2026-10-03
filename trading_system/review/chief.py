"""诸葛（复盘主持）— 编排日/周/月三个频率，产出复盘纪要并进治理事件。

频率编排（UPGRADE_PLAN_v3 §5 复盘闭环）：
  日度  归因 + 违规六条标记 + 校准层状态 → reports/复盘_<日期>.md
  周度  统计体检（三档结论）→ 并入当周末的复盘纪要
  月度  WFA 提案摘要 + 待审批提示（提案本体见 monthly.py）

审批流（三手势对齐 WorkLoom review-console）：
  approve  提案状态 → approved，写 tuned_params.json（effective_from=次日），
           复用 --use-tuned 纪律：生效动作写日报披露 + 治理事件；
  reject   必须填原因（驳回样本回流组织记忆），状态 → rejected；
  模拟盘阶段审批不阻塞 pipeline 运行（只影响参数提案生效）。

隔离性：本模块只读会计账（journal/校准样本/提案），产出只进报告层与
治理事件，绝不进入决策输入（D5 零基线）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timedelta

from .. import config
from ..ledger_io import (file_digest, file_transaction, read_json_strict,
                         write_bytes_atomic, write_json_atomic)
from ..parameters import validate_tuned_params
from . import monthly, weekly
from .attribution import VIOLATIONS, attribute_journal
from .monthly import Proposal, list_proposals, load_proposal, safe_identifier

logger = logging.getLogger(__name__)


def _read_journal(path: str) -> list[dict]:
    from ..calibration import validate_accounting_rows
    try:
        data = read_json_strict(path)
    except FileNotFoundError:
        return []
    validate_accounting_rows(data)
    return data


class ReviewChief:
    """复盘主持：三个频率编排 + 纪要落盘 + 治理事件 + 审批流。"""

    def __init__(self, out_dir: str = config.REPORTS_DIR,
                 journal_path: str | None = None,
                 proposals_dir: str | None = None,
                 bridge=None):
        self.out_dir = out_dir
        self.journal_path = journal_path or os.path.join(out_dir, "journal.json")
        self.proposals_dir = proposals_dir or os.path.join(
            out_dir, "review_proposals")
        self.bridge = bridge        # GovernanceBridge（可选，治理旁路）

    # ------------------------------------------------------------ 治理事件

    def _emit(self, action: str, object_id: str, basis: list[str],
              after: dict | None = None, rule: tuple[str, str] = ("R-T15", "pass")):
        """纪要/审批动作进治理五元事件（object=报告；旁路失败不阻塞）。"""
        if self.bridge is None:
            return None
        try:
            from ..governance_bridge import FiveElementEvent, RuleImpact
            ev = FiveElementEvent(
                who={"type": "agent", "id": "review.chief", "version": "v6.4"},
                context={"channel": "review", "stage": "paper"},
                object={"type": "report", "id": object_id},
                decision={"action": action, "basis": basis,
                          **({"after": after} if after else {})},
                rule_impact=[RuleImpact(rule[0], result=rule[1])],
            )
            return self.bridge.emit(ev)
        except Exception as exc:    # 治理旁路：失败只记 WARNING
            logger.warning("[复盘] 治理事件写入失败（不阻塞）: %s", exc)
            return None

    # ------------------------------------------------------------ 日度

    def daily(self, trade_date: str | None = None,
              snapshots: dict[str, dict] | None = None,
              include_weekly: bool | None = None) -> dict:
        """日度复盘：归因 + 违规标记 + 校准状态 → 复盘纪要。

        include_weekly=None 时按日历自动判定（周六/日并入周度体检）。
        返回 {memo_path, attribution, calibration, weekly?, ms}。
        """
        t0 = time.perf_counter()
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        records = _read_journal(self.journal_path)
        att = attribute_journal(records, snapshots)

        # 校准层状态（联动：更新样本库并取 n/50 披露；与 pipeline 同口径）
        try:
            from ..calibration import CalibrationLayer
            cal = CalibrationLayer(
                journal_path=self.journal_path,
                samples_path=os.path.join(self.out_dir,
                                          "calibration_samples.json"),
            ).run()
        except Exception as exc:
            cal = {"status": "skipped", "reason": str(exc),
                   "n": 0, "min_samples": config.CALIBRATION_MIN_SAMPLES,
                   "disclosure": f"校准层异常（已记录）: {exc}"}

        # 周度体检：周末自动并入（或显式要求）
        if include_weekly is None:
            include_weekly = datetime.strptime(
                trade_date, "%Y-%m-%d").weekday() >= 5
        weekly_out = None
        if include_weekly:
            weekly_out = weekly.weekly_checkup(records, cal)

        pending = [p for p in list_proposals(self.proposals_dir)
                   if p.status == "pending_review"]
        memo = self._render_daily(trade_date, records, att, cal,
                                  weekly_out, pending)
        memo_path = os.path.join(
            self.out_dir, f"复盘_{trade_date.replace('-', '')}.md")
        os.makedirs(self.out_dir, exist_ok=True)
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write(memo)

        self._emit(
            action="review.daily", object_id=f"复盘_{trade_date}",
            basis=[f"归因 {len(att['attributions'])} 笔",
                   f"违规命中 {att['total_violations']} 条",
                   f"校准: {cal.get('disclosure', '')}",
                   f"纪要: {memo_path}"]
            + ([f"周度体检: {weekly_out['verdict']}"] if weekly_out else []),
            after={"memo": memo_path,
                   "violations": att["violation_counts"],
                   "calibration_n": cal.get("n", 0)},
        )
        return {"memo_path": memo_path, "attribution": att,
                "calibration": cal, "weekly": weekly_out,
                "pending": [p.proposal_id for p in pending],
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # ------------------------------------------------------------ 周度

    def weekly(self) -> dict:
        """周度体检（统计员），结论进治理事件。"""
        records = _read_journal(self.journal_path)
        out = weekly.weekly_checkup(records)
        self._emit(action="review.weekly",
                   object_id=f"周度体检_{datetime.now():%Y-%m-%d}",
                   basis=[f"结论: {out['verdict']}"] + out["reasons"],
                   after={"verdict": out["verdict"]},
                   rule=("R-T15", "pass" if out["verdict"] != "告警" else "review"))
        return out

    # ------------------------------------------------------------ 月度

    def monthly(self, wfa: dict) -> Proposal:
        """月度 WFA 提案（策略优化师）：只提案不生效，结论进治理事件。"""
        p = monthly.generate_proposal(wfa, self.proposals_dir)
        self._emit(
            action="review.monthly.proposal", object_id=p.proposal_id,
            basis=[f"verdict={p.verdict}", f"DSR={p.dsr}",
                   f"OOS期望={p.oos_expectancy}R",
                   p.reason or "统计显著，待三手势审批"],
            after={"verdict": p.verdict, "dsr": p.dsr},
            rule=("R-T15", "review" if p.verdict == "pending_review" else "pass"))
        return p

    # ------------------------------------------------------------ 审批流

    def _paths(self, proposal_id: str, execution_id: str, tuned_path: str | None):
        safe_identifier(proposal_id)
        safe_identifier(execution_id)
        root = Path(self.out_dir).absolute()
        proposals = Path(self.proposals_dir).absolute()
        if root.is_symlink() or proposals.is_symlink() or not proposals.resolve().is_relative_to(root.resolve()):
            raise ValueError("Review paths must stay inside the selected output directory")
        proposal = Path(monthly._proposal_path(str(proposals), proposal_id))
        execution_dir = root / "review_executions"
        if execution_dir.is_symlink():
            raise ValueError("Execution directory cannot be a symlink")
        receipt = execution_dir / f"{execution_id}.json"
        effect = execution_dir / f"{execution_id}.effect.json"
        intent = execution_dir / f"{execution_id}.intent.json"
        active = Path(tuned_path or (root / "tuned_params.json")).absolute()
        if (active.parent.resolve() != root.resolve() or active.is_symlink()
                or any(path.is_symlink() for path in (proposal, receipt, effect, intent))):
            raise ValueError("Review effect and receipt paths must stay inside the selected output directory")
        return root, proposal, receipt, effect, intent, active

    @staticmethod
    def _hash_params(params: dict) -> str:
        raw = json.dumps(params, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _snapshot_hash(expected_sha256: str | None) -> str:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
            raise ValueError("Approval requires the reviewed proposal SHA256 snapshot")
        return expected_sha256.lower()

    def _execute(self, action: str, proposal_id: str, *, expected_sha256: str | None,
                 execution_id: str | None, tuned_path: str | None = None,
                 reason: str = "") -> Proposal:
        expected = self._snapshot_hash(expected_sha256)
        root, proposal_path, receipt_path, effect_path, intent_path, active_path = self._paths(
            proposal_id, execution_id, tuned_path)
        with file_transaction(root / ".review-state"), file_transaction(proposal_path):
            p = load_proposal(self.proposals_dir, proposal_id)
            binding = {"proposal_id": proposal_id, "execution_id": execution_id,
                       "preimage_sha256": expected, "status": action}
            # An execution identity is immutable. A retry may recover a crash,
            # but cannot approve a different snapshot or replace a later effect.
            if receipt_path.exists():
                receipt = read_json_strict(receipt_path)
                intent = read_json_strict(intent_path)
                if (not isinstance(intent, dict) or receipt != intent.get("receipt")
                        or file_digest(receipt_path) != hashlib.sha256(json.dumps(
                            intent["receipt"], ensure_ascii=False, indent=2,
                            allow_nan=False).encode("utf-8")).hexdigest()):
                    raise ValueError("Receipt differs from its immutable prepared execution")
                if (str(active_path) != intent.get("active_path")
                        or reason != intent.get("reason", "")):
                    raise ValueError("Execution retry parameters differ from the original review")
                if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in binding.items()):
                    raise ValueError("Execution id is already bound to another review action")
                if (p.status != action or p.execution_id != execution_id
                        or p.preimage_sha256 != expected
                        or file_digest(proposal_path) != receipt.get("proposal_sha256")):
                    raise ValueError("Receipt and proposal status differ")
                if action == "approved" and file_digest(effect_path) != receipt.get("tuned_sha256"):
                    raise ValueError("Approved immutable effect differs from its receipt")
                return p

            if intent_path.exists():
                intent = read_json_strict(intent_path)
                if not isinstance(intent, dict) or any(intent.get(k) != v for k, v in binding.items()):
                    raise ValueError("Execution id is already bound to another review intent")
                if str(active_path) != intent["active_path"] or reason != intent.get("reason", ""):
                    raise ValueError("Execution retry parameters differ from the original review")
                if p.status != "pending_review" and not (
                        p.status == action and p.execution_id == execution_id
                        and p.preimage_sha256 == expected):
                    raise ValueError("A later decision has already consumed this proposal")
                if p.status == "pending_review" and file_digest(proposal_path) != expected:
                    raise ValueError("Reviewed proposal snapshot changed before execution")
            else:
                if p.status != "pending_review":
                    raise ValueError(f"提案 {proposal_id} 状态为 {p.status}，仅 pending_review 可审批")
                if file_digest(proposal_path) != expected:
                    raise ValueError("Reviewed proposal snapshot changed before execution")
                expiry = datetime.fromisoformat(p.expires_at)
                if datetime.now(expiry.tzinfo) >= expiry:
                    raise ValueError("Reviewed proposal has expired; generate and review a new proposal")
                params = validate_tuned_params(p.grid_result.get("recommended_params"))
                if p.dsr < config.REVIEW_DSR_SIGNIFICANT or p.oos_expectancy <= 0:
                    raise ValueError("Proposal evidence does not meet the approval gate")
                now = datetime.now().isoformat(timespec="seconds")
                effective = (datetime.now().date() + timedelta(days=1)).isoformat() if action == "approved" else None
                old_proposal = proposal_path.read_bytes().hex()
                old_active = active_path.read_bytes().hex() if active_path.exists() else None
                p.status = p.verdict = action
                p.execution_id, p.preimage_sha256 = execution_id, expected
                p.parameters_sha256 = self._hash_params(params)
                p.effective_from = effective
                if action == "approved":
                    p.approved_at = now
                else:
                    p.rejected_at, p.reason = now, reason
                blob = None
                if action == "approved":
                    blob = {
                        "tuned_at": now, "params": params, "oos_aggregate": p.oos_aggregate,
                        "dsr": p.dsr, **binding, "parameters_sha256": p.parameters_sha256,
                        "effective_from": effective, "proposal_path": str(proposal_path),
                        "receipt_path": str(receipt_path),
                        "disclosure": f"复盘提案 {proposal_id} 经审批批准，自 {effective} 起随 --use-tuned 显式启用（次日生效；默认不加载）；DSR={p.dsr}，OOS期望={p.oos_expectancy}R",
                    }
                    raw = json.dumps(blob, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                    p.tuned_sha256 = hashlib.sha256(raw).hexdigest()
                    p.tuned_path = str(effect_path)
                receipt = {
                    **binding, "parameters_sha256": p.parameters_sha256,
                    "effective_from": effective, "tuned_sha256": p.tuned_sha256,
                    "tuned_path": p.tuned_path, "reason": reason or None,
                    "proposal_sha256": hashlib.sha256(json.dumps(
                        asdict(p), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")).hexdigest(),
                }
                intent = {
                    **binding, "reason": reason, "active_path": str(active_path),
                    "old_proposal": old_proposal, "old_active": old_active,
                    "proposal": asdict(p), "effect": blob, "receipt": receipt,
                }
                write_json_atomic(intent_path, intent)

            original = bytes.fromhex(intent["old_proposal"])
            if hashlib.sha256(original).hexdigest() != expected:
                raise ValueError("Prepared review intent does not contain the reviewed preimage")
            original_params = validate_tuned_params(json.loads(original)["grid_result"]["recommended_params"])
            target_proposal = Proposal(**intent["proposal"])
            if (any(intent["proposal"].get(k) != v for k, v in binding.items())
                    or target_proposal.parameters_sha256 != self._hash_params(original_params)
                    or intent["receipt"].get("proposal_sha256") != hashlib.sha256(json.dumps(
                        intent["proposal"], ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")).hexdigest()):
                raise ValueError("Prepared review intent changed its approved proposal")
            if action == "approved" and (intent["effect"].get("params") != original_params
                    or any(intent["effect"].get(k) != v for k, v in binding.items())
                    or hashlib.sha256(json.dumps(intent["effect"], ensure_ascii=False,
                        indent=2, allow_nan=False).encode("utf-8")).hexdigest() != intent["receipt"]["tuned_sha256"]):
                raise ValueError("Prepared review effect changed before execution")

            try:
                if action == "approved":
                    if effect_path.exists():
                        if file_digest(effect_path) != intent["receipt"]["tuned_sha256"]:
                            raise ValueError("Immutable execution effect has been changed")
                    else:
                        write_json_atomic(effect_path, intent["effect"])
                    write_json_atomic(active_path, intent["effect"])
                write_json_atomic(proposal_path, intent["proposal"])
                write_json_atomic(receipt_path, intent["receipt"])
            except Exception as error:
                # Ordinary I/O errors restore the exact reviewed bytes. The
                # prepared intent and immutable effect allow the same id/hash
                # to recover even if the process crashes during rollback.
                try:
                    write_bytes_atomic(proposal_path, bytes.fromhex(intent["old_proposal"]))
                    if action == "approved":
                        if intent["old_active"] is None:
                            active_path.unlink(missing_ok=True)
                        else:
                            write_bytes_atomic(active_path, bytes.fromhex(intent["old_active"]))
                except Exception as rollback_error:
                    raise OSError(f"Review failed and rollback requires recovery with execution id {execution_id}: {rollback_error}") from error
                raise
            p = load_proposal(self.proposals_dir, proposal_id)
        self._emit(action=f"review.proposal.{ 'approve' if action == 'approved' else 'reject'}",
                   object_id=proposal_id,
                   basis=[f"reviewed_sha256={expected}", reason or f"effective_from={p.effective_from}"],
                   after={"status": action, "execution_id": execution_id,
                          "effective_from": p.effective_from, "tuned_params": p.tuned_path})
        return p

    def approve(self, proposal_id: str, tuned_path: str | None = None, *,
                expected_sha256: str | None = None,
                execution_id: str | None = None) -> Proposal:
        """Approve exactly the reviewed bytes and persist an immutable receipt."""
        return self._execute("approved", proposal_id, expected_sha256=expected_sha256,
                             execution_id=execution_id, tuned_path=tuned_path)

    def reject(self, proposal_id: str, reason: str | None = None, *,
               expected_sha256: str | None = None,
               execution_id: str | None = None) -> Proposal:
        """Reject exactly the reviewed bytes; a rejection reason is required."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("驳回必须填写原因（--reason），驳回样本需回流组织记忆")
        return self._execute("rejected", proposal_id, expected_sha256=expected_sha256,
                             execution_id=execution_id, reason=reason.strip())

    # ------------------------------------------------------------ 纪要渲染

    def _render_daily(self, trade_date: str, records: list[dict],
                      att: dict, cal: dict, weekly_out: dict | None,
                      pending: list[Proposal]) -> str:
        lines = [f"# 复盘纪要 — {trade_date}（诸葛团队）", ""]
        lines.append("> 口径：只复盘流程不复盘运气（附录D）；归因只基于已落账数据"
                     "与当日快照，不重构决策；本纪要属会计账，不进入决策输入（D5）。")
        lines.append("")

        # ---- 日度归因 ----
        closed = [a for a in att["attributions"] if a["status"] == "closed"]
        open_ = [a for a in att["attributions"] if a["status"] == "open"]
        lines.append("## 一、日度归因")
        lines.append("")
        lines.append(f"- 台账 {len(att['attributions'])} 笔：已结算 {len(closed)}，"
                     f"持仓中 {len(open_)}")
        if att["attributions"]:
            lines.append("")
            lines.append("| 信号日 | 标的 | 信号层（MRS区间/主线/链/模板） | "
                         "入场（滑点） | 出场类型 | R | 违规 |")
            lines.append("|---|---|---|---|---|---|---|")
            for a in att["attributions"]:
                sl = a["signal_layer"]
                eq = a["entry_quality"]
                r_show = a["r"] if a["r"] is not None else (
                    f"{a['r_live']}(浮)" if a.get("r_live") is not None else "—")
                lines.append(
                    f"| {a['date']} | {a['ticker']} "
                    f"| {sl['mrs_band']}/{sl['sector']}/{sl['chain']}/{sl['template']} "
                    f"| {eq['trigger']}"
                    + (f"（{eq['slippage']:+.2f}）" if eq["slippage"] is not None else "")
                    + f" | {a['exit_type']} | {r_show} "
                    f"| {'、'.join(a['violations']) or '—'} |")
        lines.append("")

        # ---- 违规六条 ----
        lines.append("## 二、违规六条检测（附录D，命中必标记）")
        lines.append("")
        for code, label in VIOLATIONS.items():
            n = att["violation_counts"][code]
            lines.append(f"- {code} {label}: **{n}** 条" + (" ⚠️" if n else ""))
        if att["flagged"]:
            lines.append("")
            lines.append("命中明细: " + "；".join(
                f"{f_['date']} {f_['ticker']} [{'、'.join(f_['labels'])}]"
                for f_ in att["flagged"]))
        else:
            lines.append("- 本轮无违规命中。")
        lines.append("")

        # ---- 校准状态 ----
        lines.append("## 三、校准层状态")
        lines.append("")
        lines.append(f"- {cal.get('disclosure', '校准摘要缺失')}")
        lines.append("")

        # ---- 周度体检（到期时并入）----
        if weekly_out:
            lines.append("## 四、周度统计体检")
            lines.append("")
            lines.append(f"- 结论：**{weekly_out['verdict']}**")
            for r_ in weekly_out["reasons"]:
                lines.append(f"  - {r_}")
            st = weekly_out["stats"]
            lines.append(f"- 累计 {st['closed']} 笔：胜率 {st['win_rate']:.1%}，"
                         f"期望 {st['expectancy_r']}R，总 {st['total_r']}R")
            l20, l50 = st["last20"], st["last50"]
            if l20["n"]:
                lines.append(f"- 近20笔 胜率 {l20['win_rate']:.1%}（均 {l20['avg_r']}R）"
                             + (f"；近50笔 胜率 {l50['win_rate']:.1%}" if l50["n"] else ""))
            dist = weekly_out["r_distribution"]
            lines.append("- R 分布: " + " ｜ ".join(
                f"{k} {v}" for k, v in dist.items()))
            bt = st["by_template"]
            if bt:
                lines.append("- 分模板: " + " ｜ ".join(
                    f"模板{k} {v['n']}笔/胜率{v['win_rate']:.0%}/均{v['avg_r']}R"
                    for k, v in bt.items() if v["n"]))
            lines.append("")

        # ---- 参数提案 ----
        lines.append(f"## {'五' if weekly_out else '四'}、参数提案（月度 WFA）")
        lines.append("")
        if pending:
            lines.append(f"- ⚠️ 待审批提案 **{len(pending)}** 个"
                         "（`python3 main.py --review-list` 查看，"
                         "`--review-approve/--review-reject` 审批）:")
            for p in pending:
                lines.append(f"  - {p.proposal_id}: DSR={p.dsr}，"
                             f"OOS期望={p.oos_expectancy}R，"
                             f"参数={p.grid_result.get('recommended_params')}")
        else:
            lines.append("- 无待审批提案（DSR 不显著的提案已按 D7 自动 reject，"
                         "不进入审批队列）。")
        approved = [p for p in list_proposals(self.proposals_dir)
                    if p.status == "approved"]
        for p in approved:
            lines.append(f"- 披露：提案 {p.proposal_id} 已批准，"
                         f"自 {p.effective_from} 起随 --use-tuned 生效。")
        lines.append("")
        return "\n".join(lines)
