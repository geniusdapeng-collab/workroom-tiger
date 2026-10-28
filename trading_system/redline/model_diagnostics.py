"""Closed, value-free diagnostics for required model failures.

This module has no transport or provider imports. Only controlled failure
codes, HTTP status, schema locations and whitelisted numeric budget accounting
may cross the error evidence boundary.
It never extracts data from exception messages or arbitrary exception fields.
"""
from __future__ import annotations

import math
import re

from . import LLMUnavailable


MODEL_IO_METHODS = frozenset({
    "model.deepseek.chat", "model.deepseek.search", "model.deepseek.fetch",
})
MODEL_FAILURE_CODES = frozenset({
    "REQUEST_BUDGET_REFUSED",
    "ACTIVE_STAGE_REQUIRED", "API_KEY_REQUIRED", "DUPLICATE_RESPONSE_JSON_KEY",
    "EMPTY_MODEL_OUTPUT", "EMPTY_OR_NONOBJECT_MODEL_OUTPUT", "EMPTY_SEARCH_CONTENT",
    "ENDPOINT_NOT_ALLOWED", "EXECUTION_REQUIRED", "HTTP_ERROR", "HTTP_INVALID",
    "INCOMPLETE_MODEL_RESPONSE", "INCONSISTENT_USAGE", "INVALID_CHAT_REQUEST",
    "INVALID_CHOICES", "INVALID_FETCH_RESPONSE", "INVALID_JSON_OBJECT_KEY",
    "INVALID_JSON_VALUE", "INVALID_RESPONSE_BYTES", "INVALID_RESPONSE_IDENTITY",
    "INVALID_RESPONSE_JSON", "INVALID_SCHEMA_ANNOTATION", "INVALID_SCHEMA_BOUND",
    "INVALID_SCHEMA_COMBINATOR", "INVALID_SCHEMA_ENUM", "INVALID_SCHEMA_LENGTH",
    "INVALID_SCHEMA_PATTERN", "INVALID_SCHEMA_PROPERTIES", "INVALID_SCHEMA_REQUIRED",
    "INVALID_SCHEMA_TYPE", "INVALID_SCHEMA_UNIQUENESS", "INVALID_SEARCH_REQUEST",
    "INVALID_SEARCH_RESPONSE", "INVALID_SEARCH_RESULT", "INVALID_USAGE",
    "NETWORK_TIMEOUT", "NETWORK_PROTOCOL_ERROR", "NETWORK_TLS_ERROR",
    "NONFINITE_RESPONSE_JSON", "NO_USABLE_SEARCH_RESULTS",
    "OUTPUT_SCHEMA_MISMATCH", "REQUEST_TOO_LARGE", "RESPONSE_CLOSE_FAILURE",
    "RESPONSE_DEADLINE", "RESPONSE_DEPTH_LIMIT", "RESPONSE_IDENTITY_REUSED_OR_UNSAFE",
    "RESPONSE_MODEL_MISMATCH", "RESPONSE_NOT_OBJECT", "RESPONSE_OBJECT_MISMATCH",
    "RESPONSE_TOO_LARGE", "SOURCE_TITLE_LIMIT", "SOURCE_TITLE_REQUIRED",
    "RESPONSES_NOT_COMPLETED", "RESPONSES_INVALID_CREATED_AT", "RESPONSES_ERROR_PRESENT",
    "RESPONSES_INCOMPLETE_DETAILS_PRESENT", "RESPONSES_INVALID_OUTPUT_TYPE",
    "RESPONSES_INVALID_OUTPUT_COUNT", "RESPONSES_INVALID_OUTPUT_ITEM",
    "RESPONSES_INVALID_MESSAGE_FIELDS", "RESPONSES_UNEXPECTED_OUTPUT_TYPE",
    "RESPONSES_MESSAGE_NOT_COMPLETED", "RESPONSES_INVALID_MESSAGE_ROLE",
    "RESPONSES_INVALID_MESSAGE_PHASE", "RESPONSES_INVALID_CONTENT_TYPE",
    "RESPONSES_INVALID_CONTENT_COUNT", "RESPONSES_INVALID_CONTENT_PART",
    "RESPONSES_INVALID_CONTENT_FIELDS", "RESPONSES_UNEXPECTED_CONTENT_TYPE",
    "RESPONSES_INVALID_ANNOTATIONS", "RESPONSES_INVALID_LOGPROBS",
    "TRANSPORT_FAILURE", "TRUSTED_CONFIGURATION_MISMATCH", "UNEXPECTED_CONTENT_ENCODING",
    "UNEXPECTED_CONTENT_TYPE", "UNSAFE_FETCH_URL", "UNSAFE_OUTPUT_SCHEMA",
    "UNSAFE_RESPONSE_FINGERPRINT", "UNSUPPORTED_OUTPUT_SCHEMA", "USAGE_REQUIRED",
})
SCHEMA_FAILURE_KEYWORDS = frozenset({
    "type", "const", "enum", "allOf", "anyOf", "oneOf", "required",
    "additionalProperties", "minItems", "maxItems", "uniqueItems", "items",
    "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "depth",
})
_POINTER = re.compile(r"(?:/(?:[A-Za-z_][A-Za-z0-9_.:-]{0,127}|[0-9]{1,10}|\*))*")


