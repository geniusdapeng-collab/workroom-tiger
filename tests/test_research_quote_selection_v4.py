"""SYNTHETIC_TEST exact research selections; no provider or account writes.

The recorded FULL17 output is not rewritten by these fixtures. Tests exercise
source/prefix binding, format-only subparts, same-symbol retained references
and the unchanged semantic gates after deterministic selection decoding.
"""
from copy import deepcopy
from dataclasses import replace
import json
import re

import pandas as pd
import pytest

from trading_system.llm import candidate_research as research
from trading_system.llm.deepseek_required import build_chat_request, validate_schema
from trading_system.llm.required_schemas import _text_v2
from trading_system.llm.request_budget import BUDGET_POLICY_V4, BudgetGuard, token_upper_bound
from trading_system.llm.clean_output import CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from test_short_term_research_v2 import (
    ASSESSMENT, SuccessfulResearchClient, context, public_document, facts, market, thesis,
)


def fixture_payload(documents, *, symbols=("AAA",), phase="initial", preliminary=None, caps=None, previous=None):
    plan = research.build_research_plan(context(watch=symbols), ASSESSMENT,
        budget=research.ResearchBudget.for_policy(BUDGET_POLICY_V4))
    evidence, markets = [], []
    for symbol in symbols:
        for row in facts():
            value = {key: item for key, item in row.items() if key != "id"} | {"symbol": symbol}
            evidence.append({"id": "fx_" + research.digest(value)[:24], **value})
        markets.append({**market(), "symbol": symbol,
                        "id": "md_" + symbol + "_20261026_" + "1" * 24})
    payload = research.build_research_payload(plan, list(symbols), documents, evidence, markets,
        phase=phase, preliminary=preliminary)
    if caps is not None:
        by_id = {doc.doc_id: doc for doc in documents}
        payload["documents"] = [research.source_view(by_id[row["id"]], characters=caps.get(row["id"], 24000))
                                for row in payload["documents"]]
    before = deepcopy(payload)
    bound, schema = research._bound_research_request_v4(payload, plan, documents, previous or {})
    assert payload == before
    assert {key: value for key, value in bound.items()
        if key not in {"request_bindings", "research_quote_selection"}} == before
    return bound, schema


def answer(payload, *, symbol="AAA", source=None, part="p0", news=False, date_field=False):
    value = thesis(strategy="news_event_v2" if news else "technical_flow_v2")
    value["symbol"] = symbol
    for scope in value["five_part"]:
        identity = (next(row["id"] for row in payload["market_evidence"] if row["symbol"] == symbol)
                    if scope == "stock" else next(row["id"] for row in payload["fact_evidence"]
                        if row["symbol"] == symbol and row["scope"] == scope))
        value["five_part"][scope]["evidence_ids"] = [identity]
    if source is not None:
        value["claims"] = [{"claim_id": "SYNTHETIC_TEST-claim-" + symbol,
            "statement": "SYNTHETIC_TEST model-authored interpretation.",
            "source_id": source, "quote_part_id": part, "origin_id": source,
            "evidence_kind": "primary", "time_role": "current"}]
        if date_field:
            value["claims"][0]["date_source_id"] = None
    if news:
        value["issuer_name"] = "SYNTHETIC_TEST issuer " + symbol
        value["issuer_identity_evidence_ids"] = [source]
    return {"coverage_summary": "SYNTHETIC_TEST complete fixture scope.", "theses": [value]}


def resolve(value, payload, schema, documents, previous=None):
    before = deepcopy(value)
    result, usage = research._resolve_research_quotes_v4(value, payload, schema)
    views = deepcopy(previous or {})
    research._remember_presentations(views, [row["symbol"] for row in payload["targets"]], payload["documents"])
    for row in result["theses"]:
        research.validate_thesis(row, symbol=row["symbol"], documents=documents,
            market_snapshot=next(item for item in payload["market_evidence"] if item["symbol"] == row["symbol"]),
            fact_evidence=payload["fact_evidence"], assessment=payload["assessment"],
            presented_documents=views[row["symbol"]])
    assert value == before
    return result, usage


def rejected(value, payload, schema):
    before = deepcopy(value)
    with pytest.raises(RequiredModelFailed):
        research._resolve_research_quotes_v4(value, payload, schema)
    assert value == before


