"""多空辩论证据层测试（v6.3 S2，桥水 AIA 辩论制）。

覆盖四条铁律：
  ① 辩论启用/禁用两种配置下，L4 闸门放行结果【逐位一致】（辩论绝不改分）；
  ② LLM 故障注入 → 红线透传，且报告交易卡片第六段标注"本轮无辩论证据"；
  ③ 触发范围正确：标准 BUY / AVOID 日不触发，灰区（轻仓/HOLD 高分）触发，
     每日上限生效；
  ④ 证伪条件非空约束：LLM 产出缺证伪条件 = 无效产出，丢弃。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_system import config
from trading_system.agents.debate_agent import (
    DebateAgent, DebateEvidence, select_debate_targets,
)
from trading_system.redline import LLM_STEPS, LLMUnavailable, Passthrough


# ---------------------------------------------------------------- 工具
def _pick(ticker="AAA", tss=8.0, tos=5.0):
    return SimpleNamespace(ticker=ticker, tss_final=tss, tos=tos,
                           entry_template="A", stop_price=90.0,
                           chain="semis", sector="SMH", time_stop_days=7)


def _cand(ticker="AAA", tss=8.0):
    return SimpleNamespace(ticker=ticker, tss_final=tss, entry_template="A",
                           chain_id="semis", sector_etf="SMH", stop_plan="跌破关键位")


class _FakeLLM:
    """可编程 LLM：mode=ok 返回合法辩论；mode=no_fals 缺证伪条件；mode=down 抛故障。"""

    def __init__(self, mode="ok"):
        self.mode = mode

    def complete_json(self, *, system, user, schema_hint, max_tokens=1200,
                      temperature=0.2):
        if self.mode == "down":
            raise LLMUnavailable("注入故障：LLM 通道不可用")
        if self.mode == "no_fals":
            return {"bull_points": ["业绩超预期"], "bear_points": ["估值过高"],
                    "coordinator_verdict": "存疑", "verdict_reason": "各执一词",
                    "falsification_conditions": []}
        return {"bull_points": ["主业订单强劲", "板块资金流入"],
                "bear_points": ["毛利率环比下滑", "财报临近事件风险"],
                "coordinator_verdict": "存疑",
                "verdict_reason": "多空证据均有文档支撑，事件前不确定性高",
                "falsification_conditions": ["跌破入场日低点", "财报指引低于预期"]}


# ---------------------------------------------------------------- ① 核心铁律：闸门结果逐位一致
def test_gate_output_bit_identical_with_debate_on_off(tmp_path, monkeypatch):
    """辩论启用/禁用两种配置下，L4 放行结果（含交易卡片）必须逐位一致。"""
    from trading_system.pipeline import run_pipeline

    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH",
                        str(tmp_path / "no_journal.json"))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH",
                        str(tmp_path / "samples.json"))

    def snapshot(r):
        return {
            "action": r.action, "market_view": r.market_view,
            "mrs_star": r.mrs.mrs_star,
            "picks": [(p.ticker, p.tss_final, p.tos, p.entry_template,
                       p.entry_price, p.stop_price, p.shares, p.position_pct,
                       p.risk_usd, p.card, p.time_stop_days) for p in r.picks],
            "watchlist": [(c.ticker, c.tss_final, c.tos) for c in r.watchlist],
        }

    monkeypatch.setattr(config, "DEBATE_ENABLED", True)
    r_on = run_pipeline(provider_name="demo", universe_mode="core",
                        top_n=8, max_picks=3)
    monkeypatch.setattr(config, "DEBATE_ENABLED", False)
    r_off = run_pipeline(provider_name="demo", universe_mode="core",
                         top_n=8, max_picks=3)
    assert snapshot(r_on) == snapshot(r_off)
    # 禁用时辩论环节仍被点名（红线 1：环节无一遗漏）
    steps = {s["step"] for s in r_off.raw["redline"]}
    assert "decision.debate" in steps
    assert r_off.raw["debate"]["status"] == "disabled"


# ---------------------------------------------------------------- ② LLM 故障 → 透传 + 报告标注
def test_llm_failure_passthrough_and_report_marks_no_debate():
    agent = DebateAgent(_FakeLLM("down"))
    targets = [{"symbol": "AAA", "tss_final": 8.0, "mode": "轻仓试错",
                "template": "A", "chain": "semis", "sector": "SMH",
                "draft": "测试草稿"}]
    out = agent.execute(targets, docs=[])
    assert isinstance(out, Passthrough)           # 唯一合法出口，绝非规则回退
    assert out.origin == "decision.debate"

    # 报告层：触发但透传 → 交易卡片第六段标注"本轮无辩论证据"
    from trading_system.data_models import PipelineResult, TradePick
    from trading_system.report import render_markdown
    r = PipelineResult(
        trade_date="2026-08-29", provider="demo", action="HOLD",
        picks=[TradePick(ticker="AAA", tss_final=8.0, tos=5.0,
                         entry_template="A", entry_price=100.0, stop_price=90.0,
                         shares=80, position_pct=0.08, risk_usd=800.0,
                         card="【交易计划】AAA（轻仓试错）")],
        raw={"debate": {"status": "passthrough", "triggered": ["AAA"],
                        "evidence": {}}})
    md = render_markdown(r)
    assert "本轮无辩论证据" in md and "红线透传" in md


# ---------------------------------------------------------------- ③ 触发范围
def test_trigger_scope_standard_buy_not_debated():
    picks = [_pick("STD"), _pick("LIGHT")]
    rationale = {"STD": {"standard": True, "mode": "标准做多"},
                 "LIGHT": {"standard": False, "mode": "轻仓试错",
                           "shs": 7.2, "mrs_star": 5.7}}
    targets = select_debate_targets(picks, rationale, [], action="BUY")
    assert [t["symbol"] for t in targets] == ["LIGHT"]   # 标准 BUY 不辩论


def test_trigger_scope_avoid_day_not_debated():
    targets = select_debate_targets([], {}, [_cand("HOT", tss=8.5)],
                                    action="AVOID")
    assert targets == []                              # AVOID 日不触发


def test_trigger_scope_hold_zone_high_score():
    cands = [_cand("HOT", tss=8.2), _cand("MID", tss=7.5)]
    targets = select_debate_targets([], {}, cands, action="HOLD")
    assert [t["symbol"] for t in targets] == ["HOT"]  # 仅 ≥7.8 触发


def test_daily_cap_enforced():
    picks = [_pick(f"L{i}", tos=10 - i) for i in range(6)]
    rationale = {f"L{i}": {"standard": False, "mode": "轻仓试错"}
                 for i in range(6)}
    targets = select_debate_targets(picks, rationale, [], action="HOLD")
    assert len(targets) == config.DEBATE_MAX_PER_DAY  # 每日上限生效


# ---------------------------------------------------------------- ④ 证伪条件非空约束
def test_falsification_required_else_dropped():
    agent = DebateAgent(_FakeLLM("no_fals"))
    targets = [{"symbol": "AAA", "tss_final": 8.0, "mode": "轻仓试错",
                "template": "A", "chain": "semis", "sector": "SMH",
                "draft": "测试"}]
    out = agent.execute(targets, docs=[])
    assert isinstance(out, Passthrough)             # 全部无效 → 透传兜底


def test_valid_debate_evidence_structure():
    agent = DebateAgent(_FakeLLM("ok"))
    targets = [{"symbol": "AAA", "tss_final": 8.0, "mode": "轻仓试错",
                "template": "A", "chain": "semis", "sector": "SMH",
                "draft": "测试"}]
    out = agent.execute(targets, docs=[])
    assert isinstance(out, dict) and "AAA" in out
    ev: DebateEvidence = out["AAA"]
    assert ev.symbol == "AAA" and ev.llm_used is True
    assert ev.bull_points and ev.bear_points
    assert ev.coordinator_verdict in ("证据充分", "存疑", "反对")
    assert ev.falsification_conditions              # 证伪条件非空
    d = ev.to_dict()
    assert d["symbol"] == "AAA" and d["llm_used"] is True


# ---------------------------------------------------------------- 注册表
def test_debate_registered_as_llm_step():
    from trading_system.redline import STEP_REGISTRY
    assert "decision.debate" in LLM_STEPS
    order = [s.name for s in STEP_REGISTRY]
    assert order.index("layer4.risk") < order.index("decision.debate") \
        < order.index("report.emit")


def test_debate_agent_has_no_score_mutation_path():
    """静态审查：辩论 Agent 源码不得出现任何分数/闸门字段的写入路径。"""
    import inspect

    from trading_system.agents import debate_agent
    src = inspect.getsource(debate_agent)
    for forbidden in ["tss_final =", "mrs_star =", ".tos =", "pass_gates",
                      "position_cap", "allow_new_positions"]:
        assert forbidden not in src, f"辩论层疑似改分路径: {forbidden}"
