"""Offline closed-slots policy and full sealed replay; SYNTHETIC_TEST only.

These fixtures run the actual application and transport-proof code against an
explicit fake HTTP boundary. They cannot supply a model or market receipt.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import json
import shutil
import uuid

import pytest

from trading_system.agent_evidence import (EvidenceInvalid, atomic_json, digest,
    json_bytes, normalize, read_bytes, read_json, verify_events)
from trading_system.agent_runtime import (SOURCE_ROOT, freeze_source,
    policy_manifest, source_tree_hash, verify_source)
from trading_system.llm.request_budget import BUDGET_POLICY_V3
from trading_system.redline.execution import (ExecutionContractError,
    ExecutionPolicy, PUBLIC_PROVIDER_NAMES, UPGRADE_MODEL_STAGES,
    UPGRADE_POLICIES, daily_stage_contract)

RESPONSES_POLICY = "tiger.clean-output.responses-slots.v1"
CODEC = "tiger.clean-slots-wire.v1"


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_explicit_historical_admission_selects_exact_slots_policy_codec_and_v3_budget(profile):
    from trading_system.llm.clean_output import CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1
    policy = policy_manifest(profile, clean_output_policy_id=RESPONSES_POLICY)
    assert CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1 == RESPONSES_POLICY
    assert policy["execution"]["clean_output_policy_id"] == RESPONSES_POLICY
    assert policy["clean_output_policy_id"] == RESPONSES_POLICY
    assert policy["clean_output_codec_id"] == CODEC
    assert "clean_output_codec_id" not in policy["execution"]
    assert policy["execution"]["budget_policy_id"] == BUDGET_POLICY_V3
    assert policy["request_budget"]["max_tokens"] == 600000
    assert policy["request_budget"]["max_cost_usd"] == 1.


@pytest.mark.parametrize("policy,budget,selector", [
    ("tiger.public-paper-policy.v1", BUDGET_POLICY_V3, RESPONSES_POLICY),
    (next(iter(UPGRADE_POLICIES.values()))[0], None, RESPONSES_POLICY),
    (next(iter(UPGRADE_POLICIES.values()))[0], BUDGET_POLICY_V3, RESPONSES_POLICY + ".unregistered"),
    (next(iter(UPGRADE_POLICIES.values()))[0], BUDGET_POLICY_V3, False),
])
def test_slots_selector_requires_exact_trusted_upgrade_and_budget(policy, budget, selector):
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy(policy, model_mode="enabled", search_source_names=("deepseek_search",),
                        budget_policy_id=budget, clean_output_policy_id=selector)


def original_keyed_schema(inputs):
    """Rebuild the original contract from the full synthetic request inputs."""
    from trading_system import config
    from trading_system.llm.required import clean_keyed_schema_v2
    from trading_system.llm.required_schemas import clean_schema_v2
    from trading_system.tech_chain.universe import TECH_SUBCHAINS
    registry = inputs["all_source_index"]
    ids = [row[1] for row in registry["rows"]]
    by_reference = {row[0]: row[1] for row in registry["rows"]}
    table = inputs["documents"]
    docs = [dict(zip(table["columns"], row)) for row in table["rows"]]
    quotes = {by_reference[row["source_ref"]]: [part[0]
        for part in row["content_parts"]["rows"] if part[2] is True] for row in docs}
    semantic = clean_schema_v2(list(quotes), list(TECH_SUBCHAINS), config.SECTOR_ETFS,
        quote_part_ids=quotes, all_doc_ids=ids)
    return clean_keyed_schema_v2(semantic, ids)


def install_responses_fixture(monkeypatch, *, mutate_response=None, empty_sources=False):
    """Opt the synthetic fixture into the exact current trusted protocol."""
    import test_short_term_core_v2 as core
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.providers.stooq import StooqProvider

    monkeypatch.setattr(StooqProvider, "ohlcv_batch", lambda self, symbols, days=400: {})
    monkeypatch.setattr(StooqProvider, "quote", lambda self, symbol: None)
    for module_name, class_name in (("official", "OfficialMacroProvider"),
            ("tencent", "TencentProvider"), ("sina", "SinaProvider"), ("eastmoney", "EastMoneyProvider")):
        cls = getattr(importlib.import_module("trading_system.providers." + module_name), class_name)
        monkeypatch.setattr(cls, "available", staticmethod(lambda: False))
    original_policy, original_upgrade = core.ExecutionPolicy, core.upgrade_fixture
    monkeypatch.setattr(core, "ExecutionPolicy", lambda *args, **kwargs: original_policy(*args,
        **{**kwargs, "provider_names": PUBLIC_PROVIDER_NAMES, "budget_policy_id": BUDGET_POLICY_V3,
           "clean_output_policy_id": RESPONSES_POLICY}))

    def upgrade(*args, **kwargs):
        returned = original_upgrade(*args, **kwargs)
        execution, _, _, _, _, attempts = returned
        legacy_post = DeepSeekRequiredClient._post_json
        native_seen = [0]

        def post(client, path, request):
            if path != "/responses":
                response, transport_hash = legacy_post(client, path, request)
                if path == "/v1/tools/search":
                    if empty_sources:
                        response["results"] = []
                        return response, core._hash(response)
                    native_seen[0] += 1
                    if native_seen[0] == 1 and response["results"]:
                        response["results"][0]["content"] += " SYNTHETIC_TEST source-bound extra text." * 20
                        transport_hash = core._hash(response)
                return response, transport_hash
            stage = client.execution._active.spec.name
            assert stage == "clean.llm_semantic"
            attempts.append((stage, path))
            client._remote_requests += 1
            schema = request["text"]["format"]["schema"]
            inputs = json.loads(request["input"].split("\n", 1)[1])
            from trading_system.llm.required import (clean_slots_payload_v1,
                clean_keyed_payload_v2, clean_slots_output_v1)
            from trading_system.llm.clean_output import clean_slots_schema_v1
            keyed_schema = original_keyed_schema(inputs)
            assert schema == clean_slots_schema_v1(keyed_schema)
            wire = {key: value for key, value in inputs.items() if key != "assessment"}
            payload = clean_slots_payload_v1(wire, keyed_schema, decode=True)
            answer = core._answer(stage, {"assessment": inputs["assessment"],
                **clean_keyed_payload_v2(payload)}, keyed_schema)
            answer = clean_slots_output_v1(answer, keyed_schema)
            instant = datetime.fromisoformat(execution.admitted_at_utc) + timedelta(minutes=5)
            response = {"id": "resp-SYNTHETIC_TEST-" + uuid.uuid4().hex,
                "object": "response", "created_at": int(instant.timestamp()), "status": "completed",
                "model": "deepseek-flash", "output": [{"type": "message",
                    "id": "msg-SYNTHETIC_TEST-" + uuid.uuid4().hex, "status": "completed",
                    "role": "assistant", "content": [{"type": "output_text",
                        "text": json.dumps(answer, ensure_ascii=False)}]}],
                "usage": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}}
            if mutate_response is not None:
                response = mutate_response(response)
            return response, core._hash(response)

        monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
        return returned

    monkeypatch.setattr(core, "upgrade_fixture", upgrade)


def seal_fixture(root, admission, phase):
    profile = next(key for key, row in UPGRADE_POLICIES.items() if row[1] == phase)
    policy = policy_manifest(profile, clean_output_policy_id=RESPONSES_POLICY)
    atomic_json(root, "policy.json", policy)
    admission["policy_sha256"] = digest(json_bytes(policy))
    shutil.rmtree(root / "source")
    freeze_source(root, SOURCE_ROOT)
    admission["source_sha256"] = digest(read_bytes(root, "source-manifest.json"))
    return admission


def responses_fixture(tmp_path, monkeypatch, *, phase="daily", empty_sources=False, **options):
    from test_required_proof_v2 import complete_fixture
    install_responses_fixture(monkeypatch, empty_sources=empty_sources)
    execution, root, result, admission, denied, attempts = complete_fixture(
        tmp_path, monkeypatch, phase=phase, **options)
    seal_fixture(root, admission, phase)
    assert denied == []
    return execution, root, result, admission, attempts


def stage_children(root, stage):
    from test_clean_strict_policy_replay_v2 import stage_children as read_stage_children
    return read_stage_children(root, stage)


@pytest.mark.parametrize("phase", ["daily", "premarket", "intraday"])
def test_all43_stages_replay_native_slots_and_original_other_semantic_calls(tmp_path, monkeypatch, phase):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    execution, root, result, admission, attempts = responses_fixture(tmp_path, monkeypatch, phase=phase)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    execution.assert_complete()
    clean = stage_children(root, "clean.llm_semantic")
    assert clean and len(clean) == len([row for row in attempts if row[1] == "/responses"])
    for row in clean:
        request, proof = row["request"], row["proof"]
        assert request["schema"] == "tiger.deepseek-request.v3"
        assert request["endpoint"] == "https://api.deepseek.com/responses"
        from trading_system.llm.clean_output import clean_slots_schema_v1
        inputs = json.loads(request["request"]["input"].split("\n", 1)[1])
        assert request["output_schema"] == original_keyed_schema(inputs)
        assert inputs["clean_wire"]["schema"] == CODEC
        assert "format_example" not in inputs["clean_wire"]
        assert "format_example_scope" not in inputs["clean_wire"]
        assert "output_slots" in inputs["clean_wire"]
        assert request["request"]["text"]["format"]["schema"] == clean_slots_schema_v1(request["output_schema"])
        assert "allOf" in request["output_schema"]["properties"]["d"]
        assert "allOf" not in request["request"]["text"]["format"]["schema"]["properties"]["d"]
        assert proof["output_policy_id"] == request["output_policy_id"] == RESPONSES_POLICY
        assert proof["parsed"] == json.loads(proof["response"]["output"][0]["content"][0]["text"])
        assert request["schema_sha256"] == digest(json.dumps(request["output_schema"],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        prepaid = request["budget_reservation"]
        assert prepaid["input_token_upper_bound"] == len(json.dumps(request["request"],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")) + 4096 + 128
        assert prepaid["output_token_upper_bound"] == 16384
        for document in proof["parsed"]["d"].values():
            for field in ("t", "e", "h", "f", "c"):
                assert isinstance(document[field], dict) and "s0" in document[field]
        assert proof["schema"] == "tiger.deepseek-call.v3"
        assert proof["response_status"] == "completed" and proof["response"]["status"] == "completed"
        assert proof["native_usage"] == {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}
        assert proof["usage"] == {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}
        assert proof["output_message_id"] == proof["response"]["output"][0]["id"]
        assert not {"finish_reason", "system_fingerprint", "output_tool_call_id"}.intersection(proof)
    for stage in UPGRADE_MODEL_STAGES[1:]:
        chats = [row for row in stage_children(root, stage) if row["method"] == "model.deepseek.chat"]
        assert chats and all(row["request"]["schema"] == "tiger.deepseek-request.v1" for row in chats)
        assert all(row["proof"]["finish_reason"] == "stop" for row in chats)
        assert all(row["request"]["request"]["thinking"] == {"type": "disabled"}
                   and "reasoning_effort" not in row["request"]["request"] for row in chats)
    budget = result["raw"]["model"]["budget"]
    assert result["raw"]["model"]["clean_output_policy_id"] == RESPONSES_POLICY
    assert result["raw"]["model"]["clean_output_codec_id"] == CODEC
    assert budget["limits"]["max_tokens"] == 600000 and budget["reserved_tokens"] == 0
    assert budget["limits"]["max_chat_calls"] == 40 and budget["limits"]["max_remote_requests"] == 120


@pytest.mark.parametrize("scenario", ["empty_targets", "empty_sources", "holding_daily", "holding_premarket"])
def test_slots_replay_preserves_empty_and_current_holding_context(tmp_path, monkeypatch, scenario):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    options = {"held": scenario.startswith("holding"), "empty_candidates": scenario == "empty_targets"}
    if scenario == "empty_sources":
        options.update(empty_sources=True, empty_candidates=True)
    if scenario == "holding_premarket":
        options.update(phase="premarket", admitted="2026-10-20T13:00:00+00:00",
                       ready="2026-10-20T13:06:00+00:00")
    _, root, result, admission, _ = responses_fixture(tmp_path, monkeypatch, **options)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    if scenario in {"empty_targets", "empty_sources"}:
        assert result["raw"]["candidate_research"]["theses"] == []
        assert result["raw"]["candidate_research"]["plan"]["targets"] == []
        if scenario == "empty_sources":
            assert stage_children(root, "clean.llm_semantic")[0]["proof"]["parsed"] == {"d": {}}
    else:
        assert "AAA" in result["raw"]["candidate_research"]["coverage"]["selected_symbols"]
        assert result["raw"]["portfolio_snapshot"]["nav"] != 100000.
        if scenario == "holding_premarket":
            assert result["raw"]["daily_bars_cutoff"] == "2026-10-19"


def test_slots_replay_preserves_canonical_legal_buy_and_account_pending_plan(tmp_path, monkeypatch):
    import main
    from test_root_integration_review_v2 import legal_buy_fixture
    from trading_system.ledger_io import read_json_strict
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    install_responses_fixture(monkeypatch)
    execution, root, args, denied, attempts = legal_buy_fixture(tmp_path, monkeypatch)
    destination = root / "source/trading_system"
    destination.mkdir(parents=True)
    for name in ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py"):
        shutil.copy2(SOURCE_ROOT / "trading_system" / name, destination / name)
    output = main._daily(args, "yahoo", execution=execution)
    result = read_json_strict(output.report_paths["json"])
    admission = {"clock": {"run_date": execution.run_date.isoformat(),
        "market_timezone": "America/New_York", "admitted_at_utc": execution.admitted_at_utc,
        "deadline_at_utc": "2026-10-20T14:25:00+00:00"}}
    seal_fixture(root, admission, "intraday")
    assert output.result.action == "BUY"
    assert [pick.ticker for pick in output.result.picks] == ["NVDA"]
    assert [row["ticker"] for row in output.sim_state["pending"]] == ["NVDA"]
    opportunity = next(row for row in result["raw"]["opportunity_lifecycle"]["opportunities"]
                       if row["ticker"] == "NVDA")
    assert opportunity["state"] == "INTENT_PENDING"
    assert opportunity["account_plan_id"] == output.sim_state["pending"][0]["plan_id"]
    assert result["raw"]["opportunity_lifecycle"]["intents"] == []
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert any(path == "/responses" for _, path in attempts)
    assert denied == []


@pytest.mark.parametrize("tamper", ["selector_missing", "selector_none", "selector_unknown", "pipeline_policy",
    "coherent_policy_downgrade", "coherent_old_responses", "codec_missing", "codec_unknown", "execution_codec", "missing_source_hash", "source_hash", "missing_policy", "endpoint",
    "instructions", "input", "native_schema", "original_schema", "reasoning", "max_output", "tools", "response_status",
    "message_status", "message_id", "created_at", "native_usage", "mapped_usage", "output_text",
    "extra_message", "foreign_q", "local_bound", "invented_finish", "response_schema",
    "duplicate_response_id", "duplicate_message_id", "budget_counts", "budget_limit", "reservation",
    "summary_policy", "summary_codec",
    "slot_hole", "slot_missing", "slot_extra", "slot_duplicate", "slot_overflow", "slot_wrong_type"])
def test_rehashed_responses_contract_mutations_fail_independent_replay(tmp_path, monkeypatch, tamper):
    from test_required_proof_v2 import rewrite_stage, rewrite_child, h
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    _, root, result, admission, _ = responses_fixture(tmp_path, monkeypatch)
    if tamper.startswith("selector_"):
        def mutate(inp, _out):
            selected = inp["policy"]["fields"]
            if tamper == "selector_missing": selected.pop("clean_output_policy_id")
            else: selected["clean_output_policy_id"] = None if tamper == "selector_none" else "unregistered"
        rewrite_stage(root, "daily.preflight", mutate)
    elif tamper == "pipeline_policy":
        rewrite_stage(root, "pipeline.prepare", lambda inp, out:
            inp["policy"]["fields"].pop("clean_output_policy_id"))
    elif tamper in {"codec_missing", "codec_unknown", "execution_codec"}:
        policy = read_json(root, "policy.json")
        if tamper == "codec_missing": policy.pop("clean_output_codec_id")
        elif tamper == "codec_unknown": policy["clean_output_codec_id"] = CODEC + ".unregistered"
        else: policy["execution"]["clean_output_codec_id"] = CODEC
        atomic_json(root, "policy.json", policy)
        admission["policy_sha256"] = digest(json_bytes(policy))
    elif tamper == "coherent_old_responses":
        old = "tiger.clean-output.responses-schema.v1"
        policy = read_json(root, "policy.json")
        policy["execution"]["clean_output_policy_id"] = old
        policy["clean_output_policy_id"] = old
        policy.pop("clean_output_codec_id")
        atomic_json(root, "policy.json", policy)
        admission["policy_sha256"] = digest(json_bytes(policy))
        for stage in ("daily.preflight", "pipeline.prepare"):
            rewrite_stage(root, stage, lambda inp, out:
                inp["policy"]["fields"].update(clean_output_policy_id=old))
    elif tamper == "coherent_policy_downgrade":
        policy = read_json(root, "policy.json")
        policy["execution"].pop("clean_output_policy_id")
        policy.pop("clean_output_policy_id")
        policy.pop("clean_output_codec_id")
        atomic_json(root, "policy.json", policy)
        admission["policy_sha256"] = digest(json_bytes(policy))
        for stage in ("daily.preflight", "pipeline.prepare"):
            rewrite_stage(root, stage, lambda inp, out:
                inp["policy"]["fields"].pop("clean_output_policy_id"))
    elif tamper == "missing_source_hash": admission.pop("source_sha256")
    elif tamper == "source_hash": admission["source_sha256"] = "0" * 64
    elif tamper == "missing_policy": (root / "policy.json").unlink()
    elif tamper == "summary_policy": result["raw"]["model"]["clean_output_policy_id"] = "tiger.clean-output.responses-schema.v1"
    elif tamper == "summary_codec": result["raw"]["model"]["clean_output_codec_id"] = CODEC + ".unregistered"
    else:
        changed_request = {"endpoint", "instructions", "input", "native_schema", "original_schema", "reasoning", "max_output", "tools", "reservation"}
        def mutate(_inp, out):
            call = out["io_calls"][1 if tamper.startswith("duplicate_") else 0]
            first_proof = read_json(root, out["io_calls"][0]["output_ref"]["path"])["value"]
            def request(value):
                body = value["request"]
                if tamper == "endpoint": value["endpoint"] = "https://api.deepseek.com/chat/completions"
                elif tamper == "instructions": body["instructions"] += " SYNTHETIC_TEST unbound"
                elif tamper == "input": body["input"] = "SYNTHETIC_TEST omitted original documents"
                elif tamper == "native_schema": next(iter(body["text"]["format"]["schema"]["properties"]["d"]["properties"].values()))["properties"]["z"].pop("minLength")
                elif tamper == "original_schema":
                    value["output_schema"]["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"]["z"].pop("minLength")
                    value["schema_sha256"] = h(value["output_schema"])
                elif tamper == "reasoning": body["reasoning"] = {"effort": "high"}
                elif tamper == "max_output": body["max_output_tokens"] = 32768
                elif tamper == "tools": body["tools"] = [{"type": "web_search"}]
                elif tamper == "reservation": value["budget_reservation"]["input_token_upper_bound"] += 1
                value["request_sha256"] = h(body)
            def proof(value):
                response = value["response"]
                message = response["output"][0]
                if tamper in changed_request:
                    child = read_json(root, call["input_ref"]["path"])["value"]
                    value["request_sha256"] = child["request_sha256"]
                    if tamper == "original_schema": value["schema_sha256"] = child["schema_sha256"]
                elif tamper == "response_status": value["response_status"] = response["status"] = "incomplete"
                elif tamper == "message_status": message["status"] = "in_progress"
                elif tamper == "message_id": value["output_message_id"] = "msg-unbound-SYNTHETIC_TEST"
                elif tamper == "created_at": value["created_at"] = response["created_at"] = False
                elif tamper == "native_usage": value["native_usage"]["input_tokens"] += 1
                elif tamper == "mapped_usage": value["usage"]["prompt_tokens"] += 1
                elif tamper == "output_text": message["content"][0]["text"] = '{"d":{}}'
                elif tamper == "extra_message": response["output"].append(deepcopy(message))
                elif tamper == "foreign_q":
                    source = read_json(root, call["input_ref"]["path"])["value"]
                    bindings = source["output_schema"]["properties"]["d"]["allOf"][1]["properties"]
                    owned = {ref: set(binding["properties"]["c"]["items"]["properties"]["q"].get("enum", []))
                             for ref, binding in bindings.items()}
                    all_parts = set().union(*owned.values())
                    reference = next(ref for ref, doc in value["parsed"]["d"].items()
                                     if doc["c"]["s0"] is not None and all_parts - owned[ref])
                    value["parsed"]["d"][reference]["c"]["s0"]["q"] = sorted(all_parts - owned[reference])[0]
                    message["content"][0]["text"] = json.dumps(value["parsed"], sort_keys=True, separators=(",", ":"))
                elif tamper.startswith("slot_"):
                    slots = next(iter(value["parsed"]["d"].values()))["t"]
                    if tamper == "slot_hole": slots.update(s0=None, s1="AAA")
                    elif tamper == "slot_missing": slots.pop("s7")
                    elif tamper == "slot_extra": slots["unknown"] = None
                    elif tamper == "slot_duplicate": slots.update(s0="AAA", s1="AAA")
                    elif tamper == "slot_overflow": slots["s8"] = "AAA"
                    else: slots["s0"] = 17
                    message["content"][0]["text"] = json.dumps(value["parsed"], sort_keys=True, separators=(",", ":"))
                elif tamper == "local_bound":
                    next(iter(value["parsed"]["d"].values()))["z"] = ""
                    message["content"][0]["text"] = json.dumps(value["parsed"], sort_keys=True, separators=(",", ":"))
                elif tamper == "invented_finish": value["finish_reason"] = "stop"
                elif tamper == "response_schema": value["schema"] = "tiger.deepseek-call.v2"
                elif tamper == "duplicate_response_id": value["response_id"] = response["id"] = first_proof["response_id"]
                elif tamper == "duplicate_message_id": value["output_message_id"] = message["id"] = first_proof["output_message_id"]
                elif tamper == "budget_counts": value["budget_after"]["counts"]["chat"] = 0
                elif tamper == "budget_limit": value["budget_after"]["limits"]["max_tokens"] = 900000
                value["response_sha256"] = h(response)
            if tamper in changed_request: rewrite_child(root, call, "input", request)
            rewrite_child(root, call, "output", proof)
        rewrite_stage(root, "clean.llm_semantic", mutate)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid):
        verify_required_model_v2(root, result, admission)


def test_slots_policy_cannot_rehash_full_source_closure_down_to_seven_files(tmp_path, monkeypatch):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.source_contract_v1 import REQUIRED_SOURCE_PATHS, STRICT_SOURCE_CONTRACT_V1
    _, root, result, admission, _ = responses_fixture(tmp_path, monkeypatch)
    manifest = read_json(root, "source-manifest.json")
    assert len(REQUIRED_SOURCE_PATHS) == 113
    assert manifest["source_contract_id"] == STRICT_SOURCE_CONTRACT_V1
    retained = {"trading_system/" + name for name in (
        "execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py")}
    for ref in manifest["files"]:
        if ref["path"] not in retained:
            (root / "source" / ref["path"]).unlink()
    manifest["files"] = [ref for ref in manifest["files"] if ref["path"] in retained]
    manifest["tree_sha256"] = source_tree_hash(manifest["files"])
    atomic_json(root, "source-manifest.json", manifest)
    admission["source_sha256"] = digest(read_bytes(root, "source-manifest.json"))
    assert len(manifest["files"]) == 7
    with pytest.raises(EvidenceInvalid): verify_source(root)
    with pytest.raises(EvidenceInvalid): verify_required_model_v2(root, result, admission)


@pytest.mark.parametrize("override", ["clean_output_policy_id", "clean_output_codec_id"])
def test_slots_actual_admission_does_not_accept_selector_as_request_parameter(tmp_path, override):
    from trading_system.agent_runtime import _calendar_input, frozen_clock, validate_admission
    from trading_system.agent_evidence import atomic_bytes
    root = tmp_path / ("run-" + "a" * 32)
    root.mkdir()
    (root / "work").mkdir()
    profile = next(iter(UPGRADE_POLICIES))
    policy = policy_manifest(profile, clean_output_policy_id=RESPONSES_POLICY)
    request = {"schema_version": "tiger.agent-request/v1", "profile": profile,
        "paper_account": "public-usd", "idempotency_key": "SYNTHETIC_TEST-responses-admission"}
    atomic_json(root, "policy.json", policy)
    freeze_source(root, SOURCE_ROOT)
    atomic_json(root, "request.json", request)
    content, calendar = _calendar_input(root)
    atomic_json(root, "calendar-input.json", calendar)
    atomic_bytes(root, calendar["work_path"], content)
    admission = {"schema": "tiger.admission.v1", "run_id": root.name,
        "clock": frozen_clock(datetime.fromisoformat("2026-10-27T14:00:00+00:00")), "parent": None,
        "request_sha256": digest(json_bytes(request)), "policy_sha256": digest(json_bytes(policy)),
        "source_sha256": digest(read_bytes(root, "source-manifest.json"))}
    atomic_json(root, "admission.json", admission)
    assert normalize(validate_admission(root)[1]) == normalize(policy)
    request[override] = RESPONSES_POLICY if override == "clean_output_policy_id" else CODEC
    atomic_json(root, "request.json", request)
    admission["request_sha256"] = digest(json_bytes(request))
    atomic_json(root, "admission.json", admission)
    with pytest.raises(EvidenceInvalid): validate_admission(root)


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
@pytest.mark.parametrize("selector", [None, "tiger.clean-output.strict-function.v1",
    "tiger.clean-output.responses-schema.v1"])
def test_historical_policies_have_no_new_codec_field(profile, selector):
    value = policy_manifest(profile, clean_output_policy_id=selector)
    assert "clean_output_codec_id" not in value
    assert "clean_output_codec_id" not in value["execution"]
    assert value.get("clean_output_policy_id") == selector


def test_execution_codec_cannot_be_independently_overridden():
    with pytest.raises(TypeError):
        ExecutionPolicy(next(iter(UPGRADE_POLICIES.values()))[0], model_mode="enabled",
            search_source_names=("deepseek_search",), budget_policy_id=BUDGET_POLICY_V3,
            clean_output_policy_id=RESPONSES_POLICY, clean_output_codec_id=CODEC)


@pytest.mark.parametrize("selector", [None, "tiger.clean-output.strict-function.v1",
    "tiger.clean-output.responses-schema.v1"])
def test_historical_full43_summaries_retain_absent_codec_and_selector(tmp_path, monkeypatch, selector):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    if selector is None:
        from test_required_proof_v2 import complete_fixture
        _, root, result, admission, denied, _ = complete_fixture(tmp_path, monkeypatch)
        assert denied == []
    elif selector == "tiger.clean-output.strict-function.v1":
        from test_clean_strict_policy_replay_v2 import strict_fixture
        _, root, result, admission, _ = strict_fixture(tmp_path, monkeypatch)
    else:
        from test_clean_responses_policy_replay_v1 import responses_fixture as historical_fixture
        _, root, result, admission, _ = historical_fixture(tmp_path, monkeypatch)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert "clean_output_policy_id" not in result["raw"]["model"]
    assert "clean_output_codec_id" not in result["raw"]["model"]


@pytest.mark.parametrize("tamper", [None, "unsafe_provider_text", "accounting_method", "missing_diagnostic",
    "accounting_selector", "invented_finish_reason"])
def test_event_reader_revalidates_closed_responses_failure_diagnostic_and_accounting(tmp_path, tamper):
    from test_agent_evidence import complete, rewrite_first_io
    execution = complete(tmp_path)
    accounting = {"schema": "tiger.model-call-accounting.v4", "budget_policy_id": BUDGET_POLICY_V3,
        "output_policy_id": RESPONSES_POLICY, "call_id": "a" * 32, "request_sha256": "b" * 64,
        "kind": "chat", "status": "unknown", "usage": None, "native_usage": None,
        "response_status": "unknown", "counts": {"chat": 1, "search": 14, "fetch": 0},
        "actual_tokens": 0, "reserved_tokens": 20000, "estimated_charged_usd": .0105,
        "estimated_reserved_usd": .024, "frozen": True}
    payload = {"error_type": "RequiredModelUnavailable", "message": "External call failed",
        "outcome_observed": True, "diagnostic": {"schema": "tiger.model-failure.v2", "provider": "deepseek",
            "code": "HTTP_ERROR", "http_status": 400, "provider_error": {
                "schema": "tiger.responses-provider-error.v1", "category": "schema", "body_state": "parsed"}},
        "accounting": accounting}
    if tamper == "unsafe_provider_text": payload["diagnostic"]["provider_error"]["message"] = "SYNTHETIC_TEST rejected private text"
    elif tamper == "accounting_method": accounting.update(kind="fetch", counts={"chat": 1, "search": 14, "fetch": 1})
    elif tamper == "missing_diagnostic": payload.pop("diagnostic")
    elif tamper == "accounting_selector": accounting["output_policy_id"] = RESPONSES_POLICY + ".unregistered"
    elif tamper == "invented_finish_reason": accounting["finish_reason"] = "stop"
    rewrite_first_io(tmp_path, lambda call: call.update(method="model.deepseek.chat"), payload, use_error=True)
    if tamper is None:
        assert verify_events(tmp_path, execution.run_id, daily_stage_contract())["stage_count"] == 39
    else:
        with pytest.raises(EvidenceInvalid):
            verify_events(tmp_path, execution.run_id, daily_stage_contract())
