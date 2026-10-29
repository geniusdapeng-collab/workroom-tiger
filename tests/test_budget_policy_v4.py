"""Expanded single-source policy; synthetic offline bounds and proof only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.agent_runtime import frozen_clock, policy_manifest, validate_clock
from trading_system.llm.clean_output import CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1 as COMPACT
from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, BUDGET_POLICY_V3, BUDGET_POLICY_V4, policy_limits
from trading_system.llm.required_proof_v2 import ReplayClient
from trading_system.redline.execution import ExecutionContractError, UPGRADE_POLICIES
from trading_system.redline.model_diagnostics import validate_model_failure_accounting

V4_LIMITS = {"max_chat_calls": 160, "max_tokens": 20000000, "max_search_calls": 60,
    "max_fetch_calls": 20, "max_remote_requests": 240, "max_cost_usd": 30., "deadline_seconds": 7200}


def guard(clock=lambda: 0.):
    return BudgetGuard.for_policy(BUDGET_POLICY_V4, clean_output_policy_id=COMPACT, clock=clock)


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_current_compact_selects_only_exact_expanded_policy(profile):
    policy = policy_manifest(profile)
    assert policy["execution"]["budget_policy_id"] == policy["request_budget_policy_id"] == BUDGET_POLICY_V4
    assert policy["clean_output_policy_id"] == COMPACT
    assert policy["request_budget"] == policy_limits(BUDGET_POLICY_V4) == V4_LIMITS
    assert policy["business_deadline_seconds"] == 7200
    assert policy["clean_regeneration_limits"] == {"max_per_batch": 1, "max_per_run": 56}
    assert policy["clean_instruction_revision"] == "v2"
    old = policy_manifest(profile, budget_policy_id=BUDGET_POLICY_V3)
    assert old["business_deadline_seconds"] == 2400
    assert old["clean_regeneration_limits"]["max_per_run"] == 4
    assert old["request_budget"]["max_tokens"] == 600000
    assert "clean_instruction_revision" not in old
    with pytest.raises(EvidenceInvalid):
        policy_manifest(profile, clean_instruction_revision="future")


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_frozen_policy_can_still_replay_the_prior_enabled_thinking_manifest(profile):
    current = policy_manifest(profile)
    assert current["model_contract"]["thinking_mode"] == "disabled"
    historical = policy_manifest(profile, thinking_mode=None)
    assert "thinking_mode" not in historical["model_contract"]
    assert historical["limitations"][1] == (
        "All six semantic stages require actual DeepSeek Flash replies and bounded native research; "
        "failures stop publication.")
    assert all("explicitly disable thinking" not in row for row in historical["limitations"])


@pytest.mark.parametrize("clean", [None, "tiger.clean-output.strict-function.v1",
    "tiger.clean-output.responses-schema.v1", "tiger.clean-output.responses-slots.v1"])
def test_expanded_policy_cannot_be_reused_for_historical_protocol(clean):
    with pytest.raises(BudgetRefused):
        BudgetGuard.for_policy(BUDGET_POLICY_V4, clean_output_policy_id=clean)
    with pytest.raises((EvidenceInvalid, ExecutionContractError)):
        policy_manifest(next(iter(UPGRADE_POLICIES)), budget_policy_id=BUDGET_POLICY_V4,
                        clean_output_policy_id=clean)
    old = policy_manifest(next(iter(UPGRADE_POLICIES)), clean_output_policy_id=clean)
    assert old["request_budget_policy_id"] == BUDGET_POLICY_V3


def test_previous_downstream_capacity_refusal_is_admitted_and_actual_usage_settled():
    from test_budget_policy_v3 import request
    body = request(701867)
    with pytest.raises(BudgetRefused, match="TOKEN_BUDGET_LIMIT"):
        BudgetGuard.for_policy(BUDGET_POLICY_V3).reserve("chat", "a" * 32, body)
    budget = guard()
    reservation = budget.reserve("chat", "a" * 32, body)
    assert reservation["schema"] == "tiger.request-budget-reservation.v4"
    assert reservation["policy_id"] == BUDGET_POLICY_V4
    assert reservation["token_upper_bound"] == 701867
    budget.settle("a" * 32, {"prompt_tokens": 120000, "completion_tokens": 10000, "total_tokens": 130000})
    snapshot = budget.snapshot()
    assert snapshot["schema"] == "tiger.request-budget.v4"
    assert snapshot["actual_tokens"] == 130000 and snapshot["reserved_tokens"] == 0
    assert snapshot["estimated_charged_usd"] == pytest.approx(.156)


def test_closed_v4_call_allowance_and_deadline_still_fail_before_io():
    elapsed = [0.]
    budget = guard(clock=lambda: elapsed[0])
    body = {"messages": [{"role": "user", "content": "SYNTHETIC_TEST"}], "max_tokens": 10}
    for index, kind in enumerate(["chat"] * 160 + ["search"] * 60 + ["fetch"] * 20):
        identity = f"{index:032x}"
        budget.reserve(kind, identity, body if kind == "chat" else {})
        budget.settle(identity, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2} if kind == "chat" else None)
    assert sum(budget.snapshot()["counts"].values()) == 240
    with pytest.raises(BudgetRefused, match="REQUEST_COUNT_LIMIT"):
        budget.reserve("chat", "f" * 32, body)
    another = guard(clock=lambda: elapsed[0])
    elapsed[0] = 7199.
    another.reserve("search", "a" * 32, {})
    another.settle("a" * 32, None)
    elapsed[0] = 7200.
    with pytest.raises(BudgetRefused, match="BUDGET_DEADLINE"):
        another.reserve("search", "b" * 32, {})


def test_unknown_paid_outcome_still_freezes_expanded_budget():
    budget = guard()
    budget.reserve("search", "a" * 32, {})
    budget.mark_unknown("a" * 32)
    with pytest.raises(BudgetRefused, match="UNKNOWN_USAGE_FREEZES_BUDGET"):
        budget.reserve("search", "b" * 32, {})
    with pytest.raises(BudgetRefused, match="UNKNOWN_USAGE_CANNOT_BE_ERASED"):
        budget.settle("a" * 32, None)


def test_frozen_clock_binds_expanded_deadline_to_selected_policy():
    instant = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
    old, expanded = frozen_clock(instant), frozen_clock(instant, budget_policy_id=BUDGET_POLICY_V4)
    assert datetime.fromisoformat(old["deadline_at_utc"]) - instant == timedelta(seconds=2400)
    assert datetime.fromisoformat(expanded["deadline_at_utc"]) - instant == timedelta(seconds=7200)
    validate_clock(old)
    validate_clock(expanded, budget_policy_id=BUDGET_POLICY_V4)
    with pytest.raises(EvidenceInvalid): validate_clock(expanded)
    with pytest.raises(EvidenceInvalid): validate_clock(old, budget_policy_id=BUDGET_POLICY_V4)


def test_supervisor_uses_validated_policy_deadline_not_legacy_2400_cap(tmp_path, monkeypatch):
    import trading_system.agent_supervisor as worker
    run_id = "run-" + "a" * 32
    clock = frozen_clock(datetime.now(timezone.utc), budget_policy_id=BUDGET_POLICY_V4)
    policy = policy_manifest(next(iter(UPGRADE_POLICIES)))
    captured = {}
    monkeypatch.setattr(worker, "_check_lock_descriptor", lambda *a: None)
    monkeypatch.setattr(worker, "validate_admission", lambda *a: ({"clock": clock}, policy, {}))
    monkeypatch.setattr(worker, "atomic_json", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "read_bytes", lambda *a: b"SYNTHETIC_TEST")
    monkeypatch.setattr(worker, "_send_control", lambda *a: None)
    monkeypatch.setattr(worker.os, "close", lambda *a: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.signal, "setitimer", lambda _kind, seconds: captured.setdefault("alarms", []).append(seconds))
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.)
    def group(*a, **kw):
        captured["deadline"] = kw["deadline"]
        return {}
    monkeypatch.setattr(worker, "supervise_group", group)
    monkeypatch.setattr(worker, "seal_and_publish", lambda *a, **kw: {"status": "completed"})
    assert worker.supervisor(tmp_path, run_id, account_fd=1, run_fd=2, ack_fd=3) == 0
    assert 7100 < captured["alarms"][0] <= 7200
    assert 7200 < captured["deadline"] <= 7300
    assert captured["alarms"][-1] == 0


def test_failure_accounting_tracks_expanded_counts_without_response_text():
    value = {"schema": "tiger.model-call-accounting.v3", "budget_policy_id": BUDGET_POLICY_V4,
        "call_id": "a" * 32, "request_sha256": "b" * 64, "kind": "chat", "status": "unknown",
        "usage": None, "counts": {"chat": 130, "search": 54, "fetch": 20}, "actual_tokens": 1000000,
        "reserved_tokens": 100000, "estimated_charged_usd": 1.2555, "estimated_reserved_usd": .12,
        "frozen": True, "finish_reason": None}
    assert validate_model_failure_accounting(value) == value
    for change in ({"actual_tokens": 20000001}, {"reserved_tokens": 20000000},
                   {"estimated_charged_usd": 30.01}, {"budget_policy_id": BUDGET_POLICY_V3},
                   {"counts": {"chat": 161, "search": 0, "fetch": 0}}):
        with pytest.raises(ValueError): validate_model_failure_accounting({**value, **change})


def v4_pure_fixture():
    from test_clean_compact_lists_policy_proof_v1 import pure_fixture, h
    execution, arguments, valid, records = pure_fixture()
    execution.policy = replace(execution.policy, budget_policy_id=BUDGET_POLICY_V4)
    budget, previous = guard(), None
    for row in records:
        request, proof = row["request"], row["proof"]
        request["previous_rejection_sha256"] = proof["previous_rejection_sha256"] = previous
        reservation = budget.reserve("chat", request["call_id"], request["request"])
        request["budget_reservation"] = proof["budget_reservation"] = reservation
        budget.settle(request["call_id"], proof["usage"])
        proof["budget_after"] = budget.snapshot()
        previous = h(proof)
    return execution, arguments, valid, records


def test_independent_replay_accepts_fifth_regeneration_under_v4_only():
    execution, arguments, valid, records = v4_pure_fixture()
    replay = ReplayClient(execution, {"clean.llm_semantic": records})
    replay._regeneration_count = 4
    replay.set_stage("clean.llm_semantic")
    assert replay.complete_json(**arguments, reasoning_effort="none") == valid
    replay.finish()
    assert replay._regeneration_count == 5
    assert replay.budget.snapshot()["schema"] == "tiger.request-budget.v4"
    assert replay.budget.snapshot()["actual_tokens"] == 100


def test_independent_replay_refuses_57th_regeneration_after_recording_rejection():
    execution, arguments, _, records = v4_pure_fixture()
    replay = ReplayClient(execution, {"clean.llm_semantic": records})
    replay._regeneration_count = 56
    replay.set_stage("clean.llm_semantic")
    with pytest.raises(EvidenceInvalid, match="clean_regeneration_allowance"):
        replay.complete_json(**arguments, reasoning_effort="none")
    assert replay._cursor == 1 and replay.budget.snapshot()["actual_tokens"] == 50


def test_independent_replay_refuses_multi_source_v4_before_any_call():
    from trading_system import config
    from trading_system.llm.required import clean_keyed_schema_v2
    from trading_system.llm.required_schemas import clean_schema_v2
    from trading_system.tech_chain.universe import TECH_SUBCHAINS
    execution, arguments, _, _ = v4_pure_fixture()
    ids = ["SYNTHETIC_TEST_first", "SYNTHETIC_TEST_second"]
    arguments["schema"] = clean_keyed_schema_v2(clean_schema_v2(ids, list(TECH_SUBCHAINS), config.SECTOR_ETFS,
        quote_part_ids={identity: ["p0"] for identity in ids}, all_doc_ids=ids), ids)
    replay = ReplayClient(execution, {"clean.llm_semantic": []})
    replay.set_stage("clean.llm_semantic")
    with pytest.raises(EvidenceInvalid, match="compact_source_batch_limit"):
        replay.complete_json(**arguments, reasoning_effort="none")
    assert replay.budget.snapshot()["counts"]["chat"] == 0
