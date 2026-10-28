"""Synthetic compact metadata compatibility and raw transport limits; no network."""
from copy import deepcopy
from hashlib import sha256
import io
import json
import math
import socket

import pytest
import requests

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.llm import clean_output as wire
from trading_system.llm.deepseek_required import MAX_RESPONSE_BYTES, _canonical, _hash
from trading_system.llm.request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_clean_compact_envelope_v1 import client_for, replay_captured
from test_clean_compact_regeneration_core_v1 import TEST_KEY, generate, install_responses, tool_response
from test_clean_compact_lists_policy_proof_v1 import keyed_schema, pure_fixture, replay_fixture, valid_answer


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("NETWORK_FORBIDDEN_IN_COMPACT_METADATA_TEST")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(requests, "post", deny)


@pytest.mark.parametrize("policy", [BUDGET_POLICY_V3, BUDGET_POLICY_V4])
@pytest.mark.parametrize("location", ["tool", "function"])
@pytest.mark.parametrize("metadata", [
    {"index": 0},
    {"synthetic_extra": None, "synthetic_flag": False, "synthetic_score": 1.25},
    {"synthetic_extra": {"id": "synthetic_wrong_id", "type": "synthetic_wrong_type",
        "function": {"name": "synthetic_wrong_name", "arguments": '{"fake":"SYNTHETIC_METADATA_MARKER"}'},
        "arguments": '{"fake":"SYNTHETIC_METADATA_MARKER"}'}},
    {"synthetic_extra": [TEST_KEY, "api_key=SYNTHETIC_METADATA_MARKER",
                         "https://example.com/?token=SYNTHETIC_METADATA_MARKER"],
     "api_key=SYNTHETIC_METADATA_MARKER": "SYNTHETIC_METADATA_MARKER"},
], ids=["index", "scalars", "nested_fake_arguments", "synthetic_credentials"])
def test_ignored_unconsumed_metadata_never_enters_answer_projection_or_replay(policy, location, metadata):
    client, schema = client_for(policy), keyed_schema()
    expected = valid_answer(schema)
    response = tool_response(expected)
    tool = response["choices"][0]["message"]["tool_calls"][0]
    target = tool if location == "tool" else tool["function"]
    target.update(deepcopy(metadata))
    before = deepcopy(response)
    seen = install_responses(client, [response])
    assert generate(client, schema) == expected and response == before and len(seen) == 1
    proof = client.execution.records[0]["output"]
    assert proof["transport_response_sha256"] == sha256(json.dumps(response, ensure_ascii=True).encode()).hexdigest()
    projected_tool = proof["response"]["choices"][0]["message"]["tool_calls"][0]
    assert set(projected_tool) == {"id", "type", "function"}
    assert set(projected_tool["function"]) == {"name", "arguments"}
    assert json.loads(projected_tool["function"]["arguments"]) == expected == proof["parsed"]
    captured = _canonical(client.execution.records).decode()
    assert "synthetic_extra" not in captured and "SYNTHETIC_METADATA_MARKER" not in captured and TEST_KEY not in captured
    replay, answer = replay_captured(client, schema)
    assert answer == expected and replay.receipts == client.receipts
    assert replay.budget.snapshot()["actual_tokens"] == 110


@pytest.mark.parametrize("location,field,code", [
    ("tool", "id", "COMPACT_INVALID_TOOL_FIELDS"),
    ("tool", "type", "COMPACT_INVALID_TOOL_FIELDS"),
    ("tool", "function", "COMPACT_INVALID_TOOL_FIELDS"),
    ("function", "name", "COMPACT_INVALID_FUNCTION_FIELDS"),
    ("function", "arguments", "COMPACT_INVALID_FUNCTION_FIELDS"),
])
def test_required_fields_cannot_be_satisfied_by_nested_metadata(location, field, code):
    client, schema = client_for(), keyed_schema()
    response = tool_response(valid_answer(schema))
    tool = response["choices"][0]["message"]["tool_calls"][0]
    target = tool if location == "tool" else tool["function"]
    target["synthetic_extra"] = {field: target.pop(field)}
    seen = install_responses(client, [response])
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic["code"] == code
    assert len(seen) == 1 and not client.receipts and client._clean_regenerations_used == 0
    assert caught.value.accounting["status"] == "settled" and caught.value.accounting["actual_tokens"] == 110
    assert "synthetic_extra" not in _canonical(client.execution.records).decode()


@pytest.mark.parametrize("location,code", [("tool", "COMPACT_TOOL_NOT_OBJECT"),
                                         ("function", "COMPACT_FUNCTION_NOT_OBJECT")])
@pytest.mark.parametrize("value", [None, [], "SYNTHETIC_METADATA_MARKER", False, 0])
def test_nonobject_tool_and_function_have_distinct_fixed_codes(location, code, value):
    client, schema = client_for(), keyed_schema()
    response = tool_response(valid_answer(schema))
    tools = response["choices"][0]["message"]["tool_calls"]
    if location == "tool":
        tools[0] = value
    else:
        tools[0]["function"] = value
    install_responses(client, [response])
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic["code"] == code
    assert caught.value.accounting["actual_tokens"] == 110
    assert "SYNTHETIC_METADATA_MARKER" not in _canonical(client.execution.records).decode()


