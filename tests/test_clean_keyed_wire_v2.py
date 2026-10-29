"""Source-keyed clean answers preserve the original semantic gate; SYNTHETIC_TEST.

No real model/provider/network calls. The sixth invalid answer was not retained;
these counterexamples prove contracts, never reconstruct that unknown answer.
"""
from copy import deepcopy
import json

import pytest

from trading_system.llm import required
from trading_system.llm.deepseek_required import validate_schema
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from test_clean_capacity_wire_v2 import documents, schema_for, annotations
from test_short_term_research_v2 import public_document


def contract(count=2):
    docs = documents(count)
    if count:
        docs[0] = public_document(docs[0].doc_id,
            content="<SYNTHETIC_TEST unquoteable first part>" + "x" * 570 +
                    " SYNTHETIC_TEST source zero permitted second part.")
    payload = required.clean_payload_v2(docs[:8], all_docs=docs)
    ids = [row[0] for row in payload["all_source_index"]["rows"]]
    batch_ids = [row["id"] for row in payload["documents"]]
    return payload, schema_for(payload), ids, batch_ids


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_keyed_input_retains_every_body_metadata_registry_and_explicit_local_binding(count):
    payload, _, _, _ = contract(count)
    before = deepcopy(payload)
    wire = required.clean_keyed_payload_v2(payload)
    assert wire["clean_wire"]["schema"] == "tiger.clean-keyed-wire.v1"
    assert required.clean_keyed_payload_v2(wire, decode=True) == payload == before
    legacy = required.clean_payload_wire_v2(payload)
    assert {key: value for key, value in wire.items() if key != "clean_wire"} == {
        key: value for key, value in legacy.items() if key != "clean_wire"}
    bindings = wire["clean_wire"]["source_bindings"]
    assert bindings["columns"] == ["source_ref", "allowed_quote_part_ids"]
    assert bindings["rows"] == [[f"d{index}", [part[0] for part in row["content_parts"]["rows"] if part[2]]]
        for index, row in enumerate(payload["documents"])]
    assert "i" not in wire["clean_wire"]["output_fields"]["document"]
    assert "not a complete answer" in wire["clean_wire"]["format_example_scope"]
    if count:
        example = wire["clean_wire"]["format_example"]
        assert set(example["d"]) == {"d0"}
        assert example["d"]["d0"]["c"][0]["q"] == "p1"


@pytest.mark.parametrize("filler", ["x", "中", "😀"])
def test_keyed_schema_preserves_every_original_maximum_and_exact_semantic_values(filler):
    payload, schema, ids, batch = contract(55)
    value = annotations(payload, filler=filler)
    before, schema_before = deepcopy(value), deepcopy(schema)
    encoded = required.clean_keyed_output_v2(value, ids, batch_ids=batch)
    keyed_schema = required.clean_keyed_schema_v2(schema, ids)
    validate_schema(value, schema)
    validate_schema(encoded, keyed_schema)
    assert required.clean_keyed_output_v2(encoded, ids, batch_ids=batch, decode=True) == value == before
    assert schema == schema_before
    assert list(encoded["d"]) == [f"d{index}" for index in range(8)]
    assert all("i" not in row for row in encoded["d"].values())
    # The full common schema occurs once, not eight inlined copies.
    serialized = json.dumps(keyed_schema, ensure_ascii=False, separators=(",", ":"))
    assert serialized.count('"maxLength":800') == 1
    assert serialized.count('"maxLength":600') == 2
    assert "oneOf" not in serialized
    assert len(serialized.encode()) < 1.5 * len(json.dumps(
        required.clean_schema_wire_v2(schema, ids), ensure_ascii=False, separators=(",", ":")).encode())


def test_source_key_ownership_is_explicit_without_relying_on_model_array_order():
    payload, schema, ids, batch = contract(2)
    value = annotations(payload)
    reversed_value = {"documents": list(reversed(deepcopy(value["documents"])))}
    validate_schema(reversed_value, schema)  # The original schema permits this order.
    encoded = required.clean_keyed_output_v2(reversed_value, ids, batch_ids=batch)
    assert required.clean_keyed_output_v2(encoded, ids, batch_ids=batch, decode=True) == value
    assert list(encoded["d"]) == ["d0", "d1"]
    # Equality is the existing by-id/input-order semantic projection, not an
    # invented exact raw-array-order round trip for every old model JSON.
    assert reversed_value != value


