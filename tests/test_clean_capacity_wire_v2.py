"""Lossless clean wire and stage policy regressions; all data is SYNTHETIC_TEST.

These tests use no network or provider. Capacity measurements are serialized
bytes and actual BudgetGuard admission boundaries, never predicted model usage.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from trading_system.llm import deepseek_required as transport
from trading_system.llm import required
from trading_system.llm.required_schemas import clean_schema_v2
from trading_system.llm.deepseek_required import validate_schema
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from test_deepseek_required import configured, Response, SCHEMA, chat_response
from test_short_term_research_v2 import public_document, SemanticStub, research_module


def documents(count):
    return [public_document(f"ds_{index:024x}",
        content=f"SYNTHETIC_TEST source {index}; d0 d1 ds_000000000000000000000001 " +
                "原文及反证不得丢失😀Zażółć。" * (index + 1),
        published="" if index % 2 else "2026-10-26T12:00:00+00:00")
        for index in range(count)]


def schema_for(payload):
    return clean_schema_v2([row["id"] for row in payload["documents"]], ["memory"], ["SMH"],
        quote_part_ids={row["id"]: [part[0] for part in row["content_parts"]["rows"] if part[2]]
                        for row in payload["documents"]},
        all_doc_ids=[row[0] for row in payload["all_source_index"]["rows"]])


def annotations(payload, *, filler="中"):
    """All original natural-text bounds are exercised, including opaque text."""
    ids = [row[0] for row in payload["all_source_index"]["rows"]]
    text = "SYNTHETIC_TEST d0 d1 " + (ids[-1] if ids else "none") + " "

    def exact(length):
        return (text + filler * length)[:length]

    return {"documents": [{"id": row["id"], "tickers": ["AAA"], "sentiment": "mixed",
        "sentiment_score": -.125, "events": ["other"], "summary_zh": exact(800),
        "relevance": .375, "chain_ids": ["memory"], "sector_etfs": ["SMH"],
        "provenance": {"evidence_kind": "commentary", "origin_id": ids[-1],
            "origin_note": exact(600), "time_role": "unknown"},
        "claims": [{"statement": exact(600),
            "quote_part_id": next(part[0] for part in row["content_parts"]["rows"] if part[2]),
            "claim_key": f"a{number}" + "x" * 158, "time_role": "background"}
            for number in range(4)]} for row in payload["documents"]]}


@pytest.mark.parametrize("upgrade,stage,expected", [
    (True, "clean.llm_semantic", "disabled"),
    (True, "research.candidates", "enabled"),
    (True, "decision.debate", "enabled"),
    (True, "tech.risk", "enabled"),
    (False, "clean.llm_semantic", "enabled"),
])
def test_stage_owned_thinking_policy_reaches_the_exact_paid_boundary_without_network(
        configured, monkeypatch, upgrade, stage, expected):
    configured.upgrade = upgrade
    configured._active.spec.name = stage
    captured = []

    def fake_post(url, **kwargs):
        captured.append(deepcopy(kwargs["json"]))
        return Response(chat_response())

    monkeypatch.setattr(transport.requests, "post", fake_post)
    client = transport.DeepSeekRequiredClient(configured)
    effort = "low" if upgrade and stage == "clean.llm_semantic" else "high"
    assert client.complete_json(system="SYNTHETIC_TEST JSON", user="SYNTHETIC_TEST input",
        schema_hint={}, schema=SCHEMA, reasoning_effort=effort) == {"score": 7, "reason": "evidence"}
    assert len(captured) == 1
    assert captured[0]["thinking"] == {"type": expected}
    assert captured[0]["model"] == "deepseek-flash"
    assert captured[0]["reasoning_effort"] == effort
    assert configured.calls[0]["inputs"]["request"] == captured[0]
    if upgrade:
        assert client.budget.snapshot()["actual_tokens"] == 50
        assert client.budget.snapshot()["reserved_tokens"] == 0


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_wire_input_roundtrip_retains_all_metadata_parts_and_global_id_binding(count):
    originals = documents(count)
    payload = required.clean_payload_v2(originals[:8], all_docs=originals)
    before = deepcopy(payload)
    wire = required.clean_payload_wire_v2(payload)
    assert wire["clean_wire"]["schema"] == "tiger.clean-wire.v1"
    assert wire["all_source_index"]["columns"] == ["ref", "id", "title", "url", "published_at"]
    assert wire["all_source_index"]["rows"] == [[f"d{index}", *row]
        for index, row in enumerate(payload["all_source_index"]["rows"])]
    assert required.clean_payload_wire_v2(wire, decode=True) == payload == before
    refs = {row[0] for row in wire["all_source_index"]["rows"]}
    assert len(refs) == count
    positions = {key: index for index, key in enumerate(wire["documents"]["columns"])}
    for index, row in enumerate(wire["documents"]["rows"]):
        assert row[positions["source_ref"]] == f"d{index}"
        assert row[positions["content_parts"]] == payload["documents"][index]["content_parts"]
    if count >= 8:
        assert len(research_module().canonical(wire).encode()) < len(research_module().canonical(payload).encode())


@pytest.mark.parametrize("filler", ["x", "中", "😀"])
def test_wire_schema_and_every_maximum_text_roundtrip_are_bijective(filler):
    originals = documents(55)
    payload = required.clean_payload_v2(originals[:8], all_docs=originals)
    schema = schema_for(payload)
    ids = [doc.doc_id for doc in originals]
    value = annotations(payload, filler=filler)
    before, before_schema = deepcopy(value), deepcopy(schema)
    validate_schema(value, schema)
    wire_schema = required.clean_schema_wire_v2(schema, ids)
    encoded = required.clean_output_wire_v2(value, ids)
    validate_schema(encoded, wire_schema)
    assert required.clean_output_wire_v2(encoded, ids, decode=True) == value == before
    assert required.clean_schema_wire_v2(wire_schema, ids, decode=True) == schema == before_schema
    assert encoded["d"][0]["i"] == "d0" and encoded["d"][0]["p"]["o"] == "d54"
    assert encoded["d"][0]["z"] == value["documents"][0]["summary_zh"]
    assert encoded["d"][0]["p"]["n"] == value["documents"][0]["provenance"]["origin_note"]
    assert all(len(claim["s"]) == 600 and len(claim["k"]) == 160 for claim in encoded["d"][0]["c"])
    assert len(research_module().canonical(encoded).encode()) < len(research_module().canonical(value).encode())


@pytest.mark.parametrize("text", ["d0", "d54", "ds_000000000000000000000001",
    "d0 and ds_000000000000000000000001 are prose", "d0\n<d54>\t😀\u200b"])
def test_wire_codec_never_substitutes_or_repairs_arbitrary_natural_text(text):
    payload = required.clean_payload_v2(documents(2))
    value = annotations(payload)
    row = value["documents"][0]
    row["summary_zh"] = row["provenance"]["origin_note"] = row["claims"][0]["statement"] = text
    ids = [row[0] for row in payload["all_source_index"]["rows"]]
    encoded = required.clean_output_wire_v2(value, ids)
    assert encoded["d"][0]["z"] == encoded["d"][0]["p"]["n"] == encoded["d"][0]["c"][0]["s"] == text
    assert required.clean_output_wire_v2(encoded, ids, decode=True) == value
    if "\n" in text:
        with pytest.raises(LLMUnavailable):
            validate_schema(encoded, required.clean_schema_wire_v2(schema_for(payload), ids))


class DirectWireClient:
    def __init__(self, *, mutate=None, fail_call=None):
        self.execution = SemanticStub({}).execution
        self.requests = []
        self.mutate, self.fail_call = mutate, fail_call

    def complete_json(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if len(self.requests) == self.fail_call:
            raise LLMUnavailable("SYNTHETIC_TEST incomplete answer")
        payload = json.loads(kwargs["user"].split("\n", 1)[1])
        assessment = payload.pop("assessment")
        assert assessment["run_date"] == "2026-10-27"
        canonical = required.clean_keyed_payload_v2(payload, decode=True)
        ids = [row[0] for row in canonical["all_source_index"]["rows"]]
        value = required.clean_keyed_output_v2(annotations(canonical), ids,
            batch_ids=[row["id"] for row in canonical["documents"]])
        if self.mutate:
            self.mutate(value)
        return value


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_direct_production_wire_consumes_every_source_once_and_restores_original_facts(count):
    originals, client = documents(count), DirectWireClient()
    before = deepcopy(originals)
    cleaned = required.clean_v2(client, originals)
    assert originals == before and len(cleaned) == count
    assert len(client.requests) == max(1, (count + 7) // 8)
    assert [doc.raw.doc_id for doc in cleaned] == [doc.doc_id for doc in originals]
    shown = []
    for request in client.requests:
        payload = json.loads(request["user"].split("\n", 1)[1])
        payload.pop("assessment")
        decoded = required.clean_keyed_payload_v2(payload, decode=True)
        shown.extend(row["id"] for row in decoded["documents"])
        assert [row[0] for row in decoded["all_source_index"]["rows"]] == [doc.doc_id for doc in originals]
        assert request["max_tokens"] == 16384 and request["reasoning_effort"] == "low"
    assert shown == [doc.doc_id for doc in originals]
    for doc in cleaned:
        assert doc.provenance["origin_id"] == originals[-1].doc_id
        assert len(doc.summary_zh) == 800 and len(doc.provenance["origin_note"]) == 600
        assert len(doc.claims) == 4
        assert all(len(claim["statement"]) == 600 and len(claim["claim_key"]) == 160
                   and claim["quote"] in doc.raw.content for claim in doc.claims)


@pytest.mark.parametrize("mutation", ["unknown_field", "unknown_source", "foreign_part",
    "extra_quote", "duplicate_document", "omitted_document", "omitted_claim_key", "empty_summary"])
def test_direct_wire_rejects_invalid_model_answer_without_repair_retry_or_partial_success(mutation):
    def change(value):
        row = value["d"]["d0"]
        if mutation == "unknown_field": row["unregistered"] = "SYNTHETIC_TEST"
        elif mutation == "unknown_source": row["p"]["o"] = "d999"
        elif mutation == "foreign_part": row["c"][0]["q"] = "p999"
        elif mutation == "extra_quote": row["c"][0]["quote"] = "SYNTHETIC_TEST"
        elif mutation == "duplicate_document": value["d"]["d1"] = {"i": "d0", **deepcopy(row)}
        elif mutation == "omitted_document": value["d"].pop("d1")
        elif mutation == "omitted_claim_key": del row["c"][0]["k"]
        else: row["z"] = ""
    client = DirectWireClient(mutate=change)
    with pytest.raises((RequiredModelFailed, LLMUnavailable)):
        required.clean_v2(client, documents(2))
    assert len(client.requests) == 1


@pytest.mark.parametrize("mutation", ["alias", "identity", "metadata", "unknown_policy",
    "unknown_field", "short_row", "duplicate_alias"])
def test_wire_payload_inverse_rejects_noncanonical_or_ambiguous_join(mutation):
    wire = required.clean_payload_wire_v2(required.clean_payload_v2(documents(2)))
    if mutation == "alias": wire["all_source_index"]["rows"][0][0] = "d99"
    elif mutation == "identity": wire["all_source_index"]["rows"][0][1] = wire["all_source_index"]["rows"][1][1]
    elif mutation == "metadata": wire["documents"]["columns"].append("title")
    elif mutation == "unknown_policy": wire["clean_wire"]["schema"] = "tiger.clean-wire.unknown"
    elif mutation == "unknown_field": wire["clean_wire"]["unexpected"] = "SYNTHETIC_TEST"
    elif mutation == "short_row": wire["documents"]["rows"][0].pop()
    else: wire["all_source_index"]["rows"][1][0] = wire["all_source_index"]["rows"][0][0]
    with pytest.raises((RequiredModelFailed, ValueError)):
        required.clean_payload_wire_v2(wire, decode=True)


def test_v1_required_prefix_and_pure_request_default_remain_exact():
    prefix = Path(required.__file__).read_bytes()[:19031]
    assert hashlib.sha256(prefix).hexdigest() == "f2cd003fbe8a87f42aa6dd5c62b7d5cbacd6ecaa6826a889f46275f3452777b1"
    request = transport.build_chat_request(system="SYNTHETIC_TEST", user="SYNTHETIC_TEST",
        schema_hint={}, schema=SCHEMA)
    assert request == {"model": "deepseek-flash", "messages": [
        {"role": "system", "content": "SYNTHETIC_TEST\nReturn exactly one JSON object. JSON only."},
        {"role": "user", "content": "SYNTHETIC_TEST\nJSON schema:\n" +
            json.dumps(SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")) +
            "\nJSON field guidance:\n{}"}],
        "max_tokens": 16384, "temperature": .2, "stream": False,
        "response_format": {"type": "json_object"}, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
