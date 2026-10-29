"""Array membership assertions in the supported schema subset; no model IO."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from trading_system.llm.deepseek_required import (
    DeepSeekRequiredClient, _check_schema, _matches, _schema_mismatch, validate_schema,
)
from trading_system.redline.model_diagnostics import (
    RequiredModelUnavailable, model_failure_diagnostic, validate_model_diagnostic,
)


def rejected(value, schema, *, path="", keyword="contains"):
    with pytest.raises(RequiredModelUnavailable) as caught:
        validate_schema(value, schema)
    expected = {"schema": "tiger.model-failure.v1", "provider": "deepseek",
                "code": "OUTPUT_SCHEMA_MISMATCH", "schema_path": path,
                "schema_keyword": keyword}
    assert caught.value.diagnostic == expected
    assert validate_model_diagnostic(expected) == expected
    assert model_failure_diagnostic(caught.value) == expected
    return caught.value


@pytest.mark.parametrize("value", [
    ["owned"], ["owned", "other"], ["other", "owned"],
    ["other", "owned", "owned"], [None, False, 1, "owned"],
])
def test_contains_requires_at_least_one_matching_element_without_position_or_count_limit(value):
    validate_schema(value, {"type": "array", "contains": {"const": "owned"}})


@pytest.mark.parametrize("value", [[], ["other"], [None, False, 1, {}, ["owned"]]])
def test_contains_rejects_empty_and_no_matching_element(value):
    rejected(value, {"type": "array", "contains": {"const": "owned"}})


@pytest.mark.parametrize("value", [None, True, False, 0, 1.5, "owned", {}, {"owned": 1}])
def test_contains_is_ignored_for_nonarrays_when_no_type_rule_forbids_them(value):
    validate_schema(value, {"contains": {"const": "owned"}})
    rejected(value, {"type": "array", "contains": {"const": "owned"}}, keyword="type")


def test_contains_uses_existing_json_equality_and_keeps_boolean_distinct_from_number():
    validate_schema([1.0], {"contains": {"const": 1}})
    validate_schema([{"x": [1.0, None]}], {"contains": {"const": {"x": [1, None]}}})
    rejected([True], {"contains": {"const": 1}})
    rejected([1], {"contains": {"const": True}})


def test_contains_recursively_validates_a_nested_object_and_array():
    schema = {"type": "array", "contains": {"type": "object", "required": ["refs"],
        "properties": {"refs": {"type": "array", "contains": {"const": "owned"}}}}}
    validate_schema([{}, {"refs": ["other", "owned"]}], schema)
    rejected([{}, {"refs": ["other"]}], schema)


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
def test_contains_supports_existing_combinators_in_its_child(keyword):
    children = [{"type": "integer"}, {"minimum": 2}]
    if keyword == "anyOf":
        children = [{"const": 2}, {"const": 3}]
    if keyword == "oneOf":
        children = [{"const": 2}, {"type": "string"}]
    schema = {"type": "array", "contains": {keyword: children}}
    validate_schema([False, 2], schema)
    rejected([0], schema)


@pytest.mark.parametrize("value,extra,path,keyword", [
    ([], {"minItems": 1}, "", "minItems"),
    (["owned", "other"], {"maxItems": 1}, "", "maxItems"),
    (["owned", "owned"], {"uniqueItems": True}, "", "uniqueItems"),
    (["owned", 7], {"items": {"type": "string"}}, "/1", "type"),
    ([7], {"items": {"type": "string"}}, "/0", "type"),
])
def test_contains_does_not_replace_existing_array_rules_or_error_precedence(value, extra, path, keyword):
    schema = {"type": "array", "contains": {"const": "owned"}, **extra}
    rejected(value, schema, path=path, keyword=keyword)


def test_contains_diagnostic_stays_at_the_array_and_omits_unmatched_model_values():
    secret = "untrusted-model-value"
    schema = {"type": "object", "properties": {"theses": {"type": "array", "items": {
        "type": "object", "properties": {"evidence_ids": {
            "type": "array", "contains": {"const": "owned"}}}}}}}
    value = {"theses": [{"evidence_ids": [secret, {secret: secret}]}]}
    error = rejected(value, schema, path="/theses/0/evidence_ids")
    assert secret not in json.dumps([error.diagnostic, str(error)])
    assert error.diagnostic["schema_path"] != "/theses/0/evidence_ids/0"


def test_contains_diagnostic_masks_unsafe_schema_names_and_never_names_extra_model_keys():
    unsafe = "unsafe/schema-name"
    schema = {"type": "object", "properties": {unsafe: {
        "type": "array", "contains": {"const": "owned"}}}}
    error = rejected({unsafe: ["other"]}, schema, path="/*")
    assert unsafe not in json.dumps(error.diagnostic)
    extra = {"type": "object", "additionalProperties": {
        "type": "array", "contains": {"const": "owned"}}}
    rejected({"untrusted-model-key": ["other"]}, extra, keyword="additionalProperties")


def test_contains_diagnostic_locates_one_failed_allof_child_without_exposing_a_value():
    schema = {"type": "object", "properties": {"refs": {"type": "array"}}, "allOf": [
        {"properties": {"refs": {"items": {"type": "string"}}}},
        {"properties": {"refs": {"contains": {"const": "owned"}}}},
    ]}
    rejected({"refs": ["other"]}, schema, path="/refs")


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
def test_contains_in_unresolved_alternatives_keeps_the_parent_diagnostic(keyword):
    schema = {keyword: [{"contains": {"const": "first"}}, {"contains": {"const": "second"}}]}
    rejected(["other"], schema, keyword=keyword)


@pytest.mark.parametrize("child", [None, True, False, {}, [], "schema", 1])
def test_contains_child_stays_within_the_existing_nonempty_object_schema_subset(child):
    with pytest.raises(RequiredModelUnavailable) as caught:
        validate_schema(["owned"], {"contains": child})
    assert caught.value.diagnostic["code"] == "UNSUPPORTED_OUTPUT_SCHEMA"


@pytest.mark.parametrize("schema", [
    {"contains": {"unknown": True}},
    {"contains": {"type": "array", "items": {"unknown": True}}},
    {"contains": {"const": "owned"}, "unknown": True},
    {"contains": {"const": "owned"}, "minContains": 0},
    {"contains": {"const": "owned"}, "maxContains": 2},
])
@pytest.mark.parametrize("value", [None, ["owned"]])
def test_unknown_keywords_are_rejected_even_when_contains_would_be_ignored_or_match(schema, value):
    with pytest.raises(RequiredModelUnavailable) as caught:
        validate_schema(value, schema)
    assert caught.value.diagnostic["code"] == "UNSUPPORTED_OUTPUT_SCHEMA"


def nested_contains(depth):
    schema, value = {"const": "owned"}, "owned"
    for _ in range(depth):
        schema, value = {"type": "array", "contains": schema}, [value]
    return schema, value


def test_contains_schema_recursion_keeps_the_existing_depth_32_limit():
    schema, value = nested_contains(32)
    validate_schema(value, schema)
    schema, value = nested_contains(33)
    with pytest.raises(RequiredModelUnavailable) as caught:
        validate_schema(value, schema)
    assert caught.value.diagnostic["code"] == "UNSUPPORTED_OUTPUT_SCHEMA"


def test_contains_match_recursion_does_not_bypass_the_existing_depth_64_guard():
    schema = {"type": "array", "contains": {"const": "owned"}}
    _check_schema(schema)
    assert _matches(["owned"], schema, depth=63)
    assert not _matches(["owned"], schema, depth=64)
    assert _schema_mismatch(["owned"], schema, depth=65, path="/safe") == ("/safe", "depth")


def test_contains_diagnostic_keeps_the_existing_512_character_path_limit():
    name = "a" * 127
    schema, value = {"contains": {"const": "owned"}}, ["other"]
    for _ in range(6):
        schema, value = {"properties": {name: schema}}, {name: value}
    with pytest.raises(RequiredModelUnavailable) as caught:
        validate_schema(value, schema)
    diagnostic = caught.value.diagnostic
    assert diagnostic["schema_keyword"] == "depth"
    assert len(diagnostic["schema_path"]) <= 512
    assert all(part == name for part in diagnostic["schema_path"].split("/")[1:])


def test_contains_validation_is_pure_for_shared_schemas_and_concurrent_calls():
    schema = {"type": "array", "contains": {"const": "owned"}}
    values = [["owned", "other"], ["other"], []] * 8
    original = copy.deepcopy((schema, values))

    def accepted(value):
        try:
            validate_schema(value, schema)
            return True
        except RequiredModelUnavailable as error:
            assert error.diagnostic["code"] == "OUTPUT_SCHEMA_MISMATCH"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(accepted, values)) == [True, False, False] * 8
    assert (schema, values) == original


@pytest.mark.parametrize("unsafe", [False, True])
def test_contains_schema_preflight_refuses_bad_or_unsafe_children_before_any_io(monkeypatch, unsafe):
    key = "synthetic-contains-fixture"
    for name, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": key}.items():
        monkeypatch.setenv(name, value)

    def forbidden(*args, **kwargs):
        pytest.fail("No evidence, budget or network call is allowed for an invalid schema")

    execution = SimpleNamespace(_active=SimpleNamespace(spec=SimpleNamespace(name="research.candidates")),
                                call_io=forbidden)
    client = DeepSeekRequiredClient(execution)
    monkeypatch.setattr(client, "_admit_call", forbidden)
    monkeypatch.setattr(client, "_post_json", forbidden)
    schema = {"type": "object", "properties": {"refs": {
        "type": "array", "contains": {"const": key} if unsafe else {"unknown": True}}}}
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="s", user="u", schema_hint={}, schema=schema)
    expected = "UNSAFE_OUTPUT_SCHEMA" if unsafe else "UNSUPPORTED_OUTPUT_SCHEMA"
    assert caught.value.diagnostic["code"] == expected
    assert key not in json.dumps(caught.value.diagnostic)
    assert client.describe()["remote_requests"] == 0