def validate_model_diagnostic(value: object) -> dict:
    """Return a detached closed diagnostic or reject without quoting its data."""
    fields = {"schema", "provider", "code", "http_status", "schema_path", "schema_keyword"}
    version2 = (type(value) is dict and type(value.get("schema")) is str
                and value["schema"] == "tiger.model-failure.v2")
    if version2:
        fields = fields | {"provider_error"}
    if (type(value) is not dict or set(value) - fields
            or not {"schema", "provider", "code"}.issubset(value)
            or value["schema"] != ("tiger.model-failure.v2" if version2 else "tiger.model-failure.v1")
            or value["provider"] != "deepseek"
            or type(value["code"]) is not str or value["code"] not in MODEL_FAILURE_CODES):
        raise ValueError("Invalid model failure diagnostic")
    if value["code"] == "HTTP_ERROR":
        if type(value.get("http_status")) is not int or not 100 <= value["http_status"] <= 599:
            raise ValueError("Invalid model failure HTTP status")
    elif "http_status" in value:
        raise ValueError("Unexpected model failure HTTP status")
    if version2:
        detail = value.get("provider_error")
        if (any(type(key) is not str for key in value) or type(value["provider"]) is not str
                or value["code"] != "HTTP_ERROR" or value.get("http_status") not in (400, 422)
                or type(detail) is not dict or set(detail) != {"schema", "category", "body_state"}
                or any(type(key) is not str for key in detail)
                or type(detail["schema"]) is not str or detail["schema"] != "tiger.responses-provider-error.v1"
                or type(detail["category"]) is not str or detail["category"] not in
                    ("schema", "tool_choice", "parameters", "model", "request", "unknown")
                or type(detail["body_state"]) is not str or detail["body_state"] not in
                    ("parsed", "malformed", "too_large", "unavailable")
                or detail["body_state"] != "parsed" and detail["category"] != "unknown"):
            raise ValueError("Invalid Responses provider error diagnostic")
    located = {"schema_path", "schema_keyword"}.intersection(value)
    if located:
        path, keyword = value.get("schema_path"), value.get("schema_keyword")
        if (located != {"schema_path", "schema_keyword"}
                or value["code"] != "OUTPUT_SCHEMA_MISMATCH"
                or type(path) is not str or len(path) > 512 or _POINTER.fullmatch(path) is None
                or type(keyword) is not str or keyword not in SCHEMA_FAILURE_KEYWORDS):
            raise ValueError("Invalid model failure schema location")
    return {**value, **({"provider_error": dict(value["provider_error"])} if version2 else {})}


