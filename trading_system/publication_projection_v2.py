"""Explicit public data projections from privately verified complete artifacts.

This does not certify a run. The caller independently verifies every private
stage/reference first, then binds this projection into the immutable receipt.
Full articles, model-call bodies and historical account replay inputs stay in
the private evidence store. Model explanations are copied without rewriting.
"""
from __future__ import annotations

from copy import deepcopy

from .llm.candidate_research import compact_entry_budget, digest


def public_pending(rows: list[dict]) -> list[dict]:
    """Expose current commitments without their private frozen replay account."""
    keys = ("ticker", "plan_id", "shares", "entry_ref", "stop_price", "risk_group",
            "signal_date", "execution_policy")
    return [{key: deepcopy(row[key]) for key in keys if key in row} for row in rows]


def public_research(research: dict) -> dict:
    keys = ("schema", "assessment", "theses", "coverage", "counters", "coverage_summary",
            "fact_evidence", "market_evidence", "source_index")
    value = {key: deepcopy(research[key]) for key in keys}
    value["plan"] = {key: deepcopy(research["plan"][key]) for key in
                     ("targets", "requested_symbols", "deferred", "limits", "plan_sha256")}
    return value


def public_lifecycle(lifecycle: dict, run_id: str) -> dict:
    terminal = {"CLOSED", "INVALIDATED", "EXPIRED", "SUPERSEDED"}
    rows = [row for row in lifecycle["opportunities"]
            if row.get("source_run_id") == run_id or row["state"] not in terminal]
    identities = {row["opportunity_id"] for row in rows}
    value = {key: deepcopy(lifecycle[key]) for key in ("schema", "account_kind", "revision")}
    value["scope"] = {"selection": "current_run_and_all_active_opportunities", "current_run_id": run_id,
        "total_opportunities": len(lifecycle["opportunities"]), "included_opportunities": len(rows),
        "historical_opportunities_omitted": len(lifecycle["opportunities"]) - len(rows),
        "history_location": "private_verified_opportunity_ledger"}
    value["opportunities"] = deepcopy(rows)
    for name in ("intents", "events", "cohort_outcomes"):
        value[name] = [deepcopy(row) for row in lifecycle[name] if row.get("opportunity_id") in identities]
    return value


def public_upgrade_summary(raw: dict, run_id: str) -> dict:
    value = {key: deepcopy(raw[key]) for key in (
        "business_evaluation", "portfolio_snapshot", "current_portfolio_snapshot",
        "execution_policy", "universe_coverage", "workflow_phase", "lifecycle_observation",
        "numeric_approved_count", "policy_identity")}
    value["candidate_research"] = public_research(raw["candidate_research"])
    value["opportunity_lifecycle"] = public_lifecycle(raw["opportunity_lifecycle"], run_id)
    value["entry_budget"] = compact_entry_budget(raw)
    cohort = raw["cohort_inputs"]
    value["cohort_evidence"] = {"schema": "tiger.cohort-evidence.v2",
        "policy_id": cohort["policy_id"], "policy_hash": cohort["policy_hash"],
        "source_sha256": digest(cohort),
        "scope": "Full source account/snapshot/setups stay in the private verified cohort artifact; this is their immutable identity."}
    return value
