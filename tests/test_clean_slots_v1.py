"""Fixed clean slot contracts. All sources, answers and HTTP are SYNTHETIC_TEST."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pytest

from trading_system.llm import clean_output, deepseek_required as transport, required
from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, token_upper_bound
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_deepseek_required import configured, Response, SCHEMA, chat_response, KEY
from test_clean_strict_output_v2 import keyed_contract
from test_clean_responses_output_v1 import native_response
from test_clean_capacity_wire_v2 import annotations, documents, schema_for

POLICY = "tiger.clean-output.responses-slots.v1"
CODEC = "tiger.clean-slots-wire.v1"
ARRAY_FIELDS = ("t", "e", "h", "f", "c")


def test_new_selector_and_codec_are_explicit_versions():
    assert clean_output.CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1 == POLICY
    assert required.CLEAN_SLOTS_WIRE_V1 == CODEC


def slot_request():
    *_, schema, _ = keyed_contract()
    return clean_output.build_clean_slots_responses_request(system="SYNTHETIC_TEST 中文😀",
        user="SYNTHETIC_TEST 完整来源😀", schema_hint={}, schema=schema)


def slot_client(execution):
    execution.upgrade = True
    execution.policy = SimpleNamespace(budget_policy_id="tiger.budget-policy.v3",
        clean_output_policy_id=POLICY, market_timezone="America/New_York")
    return transport.DeepSeekRequiredClient(execution)


def call(client, schema):
    return client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST 完整正文😀",
        schema_hint={}, schema=schema, reasoning_effort="none")


@pytest.mark.parametrize("count", [0, 1, 8, 9, 56])
@pytest.mark.parametrize("filler", ["x", "中", "😀"])
def test_payload_and_all_semantic_values_roundtrip_exactly(count, filler):
    payload, semantic, ids, batch, keyed, _ = keyed_contract(count)
    original = annotations(payload, filler=filler)
    answer = required.clean_keyed_output_v2(original, ids, batch_ids=batch)
    before = deepcopy((payload, keyed, answer))
    wire = required.clean_slots_payload_v1(payload, keyed)
    assert wire["clean_wire"]["schema"] == CODEC
    assert "format_example" not in wire["clean_wire"]
    assert "format_example_scope" not in wire["clean_wire"]
    assert required.clean_slots_payload_v1(wire, keyed, decode=True) == payload
    legacy = required.clean_keyed_payload_v2(payload)
    assert wire["documents"] == legacy["documents"]
    assert wire["all_source_index"] == legacy["all_source_index"]
    assert wire["clean_wire"]["source_bindings"] == legacy["clean_wire"]["source_bindings"]
    encoded = required.clean_slots_output_v1(answer, keyed)
    compiled = clean_output.clean_slots_schema_v1(keyed)
    transport.validate_schema(encoded, compiled)
    restored = required.clean_slots_output_v1(encoded, keyed, decode=True)
    assert restored == answer
    assert required.clean_keyed_output_v2(restored, ids, batch_ids=batch, decode=True) == original
    transport.validate_schema(original, semantic)
    assert (payload, keyed, answer) == before
    assert "allOf" not in json.dumps(compiled)
    assert compiled["properties"]["d"]["additionalProperties"] is False
    for ref, row in answer["d"].items():
        for field in ARRAY_FIELDS:
            maximum = keyed["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"][field]["maxItems"]
            assert list(encoded["d"][ref][field]) == [f"s{index}" for index in range(maximum)]
            assert list(encoded["d"][ref][field].values()) == row[field] + [None] * (maximum - len(row[field]))
            assert wire["clean_wire"]["output_slots"]["fields"][field]["max_items"] == maximum


@pytest.mark.parametrize("field", ARRAY_FIELDS)
@pytest.mark.parametrize("size", ["empty", "max"])
def test_every_bounded_array_empty_or_max_preserves_values_order(field, size):
    *_, keyed, answer = keyed_contract()
    row = answer["d"]["d0"]
    common = keyed["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"]
    maximum = common[field]["maxItems"]
    if size == "empty":
        row[field] = []
    elif field == "t":
        row[field] = [f"TAG{index}" for index in range(maximum)]
    elif field in ("e", "h", "f"):
        row[field] = list(reversed(common[field]["items"]["enum"][:maximum]))
    else:
        row[field] = [{**row[field][0], "k": f"fact{index}", "s": f"SYNTHETIC_TEST 事实{index}😀"}
            for index in range(maximum)]
    transport.validate_schema(answer, keyed)
    encoded = required.clean_slots_output_v1(answer, keyed)
    assert required.clean_slots_output_v1(encoded, keyed, decode=True) == answer


@pytest.mark.parametrize("field", ARRAY_FIELDS)
def test_over_limit_encoding_refuses_instead_of_truncating(field):
    *_, keyed, answer = keyed_contract()
    row = answer["d"]["d0"]
    maximum = keyed["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"][field]["maxItems"]
    row[field] = [deepcopy(row[field][0]) for _ in range(maximum + 1)]
    original = deepcopy(answer)
    with pytest.raises((LLMUnavailable, RequiredModelFailed)):
        required.clean_slots_output_v1(answer, keyed)
    assert answer == original


@pytest.mark.parametrize("mutation", ["missing_slot", "unknown_slot", "hole", "wrong_container", "duplicate",
    "wrong_item", "missing_source", "unknown_source", "missing_semantic", "unknown_semantic", "own_quote",
    "bad_origin", "summary_bound", "claim_bound", "invalid_number", "bool_number", "nonfinite", "text_format"])
def test_invalid_slots_or_original_semantics_reject_without_repair(mutation):
    *_, keyed, answer = keyed_contract()
    encoded = required.clean_slots_output_v1(answer, keyed)
    row = encoded["d"]["d0"]
    if mutation == "missing_slot": del row["t"]["s7"]
    elif mutation == "unknown_slot": row["t"]["s8"] = None
    elif mutation == "hole": row["t"]["s1"] = row["t"]["s0"]; row["t"]["s0"] = None
    elif mutation == "wrong_container": row["t"] = ["AAA"]
    elif mutation == "duplicate": row["t"]["s1"] = row["t"]["s0"]
    elif mutation == "wrong_item": row["t"]["s0"] = 12
    elif mutation == "missing_source": del encoded["d"]["d1"]
    elif mutation == "unknown_source": encoded["d"]["d2"] = deepcopy(row)
    elif mutation == "missing_semantic": del row["z"]
    elif mutation == "unknown_semantic": row["unregistered"] = "SYNTHETIC_TEST"
    elif mutation == "own_quote": row["c"]["s0"]["q"] = "p0"
    elif mutation == "bad_origin": row["p"]["o"] = "d999"
    elif mutation == "summary_bound": row["z"] += "中"
    elif mutation == "claim_bound": row["c"]["s0"]["s"] += "中"
    elif mutation == "invalid_number": row["r"] = 1.1
    elif mutation == "bool_number": row["v"] = True
    elif mutation == "nonfinite": row["v"] = float("nan")
    else: row["z"] += "\n"
    with pytest.raises((LLMUnavailable, RequiredModelFailed)):
        required.clean_slots_output_v1(encoded, keyed, decode=True)


@pytest.mark.parametrize("mutation", ["binding", "slots", "codec", "unknown", "body", "source_schema"])
def test_payload_inverse_rejects_metadata_and_source_contract_tampering(mutation):
    payload, _, _, _, keyed, _ = keyed_contract()
    wire = required.clean_slots_payload_v1(payload, keyed)
    if mutation == "binding": wire["clean_wire"]["source_bindings"]["rows"][0][1].append("p0")
    elif mutation == "slots": wire["clean_wire"]["output_slots"]["fields"]["t"]["slots"].pop()
    elif mutation == "codec": wire["clean_wire"]["schema"] = "tiger.clean-slots-wire.unknown"
    elif mutation == "unknown": wire["clean_wire"]["extra"] = "SYNTHETIC_TEST"
    elif mutation == "body": wire["documents"]["rows"][0].pop()
    else: keyed["properties"]["d"]["allOf"][1]["required"].pop()
    with pytest.raises((LLMUnavailable, RequiredModelFailed, clean_output.CleanOutputInvalid)):
        required.clean_slots_payload_v1(wire, keyed, decode=True)


def test_unquoteable_source_has_four_required_null_claim_slots_only():
    from test_short_term_research_v2 import public_document
    docs = [public_document("ds_000000000000000000000000", content="<" * 601)]
    payload = required.clean_payload_v2(docs)
    keyed = required.clean_keyed_schema_v2(schema_for(payload), [docs[0].doc_id])
    compiled = clean_output.clean_slots_schema_v1(keyed)
    claims = compiled["properties"]["d"]["properties"]["d0"]["properties"]["c"]
    transport.validate_schema({f"s{i}": None for i in range(4)}, claims)
    with pytest.raises(LLMUnavailable):
        transport.validate_schema({"s0": {"s": "SYNTHETIC_TEST", "q": "p0", "k": "fact", "t": "unknown"},
            "s1": None, "s2": None, "s3": None}, claims)


def test_full_utf8_body_is_prepaid_under_only_the_new_selector_and_unchanged_v3_caps():
    body = slot_request()
    assert clean_output.clean_slots_responses_request_valid(body)
    assert not clean_output.clean_responses_request_valid(body)
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    bound = token_upper_bound(body, allow_clean_slots=True)
    assert bound == (len(serialized) + 4096 + 128, 16384)
    assert len(serialized) > len(serialized.decode())
    for flags in ({}, {"allow_clean_function": True}, {"allow_clean_responses": True}):
        with pytest.raises(BudgetRefused): token_upper_bound(body, **flags)
    guard = BudgetGuard.for_policy("tiger.budget-policy.v3", clean_output_policy_id=POLICY)
    reservation = guard.reserve("chat", "a" * 32, body)
    assert reservation["request_sha256"] == hashlib.sha256(serialized).hexdigest()
    assert reservation["token_upper_bound"] == sum(bound)
    assert guard.limits == {"max_chat_calls": 40, "max_tokens": 600000, "max_search_calls": 60,
        "max_fetch_calls": 20, "max_remote_requests": 120, "max_cost_usd": 1., "deadline_seconds": 2400}
    for other in (None, "tiger.budget-policy.v2"):
        with pytest.raises(BudgetRefused): BudgetGuard.for_policy(other, clean_output_policy_id=POLICY)


@pytest.mark.parametrize("mutation", ["name", "tools", "continuation", "thinking", "tokens", "temperature",
    "open_root", "open_source", "unknown_slot", "missing_slot", "slot_array", "nonnullable", "schema_keyword"])
def test_budget_closed_slot_request_allowlist_fails_before_io(mutation):
    body = slot_request()
    output = body["text"]["format"]
    schema = output["schema"]
    row = schema["properties"]["d"]["properties"]["d0"]
    if mutation == "name": output["name"] = "other"
    elif mutation == "tools": body["tools"] = []
    elif mutation == "continuation": body["previous_response_id"] = "SYNTHETIC_TEST"
    elif mutation == "thinking": body["reasoning"]["effort"] = "high"
    elif mutation == "tokens": body["max_output_tokens"] += 1
    elif mutation == "temperature": body["temperature"] = .3
    elif mutation == "open_root": schema["additionalProperties"] = True
    elif mutation == "open_source": row["additionalProperties"] = True
    elif mutation == "unknown_slot": row["properties"]["t"]["properties"]["s8"] = {"type": "null"}
    elif mutation == "missing_slot": row["properties"]["t"]["required"].pop()
    elif mutation == "slot_array": row["properties"]["t"] = {"type": "array", "items": {"type": "string"}}
    elif mutation == "nonnullable": row["properties"]["t"]["properties"]["s0"] = {"type": "string"}
    else: row["properties"]["z"]["$ref"] = "https://example.com/schema"
    assert not clean_output.clean_slots_responses_request_valid(body)
    with pytest.raises(BudgetRefused): token_upper_bound(body, allow_clean_slots=True)


def test_native_slot_wire_and_original_schema_hash_are_truthful_in_v3_proof(configured, monkeypatch):
    *_, keyed, original = keyed_contract()
    encoded = required.clean_slots_output_v1(original, keyed)
    reply = native_response(encoded)
    reply["output"][0]["phase"] = "final_answer"
    reply["output"][0]["content"][0]["logprobs"] = []
    response = Response(reply)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (seen.append((url, json.loads(kw["data"]))), response)[1])
    client = slot_client(configured)
    assert call(client, keyed) == encoded
    assert seen == [("https://api.deepseek.com/responses", clean_output.build_clean_slots_responses_request(
        system="SYNTHETIC_TEST", user="SYNTHETIC_TEST 完整正文😀", schema_hint={}, schema=keyed))]
    record = configured.calls[0]
    assert record["inputs"]["output_schema"] == keyed
    assert record["inputs"]["schema_sha256"] == transport._hash(keyed)
    proof = record["output"]
    assert record["inputs"]["schema"] == "tiger.deepseek-request.v3"
    assert proof["schema"] == "tiger.deepseek-call.v3"
    assert proof["output_policy_id"] == record["inputs"]["output_policy_id"] == POLICY
    assert proof["parsed"] == encoded
    assert json.loads(proof["response"]["output"][0]["content"][0]["text"]) == encoded
    assert proof["transport_response_sha256"] == hashlib.sha256(response.body).hexdigest()
    assert proof["response_sha256"] == transport._hash(proof["response"])
    assert client.budget.snapshot()["actual_tokens"] == 50
    assert client.budget.snapshot()["reserved_tokens"] == 0
    assert len(seen) == 1 and response.closed
    assert KEY not in json.dumps(configured.calls)


@pytest.mark.parametrize("mutation", ["hole", "duplicate", "own_quote", "missing_slot", "phase", "logprobs", "duplicate_json"])
def test_rejected_native_slots_keep_real_charge_policy_and_never_retry(configured, monkeypatch, mutation):
    *_, keyed, original = keyed_contract()
    encoded = required.clean_slots_output_v1(original, keyed)
    row = encoded["d"]["d0"]
    reply = native_response(encoded)
    if mutation == "hole": row["t"]["s1"] = "BBB"; row["t"]["s0"] = None
    elif mutation == "duplicate": row["t"]["s1"] = row["t"]["s0"]
    elif mutation == "own_quote": row["c"]["s0"]["q"] = "p0"
    elif mutation == "missing_slot": del row["t"]["s7"]
    elif mutation == "phase": reply["output"][0]["phase"] = "commentary"
    elif mutation == "logprobs": reply["output"][0]["content"][0]["logprobs"] = [{}]
    if mutation not in ("phase", "logprobs"):
        reply["output"][0]["content"][0]["text"] = ('{"d":{},"d":{}}' if mutation == "duplicate_json" else json.dumps(encoded))
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), Response(reply))[1])
    client = slot_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught: call(client, keyed)
    assert seen == [1] and client.receipts == []
    budget = client.budget.snapshot()
    assert budget["actual_tokens"] == 50 and budget["reserved_tokens"] == 0 and not budget["frozen"]
    assert caught.value.accounting["output_policy_id"] == POLICY
    assert caught.value.accounting["schema"] == "tiger.model-call-accounting.v4"


@pytest.mark.parametrize("count", [0, 1, 8, 9, 56])
def test_real_clean_entry_consumes_every_complete_source_and_validates_decoded_typed_result(configured, monkeypatch, count):
    originals = documents(count)
    configured.run_date = date(2026, 10, 27)
    configured.admitted_at_utc = "2026-10-27T14:00:00+00:00"
    client = slot_client(configured)
    delivered = []

    def post(url, **options):
        assert url == "https://api.deepseek.com/responses"
        assert "json" not in options and type(options["data"]) is bytes
        body = json.loads(options["data"])
        payload = json.loads(body["input"].split("\n", 1)[1])
        payload.pop("assessment")
        # Production inputs retain their ORIGINAL keyed semantic schema.
        keyed = configured.calls[-1]["inputs"]["output_schema"]
        complete = required.clean_slots_payload_v1(payload, keyed, decode=True)
        ids = [row[0] for row in complete["all_source_index"]["rows"]]
        batch = [row["id"] for row in complete["documents"]]
        assert ids == [raw.doc_id for raw in originals]
        delivered.extend(complete["documents"])
        answer = required.clean_keyed_output_v2(annotations(complete), ids, batch_ids=batch)
        # schema_for uses reduced fixture enums; production uses full enums.
        encoded = required.clean_slots_output_v1(answer, keyed)
        transport.validate_schema(encoded, body["text"]["format"]["schema"])
        reply = native_response(encoded)
        reply["id"] += f"_{len(delivered)}"
        reply["output"][0]["id"] += f"_{len(delivered)}"
        return Response(reply)

    monkeypatch.setattr(transport.requests, "post", post)
    actual = required.clean_v2(client, originals)
    assert [doc.raw for doc in actual] == originals
    assert [row["id"] for row in delivered] == [raw.doc_id for raw in originals]
    assert len(configured.calls) == max(1, (count + 7) // 8)
    for doc, shown in zip(actual, delivered):
        assert shown["content_parts"] == required.clean_payload_v2([doc.raw])["documents"][0]["content_parts"]
        assert len(doc.summary_zh) == 800 and len(doc.provenance["origin_note"]) == 600
        assert len(doc.claims) == 4 and all(claim["quote"] in doc.raw.content for claim in doc.claims)


@pytest.mark.parametrize("stage", ["tech.sentiment", "tech.risk", "sector.narrative", "research.candidates", "decision.debate"])
def test_other_five_stages_use_exact_non_thinking_chat_contract(configured, monkeypatch, stage):
    configured._active.spec.name = stage
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda url, **kw:
        (seen.append((url, kw["json"])), Response(chat_response()))[1])
    client = slot_client(configured)
    assert client.complete_json(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA) == {"score": 7, "reason": "evidence"}
    assert seen == [("https://api.deepseek.com/chat/completions", transport.build_chat_request(
        system="SYNTHETIC_TEST", user="SYNTHETIC_TEST", schema_hint={}, schema=SCHEMA))]


def full_contract():
    from trading_system import config
    from trading_system.llm.required_schemas import clean_schema_v2
    from trading_system.tech_chain.universe import TECH_SUBCHAINS
    payload, _, ids, batch, _, _ = keyed_contract()
    semantic = clean_schema_v2(batch, list(TECH_SUBCHAINS), config.SECTOR_ETFS,
        quote_part_ids={row["id"]: [part[0] for part in row["content_parts"]["rows"] if part[2]]
            for row in payload["documents"]}, all_doc_ids=ids)
    schema = required.clean_keyed_schema_v2(semantic, ids)
    answer = required.clean_keyed_output_v2(annotations(payload), ids, batch_ids=batch)
    return payload, schema, answer


@pytest.mark.parametrize("field", ARRAY_FIELDS)
@pytest.mark.parametrize("mutation", ["hole", "missing", "unknown", "duplicate"])
def test_each_original_array_rejects_noncanonical_or_duplicate_slot_values(field, mutation):
    _, schema, answer = full_contract()
    encoded = required.clean_slots_output_v1(answer, schema)
    slots = encoded["d"]["d0"][field]
    assert len(slots) >= 2
    if mutation == "hole": slots["s1"] = slots["s0"]; slots["s0"] = None
    elif mutation == "missing": slots.pop(f"s{len(slots) - 1}")
    elif mutation == "unknown": slots[f"s{len(slots)}"] = None
    else: slots["s1"] = deepcopy(slots["s0"])
    with pytest.raises(LLMUnavailable): required.clean_slots_output_v1(encoded, schema, decode=True)


def test_all_arrays_simultaneously_reach_original_maximum_and_survive_canonical_json():
    payload, schema, answer = full_contract()
    common = schema["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"]
    for row in answer["d"].values():
        row["t"] = [f"TAG{i}" for i in range(common["t"]["maxItems"])]
        for field in ("e", "h", "f"):
            row[field] = common[field]["items"]["enum"][:common[field]["maxItems"]]
        row["c"] = [{**row["c"][0], "k": f"fact{i}", "s": f"SYNTHETIC_TEST 字😀{i}"}
            for i in range(common["c"]["maxItems"])]
    encoded = required.clean_slots_output_v1(answer, schema)
    assert all(value is not None for row in encoded["d"].values() for field in ARRAY_FIELDS for value in row[field].values())
    reordered = json.loads(json.dumps(encoded, ensure_ascii=False, sort_keys=True))
    assert required.clean_slots_output_v1(reordered, schema, decode=True) == answer
    body = clean_output.build_clean_slots_responses_request(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST",
        schema_hint={}, schema=schema)
    canonical_body = json.loads(json.dumps(body, ensure_ascii=False, sort_keys=True))
    assert clean_output.clean_slots_responses_request_valid(canonical_body)
    assert token_upper_bound(body, allow_clean_slots=True) == token_upper_bound(canonical_body, allow_clean_slots=True)
    wire = required.clean_slots_payload_v1(payload, schema)
    assert required.clean_slots_payload_v1(json.loads(json.dumps(wire, sort_keys=True)), schema, decode=True) == payload


@pytest.mark.parametrize("limit", ["tokens", "cost", "count", "deadline", "unknown"])
def test_slot_budget_refusal_stays_before_another_native_io(configured, monkeypatch, limit):
    *_, schema, answer = keyed_contract()
    client = slot_client(configured)
    if limit == "tokens": client.budget.limits["max_tokens"] = 1
    elif limit == "cost": client.budget.limits["max_cost_usd"] = .000001
    elif limit == "count": client.budget.limits["max_chat_calls"] = 0
    elif limit == "deadline": client.budget._started -= 2400
    else:
        client.budget.reserve("chat", "a" * 32, slot_request())
        client.budget.mark_unknown("a" * 32)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: seen.append(1))
    with pytest.raises(RequiredModelUnavailable, match="REQUEST_BUDGET_REFUSED"):
        call(client, schema)
    assert seen == [] and configured.calls == []


def test_summary_accepts_new_native_policy_and_rejects_relabelled_old_policy():
    from test_clean_responses_output_v1 import responses_summary_client
    client = responses_summary_client()
    client.execution.policy.clean_output_policy_id = POLICY
    client.budget.clean_output_policy_id = POLICY
    clean = next(row for row in client.receipts if row["stage"] == "clean.llm_semantic")
    clean["output_policy_id"] = POLICY
    summary = required.model_summary_v2(client)
    assert summary["all_required_completed"]
    assert summary["clean_output_policy_id"] == POLICY and summary["clean_output_codec_id"] == CODEC
    clean["output_policy_id"] = "tiger.clean-output.responses-schema.v1"
    with pytest.raises(RequiredModelFailed): required.model_summary_v2(client)


def test_historical_summary_has_no_new_selector_or_codec_fields():
    from test_clean_responses_output_v1 import responses_summary_client
    from test_clean_strict_output_v2 import new_summary_client
    for client in (responses_summary_client(), new_summary_client()):
        summary = required.model_summary_v2(client)
        assert "clean_output_policy_id" not in summary and "clean_output_codec_id" not in summary


def test_all_empty_arrays_and_null_origin_remain_empty_semantics():
    _, schema, answer = full_contract()
    for row in answer["d"].values():
        for field in ARRAY_FIELDS: row[field] = []
        row["p"]["o"] = None
    encoded = required.clean_slots_output_v1(answer, schema)
    assert all(value is None for row in encoded["d"].values() for field in ARRAY_FIELDS for value in row[field].values())
    restored = required.clean_slots_output_v1(encoded, schema, decode=True)
    assert restored == answer
    assert all(row[field] == [] for row in restored["d"].values() for field in ARRAY_FIELDS)


@pytest.mark.parametrize("usage", [None, {"input_tokens": True, "output_tokens": 10, "total_tokens": 11},
    {"input_tokens": 40, "output_tokens": 10, "total_tokens": 51},
    {"input_tokens": 40, "output_tokens": 16385, "total_tokens": 16425}])
def test_unknown_or_overrun_usage_new_selector_preserves_reservation_and_freezes(configured, monkeypatch, usage):
    *_, schema, answer = keyed_contract()
    reply = native_response(required.clean_slots_output_v1(answer, schema))
    reply["usage"] = usage
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), Response(reply))[1])
    client = slot_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught: call(client, schema)
    accounting = caught.value.accounting
    assert accounting["output_policy_id"] == POLICY and accounting["status"] == "unknown"
    assert accounting["usage"] is None and accounting["native_usage"] is None
    assert accounting["frozen"] is True and accounting["reserved_tokens"] > 16384 and accounting["actual_tokens"] == 0
    with pytest.raises(RequiredModelUnavailable, match="REQUEST_BUDGET_REFUSED"): call(client, schema)
    assert seen == [1] and client.receipts == []


def test_prepared_request_and_loopback_receive_exact_prepaid_canonical_utf8_bytes(configured, monkeypatch):
    """Real HTTP only to a local server; no provider, key or outside socket."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    import requests

    *_, schema, answer = keyed_contract()
    encoded = required.clean_slots_output_v1(answer, schema)
    reply_bytes = transport._canonical(native_response(encoded))
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            observed.append((self.path, self.headers["Content-Type"], body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply_bytes)))
            self.end_headers()
            self.wfile.write(reply_bytes)

        def log_message(self, *args):
            # Local test server intentionally uses no console logging or
            # captured header/credential data; observed fields are explicit.
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    session = requests.Session()
    session.trust_env = False
    try:
        def post(url, **options):
            assert url == "https://api.deepseek.com/responses"
            assert "json" not in options and type(options["data"]) is bytes
            assert options["headers"]["Content-Type"] == "application/json"
            assert options["verify"] is True and options["allow_redirects"] is False and options["stream"] is True
            # The provider fixture key is not sent even to this loopback server.
            forwarded = {**options, "headers": {key: value for key, value in options["headers"].items()
                                                  if key != "Authorization"}}
            prepared = requests.Request("POST", f"http://127.0.0.1:{server.server_port}/responses",
                data=forwarded["data"], headers=forwarded["headers"]).prepare()
            assert prepared.body == options["data"]
            assert int(prepared.headers["Content-Length"]) == len(options["data"])
            return session.post(f"http://127.0.0.1:{server.server_port}/responses", **forwarded)

        monkeypatch.setattr(transport.requests, "post", post)
        client = slot_client(configured)
        assert call(client, schema) == encoded
        inputs = configured.calls[0]["inputs"]
        canonical = transport._canonical(inputs["request"])
        assert observed == [("/responses", "application/json", canonical)]
        assert "完整正文😀".encode() in canonical and b"\\u5b8c" not in canonical
        assert inputs["request_sha256"] == hashlib.sha256(canonical).hexdigest()
        assert inputs["budget_reservation"]["input_token_upper_bound"] == len(canonical) + 4096 + 128
        assert inputs["budget_reservation"]["request_sha256"] == hashlib.sha256(observed[0][2]).hexdigest()
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()
