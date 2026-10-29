"""Responses clean protocol contracts. Every source/HTTP reply is SYNTHETIC_TEST."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pytest

from trading_system.llm import deepseek_required as transport
from trading_system.llm import required
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_deepseek_required import configured, Response, chat_response, SCHEMA, KEY
from test_clean_strict_output_v2 import keyed_contract

POLICY = "tiger.clean-output.responses-schema.v1"


def responses_client(configured):
    configured.upgrade = True
    configured.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    return transport.DeepSeekRequiredClient(configured)


def native_response(answer):
    return {"id": "SYNTHETIC_RESPONSE_1", "object": "response", "created_at": 1793078400,
        "status": "completed", "model": "deepseek-flash", "output": [{"type": "message",
            "id": "SYNTHETIC_MESSAGE_1", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}]}],
        "usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50,
            "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 0}},
        "store": False, "parallel_tool_calls": True, "previous_response_id": None,
        "error": None, "incomplete_details": None}


def call(client, schema):
    return client.complete_json(system="SYNTHETIC_TEST 原系统", user="SYNTHETIC_TEST 完整资料😀",
        schema_hint={}, schema=schema, reasoning_effort="none")


def test_explicit_policy_sends_complete_native_schema_and_truthful_v3_proof(configured, monkeypatch):
    *_, schema, answer = keyed_contract()
    original = deepcopy(schema)
    response = Response(native_response(answer))
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (seen.append((url, deepcopy(kw["json"]), kw)), response)[1])
    client = responses_client(configured)
    assert call(client, schema) == answer
    assert len(seen) == 1 and response.closed
    endpoint, body, options = seen[0]
    assert endpoint == "https://api.deepseek.com/responses"
    assert set(body) == {"model", "instructions", "input", "reasoning", "max_output_tokens", "temperature", "stream", "text"}
    assert body["instructions"] == "SYNTHETIC_TEST 原系统"
    assert body["input"] == "SYNTHETIC_TEST 完整资料😀"
    assert body["reasoning"] == {"effort": "none"} and body["max_output_tokens"] == 16384
    assert body["temperature"] == .2 and body["stream"] is False and body["model"] == "deepseek-flash"
    assert body["text"] == {"format": {"type": "json_schema", "name": "tiger_clean_v2", "schema": original}}
    assert schema == original  # No projection, allOf/own-q/bounds survive exactly.
    assert options["stream"] is True and options["allow_redirects"] is False and options["verify"] is True
    invocation = configured.calls[0]
    proof = invocation["output"]
    assert invocation["inputs"]["schema"] == "tiger.deepseek-request.v3"
    assert invocation["inputs"]["output_schema"] == original
    assert invocation["inputs"]["output_policy_id"] == POLICY
    assert proof["schema"] == "tiger.deepseek-call.v3" and proof["response_status"] == "completed"
    assert proof["created_at"] == 1793078400 and proof["output_message_id"] == "SYNTHETIC_MESSAGE_1"
    assert proof["usage"] == {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
    assert proof["native_usage"] == native_response(answer)["usage"]
    safe = proof["response"]
    assert safe["object"] == "response" and safe["status"] == "completed"
    assert safe["usage"] == proof["native_usage"]
    assert json.loads(safe["output"][0]["content"][0]["text"]) == answer
    assert "annotations" not in safe["output"][0]["content"][0]
    for key in ("finish_reason", "output_tool_call_id", "system_fingerprint"):
        assert key not in proof and key not in client.receipts[0]
    assert proof["transport_response_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert proof["response_sha256"] == transport._hash(safe)
    assert client.budget.snapshot()["actual_tokens"] == 50
    assert client.budget.snapshot()["reserved_tokens"] == 0
    assert KEY not in json.dumps(configured.calls)


@pytest.mark.parametrize("count", [0, 1, 8, 9, 56])
def test_builder_preserves_original_complete_keyed_schema_without_mutation(count):
    from trading_system.llm.clean_output import build_clean_responses_request, clean_responses_request_valid
    *_, schema, answer = keyed_contract(count)
    before = deepcopy(schema)
    request = build_clean_responses_request(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=schema)
    assert request["text"]["format"]["schema"] == before and schema == before
    assert clean_responses_request_valid(request)
    transport.validate_schema(answer, request["text"]["format"]["schema"])
    request["text"]["format"]["schema"]["additionalProperties"] = True
    assert schema == before  # Detached request, no shared mutable schema.


def responses_request():
    from trading_system.llm.clean_output import build_clean_responses_request
    *_, schema, _ = keyed_contract()
    return build_clean_responses_request(system="SYNTHETIC_TEST 中文😀", user="SYNTHETIC_TEST 中文😀", schema_hint={}, schema=schema)


def test_native_request_budget_counts_entire_canonical_utf8_and_requires_two_explicit_policies():
    from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, token_upper_bound
    body = responses_request()
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    bound = token_upper_bound(body, allow_clean_responses=True)
    assert bound == (len(canonical) + 4096 + 128, 16384)
    assert len(canonical) > len(canonical.decode())
    for flags in ({}, {"allow_clean_function": True}):
        with pytest.raises(BudgetRefused):
            token_upper_bound(body, **flags)
    for policy in (None, "tiger.budget-policy.v3"):
        with pytest.raises(BudgetRefused):
            BudgetGuard.for_policy(policy).reserve("chat", "a" * 32, body)
    for policy in (None, "tiger.budget-policy.v2"):
        with pytest.raises(BudgetRefused):
            BudgetGuard.for_policy(policy, clean_output_policy_id=POLICY)
    guard = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    reservation = guard.reserve("chat", "a" * 32, body)
    assert reservation["token_upper_bound"] == sum(bound)
    assert guard.limits == {"max_chat_calls": 40, "max_tokens": 600000, "max_search_calls": 60,
        "max_fetch_calls": 20, "max_remote_requests": 120, "max_cost_usd": 1., "deadline_seconds": 2400}


@pytest.mark.parametrize("mutation", ["model", "array_input", "input_tool", "empty_input", "empty_instructions",
    "reasoning", "reasoning_summary", "max_output", "temperature", "stream", "tools", "tool_choice",
    "strict", "previous_response_id", "conversation", "metadata", "response_format", "format_name", "format_type",
    "format_extra", "open_schema", "arbitrary_schema", "schema_keyword", "unknown_field"])
def test_closed_budget_refuses_unreviewed_native_protocol_and_tool_loops(mutation):
    from trading_system.llm.clean_output import clean_responses_request_valid
    from trading_system.llm.request_budget import BudgetRefused, token_upper_bound
    body = responses_request()
    if mutation == "model": body["model"] = "deepseek-v4-pro"
    elif mutation == "array_input": body["input"] = [{"role": "user", "content": body["input"]}]
    elif mutation == "input_tool": body["input"] = [{"type": "function_call_output", "output": "SYNTHETIC_TEST"}]
    elif mutation == "empty_input": body["input"] = " "
    elif mutation == "empty_instructions": body["instructions"] = " "
    elif mutation == "reasoning": body["reasoning"]["effort"] = "high"
    elif mutation == "reasoning_summary": body["reasoning"]["summary"] = "auto"
    elif mutation == "max_output": body["max_output_tokens"] = 16385
    elif mutation == "temperature": body["temperature"] = 0.3
    elif mutation == "stream": body["stream"] = True
    elif mutation == "format_name": body["text"]["format"]["name"] = "other"
    elif mutation == "format_type": body["text"]["format"]["type"] = "json_object"
    elif mutation == "format_extra": body["text"]["format"]["strict"] = True
    elif mutation == "open_schema": body["text"]["format"]["schema"]["additionalProperties"] = True
    elif mutation == "arbitrary_schema": body["text"]["format"]["schema"] = {"type": "object"}
    elif mutation == "schema_keyword": body["text"]["format"]["schema"]["$ref"] = "https://example.com/schema"
    else: body[mutation] = None
    assert clean_responses_request_valid(body) is False
    with pytest.raises(BudgetRefused):
        token_upper_bound(body, allow_clean_responses=True)


@pytest.mark.parametrize("mutation", ["incomplete", "failed", "in_progress", "bad_status", "object", "model",
    "response_id", "message_id", "created_bool", "created_zero", "empty_output", "double_message", "reasoning",
    "function_call", "custom_tool", "message_status", "role", "part_type", "double_text", "empty_text", "annotations",
    "tool_in_message", "hidden_reasoning", "error", "incomplete_details", "nonobject", "duplicate_json", "nonfinite",
    "malformed_json", "missing_source", "own_quote", "too_long", "fifth_claim", "secret", "credential_url"])
def test_rejected_native_output_is_never_repaired_retried_or_unbilled(configured, monkeypatch, mutation):
    *_, schema, answer = keyed_contract()
    reply = native_response(answer)
    item, part = reply["output"][0], reply["output"][0]["content"][0]
    if mutation in {"incomplete", "failed", "in_progress"}: reply["status"] = mutation
    elif mutation == "bad_status": reply["status"] = "SYNTHETIC_REJECTED_TEXT"
    elif mutation == "object": reply["object"] = "chat.completion"
    elif mutation == "model": reply["model"] = "deepseek-v4-pro"
    elif mutation == "response_id": reply["id"] = KEY
    elif mutation == "message_id": item["id"] = KEY
    elif mutation == "created_bool": reply["created_at"] = True
    elif mutation == "created_zero": reply["created_at"] = 0
    elif mutation == "empty_output": reply["output"] = []
    elif mutation == "double_message": reply["output"].append(deepcopy(item))
    elif mutation in {"reasoning", "function_call", "custom_tool"}:
        reply["output"].insert(0, {"type": mutation, "text": "SYNTHETIC_REJECTED_TEXT"})
    elif mutation == "message_status": item["status"] = "incomplete"
    elif mutation == "role": item["role"] = "user"
    elif mutation == "part_type": part["type"] = "reasoning_text"
    elif mutation == "double_text": item["content"].append(deepcopy(part))
    elif mutation == "empty_text": part["text"] = " "
    elif mutation == "annotations": part["annotations"] = [{"text": "SYNTHETIC_REJECTED_TEXT"}]
    elif mutation == "tool_in_message": item["tool_calls"] = [{}]
    elif mutation == "hidden_reasoning": item["reasoning_content"] = "SYNTHETIC_REJECTED_TEXT"
    elif mutation == "error": reply["error"] = {"message": "SYNTHETIC_REJECTED_TEXT"}
    elif mutation == "incomplete_details": reply["incomplete_details"] = {"reason": "max_output_tokens"}
    elif mutation == "nonobject": part["text"] = "[]"
    elif mutation == "duplicate_json": part["text"] = '{"d":{},"d":{}}'
    elif mutation == "nonfinite": part["text"] = '{"d":NaN}'
    elif mutation == "malformed_json": part["text"] = "{SYNTHETIC_REJECTED_TEXT"
    else:
        row = answer["d"]["d0"]
        if mutation == "missing_source": del answer["d"]["d1"]
        elif mutation == "own_quote": row["c"][0]["q"] = "p0"
        elif mutation == "too_long": row["z"] += "字"
        elif mutation == "fifth_claim": row["c"].append({**row["c"][0], "k": "fifth"})
        elif mutation == "secret": row["z"] = KEY
        else: row["z"] = "https://example.com/?token=private-response"
        part["text"] = json.dumps(answer)
    seen = []
    response = Response(reply)
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), response)[1])
    client = responses_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(client, schema)
    assert seen == [1] and response.closed and client.receipts == []
    state = client.budget.snapshot()
    assert state["actual_tokens"] == 50 and state["reserved_tokens"] == 0 and state["frozen"] is False
    accounting = caught.value.accounting
    assert accounting["schema"] == "tiger.model-call-accounting.v4" and accounting["status"] == "settled"
    assert accounting["native_usage"] == reply["usage"] and "finish_reason" not in accounting
    serialized = json.dumps([configured.calls, caught.value.diagnostic, accounting, str(caught.value)])
    assert all(secret not in serialized for secret in (KEY, "SYNTHETIC_REJECTED_TEXT", "private-response"))


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True, "output_tokens": 10, "total_tokens": 11},
    {"input_tokens": 40, "output_tokens": 10, "total_tokens": 51},
    {"input_tokens": 40, "output_tokens": 16385, "total_tokens": 16425},
    {"input_tokens": 600000, "output_tokens": 10, "total_tokens": 600010},
    {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50, "input_tokens_details": {"cached_tokens": 41}},
    {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50, "output_tokens_details": {"reasoning_tokens": 11}}])
def test_unknown_inconsistent_or_overrun_native_usage_retains_reservation_and_freezes(configured, monkeypatch, usage):
    *_, schema, answer = keyed_contract()
    reply = native_response(answer)
    reply["usage"] = usage
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), Response(reply))[1])
    client = responses_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(client, schema)
    accounting = caught.value.accounting
    assert accounting["status"] == "unknown" and accounting["frozen"] is True
    assert accounting["usage"] is None and accounting["native_usage"] is None
    assert accounting["actual_tokens"] == 0 and accounting["reserved_tokens"] > 16384
    with pytest.raises(RequiredModelUnavailable, match="REQUEST_BUDGET_REFUSED"):
        call(client, schema)
    assert seen == [1] and client.receipts == []


@pytest.mark.parametrize("reuse", ["id", "message"])
def test_native_response_and_message_identities_cannot_be_reused(configured, monkeypatch, reuse):
    *_, schema, answer = keyed_contract()
    replies = [native_response(answer), native_response(answer)]
    if reuse == "id": replies[1]["output"][0]["id"] += "_2"
    else: replies[1]["id"] += "_2"
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw:
        (seen.append(1), Response(replies[len(seen) - 1]))[1])
    client = responses_client(configured)
    assert call(client, schema) == answer
    with pytest.raises(RequiredModelUnavailable):
        call(client, schema)
    assert len(seen) == 2 and len(client.receipts) == 1 and client.budget.snapshot()["actual_tokens"] == 100


@pytest.mark.parametrize("stage", ["tech.sentiment", "tech.risk", "sector.narrative", "research.candidates", "decision.debate"])
def test_other_five_stages_use_non_thinking_chat_requests(configured, monkeypatch, stage):
    configured._active.spec.name = stage
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw: (seen.append((url, kw["json"])), Response(chat_response()))[1])
    client = responses_client(configured)
    assert client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA) == {
        "score": 7, "reason": "evidence"}
    assert seen == [("https://api.deepseek.com/chat/completions", transport.build_chat_request(
        system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA))]
    assert configured.calls[0]["output"]["schema"] == "tiger.deepseek-call.v1"


@pytest.mark.parametrize("mutation", ["upgrade", "budget", "policy", "effort", "tokens", "temperature", "hint", "schema"])
def test_invalid_trusted_selection_or_request_fails_before_paid_io(configured, mutation):
    *_, schema, _ = keyed_contract()
    configured.upgrade = True
    configured.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    args = dict(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=schema, reasoning_effort="none")
    if mutation == "upgrade": configured.upgrade = False
    elif mutation == "budget": configured.policy.budget_policy_id = None
    elif mutation == "policy": configured.policy.clean_output_policy_id = "unregistered"
    elif mutation == "effort": args["reasoning_effort"] = "high"
    elif mutation == "tokens": args["max_tokens"] = 16385
    elif mutation == "temperature": args["temperature"] = .3
    elif mutation == "hint": args["schema_hint"] = {"new": "SYNTHETIC_TEST"}
    else: args["schema"] = SCHEMA
    with pytest.raises((RequiredModelUnavailable, RuntimeError)):
        client = transport.DeepSeekRequiredClient(configured)
        client.complete_json(**args)
    assert configured.calls == []


def responses_summary_client():
    from test_clean_strict_output_v2 import new_summary_client
    client = new_summary_client()
    client.execution.policy.clean_output_policy_id = POLICY
    for row in client.receipts:
        if row["stage"] != "clean.llm_semantic":
            continue
        row.pop("finish_reason")
        row.pop("output_tool_call_id")
        usage = row["usage"]
        row.update(output_policy_id=POLICY, response_status="completed", created_at=1793078400,
            output_message_id="SYNTHETIC_MESSAGE_1", native_usage={"input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"], "total_tokens": usage["total_tokens"]})
    return client


def test_six_stage_summary_accepts_truthful_native_completion_and_usage():
    result = required.model_summary_v2(responses_summary_client())
    assert result["all_required_completed"] is True and result["budget"]["actual_tokens"] == 36


@pytest.mark.parametrize("mutation", ["finish_reason", "system_fingerprint", "tool_id", "status", "policy",
    "native_usage", "created_at", "message_id", "other_stage_native"])
def test_summary_refuses_unbound_or_relabelled_native_completion(mutation):
    client = responses_summary_client()
    row = next(item for item in client.receipts if item["stage"] == "clean.llm_semantic")
    if mutation == "finish_reason": row["finish_reason"] = "stop"
    elif mutation == "system_fingerprint": row["system_fingerprint"] = None
    elif mutation == "tool_id": row["output_tool_call_id"] = "SYNTHETIC_TEST"
    elif mutation == "status": row["response_status"] = "incomplete"
    elif mutation == "policy": del row["output_policy_id"]
    elif mutation == "native_usage": row["native_usage"]["total_tokens"] += 1
    elif mutation == "created_at": row["created_at"] = True
    elif mutation == "message_id": row["output_message_id"] = ""
    else: next(item for item in client.receipts if item is not row)["response_status"] = "completed"
    from trading_system.redline.execution import RequiredModelFailed
    with pytest.raises(RequiredModelFailed):
        required.model_summary_v2(client)


@pytest.mark.parametrize("count", [0, 1, 8, 9, 56])
def test_real_clean_entry_uses_native_transport_for_every_complete_eight_source_batch(configured, monkeypatch, count):
    from test_clean_capacity_wire_v2 import documents, annotations
    originals = documents(count)
    configured.run_date = date(2026, 10, 27)
    configured.admitted_at_utc = "2026-10-27T14:00:00+00:00"
    client = responses_client(configured)
    configured.policy.market_timezone = "America/New_York"
    delivered = []

    def post(url, **options):
        assert url == "https://api.deepseek.com/responses"
        body = options["json"]
        assert body["instructions"] == required.SYSTEM_V2
        payload = json.loads(body["input"].split("\n", 1)[1])
        assert payload.pop("assessment")["run_date"] == "2026-10-27"
        complete = required.clean_keyed_payload_v2(payload, decode=True)
        ids = [row[0] for row in complete["all_source_index"]["rows"]]
        batch = [row["id"] for row in complete["documents"]]
        assert ids == [raw.doc_id for raw in originals]
        delivered.extend(complete["documents"])
        answer = required.clean_keyed_output_v2(annotations(complete), ids, batch_ids=batch)
        transport.validate_schema(answer, body["text"]["format"]["schema"])
        reply = native_response(answer)
        reply["id"] += f"_{len(delivered)}"
        reply["output"][0]["id"] += f"_{len(delivered)}"
        return Response(reply)

    monkeypatch.setattr(transport.requests, "post", post)
    result = required.clean_v2(client, originals)
    assert [doc.raw for doc in result] == originals
    assert len(configured.calls) == max(1, (count + 7) // 8)
    assert [row["id"] for row in delivered] == [raw.doc_id for raw in originals]
    for raw, presented, cleaned in zip(originals, delivered, result):
        expected = required.clean_payload_v2([raw])["documents"][0]
        assert presented["content_parts"] == expected["content_parts"]
        assert len(cleaned.summary_zh) == 800 and len(cleaned.provenance["origin_note"]) == 600
        assert len(cleaned.claims) == 4
        assert all(claim["quote"] in raw.content for claim in cleaned.claims)


def test_native_budget_rejects_hybrid_chat_fields_and_preserves_count_deadline_unknown_caps():
    from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, token_upper_bound
    body = responses_request()
    hybrid = {**body, "messages": [{"role": "user", "content": "SYNTHETIC_TEST"}], "max_tokens": 16384}
    with pytest.raises(BudgetRefused):
        token_upper_bound(hybrid, allow_clean_responses=True)
    elapsed = [0.]
    guard = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY, clock=lambda: elapsed[0])
    for index in range(40):
        identity = f"{index:032x}"
        guard.reserve("chat", identity, body)
        guard.settle(identity, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    with pytest.raises(BudgetRefused, match="REQUEST_COUNT_LIMIT"):
        guard.reserve("chat", "f" * 32, body)
    elapsed[0] = 2400.
    with pytest.raises(BudgetRefused, match="BUDGET_DEADLINE"):
        guard.reserve("search", "e" * 32, {})
    fresh = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    fresh.reserve("chat", "a" * 32, body)
    fresh.mark_unknown("a" * 32)
    with pytest.raises(BudgetRefused, match="UNKNOWN_USAGE_FREEZES_BUDGET"):
        fresh.reserve("chat", "b" * 32, body)
