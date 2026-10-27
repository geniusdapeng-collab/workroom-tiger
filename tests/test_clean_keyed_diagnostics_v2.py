"""Diagnostic locations remain schema-owned and value-free; SYNTHETIC_TEST."""
from copy import deepcopy
import json

import pytest

from trading_system.llm import required
from trading_system.llm.deepseek_required import validate_schema
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_clean_keyed_wire_v2 import contract
from test_clean_capacity_wire_v2 import annotations


@pytest.mark.parametrize("mutation,path,keyword", [
    ("own_quote", "/d/d0/c/0/q", "enum"),
    ("summary", "/d/d0/z", "maxLength"),
    ("origin", "/d/d0/p/n", "maxLength"),
    ("statement", "/d/d0/c/0/s", "maxLength"),
    ("score", "/d/d0/v", "type"),
    ("missing_key", "/d", "required"),
    ("missing_summary", "/d/d0", "required"),
    ("extra_identity", "/d/d0", "additionalProperties"),
    ("untrusted_key", "/d", "additionalProperties"),
])
def test_single_failed_keyed_branch_locates_only_trusted_schema_names(mutation, path, keyword):
    payload, schema, ids, batch = contract(2)
    value = required.clean_keyed_output_v2(annotations(payload), ids, batch_ids=batch)
    before = deepcopy(value)
    secret = "SYNTHETIC_TEST_never_emit_body_or_actual_extra_key"
    row = value["d"]["d0"]
    if mutation == "own_quote": row["c"][0]["q"] = "p0"
    elif mutation == "summary": row["z"] += secret
    elif mutation == "origin": row["p"]["n"] += secret
    elif mutation == "statement": row["c"][0]["s"] += secret
    elif mutation == "score": row["v"] = secret
    elif mutation == "missing_key": del value["d"]["d1"]
    elif mutation == "missing_summary": del row["z"]
    elif mutation == "extra_identity": row["i"] = secret
    else: value["d"][secret] = deepcopy(value["d"]["d1"])
    changed = deepcopy(value)
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema(value, required.clean_keyed_schema_v2(schema, ids))
    diagnostic = error.value.diagnostic
    assert diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": path, "schema_keyword": keyword}
    assert secret not in json.dumps(diagnostic) and secret not in str(error.value)
    assert value == changed and value != before


def test_multiple_failed_allof_branches_remain_parent_located_without_guessing():
    schema = {"allOf": [{"type": "string"}, {"type": "number"}]}
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema({"SYNTHETIC_TEST_unknown_key": "never emit"}, schema)
    assert error.value.diagnostic["schema_path"] == ""
    assert error.value.diagnostic["schema_keyword"] == "allOf"


def test_unresolved_oneof_never_guesses_an_invalid_value_or_branch():
    schema = {"oneOf": [{"type": "string"}, {"type": "number"}]}
    with pytest.raises(RequiredModelUnavailable) as error:
        validate_schema({"SYNTHETIC_TEST_unknown_key": "never emit"}, schema)
    assert error.value.diagnostic["schema_path"] == ""
    assert error.value.diagnostic["schema_keyword"] == "oneOf"
