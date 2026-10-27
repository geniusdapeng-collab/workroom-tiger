"""Own-issuer final reuse and opaque identity wire; SYNTHETIC_TEST only."""
from copy import deepcopy
import hashlib
import json

import pytest

from trading_system.llm import required
from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.required_schemas import decision_schema_v2
from trading_system.redline.execution import RequiredModelFailed
from test_short_term_research_v2 import (SemanticStub, partial_news_context,
    semantic_capacity_documents, context as research_context, SuccessfulResearchClient, research_module)


def answer_for(context, *, reuse=True):
    research = context["candidate_research"]
    thesis = research["theses"][0]
    symbol = thesis["symbol"]
    return {"action": "WAIT", "action_summary": "SYNTHETIC_TEST 等待完整条件，不构成实际交易建议。",
        "decisions": [{"symbol": symbol, "action": "watch", "action_summary": "SYNTHETIC_TEST 等待复核。",
            "why_this": "SYNTHETIC_TEST 保留 d0 w0 ds_test_primary 这些正文字符，来源归属由已完成研究约束。",
            "why_now": "SYNTHETIC_TEST 当前证据及数值闸门未同时满足。",
            "entry_condition": "SYNTHETIC_TEST 复核完整计划后重新研究。",
            "invalidation": "SYNTHETIC_TEST 原结构失效即取消。",
            "risk_note": "SYNTHETIC_TEST 原始来源与时效限定仍全部保留。",
            "source_ids": [research["market_evidence"][0]["id"]],
            "thesis": {"reuse_thesis": symbol} if reuse else deepcopy(thesis)}]}


class FinalWireClient(SemanticStub):
    def complete_json(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        payload = json.loads(kwargs["user"].split("\n", 1)[1])
        if "final_wire" in payload:
            identities = [row[1] for row in payload["final_wire"]["identity_index"]["rows"]]
            return required.final_identity_values_v2(deepcopy(self.answer), identities)
        return deepcopy(self.answer)


def test_final_model_boundary_can_select_its_own_completed_issuer_thesis_without_recopy():
    context, _ = partial_news_context()
    original = deepcopy(context["candidate_research"]["theses"][0])
    client = FinalWireClient(answer_for(context))
    output = required.decision_v2(client, context, [], 100000.)
    assert output["model_output"]["decisions"][0]["thesis"] == original
    assert context["action"] == "WAIT" and context["picks"] == []
    assert output["model_reference_usage"] == [{"symbol": "AAA", "field": "thesis",
        "source": "candidate_research.theses", "source_symbol": "AAA"}]
    assert output["model_output"]["decisions"][0]["why_this"] == client.answer["decisions"][0]["why_this"]


def test_final_request_explicitly_preserves_full_original_id_registry():
    context, doc = partial_news_context()
    client = FinalWireClient(answer_for(context, reuse=False))
    required.decision_v2(client, context, [], 100000.)
    payload = json.loads(client.requests[0]["user"].split("\n", 1)[1])
    assert payload["final_wire"]["schema"] == "tiger.final-identity-wire.v1"
    registry = payload["final_wire"]["identity_index"]
    assert registry["columns"] == ["ref", "id"]
    assert doc.doc_id in [row[1] for row in registry["rows"]]
    assert context["candidate_research"]["market_evidence"][0]["id"] in [row[1] for row in registry["rows"]]


def two_issuer_context():
    import pandas as pd
    context = research_context(watch=["AAA", "BBB"])
    frame = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))
    context["market_data"] = {"stock_ohlcv": {symbol: frame for symbol in ("AAA", "BBB")}}
    context["candidate_research"] = research_module().research_candidates(SuccessfulResearchClient(), context, [])
    return context


def two_issuer_answer(context):
    result = answer_for(context)
    research = context["candidate_research"]
    second = deepcopy(result["decisions"][0])
    second.update(symbol="BBB", thesis={"reuse_thesis": "BBB"},
        source_ids=[next(row["id"] for row in research["market_evidence"] if row["symbol"] == "BBB")])
    result["decisions"].append(second)
    return result


@pytest.mark.parametrize("mutation", ["other_issuer", "unresearched", "watch_symbol_mismatch",
    "reference_plus_extra", "incorrect_type", "edited_selected_field", "unapproved_buy"])
