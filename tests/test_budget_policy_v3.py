"""Versioned policy admission/replay; all transport is synthetic and offline."""
import hashlib
from types import SimpleNamespace
from copy import deepcopy

import pytest

from trading_system.agent_evidence import EvidenceInvalid, json_bytes, normalize
from trading_system.agent_runtime import policy_manifest
from trading_system.llm.request_budget import BudgetGuard, BudgetRefused, LIMITS, token_upper_bound
from trading_system.redline.execution import ExecutionPolicy, ExecutionContractError, UPGRADE_POLICIES

V3 = "tiger.budget-policy.v3"


def request(total=500000):
    value = {"messages": [{"role": "user", "content": "x"}], "max_tokens": min(300000, total // 2, max(1, total - 4500))}
    initial = sum(token_upper_bound(value))
    value["messages"][0]["content"] += "x" * (total - initial)
    assert sum(token_upper_bound(value)) == total
    return value


def test_factory_keeps_legacy_exact_and_admits_only_the_explicit_v3():
    old = BudgetGuard(clock=lambda: 0.)
    assert old.snapshot() == BudgetGuard.for_policy(None, clock=lambda: 0.).snapshot()
    assert old.snapshot() == BudgetGuard.for_policy("tiger.budget-policy.v2", clock=lambda: 0.).snapshot()
    with pytest.raises(BudgetRefused, match="TOKEN_BUDGET_LIMIT"):
        old.reserve("chat", "a" * 32, request())
    new = BudgetGuard.for_policy(V3, clock=lambda: 0.)
    reservation = new.reserve("chat", "a" * 32, request())
    assert new.snapshot()["schema"] == "tiger.request-budget.v3"
    assert new.snapshot()["policy_id"] == reservation["policy_id"] == V3
    assert reservation["schema"] == "tiger.request-budget-reservation.v3"
    assert new.limits == {**LIMITS, "max_tokens": 600000}
    assert LIMITS["max_tokens"] == 300000
    with pytest.raises(TypeError):
        BudgetGuard.for_policy(V3, max_tokens=900000)


@pytest.mark.parametrize("identity", ["tiger.budget-policy.v4", "", {}, 3, False])
def test_unregistered_policy_cannot_choose_a_larger_budget(identity):
    with pytest.raises(BudgetRefused, match="INVALID_BUDGET_POLICY"):
        BudgetGuard.for_policy(identity)


def test_v3_exact_ceiling_and_unknown_usage_preserve_prepaid_limits():
    guard = BudgetGuard.for_policy(V3, clock=lambda: 0.)
    r = guard.reserve("chat", "a" * 32, request(600000))
    assert guard.reserve("chat", "a" * 32, request(600000)) == r
    guard.settle("a" * 32, {"prompt_tokens": r["input_token_upper_bound"],
        "completion_tokens": r["output_token_upper_bound"], "total_tokens": 600000})
    assert guard.snapshot()["estimated_charged_usd"] == pytest.approx(.72)
    with pytest.raises(BudgetRefused, match="TOKEN_BUDGET_LIMIT"):
        guard.reserve("chat", "b" * 32, request(5000))
    guard.reserve("search", "b" * 32, {"query": "synthetic"})
    guard.mark_unknown("b" * 32)
    with pytest.raises(BudgetRefused, match="UNKNOWN_USAGE_FREEZES_BUDGET"):
        guard.reserve("fetch", "c" * 32, {"url": "https://example.com"})
    assert guard.snapshot()["limits"]["max_cost_usd"] == 1.


def test_v3_does_not_raise_chat_count_or_deadline():
    elapsed = [0.]
    guard = BudgetGuard.for_policy(V3, clock=lambda: elapsed[0])
    body = request(5000)
    for index in range(40):
        identity = f"{index:032x}"
        guard.reserve("chat", identity, body)
        guard.settle(identity, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    with pytest.raises(BudgetRefused, match="REQUEST_COUNT_LIMIT"):
        guard.reserve("chat", "f" * 32, body)
    elapsed[0] = 2400.
    with pytest.raises(BudgetRefused, match="BUDGET_DEADLINE"):
        guard.reserve("search", "e" * 32, {})


@pytest.mark.parametrize("profile", list(UPGRADE_POLICIES))
def test_current_v2_profile_selects_v3_from_frozen_policy(profile):
    policy = policy_manifest(profile)
    assert policy["execution"]["budget_policy_id"] == V3
    assert policy["request_budget"] == {**LIMITS, "max_tokens": 600000}
    assert policy["request_budget_policy_id"] == V3


@pytest.mark.parametrize("profile,expected", [
    ("us-public-paper-v1", "e47f9a23d1553015a54b0f097e97c34e6e0b4a6bc6eb17b155cd33b6efb2064f"),
    ("us-public-active-paper-v1", "477a5c2c4e43560401198c4eb87927366a30ee80228f81f9327e8f9e1ae0d93e"),
    ("us-public-model-required-paper-v1", "2cc6b88fdbafa1ad88ec1a35f0ad8a569254724736fdf3f3d4d8b4c8e934f05d"),
    ("us-public-model-required-paper-v2", "010c6acf5d117c3f412a27d4d2d4bdc4b6d8b79d5d9aee175ad6ed46e3af9386"),
    ("us-public-model-required-premarket-paper-v2", "1dde1ceb108765426acd38a7cd69233d9b28a320cffd980e510c8e18c8545718"),
    ("us-public-model-required-intraday-paper-v2", "255f95a5f9eae3897f9c68c14e23088ce9605a9b8d5d59b269ae4abe68ac66dc"),
])
def test_historical_policy_manifest_bytes_stay_exact(profile, expected):
    assert hashlib.sha256(json_bytes(policy_manifest(profile, budget_policy_id=None,
        clean_output_policy_id=None))).hexdigest() == expected


def test_legacy_typed_policy_omits_only_the_new_absent_field():
    old = ExecutionPolicy("tiger.public-paper-policy.v1")
    fields = normalize(old)["fields"]
    assert "budget_policy_id" not in fields and len(fields) == 5
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy(old.policy_id, budget_policy_id=V3)
    with pytest.raises(ExecutionContractError):
        ExecutionPolicy(next(iter(UPGRADE_POLICIES.values()))[0], model_mode="enabled",
                        search_source_names=("deepseek_search",), budget_policy_id="unregistered")


def test_actual_client_uses_execution_policy_not_report_limits(monkeypatch):
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "SYNTHETIC_TEST-only"}.items():
        monkeypatch.setenv(key, value)
    execution = SimpleNamespace(upgrade=True, call_io=lambda *a: None,
                               policy=SimpleNamespace(budget_policy_id=V3))
    assert DeepSeekRequiredClient(execution).budget.snapshot()["limits"]["max_tokens"] == 600000
    execution.policy.budget_policy_id = None
    assert DeepSeekRequiredClient(execution).budget.snapshot()["schema"] == "tiger.request-budget.v2"


@pytest.mark.parametrize("failure", ["settled_schema_failure", "unknown_after_400k"])
def test_v3_failure_accounting_retains_over300k_usage_without_failed_text(monkeypatch, failure):
    import json
    from trading_system.llm.deepseek_required import DeepSeekRequiredClient
    from trading_system.redline.model_diagnostics import RequiredModelUnavailable, validate_model_failure_accounting
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "SYNTHETIC_TEST-only"}.items():
        monkeypatch.setenv(key, value)
    execution = SimpleNamespace(upgrade=True, call_io=lambda method, inputs, invoke: invoke(),
        policy=SimpleNamespace(budget_policy_id=V3),
        _active=SimpleNamespace(spec=SimpleNamespace(name="decision.debate")))
    client = DeepSeekRequiredClient(execution)
    calls = []
    def post(path, body):
        calls.append(path)
        if len(calls) > 1:
            raise RequiredModelUnavailable("NETWORK_TIMEOUT")
        response = {"object": "chat.completion", "id": "SYNTHETIC_RESPONSE", "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"status": "SYNTHETIC_TEST"})}}],
            "usage": {"prompt_tokens": 170000, "completion_tokens": 230000, "total_tokens": 400000}}
        if failure == "settled_schema_failure":
            response["choices"][0]["message"]["content"] = json.dumps({"untrusted": "PRIVATE_REJECTED_TEXT"})
        return response, "a" * 64
    monkeypatch.setattr(client, "_post_json", post)
    args = dict(system="SYNTHETIC_TEST", user="x" * 180000, schema_hint={}, max_tokens=300000,
        schema={"type": "object", "properties": {"status": {"const": "SYNTHETIC_TEST"}},
                "required": ["status"], "additionalProperties": False})
    if failure == "unknown_after_400k":
        assert client.complete_json(**args) == {"status": "SYNTHETIC_TEST"}
        args.update(user="next synthetic call", max_tokens=100)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(**args)
    accounting = caught.value.accounting
    assert accounting["schema"] == "tiger.model-call-accounting.v3"
    assert accounting["budget_policy_id"] == V3 and accounting["actual_tokens"] == 400000
    assert accounting["status"] == ("settled" if failure == "settled_schema_failure" else "unknown")
    assert accounting["frozen"] is (failure == "unknown_after_400k")
    assert "PRIVATE_REJECTED_TEXT" not in json.dumps(accounting)
    assert validate_model_failure_accounting(accounting) == accounting
    malformed = {**accounting, "schema": "tiger.model-call-accounting.v2"}
    malformed.pop("budget_policy_id")
    with pytest.raises(ValueError):
        validate_model_failure_accounting(malformed)


@pytest.mark.parametrize("tamper", ["policy_missing", "policy_unknown", "actual_over", "sum_over", "price_over"])
def test_v3_failure_accounting_rejects_unsupported_policy_or_excess(tamper):
    from trading_system.redline.model_diagnostics import validate_model_failure_accounting
    value = {"schema": "tiger.model-call-accounting.v3", "budget_policy_id": V3,
        "call_id": "a" * 32, "request_sha256": "b" * 64, "kind": "chat", "status": "unknown",
        "usage": None, "counts": {"chat": 2, "search": 14, "fetch": 0}, "actual_tokens": 400000,
        "reserved_tokens": 100000, "estimated_charged_usd": .48, "estimated_reserved_usd": .12,
        "frozen": True, "finish_reason": None}
    if tamper == "policy_missing": value.pop("budget_policy_id")
    elif tamper == "policy_unknown": value["budget_policy_id"] = "tiger.budget-policy.v4"
    elif tamper == "actual_over": value["actual_tokens"] = 600001
    elif tamper == "sum_over": value["reserved_tokens"] = 200001
    else: value["estimated_reserved_usd"] = .6
    with pytest.raises(ValueError):
        validate_model_failure_accounting(value)


def frozen_fixture(tmp_path, monkeypatch):
    import test_short_term_core_v2 as core
    from test_required_proof_v2 import complete_fixture
    from trading_system.agent_runtime import source_tree_hash
    from trading_system.agent_evidence import atomic_json, file_ref
    from trading_system.redline.execution import PUBLIC_PROVIDER_NAMES
    original = core.ExecutionPolicy
    monkeypatch.setattr(core, "ExecutionPolicy", lambda *args, **kwargs: original(*args,
        **{**kwargs, "provider_names": PUBLIC_PROVIDER_NAMES, "budget_policy_id": V3}))
    execution, root, result, admission, denied, attempts = complete_fixture(tmp_path, monkeypatch)
    policy = policy_manifest("us-public-model-required-paper-v2", clean_output_policy_id=None)
    atomic_json(root, "policy.json", policy)
    admission["policy_sha256"] = hashlib.sha256(json_bytes(policy)).hexdigest()
    files = [file_ref(root / "source", path.relative_to(root / "source").as_posix(), "application/source")
             for path in sorted((root / "source").rglob("*")) if path.is_file()]
    manifest = {"schema": "tiger.source.v1", "files": files, "tree_sha256": source_tree_hash(files)}
    atomic_json(root, "source-manifest.json", manifest)
    admission["source_sha256"] = hashlib.sha256(json_bytes(manifest)).hexdigest()
    return execution, root, result, admission


def test_all43stage_v3_proof_reconstructs_budget_from_sealed_policy(tmp_path, monkeypatch):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    _, root, result, admission = frozen_fixture(tmp_path, monkeypatch)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert result["raw"]["model"]["budget"]["policy_id"] == V3


@pytest.mark.parametrize("tamper", ["missing_policy", "admission_hash", "source_hash", "limits_only", "preflight_downgrade"])
def test_v3_replay_rejects_policy_identity_or_closed_selector_tamper(tmp_path, monkeypatch, tamper):
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.agent_evidence import atomic_json, read_json
    from test_required_proof_v2 import rewrite_stage
    _, root, result, admission = frozen_fixture(tmp_path, monkeypatch)
    if tamper == "missing_policy":
        (root / "policy.json").unlink()
    elif tamper == "admission_hash":
        admission["policy_sha256"] = "0" * 64
    elif tamper == "source_hash":
        admission["source_sha256"] = "0" * 64
    elif tamper == "limits_only":
        policy = read_json(root, "policy.json")
        policy["request_budget"]["max_tokens"] = 900000
        atomic_json(root, "policy.json", policy)
        admission["policy_sha256"] = hashlib.sha256(json_bytes(policy)).hexdigest()
    else:
        rewrite_stage(root, "daily.preflight", lambda inp, out: inp["policy"]["fields"].pop("budget_policy_id"))
    with pytest.raises(EvidenceInvalid):
        verify_required_model_v2(root, result, admission)


@pytest.mark.parametrize("legacy", [True, False])
def test_rehashed_pipeline_policy_must_equal_complete_frozen_execution(tmp_path, monkeypatch, legacy):
    from test_required_proof_v2 import complete_fixture, rewrite_stage
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    if legacy:
        execution, root, result, admission, _, _ = complete_fixture(tmp_path, monkeypatch)
    else:
        execution, root, result, admission = frozen_fixture(tmp_path, monkeypatch)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    original = normalize(execution.policy)
    changed = {
        "budget_policy_id": V3 if legacy else None,
        "policy_id": "tiger.public-model-required-premarket-paper-policy.v2",
        "model_mode": "disabled", "provider_names": ["stooq"],
        "search_source_names": [], "market_timezone": "UTC", "unexpected": True,
    }
    for field, value in changed.items():
        modified = deepcopy(original)
        if field == "budget_policy_id" and not legacy:
            modified["fields"].pop(field)
        else:
            modified["fields"][field] = value
        rewrite_stage(root, "pipeline.prepare", lambda inp, out: inp.update(policy=modified))
        with pytest.raises(EvidenceInvalid, match="pipeline_frozen_execution_policy_binding"):
            verify_required_model_v2(root, result, admission)
        rewrite_stage(root, "pipeline.prepare", lambda inp, out: inp.update(policy=deepcopy(original)))
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
