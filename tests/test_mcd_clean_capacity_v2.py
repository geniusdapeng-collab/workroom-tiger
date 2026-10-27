"""MC-010 deterministic source probes; synthetic fixtures, no model/DB/network.

Unchanged AST nodes of selected pure helpers run with standard-library
dependencies only. Relative required_schemas imports resolve to its exact local
source; product exception classes are adapted to ProbeRejected. These tests
cover codec/policy contracts, not the complete business chain or real capacity.
Run from the exact candidate repository root with the wrapper-bound Python.
"""
import ast
import builtins
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

ROOT = Path.cwd()
LLM = ROOT / "trading_system/llm"


class ProbeRejected(RuntimeError):
    def __init__(self, code, **metadata):
        super().__init__(code)
        self.code, self.metadata = code, metadata


def plain_module(name):
    """The schemas/budget modules have only standard-library imports."""
    path = LLM / name
    namespace = {"__name__": "mcd_probe_" + path.stem}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace


def pure_nodes(name, functions, assignments=(), *, schemas=None):
    path = LLM / name
    tree = ast.parse(path.read_text(), filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id in assignments for target in node.targets):
            selected.append(node)
    original_import = builtins.__import__

    def local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and name == "required_schemas" and schemas is not None:
            return SimpleNamespace(**schemas)
        assert level == 0 and name in {"copy", "__future__"}, "Unexpected pure-helper import: " + name
        return original_import(name, globals, locals, fromlist, level)

    namespace = {"__name__": "mcd_probe", "__package__": "mcd_probe",
        "__builtins__": {**vars(builtins), "__import__": local_import},
        "json": json, "re": re, "math": math,
        "RequiredModelFailed": ProbeRejected, "RequiredModelUnavailable": ProbeRejected}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def modules():
    schemas = plain_module("required_schemas.py")
    required = pure_nodes("required.py", {
        "_fail", "_clean_wire_ids_v2", "_clean_wire_context_v2", "clean_schema_wire_v2",
        "clean_output_wire_v2", "clean_payload_wire_v2", "_column_table_v2"},
        {"CLEAN_WIRE_V2", "_CLEAN_WIRE_FIELDS_V2"}, schemas=schemas)
    transport = pure_nodes("deepseek_required.py", {
        "_fail", "_canonical", "_finite_number", "_json_equal", "_check_schema",
        "_matches_type", "_matches", "_schema_child_path", "_schema_mismatch", "validate_schema",
        "build_chat_request", "stage_reasoning_effort", "stage_thinking_mode"},
        {"MODEL", "_SCHEMA_KEYS", "_TYPES"})
    return required, transport, schemas


@pytest.mark.parametrize("upgrade,stage,thinking,effort", [
    (True, "clean.llm_semantic", "disabled", "low"),
    (True, "tech.sentiment", "enabled", "high"),
    (True, "tech.risk", "enabled", "high"),
    (True, "research.candidates", "enabled", "high"),
    (True, "decision.debate", "enabled", "high"),
    (False, "clean.llm_semantic", "enabled", "high"),
])
def test_actual_pure_request_policy_preserves_v1_and_other_semantic_stages(
        modules, upgrade, stage, thinking, effort):
    _, transport, _ = modules
    kwargs = dict(system="SYNTHETIC_TEST JSON", user="SYNTHETIC_TEST", schema_hint={},
        schema={"type": "object"}, reasoning_effort=transport["stage_reasoning_effort"](stage, upgrade=upgrade))
    # The old actual builder has no stage switch and sends enabled. Preserve
    # that real red observation rather than requiring a nonexistent old helper.
    if "stage_thinking_mode" in transport:
        kwargs["thinking_mode"] = transport["stage_thinking_mode"](stage, upgrade=upgrade)
    request = transport["build_chat_request"](**kwargs)
    assert request["thinking"] == {"type": thinking}
    assert request["reasoning_effort"] == effort
    assert request["max_tokens"] == 16384
    assert request["model"] == "deepseek-flash"


def fixture(count):
    ids = [f"synthetic_{index}" for index in range(count)]
    rows = [[identity, f"SYNTHETIC_TEST title {index}", f"https://example.invalid/{index}",
             None if index % 2 else "2026-10-27"] for index, identity in enumerate(ids)]
    selected = [{**dict(zip(["id", "title", "url", "published_at"], row)),
        "content_parts": {"columns": ["part_id", "text", "is_quoteable"],
            "rows": [["p0", f"SYNTHETIC_TEST d0 w0 原文{index}😀", True], ["p1", "\n<literal>\t", False]]},
        "content_characters": 999, "provider_content_complete": False,
        "opaque_metadata": {"id_like_prose": "d0 w0 synthetic_1"}}
        for index, row in enumerate(rows[:8])]
    return {"documents": selected, "all_source_index": {
        "columns": ["id", "title", "url", "published_at"], "rows": rows}}


@pytest.mark.parametrize("count", [0, 1, 8, 9, 55, 56])
def test_exact_payload_roundtrip_preserves_full_parts_and_global_registry(modules, count):
    required, _, _ = modules
    payload = fixture(count)
    before = deepcopy(payload)
    codec = required.get("clean_payload_wire_v2")
    assert callable(codec), "old clean path has no lossless metadata-join wire"
    wire = codec(payload)
    assert codec(wire, decode=True) == before == payload
    assert wire["all_source_index"]["rows"] == [[f"d{i}", *row]
        for i, row in enumerate(payload["all_source_index"]["rows"])]
    index = wire["documents"]["columns"].index("content_parts") if count else None
    for position, row in enumerate(wire["documents"]["rows"]):
        assert row[index] == payload["documents"][position]["content_parts"]


