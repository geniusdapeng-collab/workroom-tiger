"""Offline strict-clean policy and sealed wire replay, SYNTHETIC_TEST only."""
from copy import deepcopy
import hashlib

import pytest

from trading_system.agent_evidence import EvidenceInvalid, json_bytes, normalize
from trading_system.agent_runtime import policy_manifest
from trading_system.llm.clean_output import CLEAN_OUTPUT_POLICY_V1
from trading_system.llm.request_budget import BUDGET_POLICY_V3
from trading_system.redline.execution import ExecutionContractError, ExecutionPolicy, UPGRADE_POLICIES


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_new_admission_selects_closed_strict_clean_policy(profile):
    value = policy_manifest(profile, clean_output_policy_id=CLEAN_OUTPUT_POLICY_V1)
    assert value["execution"]["clean_output_policy_id"] == CLEAN_OUTPUT_POLICY_V1
    assert value["execution"]["budget_policy_id"] == BUDGET_POLICY_V3
    assert value["clean_output_policy_id"] == CLEAN_OUTPUT_POLICY_V1


def test_absent_clean_selector_keeps_historical_five_and_six_field_serialization():
    old = ExecutionPolicy("tiger.public-paper-policy.v1")
    assert len(normalize(old)["fields"]) == 5
    profile = next(iter(UPGRADE_POLICIES))
    old_v3 = policy_manifest(profile, budget_policy_id=BUDGET_POLICY_V3,
                             clean_output_policy_id=None)
    assert len(old_v3["execution"]) == 6
    assert "clean_output_policy_id" not in old_v3
    original = policy_manifest(profile, budget_policy_id=None, thinking_mode=None)
    assert len(original["execution"]) == 5
    assert hashlib.sha256(json_bytes(original)).hexdigest() == "010c6acf5d117c3f412a27d4d2d4bdc4b6d8b79d5d9aee175ad6ed46e3af9386"


@pytest.mark.parametrize("profile,budget,identity", [
    ("tiger.public-paper-policy.v1", BUDGET_POLICY_V3, CLEAN_OUTPUT_POLICY_V1),
    (next(iter(UPGRADE_POLICIES.values()))[0], None, CLEAN_OUTPUT_POLICY_V1),
    (next(iter(UPGRADE_POLICIES.values()))[0], BUDGET_POLICY_V3, "unregistered"),
    (next(iter(UPGRADE_POLICIES.values()))[0], BUDGET_POLICY_V3, False),
])
def test_execution_rejects_unregistered_or_non_v2_strict_output(profile, budget, identity):
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy(profile, model_mode="enabled", search_source_names=("deepseek_search",),
                        budget_policy_id=budget, clean_output_policy_id=identity)


