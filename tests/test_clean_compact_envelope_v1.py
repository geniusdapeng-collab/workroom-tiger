"""Synthetic compact envelope compatibility and independent replay; no network."""
from copy import deepcopy
from dataclasses import replace
import json
import socket
from unittest.mock import patch

import pytest
import requests

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.llm import clean_output as wire
from trading_system.llm.deepseek_required import DeepSeekRequiredClient, MAX_RESPONSE_BYTES, _canonical, _hash
from trading_system.llm.request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4
from trading_system.llm.required_proof_v2 import ReplayClient
from trading_system.redline.model_diagnostics import RequiredModelUnavailable, validate_model_diagnostic
from test_clean_compact_regeneration_core_v1 import (
    ENVIRONMENT, TEST_KEY, SyntheticExecution, generate, install_responses, tool_response,
)
from test_clean_compact_lists_policy_proof_v1 import (
    h, keyed_schema, pure_fixture, replay_fixture, valid_answer,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("NETWORK_FORBIDDEN_IN_COMPACT_ENVELOPE_TEST")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(requests, "post", deny)


def client_for(policy=BUDGET_POLICY_V4, *, output_policy=wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1):
    execution = SyntheticExecution(output_policy=output_policy)
    execution.policy = replace(execution.policy, budget_policy_id=policy)
    with patch.dict("os.environ", ENVIRONMENT):
        return DeepSeekRequiredClient(execution)


def replay_captured(client, schema):
    calls = [{"method": row["method"], "request": row["input"], "proof": row["output"]}
             for row in client.execution.records]
    replay = ReplayClient(client.execution, {"clean.llm_semantic": calls})
    replay.set_stage("clean.llm_semantic")
    answer = generate(replay, schema)
    replay.finish()
    return replay, answer


@pytest.mark.parametrize("policy", [BUDGET_POLICY_V3, BUDGET_POLICY_V4])
@pytest.mark.parametrize("content", [None, "", "SYNTHETIC_TEST visible note 中文😀\nPreserve spacing.  "],
                         ids=["null", "empty", "visible_unicode"])
def test_compact_visible_content_is_preserved_but_never_used_as_semantic_arguments(policy, content):
    client = client_for(policy)
    schema = keyed_schema()
    expected = valid_answer(schema)
    response = tool_response(expected)
    response["choices"][0]["message"]["content"] = content
    before = deepcopy(response)
    seen = install_responses(client, [response])
    assert generate(client, schema) == expected
    assert len(seen) == 1 and response == before
    proof = client.execution.records[0]["output"]
    assert proof["response"]["choices"][0]["message"]["content"] == content
    assert proof["response_sha256"] == _hash(proof["response"])
    assert proof["parsed"] == expected
    assert not {"content", "response", "parsed"}.intersection(client.receipts[0])
    replay, actual = replay_captured(client, schema)
    assert actual == expected and replay.receipts == client.receipts
    assert replay.budget.snapshot()["actual_tokens"] == 110


def test_rejected_and_regenerated_content_are_bound_separately_without_becoming_feedback():
    client = client_for()
    schema = keyed_schema()
    expected = valid_answer(schema)
    invalid = deepcopy(expected)
    invalid["d0__z"] = "X" * 801
    first, second = tool_response(invalid, 1), tool_response(expected, 2)
    notes = ["SYNTHETIC_TEST first visible note", "SYNTHETIC_TEST second visible note"]
    for response, content in zip((first, second), notes):
        response["choices"][0]["message"]["content"] = content
    seen = install_responses(client, [first, second])
    assert generate(client, schema) == expected
    proofs = [row["output"] for row in client.execution.records]
    assert [proof["response"]["choices"][0]["message"]["content"] for proof in proofs] == notes
    assert [proof["outcome"] for proof in proofs] == ["rejected", "accepted"]
    assert proofs[1]["previous_rejection_sha256"] == _hash(proofs[0])
    assert all(note not in _canonical(seen[1][1]).decode() for note in notes)
    replay, actual = replay_captured(client, schema)
    assert actual == expected and replay.budget.snapshot()["actual_tokens"] == 220


@pytest.mark.parametrize("case,code", [
    ("choice_index", "COMPACT_INVALID_CHOICE_INDEX"),
    ("choice_bool", "COMPACT_INVALID_CHOICE_INDEX"),
    ("finish", "COMPACT_INVALID_FINISH_REASON"),
    ("message_type", "COMPACT_INVALID_MESSAGE_TYPE"),
    ("role", "COMPACT_INVALID_MESSAGE_ROLE"),
    ("content_missing", "COMPACT_INVALID_CONTENT_TYPE"),
    ("content_list", "COMPACT_INVALID_CONTENT_TYPE"),
    ("content_dict", "COMPACT_INVALID_CONTENT_TYPE"),
    ("content_bool", "COMPACT_INVALID_CONTENT_TYPE"),
    ("content_number", "COMPACT_INVALID_CONTENT_TYPE"),
    ("legacy_function", "COMPACT_LEGACY_FUNCTION_PRESENT"),
    ("reasoning", "COMPACT_REASONING_PRESENT"),
    ("tools_missing", "COMPACT_INVALID_TOOL_COUNT"),
    ("tools_empty", "COMPACT_INVALID_TOOL_COUNT"),
    ("tools_multiple", "COMPACT_INVALID_TOOL_COUNT"),
    ("tool_type_not_object", "COMPACT_TOOL_NOT_OBJECT"),
    ("tool_missing", "COMPACT_INVALID_TOOL_FIELDS"),
    ("tool_kind", "COMPACT_INVALID_TOOL_TYPE"),
    ("function_type", "COMPACT_FUNCTION_NOT_OBJECT"),
    ("function_missing", "COMPACT_INVALID_FUNCTION_FIELDS"),
    ("function_name", "COMPACT_INVALID_FUNCTION_NAME"),
])
def test_each_envelope_refusal_has_one_closed_value_free_code_and_retains_paid_usage(case, code):
    client = client_for()
    schema = keyed_schema()
    response = tool_response(valid_answer(schema))
    choice = response["choices"][0]
    message = choice["message"]
    tool = message["tool_calls"][0]
    marker = "SYNTHETIC_UNTRUSTED_RESPONSE_MARKER"
    if case == "choice_index": choice["index"] = 1
    elif case == "choice_bool": choice["index"] = False
    elif case == "finish": choice["finish_reason"] = "length"
    elif case == "message_type": choice["message"] = marker
    elif case == "role": message["role"] = marker
    elif case == "content_missing": message.pop("content")
    elif case == "content_list": message["content"] = [marker]
    elif case == "content_dict": message["content"] = {marker: marker}
    elif case == "content_bool": message["content"] = False
    elif case == "content_number": message["content"] = 0
    elif case == "legacy_function": message["function_call"] = {"name": marker}
    elif case == "reasoning": message["reasoning_content"] = marker
    elif case == "tools_missing": message.pop("tool_calls")
    elif case == "tools_empty": message["tool_calls"] = []
    elif case == "tools_multiple": message["tool_calls"].append(deepcopy(tool))
    elif case == "tool_type_not_object": message["tool_calls"][0] = marker
    elif case == "tool_missing": tool.pop("id")
    elif case == "tool_kind": tool["type"] = marker
    elif case == "function_type": tool["function"] = marker
    elif case == "function_missing": tool["function"].pop("name")
    else: tool["function"]["name"] = marker
    seen = install_responses(client, [response])
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
    assert validate_model_diagnostic(caught.value.diagnostic) == caught.value.diagnostic
    assert len(seen) == 1 and not client.receipts and client._clean_regenerations_used == 0
    failure = client.execution.records[0]["error"]
    assert failure["diagnostic"]["code"] == code
    assert failure["accounting"]["status"] == "settled"
    assert failure["accounting"]["actual_tokens"] == 110
    assert failure["accounting"]["reserved_tokens"] == 0 and failure["accounting"]["frozen"] is False
    assert marker not in _canonical(client.execution.records).decode()


@pytest.mark.parametrize("content,code", [
    (TEST_KEY, "COMPACT_UNSAFE_CONTENT"),
    ("api_key=SYNTHETIC_FAKE_SECRET", "COMPACT_UNSAFE_CONTENT"),
    ("https://example.com/?token=SYNTHETIC_FAKE_SECRET", "COMPACT_UNSAFE_CONTENT"),
    (chr(0xD800), "COMPACT_INVALID_CONTENT_UTF8"),
    ("X" * (MAX_RESPONSE_BYTES + 1), "COMPACT_CONTENT_TOO_LARGE"),
], ids=["current_key", "credential_pattern", "unsafe_url", "unpaired_surrogate", "over_body_limit"])
def test_unsafe_content_is_never_stored_repaired_or_regenerated(content, code):
    client = client_for()
    schema = keyed_schema()
    response = tool_response(valid_answer(schema))
    response["choices"][0]["message"]["content"] = content
    seen = install_responses(client, [response])
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic["code"] == code
    assert len(seen) == 1 and not client.receipts and client._clean_regenerations_used == 0
    assert client.budget.snapshot()["actual_tokens"] == 110 and not client.budget.snapshot()["frozen"]
    captured = json.dumps(client.execution.records, ensure_ascii=True)
    assert "SYNTHETIC_FAKE_SECRET" not in captured and TEST_KEY not in captured
    assert "\\ud800" not in captured and "X" * 100 not in captured


@pytest.mark.parametrize("rejection", [False, True])
def test_independent_replay_accepts_safe_content_and_preserves_each_rejection_binding(rejection):
    fixture = pure_fixture(rejection=rejection)
    previous = None
    for index, record in enumerate(fixture[3]):
        proof = record["proof"]
        proof["response"]["choices"][0]["message"]["content"] = f"SYNTHETIC_TEST independent content {index} 中文😀"
        proof["response_sha256"] = h(proof["response"])
        record["request"]["previous_rejection_sha256"] = proof["previous_rejection_sha256"] = previous
        previous = h(proof)
    replay, actual = replay_fixture(fixture)
    assert actual == fixture[2]
    assert replay.budget.snapshot()["actual_tokens"] == 50 * (1 + rejection)
    assert [row["proof_sha256"] for row in replay.receipts] == [h(row["proof"]) for row in fixture[3]]


@pytest.mark.parametrize("case", ["hash_unbound", "type", "secret", "unsafe_url", "missing_content", "multiple_tools", "reasoning"])
def test_independent_replay_refuses_unbound_or_coherently_rehashed_unsafe_content(case):
    fixture = pure_fixture(rejection=False)
    proof = fixture[3][0]["proof"]
    message = proof["response"]["choices"][0]["message"]
    if case == "hash_unbound": message["content"] = "SYNTHETIC_TEST unbound edit"
    elif case == "type": message["content"] = {"untrusted": "SYNTHETIC_TEST"}
    elif case == "secret": message["content"] = "api_key=SYNTHETIC_FAKE_SECRET"
    elif case == "unsafe_url": message["content"] = "https://example.com/?token=SYNTHETIC_FAKE_SECRET"
    elif case == "missing_content": message.pop("content")
    elif case == "multiple_tools": message["tool_calls"].append(deepcopy(message["tool_calls"][0]))
    else: message["reasoning_content"] = "SYNTHETIC_TEST hidden text"
    if case != "hash_unbound":
        proof["response_sha256"] = h(proof["response"])
    with pytest.raises(EvidenceInvalid):
        replay_fixture(fixture)


def test_old_noncompact_strict_function_retains_its_historical_empty_content_contract():
    client = client_for(BUDGET_POLICY_V3, output_policy=wire.CLEAN_OUTPUT_POLICY_V1)
    schema = keyed_schema()
    response = tool_response(wire.clean_compact_lists_output_v1(valid_answer(schema), schema, decode=True))
    response["choices"][0]["message"]["content"] = "SYNTHETIC_TEST visible text"
    install_responses(client, [response])
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic["code"] == "INCOMPLETE_MODEL_RESPONSE"
    assert client.budget.snapshot()["actual_tokens"] == 110