@pytest.mark.parametrize("body", [
    "", "SYNTHETIC_TEST " + "甲🙂e\u0301 AbC,;!’" * 130,
    "  leading safe text <unsafe> trailing safe text  ",
    "<before", "after>", "a<>b", "<  >", "<", "  ",
    "x" * 599 + "<" + "safe tail with spaces  ",
])
def test_fixed_parts_and_all_format_safe_runs_preserve_exact_characters(body):
    parts = research._research_source_parts_v4(body)
    assert "".join(row[1] for row in parts["content_parts"]["rows"]) == body
    assert all(0 < len(row[1]) <= 600 for row in parts["content_parts"]["rows"])
    ids = [row[0] for row in parts["quote_parts"]["rows"]]
    assert len(ids) == len(set(ids))
    for identity, text in parts["quote_parts"]["rows"]:
        assert text in body and 0 < len(text) <= 600
        assert re.fullmatch(_text_v2(1000)["pattern"], text)
        block = parts["content_parts"]["rows"][int(identity.split(".")[0][1:])]
        assert text in block[1]
        if ".s" in identity:
            assert block[2] is False
        else:
            assert block[2] is True and text == block[1]
    if body == "  leading safe text <unsafe> trailing safe text  ":
        assert parts["quote_parts"]["rows"] == [["p0.s0", "  leading safe text "],
            ["p0.s1", "unsafe"], ["p0.s2", " trailing safe text  "]]
    if body in {"<  >", "<", "  ", ""}:
        assert parts["quote_parts"]["rows"] == []


@pytest.mark.parametrize("forbidden", ["<", ">", "\x00", "\x1f", "\x7f", "\x9f", "\u200b", "\u200f",
    "\u2028", "\u2029", "\u202a", "\u202e", "\u2060", "\u2066", "\u206f", "\ufeff"])
def test_forbidden_characters_only_split_safe_runs_without_removing_surrounding_spaces(forbidden):
    original = " safe LEFT " + forbidden + " safe RIGHT "
    parts = research._research_source_parts_v4(original)
    assert parts["content_parts"]["rows"] == [["p0", original, False]]
    assert parts["quote_parts"]["rows"] == [["p0.s0", " safe LEFT "], ["p0.s1", " safe RIGHT "]]


def test_source_view_normalization_and_unicode_are_exact_without_case_or_punctuation_repair():
    doc = public_document(content="SYNTHETIC_TEST\n\tABC—‘Unicode’\u00a0mixed CASE, punctuation!" * 20)
    payload, schema = fixture_payload([doc])
    original = payload["documents"][0]["content"]
    assert original == re.sub(r"\s+", " ", doc.content).strip()
    value = answer(payload, source=doc.doc_id, part="p1")
    result, _ = resolve(value, payload, schema, [doc])
    assert result["theses"][0]["claims"][0]["quote"] == original[600:1200]
    assert "CASE" in result["theses"][0]["claims"][0]["quote"]
    for bad in ("P1", "p01", "p1 ", "p1+p0"):
        mutated = deepcopy(value)
        mutated["theses"][0]["claims"][0]["quote_part_id"] = bad
        rejected(mutated, payload, schema)


def test_source_local_same_part_id_resolves_only_the_selected_source_and_preserves_origin_difference():
    a = public_document(content="SYNTHETIC_TEST A literal text.")
    b = public_document("ds_neighbor", content="SYNTHETIC_TEST B DIFFERENT text.")
    payload, schema = fixture_payload([a, b])
    value = answer(payload, source=b.doc_id)
    value["theses"][0]["claims"][0]["origin_id"] = a.doc_id
    result, _ = resolve(value, payload, schema, [a, b])
    claim = result["theses"][0]["claims"][0]
    assert claim["quote"] == b.content and claim["source_id"] == b.doc_id and claim["origin_id"] == a.doc_id
    assert {key: item for key, item in claim.items() if key != "quote"} == {
        key: item for key, item in value["theses"][0]["claims"][0].items() if key != "quote_part_id"}


def test_ordinary_claim_may_use_other_issuer_shown_source_but_identity_is_still_own():
    other = replace(public_document(), meta={"target_symbols": ["BBB"]})
    payload, schema = fixture_payload([other], symbols=("AAA", "BBB"))
    value = answer(payload, source=other.doc_id)
    value["theses"].extend(answer(payload, symbol="BBB")["theses"])
    resolve(value, payload, schema, [other])
    value["theses"][0]["issuer_identity_evidence_ids"] = [other.doc_id]
    rejected(value, payload, schema)


