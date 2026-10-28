"""Focused local regression for the five v4 semantic output ceilings.

SYNTHETIC_TEST fixtures have no network fallback and are not business evidence.
"""
from dataclasses import asdict
from datetime import date
from types import SimpleNamespace
import json

import pytest

from trading_system.llm import request_budget as budgets
from trading_system.llm import required
from trading_system.llm.candidate_research import (
    ResearchBudget, assessment_from_execution, build_research_plan, research_candidates)
from trading_system.llm.clean_output import CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1 as COMPACT
from trading_system.llm.deepseek_required import build_chat_request
from trading_system.search.models import RawDocument
from trading_system.redline.execution import RequiredModelFailed

POLICIES = (None, "tiger.budget-policy.v2", "tiger.budget-policy.v3", "tiger.budget-policy.v4")
OLD_CAPS = {"tech.sentiment": 8192, "tech.risk": 12288, "sector.narrative": 12288,
            "research.candidates": 8192, "decision.debate": 16384}


def execution(policy):
    return SimpleNamespace(upgrade=True, run_date=date(2026, 10, 28),
        admitted_at_utc="2026-10-28T20:00:00+00:00",
        policy=SimpleNamespace(budget_policy_id=policy, market_timezone="America/New_York",
            clean_output_policy_id=COMPACT if policy == POLICIES[-1] else None))


class Captured(Exception):
    pass


class CaptureClient:
    def __init__(self, policy):
        self.execution, self.calls = execution(policy), []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        raise Captured


def empty_context(ex):
    return {"picks": [], "watchlist": [], "research_holdings": [], "market_data": {},
        "candidate_research": {"schema": "tiger.candidate-research.v2",
            "assessment": assessment_from_execution(ex), "plan": {"targets": []},
            "documents": [], "theses": [], "coverage": {}, "coverage_summary": "SYNTHETIC_TEST",
            "source_index": [], "fact_evidence": []}}


def test_actual_semantic_callers_and_clean_select_only_current_policy():
    for policy in POLICIES:
        for stage, invoke in {
            "tech.sentiment": lambda c: required.sentiment_v2(c, []),
            "tech.risk": lambda c: required.risk_v2(c, []),
            "sector.narrative": lambda c: required.narrative_v2(c, [], ["XLK"]),
            "decision.debate": lambda c: required.decision_v2(c, empty_context(c.execution), [], 10000),
            "research.candidates": lambda c: research_candidates(c, empty_context(c.execution), []),
        }.items():
            client = CaptureClient(policy)
            with pytest.raises(Captured):
                invoke(client)
            assert len(client.calls) == 1
            arguments = client.calls[0]
            assert arguments["max_tokens"] == (65536 if policy == POLICIES[-1] else OLD_CAPS[stage])
            assert build_chat_request(**arguments)["reasoning_effort"] == "high"
    client = CaptureClient(POLICIES[-1])
    raw = RawDocument("SYNTHETIC_TEST_doc", "fixture", "SYNTHETIC_TEST", "https://example.com/a",
        "SYNTHETIC_TEST public source body", "", 1793145600., {})
    with pytest.raises(Captured):
        required.clean_v2(client, [raw])
    assert client.calls[0]["max_tokens"] == 32768
    assert client.calls[0]["reasoning_effort"] == "none"


