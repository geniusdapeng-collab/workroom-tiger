"""S6 复盘闭环测试（诸葛团队）。

覆盖：
  ① 违规六条检测命中（构造样本逐条触发 V1~V6）
  ② 周度体检三档判定（符合预期 / 关注 / 告警）
  ③ DSR 不显著 → 月度提案自动 reject（不进入待审批）
  ④ approve → 次日生效 + 披露；reject 无 reason 报错
  ⑤ 零基线审计：复盘产物不进决策输入（静态审查 + 注册表延迟环节）
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta

import pytest

from trading_system import config
from trading_system.review import monthly, weekly
from trading_system.review.attribution import (
    VIOLATIONS, attribute_journal, detect_violations,
)
from trading_system.review.chief import ReviewChief


# ---------------------------------------------------------------- 样本构造
def _rec(**kw):
    base = {
        "date": "2026-08-20", "ticker": "AAA", "mode": "标准做多",
        "template": "A", "sector": "XLK", "chain": "semis",
        "entry_ref": 100.0, "stop": 95.0, "tss_final": 7.5, "tos": 3.2,
        "time_stop_days": 7, "mrs_star": 7.0, "action": "BUY",
        "status": "closed", "entry": 100.5, "entry_date": "2026-08-21",
        "exit": 106.0, "exit_date": "2026-08-25", "r": 1.1, "win": True,
        "note": "时间止损到期平仓",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- ① 违规六条
class TestViolationSix:
    def test_v1_mrs_block_entry(self):
        """V1：MRS*<4.0 禁开仓却入场 → 命中。"""
        rec = _rec(mrs_star=3.5)
        assert "V1" in detect_violations(rec)
        rec_ok = _rec(mrs_star=6.5)
        assert "V1" not in detect_violations(rec_ok)

    def test_v2_non_mainline(self):
        """V2：无主线板块且无产业链归属 → 非主线交易。"""
        rec = _rec(sector="", chain="")
        assert "V2" in detect_violations(rec)
        assert "V2" not in detect_violations(_rec())

    def test_v3_chase_under_no_chase(self):
        """V3：当日快照禁追高（广度<40 仅权重拉动）仍入场 → 命中。"""
        rec = _rec()
        assert "V3" in detect_violations(rec, {"no_chase": True})
        assert "V3" not in detect_violations(rec, {"no_chase": False})
        assert "V3" not in detect_violations(rec)          # 无快照不猜

    def test_v4_earnings_not_reduced(self):
        """V4：财报 1-2 天内标的仍以标准仓入场（未降仓）→ 命中。"""
        rec = _rec(mode="标准做多")
        assert "V4" in detect_violations(rec, {"earnings": ["AAA"]})
        light = _rec(mode="轻仓试错")
        assert "V4" not in detect_violations(light, {"earnings": ["AAA"]})

    def test_v5_stop_not_executed(self):
        """V5：持仓浮动 R ≤ -1R（已穿止损）仍未平仓 → 触发止损不执行。"""
        rec = _rec(status="open", exit=None, exit_date=None, r=None,
                   win=None, r_live=-1.2, protected=False)
        assert "V5" in detect_violations(rec)
        ok = _rec(status="open", exit=None, exit_date=None, r=None,
                  win=None, r_live=-0.5, protected=False)
        assert "V5" not in detect_violations(ok)

    def test_v6_no_protection_above_2r(self):
        """V6：浮动 ≥2R 但未启动盈利保护 → 命中。"""
        rec = _rec(status="open", exit=None, exit_date=None, r=None,
                   win=None, r_live=2.3, protected=False)
        assert "V6" in detect_violations(rec)
        protected = _rec(status="open", exit=None, exit_date=None, r=None,
                         win=None, r_live=2.3, protected=True)
        assert "V6" not in detect_violations(protected)

    def test_void_record_never_flagged(self):
        """作废/未入场记录不判违规（未成交即无流程违规）。"""
        rec = _rec(status="void", entry=None, entry_date=None,
                   exit=None, exit_date=None, r=None, win=None,
                   mrs_star=3.0, sector="", chain="")
        assert detect_violations(rec, {"no_chase": True,
                                       "earnings": ["AAA"]}) == []

    def test_journal_aggregation_counts(self):
        """批量归因：命中即标记并计数，进 flagged 明细。"""
        records = [
            _rec(ticker="V1T", mrs_star=3.0),
            _rec(ticker="V2T", sector="", chain=""),
            _rec(ticker="OK"),
        ]
        out = attribute_journal(records, {"2026-08-20": {"no_chase": True}})
        assert out["violation_counts"]["V1"] == 1
        assert out["violation_counts"]["V2"] == 1
        assert out["violation_counts"]["V3"] == 3        # 当日快照禁追高，三笔皆中
        assert out["total_violations"] == 5
        tickers = {f["ticker"] for f in out["flagged"]}
        assert tickers == {"V1T", "V2T", "OK"}
        assert set(VIOLATIONS) == {"V1", "V2", "V3", "V4", "V5", "V6"}


# ---------------------------------------------------------------- ② 周度体检三档
class TestWeeklyVerdict:
    def _closed(self, rs):
        return [_rec(ticker=f"T{i}", r=r, win=r > 0) for i, r in enumerate(rs)]

    def test_alert(self):
        """告警：期望 ≤0 或胜率 <30%。"""
        recs = self._closed([-1.0] * 8 + [0.5] * 4)     # 期望<0, 胜率33%
        out = weekly.weekly_checkup(recs)
        assert out["verdict"] == "告警"

    def test_warn_low_sample(self):
        """关注：样本 <10（积累中，不作结论性判断）。"""
        recs = self._closed([1.5, 1.0, -0.8])
        out = weekly.weekly_checkup(recs)
        assert out["verdict"] == "关注"
        assert "样本" in out["reasons"][0]

    def test_warn_marginal(self):
        """关注：样本够但期望/胜率触关注线。"""
        recs = self._closed([2.0] * 4 + [-1.0] * 8)     # 胜率33%<40% 但期望=0
        out = weekly.weekly_checkup(recs)
        assert out["verdict"] in ("关注", "告警")

    def test_pass(self):
        """符合预期：样本充足且期望/胜率均在阈值之上。"""
        recs = self._closed([1.2] * 6 + [-0.6] * 4 + [0.8] * 2)  # 胜率67% 期望0.58
        out = weekly.weekly_checkup(recs)
        assert out["verdict"] == "符合预期"
        assert out["stats"]["closed"] == 12
        assert out["r_distribution"]["1~2R"] == 6

    def test_calibration_linkage(self):
        """校准层联动：样本状态 n/50 如实带进体检输出。"""
        cal = {"status": "accumulating", "n": 12, "min_samples": 50,
               "disclosure": "校准样本积累中（12/50）"}
        out = weekly.weekly_checkup(self._closed([1.0] * 12), cal)
        assert out["calibration"]["n"] == 12
        assert out["calibration"]["min_samples"] == 50


# ---------------------------------------------------------------- ③ DSR 不显著自动 reject
def _wfa(dsr, oos_exp, rec):
    return {"dsr": dsr, "recommended_params": rec,
            "oos_aggregate": {"expectancy_r": oos_exp, "trades": 20,
                              "win_rate": 0.5, "sharpe": 1.0, "max_dd": 0.05},
            "n_folds": 3, "grid_size": 27, "dsr_note": f"DSR={dsr}"}


class TestMonthlyProposal:
    def test_insignificant_auto_reject(self, tmp_path):
        """DSR<0.95 → 自动 reject，不进入待审批队列（D7 迭代诚实）。"""
        p = monthly.generate_proposal(
            _wfa(0.40, 0.15, {"mrs_gate": 6.5}), str(tmp_path))
        assert p.verdict == "reject"
        assert p.status == "reject"
        assert "不显著" in p.reason
        pending = [x for x in monthly.list_proposals(str(tmp_path))
                   if x.status == "pending_review"]
        assert pending == []
        # 落盘留痕（会计账），但绝不写 tuned_params.json
        assert (tmp_path / f"{p.proposal_id}.json").exists()
        assert not (tmp_path / "tuned_params.json").exists()

    def test_negative_oos_auto_reject(self, tmp_path):
        """OOS 期望非正 → 自动 reject。"""
        p = monthly.generate_proposal(
            _wfa(0.97, -0.05, {"mrs_gate": 6.5}), str(tmp_path))
        assert p.verdict == "reject"

    def test_significant_pending_review(self, tmp_path):
        """DSR≥0.95 且 OOS 期望为正 → pending_review 待审批。"""
        p = monthly.generate_proposal(
            _wfa(0.97, 0.20, {"mrs_gate": 6.5}), str(tmp_path))
        assert p.verdict == "pending_review"
        assert p.status == "pending_review"
        assert not (tmp_path / "tuned_params.json").exists()  # 只提案不生效


# ---------------------------------------------------------------- ④ 审批流
class TestApprovalFlow:
    def _pending(self, tmp_path):
        chief = ReviewChief(out_dir=str(tmp_path),
                            proposals_dir=str(tmp_path / "props"))
        p = monthly.generate_proposal(
            _wfa(0.97, 0.20, {"mrs_gate": 6.5}), str(tmp_path / "props"))
        return chief, p

    def test_approve_next_day_effective_and_disclosure(self, tmp_path):
        """approve → approved + tuned_params.json（次日生效 + 披露）。"""
        chief, p = self._pending(tmp_path)
        tuned = str(tmp_path / "tuned_params.json")
        p2 = chief.approve(p.proposal_id, tuned_path=tuned)
        assert p2.status == "approved"
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        assert p2.effective_from == tomorrow
        blob = json.load(open(tuned, encoding="utf-8"))
        assert blob["params"] == {"mrs_gate": 6.5}
        assert blob["effective_from"] == tomorrow
        assert blob["proposal_id"] == p.proposal_id
        assert "披露" in blob["disclosure"] or "生效" in blob["disclosure"]

        # 次日生效纪律：今日 apply_tuned_params 拒绝加载
        from trading_system.backtest import apply_tuned_params
        assert apply_tuned_params(tuned) is None
        # 生效日已到（模拟昨天批准）→ 正常加载
        blob["effective_from"] = (
            datetime.now().date() - timedelta(days=1)).isoformat()
        json.dump(blob, open(tuned, "w"), ensure_ascii=False)
        applied = apply_tuned_params(tuned)
        assert applied == {"mrs_gate": 6.5}

    def test_approve_twice_blocked(self, tmp_path):
        chief, p = self._pending(tmp_path)
        chief.approve(p.proposal_id, tuned_path=str(tmp_path / "t.json"))
        with pytest.raises(ValueError):
            chief.approve(p.proposal_id, tuned_path=str(tmp_path / "t.json"))

    def test_reject_requires_reason(self, tmp_path):
        """reject 无 reason → 报错（三手势纪律：驳回必填原因）。"""
        chief, p = self._pending(tmp_path)
        with pytest.raises(ValueError, match="原因"):
            chief.reject(p.proposal_id, "")
        with pytest.raises(ValueError, match="原因"):
            chief.reject(p.proposal_id, None)
        p2 = chief.reject(p.proposal_id, "OOS 样本仅 20 笔，再观察一个月")
        assert p2.status == "rejected"
        assert p2.reason == "OOS 样本仅 20 笔，再观察一个月"
        # 驳回后不得再生效
        assert not (tmp_path / "tuned_params.json").exists()

    def test_approve_emits_governance_event(self, tmp_path):
        """approve/reject 进治理五元事件（object=报告，哈希链可验）。"""
        from trading_system.governance_bridge import GovernanceBridge
        bridge = GovernanceBridge(str(tmp_path / "events.jsonl"))
        chief = ReviewChief(out_dir=str(tmp_path),
                            proposals_dir=str(tmp_path / "props"),
                            bridge=bridge)
        p = monthly.generate_proposal(
            _wfa(0.97, 0.20, {"mrs_gate": 6.5}), str(tmp_path / "props"))
        chief.approve(p.proposal_id, tuned_path=str(tmp_path / "t.json"))
        ok, errs = GovernanceBridge.verify_chain(str(tmp_path / "events.jsonl"))
        assert ok, errs
        lines = [json.loads(x) for x in
                 open(tmp_path / "events.jsonl", encoding="utf-8")]
        actions = [x["payload"]["decision"]["action"] for x in lines]
        assert "review.proposal.approve" in actions
        assert lines[-1]["payload"]["object"]["type"] == "report"


# ---------------------------------------------------------------- 复盘纪要
class TestDailyMemo:
    def test_daily_memo_generated(self, tmp_path):
        """日度复盘：纪要落盘 + 归因/违规/校准章节齐全 + 治理事件。"""
        from trading_system.governance_bridge import GovernanceBridge
        journal = tmp_path / "journal.json"
        journal.write_text(json.dumps([
            _rec(ticker="AAA", mrs_star=7.2, r=1.1),
            _rec(ticker="BBB", mrs_star=3.0, sector="", chain="", r=-1.0,
                 win=False, note="止损离场"),
        ], ensure_ascii=False), encoding="utf-8")
        bridge = GovernanceBridge(str(tmp_path / "events.jsonl"))
        chief = ReviewChief(out_dir=str(tmp_path),
                            journal_path=str(journal), bridge=bridge)
        out = chief.daily("2026-08-28", include_weekly=True)
        memo = open(out["memo_path"], encoding="utf-8").read()
        assert "复盘纪要 — 2026-08-28" in memo
        assert "日度归因" in memo and "违规六条" in memo
        assert "校准" in memo and "周度统计体检" in memo
        assert "参数提案" in memo
        assert out["attribution"]["violation_counts"]["V1"] == 1
        assert out["attribution"]["violation_counts"]["V2"] == 1
        ok, _ = GovernanceBridge.verify_chain(str(tmp_path / "events.jsonl"))
        assert ok


# ---------------------------------------------------------------- ⑤ 零基线审计
class TestZeroBaselineAudit:
    def test_review_package_no_decision_imports(self):
        """静态审查：review 包不得 import 决策链路（gate/agents/pipeline/
        simulator/backtest），不得改 config 闸门——复盘只读会计账。"""
        from trading_system.review import attribution, chief, monthly, weekly
        for mod in (attribution, weekly, monthly, chief):
            src = inspect.getsource(mod)
            for forbidden in ("from ..gate import", "from ..agents import",
                              "from ..pipeline import", "from ..simulator import",
                              "from ..backtest import", "from .gate import",
                              "OPEN_LONG[\"mrs\"] =", "OPEN_LONG[\"tss\"] =",
                              "config.SHS_MAIN_POOL =",
                              "config.MRS_GATE_BLOCK ="):
                assert forbidden not in src, f"{mod.__name__} 疑似决策路径: {forbidden}"

    def test_pipeline_not_consuming_review_artifacts(self):
        """pipeline/main 决策链路不得读取复盘产物（提案/纪要）。"""
        from trading_system import pipeline
        src = inspect.getsource(pipeline)
        for forbidden in ("review_proposals", "复盘_",
                          "from .review", "import review"):
            assert forbidden not in src, f"pipeline 疑似消费复盘产物: {forbidden}"

    def test_review_step_registered_deferred(self):
        """review.daily 注册进 STEP_REGISTRY 且为延迟环节（pipeline 外编排）。"""
        from trading_system.redline import DEFERRED_STEPS, STEP_REGISTRY
        names = [s.name for s in STEP_REGISTRY]
        assert "review.daily" in names
        assert "review.daily" in DEFERRED_STEPS
        spec = next(s for s in STEP_REGISTRY if s.name == "review.daily")
        assert spec.driver == "rules" and spec.owner == "review.chief"
        assert names.index("report.emit") < names.index("review.daily")

    def test_deferred_step_pipeline_marks_deferred(self):
        """demo pipeline：review.daily 记 deferred 占位；全环节无一遗漏。"""
        from trading_system.pipeline import run_pipeline
        from trading_system.redline import STEP_REGISTRY
        r = run_pipeline(provider_name="demo", universe_mode="core",
                         top_n=8, max_picks=3)
        redline = {s["step"]: s for s in r.raw["redline"]}
        assert set(redline) == {s.name for s in STEP_REGISTRY}
        assert redline["review.daily"]["status"] == "deferred"