def strict_fixture(tmp_path, monkeypatch, *, phase="daily"):
    """Run all actual stages and real transport proof code using fake HTTP only."""
    import json
    import shutil
    import uuid
    from pathlib import Path
    import test_short_term_core_v2 as core
    from test_required_proof_v2 import complete_fixture
    from trading_system.agent_evidence import atomic_json, digest
    from trading_system.agent_runtime import freeze_source
    from trading_system.llm.clean_output import CLEAN_OUTPUT_FUNCTION, CLEAN_OUTPUT_PATH
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.redline.execution import PUBLIC_PROVIDER_NAMES
    from trading_system.providers.stooq import StooqProvider
    import importlib

    # The real policy retains its complete public allowlist. Every fallback
    # boundary is explicitly replaced; no private network client is exercised.
    monkeypatch.setattr(StooqProvider, "ohlcv_batch", lambda self, symbols, days=400: {})
    monkeypatch.setattr(StooqProvider, "quote", lambda self, symbol: None)
    for module_name, class_name in (("official", "OfficialMacroProvider"),
            ("tencent", "TencentProvider"), ("sina", "SinaProvider"), ("eastmoney", "EastMoneyProvider")):
        cls = getattr(importlib.import_module("trading_system.providers." + module_name), class_name)
        monkeypatch.setattr(cls, "available", staticmethod(lambda: False))

    original_policy, original_upgrade = core.ExecutionPolicy, core.upgrade_fixture
    monkeypatch.setattr(core, "ExecutionPolicy", lambda *args, **kwargs: original_policy(*args,
        **{**kwargs, "provider_names": PUBLIC_PROVIDER_NAMES, "budget_policy_id": BUDGET_POLICY_V3,
           "clean_output_policy_id": CLEAN_OUTPUT_POLICY_V1}))

    def upgrade(*args, **kwargs):
        returned = original_upgrade(*args, **kwargs)
        attempts = returned[-1]
        legacy_post = DeepSeekRequiredClient._post_json
        native_seen = [0]

        def post(client, path, request):
            if path != CLEAN_OUTPUT_PATH:
                response, transport_hash = legacy_post(client, path, request)
                if path == "/v1/tools/search":
                    native_seen[0] += 1
                    if native_seen[0] == 1:
                        # Give this source a legal p1 while neighbors retain
                        # only p0, to test actual source ownership in replay.
                        response["results"][0]["content"] += " SYNTHETIC_TEST source-bound extra text." * 20
                        transport_hash = core._hash(response)
                return response, transport_hash
            stage = client.execution._active.spec.name
            assert stage == "clean.llm_semantic"
            attempts.append((stage, path))
            client._remote_requests += 1
            user, framed = request["messages"][1]["content"].split(
                "\nLocal JSON schema (including mandatory local-only bounds):\n", 1)
            schema = json.loads(framed.split("\nJSON field guidance:\n", 1)[0])
            inputs = json.loads(user.split("\n", 1)[1])
            answer = core._answer(stage, inputs, schema)
            response = {"id": "SYNTHETIC_TEST-" + uuid.uuid4().hex, "model": "deepseek-flash",
                "object": "chat.completion", "system_fingerprint": "fp-SYNTHETIC_TEST",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": "call-SYNTHETIC_TEST-" + uuid.uuid4().hex,
                        "type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION,
                            "arguments": json.dumps(answer, ensure_ascii=False)}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
            return response, core._hash(response)

        monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
        return returned

    monkeypatch.setattr(core, "upgrade_fixture", upgrade)
    execution, root, result, admission, denied, attempts = complete_fixture(tmp_path, monkeypatch, phase=phase)
    profile = next(key for key, value in UPGRADE_POLICIES.items() if value[1] == phase)
    policy = policy_manifest(profile, clean_output_policy_id=CLEAN_OUTPUT_POLICY_V1)
    atomic_json(root, "policy.json", policy)
    admission["policy_sha256"] = digest(json_bytes(policy))
    # This new protocol requires the complete executable frozen source manifest,
    # even though historical minimal test fixtures retain their old contract.
    shutil.rmtree(root / "source")
    source = Path(__file__).resolve().parents[1]
    freeze_source(root, source)
    admission["source_sha256"] = digest((root / "source-manifest.json").read_bytes())
    assert denied == []
    return execution, root, result, admission, attempts


def stage_children(root, stage):
    from trading_system.agent_evidence import read_snapshot, read_bytes, strict_json
    end = next(row for row in map(strict_json, read_bytes(root, "events.jsonl").splitlines())
               if row["stage"] == stage and row["kind"] == "end")
    output = read_snapshot(root, end["output_ref"], run_id=root.name, stage=stage, role="output")
    return [{"method": row["method"],
             "request": read_snapshot(root, row["input_ref"], run_id=root.name, stage=stage, role="input"),
             "proof": read_snapshot(root, row["output_ref"], run_id=root.name, stage=stage, role="output")}
            for row in output["io_calls"]]