def install_raw_body(monkeypatch, body):
    seen = []
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/json", "Content-Encoding": "identity"}
        def __init__(self):
            self.raw = io.BytesIO(body)
        def close(self):
            self.raw.close()
    def post(url, **options):
        seen.append((url, options))
        return Response()
    monkeypatch.setattr(requests, "post", post)
    return seen


@pytest.mark.parametrize("token", ["1.25", "1e200", "-1e200"])
def test_actual_transport_binds_full_metadata_body_but_projects_only_consumed_fields(monkeypatch, token):
    client, schema = client_for(), keyed_schema()
    response = tool_response(valid_answer(schema))
    response["choices"][0]["message"]["tool_calls"][0]["synthetic_extra"] = "SYNTHETIC_NUMBER"
    body = json.dumps(response).replace('"SYNTHETIC_NUMBER"', token).encode()
    seen = install_raw_body(monkeypatch, body)
    assert generate(client, schema) == valid_answer(schema) and len(seen) == 1
    proof = client.execution.records[0]["output"]
    assert proof["transport_response_sha256"] == sha256(body).hexdigest()
    assert "synthetic_extra" not in _canonical(client.execution.records).decode()


@pytest.mark.parametrize("case,code", [
    ("duplicate", "DUPLICATE_RESPONSE_JSON_KEY"),
    ("nan", "NONFINITE_RESPONSE_JSON"),
    ("infinity", "NONFINITE_RESPONSE_JSON"),
    ("positive_overflow", "NONFINITE_RESPONSE_JSON"),
    ("negative_overflow", "NONFINITE_RESPONSE_JSON"),
    ("body_limit", "RESPONSE_TOO_LARGE"),
])
def test_raw_ignored_metadata_still_obeys_strict_json_and_full_body_limit(monkeypatch, case, code):
    client, schema = client_for(), keyed_schema()
    response = tool_response(valid_answer(schema))
    tool = response["choices"][0]["message"]["tool_calls"][0]
    tool["synthetic_extra"] = "SYNTHETIC_RAW_TOKEN"
    replacement = {"duplicate": '{"same":1,"same":2}', "nan": "NaN", "infinity": "Infinity",
                   "positive_overflow": "1e999", "negative_overflow": "-1e999",
                   "body_limit": '"' + "X" * (MAX_RESPONSE_BYTES + 1) + '"'}[case]
    body = json.dumps(response).replace('"SYNTHETIC_RAW_TOKEN"', replacement).encode()
    seen = install_raw_body(monkeypatch, body)
    with pytest.raises(RequiredModelUnavailable) as caught:
        generate(client, schema)
    assert caught.value.diagnostic["code"] == code
    assert len(seen) == 1 and not client.receipts and client._clean_regenerations_used == 0
    budget = client.budget.snapshot()
    assert budget["frozen"] and budget["actual_tokens"] == 0 and budget["reserved_tokens"] > 0
    assert "synthetic_extra" not in _canonical(client.execution.records).decode()


@pytest.mark.parametrize("output_policy,path", [
    (wire.CLEAN_OUTPUT_POLICY_V1, wire.CLEAN_OUTPUT_PATH),
    (wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, "/v1/tools/search"),
    (wire.CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, "/v1/tools/fetch"),
])
def test_finite_float_transport_change_is_scoped_to_compact_chat(monkeypatch, output_policy, path):
    client = client_for(BUDGET_POLICY_V3, output_policy=output_policy)
    body = b'{"synthetic_extra":1e999}'
    install_raw_body(monkeypatch, body)
    response, body_hash = client._post_json(path, {"synthetic": "request"})
    assert math.isinf(response["synthetic_extra"]) and body_hash == sha256(body).hexdigest()


def test_extra_metadata_cannot_override_invalid_closed_arguments_or_regeneration_binding():
    client, schema = client_for(), keyed_schema()
    expected = valid_answer(schema)
    invalid = {**expected, "SYNTHETIC_EXTRA_ARGUMENT": "invalid"}
    responses = [tool_response(invalid, 1), tool_response(expected, 2)]
    for response in responses:
        tool = response["choices"][0]["message"]["tool_calls"][0]
        tool["synthetic_extra"] = {"arguments": json.dumps(expected)}
        tool["function"]["synthetic_extra"] = {"arguments": json.dumps(expected)}
    seen = install_responses(client, responses)
    assert generate(client, schema) == expected and len(seen) == 2
    rejected, accepted = [row["output"] for row in client.execution.records]
    assert rejected["outcome"] == "rejected" and rejected["rejection_diagnostic"]["schema_keyword"] == "additionalProperties"
    assert accepted["previous_rejection_sha256"] == _hash(rejected)
    assert "synthetic_extra" not in _canonical(client.execution.records).decode()
    replay, answer = replay_captured(client, schema)
    assert answer == expected and replay.budget.snapshot()["actual_tokens"] == 220


@pytest.mark.parametrize("location", ["tool", "function"])
def test_replay_rejects_metadata_smuggled_into_closed_safe_projection_even_rehashed(location):
    fixture = pure_fixture(rejection=False)
    proof = fixture[3][0]["proof"]
    tool = proof["response"]["choices"][0]["message"]["tool_calls"][0]
    target = tool if location == "tool" else tool["function"]
    target["synthetic_extra"] = {"arguments": "SYNTHETIC_METADATA_MARKER"}
    proof["response_sha256"] = _hash(proof["response"])
    with pytest.raises(EvidenceInvalid):
        replay_fixture(fixture)
