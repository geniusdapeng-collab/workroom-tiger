"""Versioned closed output protocols for v2 semantic cleaning.

The historical strict-function beta enforces source keys, field types and enums.
Its documented schema subset does not support string/array length bounds; those
and uniqueness remain mandatory in the unchanged full local keyed and decoded
semantic validators. The provider projection is not a semantic acceptance gate.
No function is executed, no tool result is sent and no second model turn exists.
The separately selected Responses protocol sends the complete existing keyed
schema, including every bound and source-local quote binding, without projection.
Both protocols retain the complete unchanged local semantic acceptance gates.

Official contract checked 2026-10-27:
https://api-docs.deepseek.com/guides/tool_calls/
https://api-docs.deepseek.com/api/create-chat-completion/
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

CLEAN_OUTPUT_POLICY_V1 = "tiger.clean-output.strict-function.v1"
CLEAN_OUTPUT_RESPONSES_POLICY_V1 = "tiger.clean-output.responses-schema.v1"
CLEAN_OUTPUT_RESPONSES_PATH = "/responses"
CLEAN_OUTPUT_FUNCTION = "emit_tiger_clean_v2"
CLEAN_OUTPUT_PATH = "/beta/chat/completions"
CLEAN_OUTPUT_DESCRIPTION = (
    "Return this batch's complete source-keyed semantic cleaning result. "
    "This output-only function performs no external action and has no tool result or follow-up turn.")
CLEAN_OUTPUT_INSTRUCTION = (
    "\nReturn exactly one call to emit_tiger_clean_v2 with the complete JSON result as its arguments. "
    "Do not execute a tool or add prose. Every local schema constraint is still mandatory.")
_LOCAL_ONLY = frozenset({"minLength", "maxLength", "minItems", "maxItems", "uniqueItems"})
_SERVER_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "anyOf"})
_DOCUMENT_FIELDS = frozenset({"t", "s", "v", "e", "z", "r", "h", "f", "p", "c"})


class CleanOutputInvalid(ValueError):
    """A fixed diagnostic without interpolating source or model data."""


def _invalid():
    raise CleanOutputInvalid("INVALID_STRICT_CLEAN_CONTRACT")


def _canonical(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _invalid()


def clean_output_policy(execution) -> str | None:
    """Absent means historical JSON; only the reviewed explicit policy opts in."""
    value = getattr(getattr(execution, "policy", None), "clean_output_policy_id", None)
    if value is None:
        return None
    if (type(value) is not str or value not in (CLEAN_OUTPUT_POLICY_V1, CLEAN_OUTPUT_RESPONSES_POLICY_V1)
            or getattr(execution, "upgrade", False) is not True):
        _invalid()
    return value


def _closed_object(node):
    return (type(node) is dict and set(node) == {"type", "properties", "required", "additionalProperties"}
        and node["type"] == "object" and type(node["properties"]) is dict
        and type(node["required"]) is list and len(node["required"]) == len(node["properties"])
        and set(node["required"]) == set(node["properties"]) and node["additionalProperties"] is False)


def strict_clean_schema(schema: dict) -> dict:
    """Flatten the registered keyed contract, preserving each own q binding.

    This accepts only the existing clean keyed allOf shape. Unexpected future
    constraints fail before I/O instead of silently being dropped. The only
    projected-away constraints are the documented local-only bounds above.
    """
    try:
        if not _closed_object(schema) or set(schema["properties"]) != {"d"}:
            _invalid()
        keyed = schema["properties"]["d"]
        if type(keyed) is not dict or set(keyed) != {"allOf"} or len(keyed["allOf"]) != 2:
            _invalid()
        shared, owned = keyed["allOf"]
        if (type(shared) is not dict or set(shared) != {"type", "additionalProperties"}
                or shared["type"] != "object" or not _closed_object(owned)):
            _invalid()
        common = shared["additionalProperties"]
        if not _closed_object(common) or set(common["properties"]) != _DOCUMENT_FIELDS:
            _invalid()
        common_quote = common["properties"]["c"]["items"]["properties"]["q"]
        if (common_quote != {"type": "string", "pattern": r"(?!)"}
                and (type(common_quote) is not dict or set(common_quote) != {"type", "enum"}
                or common_quote["type"] != "string" or type(common_quote["enum"]) is not list
                or not common_quote["enum"] or len(set(common_quote["enum"])) != len(common_quote["enum"])
                or any(type(part) is not str or re.fullmatch(r"p(?:0|[1-9][0-9]*)", part) is None
                       for part in common_quote["enum"]))):
            _invalid()
        flattened = deepcopy(owned)
        for reference, binding in owned["properties"].items():
            if type(reference) is not str or re.fullmatch(r"d(?:0|[1-9][0-9]*)", reference) is None:
                _invalid()
            quote = binding["properties"]["c"]["items"]["properties"]["q"]
            if binding != {"properties": {"c": {"items": {"properties": {"q": quote}}}}}:
                _invalid()
            if (quote != {"type": "string", "pattern": r"(?!)"}
                    and (type(quote) is not dict or set(quote) != {"type", "enum"}
                    or quote["type"] != "string" or type(quote["enum"]) is not list
                    or not quote["enum"] or len(set(quote["enum"])) != len(quote["enum"])
                    or any(type(part) is not str or re.fullmatch(r"p(?:0|[1-9][0-9]*)", part) is None
                           for part in quote["enum"]))):
                _invalid()
            if "enum" in quote and not set(quote["enum"]).issubset(common_quote.get("enum", [])):
                _invalid()
            row = deepcopy(common)
            if "enum" in quote:
                row["properties"]["c"]["items"]["properties"]["q"] = deepcopy(quote)
            else:
                # No legal source-local quote makes every nonempty claim array
                # impossible in the original contract. Encode that exact empty
                # set using a supported enum, without an impossible regexp.
                row["properties"]["c"] = {"type": "array", "items": {"type": "string"}, "enum": [[]]}
            flattened["properties"][reference] = row
        result = deepcopy(schema)
        result["properties"]["d"] = flattened

        def project(node):
            if type(node) is not dict or not node:
                _invalid()
            if set(node) - (_SERVER_KEYS | _LOCAL_ONLY):
                _invalid()
            if node == {"type": "null"}:
                return {"enum": [None]}
            if node == {"type": "string", "pattern": r"(?!)"}:
                _invalid()
            result = {}
            for key, value in node.items():
                if key in _LOCAL_ONLY:
                    if key == "uniqueItems":
                        if type(value) is not bool:
                            _invalid()
                    elif type(value) is not int or value < 0:
                        _invalid()
                    continue
                if key == "properties":
                    result[key] = {name: project(child) for name, child in value.items()}
                elif key == "items":
                    result[key] = project(value)
                elif key == "anyOf":
                    # The only impossible alternative is an absent source-ID
                    # set in nullable origin_id; its surviving null is exact.
                    alternatives = [project(child) for child in value
                        if child != {"type": "string", "pattern": r"(?!)"}]
                    if not alternatives:
                        _invalid()
                    if len(alternatives) == 1:
                        return alternatives[0]
                    result[key] = alternatives
                else:
                    result[key] = deepcopy(value)
            return result

        result = project(result)
        _validate_server_schema(result)
        _canonical(result)
        return result
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError):
        _invalid()


def _validate_server_schema(node, depth=0):
    """A deliberately small documented subset; no refs or arbitrary tools."""
    if (depth > 32 or type(node) is not dict or not node or set(node) - _SERVER_KEYS):
        _invalid()
    kind = node.get("type")
    if kind is not None and (type(kind) is not str or kind not in
            {"object", "string", "number", "integer", "boolean", "array"}):
        _invalid()
    if kind == "object":
        if not _closed_object(node):
            _invalid()
        for name, child in node["properties"].items():
            if type(name) is not str:
                _invalid()
            _validate_server_schema(child, depth + 1)
    elif "properties" in node or "required" in node or "additionalProperties" in node:
        _invalid()
    if kind == "array":
        if "items" not in node:
            _invalid()
        _validate_server_schema(node["items"], depth + 1)
    elif "items" in node:
        _invalid()
    if "enum" in node and (type(node["enum"]) is not list or not node["enum"]):
        _invalid()
    if "anyOf" in node:
        if set(node) != {"anyOf"} or type(node["anyOf"]) is not list or not node["anyOf"]:
            _invalid()
        for child in node["anyOf"]:
            _validate_server_schema(child, depth + 1)
    if "pattern" in node:
        if kind != "string" or type(node["pattern"]) is not str or len(node["pattern"]) > 1000:
            _invalid()
        try:
            re.compile(node["pattern"])
        except re.error:
            _invalid()
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in node and (type(node[key]) not in (int, float) or not math.isfinite(node[key])):
            _invalid()


def build_clean_request(*, system: str, user: str, schema_hint: dict, schema: dict,
                        max_tokens: int = 16384, temperature: float = 0.2) -> dict:
    """One output-only call, no stream, execution loop or hidden thinking."""
    if (type(system) is not str or not system.strip() or type(user) is not str or not user.strip()
            or type(schema_hint) is not dict or type(max_tokens) is not int or not 1 <= max_tokens <= 393216
            or type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        _invalid()
    parameters = strict_clean_schema(schema)
    return {"model": "deepseek-flash", "messages": [
        {"role": "system", "content": system + CLEAN_OUTPUT_INSTRUCTION},
        {"role": "user", "content": user + "\nLocal JSON schema (including mandatory local-only bounds):\n" +
            _canonical(schema) + "\nJSON field guidance:\n" + _canonical(schema_hint)}],
        "max_tokens": max_tokens, "temperature": temperature, "stream": False,
        "thinking": {"type": "disabled"}, "reasoning_effort": "none",
        "tools": [{"type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION,
            "strict": True, "description": CLEAN_OUTPUT_DESCRIPTION, "parameters": parameters}}],
        "tool_choice": {"type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION}}}


def clean_function_request_valid(request) -> bool:
    """Budget allowlist for exactly this finite output call, never tool loops."""
    try:
        if (type(request) is not dict or set(request) != {"model", "messages", "max_tokens", "temperature",
                "stream", "thinking", "reasoning_effort", "tools", "tool_choice"}
                or request["model"] != "deepseek-flash" or request["stream"] is not False
                or request["thinking"] != {"type": "disabled"} or request["reasoning_effort"] != "none"
                or type(request["max_tokens"]) is not int or not 1 <= request["max_tokens"] <= 393216
                or type(request["temperature"]) not in (int, float) or not math.isfinite(request["temperature"])
                or not 0 <= request["temperature"] <= 2
                or request["tool_choice"] != {"type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION}}
                or type(request["tools"]) is not list or len(request["tools"]) != 1):
            return False
        messages = request["messages"]
        if (type(messages) is not list or len(messages) != 2
                or any(type(message) is not dict or set(message) != {"role", "content"}
                    or message["role"] != role or type(message["content"]) is not str
                    or not message["content"].strip() for message, role in zip(messages, ("system", "user")))
                or not messages[0]["content"].endswith(CLEAN_OUTPUT_INSTRUCTION)):
            return False
        tool = request["tools"][0]
        if type(tool) is not dict or set(tool) != {"type", "function"} or tool["type"] != "function":
            return False
        function = tool["function"]
        if (type(function) is not dict or set(function) != {"name", "strict", "description", "parameters"}
                or function["name"] != CLEAN_OUTPUT_FUNCTION or function["strict"] is not True
                or function["description"] != CLEAN_OUTPUT_DESCRIPTION):
            return False
        parameters = function["parameters"]
        _validate_server_schema(parameters)
        if (not _closed_object(parameters) or set(parameters["properties"]) != {"d"}
                or not _closed_object(parameters["properties"]["d"])):
            return False
        for reference, row in parameters["properties"]["d"]["properties"].items():
            if (re.fullmatch(r"d(?:0|[1-9][0-9]*)", reference) is None
                    or not _closed_object(row) or set(row["properties"]) != _DOCUMENT_FIELDS):
                return False
        _canonical(request)
        return True
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError):
        return False


def build_clean_safe_response(*, response_id: str, system_fingerprint: str | None,
                              usage: dict, parsed: dict, tool_call_id: str) -> dict:
    """Pure sealed-response projection; caller validates identities and schema."""
    return {"id": response_id, "model": "deepseek-flash", "system_fingerprint": system_fingerprint,
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": tool_call_id,
                "type": "function", "function": {"name": CLEAN_OUTPUT_FUNCTION,
                    "arguments": _canonical(parsed)}}]}}], "usage": deepcopy(usage)}


def _validate_responses_clean_schema(schema: dict) -> None:
    """Admit the complete keyed shape without flattening or deleting a rule.

    The original semantic builders remain the schema's sole source. This
    structural boundary rejects arbitrary tools/schemas; the complete local
    schema validator additionally checks every keyword and bound before I/O.
    Provider acceptance of this particular schema is a separate live fact.
    """
    from .deepseek_required import _check_schema
    from ..redline import LLMUnavailable
    try:
        _check_schema(schema)
        if not _closed_object(schema) or set(schema["properties"]) != {"d"}:
            _invalid()
        keyed = schema["properties"]["d"]
        if type(keyed) is not dict or set(keyed) != {"allOf"} or len(keyed["allOf"]) != 2:
            _invalid()
        shared, owned = keyed["allOf"]
        if (type(shared) is not dict or set(shared) != {"type", "additionalProperties"}
                or shared["type"] != "object" or not _closed_object(owned)):
            _invalid()
        common = shared["additionalProperties"]
        if not _closed_object(common) or set(common["properties"]) != _DOCUMENT_FIELDS:
            _invalid()

        def quote_ids(quote):
            if quote == {"type": "string", "pattern": r"(?!)"}:
                return []
            if (type(quote) is not dict or set(quote) != {"type", "enum"}
                    or quote["type"] != "string" or type(quote["enum"]) is not list
                    or not quote["enum"] or any(type(part) is not str
                        or re.fullmatch(r"p(?:0|[1-9][0-9]*)", part) is None for part in quote["enum"])
                    or len(set(quote["enum"])) != len(quote["enum"])):
                _invalid()
            return quote["enum"]

        common_ids = quote_ids(common["properties"]["c"]["items"]["properties"]["q"])
        for reference, binding in owned["properties"].items():
            if type(reference) is not str or re.fullmatch(r"d(?:0|[1-9][0-9]*)", reference) is None:
                _invalid()
            quote = binding["properties"]["c"]["items"]["properties"]["q"]
            if (binding != {"properties": {"c": {"items": {"properties": {"q": quote}}}}}
                    or not set(quote_ids(quote)).issubset(common_ids)):
                _invalid()
        _canonical(schema)
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, LLMUnavailable):
        _invalid()


def build_clean_responses_request(*, system: str, user: str, schema_hint: dict,
                                  schema: dict, max_tokens: int = 16384,
                                  temperature: float = 0.2) -> dict:
    """One stateless, non-thinking response with the exact full keyed schema.

    Official contract checked 2026-10-27 at
    https://api-docs.deepseek.com/api/create-response/ and
    https://api-docs.deepseek.com/guides/responses_api/ . No undocumented strict
    field, tool, continuation, answer repair or hidden reasoning is requested.
    The clean stage's guidance is already complete in its original user text;
    a nonempty extra schema hint fails instead of silently being discarded.
    """
    if (type(system) is not str or not system.strip() or type(user) is not str or not user.strip()
            or type(schema_hint) is not dict or schema_hint
            or type(max_tokens) is not int or max_tokens != 16384
            or type(temperature) not in (int, float) or temperature != 0.2):
        _invalid()
    _validate_responses_clean_schema(schema)
    return {"model": "deepseek-flash", "instructions": system, "input": user,
        "reasoning": {"effort": "none"}, "max_output_tokens": max_tokens,
        "temperature": temperature, "stream": False,
        "text": {"format": {"type": "json_schema", "name": "tiger_clean_v2", "schema": deepcopy(schema)}}}


def clean_responses_request_valid(request) -> bool:
    """Budget allowlist for this single text-only Responses protocol."""
    try:
        if (type(request) is not dict or set(request) != {"model", "instructions", "input", "reasoning",
                "max_output_tokens", "temperature", "stream", "text"}
                or request["model"] != "deepseek-flash" or request["stream"] is not False
                or request["reasoning"] != {"effort": "none"}
                or type(request["max_output_tokens"]) is not int or request["max_output_tokens"] != 16384
                or type(request["temperature"]) not in (int, float) or request["temperature"] != 0.2
                or any(type(request[key]) is not str or not request[key].strip()
                       for key in ("instructions", "input"))
                or type(request["text"]) is not dict or set(request["text"]) != {"format"}):
            return False
        output = request["text"]["format"]
        if (type(output) is not dict or set(output) != {"type", "name", "schema"}
                or output["type"] != "json_schema" or output["name"] != "tiger_clean_v2"):
            return False
        _validate_responses_clean_schema(output["schema"])
        _canonical(request)
        return True
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError):
        return False


def build_clean_responses_safe_response(*, response_id: str, created_at: int,
                                       usage: dict, parsed: dict, message_id: str) -> dict:
    """Sealed native projection; JSON spacing is canonical, semantics exact.

    The caller validates observed identities/status/usage and the full schema.
    Transport SHA-256 separately binds the actual received bytes. This native
    envelope has no Chat finish reason, tool call or fabricated fingerprint.
    """
    return {"id": response_id, "object": "response", "created_at": created_at,
        "status": "completed", "model": "deepseek-flash", "output": [{"type": "message",
            "id": message_id, "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": _canonical(parsed)}]}], "usage": deepcopy(usage)}
