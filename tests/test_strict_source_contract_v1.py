"""SYNTHETIC_TEST strict release source closure; no network or model calls."""

import pytest

from trading_system import agent_runtime as runtime
from trading_system.agent_evidence import (
    EvidenceInvalid, atomic_json, digest, json_bytes, read_json, verify_events,
)
from trading_system.llm.required_proof_v2 import verify_required_model_v2
from trading_system.redline.execution import daily_stage_contract


def _prune_and_rehash(root, admission, retained):
    manifest = read_json(root, "source-manifest.json")
    for ref in manifest["files"]:
        if ref["path"] not in retained:
            (root / "source" / ref["path"]).unlink()
    manifest["files"] = [ref for ref in manifest["files"] if ref["path"] in retained]
    manifest["tree_sha256"] = runtime.source_tree_hash(manifest["files"])
    atomic_json(root, "source-manifest.json", manifest)
    admission["source_sha256"] = digest((root / "source-manifest.json").read_bytes())
    return manifest


def test_strict_replay_rejects_same_independent_seven_file_pruning_counterexample(tmp_path, monkeypatch):
    from test_clean_strict_policy_replay_v2 import strict_fixture
    _, root, result, admission, _ = strict_fixture(tmp_path, monkeypatch)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    keep = {"trading_system/" + name for name in (
        "execution_policy.py", "exit_engine.py", "simulator.py", "config.py",
        "gate.py", "currency.py", "calendar.py")}
    manifest = _prune_and_rehash(root, admission, keep)
    assert len(manifest["files"]) == 7
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid):
        verify_required_model_v2(root, result, admission)


def _copy_release(target):
    for name in runtime.executable_files(runtime.SOURCE_ROOT):
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((runtime.SOURCE_ROOT / name).read_bytes())
    return target


def _freeze_policy(run, *, strict=True, budget_v3=True):
    from trading_system.llm.clean_output import CLEAN_OUTPUT_POLICY_V1
    from trading_system.llm.request_budget import BUDGET_POLICY_V3
    run.mkdir()
    policy = runtime.policy_manifest("us-public-model-required-paper-v2",
        budget_policy_id=BUDGET_POLICY_V3 if budget_v3 else None,
        clean_output_policy_id=CLEAN_OUTPUT_POLICY_V1 if strict else None)
    atomic_json(run, "policy.json", policy)
    return policy


def _no_git(monkeypatch):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError("SYNTHETIC_TEST no Git access")
    monkeypatch.setattr(runtime.subprocess, "run", unavailable)
    return calls


def test_canonical_release_inventory_matches_trusted_static_registry():
    from trading_system.source_contract_v1 import (REQUIRED_SOURCE_PATHS,
        OPTIONAL_SOURCE_PATHS, STRICT_SOURCE_CONTRACT_V1, validate_source_paths)
    actual = runtime.executable_files(runtime.SOURCE_ROOT)
    validate_source_paths(actual, contract_id=STRICT_SOURCE_CONTRACT_V1)
    assert set(actual) == REQUIRED_SOURCE_PATHS | (set(actual) & OPTIONAL_SOURCE_PATHS)
    assert OPTIONAL_SOURCE_PATHS == {"events_calendar.json"}
    assert {"main.py", "requirements.txt", "trading_system/source_contract_v1.py"} <= REQUIRED_SOURCE_PATHS


def test_static_contract_rejects_each_missing_release_path_independently():
    from trading_system.source_contract_v1 import (REQUIRED_SOURCE_PATHS,
        STRICT_SOURCE_CONTRACT_V1, validate_source_paths)
    complete = sorted(REQUIRED_SOURCE_PATHS)
    validate_source_paths(complete, contract_id=STRICT_SOURCE_CONTRACT_V1)
    for missing in complete:
        with pytest.raises(ValueError):
            validate_source_paths([name for name in complete if name != missing],
                                  contract_id=STRICT_SOURCE_CONTRACT_V1)


@pytest.mark.parametrize("mutation", ["empty", "extra", "duplicate", "nonstring", "unknown_contract"])
def test_static_contract_rejects_empty_extra_duplicate_and_unregistered_inventory(mutation):
    from trading_system.source_contract_v1 import (REQUIRED_SOURCE_PATHS,
        STRICT_SOURCE_CONTRACT_V1, validate_source_paths)
    paths = sorted(REQUIRED_SOURCE_PATHS)
    contract = STRICT_SOURCE_CONTRACT_V1
    if mutation == "empty": paths = []
    elif mutation == "extra": paths.append("trading_system/UNREGISTERED.py")
    elif mutation == "duplicate": paths.append(paths[0])
    elif mutation == "nonstring": paths.append(False)
    else: contract = "tiger.strict-source-inventory.unregistered"
    with pytest.raises(ValueError): validate_source_paths(paths, contract_id=contract)