@pytest.mark.parametrize("change", ["unknown", "neighbor_only", "unshown_tail", "unsafe_whole", "literal", "both", "missing"])
def test_current_claim_rejects_unknown_unshown_unsafe_and_mixed_literal_bindings(change):
    first = public_document(content="SYNTHETIC_TEST <unsafe> safe original " + "x" * 900)
    second = public_document("ds_second", content="SYNTHETIC_TEST " + "y" * 1300)
    payload, schema = fixture_payload([first, second], caps={first.doc_id: 600})
    value = answer(payload, source=first.doc_id, part="p0.s0")
    claim = value["theses"][0]["claims"][0]
    if change == "unknown": claim["quote_part_id"] = "p999"
    elif change in {"neighbor_only", "unshown_tail"}: claim["quote_part_id"] = "p1"
    elif change == "unsafe_whole": claim["quote_part_id"] = "p0"
    elif change == "literal":
        claim.pop("quote_part_id")
        claim["quote"] = "SYNTHETIC_TEST"
    elif change == "both": claim["quote"] = "SYNTHETIC_TEST"
    else: claim.pop("quote_part_id")
    rejected(value, payload, schema)


@pytest.mark.parametrize("date_field", [False, True])
def test_retained_initial_claim_preserves_exact_date_field_presence_and_changes_only_model_statement(date_field):
    doc = public_document(content="SYNTHETIC_TEST complete initial source.")
    initial, initial_schema = fixture_payload([doc])
    first, _ = resolve(answer(initial, source=doc.doc_id, news=True, date_field=date_field), initial, initial_schema, [doc])
    previous = {"AAA": initial["documents"]}
    final, schema = fixture_payload([doc], phase="final", preliminary=first["theses"], caps={doc.doc_id: 0}, previous=previous)
    value = deepcopy(first)
    reference = final["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]
    value["theses"][0]["claims"] = [{"retained_initial_claim_ref": reference["retained_initial_claim_ref"],
        "statement": "SYNTHETIC_TEST changed model interpretation; original source retained."}]
    result, usage = resolve(value, final, schema, [doc], previous)
    claim, old = result["theses"][0]["claims"][0], first["theses"][0]["claims"][0]
    assert ("date_source_id" in claim) is date_field
    assert {key: item for key, item in claim.items() if key != "statement"} == {
        key: item for key, item in old.items() if key != "statement"}
    assert claim["statement"] != old["statement"]
    assert usage[0]["initial_claim_sha256"] == research.digest(old)
    assert usage[0]["initial_thesis_sha256"] == research.digest(first["theses"][0])
    assert final["documents"][0]["content"] == "" and final["research_quote_selection"]["sources"][0]["quote_parts"]["rows"] == []


@pytest.mark.parametrize("change", ["foreign_symbol", "unknown", "mixed", "extra_date", "stale_initial", "future_phase"])
def test_retained_claim_references_are_own_symbol_exact_initial_version_and_final_only(change):
    a, b = public_document(), replace(public_document("ds_bbb"), meta={"target_symbols": ["BBB"]})
    initial, initial_schema = fixture_payload([a, b], symbols=("AAA", "BBB"))
    value = answer(initial, source=a.doc_id)
    value["theses"].extend(answer(initial, symbol="BBB", source=b.doc_id)["theses"])
    first, _ = resolve(value, initial, initial_schema, [a, b])
    previous = {symbol: initial["documents"] for symbol in ("AAA", "BBB")}
    final, schema = fixture_payload([a, b], symbols=("AAA", "BBB"), phase="final", preliminary=first["theses"], previous=previous)
    refs = final["research_quote_selection"]["retained_initial_claims"]
    output = deepcopy(first)
    for index, item in enumerate(output["theses"]):
        item["claims"] = [{"retained_initial_claim_ref": refs[index]["claims"][0]["retained_initial_claim_ref"],
                           "statement": "SYNTHETIC_TEST retained interpretation."}]
    if change == "foreign_symbol": output["theses"][0]["claims"][0]["retained_initial_claim_ref"] = refs[1]["claims"][0]["retained_initial_claim_ref"]
    elif change == "unknown": output["theses"][0]["claims"][0]["retained_initial_claim_ref"] = "ri_unknown"
    elif change == "mixed": output["theses"][0]["claims"][0]["quote_part_id"] = "p0"
    elif change == "extra_date": output["theses"][0]["claims"][0]["date_source_id"] = None
    elif change == "stale_initial":
        changed = deepcopy(first["theses"])
        changed[0]["claims"][0]["statement"] += " Updated initial version."
        final, schema = fixture_payload([a, b], symbols=("AAA", "BBB"), phase="final", preliminary=changed, previous=previous)
    else:
        final, schema = initial, initial_schema
    rejected(output, final, schema)


def test_retained_claim_cannot_vacuously_satisfy_news_and_decoded_original_gate_is_independent():
    doc = public_document()
    initial, initial_schema = fixture_payload([doc])
    value = answer(initial, source=doc.doc_id)
    value["theses"][0]["claims"][0]["evidence_kind"] = "commentary"
    first, _ = resolve(value, initial, initial_schema, [doc])
    final, schema = fixture_payload([doc], phase="final", preliminary=first["theses"], previous={"AAA": initial["documents"]})
    value = answer(final, source=doc.doc_id, news=True)
    ref = final["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]["retained_initial_claim_ref"]
    value["theses"][0]["claims"] = [{"retained_initial_claim_ref": ref, "statement": "SYNTHETIC_TEST changed interpretation."}]
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)
    weakened = deepcopy(schema)
    weakened["properties"]["theses"]["items"].pop("allOf")
    validate_schema(value, weakened)
    rejected(value, final, weakened)


@pytest.mark.parametrize("fetch_parent", [False, True])
def test_own_retained_claim_keeps_previously_shown_other_issuer_source_metadata_only(fetch_parent):
    other = replace(public_document("ds_bbb"), meta={"target_symbols": ["BBB"]})
    docs, selected = [other], other
    if fetch_parent:
        selected = replace(other, doc_id="ds_bbb_fetch", source="deepseek_fetch", published="",
            meta={"target_symbols": ["BBB"], "search_source_id": other.doc_id})
        docs.append(selected)
    initial, initial_schema = fixture_payload(docs, symbols=("AAA", "BBB"))
    value = answer(initial, source=selected.doc_id)
    if fetch_parent:
        value["theses"][0]["claims"][0]["date_source_id"] = other.doc_id
    value["theses"].extend(answer(initial, symbol="BBB")["theses"])
    first, _ = resolve(value, initial, initial_schema, docs)
    own = [first["theses"][0]]
    previous = {"AAA": initial["documents"], "BBB": initial["documents"]}
    final, schema = fixture_payload(docs, phase="final", preliminary=own, previous=previous)
    assert final["documents"] == []
    assert final["research_quote_selection"]["sources"] == []
    assert {row["source_id"] for row in final["request_bindings"]["sources"]} == {doc.doc_id for doc in docs}
    output = {"coverage_summary": value["coverage_summary"], "theses": deepcopy(own)}
    reference = final["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]["retained_initial_claim_ref"]
    output["theses"][0]["claims"] = [{"retained_initial_claim_ref": reference,
                                      "statement": "SYNTHETIC_TEST reconsider prior shown evidence."}]
    result, _ = resolve(output, final, schema, docs, previous)
    expected = first["theses"][0]["claims"][0]
    assert {key: item for key, item in result["theses"][0]["claims"][0].items() if key != "statement"} == {
        key: item for key, item in expected.items() if key != "statement"}
    # Expanded metadata cannot authorize a new quote from an old-only body.
    current = answer(final, source=selected.doc_id)
    if fetch_parent: current["theses"][0]["claims"][0]["date_source_id"] = other.doc_id
    rejected(current, final, schema)


@pytest.mark.parametrize("change", ["unshown", "missing_metadata", "changed_body", "changed_original_hash"])
def test_retained_registry_rejects_unshown_or_forged_previous_source_metadata(change):
    doc = replace(public_document("ds_bbb"), meta={"target_symbols": ["BBB"]})
    initial, initial_schema = fixture_payload([doc], symbols=("AAA", "BBB"))
    output = answer(initial, source=doc.doc_id)
    output["theses"].extend(answer(initial, symbol="BBB")["theses"])
    first, _ = resolve(output, initial, initial_schema, [doc])
    previous = {"AAA": deepcopy(initial["documents"])}
    if change == "unshown": previous["AAA"] = [research.source_view(doc, characters=0)]
    elif change == "missing_metadata": previous["AAA"] = []
    elif change == "changed_body": previous["AAA"][0]["content"] = "SYNTHETIC_TEST forged quote body"
    else: previous["AAA"][0]["original_content_sha256"] = "0" * 64
    with pytest.raises(RequiredModelFailed):
        fixture_payload([doc], phase="final", preliminary=[first["theses"][0]], previous=previous)


@pytest.mark.parametrize("change", ["legal_growth", "invented_old_owner", "reordered_old_owner", "new_owner_identity"])
def test_retained_registry_accepts_only_monotonic_ownership_without_retroactive_initial_identity(change):
    doc = public_document(content="SYNTHETIC_TEST source with newly observed owner.")
    initial, initial_schema = fixture_payload([doc], symbols=("AAA", "BBB"))
    value = answer(initial, source=doc.doc_id)
    value["theses"].extend(answer(initial, symbol="BBB", source=doc.doc_id)["theses"])
    first, _ = resolve(value, initial, initial_schema, [doc])
    previous = {symbol: deepcopy(initial["documents"]) for symbol in ("AAA", "BBB")}
    merged = research.merge_documents([doc], [replace(doc, meta={"target_symbols": ["BBB"]})])
    assert merged[0].meta["target_symbols"] == ["AAA", "BBB"]
    own = [deepcopy(first["theses"][1])]
    if change == "invented_old_owner": previous["BBB"][0]["target_symbols"] = ["CCC"]
    elif change == "reordered_old_owner": previous["BBB"][0]["target_symbols"] = ["BBB", "AAA"]
    elif change == "new_owner_identity": own[0]["issuer_identity_evidence_ids"] = [doc.doc_id]
    if change != "legal_growth":
        with pytest.raises(RequiredModelFailed):
            fixture_payload(merged, symbols=("BBB",), phase="final", preliminary=own, previous=previous)
        return
    final, schema = fixture_payload(merged, symbols=("BBB",), phase="final", preliminary=own, previous=previous)
    output = {"coverage_summary": value["coverage_summary"], "theses": deepcopy(own)}
    reference = final["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]["retained_initial_claim_ref"]
    output["theses"][0]["claims"] = [{"retained_initial_claim_ref": reference,
        "statement": "SYNTHETIC_TEST newly interpreted unchanged retained fact."}]
    result, _ = resolve(output, final, schema, merged, previous)
    assert result["theses"][0]["claims"][0]["source_id"] == doc.doc_id


def test_ownership_growth_completes_research_and_roundtrips_final_source_index():
    class GrowingOwner(SuccessfulResearchClient):
        def __init__(self):
            super().__init__()
            self.execution.policy.budget_policy_id = BUDGET_POLICY_V4

        def search(self, query, **kwargs):
            if "SYNTHETIC_TEST shared source" in query:
                self.calls.append(("search", "BBB"))
                return [self.document("AAA")]
            return super().search(query, **kwargs)

        def complete_json(self, **kwargs):
            value = super().complete_json(**kwargs)
            payload = self.payloads[-1]
            shared = self.document("AAA")["id"]
            if payload["phase"] == "initial":
                for row in value["theses"]:
                    row["claims"] = answer(payload, symbol=row["symbol"], source=shared)["theses"][0]["claims"]
                    if row["symbol"] == "BBB":
                        row["followup"] = {"needed": True, "question": "SYNTHETIC_TEST shared source", "fetch_source_id": None}
            else:
                ref = payload["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]["retained_initial_claim_ref"]
                value["theses"][0]["claims"] = [{"retained_initial_claim_ref": ref,
                    "statement": "SYNTHETIC_TEST reconsider the exact retained original."}]
            return value

    ctx = context(watch=["AAA", "BBB"])
    ctx["market_data"] = {"stock_ohlcv": {symbol: pd.DataFrame({"Close": [100.]},
        index=pd.to_datetime(["2026-10-26"])) for symbol in ("AAA", "BBB")}}
    client = GrowingOwner()
    result = research.research_candidates(client, ctx, [])
    shared = client.document("AAA")["id"]
    assert result["counters"] == {"search": 3, "fetch": 0, "chat": 2}
    index = next(row for row in result["source_index"] if row["id"] == shared)
    assert index["target_symbols"] == ["AAA", "BBB"]
    for symbol in ("AAA", "BBB"):
        restored = research.presented_views_for_symbol(result, symbol)
        view = next(row for row in restored if row["id"] == shared)
        assert view["content"] == client.document("AAA")["content"]
        assert view["target_symbols"] == ["AAA", "BBB"]


@pytest.mark.parametrize("change", ["missing_stock", "wrong_scope", "future", "unknown_date", "origin", "kind", "gap", "quote_identity"])
def test_original_semantic_predicates_still_reject_after_selection(change):
    doc = public_document()
    if change == "future": doc = replace(doc, published="2026-10-28T14:00:00+00:00")
    elif change == "unknown_date": doc = replace(doc, published="")
    payload, schema = fixture_payload([doc])
    value = answer(payload, source=doc.doc_id, news=True)
    row = value["theses"][0]
    if change == "missing_stock": row["five_part"]["stock"]["evidence_ids"] = [doc.doc_id]
    elif change == "wrong_scope": row["five_part"]["market"]["evidence_ids"] = row["five_part"]["plan"]["evidence_ids"]
    elif change == "origin": row["claims"][0]["origin_id"] = None
    elif change == "kind": row["claims"][0]["evidence_kind"] = "media"
    elif change == "gap": row.update(status="insufficient", gaps=[])
    elif change == "quote_identity": row["claims"][0]["date_source_id"] = doc.doc_id
    rejected(value, payload, schema)


@pytest.mark.parametrize("kind", ["literal_quote", "duplicate_claim_id"])
def test_initial_invalid_output_is_rejected_before_any_followup_io(kind):
    class InvalidInitial(SuccessfulResearchClient):
        def __init__(self):
            super().__init__(fetch=True)
            self.execution.policy.budget_policy_id = BUDGET_POLICY_V4

        def complete_json(self, **kwargs):
            value = super().complete_json(**kwargs)
            payload = self.payloads[-1]
            row = value["theses"][0]
            row["claims"] = answer(payload, source=payload["documents"][0]["id"])["theses"][0]["claims"]
            if kind == "literal_quote":
                row["claims"][0].pop("quote_part_id")
                row["claims"][0]["quote"] = "SYNTHETIC_TEST invented literal quote."
            else:
                row["claims"].append({**row["claims"][0], "statement": "SYNTHETIC_TEST distinct statement, duplicate identity."})
            return value

    ctx = context(watch=["AAA"])
    ctx["market_data"] = {"stock_ohlcv": {"AAA": pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))}}
    client = InvalidInitial()
    before = research.canonical(ctx)
    with pytest.raises(RequiredModelFailed):
        research.research_candidates(client, ctx, [])
    assert client.calls == [("search", "AAA"), ("chat", "initial")]
    assert research.canonical(ctx) == before


def test_more_than_one_thousand_safe_runs_are_not_silently_dropped_or_rejected_by_generic_id_cap():
    doc = public_document(content="a<" * 1500)
    payload, schema = fixture_payload([doc])
    choices = payload["research_quote_selection"]["sources"][0]["quote_parts"]["rows"]
    assert len(choices) == 1500 and choices[-1] == ["p4.s299", "a"]
    value = answer(payload, source=doc.doc_id, part=choices[-1][0])
    result, _ = resolve(value, payload, schema, [doc])
    assert result["theses"][0]["claims"][0]["quote"] == "a"
    request = build_chat_request(system=research.RESEARCH_SYSTEM_V4,
        user=research.RESEARCH_TASK_V4 + "\n" + research.canonical(payload), schema_hint={}, schema=schema, max_tokens=65536)
    guard = BudgetGuard.for_policy(BUDGET_POLICY_V4,
        clean_output_policy_id=CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, clock=lambda: 0.)
    reservation = guard.reserve("chat", "a" * 32, request)
    assert reservation["input_token_upper_bound"] == token_upper_bound(request)[0]
    assert reservation["token_upper_bound"] < guard.limits["max_tokens"]


@pytest.mark.parametrize("change", ["text", "source_hash", "retained_hash"])
def test_selection_metadata_cannot_be_changed_even_with_a_permissive_caller_schema(change):
    doc = public_document()
    initial, initial_schema = fixture_payload([doc])
    first, _ = resolve(answer(initial, source=doc.doc_id), initial, initial_schema, [doc])
    payload, schema = fixture_payload([doc], phase="final", preliminary=first["theses"], previous={"AAA": initial["documents"]})
    value = answer(payload, source=doc.doc_id)
    forged = deepcopy(payload)
    if change == "text": forged["research_quote_selection"]["sources"][0]["quote_parts"]["rows"][0][1] += " forged"
    elif change == "source_hash": forged["research_quote_selection"]["sources"][0]["presented_content_sha256"] = "0" * 64
    else: forged["research_quote_selection"]["retained_initial_claims"][0]["initial_thesis_sha256"] = "0" * 64
    rejected(value, forged, schema)
