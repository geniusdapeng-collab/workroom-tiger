"""Bounded synthetic checks for complete claim quartets; no network or repair."""
from copy import deepcopy
import json
import socket

import pytest
import requests

from trading_system.llm import clean_output as wire
from trading_system.llm.deepseek_required import _canonical, _hash, validate_schema
from trading_system.llm.request_budget import token_upper_bound
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_clean_compact_envelope_v1 import client_for, replay_captured
from test_clean_compact_regeneration_core_v1 import contract, generate, install_responses, tool_response
from test_clean_compact_lists_policy_proof_v1 import keyed_schema, valid_answer


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("NETWORK_FORBIDDEN_IN_CLAIM_QUARTET_TEST")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(requests, "post", deny)


def complete_answer(schema, count):
    answer = valid_answer(schema)
    for index in range(count):
        for field, value in (("s", "SYNTHETIC_TEST supported statement"), ("q", "p0"),
                             ("k", "synthetic_claim_" + str(index)), ("t", "background")):
            answer[f"d0__c__s{index}__{field}"] = value
    return answer


@pytest.mark.parametrize("mask", range(1, 15))
def test_partial_quartet_still_rejects_with_precise_original_leaf_location(mask):
    schema = keyed_schema()
    answer = complete_answer(schema, 1)
    fields = ("s", "q", "k", "t")
    for index, field in enumerate(fields):
        if mask & (1 << index):
            answer["d0__c__s0__" + field] = ""
    before = deepcopy(answer)
    validate_schema(answer, wire.clean_compact_lists_schema_v1(schema))
    first = next(field for index, field in enumerate(fields) if mask & (1 << index))
    with pytest.raises(RequiredModelUnavailable) as caught:
        wire.clean_compact_lists_output_v1(answer, schema, decode=True)
    assert caught.value.diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": "/d/d0/c/0/" + first,
        "schema_keyword": "minLength" if first in ("s", "k") else "enum"}
    assert answer == before


@pytest.mark.parametrize("count", [0, 1, 4])
def test_zero_one_four_complete_claims_preserve_exact_inverse_and_roundtrip(count):
    schema = keyed_schema()
    answer = complete_answer(schema, count)
    before = deepcopy(answer)
    decoded = wire.clean_compact_lists_output_v1(answer, schema, decode=True)
    assert len(decoded["d"]["d0"]["c"]) == count
    assert wire.clean_compact_lists_output_v1(decoded, schema) == answer == before


def test_provider_required_array_is_semantic_first_but_leaf_constraints_and_definitions_stay_exact():
    schema, _ = contract()
    local = wire.clean_compact_lists_schema_v1(schema)
    provider = wire.clean_compact_lists_provider_schema_v1(schema)
    expected_suffixes = ["z", "p__k", "p__o", "p__n", "p__t", "r", "t", "e", "h", "f", "s", "v"]
    expected_suffixes += [f"c__s{slot}__{field}" for slot in range(4) for field in ("s", "q", "k", "t")]
    assert provider["required"] == [f"d{source}__{suffix}" for source in range(4) for suffix in expected_suffixes]
    assert list(provider["properties"]) == sorted(local["properties"])
    projected = wire.expand_clean_compact_lists_provider_schema_v1(provider, keyed_schema=schema)
    expected = deepcopy(local)
    for leaf in expected["properties"].values():
        leaf.pop("minLength", None)
        leaf.pop("maxLength", None)
    assert projected == expected
    # Rebuild the original scalar interning in original lexicographic order.
    definitions, aliases, properties = {}, {}, {}
    for name, leaf in expected["properties"].items():
        identity = _canonical(leaf)
        if identity not in aliases:
            alias = "l" + str(len(aliases))
            aliases[identity], definitions[alias] = alias, deepcopy(leaf)
        properties[name] = {"$ref": "#/$def/" + aliases[identity]}
    assert provider["properties"] == properties and provider["$def"] == definitions
    # Actual canonical HTTP bytes sort properties; only required array retains
    # the requested order. This does not claim a provider emission guarantee.
    serialized = json.loads(_canonical(provider))
    assert serialized["required"] == provider["required"]
    assert list(serialized["properties"]) == sorted(local["properties"])


@pytest.mark.parametrize("change", ["reverse", "lexicographic", "duplicate"])
def test_noncanonical_required_array_is_rejected_by_expansion_and_budget_boundary(change):
    schema = keyed_schema()
    request = wire.build_clean_compact_lists_request(system="SYNTHETIC_TEST system",
        user="SYNTHETIC_TEST unchanged complete source", schema_hint={}, schema=schema)
    provider = request["tools"][0]["function"]["parameters"]
    if change == "reverse":
        provider["required"].reverse()
    elif change == "lexicographic":
        provider["required"] = sorted(provider["properties"])
    else:
        provider["required"][0] = provider["required"][1]
    with pytest.raises(wire.CleanOutputInvalid):
        wire.expand_clean_compact_lists_provider_schema_v1(provider, keyed_schema=schema)
    assert not wire.clean_compact_lists_function_request_valid(request)


def test_partial_then_whole_valid_generation_preserves_full_input_charges_proof_and_precise_feedback():
    client, schema = client_for(), keyed_schema()
    bad, good = complete_answer(schema, 0), complete_answer(schema, 1)
    bad["d0__c__s0__k"] = "SYNTHETIC_TEST_PARTIAL_KEY_LOWER".lower()
    seen = install_responses(client, [tool_response(bad, 1), tool_response(good, 2)])
    assert generate(client, schema) == good and len(seen) == 2
    first, second = [row["output"] for row in client.execution.records]
    assert first["rejection_diagnostic"]["schema_path"] == "/d/d0/c/0/s"
    assert first["rejection_diagnostic"]["schema_keyword"] == "minLength"
    assert second["previous_rejection_sha256"] == _hash(first)
    assert seen[0][1]["messages"][1] == seen[1][1]["messages"][1]
    assert seen[0][1]["tools"] == seen[1][1]["tools"]
    for _, request in seen:
        assert "s/q/k/t must either all be nonempty" in request["messages"][0]["content"]
        assert "including claim_key k" in request["messages"][0]["content"]
        assert "required array order" in request["messages"][0]["content"]
        assert wire.clean_compact_lists_function_request_valid(request)
        assert token_upper_bound(request, allow_clean_compact_regeneration=True)[1] == 32768
        assert "synthetic_test_partial_key_lower" not in _canonical(request).decode()
    assert first["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == json.dumps(bad, ensure_ascii=False)
    replay, answer = replay_captured(client, schema)
    assert answer == good and replay.receipts == client.receipts
    assert client.budget.snapshot()["actual_tokens"] == replay.budget.snapshot()["actual_tokens"] == 220


def test_request_guard_requires_fixed_quartet_instruction_without_touching_user_payload():
    schema, user = keyed_schema(), "SYNTHETIC_TEST exact input 中文\nspacing  "
    request = wire.build_clean_compact_lists_request(system="SYNTHETIC_TEST system", user=user,
        schema_hint={}, schema=schema)
    assert request["messages"][1] == {"role": "user", "content": user}
    assert "s/q/k/t must either all be nonempty" in request["messages"][0]["content"]
    request["messages"][0]["content"] = "SYNTHETIC_TEST system" + wire.CLEAN_OUTPUT_INSTRUCTION
    assert not wire.clean_compact_lists_function_request_valid(request)