def test_invalid_reuse_or_identity_never_becomes_a_model_approved_buy(mutation):
    context = two_issuer_context()
    answer = two_issuer_answer(context)
    row = answer["decisions"][0]
    if mutation == "other_issuer": row["thesis"] = {"reuse_thesis": "BBB"}
    elif mutation == "unresearched": row["thesis"] = {"reuse_thesis": "ZZZ"}
    elif mutation == "watch_symbol_mismatch": row["symbol"] = "ZZZ"
    elif mutation == "reference_plus_extra": row["thesis"]["status"] = "sufficient"
    elif mutation == "incorrect_type": row["thesis"]["reuse_thesis"] = 0
    elif mutation == "edited_selected_field": row["thesis"]["five_part"] = {"stock": {"text": "SYNTHETIC_TEST altered"}}
    else:
        answer["action"] = "BUY"
        row["action"] = "buy"
    before = deepcopy(context)
    client = FinalWireClient(answer)
    with pytest.raises(RequiredModelFailed):
        required.decision_v2(client, context, [], 100000.)
    assert research_module().canonical(context) == research_module().canonical(before)
    assert context["picks"] == [] and context["action"] == "WAIT"
    assert len(client.requests) == 1


def test_final_input_and_schema_roundtrip_retains_all_values_and_parent_sources():
    context = two_issuer_context()
    docs = semantic_capacity_documents(4)
    payload = required.build_decision_payload_v2(context, docs, 100000.)
    before = deepcopy(payload)
    wire = required.final_payload_wire_v2(payload)
    identities = [row[1] for row in wire["final_wire"]["identity_index"]["rows"]]
    assert len(identities) == len(set(identities))
    assert all(doc.raw.doc_id in identities for doc in docs)
    assert required.final_payload_wire_v2(wire, decode=True) == payload == before
    raw_ids = [row["id"] for row in context["candidate_research"]["source_index"]]
    schema = decision_schema_v2(["AAA", "BBB"], raw_ids,
        [row["id"] for row in payload["market_evidence"]], [row["id"] for row in payload["fact_evidence"]])
    original_schema = deepcopy(schema)
    aliased = required.final_identity_schema_v2(schema, identities)
    assert required.final_identity_schema_v2(aliased, identities, decode=True) == schema == original_schema
    assert wire["document_index"]["rows"][0][0] != wire["document_index"]["rows"][1][0]
    assert wire["document_index"]["rows"][0][-2] != wire["document_index"]["rows"][1][-2]


def test_identical_quote_hashes_in_distinct_sources_keep_distinct_parent_identities():
    from dataclasses import replace
    context, _ = partial_news_context()
    docs = semantic_capacity_documents(1)[:2]
    quote = docs[0].claims[0]["quote"]
    docs[1].raw = replace(docs[1].raw, content=quote)
    docs[1].claims[0]["quote"] = quote
    payload = required.build_decision_payload_v2(context, docs, 100000.)
    wire = required.final_payload_wire_v2(payload)
    index = wire["document_index"]
    positions = {name: position for position, name in enumerate(index["columns"])}
    first, second = index["rows"]
    assert first[positions["id"]] != second[positions["id"]]
    one, two = first[positions["claims"]], second[positions["claims"]]
    quote_position = one["columns"].index("quote_ref")
    assert one["rows"][0][quote_position] == two["rows"][0][quote_position]
    originals = [row[1] for row in wire["final_wire"]["identity_index"]["rows"]]
    assert all(doc.raw.doc_id in originals for doc in docs)
    assert required.final_payload_wire_v2(wire, decode=True) == payload


@pytest.mark.parametrize("text", ["w0", "ds_test_primary", "md_AAA_20261026_111111111111111111111111",
    "w0 ds_test_primary remain prose", "w0\n<ds_test_primary>\t😀\u200b"])
def test_final_codec_changes_only_controlled_id_positions_and_no_arbitrary_text(text):
    ids = ["ds_test_primary", "md_AAA_20261026_111111111111111111111111"]
    value = {"source_ids": ids, "why_this": text,
        "claim_key": "ds_test_primary", "claim_id": "ds_test_primary",
        "thesis": {"five_part": {"stock": {"text": text, "evidence_ids": ids}},
                   "claims": [{"source_id": ids[0], "quote": text, "statement": text}]},
        "document_index": {"columns": ["id", "summary_zh"], "rows": [[ids[0], text]]}}
    encoded = required.final_identity_values_v2(value, ids)
    assert encoded["source_ids"] == ["w0", "w1"]
    assert encoded["why_this"] == encoded["thesis"]["five_part"]["stock"]["text"] == text
    assert encoded["claim_key"] == encoded["claim_id"] == "ds_test_primary"
    assert encoded["document_index"]["rows"] == [["w0", text]]
    assert required.final_identity_values_v2(encoded, ids, decode=True) == value


