"""The public envelope retains decisions and evidence without article/history bodies."""
from copy import deepcopy

from trading_system.publication_projection_v2 import public_research, public_lifecycle, public_upgrade_summary


def test_candidate_projection_keeps_thesis_and_presented_coverage_without_private_article_or_rounds():
    source = {"schema": "tiger.candidate-research.v2", "assessment": {"run_date": "2026-10-27"},
        "plan": {"schema": "private-plan-envelope", "assessment": {}, "targets": [], "requested_symbols": [],
                 "deferred": [], "limits": {}, "plan_sha256": "a" * 64},
        "documents": [{"content": "DO_NOT_PUBLISH_FULL_ARTICLE"}], "model_rounds": ["PRIVATE_ROUND"],
        "actions": [], "theses": [{"symbol": "AAA", "five_part": {"market": {"text": "SYNTHETIC_TEST 完整理由"}}}],
        "coverage": {}, "counters": {}, "coverage_summary": "SYNTHETIC_TEST 原话保留",
        "fact_evidence": [], "market_evidence": [], "source_index": [{"id": "ds_a", "presentations": []}]}
    before = deepcopy(source)
    projected = public_research(source)
    assert "documents" not in projected and "model_rounds" not in projected and "actions" not in projected
    assert projected["theses"] == source["theses"]
    assert projected["coverage_summary"] == source["coverage_summary"]
    assert projected["source_index"] == source["source_index"]
    assert source == before


def test_lifecycle_selection_discloses_history_scope_and_keeps_active_held_from_older_run():
    source = {"schema": "tiger.opportunity-ledger.v2", "account_kind": "paper", "revision": 5,
        "opportunities": [{"opportunity_id": "new", "source_run_id": "run-now", "state": "WATCH"},
                          {"opportunity_id": "held", "source_run_id": "run-old", "state": "HELD"},
                          {"opportunity_id": "history", "source_run_id": "run-old", "state": "CLOSED"}],
        "intents": [{"opportunity_id": "held", "requires_execution_recheck": True}],
        "events": [{"opportunity_id": "new"}, {"opportunity_id": "history"}],
        "cohort_outcomes": [{"opportunity_id": "history"}],
        "research_runs": [{"cohort_inputs": {"portfolio_state": "PRIVATE_HISTORY"}}], "account_snapshots": []}
    projected = public_lifecycle(source, "run-now")
    assert [row["opportunity_id"] for row in projected["opportunities"]] == ["new", "held"]
    assert projected["scope"]["historical_opportunities_omitted"] == 1
    assert projected["scope"]["total_opportunities"] == 3
    assert projected["events"] == [{"opportunity_id": "new"}]
    assert "research_runs" not in projected and "account_snapshots" not in projected