def validate_model_failure_accounting(value: object) -> dict:
    """Whitelist numbers and fixed labels, never failed response text or keys."""
    fields = {"schema", "call_id", "request_sha256", "kind", "status", "usage",
              "counts", "actual_tokens", "reserved_tokens", "estimated_charged_usd",
              "estimated_reserved_usd", "frozen", "finish_reason"}
    invalid = ValueError("Invalid model failure accounting")
    version4 = type(value) is dict and value.get("schema") == "tiger.model-call-accounting.v4"
    version3 = type(value) is dict and type(value.get("schema")) is str and value["schema"] in (
        "tiger.model-call-accounting.v3", "tiger.model-call-accounting.v4")
    if version3:
        fields = fields | {"budget_policy_id"}
    if version4:
        fields = (fields - {"finish_reason"}) | {"output_policy_id", "response_status", "native_usage"}
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or set(value) != fields
            or type(value["schema"]) is not str or value["schema"] != ("tiger.model-call-accounting.v4" if version4 else
                "tiger.model-call-accounting.v3" if version3 else "tiger.model-call-accounting.v2")
            or version3 and (type(value["budget_policy_id"]) is not str
                or value["budget_policy_id"] not in ("tiger.budget-policy.v3", "tiger.budget-policy.v4"))
            or version4 and value["budget_policy_id"] != "tiger.budget-policy.v3"
            or type(value["call_id"]) is not str or re.fullmatch(r"[a-f0-9]{32}", value["call_id"]) is None
            or type(value["request_sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", value["request_sha256"]) is None
            or type(value["kind"]) is not str or value["kind"] not in ("chat", "search", "fetch")
            or type(value["status"]) is not str or value["status"] not in ("settled", "unknown")
            or type(value["frozen"]) is not bool):
        raise invalid
    from ..llm.request_budget import policy_limits
    limits = policy_limits(value["budget_policy_id"] if version3 else None)
    token_limit = limits["max_tokens"]
    counts = value["counts"]
    if (type(counts) is not dict or any(type(key) is not str for key in counts)
            or set(counts) != {"chat", "search", "fetch"}
            or any(type(counts[k]) is not int or not 0 <= counts[k] <= bound
                   for k, bound in ((kind, limits[f"max_{kind}_calls"])
                                    for kind in ("chat", "search", "fetch")))
            or sum(counts.values()) > limits["max_remote_requests"] or counts[value["kind"]] < 1):
        raise invalid
    for key in ("actual_tokens", "reserved_tokens"):
        if type(value[key]) is not int or not 0 <= value[key] <= token_limit:
            raise invalid
    if value["actual_tokens"] + value["reserved_tokens"] > token_limit:
        raise invalid
    for key in ("estimated_charged_usd", "estimated_reserved_usd"):
        if (type(value[key]) not in (int, float) or not 0 <= value[key] <= limits["max_cost_usd"] + 1e-12
                or not math.isfinite(value[key])):
            raise invalid
    if value["estimated_charged_usd"] + value["estimated_reserved_usd"] > limits["max_cost_usd"] + 1e-12:
        raise invalid
    usage = value["usage"]
    if value["status"] == "settled" and value["kind"] == "chat":
        if (type(usage) is not dict or any(type(key) is not str for key in usage)
                or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}
                or any(type(n) is not int or not 0 <= n <= token_limit for n in usage.values())
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
                or usage["total_tokens"] > value["actual_tokens"]):
            raise invalid
    elif usage is not None:
        raise invalid
    if value["status"] == "unknown" and value["frozen"] is not True:
        raise invalid
    if version4:
        if (value["kind"] != "chat" or type(value["output_policy_id"]) is not str
                or value["output_policy_id"] not in ("tiger.clean-output.responses-schema.v1", "tiger.clean-output.responses-slots.v1")
                or type(value["response_status"]) is not str or value["response_status"] not in
                    ("in_progress", "completed", "incomplete", "failed", "unknown", "other")):
            raise invalid
        native = value["native_usage"]
        if value["status"] == "settled":
            if (type(native) is not dict or any(type(key) is not str for key in native)
                    or set(native) - {"input_tokens", "output_tokens", "total_tokens",
                    "input_tokens_details", "output_tokens_details"}
                    or not {"input_tokens", "output_tokens", "total_tokens"}.issubset(native)
                    or native["input_tokens"] != usage["prompt_tokens"]
                    or native["output_tokens"] != usage["completion_tokens"]
                    or native["total_tokens"] != usage["total_tokens"]
                    or any(type(native[key]) is not int for key in ("input_tokens", "output_tokens", "total_tokens"))):
                raise invalid
            native = dict(native)
            for parent, child, limit in (("input_tokens_details", "cached_tokens", "input_tokens"),
                                         ("output_tokens_details", "reasoning_tokens", "output_tokens")):
                if parent in native:
                    details = native[parent]
                    if (type(details) is not dict or any(type(key) is not str for key in details) or set(details) != {child}
                            or type(details[child]) is not int or not 0 <= details[child] <= native[limit]):
                        raise invalid
                    native[parent] = dict(details)
        elif native is not None:
            raise invalid
        return {**value, "counts": dict(counts), "usage": dict(usage) if usage is not None else None,
                "native_usage": native}
    if (value["finish_reason"] is not None and type(value["finish_reason"]) is not str
            or value["finish_reason"] not in (None, "stop", "length", "tool_calls", "content_filter", "other", "unknown")
            or value["kind"] != "chat" and value["finish_reason"] is not None):
        raise invalid
    return {**value, "counts": dict(counts), "usage": dict(usage) if usage is not None else None}