@pytest.mark.parametrize("missing", ["main.py", "requirements.txt", "scripts/tiger-agent.mjs",
    "bundles/trading/agent-workflow.schema.json", "bundles/trading/capabilities/required-v2-contract.mjs",
    "trading_system/llm/clean_output.py", "trading_system/source_contract_v1.py",
    "trading_system/redline/model_diagnostics.py"])
def test_rehashed_single_file_pruning_cannot_preserve_a_strict_replay(tmp_path, monkeypatch, missing):
    from test_clean_strict_policy_replay_v2 import strict_fixture
    _, root, result, admission, _ = strict_fixture(tmp_path, monkeypatch)
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    retained = {row["path"] for row in read_json(root, "source-manifest.json")["files"]} - {missing}
    _prune_and_rehash(root, admission, retained)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    with pytest.raises(EvidenceInvalid): verify_required_model_v2(root, result, admission)


@pytest.mark.parametrize("change", ["main.py", "requirements.txt", "scripts/tiger-agent.mjs",
    "bundles/trading/agent-workflow.schema.json", "trading_system/llm/clean_output.py",
    "trading_system/source_contract_v1.py", "extra_module"])
def test_strict_freeze_refuses_incomplete_or_drifted_source_before_copy_or_worker(tmp_path, monkeypatch, change):
    source = _copy_release(tmp_path / "source")
    run = tmp_path / "new-run"
    _freeze_policy(run)
    calls = _no_git(monkeypatch)
    if change == "extra_module":
        (source / "trading_system/UNREGISTERED.py").write_text('"SYNTHETIC_TEST drift"\n')
    else:
        (source / change).unlink()
    with pytest.raises(EvidenceInvalid): runtime.freeze_source(run, source)
    assert calls == []
    assert not (run / "source").exists()
    assert not (run / "source-manifest.json").exists()


@pytest.mark.parametrize("present", [False, True])
def test_strict_freeze_accepts_exact_release_and_explicit_optional_calendar(tmp_path, monkeypatch, present):
    from trading_system.source_contract_v1 import (REQUIRED_SOURCE_PATHS,
        STRICT_SOURCE_CONTRACT_V1)
    source = _copy_release(tmp_path / "source")
    if present:
        (source / "events_calendar.json").write_bytes(json_bytes({"macro": {}, "earnings": {}}))
    run = tmp_path / "new-run"
    _freeze_policy(run)
    _no_git(monkeypatch)
    manifest = runtime.freeze_source(run, source)
    assert manifest["source_contract_id"] == STRICT_SOURCE_CONTRACT_V1
    assert {ref["path"] for ref in manifest["files"]} == (
        REQUIRED_SOURCE_PATHS | ({"events_calendar.json"} if present else set()))
    assert runtime.verify_source(run) == manifest


@pytest.mark.parametrize("policy", ["absent", "legacy300", "old_v3_json"])
def test_legacy_minimal_source_freeze_and_verification_keep_original_contract(tmp_path, monkeypatch, policy):
    source = tmp_path / "minimal"
    source.mkdir()
    (source / "main.py").write_text('"SYNTHETIC_TEST minimal historical fixture"\n')
    (source / "requirements.txt").write_text("")
    run = tmp_path / "historical-run"
    if policy == "absent": run.mkdir()
    else: _freeze_policy(run, strict=False, budget_v3=policy == "old_v3_json")
    _no_git(monkeypatch)
    manifest = runtime.freeze_source(run, source)
    assert "source_contract_id" not in manifest
    assert len(manifest["files"]) == 2
    assert runtime.verify_source(run) == manifest


@pytest.mark.parametrize("policy", [None, [], {"execution": {}},
    {"profile": "us-public-model-required-paper-v2", "execution": {"clean_output_policy_id": "unregistered"}}])
def test_present_malformed_policy_cannot_silently_freeze_as_legacy(tmp_path, monkeypatch, policy):
    source = _copy_release(tmp_path / "source")
    run = tmp_path / "new-run"
    run.mkdir()
    atomic_json(run, "policy.json", policy)
    calls = _no_git(monkeypatch)
    with pytest.raises(EvidenceInvalid): runtime.freeze_source(run, source)
    assert calls == [] and not (run / "source").exists()


@pytest.mark.parametrize("identity", [None, "tiger.strict-source-inventory.unregistered"])
def test_strict_source_manifest_cannot_select_a_missing_or_unregistered_contract(tmp_path, monkeypatch, identity):
    source = _copy_release(tmp_path / "source")
    run = tmp_path / "new-run"
    _freeze_policy(run)
    _no_git(monkeypatch)
    manifest = runtime.freeze_source(run, source)
    if identity is None: manifest.pop("source_contract_id")
    else: manifest["source_contract_id"] = identity
    atomic_json(run, "source-manifest.json", manifest)
    with pytest.raises(EvidenceInvalid): runtime.verify_source(run)