@pytest.mark.parametrize("text", ["d0 w0 synthetic_1", "SYNTHETIC_TEST 中文😀", "x" * 600])
def test_schema_and_answer_wire_are_bijective_with_all_original_semantic_constraints(modules, text):
    required, transport, schemas = modules
    ids = ["synthetic_0", "synthetic_1", "synthetic_2"]
    schema = schemas["clean_schema_v2"](ids[:2], ["memory"], ["SMH"],
        quote_part_ids={"synthetic_0": ["p0"], "synthetic_1": ["p1"]}, all_doc_ids=ids)
    output = {"documents": [{"id": identity, "tickers": ["AAA"], "sentiment": "mixed",
        "sentiment_score": -.25, "events": ["other"], "summary_zh": text,
        "relevance": .5, "chain_ids": ["memory"], "sector_etfs": ["SMH"],
        "provenance": {"evidence_kind": "commentary", "origin_id": ids[-1],
            "origin_note": text, "time_role": "unknown"},
        "claims": [{"statement": text, "quote_part_id": f"p{index}",
            "claim_key": f"synthetic_claim_{index}", "time_role": "background"}]}
        for index, identity in enumerate(ids[:2])]}
    wire_schema = required.get("clean_schema_wire_v2")
    wire_output = required.get("clean_output_wire_v2")
    assert callable(wire_schema) and callable(wire_output), "old clean response repeats full field/source names"
    original_schema = deepcopy(schema)
    transport["validate_schema"](output, schema)
    encoded_schema, encoded = wire_schema(schema, ids), wire_output(output, ids)
    transport["validate_schema"](encoded, encoded_schema)
    assert wire_schema(encoded_schema, ids, decode=True) == original_schema == schema
    assert wire_output(encoded, ids, decode=True) == output
    assert encoded["d"][0]["z"] == encoded["d"][0]["p"]["n"] == text
    for change in ("foreign_part", "missing_claim_key", "unknown_origin"):
        bad = deepcopy(encoded)
        if change == "foreign_part": bad["d"][0]["c"][0]["q"] = "p1"
        elif change == "missing_claim_key": del bad["d"][0]["c"][0]["k"]
        else: bad["d"][0]["p"]["o"] = "d999"
        with pytest.raises(ProbeRejected):
            transport["validate_schema"](bad, encoded_schema)


@pytest.mark.parametrize("change", ["alias", "unknown_policy", "short_row", "duplicate_identity"])
def test_noncanonical_payload_inverse_is_rejected(modules, change):
    required, _, _ = modules
    codec = required.get("clean_payload_wire_v2")
    assert callable(codec)
    wire = codec(fixture(2))
    if change == "alias": wire["all_source_index"]["rows"][0][0] = "d999"
    elif change == "unknown_policy": wire["clean_wire"]["schema"] += "unknown"
    elif change == "short_row": wire["documents"]["rows"][0].pop()
    else: wire["all_source_index"]["rows"][1][1] = wire["all_source_index"]["rows"][0][1]
    with pytest.raises((ProbeRejected, ValueError)):
        codec(wire, decode=True)


def test_wire_helpers_are_consumed_by_clean_and_both_real_and_replay_request_builders():
    required_tree = ast.parse((LLM / "required.py").read_text())
    clean = next(node for node in required_tree.body if isinstance(node, ast.FunctionDef) and node.name == "clean_v2")
    calls = [node.func.id for node in ast.walk(clean) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    for name in ("clean_payload_wire_v2", "clean_schema_wire_v2", "clean_output_wire_v2", "validate_schema", "_coverage"):
        assert name in calls, "clean_v2 must actually consume " + name
    for file in ("deepseek_required.py", "required_proof_v2.py"):
        tree = ast.parse((LLM / file).read_text())
        frames = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "build_chat_request"]
        assert frames
        assert all(any(keyword.arg == "thinking_mode" and isinstance(keyword.value, ast.Call)
            and isinstance(keyword.value.func, ast.Name) and keyword.value.func.id == "stage_thinking_mode"
            for keyword in frame.keywords) for frame in frames)


def test_v1_prefix_semantic_schema_and_budget_limits_are_preserved(modules):
    _, transport, _ = modules
    prefix = (LLM / "required.py").read_bytes()[:19031]
    assert hashlib.sha256(prefix).hexdigest() == "f2cd003fbe8a87f42aa6dd5c62b7d5cbacd6ecaa6826a889f46275f3452777b1"
    assert hashlib.sha256((LLM / "required_schemas.py").read_bytes()).hexdigest() == \
        "7896f1c9ec8237e95415645c32f4db188a094fa1327f2a8e1a527bd105795e71"
    budget = plain_module("request_budget.py")
    assert budget["LIMITS"] == {"max_chat_calls": 40, "max_tokens": 300000,
        "max_search_calls": 60, "max_fetch_calls": 20, "max_remote_requests": 120,
        "max_cost_usd": 1.0, "deadline_seconds": 2400}
    request = transport["build_chat_request"](system="SYNTHETIC_TEST", user="SYNTHETIC_TEST",
        schema_hint={}, schema={"type": "object"})
    assert request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "high"
    guard = budget["BudgetGuard"](clock=lambda: 0)
    call_id = "a" * 32
    reservation = guard.reserve("chat", call_id, request)
    assert reservation["output_token_upper_bound"] == 16384
    guard.mark_unknown(call_id)
    assert guard.snapshot()["reserved_tokens"] > 0
    with pytest.raises(budget["BudgetRefused"], match="UNKNOWN_USAGE_FREEZES_BUDGET"):
        guard.reserve("chat", "b" * 32, request)