class RequiredModelUnavailable(LLMUnavailable):
    """A transport-created failure carrying only prevalidated safe metadata."""

    def __init__(self, code: str, *, http_status: int | None = None,
                 schema_path: str | None = None, schema_keyword: str | None = None,
                 provider_error: dict | None = None):
        diagnostic = {"schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
        if provider_error is not None:
            diagnostic.update(schema="tiger.model-failure.v2", provider_error=provider_error)
        if http_status is not None:
            diagnostic["http_status"] = http_status
        if schema_path is not None:
            diagnostic["schema_path"] = schema_path
        if schema_keyword is not None:
            diagnostic["schema_keyword"] = schema_keyword
        self._diagnostic = validate_model_diagnostic(diagnostic)
        self._accounting = None
        label = f"HTTP_{http_status}" if code == "HTTP_ERROR" else code
        super().__init__(f"DeepSeek required: {label}")

    @property
    def diagnostic(self) -> dict:
        return validate_model_diagnostic(self._diagnostic)

    def bind_accounting(self, value: dict) -> None:
        safe = validate_model_failure_accounting(value)
        if self._accounting is not None and self._accounting != safe:
            raise ValueError("Conflicting model failure accounting")
        self._accounting = safe

    @property
    def accounting(self) -> dict | None:
        return (None if self._accounting is None else
                validate_model_failure_accounting(self._accounting))


def model_failure_diagnostic(error: BaseException) -> dict | None:
    """Ignore generic failures, subclasses and malformed metadata entirely."""
    if type(error) is not RequiredModelUnavailable:
        return None
    try:
        return error.diagnostic
    except ValueError:
        # The original failure remains the outcome; malformed extra metadata is
        # not evidence and cannot replace it or enter persisted output.
        return None


def model_failure_accounting(error: BaseException) -> dict | None:
    """Only the exact trusted error can supply validated numeric accounting."""
    if type(error) is not RequiredModelUnavailable:
        return None
    try:
        return error.accounting
    except ValueError:
        return None
