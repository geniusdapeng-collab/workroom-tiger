"""Stage-specific effort is a trusted policy, not a caller-selected downgrade."""
import pytest
import requests

from trading_system.llm.deepseek_required import DeepSeekRequiredClient, build_chat_request
from trading_system.redline import LLMUnavailable
from test_deepseek_required import configured, Response, SCHEMA, chat_response


@pytest.mark.parametrize("upgrade,stage,effort", [
    (True, "clean.llm_semantic", "low"),
    (True, "research.candidates", "high"),
    (True, "decision.debate", "high"),
    (False, "clean.llm_semantic", "high"),
])
def test_only_v2_extraction_uses_low_effort_with_actual_budget_bound_request(
        configured, monkeypatch, upgrade, stage, effort):
    configured.upgrade = upgrade
    configured._active.spec.name = stage
    requests_seen = []
    def post(url, **kwargs):
        requests_seen.append(kwargs["json"])
        return Response(chat_response())
    monkeypatch.setattr(requests, "post", post)
    client = DeepSeekRequiredClient(configured)
    answer = client.complete_json(system="Analyze JSON", user="Public evidence",
        schema_hint={}, schema=SCHEMA, max_tokens=16384, reasoning_effort=effort)
    assert answer == {"score": 7, "reason": "evidence"}
    assert len(requests_seen) == 1
    assert requests_seen[0]["reasoning_effort"] == effort
    assert requests_seen[0]["thinking"] == {"type":
        "disabled" if upgrade and stage == "clean.llm_semantic" else "enabled"}
    assert configured.calls[0]["output"]["request_sha256"] == configured.calls[0]["inputs"]["request_sha256"]
    if upgrade:
        assert configured.calls[0]["inputs"]["budget_reservation"]["request_sha256"] == client.receipts[0]["request_sha256"]
        assert client.budget.snapshot()["counts"]["chat"] == 1
        assert client.budget.snapshot()["actual_tokens"] == 50


@pytest.mark.parametrize("upgrade,stage,effort", [
    (True, "clean.llm_semantic", "high"),
    (True, "decision.debate", "low"),
    (True, "research.candidates", "low"),
    (True, "clean.llm_semantic", "medium"),
    (True, "clean.llm_semantic", "max"),
    (True, "clean.llm_semantic", True),
    (False, "clean.llm_semantic", "low"),
])
def test_effort_policy_refuses_before_any_paid_request_or_reservation(configured, upgrade, stage, effort):
    configured.upgrade = upgrade
    configured._active.spec.name = stage
    client = DeepSeekRequiredClient(configured)
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="Analyze JSON", user="Public evidence",
            schema_hint={}, schema=SCHEMA, reasoning_effort=effort)
    assert configured.calls == [] and client.receipts == []
    if upgrade:
        assert client.budget.snapshot()["records"] == []


def test_pure_request_builder_preserves_legacy_default_and_accepts_explicit_policy_value():
    common = dict(system="Analyze JSON", user="Public evidence", schema_hint={}, schema=SCHEMA)
    legacy = build_chat_request(**common)
    assert legacy["reasoning_effort"] == "high"
    low = build_chat_request(**common, reasoning_effort="low")
    assert low == {**legacy, "reasoning_effort": "low"}
