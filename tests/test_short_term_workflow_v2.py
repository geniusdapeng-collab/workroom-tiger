"""Business guardrails for the whitepaper workflow upgrade (no paid I/O)."""
from datetime import datetime, timezone

import pytest

from trading_system.llm.request_budget import BudgetGuard, BudgetRefused
from trading_system.agent_runtime import policy_manifest
from trading_system.redline.execution import (
    UPGRADE_PROFILE, PREMARKET_PROFILE, INTRADAY_PROFILE,
)


def test_v2_profiles_are_full_research_with_same_hard_risk_policy():
    for profile, phase in ((UPGRADE_PROFILE, "daily"), (PREMARKET_PROFILE, "premarket"),
                           (INTRADAY_PROFILE, "intraday")):
        policy = policy_manifest(profile)
        assert policy["workflow_phase"] == phase
        assert policy["universe"] == "full"
        assert policy["max_positions"] == 10
        assert policy["execution_policy"] == "whitepaper-execution-v2"
        assert policy["real_broker"] is False
        names = [stage["name"] for stage in policy["stages"]]
        assert names.index("research.candidates") < names.index("decision.debate")
        assert names.index("paper.persist") < names.index("opportunity.lifecycle")
        assert names.index("opportunity.lifecycle") < names.index("business.evaluate")
        assert names.index("business.evaluate") < names.index("report.final_json")


def test_old_profile_contract_remains_fixed_for_history():
    old = policy_manifest("us-public-model-required-paper-v1")
    assert old["universe"] == "extended"
    assert len(old["stages"]) == 39
    assert "workflow_phase" not in old
    assert "request_budget" not in old


def test_budget_rejects_before_paid_call_and_never_forgets_unknown_usage():
    budget = BudgetGuard(max_chat_calls=2, max_tokens=10000, max_search_calls=1,
                         max_fetch_calls=1, max_remote_requests=4, max_cost_usd=1,
                         deadline_seconds=120)
    request = {"messages": [{"role": "user", "content": "价格风险"}], "max_tokens": 100}
    first = budget.reserve("chat", "a" * 32, request)
    assert first["token_upper_bound"] >= len("价格风险".encode()) + 100
    assert budget.reserve("chat", "a" * 32, request) == first
    with pytest.raises(BudgetRefused):
        budget.reserve("chat", "a" * 32, {**request, "max_tokens": 101})
    budget.mark_unknown("a" * 32)
    with pytest.raises(BudgetRefused):
        budget.reserve("search", "b" * 32, {"query": "issuer risk", "count": 1})
    assert budget.snapshot()["frozen"] is True


def test_budget_settlement_releases_unused_reservation_without_erasing_charge():
    budget = BudgetGuard(max_tokens=10000, deadline_seconds=120)
    request = {"messages": [{"role": "user", "content": "business evidence"}], "max_tokens": 100}
    budget.reserve("chat", "a" * 32, request)
    budget.settle("a" * 32, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    budget.settle("a" * 32, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    snap = budget.snapshot()
    assert snap["actual_tokens"] == 15
    assert snap["reserved_tokens"] == 0
    assert snap["counts"]["chat"] == 1
    with pytest.raises(BudgetRefused):
        budget.settle("a" * 32, {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16})


def test_budget_counts_search_and_fetch_even_without_returned_documents():
    budget = BudgetGuard(max_search_calls=1, max_fetch_calls=1, max_remote_requests=2,
                         deadline_seconds=120)
    budget.reserve("search", "a" * 32, {"query": "issuer", "count": 1})
    budget.settle("a" * 32, None)
    budget.reserve("fetch", "b" * 32, {"url": "https://www.sec.gov/"})
    budget.settle("b" * 32, None)
    with pytest.raises(BudgetRefused):
        budget.reserve("search", "c" * 32, {"query": "another issuer", "count": 1})
    assert budget.snapshot()["counts"] == {"chat": 0, "search": 1, "fetch": 1}


def test_budget_refuses_nonfinite_and_usage_beyond_reserved_bound():
    with pytest.raises(BudgetRefused):
        BudgetGuard(max_cost_usd=float("nan"))
    budget = BudgetGuard(max_tokens=10000, deadline_seconds=120)
    reservation = budget.reserve("chat", "a" * 32,
                                 {"messages": [{"role": "user", "content": "x"}], "max_tokens": 1})
    with pytest.raises(BudgetRefused):
        budget.settle("a" * 32, {"prompt_tokens": reservation["token_upper_bound"],
                                "completion_tokens": 1,
                                "total_tokens": reservation["token_upper_bound"] + 1})
    assert budget.snapshot()["frozen"] is True