@pytest.mark.parametrize("phase", ["daily", "premarket", "intraday"])
def test_all43_stages_strict_clean_and_original_other_stages_independently_replay(tmp_path, monkeypatch, phase):
    from trading_system.agent_evidence import verify_events
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.redline.execution import daily_stage_contract, UPGRADE_MODEL_STAGES
    execution, root, result, admission, attempts = strict_fixture(tmp_path, monkeypatch, phase=phase)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    execution.assert_complete()
    clean = stage_children(root, "clean.llm_semantic")
    assert clean and all(row["request"]["schema"] == "tiger.deepseek-request.v2" for row in clean)
    assert all(row["request"]["endpoint"] == "https://api.deepseek.com/beta/chat/completions" for row in clean)
    assert all(row["proof"]["finish_reason"] == "tool_calls" for row in clean)
    assert len(clean) == len([row for row in attempts if row[1] == "/beta/chat/completions"])
    for stage in UPGRADE_MODEL_STAGES[1:]:
        chats = [row for row in stage_children(root, stage) if row["method"] == "model.deepseek.chat"]
        assert chats and all(row["request"]["schema"] == "tiger.deepseek-request.v1" for row in chats)
        assert all(row["proof"]["finish_reason"] == "stop" for row in chats)
        assert all(row["request"]["request"]["thinking"] == {"type": "disabled"}
                   and "reasoning_effort" not in row["request"]["request"] for row in chats)
    budget = result["raw"]["model"]["budget"]
    assert budget["limits"]["max_tokens"] == 600000 and budget["reserved_tokens"] == 0
    assert budget["limits"]["max_chat_calls"] == 40 and budget["limits"]["max_remote_requests"] == 120


@pytest.mark.parametrize("tamper", ["selector_missing", "selector_unknown", "selector_none", "pipeline_policy",
    "coherent_policy_downgrade", "missing_source_hash", "source_hash", "missing_policy", "endpoint", "function",
    "schema", "arguments", "multiple_tools", "foreign_q", "local_bound", "tool_id", "finish_reason"])
def test_rehashed_strict_clean_contract_tampering_cannot_pass_offline_replay(tmp_path, monkeypatch, tamper):
    import json
    from test_required_proof_v2 import rewrite_stage, rewrite_child, h
    from trading_system.agent_evidence import atomic_json, read_json, verify_events
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.redline.execution import daily_stage_contract
    _, root, result, admission, _ = strict_fixture(tmp_path, monkeypatch)
    if tamper.startswith("selector_"):
        def mutate(inp, _out):
            selected = inp["policy"]["fields"]
            if tamper == "selector_missing": selected.pop("clean_output_policy_id")
            else: selected["clean_output_policy_id"] = None if tamper == "selector_none" else "unregistered"
        rewrite_stage(root, "daily.preflight", mutate)
    elif tamper == "pipeline_policy":
        rewrite_stage(root, "pipeline.prepare", lambda inp, out:
            inp["policy"]["fields"].pop("clean_output_policy_id"))
    elif tamper == "coherent_policy_downgrade":
        policy = read_json(root, "policy.json")
        policy["execution"].pop("clean_output_policy_id")
        policy.pop("clean_output_policy_id")
        atomic_json(root, "policy.json", policy)
        from trading_system.agent_evidence import digest
        admission["policy_sha256"] = digest(json_bytes(policy))
        for stage in ("daily.preflight", "pipeline.prepare"):
            rewrite_stage(root, stage, lambda inp, out:
                inp["policy"]["fields"].pop("clean_output_policy_id"))
    elif tamper == "missing_source_hash": admission.pop("source_sha256")
    elif tamper == "source_hash": admission["source_sha256"] = "0" * 64
    elif tamper == "missing_policy": (root / "policy.json").unlink()
    else:
        def mutate(_inp, out):
            call = out["io_calls"][0]
            def request(value):
                if tamper == "endpoint": value["endpoint"] = "https://api.deepseek.com/chat/completions"
                elif tamper == "function": value["request"]["tools"][0]["function"]["name"] = "execute_order"
                elif tamper == "schema":
                    value["request"]["tools"][0]["function"]["parameters"]["properties"]["d"]["additionalProperties"] = True
                value["request_sha256"] = h(value["request"])
            def proof(value):
                message = value["response"]["choices"][0]["message"]
                if tamper in {"function", "schema"}:
                    child = read_json(root, call["input_ref"]["path"])["value"]
                    value["request_sha256"] = child["request_sha256"]
                elif tamper == "arguments": message["tool_calls"][0]["function"]["arguments"] = '{"d":{}}'
                elif tamper == "multiple_tools": message["tool_calls"].append(deepcopy(message["tool_calls"][0]))
                elif tamper == "foreign_q":
                    original_request = read_json(root, call["input_ref"]["path"])["value"]
                    bindings = original_request["output_schema"]["properties"]["d"]["allOf"][1]["properties"]
                    owned = {ref: set(binding["properties"]["c"]["items"]["properties"]["q"].get("enum", []))
                             for ref, binding in bindings.items()}
                    all_parts = set().union(*owned.values())
                    reference = next(ref for ref, doc in value["parsed"]["d"].items()
                                     if doc["c"] and all_parts - owned[ref])
                    value["parsed"]["d"][reference]["c"][0]["q"] = sorted(all_parts - owned[reference])[0]
                    message["tool_calls"][0]["function"]["arguments"] = json.dumps(value["parsed"], sort_keys=True, separators=(",", ":"))
                elif tamper == "local_bound":
                    doc = next(iter(value["parsed"]["d"].values()))
                    doc["z"] = ""
                    message["tool_calls"][0]["function"]["arguments"] = json.dumps(value["parsed"], sort_keys=True, separators=(",", ":"))
                elif tamper == "tool_id": value["output_tool_call_id"] = "call-unbound-SYNTHETIC_TEST"
                elif tamper == "finish_reason": value["finish_reason"] = "stop"
                value["response_sha256"] = h(value["response"])
            if tamper in {"endpoint", "function", "schema"}: rewrite_child(root, call, "input", request)
            if tamper != "endpoint": rewrite_child(root, call, "output", proof)
        rewrite_stage(root, "clean.llm_semantic", mutate)
    # Rehashed event and child references remain valid: rejection comes from
    # reconstructing trusted policy / original request / source ownership.
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid):
        verify_required_model_v2(root, result, admission)


