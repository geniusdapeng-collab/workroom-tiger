"""Existing research predicates are visible before generation; no remote IO.

The FULL16 fixture contains unchanged IDs, selections and time metadata only.
Its surrounding prose below is SYNTHETIC_TEST, never a repaired real answer.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from trading_system.llm import candidate_research as research, required, required_schemas as schemas
from trading_system.llm.deepseek_required import build_chat_request, validate_schema
from trading_system.llm.request_budget import BUDGET_POLICY_V4, token_upper_bound
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from trading_system.search.models import RawDocument
from test_short_term_research_v2 import (ASSESSMENT, SuccessfulResearchClient, SemanticStub,
    context, public_document, market, facts, thesis, event_thesis, partial_news_context,
    semantic_capacity_documents)
from test_final_capacity_wire_v2 import FinalWireClient, answer_for


FIXTURES = Path(__file__).parent / "fixtures"
FULL16_SHA256 = "ce24dae3fe396e791bc0dda5cf70ca64c4cbb8bff93417567bee77b4606748de"


def schema_for(documents=(), *, phase="initial", shown=None, symbol="AAA"):
    plan = research.build_research_plan(context(watch=[symbol]), ASSESSMENT)
    guide = research.build_thesis_binding_guide(symbols=[symbol], documents=documents,
        fact_evidence=facts(), market_evidence=[market()], assessment=ASSESSMENT, phase=phase,
        presented_by_symbol={symbol: shown if shown is not None else [research.source_view(doc) for doc in documents]},
        supplemental_query_templates={row["symbol"]: row["supplemental_query_template"] for row in plan["targets"]})
    return schemas.thesis_schema_v4([symbol], [doc.doc_id for doc in documents],
        [row["id"] for row in facts()], [market()["id"]], bindings=guide), guide


def reject(value, schema):
    before = deepcopy(value)
    with pytest.raises(LLMUnavailable):
        validate_schema(value, schema)
    assert value == before


def original_validate(value, documents=(), shown=None):
    research.validate_thesis(value, symbol="AAA", documents=documents,
        market_snapshot=market(), fact_evidence=facts(), assessment=ASSESSMENT,
        presented_documents=shown)


def test_stock_md_only_and_technical_flow_without_claims_remain_legal():
    schema, _ = schema_for()
    value = thesis()
    assert value["five_part"]["stock"]["evidence_ids"] == [market()["id"]]
    validate_schema(value, schema)
    original_validate(value)


def test_ordinary_claim_can_keep_a_displayed_other_issuer_source_without_new_ownership_rule():
    doc = replace(public_document(), meta={"target_symbols": ["BBB"]})
    value = event_thesis(doc)
    value.update(strategy="technical_flow_v2", issuer_name=None, issuer_identity_evidence_ids=[])
    schema, _ = schema_for([doc])
    validate_schema(value, schema)
    original_validate(value, [doc])
    value["issuer_identity_evidence_ids"] = [doc.doc_id]
    reject(value, schema)
    with pytest.raises(RequiredModelFailed):
        original_validate(value, [doc])


@pytest.mark.parametrize("seconds,valid", [(0, True), (604800, True), (604801, False), (-1, False)])
@pytest.mark.parametrize("phase", ["initial", "final", "decision"])
def test_current_inclusive_seven_day_boundary_uses_frozen_instant(seconds, valid, phase):
    published = (datetime.fromisoformat(ASSESSMENT["admitted_at_utc"]) - timedelta(seconds=seconds)).isoformat()
    doc = public_document(published=published)
    value = event_thesis(doc)
    schema, guide = schema_for([doc], phase=phase)
    assert guide["sources"][0]["date_options"][0]["age_seconds_at_admission"] == seconds
    if valid:
        validate_schema(value, schema)
        original_validate(value, [doc])
    else:
        reject(value, schema)
        with pytest.raises(RequiredModelFailed):
            original_validate(value, [doc])
        value.update(strategy="technical_flow_v2", issuer_name=None, issuer_identity_evidence_ids=[])
        value["claims"][0]["time_role"] = "background"
        validate_schema(value, schema)
        original_validate(value, [doc])


@pytest.mark.parametrize("published", ["", "2026-10-27", "2026-10-27T13:00:00", "not-a-date"])
def test_unknown_or_naive_publication_cannot_be_current(published):
    doc = public_document(published=published)
    schema, _ = schema_for([doc])
    reject(event_thesis(doc), schema)
    with pytest.raises(RequiredModelFailed):
        original_validate(event_thesis(doc), [doc])


@pytest.mark.parametrize("explicit_null", [False, True])
def test_search_date_omission_or_null_is_legal_but_self_reference_is_not(explicit_null):
    doc = public_document()
    schema, _ = schema_for([doc])
    value = event_thesis(doc)
    if explicit_null:
        value["claims"][0]["date_source_id"] = None
    validate_schema(value, schema)
    original_validate(value, [doc])
    value["claims"][0]["date_source_id"] = doc.doc_id
    reject(value, schema)
    with pytest.raises(RequiredModelFailed):
        original_validate(value, [doc])


@pytest.mark.parametrize("change", ["none", "same_url", "other_url", "unobserved"])
def test_fetch_dates_only_use_an_observed_same_url_metadata_parent(change):
    search = public_document()
    page = replace(search, doc_id="ds_synthetic_fetch", source="deepseek_fetch", published="",
                   meta={"target_symbols": ["AAA"], "search_source_id": search.doc_id})
    if change == "other_url":
        page = replace(page, url="https://issuer.example/other")
    elif change == "unobserved":
        page = replace(page, meta={"target_symbols": ["AAA"], "search_source_id": "ds_missing"})
    schema, _ = schema_for([search, page])
    value = event_thesis(page)
    value["claims"][0]["date_source_id"] = None if change == "none" else search.doc_id
    if change == "same_url":
        validate_schema(value, schema)
        original_validate(value, [search, page])
    else:
        reject(value, schema)
        with pytest.raises(RequiredModelFailed):
            original_validate(value, [search, page])


@pytest.mark.parametrize("field", ["market", "sector", "chain", "stock", "plan"])
def test_wrong_scope_reference_and_missing_own_market_are_rejected(field):
    value = thesis()
    value["five_part"][field]["evidence_ids"] = [next(row["id"] for row in facts() if row["scope"] == "stock")]
    schema, _ = schema_for()
    reject(value, schema)
    with pytest.raises(RequiredModelFailed):
        original_validate(value)


@pytest.mark.parametrize("change", ["partial", "identity", "origin", "kind", "issuer_name"])
def test_news_sufficiency_keeps_same_source_full_current_identity_intersection(change):
    doc, neighbor = public_document(), public_document("ds_synthetic_neighbor")
    views = [research.source_view(item) for item in (doc, neighbor)]
    value = event_thesis(doc)
    if change == "partial": views[0] = research.source_view(doc, characters=60)
    elif change == "identity": value["issuer_identity_evidence_ids"] = [neighbor.doc_id]
    elif change == "origin": value["claims"][0]["origin_id"] = neighbor.doc_id
    elif change == "kind": value["claims"][0]["evidence_kind"] = "media"
    else: value["issuer_name"] = None
    schema, _ = schema_for([doc, neighbor], shown=views)
    reject(value, schema)
    with pytest.raises(RequiredModelFailed):
        original_validate(value, [doc, neighbor], views)


@pytest.mark.parametrize("status", ["insufficient", "contradictory"])
def test_nonsufficient_status_keeps_explicit_gap_and_does_not_require_news_proof(status):
    value = thesis(strategy="news_event_v2", status=status)
    schema, _ = schema_for()
    validate_schema(value, schema)
    original_validate(value)
    value["gaps"] = []
    reject(value, schema)


@pytest.mark.parametrize("followup,initial_ok", [
    ({"needed": False, "question": None, "fetch_source_id": None}, True),
    ({"needed": True, "question": "SYNTHETIC_TEST question", "fetch_source_id": None}, True),
    ({"needed": True, "question": None, "fetch_source_id": "ds_test_primary"}, True),
    ({"needed": True, "question": None, "fetch_source_id": None}, False),
    ({"needed": False, "question": "SYNTHETIC_TEST", "fetch_source_id": None}, False),
    ({"needed": False, "question": None, "fetch_source_id": "ds_test_primary"}, False),
    ({"needed": True, "question": "site:other.example", "fetch_source_id": None}, False),
    ({"needed": True, "question": '"BBB"', "fetch_source_id": None}, False),
    ({"needed": True, "question": "x" * 41, "fetch_source_id": None}, False),
    ({"needed": True, "question": "x\ny", "fetch_source_id": None}, False),
])
def test_followup_actions_are_explicit_and_final_rounds_cannot_request_more(followup, initial_ok):
    doc = public_document()
    value = thesis()
    value["followup"] = followup
    initial, _ = schema_for([doc])
    if initial_ok: validate_schema(value, initial)
    else: reject(value, initial)
    for phase in ("final", "decision"):
        schema, _ = schema_for([doc], phase=phase)
        if followup == {"needed": False, "question": None, "fetch_source_id": None}:
            validate_schema(value, schema)
        else:
            reject(value, schema)


class CapturingResearch(SuccessfulResearchClient):
    def __init__(self, policy, *, fetch=True):
        super().__init__(fetch=fetch)
        self.execution.policy.budget_policy_id = policy
        self.requests = []

    def complete_json(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return super().complete_json(**kwargs)


def capture_profile(policy, *, symbols=("AAA", "BBB", "CCC", "DDD"), fetch=True):
    import pandas as pd
    ctx = context(watch=symbols)
    frame = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))
    ctx["market_data"] = {"stock_ohlcv": {symbol: frame for symbol in symbols}}
    before = research.canonical(ctx)
    client = CapturingResearch(policy, fetch=fetch)
    result = research.research_candidates(client, ctx, [])
    assert research.canonical(ctx) == before
    ctx["candidate_research"] = result
    answer = {"action": "WAIT", "action_summary": "SYNTHETIC_TEST no live conclusion", "decisions": []}
    for index, symbol in enumerate(symbols):
        row = answer_for({"candidate_research": {**result, "theses": [result["theses"][index]],
            "market_evidence": [next(item for item in result["market_evidence"] if item["symbol"] == symbol)]}})["decisions"][0]
        answer["decisions"].append(row)
    final = FinalWireClient(answer)
    final.execution.policy.budget_policy_id = policy
    final_result = required.decision_v2(final, ctx, semantic_capacity_documents(3), 100000.)
    return {"research_requests": client.requests, "decision_requests": final.requests,
            "research": result, "decision": final_result, "native_calls": client.calls}


def request_digest(value):
    return hashlib.sha256(research.canonical(value).encode()).hexdigest()


@pytest.mark.parametrize("policy", [None, "tiger.budget-policy.v2", "tiger.budget-policy.v3"])
def test_all_legacy_profile_request_and_schema_bytes_equal_exact_b304_goldens(policy):
    golden = json.loads((FIXTURES / "research-request-contract-legacy-b304.json").read_text())
    actual = capture_profile(policy)
    key = "none" if policy is None else policy
    row = golden["profiles"][key]
    for name in ("research_requests", "decision_requests", "research", "decision", "native_calls"):
        assert request_digest(actual[name]) == row[name], (policy, name)
    assert all("request_bindings" not in row["user"] for row in actual["research_requests"] + actual["decision_requests"])


def test_v4_initial_final_decision_keep_every_old_input_and_use_exact_new_schema():
    old, new = capture_profile(None), capture_profile(BUDGET_POLICY_V4)
    assert new["native_calls"] == old["native_calls"]
    assert len(new["research_requests"]) == 4 and len(new["decision_requests"]) == 1
    for original, current in zip(old["research_requests"], new["research_requests"]):
        original_payload = json.loads(original["user"].split("\n", 1)[1])
        payload = json.loads(current["user"].split("\n", 1)[1])
        guide = payload.pop("request_bindings")
        selections = payload.pop("research_quote_selection")
        assert selections["schema"] == research.RESEARCH_QUOTE_SELECTION_V4
        for original_source, selection in zip(original_payload["documents"], selections["sources"]):
            assert selection["source_id"] == original_source["id"]
            assert "".join(part[1] for part in selection["content_parts"]["rows"]) == original_source["content"]
        assert payload == original_payload
        assert guide["phase"] == payload["phase"] and guide["schema"] == research.REQUEST_BINDINGS_V4
        assert current["max_tokens"] == 65536
        request = build_chat_request(**current)
        upper, output = token_upper_bound(request)
        assert upper > len(research.canonical(payload).encode()) and output == 65536
        assert "contains" in research.canonical(current["schema"])
    legacy_wire = json.loads(old["decision_requests"][0]["user"].split("\n", 1)[1])
    current_wire = json.loads(new["decision_requests"][0]["user"].split("\n", 1)[1])
    legacy_wire.pop("assessment")
    current_wire.pop("assessment")
    restored = required.final_payload_wire_v2(current_wire, decode=True)
    assert restored.pop("request_bindings")["phase"] == "decision"
    assert restored == required.final_payload_wire_v2(legacy_wire, decode=True)
    assert new["decision"]["model_output"] == old["decision"]["model_output"]
    assert new["decision"]["model_reference_usage"] == old["decision"]["model_reference_usage"]


def test_v4_empty_research_and_final_decision_remain_valid():
    flow = capture_profile(BUDGET_POLICY_V4, symbols=(), fetch=False)
    assert flow["research"]["theses"] == [] and flow["decision"]["model_output"]["decisions"] == []
    assert flow["native_calls"] == [("chat", "initial")]


def test_v4_binding_preserves_preexisting_source_owned_decision_fields_and_constraints():
    _, guide = schema_for(phase="decision")
    original = schemas.decision_schema_v2(["AAA"], [], [market()["id"]], [row["id"] for row in facts()])
    row = original["properties"]["decisions"]["items"]
    row["properties"]["synthetic_controlled_authority"] = {"const": "withhold"}
    row["required"].append("synthetic_controlled_authority")
    row["allOf"] = [{"properties": {"action": {"const": "watch"}}}]
    before = deepcopy(original)
    bound = schemas.decision_schema_v4(["AAA"], [], [market()["id"]], [item["id"] for item in facts()],
                                       bindings=guide, base_schema=original)
    actual = bound["properties"]["decisions"]["items"]
    assert actual["properties"]["synthetic_controlled_authority"] == {"const": "withhold"}
    assert actual["required"] == row["required"]
    assert actual["allOf"][0] == row["allOf"][0]
    assert original == before


def test_complete_v4_request_frames_fit_the_unchanged_guard_without_dropping_content():
    from trading_system.llm.clean_output import CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1
    from trading_system.llm.request_budget import BudgetGuard
    flow = capture_profile(BUDGET_POLICY_V4)
    guard = BudgetGuard.for_policy(BUDGET_POLICY_V4, clean_output_policy_id=CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1,
                                   clock=lambda: 0.)
    expected = 0
    for index, kwargs in enumerate(flow["research_requests"] + flow["decision_requests"], 1):
        request = build_chat_request(**kwargs)
        upper, output = token_upper_bound(request)
        assert output == 65536
        reservation = guard.reserve("chat", f"{index:032x}", request)
        assert reservation["input_token_upper_bound"] == upper
        assert reservation["output_token_upper_bound"] == output
        expected += upper + output
    snapshot = guard.snapshot()
    assert snapshot["reserved_tokens"] == expected < snapshot["limits"]["max_tokens"]
    assert snapshot["counts"]["chat"] == 5 and snapshot["actual_tokens"] == 0
    assert snapshot["estimated_reserved_usd"] < snapshot["limits"]["max_cost_usd"]


def test_final_partial_current_presentation_does_not_erase_previous_complete_delivery():
    doc = public_document(content="SYNTHETIC_TEST AAA reported orders increased " + "x" * 100)
    plan = research.build_research_plan(context(watch=["AAA"]), ASSESSMENT)
    payload = research.build_research_payload(plan, ["AAA"], [doc], facts(), [market()], phase="final")
    payload["documents"] = [research.source_view(doc, characters=30)]
    payload["preliminary"] = [event_thesis(doc)]
    original = deepcopy(payload)
    bound, schema = research._bound_research_request_v4(payload, plan, [doc], {"AAA": [research.source_view(doc)]})
    assert payload == original and {key: value for key, value in bound.items()
        if key not in {"request_bindings", "research_quote_selection"}} == original
    assert bound["request_bindings"]["targets"][0]["news_current_complete_options"]
    value = event_thesis(doc)
    reference = bound["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]
    value["claims"] = [{"retained_initial_claim_ref": reference["retained_initial_claim_ref"],
                        "statement": value["claims"][0]["statement"]}]
    output = {"coverage_summary": "SYNTHETIC_TEST retained full observation", "theses": [value]}
    validate_schema(output, schema)
    restored, _ = research._resolve_research_quotes_v4(output, bound, schema)
    original_validate(restored["theses"][0], [doc], [research.source_view(doc)])


@pytest.mark.parametrize("change", ["missing_md", "self_date", "invented_quote", "duplicate_claim_id", "cross_symbol_reuse", "unapproved_buy"])
def test_v4_final_literal_and_reuse_keep_all_original_strict_rejection_paths(change):
    ctx, doc = partial_news_context()
    answer = answer_for(ctx, reuse=False)
    row, value = answer["decisions"][0], answer["decisions"][0]["thesis"]
    if change == "missing_md": value["five_part"]["stock"]["evidence_ids"] = [doc.doc_id]
    elif change == "self_date": value["claims"][0]["date_source_id"] = doc.doc_id
    elif change == "invented_quote": value["claims"][0]["quote"] = "SYNTHETIC_TEST"
    elif change == "duplicate_claim_id":
        value["claims"].append({**value["claims"][0], "statement": "SYNTHETIC_TEST other interpretation"})
    elif change == "cross_symbol_reuse": row["thesis"] = {"reuse_thesis": "BBB"}
    else:
        ctx["watchlist"], ctx["picks"] = [ctx["picks"][0]], []
        # This fixture only needs a symbol-bearing candidate for payload construction.
        row["action"], answer["action"] = "buy", "BUY"
    before = research.canonical(ctx)
    client = FinalWireClient(answer)
    client.execution.policy.budget_policy_id = BUDGET_POLICY_V4
    with pytest.raises(RequiredModelFailed):
        required.decision_v2(client, ctx, [], 100000.)
    assert research.canonical(ctx) == before and len(client.requests) == 1


def actual_projection():
    raw = (FIXTURES / "research-full16-fields-b304.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FULL16_SHA256
    return json.loads(raw)


def test_exact_full16_projection_rejects_all_three_independent_failure_classes():
    observed = actual_projection()
    assert observed["actual_failed"] and observed["business_successes_claimed"] == 0
    documents = [RawDocument(row["source_id"], row["source"], "SYNTHETIC_TEST no original text",
        "https://fixture.example/" + row["url_equivalence_sha256"], "SYNTHETIC_TEST placeholder only",
        row["published_at"] or "", 0., {"target_symbols": row["target_symbols"],
            **({"search_source_id": row["search_source_id"]} if row["search_source_id"] is not None else {})})
        for row in observed["documents"]]
    fact_rows = [{"id": identity, "symbol": row["symbol"], "scope": scope}
        for row in observed["theses"] for scope, identities in row["scope_fact_ids"].items() for identity in identities]
    markets = [{"symbol": row["symbol"], "id": row["market_id"]} for row in observed["theses"]]
    symbols = [row["symbol"] for row in observed["theses"]]
    guide = research.build_thesis_binding_guide(symbols=symbols, documents=documents, fact_evidence=fact_rows,
        market_evidence=markets, assessment=observed["assessment"], phase="decision",
        presented_by_symbol={symbol: [research.source_view(doc) for doc in documents] for symbol in symbols})
    schema = schemas.thesis_schema_v4(symbols, [doc.doc_id for doc in documents],
        [row["id"] for row in fact_rows], [row["id"] for row in markets], bindings=guide)
    counts = {"missing_own_md": 0, "invalid_date_reference": 0, "expired_current": 0}
    sources = {row["source_id"]: row for row in guide["sources"]}
    for observed_thesis, target in zip(observed["theses"], schema["allOf"][1]["oneOf"]):
        stock_rule = target["properties"]["five_part"]["properties"]["stock"]["properties"]["evidence_ids"]
        try: validate_schema(observed_thesis["five_part_evidence_ids"]["stock"], stock_rule)
        except LLMUnavailable: counts["missing_own_md"] += 1
        for claim in observed_thesis["claims"]:
            options = sources[claim["source_id"]]["date_options"]
            # The two predicates are checked independently against unchanged
            # observed fields, so an invalid date ID cannot hide stale current.
            date_value = claim["date_source_id"] if claim["date_source_id_present"] else None
            try: validate_schema(date_value, {"enum": [option["date_source_id"] for option in options]})
            except LLMUnavailable: counts["invalid_date_reference"] += 1
            assert options[0]["date_source_id"] is None  # actual sources are all Search
            try: validate_schema(claim["time_role"], {"enum": options[0]["allowed_time_roles"]})
            except LLMUnavailable: counts["expired_current"] += 1
    assert counts == {"missing_own_md": 3, "invalid_date_reference": 46, "expired_current": 26}
    assert len(observed["theses"]) == 10 and sum(len(row["claims"]) for row in observed["theses"]) == 59
    assert hashlib.sha256((FIXTURES / "research-full16-fields-b304.json").read_bytes()).hexdigest() == FULL16_SHA256
