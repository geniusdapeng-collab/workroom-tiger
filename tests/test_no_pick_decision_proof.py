"""WorkStore decision proof preserves the sealed no-pick rationale contract."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system.agent_evidence import EvidenceInvalid, normalize
from trading_system.llm import intelligence_required, work_proof
from trading_system.llm.intelligence_proof import _verify_decision


@pytest.mark.parametrize("action", ["WAIT", "AVOID"])
@pytest.mark.parametrize("rationale", [None, "malformed"])
def test_no_pick_workstore_decision_replays_sealed_rationale(monkeypatch, action, rationale):
    stage = "decision.debate"
    date = "2026-11-05"
    assessment = {"run_date": date, "market_timezone": "America/New_York"}
    prices = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime([date]))
    market = normalize({"stock_ohlcv": {}, "spy": prices, "sector_etfs": {},
                        "freshness": {"benchmark_last_bar": date, "benchmark_lag_days": 0}})
    bundle = {"bundle_id": "bundle_fixture", "manifest_sha256": "1" * 64, "gaps": []}
    frontier = {"symbols": [], "security_ids": {}}
    response = {"action": action, "action_summary": "本轮无可执行入场。", "decisions": []}
    company = {"contract": intelligence_required.COMPANY_CONTRACT, "bundle_id": bundle["bundle_id"],
               "manifest_sha256": bundle["manifest_sha256"], "candidates": {}}
    decision = {"status": "model_required", "triggered": [], "evidence": {},
                "model_output": response, "execution_decisions": [],
                "action_summary": response["action_summary"], "company_evidence": company}

    bound = {"picks": [], "watchlist": [], "mrs": {}, "sectors": [], "action": action,
             "rationale": rationale, "rejections": [], "documents": [], "assessment": assessment,
             "market_evidence": [], "market_asof": intelligence_required.market_asof(market, [], assessment),
             "account_usd": 100_000}
    outputs = {"layer1.mrs": {}, "layer2.sector": {"sectors": []},
               "layer3.tss": {"watchlist": []},
               "layer4.risk": {"picks": [], "action": action, "rejections": [],
                               "pick_rationale": rationale}, stage: decision}
    replay = SimpleNamespace(
        inputs={stage: bound, "layer4.risk": {"trade_date": date}},
        value=outputs.__getitem__, assessment=assessment,
        cutoff=datetime(2026, 11, 5, 21, 30, tzinfo=timezone.utc),
        policy={"research_work": {}},
        io=lambda _: [({"method": "tasks.child.join"}, None, None)],
        planner=lambda _: lambda *_: ({}, 100, 100),
    )
    result = {"action": action, "market_view": response["action_summary"], "picks": [],
              "raw": {"debate": decision, "execution_decisions": [],
                      "action_summary": response["action_summary"]}}
    outcome = []

    def verify_call(_replay, _call, payload, _schema, *, numeric_context, account_usd):
        assert payload["risk_rationale"] == rationale
        assert numeric_context["pick_rationale"] == rationale
        assert account_usd == 100_000
        return response

    def verify_outcome(_replay, value, *, selected_symbols, pick_rationale):
        assert value == decision
        outcome.append((selected_symbols, pick_rationale))

    monkeypatch.setattr(work_proof, "verify_decision_call", verify_call)
    monkeypatch.setattr(work_proof, "verify_decision_outcome", verify_outcome)
    if rationale is None:
        _verify_decision(replay, [], frontier, bundle, market, result)
        assert outcome == [([], {})]
    else:
        with pytest.raises(EvidenceInvalid) as rejected:
            _verify_decision(replay, [], frontier, bundle, market, result)
        assert rejected.value.stage == stage
        assert outcome == []


def test_selected_buys_do_not_accept_malformed_rationale_or_expand_ownership():
    from trading_system.llm.intelligence_proof import _selected_pick_rationale

    selected = {"NVDA"}
    assert _selected_pick_rationale({"NVDA": "数值通过", "AMD": "未选取"}, selected) == {
        "NVDA": "数值通过",
    }
    with pytest.raises(EvidenceInvalid) as rejected:
        _selected_pick_rationale([("NVDA", "伪造类型")], selected)
    assert rejected.value.stage == "decision.debate"