@pytest.mark.parametrize("strict", [False, True])
def test_actual_admission_retains_legacy_v3_and_rejects_request_free_override(tmp_path, strict):
    from datetime import datetime
    from trading_system.agent_runtime import (freeze_source, frozen_clock, validate_admission,
        _calendar_input, SOURCE_ROOT)
    from trading_system.agent_evidence import atomic_json, atomic_bytes, digest
    root = tmp_path / ("run-" + "a" * 32)
    root.mkdir()
    (root / "work").mkdir()
    profile = next(iter(UPGRADE_POLICIES))
    policy = policy_manifest(profile, budget_policy_id=BUDGET_POLICY_V3,
        clean_output_policy_id=CLEAN_OUTPUT_POLICY_V1 if strict else None)
    request = {"schema_version": "tiger.agent-request/v1", "profile": profile,
               "paper_account": "public-usd", "idempotency_key": "SYNTHETIC_TEST-legacy-v3"}
    atomic_json(root, "policy.json", policy)
    freeze_source(root, SOURCE_ROOT)
    atomic_json(root, "request.json", request)
    content, calendar = _calendar_input(root)
    atomic_json(root, "calendar-input.json", calendar)
    atomic_bytes(root, calendar["work_path"], content)
    admission = {"schema": "tiger.admission.v1", "run_id": root.name,
        "clock": frozen_clock(datetime.fromisoformat("2026-10-27T14:00:00+00:00")), "parent": None,
        "request_sha256": digest(json_bytes(request)), "policy_sha256": digest(json_bytes(policy)),
        "source_sha256": digest((root / "source-manifest.json").read_bytes())}
    atomic_json(root, "admission.json", admission)
    assert normalize(validate_admission(root)[1]) == normalize(policy)
    request["clean_output_policy_id"] = CLEAN_OUTPUT_POLICY_V1
    atomic_json(root, "request.json", request)
    admission["request_sha256"] = digest(json_bytes(request))
    atomic_json(root, "admission.json", admission)
    with pytest.raises(EvidenceInvalid): validate_admission(root)