def test_policy_selector_and_research_limits_do_not_add_serialized_plan_fields():
    for policy in POLICIES:
        ex = execution(policy)
        budget = ResearchBudget.for_policy(policy)
        selected = 65536 if policy == POLICIES[-1] else 8192
        assert budget.max_output_tokens == selected
        assert set(asdict(budget)) == set(asdict(ResearchBudget()))
        assert "_budget_policy_id" not in asdict(budget)
        plan = build_research_plan(empty_context(ex), assessment_from_execution(ex), budget=budget)
        assert plan["limits"] == asdict(budget)
        for stage, previous in OLD_CAPS.items():
            assert budgets.semantic_output_tokens(stage, policy) == (65536 if policy == POLICIES[-1] else previous)
    for policy, stage in (("unapproved", "tech.sentiment"), (POLICIES[-1], "clean.llm_semantic"),
                          (POLICIES[-1], [])):
        with pytest.raises(budgets.BudgetRefused):
            budgets.semantic_output_tokens(stage, policy)
    with pytest.raises(RequiredModelFailed, match="frozen v2 limits"):
        ResearchBudget(max_output_tokens=65536)
    for policy, explicit in ((POLICIES[-1], ResearchBudget()),
                             (None, ResearchBudget.for_policy(POLICIES[-1]))):
        client = CaptureClient(policy)
        with pytest.raises(RequiredModelFailed, match="frozen execution policy"):
            research_candidates(client, empty_context(client.execution), [], budget=explicit)
        assert client.calls == []


class ResearchClient(CaptureClient):
    def __init__(self, policy):
        super().__init__(policy)
        self.searches = []

    def search(self, query, **kwargs):
        self.searches.append((query, kwargs))
        return []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["user"].split("\n", 1)[1])
        parts = {scope: {"text": "SYNTHETIC_TEST missing source remains explicit",
            "evidence_ids": [row["id"] for row in payload["fact_evidence"] if row["scope"] == scope]}
            for scope in ("market", "sector", "chain", "stock", "plan")}
        parts["stock"]["evidence_ids"].append(payload["market_evidence"][0]["id"])
        initial = payload["phase"] == "initial"
        return {"coverage_summary": "SYNTHETIC_TEST evidence remains insufficient", "theses": [{
            "schema_version": 2, "symbol": "AAA", "strategy": "technical_flow_v2",
            "status": "insufficient", "issuer_name": None, "issuer_identity_evidence_ids": [],
            "five_part": parts, "claims": [], "event_risk": {"status": "unknown",
                "text": "SYNTHETIC_TEST unverified", "source_ids": []},
            "entry_triggers": ["SYNTHETIC_TEST recheck price"], "falsification": ["SYNTHETIC_TEST invalid price"],
            "risk_notes": ["SYNTHETIC_TEST event unknown"], "gaps": ["SYNTHETIC_TEST no issuer source"],
            "followup": {"needed": initial, "question": "SYNTHETIC_TEST issuer" if initial else None,
                         "fetch_source_id": None}}]}


def test_actual_initial_and_supplemental_research_share_frozen_output_ceiling():
    frame = {"__type__": "dataframe", "index": ["2026-10-27"], "index_names": [None],
        "columns": ["Close"], "column_names": [None], "dtypes": ["float64"], "values": [[100.]]}
    for policy in POLICIES:
        client = ResearchClient(policy)
        context = {"picks": [], "watchlist": [{"ticker": "AAA"}], "research_holdings": [],
                   "market_data": {"stock_ohlcv": {"AAA": frame}}}
        result = research_candidates(client, context, [])
        selected = 65536 if policy == POLICIES[-1] else 8192
        assert result["plan"]["limits"]["max_output_tokens"] == selected
        assert [row["max_tokens"] for row in client.calls] == [selected, selected]
        assert [row["phase"] for row in result["model_rounds"]] == ["initial", "final"]
        assert len(client.searches) == 2 and result["theses"][0]["status"] == "insufficient"


def test_remaining_time_is_bounded_and_invalid_clocks_fail_closed():
    clock = [0.]
    guard = budgets.BudgetGuard.for_policy(POLICIES[-1], clean_output_policy_id=COMPACT, clock=lambda: clock[0])
    assert guard.remaining_seconds() == 7200
    clock[0] = 7123.
    assert guard.remaining_seconds() == 77
    clock[0] = 7200.
    assert guard.remaining_seconds() == 0
    clock[0] = 7201.
    assert guard.remaining_seconds() == 0
    for invalid in (-1., float("nan"), float("inf")):
        clock[0] = invalid
        with pytest.raises(budgets.BudgetRefused, match="INVALID_BUDGET_CLOCK"):
            guard.remaining_seconds()