def test_schema_aliasing_never_changes_categorical_values_that_look_like_registered_ids():
    ids = ["technical_flow_v2", "BUY"]
    schema = {"type": "object", "properties": {
        "strategy": {"enum": ["technical_flow_v2", "news_event_v2"]},
        "action": {"enum": ["BUY", "WAIT", "AVOID"]},
        "source_ids": {"type": "array", "items": {"enum": ids}}}}
    wire = required.final_identity_schema_v2(schema, ids)
    assert wire["properties"]["strategy"] == schema["properties"]["strategy"]
    assert wire["properties"]["action"] == schema["properties"]["action"]
    assert wire["properties"]["source_ids"]["items"]["enum"] == ["w0", "w1"]
    assert required.final_identity_schema_v2(wire, ids, decode=True) == schema


@pytest.mark.parametrize("tamper", ["registry_alias", "registry_id", "registry_missing", "registry_extra", "policy"])
def test_final_wire_decoder_refuses_changed_or_ambiguous_original_registry(tamper):
    context, _ = partial_news_context()
    wire = required.final_payload_wire_v2(required.build_decision_payload_v2(context, [], 100000.))
    registry = wire["final_wire"]["identity_index"]
    if tamper == "registry_alias": registry["rows"][0][0] = "w999"
    elif tamper == "registry_id": registry["rows"][0][1] = "ds_invented"
    elif tamper == "registry_missing": registry["rows"].pop()
    elif tamper == "registry_extra": registry["rows"].append(["w999", "ds_invented"])
    else: wire["final_wire"]["schema"] = "tiger.final-identity-wire.unknown"
    with pytest.raises(RequiredModelFailed):
        required.final_payload_wire_v2(wire, decode=True)


@pytest.mark.parametrize("phase", ["daily", "premarket", "intraday"])
def test_complete_43stage_replay_accepts_exact_own_thesis_references(tmp_path, monkeypatch, phase):
    from test_required_proof_v2 import complete_fixture
    from trading_system.llm.required_proof_v2 import verify_required_model_v2

    def reuse(stage, answer):
        if stage == "decision.debate":
            for row in answer["decisions"]:
                row["thesis"] = {"reuse_thesis": row["symbol"]}
        return answer

    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch,
        phase=phase, mutate_answer=reuse)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    references = result["raw"]["debate"]["model_reference_usage"]
    assert references and all(row["symbol"] == row["source_symbol"] for row in references)
    assert denied == []


def test_invalid_final_reference_in_actual_orchestration_never_commits_account(tmp_path, monkeypatch):
    import main
    from test_short_term_core_v2 import upgrade_fixture

    def wrong_reference(stage, answer):
        if stage == "decision.debate":
            answer["action"] = "BUY"
            for row in answer["decisions"]:
                row.update(action="buy", thesis={"reuse_thesis": "ZZZ"})
        return answer

    execution, root, args, _, denied, _ = upgrade_fixture(tmp_path, monkeypatch,
        mutate_answer=wrong_reference)
    before = execution.paths.sim_path.read_bytes()
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == "decision.debate"
    assert execution.summary()[-1]["status"] == "failed"
    assert execution.paths.sim_path.read_bytes() == before
    assert not (root / "receipt.json").exists() and denied == []