def test_keyed_schema_never_silently_drops_an_unregistered_original_identity_constraint():
    _, schema, ids, _ = contract(2)
    schema["properties"]["documents"]["items"]["properties"]["id"]["maxLength"] = 1
    with pytest.raises(RequiredModelFailed):
        required.clean_keyed_schema_v2(schema, ids)


@pytest.mark.parametrize("mutation", ["missing_source", "extra_source", "foreign_quote",
    "unsafe_quote", "extra_identity", "missing_summary", "extra_field", "bad_origin",
    "oversize_summary", "oversize_origin", "oversize_statement", "oversize_claim_key",
    "too_many_claims", "newline", "wrong_type", "missing_claim_key"])
def test_keyed_boundaries_and_source_specific_quotes_reject_without_semantic_weakening(mutation):
    payload, schema, ids, batch = contract(2)
    answer = required.clean_keyed_output_v2(annotations(payload), ids, batch_ids=batch)
    row = answer["d"]["d0"]
    if mutation == "missing_source": del answer["d"]["d1"]
    elif mutation == "extra_source": answer["d"]["d999"] = deepcopy(row)
    elif mutation in {"foreign_quote", "unsafe_quote"}: row["c"][0]["q"] = "p0"
    elif mutation == "extra_identity": row["i"] = "d1"
    elif mutation == "missing_summary": del row["z"]
    elif mutation == "extra_field": row["SYNTHETIC_TEST_untrusted_key"] = "never logged"
    elif mutation == "bad_origin": row["p"]["o"] = "d999"
    elif mutation == "oversize_summary": row["z"] += "x"
    elif mutation == "oversize_origin": row["p"]["n"] += "x"
    elif mutation == "oversize_statement": row["c"][0]["s"] += "x"
    elif mutation == "oversize_claim_key": row["c"][0]["k"] += "x"
    elif mutation == "too_many_claims": row["c"].append({**row["c"][0], "k": "fifth"})
    elif mutation == "newline": row["z"] += "\n"
    elif mutation == "wrong_type": row["v"] = "0"
    else: del row["c"][0]["k"]
    with pytest.raises(LLMUnavailable):
        validate_schema(answer, required.clean_keyed_schema_v2(schema, ids))


@pytest.mark.parametrize("mutation", ["binding", "example", "extra_policy", "schema"])
def test_keyed_input_inverse_cannot_hide_policy_or_local_quote_tampering(mutation):
    payload, _, _, _ = contract(2)
    wire = required.clean_keyed_payload_v2(payload)
    policy = wire["clean_wire"]
    if mutation == "binding": policy["source_bindings"]["rows"][0][1].insert(0, "p0")
    elif mutation == "example": policy["format_example"]["d"]["d0"]["c"][0]["q"] = "p0"
    elif mutation == "extra_policy": policy["untrusted"] = "SYNTHETIC_TEST"
    else: policy["schema"] = "tiger.clean-keyed-wire.unknown"
    with pytest.raises(RequiredModelFailed):
        required.clean_keyed_payload_v2(wire, decode=True)


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_keyed_wire_direct_production_consumes_all_sources_and_keeps_original_quotes(count):
    from test_short_term_research_v2 import SemanticStub
    docs = documents(count)

    class Client:
        execution = SemanticStub({}).execution

        def __init__(self):
            self.requests = []

        def complete_json(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            payload = json.loads(kwargs["user"].split("\n", 1)[1])
            payload.pop("assessment")
            original = required.clean_keyed_payload_v2(payload, decode=True)
            ids = [row[0] for row in original["all_source_index"]["rows"]]
            batch = [row["id"] for row in original["documents"]]
            answer = required.clean_keyed_output_v2(annotations(original), ids, batch_ids=batch)
            validate_schema(answer, kwargs["schema"])
            return answer

    client = Client()
    actual = required.clean_v2(client, docs)
    assert [doc.raw for doc in actual] == docs
    assert len(client.requests) == max(1, (count + 7) // 8)
    assert all(request["max_tokens"] == 16384 and request["reasoning_effort"] == "low"
               for request in client.requests)
    assert all(len(doc.summary_zh) == 800 and len(doc.provenance["origin_note"]) == 600
               and len(doc.claims) == 4 for doc in actual)
    assert all(claim["quote"] in doc.raw.content for doc in actual for claim in doc.claims)
