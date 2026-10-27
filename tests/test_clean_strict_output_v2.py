"""Strict clean contract; every response and source is SYNTHETIC_TEST, offline."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from trading_system.llm import deepseek_required as transport
from trading_system.llm import required
from test_deepseek_required import configured, Response, chat_response
from test_clean_keyed_wire_v2 import contract
from test_clean_capacity_wire_v2 import annotations

POLICY = "tiger.clean-output.strict-function.v1"
FUNCTION = "emit_tiger_clean_v2"


def keyed_contract(count=2):
    payload, schema, ids, batch = contract(count)
    keyed = required.clean_keyed_schema_v2(schema, ids)
    answer = required.clean_keyed_output_v2(annotations(payload), ids, batch_ids=batch)
    return payload, schema, ids, batch, keyed, answer


def function_response(answer):
    response = chat_response()
    response["choices"] = [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "SYNTHETIC_TOOL_CALL",
            "type": "function", "function": {"name": FUNCTION, "arguments": json.dumps(answer)}}]}}]
    return response


def strict_client(configured):
    configured.upgrade = True
    configured.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    return transport.DeepSeekRequiredClient(configured)


def test_explicit_policy_reaches_single_forced_provider_function(configured, monkeypatch):
    *_, keyed, answer = keyed_contract()
    captured = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (captured.append((url, deepcopy(kw["json"]))), Response(function_response(answer)))[1])
    client = strict_client(configured)
    actual = client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={},
        schema=keyed, reasoning_effort="none")
    assert actual == answer
    assert len(captured) == 1
    url, request = captured[0]
    assert url == "https://api.deepseek.com/beta/chat/completions"
    assert request["tool_choice"] == {"type": "function", "function": {"name": FUNCTION}}
    assert request["thinking"] == {"type": "disabled"} and request["reasoning_effort"] == "none"
    assert len(request["tools"]) == 1 and request["tools"][0]["function"]["strict"] is True
    assert client.receipts[0]["finish_reason"] == "tool_calls"
    assert client.receipts[0]["output_policy_id"] == POLICY
    assert configured.calls[0]["inputs"]["schema"] == "tiger.deepseek-request.v2"
    assert configured.calls[0]["output"]["schema"] == "tiger.deepseek-call.v2"
    assert client.budget.snapshot()["actual_tokens"] == 50
    assert client.budget.snapshot()["reserved_tokens"] == 0


def test_provider_projection_keeps_exact_own_quote_enum_and_full_local_gate():
    from trading_system.llm.clean_output import strict_clean_schema
    *_, keyed, answer = keyed_contract()
    before = deepcopy(keyed)
    projected = strict_clean_schema(keyed)
    assert keyed == before
    assert projected["properties"]["d"]["properties"]["d0"]["properties"]["c"]["items"]["properties"]["q"] == {
        "type": "string", "enum": ["p1"]}
    assert projected["properties"]["d"]["properties"]["d1"]["properties"]["c"]["items"]["properties"]["q"] == {
        "type": "string", "enum": ["p0"]}
    transport.validate_schema(answer, projected)
    invalid = deepcopy(answer)
    invalid["d"]["d0"]["c"][0]["q"] = "p0"
    with pytest.raises(transport.LLMUnavailable):
        transport.validate_schema(invalid, projected)
    with pytest.raises(transport.LLMUnavailable):
        transport.validate_schema(invalid, keyed)
    # The official beta cannot enforce these bounds. They remain unchanged in
    # the local schema and are never described as a provider guarantee.
    oversized = deepcopy(answer)
    oversized["d"]["d0"]["z"] += "中"
    transport.validate_schema(oversized, projected)
    with pytest.raises(transport.LLMUnavailable):
        transport.validate_schema(oversized, keyed)
    assert not any(word in json.dumps(projected) for word in
        ("allOf", "oneOf", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems"))


@pytest.mark.parametrize("count", [0, 1, 8, 9, 56])
def test_schema_empty_and_full_batch_have_exact_source_coverage(count):
    from trading_system.llm.clean_output import strict_clean_schema
    *_, keyed, answer = keyed_contract(count)
    projected = strict_clean_schema(keyed)
    transport.validate_schema(answer, projected)
    assert set(projected["properties"]["d"]["properties"]) == set(answer["d"])
    assert projected["properties"]["d"]["additionalProperties"] is False
    for row in projected["properties"]["d"]["properties"].values():
        assert row["additionalProperties"] is False
        assert set(row["required"]) == set(row["properties"])


def test_no_quoteable_part_admits_only_the_original_empty_claim_array():
    from trading_system.llm.clean_output import strict_clean_schema
    from test_short_term_research_v2 import public_document
    from test_clean_capacity_wire_v2 import schema_for
    docs = [public_document("ds_000000000000000000000000", content="<" * 601)]
    payload = required.clean_payload_v2(docs)
    ids = [docs[0].doc_id]
    schema = schema_for(payload)
    keyed = required.clean_keyed_schema_v2(schema, ids)
    projected = strict_clean_schema(keyed)
    claim = projected["properties"]["d"]["properties"]["d0"]["properties"]["c"]
    assert claim == {"type": "array", "items": {"type": "string"}, "enum": [[]]}
    transport.validate_schema([], claim)
    with pytest.raises(transport.LLMUnavailable):
        transport.validate_schema(["SYNTHETIC_TEST"], claim)


@pytest.mark.parametrize("mutation", ["root_extra", "unregistered_common", "unregistered_source",
    "unregistered_quote", "empty_quote_enum", "foreign_quote_syntax", "open_object", "unsupported_keyword"])
def test_projection_rejects_unregistered_schema_changes_before_io(mutation):
    from trading_system.llm.clean_output import strict_clean_schema, CleanOutputInvalid
    *_, schema, _ = keyed_contract()
    shared, owned = schema["properties"]["d"]["allOf"]
    common = shared["additionalProperties"]
    if mutation == "root_extra": schema["allOf"] = [{"type": "object"}]
    elif mutation == "unregistered_common": common["properties"]["new"] = {"type": "string"}
    elif mutation == "unregistered_source": owned["properties"]["ds_raw"] = owned["properties"].pop("d0")
    elif mutation == "unregistered_quote": owned["properties"]["d0"]["type"] = "object"
    elif mutation == "empty_quote_enum": owned["properties"]["d0"]["properties"]["c"]["items"]["properties"]["q"]["enum"] = []
    elif mutation == "foreign_quote_syntax": owned["properties"]["d0"]["properties"]["c"]["items"]["properties"]["q"]["enum"] = ["p01"]
    elif mutation == "open_object": common["additionalProperties"] = True
    else: common["properties"]["z"]["format"] = "email"
    with pytest.raises(CleanOutputInvalid):
        strict_clean_schema(schema)


def strict_request():
    from trading_system.llm.clean_output import build_clean_request
    *_, schema, _ = keyed_contract()
    return build_clean_request(system="SYNTHETIC_TEST 中文😀", user="SYNTHETIC_TEST 中文😀",
        schema_hint={}, schema=schema)


def test_exact_full_utf8_request_budget_and_legacy_guard_behavior():
    from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, token_upper_bound
    from trading_system.llm.clean_output import clean_function_request_valid
    request = strict_request()
    assert clean_function_request_valid(request)
    serialized = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    bound = token_upper_bound(request, allow_clean_function=True)
    assert bound == (len(serialized) + 4096 + 128, 16384)
    assert len(serialized) > len(serialized.decode())  # UTF8 bytes, not character average.
    with pytest.raises(BudgetRefused):
        token_upper_bound(request)
    for policy in (None, "tiger.budget-policy.v3"):
        with pytest.raises(BudgetRefused):
            BudgetGuard.for_policy(policy).reserve("chat", "a" * 32, request)
    new = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    reservation = new.reserve("chat", "a" * 32, request)
    assert reservation["token_upper_bound"] == sum(bound)
    assert new.limits == {"max_chat_calls": 40, "max_tokens": 600000, "max_search_calls": 60,
        "max_fetch_calls": 20, "max_remote_requests": 120, "max_cost_usd": 1., "deadline_seconds": 2400}
    assert "clean_output_policy_id" not in new.snapshot()
    for budget in (None, "tiger.budget-policy.v2"):
        with pytest.raises(BudgetRefused):
            BudgetGuard.for_policy(budget, clean_output_policy_id=POLICY)


@pytest.mark.parametrize("mutation", ["tool_name", "tool_description", "arbitrary_tools", "not_strict", "auto_choice",
    "tool_choice_name", "thinking", "reasoning", "tool_result", "stream", "response_format", "schema_open",
    "schema_arbitrary", "max_tokens", "model", "stream_options", "extra_field"])
def test_arbitrary_tool_requests_never_enter_bounded_output_budget(mutation):
    from trading_system.llm.request_budget import BudgetRefused, token_upper_bound
    from trading_system.llm.clean_output import clean_function_request_valid
    request = strict_request()
    if mutation == "tool_name": request["tools"][0]["function"]["name"] = "run_shell"
    elif mutation == "tool_description": request["tools"][0]["function"]["description"] = "arbitrary"
    elif mutation == "arbitrary_tools": request["tools"].append(deepcopy(request["tools"][0]))
    elif mutation == "not_strict": request["tools"][0]["function"]["strict"] = False
    elif mutation == "auto_choice": request["tool_choice"] = "auto"
    elif mutation == "tool_choice_name": request["tool_choice"]["function"]["name"] = "run_shell"
    elif mutation == "thinking": request["thinking"] = {"type": "enabled"}
    elif mutation == "reasoning": request["reasoning_effort"] = "low"
    elif mutation == "tool_result": request["messages"].append({"role": "tool", "content": "loop"})
    elif mutation == "stream": request["stream"] = True
    elif mutation == "response_format": request["response_format"] = {"type": "json_object"}
    elif mutation == "schema_open": request["tools"][0]["function"]["parameters"]["additionalProperties"] = True
    elif mutation == "schema_arbitrary": request["tools"][0]["function"]["parameters"] = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    elif mutation == "max_tokens": request["max_tokens"] = 393217
    elif mutation == "model": request["model"] = "other"
    elif mutation == "stream_options": request["stream_options"] = {}
    else: request["unregistered"] = True
    assert clean_function_request_valid(request) is False
    with pytest.raises(BudgetRefused):
        token_upper_bound(request, allow_clean_function=True)


@pytest.mark.parametrize("mutation,finish", [
    ("wrong_name", "tool_calls"), ("multiple_tools", "tool_calls"), ("missing_args", "tool_calls"),
    ("null_args", "tool_calls"), ("malformed_args", "tool_calls"), ("duplicate_json", "tool_calls"),
    ("length", "length"), ("stop", "stop"), ("prose", "tool_calls"),
    ("legacy_function", "tool_calls"), ("hidden_reasoning", "tool_calls"), ("secret", "tool_calls"),
    ("unsafe_tool_id", "tool_calls"), ("wrong_type", "tool_calls"), ("missing_source", "tool_calls"),
    ("own_quote", "tool_calls"), ("over_length", "tool_calls"), ("too_many_claims", "tool_calls"),
    ("nonobject", "tool_calls"), ("wrong_model", "tool_calls"), ("missing_claim_key", "tool_calls"),
])
def test_invalid_output_stops_without_repair_retry_or_lost_paid_usage(configured, monkeypatch, mutation, finish):
    *_, keyed, answer = keyed_contract()
    response = function_response(answer)
    choice = response["choices"][0]
    message = choice["message"]
    tool = message["tool_calls"][0]
    if mutation == "wrong_name": tool["function"]["name"] = "run_shell"
    elif mutation == "multiple_tools": message["tool_calls"].append(deepcopy(tool))
    elif mutation == "missing_args": del tool["function"]["arguments"]
    elif mutation == "null_args": tool["function"]["arguments"] = None
    elif mutation == "malformed_args": tool["function"]["arguments"] = "{SYNTHETIC_REJECTED_TEXT"
    elif mutation == "duplicate_json": tool["function"]["arguments"] = '{"d":{},"d":{}}'
    elif mutation in {"length", "stop"}: choice["finish_reason"] = mutation
    elif mutation == "prose": message["content"] = "SYNTHETIC_REJECTED_TEXT"
    elif mutation == "legacy_function": message["function_call"] = {"name": FUNCTION}
    elif mutation == "hidden_reasoning": message["reasoning_content"] = "SYNTHETIC_REJECTED_TEXT"
    elif mutation == "unsafe_tool_id": tool["id"] = "secret unsafe id"
    elif mutation == "wrong_type": tool["type"] = "computer"
    elif mutation == "wrong_model": response["model"] = "other"
    elif mutation == "nonobject": tool["function"]["arguments"] = "[]"
    else:
        row = answer["d"]["d0"]
        if mutation == "secret": row["z"] = "fixture-deepseek-credential"
        elif mutation == "missing_source": del answer["d"]["d1"]
        elif mutation == "own_quote": row["c"][0]["q"] = "p0"
        elif mutation == "over_length": row["z"] += "x"
        elif mutation == "too_many_claims": row["c"].append({**row["c"][0], "k": "fifth"})
        else: del row["c"][0]["k"]
        tool["function"]["arguments"] = json.dumps(answer)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw:
        (seen.append(kw["json"]), Response(response))[1])
    client = strict_client(configured)
    from trading_system.redline.model_diagnostics import RequiredModelUnavailable
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={},
            schema=keyed, reasoning_effort="none")
    assert len(seen) == 1 and client.receipts == []
    budget = client.budget.snapshot()
    assert budget["actual_tokens"] == 50 and budget["reserved_tokens"] == 0 and budget["frozen"] is False
    accounting = caught.value.accounting
    assert accounting["status"] == "settled" and accounting["finish_reason"] == finish
    assert accounting["actual_tokens"] == 50 and accounting["reserved_tokens"] == 0
    assert "SYNTHETIC_REJECTED_TEXT" not in json.dumps(accounting)
    assert "fixture-deepseek-credential" not in json.dumps(configured.calls)


def test_usage_overrun_freezes_reservation_even_with_valid_function(configured, monkeypatch):
    *_, keyed, answer = keyed_contract()
    response = function_response(answer)
    response["usage"] = {"prompt_tokens": 10, "completion_tokens": 16385, "total_tokens": 16395}
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: Response(response))
    client = strict_client(configured)
    from trading_system.redline.model_diagnostics import RequiredModelUnavailable
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={},
            schema=keyed, reasoning_effort="none")
    accounting = caught.value.accounting
    assert accounting["status"] == "unknown" and accounting["frozen"] is True
    assert accounting["actual_tokens"] == 0 and accounting["reserved_tokens"] > 16384
    assert client.receipts == []


def test_tool_call_identity_is_bound_and_cannot_be_reused(configured, monkeypatch):
    *_, keyed, answer = keyed_contract()
    responses = [function_response(answer), function_response(answer)]
    responses[1]["id"] += "-2"
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw:
        (seen.append(kw["json"]), Response(responses[len(seen) - 1]))[1])
    client = strict_client(configured)
    args = dict(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=keyed, reasoning_effort="none")
    assert client.complete_json(**args) == answer
    proof = configured.calls[0]["output"]
    assert proof["output_tool_call_id"] == proof["response"]["choices"][0]["message"]["tool_calls"][0]["id"]
    with pytest.raises(transport.LLMUnavailable):
        client.complete_json(**args)
    assert len(seen) == 2 and len(client.receipts) == 1
    assert client.budget.snapshot()["actual_tokens"] == 100


def test_invalid_schema_fails_before_paid_io(configured, monkeypatch):
    *_, keyed, _ = keyed_contract()
    keyed["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"]["z"]["oneOf"] = [{"type": "string"}]
    client = strict_client(configured)
    with pytest.raises(transport.LLMUnavailable):
        client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=keyed, reasoning_effort="none")
    assert configured.calls == [] and client.budget.snapshot()["counts"]["chat"] == 0


@pytest.mark.parametrize("policy", [None, "absent"])
def test_old_absent_policy_keeps_legacy_json_request_exact(configured, monkeypatch, policy):
    from test_deepseek_required import SCHEMA
    configured.upgrade = True
    configured.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3")
    if policy is None:
        configured.policy.clean_output_policy_id = None
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (seen.append((url, kw["json"])), Response(chat_response()))[1])
    client = transport.DeepSeekRequiredClient(configured)
    result = client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA, reasoning_effort="low")
    assert result == {"score": 7, "reason": "evidence"}
    expected = transport.build_chat_request(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA,
        reasoning_effort="low", thinking_mode="disabled")
    assert seen == [("https://api.deepseek.com/chat/completions", expected)]
    assert configured.calls[0]["inputs"]["schema"] == "tiger.deepseek-request.v1"
    assert "output_policy_id" not in client.receipts[0] and client.receipts[0]["finish_reason"] == "stop"


def test_explicit_policy_does_not_change_other_stage_requests(configured, monkeypatch):
    from test_deepseek_required import SCHEMA
    configured._active.spec.name = "tech.risk"
    client = strict_client(configured)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (seen.append((url, kw["json"])), Response(chat_response()))[1])
    assert client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA) == {
        "score": 7, "reason": "evidence"}
    assert seen[0][0] == "https://api.deepseek.com/chat/completions"
    assert seen[0][1]["thinking"] == {"type": "enabled"} and seen[0][1]["reasoning_effort"] == "high"
    assert "tools" not in seen[0][1]


@pytest.mark.parametrize("field,value", [("clean_output_policy_id", "unregistered"), ("upgrade", False)])
def test_untrusted_policy_cannot_enable_function_output(configured, field, value):
    configured.upgrade = True
    configured.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    if field == "upgrade": configured.upgrade = value
    else: configured.policy.clean_output_policy_id = value
    from test_deepseek_required import SCHEMA
    with pytest.raises(transport.LLMUnavailable):
        client = transport.DeepSeekRequiredClient(configured)
        client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA)
    assert configured.calls == []


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_direct_new_clean_dispatch_keeps_all_semantics_and_original_bodies(count):
    from test_short_term_research_v2 import SemanticStub
    from test_clean_capacity_wire_v2 import documents
    originals = documents(count)
    execution = SemanticStub({}).execution
    execution.upgrade = True
    execution.policy.clean_output_policy_id = POLICY
    execution.policy.budget_policy_id = "tiger.budget-policy.v3"

    class Client:
        def __init__(self):
            self.execution = execution
            self.requests = []

        def complete_json(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            assert kwargs["reasoning_effort"] == "none"
            payload = json.loads(kwargs["user"].split("\n", 1)[1])
            payload.pop("assessment")
            original = required.clean_keyed_payload_v2(payload, decode=True)
            ids = [row[0] for row in original["all_source_index"]["rows"]]
            batch = [row["id"] for row in original["documents"]]
            answer = required.clean_keyed_output_v2(annotations(original), ids, batch_ids=batch)
            transport.validate_schema(answer, kwargs["schema"])
            return answer

    client = Client()
    actual = required.clean_v2(client, originals)
    assert [doc.raw for doc in actual] == originals
    assert len(client.requests) == max(1, (count + 7) // 8)
    assert all(len(doc.summary_zh) == 800 and len(doc.provenance["origin_note"]) == 600
        and len(doc.claims) == 4 for doc in actual)
    assert all(claim["quote"] in doc.raw.content for doc in actual for claim in doc.claims)


def new_summary_client():
    from test_short_term_research_v2 import summary_client
    from trading_system.llm.request_budget import BudgetGuard
    client = summary_client()
    old_budget = client.budget.snapshot()
    client.budget = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    client.execution = SimpleNamespace(upgrade=True, policy=SimpleNamespace(
        budget_policy_id="tiger.budget-policy.v3", clean_output_policy_id=POLICY))
    for receipt in client.receipts:
        request = {"messages": [{"role": "user", "content": "SYNTHETIC_TEST"}], "max_tokens": 16}
        reservation = client.budget.reserve("chat", receipt["call_id"], request)
        assert reservation["request_sha256"] == receipt["request_sha256"]
        client.budget.settle(receipt["call_id"], receipt["usage"])
        if receipt["stage"] == "clean.llm_semantic":
            receipt.update(finish_reason="tool_calls", output_policy_id=POLICY, output_tool_call_id="SYNTHETIC_TOOL")
    for number, receipt in enumerate(client.search_receipts):
        client.budget.reserve("search", receipt["call_id"], {"query": f"SYNTHETIC_TEST {number}"})
        client.budget.settle(receipt["call_id"], None)
    return client


def test_summary_accepts_truthful_tool_finish_only_for_trusted_new_clean_policy():
    client = new_summary_client()
    summary = required.model_summary_v2(client)
    assert summary["all_required_completed"] is True and summary["budget"]["actual_tokens"] == 36
    assert summary["stage_counts"]["clean.llm_semantic"] == 1
    assert "output_policy_id" not in summary and "output_tool_call_id" not in summary


@pytest.mark.parametrize("mutation", ["old_clean_stop", "other_stage_tool", "missing_policy", "wrong_policy", "missing_tool_id", "old_policy_new_receipt"])
def test_summary_rejects_unbound_or_relabelled_tool_completion(mutation):
    client = new_summary_client()
    clean = next(row for row in client.receipts if row["stage"] == "clean.llm_semantic")
    if mutation == "old_clean_stop": clean["finish_reason"] = "stop"
    elif mutation == "other_stage_tool": next(row for row in client.receipts if row is not clean)["finish_reason"] = "tool_calls"
    elif mutation == "missing_policy": del clean["output_policy_id"]
    elif mutation == "wrong_policy": clean["output_policy_id"] = "unregistered"
    elif mutation == "missing_tool_id": del clean["output_tool_call_id"]
    else: client.execution.policy.clean_output_policy_id = None
    from trading_system.redline.execution import RequiredModelFailed
    with pytest.raises(RequiredModelFailed):
        required.model_summary_v2(client)


def test_nullable_origin_is_a_supported_enum_alternative_without_defaulting():
    from trading_system.llm.clean_output import strict_clean_schema
    *_, keyed, answer = keyed_contract()
    answer["d"]["d0"]["p"]["o"] = None
    projected = strict_clean_schema(keyed)
    origin = projected["properties"]["d"]["properties"]["d0"]["properties"]["p"]["properties"]["o"]
    assert {"enum": [None]} in origin["anyOf"]
    transport.validate_schema(answer, projected)
    transport.validate_schema(answer, keyed)


def test_strict_budget_retains_unknown_freeze_and_count_deadline_caps():
    from trading_system.llm.request_budget import BudgetGuard, BudgetRefused
    body = strict_request()
    elapsed = [0.]
    guard = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY, clock=lambda:elapsed[0])
    for index in range(40):
        identity = f"{index:032x}"
        guard.reserve("chat", identity, body)
        guard.settle(identity, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    with pytest.raises(BudgetRefused, match="REQUEST_COUNT_LIMIT"):
        guard.reserve("chat", "f" * 32, body)
    elapsed[0] = 2400.
    with pytest.raises(BudgetRefused, match="BUDGET_DEADLINE"):
        guard.reserve("search", "e" * 32, {})
    fresh = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY, clock=lambda:0.)
    fresh.reserve("chat", "a" * 32, body)
    fresh.mark_unknown("a" * 32)
    with pytest.raises(BudgetRefused, match="UNKNOWN_USAGE_FREEZES_BUDGET"):
        fresh.reserve("chat", "b" * 32, body)


def test_summary_rejects_duplicate_strict_tool_call_identity():
    client = new_summary_client()
    first = next(row for row in client.receipts if row["stage"] == "clean.llm_semantic")
    duplicate = deepcopy(first)
    identity = "e" * 32
    reservation = client.budget.reserve("chat", identity,
        {"messages": [{"role": "user", "content": "SYNTHETIC_TEST"}], "max_tokens": 16})
    client.budget.settle(identity, first["usage"])
    duplicate.update(call_id=identity, request_sha256=reservation["request_sha256"], response_id="SYNTHETIC_NEW_RESPONSE")
    client.receipts.append(duplicate)
    from trading_system.redline.execution import RequiredModelFailed
    with pytest.raises(RequiredModelFailed):
        required.model_summary_v2(client)
