"""SYNTHETIC_TEST documented optional metadata, never a replay of ninth raw body.

DeepSeek declares Responses format compatibility. Official OpenAPI-generated
OpenAI types allow message.phase and output_text.logprobs as optional metadata:
https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/responses/response_output_message.py
https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/responses/response_output_text.py
Only final-answer/empty metadata is admitted by this bounded clean protocol.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from trading_system.llm import deepseek_required as transport
from trading_system.llm.clean_output import build_clean_responses_safe_response
from trading_system.redline.model_diagnostics import RequiredModelUnavailable, validate_model_diagnostic
from test_deepseek_required import configured, Response, KEY
from test_clean_responses_output_v1 import responses_client, keyed_contract, native_response, call

ABSENT = "SYNTHETIC_ABSENT_METADATA"


@pytest.mark.parametrize("phase", [ABSENT, None, "final_answer"])
@pytest.mark.parametrize("logprobs", [ABSENT, None, []])
@pytest.mark.parametrize("annotations", [ABSENT, []])
def test_documented_optional_metadata_keeps_exact_semantics_and_sealed_projection(
        configured, monkeypatch, phase, logprobs, annotations):
    *_, schema, answer = keyed_contract()
    original_answer, original_schema = deepcopy(answer), deepcopy(schema)
    reply = native_response(answer)
    message = reply["output"][0]
    part = message["content"][0]
    if phase != ABSENT:
        message["phase"] = phase
    if logprobs != ABSENT:
        part["logprobs"] = deepcopy(logprobs)
    if annotations == ABSENT:
        part.pop("annotations")
    response = Response(reply)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), response)[1])
    client = responses_client(configured)
    assert call(client, schema) == answer == original_answer
    assert schema == original_schema and response.closed and seen == [1]
    proof = configured.calls[0]["output"]
    expected = build_clean_responses_safe_response(response_id=reply["id"], created_at=reply["created_at"],
        usage=reply["usage"], parsed=answer, message_id=message["id"])
    assert proof["response"] == expected and proof["response_sha256"] == transport._hash(expected)
    assert proof["transport_response_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert "phase" not in proof["response"]["output"][0]
    assert set(proof["response"]["output"][0]["content"][0]) == {"type", "text"}
    assert client.budget.snapshot()["actual_tokens"] == 50 and client.budget.snapshot()["reserved_tokens"] == 0


@pytest.mark.parametrize("field,value", [("phase", "final_answer"), ("logprobs", [])])
def test_original_documented_native_shape_counterexample(configured, monkeypatch, field, value):
    """Original exact-set code fails each legitimate optional field in isolation."""
    *_, schema, answer = keyed_contract()
    reply = native_response(answer)
    if field == "phase":
        reply["output"][0][field] = value
    else:
        reply["output"][0]["content"][0][field] = value
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: Response(reply))
    assert call(responses_client(configured), schema) == answer


@pytest.mark.parametrize("mutation,code", [
    ("status", "RESPONSES_NOT_COMPLETED"),
    ("created", "RESPONSES_INVALID_CREATED_AT"),
    ("error", "RESPONSES_ERROR_PRESENT"),
    ("incomplete_details", "RESPONSES_INCOMPLETE_DETAILS_PRESENT"),
    ("output_type", "RESPONSES_INVALID_OUTPUT_TYPE"),
    ("output_count", "RESPONSES_INVALID_OUTPUT_COUNT"),
    ("output_item", "RESPONSES_INVALID_OUTPUT_ITEM"),
    ("message_fields", "RESPONSES_INVALID_MESSAGE_FIELDS"),
    ("message_missing", "RESPONSES_INVALID_MESSAGE_FIELDS"),
    ("message_type", "RESPONSES_UNEXPECTED_OUTPUT_TYPE"),
    ("message_status", "RESPONSES_MESSAGE_NOT_COMPLETED"),
    ("message_role", "RESPONSES_INVALID_MESSAGE_ROLE"),
    ("phase_commentary", "RESPONSES_INVALID_MESSAGE_PHASE"),
    ("phase_unknown", "RESPONSES_INVALID_MESSAGE_PHASE"),
    ("phase_object", "RESPONSES_INVALID_MESSAGE_PHASE"),
    ("content_type", "RESPONSES_INVALID_CONTENT_TYPE"),
    ("content_count", "RESPONSES_INVALID_CONTENT_COUNT"),
    ("part_type", "RESPONSES_INVALID_CONTENT_PART"),
    ("part_fields", "RESPONSES_INVALID_CONTENT_FIELDS"),
    ("part_missing", "RESPONSES_INVALID_CONTENT_FIELDS"),
    ("content_kind", "RESPONSES_UNEXPECTED_CONTENT_TYPE"),
    ("annotations_nonempty", "RESPONSES_INVALID_ANNOTATIONS"),
    ("annotations_null", "RESPONSES_INVALID_ANNOTATIONS"),
    ("annotations_object", "RESPONSES_INVALID_ANNOTATIONS"),
    ("logprobs_nonempty", "RESPONSES_INVALID_LOGPROBS"),
    ("logprobs_object", "RESPONSES_INVALID_LOGPROBS"),
    ("logprobs_string", "RESPONSES_INVALID_LOGPROBS"),
])
def test_each_native_rejection_branch_has_fixed_value_free_diagnostic(configured, monkeypatch, mutation, code):
    *_, schema, answer = keyed_contract()
    reply = native_response(answer)
    message, part = reply["output"][0], reply["output"][0]["content"][0]
    secret = KEY + " SYNTHETIC_REJECTED_NATIVE_METADATA"
    if mutation == "status": reply["status"] = "incomplete"
    elif mutation == "created": reply["created_at"] = True
    elif mutation == "error": reply["error"] = {"message": secret}
    elif mutation == "incomplete_details": reply["incomplete_details"] = {"reason": secret}
    elif mutation == "output_type": reply["output"] = {"value": secret}
    elif mutation == "output_count": reply["output"] = []
    elif mutation == "output_item": reply["output"] = [secret]
    elif mutation == "message_fields": message[secret] = secret
    elif mutation == "message_missing": message.pop("role")
    elif mutation == "message_type": message["type"] = "reasoning"
    elif mutation == "message_status": message["status"] = "incomplete"
    elif mutation == "message_role": message["role"] = "user"
    elif mutation == "phase_commentary": message["phase"] = "commentary"
    elif mutation == "phase_unknown": message["phase"] = secret
    elif mutation == "phase_object": message["phase"] = {"value": secret}
    elif mutation == "content_type": message["content"] = {"text": secret}
    elif mutation == "content_count": message["content"] = []
    elif mutation == "part_type": message["content"] = [secret]
    elif mutation == "part_fields": part[secret] = secret
    elif mutation == "part_missing": part.pop("text")
    elif mutation == "content_kind": part["type"] = "reasoning_text"
    elif mutation == "annotations_nonempty": part["annotations"] = [{"text": secret}]
    elif mutation == "annotations_null": part["annotations"] = None
    elif mutation == "annotations_object": part["annotations"] = {"text": secret}
    elif mutation == "logprobs_nonempty": part["logprobs"] = [{"token": secret}]
    elif mutation == "logprobs_object": part["logprobs"] = {"token": secret}
    else: part["logprobs"] = secret
    response = Response(reply)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), response)[1])
    client = responses_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(client, schema)
    assert caught.value.diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
    assert validate_model_diagnostic(caught.value.diagnostic) == caught.value.diagnostic
    assert caught.value.accounting["status"] == "settled" and caught.value.accounting["actual_tokens"] == 50
    assert caught.value.accounting["reserved_tokens"] == 0 and client.receipts == []
    assert response.closed and seen == [1]
    persisted = json.dumps([configured.calls, caught.value.diagnostic, caught.value.accounting, str(caught.value)])
    assert KEY not in persisted and "SYNTHETIC_REJECTED_NATIVE_METADATA" not in persisted


def test_historical_generic_incomplete_diagnostic_remains_valid():
    diagnostic = {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": "INCOMPLETE_MODEL_RESPONSE"}
    assert validate_model_diagnostic(diagnostic) == diagnostic


@pytest.mark.parametrize("mutation", ["wrong_quote", "oversized_summary", "secret_text", "missing_source"])
def test_optional_metadata_never_weakens_original_semantic_gates(configured, monkeypatch, mutation):
    *_, schema, answer = keyed_contract()
    if mutation == "wrong_quote": answer["d"]["d0"]["c"][0]["q"] = "p0"
    elif mutation == "oversized_summary": answer["d"]["d0"]["z"] += "中"
    elif mutation == "secret_text": answer["d"]["d0"]["z"] = KEY
    else: answer["d"].pop("d1")
    reply = native_response(answer)
    reply["output"][0]["phase"] = "final_answer"
    reply["output"][0]["content"][0]["logprobs"] = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: Response(reply))
    client = responses_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(client, schema)
    assert caught.value.diagnostic["code"] in {"OUTPUT_SCHEMA_MISMATCH", "INVALID_JSON_VALUE"}
    assert client.receipts == [] and caught.value.accounting["status"] == "settled"