def test_legal_buy_with_reused_issuer_thesis_still_passes_all_original_account_authorities(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    import main
    from trading_system.agent_evidence import verify_events
    from trading_system.ledger_io import read_json_strict
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.redline.execution import daily_stage_contract
    from test_root_integration_review_v2 import legal_buy_fixture
    execution, root, args, denied, _ = legal_buy_fixture(tmp_path, monkeypatch)
    original_post = DeepSeekRequiredClient._post_json

    def select_reference(client, path, request):
        response, response_hash = original_post(client, path, request)
        if client.execution._active.spec.name == "decision.debate" and path == "/chat/completions":
            answer = json.loads(response["choices"][0]["message"]["content"])
            for row in answer["decisions"]:
                row["thesis"] = {"reuse_thesis": row["symbol"]}
            response["choices"][0]["message"]["content"] = json.dumps(answer)
            response_hash = hashlib.sha256(json.dumps(response, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return response, response_hash

    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", select_reference)
    source = Path(__file__).resolve().parents[1] / "trading_system"
    destination = root / "source/trading_system"
    destination.mkdir(parents=True)
    for name in ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py"):
        shutil.copy2(source / name, destination / name)
    output = main._daily(args, "yahoo", execution=execution)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    assert output.result.action == "BUY" and [pick.ticker for pick in output.result.picks] == ["NVDA"]
    assert [row["ticker"] for row in output.sim_state["pending"]] == ["NVDA"]
    assert output.result.raw["debate"]["model_reference_usage"]
    result = read_json_strict(output.report_paths["json"])
    admission = {"clock": {"run_date": execution.run_date.isoformat(), "market_timezone": "America/New_York",
        "admitted_at_utc": execution.admitted_at_utc, "deadline_at_utc": "2026-10-20T14:25:00+00:00"}}
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert denied == []


@pytest.mark.parametrize("tamper", ["registry_alias", "registry_original_id", "issuer_text", "selected_reference"])
def test_rehashed_final_registry_text_and_reference_tampering_fails_semantic_replay(tmp_path, monkeypatch, tamper):
    from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, file_ref,
        read_json, SNAPSHOT_SCHEMA, verify_events)
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.redline.execution import daily_stage_contract
    from test_required_proof_v2 import complete_fixture, h, rewrite_stage

    def reuse(stage, answer):
        if stage == "decision.debate":
            for row in answer["decisions"]:
                row["thesis"] = {"reuse_thesis": row["symbol"]}
        return answer

    _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch, mutate_answer=reuse)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]

    def change(_, output):
        call = output["io_calls"][0]
        request_blob = read_json(root, call["input_ref"]["path"])
        proof_blob = read_json(root, call["output_ref"]["path"])
        request, proof = request_blob["value"], proof_blob["value"]
        if tamper == "selected_reference":
            proof["parsed"]["decisions"][0]["thesis"] = {"reuse_thesis": "ZZZ"}
            proof["response"]["choices"][0]["message"]["content"] = json.dumps(
                proof["parsed"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            proof["response_sha256"] = h(proof["response"])
        else:
            task, remainder = request["request"]["messages"][1]["content"].split("\n", 1)
            serialized, framing = remainder.split("\nJSON schema:\n", 1)
            payload = json.loads(serialized)
            if tamper == "registry_alias": payload["final_wire"]["identity_index"]["rows"][0][0] = "w999"
            elif tamper == "registry_original_id": payload["final_wire"]["identity_index"]["rows"][0][1] = "ds_invented"
            else: payload["candidate_research"]["theses"][0]["five_part"]["stock"]["text"] += " SYNTHETIC_TEST invented text"
            if tamper in {"registry_original_id", "issuer_text"}:
                identities = [row[1] for row in payload["final_wire"]["identity_index"]["rows"]]
                forged_original = required.final_identity_values_v2({key: value for key, value in payload.items()
                    if key not in {"final_wire", "assessment"}}, identities, decode=True)
                payload["final_wire"]["source_payload_sha256"] = hashlib.sha256(
                    research_module().canonical(forged_original).encode()).hexdigest()
                assert required.final_payload_wire_v2({key: value for key, value in payload.items()
                    if key != "assessment"}, decode=True) == forged_original
            request["request"]["messages"][1]["content"] = task + "\n" + json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\nJSON schema:\n" + framing
            request["request_sha256"] = h(request["request"])
            proof["request_sha256"] = request["request_sha256"]
            atomic_json(root, call["input_ref"]["path"], request_blob)
            call["input_ref"] = file_ref(root, call["input_ref"]["path"], SNAPSHOT_SCHEMA)
        atomic_json(root, call["output_ref"]["path"], proof_blob)
        call["output_ref"] = file_ref(root, call["output_ref"]["path"], SNAPSHOT_SCHEMA)

    rewrite_stage(root, "decision.debate", change)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid):
        verify_required_model_v2(root, result, admission)
    assert denied == []
