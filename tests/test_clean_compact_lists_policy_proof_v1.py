"""Source-selected compact regeneration and independent proof; SYNTHETIC_TEST.

Every provider boundary is synthetic. No successful test is a business receipt.
The pure records deliberately exercise proof independently of transport code.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import importlib
import json
import shutil
from types import SimpleNamespace
import uuid

import pytest

from trading_system.agent_evidence import EvidenceInvalid, atomic_json, digest, json_bytes, read_bytes
from trading_system.agent_runtime import SOURCE_ROOT, freeze_source, policy_manifest
from trading_system.llm.clean_output import (
    CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1 as POLICY, CLEAN_COMPACT_LISTS_WIRE_V1 as CODEC,
    CLEAN_REGENERATION_POLICY_V1 as REGEN, CLEAN_OUTPUT_PATH, CLEAN_OUTPUT_FUNCTION,
    build_clean_compact_lists_request, build_clean_compact_lists_safe_response,
    clean_compact_lists_output_v1, clean_compact_lists_schema_v1,
    clean_compact_lists_payload_v1, clean_regeneration_feedback_v1, clean_logical_call_sha256,
)
from trading_system.llm.deepseek_required import MODEL, RELEASE, SEARCH_MODE, validate_schema
from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, BUDGET_POLICY_V3, token_upper_bound
from trading_system.llm.required_proof_v2 import ReplayClient, verify_required_model_v2
from trading_system.redline.execution import ExecutionPolicy, ExecutionContractError, UPGRADE_POLICIES, PUBLIC_PROVIDER_NAMES
from trading_system.redline.model_diagnostics import RequiredModelUnavailable

ADMITTED = "2026-10-27T14:00:00+00:00"
ASSESSMENT = {"run_date": "2026-10-27", "market_timezone": "America/New_York", "admitted_at_utc": ADMITTED}


def h(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def keyed_schema(empty=False):
    from trading_system import config
    from trading_system.llm.required import clean_keyed_schema_v2
    from trading_system.llm.required_schemas import clean_schema_v2
    from trading_system.tech_chain.universe import TECH_SUBCHAINS
    ids = [] if empty else ["SYNTHETIC_TEST_source"]
    schema = clean_schema_v2(ids, list(TECH_SUBCHAINS), config.SECTOR_ETFS,
        quote_part_ids={identity: ["p0"] for identity in ids}, all_doc_ids=ids)
    return clean_keyed_schema_v2(schema, ids)


def valid_answer(schema):
    refs = schema["properties"]["d"]["allOf"][1]["properties"]
    keyed = {"d": {ref: {"t": [], "s": "neutral", "v": 0., "e": [],
        "z": "SYNTHETIC_TEST 完整原始资料尚不能证明业务结论。", "r": .5, "h": [], "f": [],
        "p": {"k": "unknown", "o": None, "n": "SYNTHETIC_TEST 来源归属未知。", "t": "unknown"},
        "c": []} for ref in refs}}
    return clean_compact_lists_output_v1(keyed, schema)


def pure_fixture(*, rejection=True, second_rejected=False, empty=False, invalid_kind="summary"):
    """Closed synthetic proofs with a real guard and actual deterministic rejection."""
    schema = keyed_schema(empty)
    valid = valid_answer(schema)
    arguments = {"system": "SYNTHETIC_TEST retain every source and constraint.",
        "user": "SYNTHETIC_TEST complete original source body and original registry.",
        "schema_hint": {}, "schema": schema, "max_tokens": 32768, "temperature": .2}
    profile = next(iter(UPGRADE_POLICIES))
    policy = ExecutionPolicy(UPGRADE_POLICIES[profile][0], model_mode="enabled",
        search_source_names=("deepseek_search",), budget_policy_id=BUDGET_POLICY_V3,
        clean_output_policy_id=POLICY)
    execution = SimpleNamespace(run_id="run-" + "a" * 32, run_date=date(2026, 10, 27),
        admitted_at_utc=ADMITTED, deadline_at_utc="2026-10-27T14:40:00+00:00", upgrade=True, policy=policy)
    guard = BudgetGuard.for_policy(BUDGET_POLICY_V3, clean_output_policy_id=POLICY, clock=lambda: 0.)
    base = build_clean_compact_lists_request(**arguments)
    logical = clean_logical_call_sha256(run_id=execution.run_id, assessment=ASSESSMENT, request=base, schema=schema)
    records, previous, feedback = [], None, None
    for generation in range(2 if rejection else 1):
        reject = rejection and (generation == 0 or second_rejected)
        body = build_clean_compact_lists_request(**arguments, regeneration_feedback=feedback)
        answer = deepcopy(valid)
        if reject:
            if empty:
                answer = {"foreign": "SYNTHETIC_TEST extra field"}
            elif invalid_kind == "prefix":
                for field, value in (("s", "SYNTHETIC_TEST 合成声明。"), ("q", "p0"),
                                     ("k", "synthetic_test"), ("t", "unknown")):
                    answer["d0__c__s1__" + field] = value
            else:
                answer["d0__z"] = "SYNTHETIC_TEST " + "长" * 900
        diagnostic = None
        try:
            validate_schema(answer, clean_compact_lists_schema_v1(schema))
            clean_compact_lists_output_v1(answer, schema, decode=True)
        except RequiredModelUnavailable as error:
            diagnostic = error.diagnostic
        assert reject == (diagnostic is not None)
        text = json.dumps(answer, ensure_ascii=False, indent=1)
        identity = f"{generation + 1:032x}"
        reservation = guard.reserve("chat", identity, body)
        usage = {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
        guard.settle(identity, usage)
        start = datetime.fromisoformat(ADMITTED) + timedelta(seconds=1 + generation * 2)
        finish = start + timedelta(seconds=1)
        provenance = {"output_policy_id": POLICY, "output_codec_id": CODEC,
            "regeneration_policy_id": REGEN, "generation_index": generation,
            "logical_call_sha256": logical, "previous_rejection_sha256": previous}
        request = {"schema": "tiger.deepseek-request.v4", "call_id": identity,
            "stage": "clean.llm_semantic", "provider": "deepseek", "method": "POST",
            "endpoint": "https://api.deepseek.com" + CLEAN_OUTPUT_PATH,
            "request": body, "request_sha256": h(body), "output_schema": schema,
            "schema_sha256": h(schema), "release": RELEASE, "budget_reservation": reservation,
            "started_at_utc": start.isoformat(), **provenance}
        response = build_clean_compact_lists_safe_response(response_id="SYNTHETIC_TEST-response-" + identity,
            system_fingerprint="fp-SYNTHETIC_TEST", usage=usage, tool_call_id="call-SYNTHETIC_TEST-" + identity,
            **({"visible_arguments": text} if reject else {"parsed": answer}))
        proof = {"schema": "tiger.deepseek-call.v4", "call_id": identity, "stage": "clean.llm_semantic",
            "provider": "deepseek", "requested_model": MODEL, "response_id": response["id"], "model": MODEL,
            "release": RELEASE, "usage": usage, "finish_reason": "tool_calls", "request_sha256": h(body),
            "response_sha256": h(response), "transport_response_sha256": h(response), "schema_sha256": h(schema),
            "response": response, "system_fingerprint": "fp-SYNTHETIC_TEST", "search_mode": SEARCH_MODE,
            "output_tool_call_id": response["choices"][0]["message"]["tool_calls"][0]["id"],
            "budget_reservation": reservation, "budget_after": guard.snapshot(), "started_at_utc": start.isoformat(),
            "finished_at_utc": finish.isoformat(), "elapsed_ms": 1000., **provenance,
            "outcome": "rejected" if reject else "accepted"}
        if reject:
            proof.update(visible_arguments_sha256=hashlib.sha256(text.encode()).hexdigest(), rejection_diagnostic=diagnostic)
            feedback = clean_regeneration_feedback_v1(diagnostic)
            previous = h(proof)
        else:
            proof["parsed"] = answer
        records.append({"method": "model.deepseek.chat", "request": request, "proof": proof})
    return execution, arguments, valid, records


def replay_fixture(value):
    execution, arguments, valid, records = value
    replay = ReplayClient(execution, {"clean.llm_semantic": records})
    replay.set_stage("clean.llm_semantic")
    answer = replay.complete_json(**arguments, reasoning_effort="none")
    replay.finish()
    return replay, answer


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_explicit_v3_admission_retains_original_selector_codec_and_limits(profile):
    value = policy_manifest(profile, budget_policy_id=BUDGET_POLICY_V3)
    assert value["execution"]["clean_output_policy_id"] == value["clean_output_policy_id"] == POLICY
    assert value["clean_output_codec_id"] == CODEC and value["regeneration_policy_id"] == REGEN
    assert value["clean_regeneration_limits"] == {"max_per_batch": 1, "max_per_run": 4}
    assert value["execution"]["budget_policy_id"] == BUDGET_POLICY_V3
    assert value["request_budget"] == {"max_chat_calls": 40, "max_tokens": 600000,
        "max_search_calls": 60, "max_fetch_calls": 20, "max_remote_requests": 120,
        "max_cost_usd": 1., "deadline_seconds": 2400}
    assert not {"regeneration_policy_id", "clean_regeneration_limits", "clean_output_codec_id"}.intersection(value["execution"])


@pytest.mark.parametrize("selector", [None, "tiger.clean-output.strict-function.v1",
    "tiger.clean-output.responses-schema.v1", "tiger.clean-output.responses-slots.v1"])
def test_old_explicit_selectors_keep_original_manifest_without_regeneration_fields(selector):
    value = policy_manifest(next(iter(UPGRADE_POLICIES)), clean_output_policy_id=selector)
    assert "regeneration_policy_id" not in value and "clean_regeneration_limits" not in value
    assert value.get("clean_output_policy_id") == selector


@pytest.mark.parametrize("selector,budget,upgrade", [(POLICY, None, True), (POLICY, BUDGET_POLICY_V3, False),
    (POLICY + ".unknown", BUDGET_POLICY_V3, True), (False, BUDGET_POLICY_V3, True)])
def test_new_selector_requires_exact_upgrade_and_budget(selector, budget, upgrade):
    identity = next(iter(UPGRADE_POLICIES.values()))[0] if upgrade else "tiger.public-model-required-paper-policy.v1"
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy(identity, model_mode="enabled", search_source_names=("deepseek_search",),
            budget_policy_id=budget, clean_output_policy_id=selector)
    if upgrade:
        with pytest.raises(BudgetRefused):
            BudgetGuard.for_policy(budget, clean_output_policy_id=selector)


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("rejection", [False, True])
def test_independent_replay_accepts_first_or_only_proved_regeneration_and_charges_both(empty, rejection):
    fixture = pure_fixture(empty=empty, rejection=rejection)
    replay, answer = replay_fixture(fixture)
    assert answer == fixture[2]
    assert len(replay.receipts) == 1 + rejection
    assert replay.budget.snapshot()["actual_tokens"] == 50 * (1 + rejection)
    assert replay.budget.snapshot()["counts"]["chat"] == 1 + rejection
    assert replay.budget.snapshot()["reserved_tokens"] == 0
    for receipt, record in zip(replay.receipts, fixture[3]):
        assert receipt["proof_sha256"] == h(record["proof"])
        assert not {"response", "parsed", "rejection_diagnostic"}.intersection(receipt)
    if rejection:
        assert replay.receipts[1]["previous_rejection_sha256"] == replay.receipts[0]["proof_sha256"]
        assert fixture[3][0]["request"]["request"]["messages"][1] == fixture[3][1]["request"]["request"]["messages"][1]


def test_inverse_only_prefix_hole_rejection_has_fixed_no_path_feedback_and_is_replayed():
    fixture = pure_fixture(invalid_kind="prefix")
    diagnostic = fixture[3][0]["proof"]["rejection_diagnostic"]
    assert diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": "OUTPUT_SCHEMA_MISMATCH"}
    assert replay_fixture(fixture)[1] == fixture[2]


@pytest.mark.parametrize("tamper", ["logical", "run", "admission", "predecessor", "generation", "feedback", "input",
    "schema", "policy", "codec", "regen", "missing_rejection", "reordered", "extra", "visible_hash", "diagnostic",
    "false_rejection", "false_acceptance", "unsafe_decoded_value", "unsafe_decoded_key", "syntax", "duplicate",
    "nonfinite", "unsafe_unicode", "response_identity", "tool_identity", "rejected_parsed", "accepted_missing_parsed", "usage", "ledger"])
def test_rehashed_attempt_and_admission_mutations_fail_independent_replay(tamper):
    fixture = pure_fixture()
    execution, arguments, valid, records = fixture
    initial, last = records[0], records[1]
    proof, request = initial["proof"], initial["request"]
    if tamper == "logical": proof["logical_call_sha256"] = "b" * 64
    elif tamper == "run": execution.run_id = "run-" + "b" * 32
    elif tamper == "admission": execution.admitted_at_utc = "2026-10-27T14:00:00.001000+00:00"
    elif tamper == "predecessor": last["proof"]["previous_rejection_sha256"] = "b" * 64
    elif tamper == "generation": last["proof"]["generation_index"] = True
    elif tamper in {"feedback", "input"}:
        target = last["request"]["request"]["messages"][0 if tamper == "feedback" else 1]
        target["content"] += " SYNTHETIC_TEST unbound text"
        last["request"]["request_sha256"] = last["proof"]["request_sha256"] = h(last["request"]["request"])
    elif tamper == "schema": request["output_schema"]["properties"]["d"]["allOf"][0]["additionalProperties"]["properties"]["z"]["maxLength"] = 1000
    elif tamper in {"policy", "codec", "regen"}:
        proof[{"policy": "output_policy_id", "codec": "output_codec_id", "regen": "regeneration_policy_id"}[tamper]] += ".unknown"
    elif tamper == "missing_rejection": records.pop(0)
    elif tamper == "reordered": records.reverse()
    elif tamper == "extra": proof["retry_allowed"] = True
    elif tamper == "visible_hash": proof["visible_arguments_sha256"] = "b" * 64
    elif tamper == "diagnostic": proof["rejection_diagnostic"]["schema_keyword"] = "type"
    elif tamper == "false_rejection":
        text = json.dumps(valid, ensure_ascii=False)
        proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = text
        proof["visible_arguments_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    elif tamper == "false_acceptance":
        proof["parsed"] = json.loads(proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        proof["outcome"] = "accepted"
        proof.pop("visible_arguments_sha256"); proof.pop("rejection_diagnostic")
    elif tamper in {"unsafe_decoded_value", "unsafe_decoded_key", "syntax", "duplicate", "nonfinite", "unsafe_unicode"}:
        if tamper == "unsafe_decoded_value": text = '{"d0__z":"\\u0061pi_key=\\u0073ynthetic-credential"}'
        elif tamper == "unsafe_decoded_key": text = '{"\\u0061pi_key=\\u0073ynthetic-credential":"x"}'
        elif tamper == "syntax": text = '{"x":'
        elif tamper == "duplicate": text = '{"x":1,"x":2}'
        elif tamper == "nonfinite": text = '{"x":NaN}'
        else: text = '{"d0__z":"\\ud800"}'
        proof["response"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = text
        proof["visible_arguments_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    elif tamper == "response_identity": last["proof"]["response_id"] = last["proof"]["response"]["id"] = proof["response_id"]
    elif tamper == "tool_identity":
        last["proof"]["output_tool_call_id"] = last["proof"]["response"]["choices"][0]["message"]["tool_calls"][0]["id"] = proof["output_tool_call_id"]
    elif tamper == "rejected_parsed": proof["parsed"] = valid
    elif tamper == "accepted_missing_parsed": last["proof"].pop("parsed")
    elif tamper == "usage": proof["usage"]["total_tokens"] += 1
    else: proof["budget_after"]["actual_tokens"] = 0
    for record in records:
        record["proof"]["response_sha256"] = h(record["proof"]["response"])
    with pytest.raises((EvidenceInvalid, RequiredModelUnavailable, BudgetRefused)):
        replay_fixture(fixture)


def test_second_schema_rejection_is_charged_and_preserved_but_cannot_complete():
    fixture = pure_fixture(second_rejected=True)
    execution, arguments, _, records = fixture
    replay = ReplayClient(execution, {"clean.llm_semantic": records})
    replay.set_stage("clean.llm_semantic")
    with pytest.raises(EvidenceInvalid, match="second_compact_rejection"):
        replay.complete_json(**arguments, reasoning_effort="none")
    assert len(replay.receipts) == 2
    assert [row["outcome"] for row in replay.receipts] == ["rejected", "rejected"]
    assert replay.budget.snapshot()["actual_tokens"] == 100


def test_exhausted_run_allowance_preserves_fifth_initial_rejection_without_consuming_generation_one():
    fixture = pure_fixture()
    execution, arguments, _, records = fixture
    replay = ReplayClient(execution, {"clean.llm_semantic": records})
    replay._regeneration_count = 4
    replay.set_stage("clean.llm_semantic")
    with pytest.raises(EvidenceInvalid, match="clean_regeneration_allowance"):
        replay.complete_json(**arguments, reasoning_effort="none")
    assert replay._cursor == 1 and replay.budget.snapshot()["counts"]["chat"] == 1
    assert replay.receipts[0]["outcome"] == "rejected" and replay.budget.snapshot()["actual_tokens"] == 50


@pytest.mark.parametrize("rejection", [False, True])
def test_prepaid_finite_compact_request_is_new_flag_only_and_full_second_reservation(rejection):
    fixture = pure_fixture(rejection=rejection)
    for record in fixture[3]:
        body = record["request"]["request"]
        expected = len(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()) + 4096 + 128
        assert token_upper_bound(body, allow_clean_compact_regeneration=True) == (expected, 32768)
        for old in ({}, {"allow_clean_function": True}, {"allow_clean_responses": True}, {"allow_clean_slots": True}):
            with pytest.raises(BudgetRefused): token_upper_bound(body, **old)


def install_compact_fixture(monkeypatch, *, reject_once=True, empty_sources=False):
    """Actual application/client/proof code, synthetic HTTP only."""
    import test_short_term_core_v2 as core
    from test_clean_slots_policy_replay_v1 import original_keyed_schema
    from trading_system.llm.required import clean_keyed_payload_v2
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.providers.stooq import StooqProvider
    monkeypatch.setattr(StooqProvider, "ohlcv_batch", lambda self, symbols, days=400: {})
    monkeypatch.setattr(StooqProvider, "quote", lambda self, symbol: None)
    for name, cls_name in (("official", "OfficialMacroProvider"), ("tencent", "TencentProvider"),
            ("sina", "SinaProvider"), ("eastmoney", "EastMoneyProvider")):
        cls = getattr(importlib.import_module("trading_system.providers." + name), cls_name)
        monkeypatch.setattr(cls, "available", staticmethod(lambda: False))
    original_policy, original_upgrade = core.ExecutionPolicy, core.upgrade_fixture
    monkeypatch.setattr(core, "ExecutionPolicy", lambda *args, **kwargs: original_policy(*args,
        **{**kwargs, "provider_names": PUBLIC_PROVIDER_NAMES, "budget_policy_id": BUDGET_POLICY_V3,
           "clean_output_policy_id": POLICY}))

    def upgrade(*args, **kwargs):
        returned = original_upgrade(*args, **kwargs)
        attempts, native_seen = returned[-1], [0]
        legacy_post = DeepSeekRequiredClient._post_json

        def post(client, path, request):
            if path != CLEAN_OUTPUT_PATH:
                response, transport_hash = legacy_post(client, path, request)
                if empty_sources and path == "/v1/tools/search":
                    response["results"] = []
                    return response, h(response)
                return response, transport_hash
            stage = client.execution._active.spec.name
            assert stage == "clean.llm_semantic"
            attempts.append((stage, path)); client._remote_requests += 1
            inputs = json.loads(request["messages"][1]["content"].split("\n", 1)[1])
            schema = original_keyed_schema(inputs)
            payload = clean_compact_lists_payload_v1({key: value for key, value in inputs.items() if key != "assessment"}, schema, decode=True)
            answer = core._answer(stage, {"assessment": inputs["assessment"], **clean_keyed_payload_v2(payload)}, schema)
            compact = clean_compact_lists_output_v1(answer, schema)
            native_seen[0] += 1
            if reject_once and native_seen[0] == 1:
                if compact: compact[next(key for key in compact if key.endswith("__z"))] = "SYNTHETIC_TEST " + "长" * 900
                else: compact["foreign"] = "SYNTHETIC_TEST empty-source invalid key"
            response = {"id": "SYNTHETIC_TEST-" + uuid.uuid4().hex, "model": MODEL, "object": "chat.completion",
                "system_fingerprint": "fp-SYNTHETIC_TEST", "choices": [{"index": 0, "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-SYNTHETIC_TEST-" + uuid.uuid4().hex,
                        "type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION, "arguments": json.dumps(compact, ensure_ascii=False)}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
            return response, h(response)

        monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
        return returned

    monkeypatch.setattr(core, "upgrade_fixture", upgrade)


def compact_complete_fixture(tmp_path, monkeypatch, *, phase="daily", reject_once=True, empty_sources=False, **options):
    from test_required_proof_v2 import complete_fixture
    install_compact_fixture(monkeypatch, reject_once=reject_once, empty_sources=empty_sources)
    execution, root, result, admission, denied, attempts = complete_fixture(tmp_path, monkeypatch, phase=phase, **options)
    profile = next(key for key, row in UPGRADE_POLICIES.items() if row[1] == phase)
    policy = policy_manifest(profile, budget_policy_id=BUDGET_POLICY_V3)
    atomic_json(root, "policy.json", policy)
    admission["policy_sha256"] = digest(json_bytes(policy))
    shutil.rmtree(root / "source"); freeze_source(root, SOURCE_ROOT)
    admission["source_sha256"] = digest(read_bytes(root, "source-manifest.json"))
    assert denied == []
    return execution, root, result, admission, attempts


@pytest.mark.parametrize("phase,empty_sources", [("daily", False), ("premarket", False), ("intraday", True)])
def test_actual43_stages_replay_one_real_synthetic_schema_rejection_with_all_six_accepted_outputs(tmp_path, monkeypatch, phase, empty_sources):
    from test_clean_strict_policy_replay_v2 import stage_children
    from trading_system.agent_evidence import verify_events
    from trading_system.redline.execution import daily_stage_contract
    execution, root, result, admission, _ = compact_complete_fixture(tmp_path, monkeypatch,
        phase=phase, empty_sources=empty_sources, empty_candidates=empty_sources)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    execution.assert_complete()
    children = stage_children(root, "clean.llm_semantic")
    assert children[0]["proof"]["outcome"] == "rejected" and children[1]["proof"]["outcome"] == "accepted"
    assert children[1]["proof"]["previous_rejection_sha256"] == h(children[0]["proof"])
    assert children[0]["request"]["request"]["messages"][1] == children[1]["request"]["request"]["messages"][1]
    model = result["raw"]["model"]
    assert model["clean_output_policy_id"] == POLICY and model["clean_output_codec_id"] == CODEC
    assert model["regeneration_policy_id"] == REGEN and model["rejected_call_count"] == 1
    assert model["accepted_call_count"] + model["rejected_call_count"] == model["call_count"]
    assert model["budget"]["counts"]["chat"] == model["call_count"] == len(model["response_ids"])
    assert model["budget"]["actual_tokens"] == model["call_count"] * 200
    assert len(set(model["response_ids"])) == model["call_count"] and model["budget"]["reserved_tokens"] == 0
    if empty_sources: assert children[1]["proof"]["parsed"] == {}
