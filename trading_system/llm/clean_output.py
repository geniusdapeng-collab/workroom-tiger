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
CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1 = "tiger.clean-output.responses-slots.v1"
CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1 = "tiger.clean-output.compact-lists-regeneration.v1"
CLEAN_COMPACT_LISTS_WIRE_V1 = "tiger.clean-compact-lists-wire.v1"
CLEAN_REGENERATION_POLICY_V1 = "tiger.model-regeneration.clean-schema.v1"
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
    if (type(value) is not str or value not in (CLEAN_OUTPUT_POLICY_V1, CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                                               CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1,
                                               CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1)
            or getattr(execution, "upgrade", False) is not True):
        _invalid()
    return value


def clean_compact_execution_limits(execution) -> tuple[int, int]:
    """Select source batch and complete-regeneration caps from source policy.

    v4 simplifies each model assignment to one complete source. Historical v3
    evidence retains its four-source batch and four extra generations per run.
    Numeric claims in output or evidence cannot select either policy.
    """
    from .request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4
    policy = getattr(getattr(execution, "policy", None), "budget_policy_id", None)
    if policy == BUDGET_POLICY_V4:
        return 1, 56
    if policy is None or policy == BUDGET_POLICY_V3:
        return 4, 4
    _invalid()


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


def clean_slots_schema_v1(schema: dict) -> dict:
    """Compile each source's complete contract into closed nullable slots.

    Only the keyed clean contract is accepted. Every source gets its own full
    document rules and exact own-quote enum. Every bounded array becomes N
    required s0..s(N-1) properties, each the unchanged item schema or null.
    String, numeric, enum and nullable-origin rules remain unchanged. Prefix,
    minimum and uniqueness are still checked against the original array schema
    after decoding; this provider representation is not semantic acceptance.
    """
    _validate_responses_clean_schema(schema)
    try:
        shared, owned = schema["properties"]["d"]["allOf"]
        common = shared["additionalProperties"]

        def slots(node):
            if node.get("type") == "array":
                if (set(node) != {"type", "items", "minItems", "maxItems", "uniqueItems"}
                        or type(node["minItems"]) is not int or type(node["maxItems"]) is not int
                        or not 0 <= node["minItems"] <= node["maxItems"] <= 1000
                        or node["uniqueItems"] is not True):
                    _invalid()
                item = slots(node["items"])
                properties = {f"s{index}": {"anyOf": [deepcopy(item), {"type": "null"}]}
                              for index in range(node["maxItems"])}
                return {"type": "object", "properties": properties,
                        "required": list(properties), "additionalProperties": False}
            result = {}
            for key, value in node.items():
                if key == "properties":
                    result[key] = {name: slots(child) for name, child in value.items()}
                elif key in ("anyOf", "oneOf", "allOf"):
                    result[key] = [slots(child) for child in value]
                else:
                    result[key] = deepcopy(value)
            return result

        result = deepcopy(schema)
        result["properties"]["d"] = deepcopy(owned)
        for reference, binding in owned["properties"].items():
            row = deepcopy(common)
            row["properties"]["c"]["items"]["properties"]["q"] = deepcopy(
                binding["properties"]["c"]["items"]["properties"]["q"])
            result["properties"]["d"]["properties"][reference] = slots(row)
        _validate_clean_slots_schema_v1(result)
        return result
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError):
        _invalid()


def _validate_clean_slots_schema_v1(schema: dict) -> None:
    """Closed request boundary for the new codec, independent of user text."""
    from .deepseek_required import _check_schema
    from ..redline import LLMUnavailable
    try:
        _check_schema(schema)
        def closed_tree(node):
            if node.get("type") == "object" and not _closed_object(node):
                _invalid()
            if node.get("type") == "array":
                _invalid()
            for child in node.get("properties", {}).values():
                closed_tree(child)
            for key in ("anyOf", "oneOf", "allOf"):
                for child in node.get(key, []):
                    closed_tree(child)

        closed_tree(schema)
        if not _closed_object(schema) or set(schema["properties"]) != {"d"}:
            _invalid()
        sources = schema["properties"]["d"]
        if not _closed_object(sources):
            _invalid()
        fields = {"t": 8, "e": 6, "h": None, "f": None, "c": 4}
        common = None
        for reference, row in sources["properties"].items():
            if (type(reference) is not str or re.fullmatch(r"d(?:0|[1-9][0-9]*)", reference) is None
                    or not _closed_object(row) or set(row["properties"]) != _DOCUMENT_FIELDS):
                _invalid()
            restored = deepcopy(row)
            for field, maximum in fields.items():
                array = row["properties"][field]
                if not _closed_object(array):
                    _invalid()
                size = len(array["properties"])
                if (not 1 <= size <= 1000 or maximum is not None and size != maximum
                        or set(array["properties"]) != {f"s{index}" for index in range(size)}):
                    _invalid()
                item = None
                for value in array["properties"].values():
                    if (type(value) is not dict or set(value) != {"anyOf"}
                            or type(value["anyOf"]) is not list or len(value["anyOf"]) != 2
                            or value["anyOf"][1] != {"type": "null"}):
                        _invalid()
                    if item is None:
                        item = deepcopy(value["anyOf"][0])
                    elif item != value["anyOf"][0]:
                        _invalid()
                if field in ("h", "f") and (item.get("type") != "string"
                        or set(item) != {"type", "enum"} or size != len(item["enum"])):
                    _invalid()
                restored["properties"][field] = {"type": "array", "items": item,
                    "minItems": 0, "maxItems": size, "uniqueItems": True}
            quote = restored["properties"]["c"]["items"]["properties"]["q"]
            if quote != {"type": "string", "pattern": r"(?!)"} and (
                    type(quote) is not dict or set(quote) != {"type", "enum"}
                    or quote["type"] != "string" or type(quote["enum"]) is not list or not quote["enum"]
                    or len(set(quote["enum"])) != len(quote["enum"])
                    or any(type(part) is not str or re.fullmatch(r"p(?:0|[1-9][0-9]*)", part) is None
                           for part in quote["enum"])):
                _invalid()
            # Common document semantics must be identical across sources;
            # only the source-local q selector may vary.
            restored["properties"]["c"]["items"]["properties"]["q"] = {"type": "string", "pattern": r"(?!)"}
            if common is None:
                common = restored
            elif common != restored:
                _invalid()
        _canonical(schema)
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, LLMUnavailable):
        _invalid()


def build_clean_slots_responses_request(*, system: str, user: str, schema_hint: dict,
                                        schema: dict, max_tokens: int = 16384,
                                        temperature: float = 0.2) -> dict:
    """Same finite native transport, explicitly versioned fixed-slot output."""
    if (type(system) is not str or not system.strip() or type(user) is not str or not user.strip()
            or type(schema_hint) is not dict or schema_hint
            or type(max_tokens) is not int or max_tokens != 16384
            or type(temperature) not in (int, float) or temperature != 0.2):
        _invalid()
    return {"model": "deepseek-flash", "instructions": system, "input": user,
        "reasoning": {"effort": "none"}, "max_output_tokens": max_tokens,
        "temperature": temperature, "stream": False,
        "text": {"format": {"type": "json_schema", "name": "tiger_clean_slots_v1",
                             "schema": clean_slots_schema_v1(schema)}}}


def clean_slots_responses_request_valid(request) -> bool:
    """Admit only one stateless text-only native request with closed slots."""
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
                or output["type"] != "json_schema" or output["name"] != "tiger_clean_slots_v1"):
            return False
        _validate_clean_slots_schema_v1(output["schema"])
        _canonical(request)
        return True
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError):
        return False


# This source-selected protocol preserves all historical builders above. Its
# provider representation is a finite scalar view, never a wider local schema.
COMPACT_DESCRIPTION = (
    'Return this batch as one complete flat scalar JSON object containing exactly all required schema keys. '
    'Source refs occur only in the declared flat keys and bound scalar values; no nested source objects or arrays. '
    'This output-only function performs no external action and has no tool result or follow-up turn.')
COMPACT_EXPRESSION_GUIDANCE = (
    '摘要建议200–450字符，在原800字符硬上限内保留本篇必要事实、限定、反证和未知；'
    '这是表达建议，不是新的验收下限或省略证据的许可。'
    't字段只能写入符合provider schema ticker正则的代码；带交易所前缀或冒号的标签'
    '（例如KRX:000660）不得写入，无法映射为合法ticker时省略该项。')
_COMPACT_LISTS = ('t', 'e', 'h', 'f')
_COMPACT_SUFFIXES = frozenset((*_COMPACT_LISTS, 's', 'v', 'z', 'r',
    *(f'p__{name}' for name in ('k', 'o', 'n', 't')),
    *(f'c__s{index}__{name}' for index in range(4) for name in ('s', 'q', 'k', 't'))))
_COMPACT_END = r'(?![\s\S])'
_COMPACT_EMPTY_HEAD = r'(?:^(?![\s\S])|(?:'
_COMPACT_EMPTY_TAIL = '))'
_COMPACT_REGEN_PREFIX = '\nTiger complete-generation feedback (fixed source policy):\n'
_COMPACT_REGEN_INSTRUCTION = (
    '\nGenerate the entire original batch again from all unchanged inputs. Return every required schema key. '
    'The JSON object must contain exactly the complete provider required key set for every source in this batch. '
    'Returning only the rejected keys, only one source_ref, or any partial/delta object is invalid. '
    'If a claim leaf s/q/k/t is rejected, either provide all four leaves as valid nonempty values or clear all four leaves together. '
    'Do not repair, copy, truncate or reuse any earlier answer. The feedback contains only the trusted schema rejection; '
    'all original evidence, semantic and source constraints remain mandatory.\n')
_COMPACT_QUARTET_INSTRUCTION = (
    '\nCompact claim contract: Within every claim slot, s/q/k/t must either all be nonempty '
    'and satisfy their original constraints, or all be empty strings, including claim_key k. '
    'Decide the supported statement and its own-source quote first; never prefill a key for an absent or discarded claim. '
    'Do not invent a statement, quote or time role to complete a slot. Actual claims occupy a consecutive prefix; '
    'every unused trailing slot has all four leaves empty. Emit every key in the provider required array order: '
    'summary and provenance before claims, then statement s, quote q, claim_key k and time_role t within each claim.')


def _compact_array_schema(node):
    from .required_schemas import TICKER_PATTERN
    from .deepseek_required import _check_schema
    end = _COMPACT_END
    if (type(node) is not dict
            or set(node) != {'type', 'items', 'minItems', 'maxItems', 'uniqueItems'}
            or node['type'] != 'array' or node['uniqueItems'] is not True
            or type(node['minItems']) is not int or type(node['maxItems']) is not int
            or not 0 <= node['minItems'] <= node['maxItems'] <= 1000):
        _invalid()
    item = node['items']
    if item == {'type': 'string', 'pattern': TICKER_PATTERN[:-1] + end}:
        token, minimum, maximum = TICKER_PATTERN[1:-1], 1, 15
    elif (type(item) is dict and set(item) == {'type', 'enum'}
            and item['type'] == 'string' and type(item['enum']) is list and item['enum']
            and all(type(value) is str and value and ',' not in value
                    and not any(character.isspace() for character in value) for value in item['enum'])
            and len(set(item['enum'])) == len(item['enum'])):
        token = '(?:' + '|'.join(re.escape(value) for value in item['enum']) + ')'
        minimum, maximum = min(map(len, item['enum'])), max(map(len, item['enum']))
    elif item == {'type': 'string', 'pattern': r'(?!)'} and node['minItems'] == 0:
        return {'type': 'string', 'pattern': '^' + end, 'minLength': 0, 'maxLength': 0}
    else:
        _invalid()
    low, high = node['minItems'], node['maxItems']
    if high == 0:
        pattern = '^' + end
    else:
        body = token + '(?:,' + token + '){' + str(max(1, low) - 1) + ',' + str(high - 1) + '}'
        pattern = '^(?:' + body + ')?' + end if low == 0 else '^' + body + end
    result = {'type': 'string', 'pattern': pattern,
        'minLength': low * minimum + max(0, low - 1),
        'maxLength': high * maximum + max(0, high - 1)}
    _check_schema(result)
    return result


def _compact_empty_string(node):
    """Prove an empty marker cannot collide with an original actual string."""
    from .deepseek_required import _matches
    if (type(node) is not dict or node.get('type') != 'string'
            or set(node) - {'type', 'enum', 'pattern', 'minLength', 'maxLength'}
            or node.get('minLength', 1) != 1 or _matches('', node)):
        _invalid()
    if node == {'type': 'string', 'pattern': r'(?!)'}:
        return {'type': 'string', 'enum': ['']}
    result = deepcopy(node)
    if 'enum' in node:
        values = node['enum']
        if (type(values) is not list or not values
                or any(type(value) is not str or not value for value in values)
                or len(set(values)) != len(values)):
            _invalid()
        result['enum'] = ['', *values]
    if 'pattern' in node:
        result['pattern'] = _COMPACT_EMPTY_HEAD + node['pattern'] + _COMPACT_EMPTY_TAIL
    if 'minLength' in result:
        result['minLength'] = 0
    return result


def _compact_bounded_leaf(node):
    from .required_schemas import _text_v2
    if not set(node).intersection({'minLength', 'maxLength'}):
        return deepcopy(node)
    if (set(node) != {'type', 'pattern', 'minLength', 'maxLength'} or node['type'] != 'string'
            or type(node['minLength']) is not int or type(node['maxLength']) is not int
            or not 1 <= node['maxLength'] <= 1000000):
        _invalid()
    pattern = node['pattern']
    optional = pattern.startswith(_COMPACT_EMPTY_HEAD) and pattern.endswith(_COMPACT_EMPTY_TAIL)
    base = pattern[len(_COMPACT_EMPTY_HEAD):-len(_COMPACT_EMPTY_TAIL)] if optional else pattern
    if node['minLength'] != (0 if optional else 1):
        _invalid()
    if base == _text_v2(1)['pattern']:
        tail = r']+(?![\s\S])'
        if not base.endswith(tail) or base.count(tail) != 1:
            _invalid()
        bounded = base.replace(tail, ']{{1,{}}}'.format(node['maxLength']) + _COMPACT_END)
        pattern = _COMPACT_EMPTY_HEAD + bounded + _COMPACT_EMPTY_TAIL if optional else bounded
    elif base != r'^[a-z0-9][a-z0-9_.:-]{0,159}(?![\s\S])' or node['maxLength'] != 160:
        _invalid()
    return {**deepcopy(node), 'pattern': pattern}


def clean_compact_lists_schema_v1(keyed_schema):
    """Complete original contract -> 28 required scalar leaves per source."""
    from .deepseek_required import _check_schema
    from ..redline import LLMUnavailable
    try:
        # Existing compilation checks the full registered array/claim shape.
        clean_slots_schema_v1(keyed_schema)
        shared, owned = keyed_schema['properties']['d']['allOf']
        common = shared['additionalProperties']['properties']
        properties = {}
        for reference, binding in owned['properties'].items():
            row = {}
            for field in _COMPACT_LISTS:
                row[field] = _compact_array_schema(common[field])
            for field in ('s', 'v', 'z', 'r'):
                row[field] = _compact_bounded_leaf(common[field])
            for field in ('k', 'o', 'n', 't'):
                node = common['p']['properties'][field]
                if field == 'o':
                    if (type(node) is not dict or set(node) != {'anyOf'}
                            or len(node['anyOf']) != 2 or node['anyOf'][1] != {'type': 'null'}):
                        _invalid()
                    node = _compact_empty_string(node['anyOf'][0])
                row['p__' + field] = _compact_bounded_leaf(node)
            claims = deepcopy(common['c']['items']['properties'])
            claims['q'] = deepcopy(binding['properties']['c']['items']['properties']['q'])
            for index in range(4):
                for field, node in claims.items():
                    row[f'c__s{index}__{field}'] = _compact_bounded_leaf(_compact_empty_string(node))
            if set(row) != _COMPACT_SUFFIXES:
                _invalid()
            properties.update({reference + '__' + suffix: leaf for suffix, leaf in row.items()})
        properties = dict(sorted(properties.items()))
        result = {'type': 'object', 'properties': properties,
                  'required': list(properties), 'additionalProperties': False}
        _check_schema(result)
        return result
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, LLMUnavailable):
        _invalid()


def clean_compact_lists_output_v1(value, keyed_schema, *, decode=False):
    """Exact join/split, empty-origin and trailing-claim inverse; no repair."""
    from .deepseek_required import validate_schema
    from ..redline.model_diagnostics import RequiredModelUnavailable
    local = clean_compact_lists_schema_v1(keyed_schema)
    validate_schema(value, local if decode else keyed_schema)
    references = keyed_schema['properties']['d']['allOf'][1]['properties']
    if not decode:
        result = {}
        for reference in references:
            row = value['d'][reference]
            for field in _COMPACT_LISTS:
                result[reference + '__' + field] = ','.join(row[field])
            for field in ('s', 'v', 'z', 'r'):
                result[reference + '__' + field] = deepcopy(row[field])
            for field in ('k', 'o', 'n', 't'):
                child = row['p'][field]
                result[reference + '__p__' + field] = '' if child is None else deepcopy(child)
            for index in range(4):
                for field in ('s', 'q', 'k', 't'):
                    result[f'{reference}__c__s{index}__{field}'] = (
                        deepcopy(row['c'][index][field]) if index < len(row['c']) else '')
        validate_schema(result, local)
        return result
    result = {'d': {}}
    original_claim_leaves = keyed_schema['properties']['d']['allOf'][0]['additionalProperties']['properties']['c']['items']['properties']
    for reference in references:
        row = {field: ([] if value[reference + '__' + field] == '' else
                       value[reference + '__' + field].split(',')) for field in _COMPACT_LISTS}
        row.update({field: deepcopy(value[reference + '__' + field]) for field in ('s', 'v', 'z', 'r')})
        row['p'] = {field: deepcopy(value[reference + '__p__' + field]) for field in ('k', 'o', 'n', 't')}
        if row['p']['o'] == '':
            row['p']['o'] = None
        row['c'], unused = [], False
        for index in range(4):
            claim = {field: deepcopy(value[f'{reference}__c__s{index}__{field}']) for field in ('s', 'q', 'k', 't')}
            if all(child == '' for child in claim.values()):
                unused = True
            else:
                empty = next((field for field in ('s', 'q', 'k', 't') if claim[field] == ''), None)
                if empty is not None:
                    # A partial quartet already violates the original claim
                    # contract. Locate that exact original leaf before the
                    # two mandatory keyed allOf branches collapse its detail.
                    try:
                        validate_schema(claim[empty], original_claim_leaves[empty])
                    except RequiredModelUnavailable as error:
                        diagnostic = error.diagnostic
                        if diagnostic['code'] != 'OUTPUT_SCHEMA_MISMATCH' or 'schema_keyword' not in diagnostic:
                            raise
                        raise RequiredModelUnavailable('OUTPUT_SCHEMA_MISMATCH',
                            schema_path=f'/{reference}__c__s{index}__{empty}' + diagnostic['schema_path'],
                            schema_keyword=diagnostic['schema_keyword']) from None
                    _invalid()
                if unused:
                    raise RequiredModelUnavailable('OUTPUT_SCHEMA_MISMATCH')
                row['c'].append(claim)
        result['d'][reference] = row
    validate_schema(result, keyed_schema)
    return result


def _compact_project(local):
    result = deepcopy(local)
    for leaf in result['properties'].values():
        leaf.pop('minLength', None)
        leaf.pop('maxLength', None)
    _validate_server_schema(result)
    return result


def _compact_share(projected):
    result = deepcopy(projected)
    interned, definitions = {}, {}
    for name, leaf in projected['properties'].items():
        identity = _canonical(leaf)
        if identity not in interned:
            alias = 'l' + str(len(interned))
            interned[identity], definitions[alias] = alias, deepcopy(leaf)
        result['properties'][name] = {'$ref': '#/$def/' + interned[identity]}
    result['$def'] = definitions
    result['required'] = _compact_required_order(projected['properties'])
    return result


def _compact_required_order(properties):
    """Order only the required array; canonical properties/refs stay exact."""
    suffixes = ('z', 'p__k', 'p__o', 'p__n', 'p__t', 'r', 't', 'e', 'h', 'f', 's', 'v',
        *(f'c__s{index}__{field}' for index in range(4) for field in ('s', 'q', 'k', 't')))
    groups = {}
    for name in properties:
        if type(name) is not str or '__' not in name:
            _invalid()
        source, suffix = name.split('__', 1)
        if re.fullmatch(r'd(?:0|[1-9][0-9]*)', source) is None or suffix not in _COMPACT_SUFFIXES:
            _invalid()
        groups.setdefault(source, set()).add(suffix)
    if any(fields != _COMPACT_SUFFIXES for fields in groups.values()):
        _invalid()
    return [source + '__' + suffix for source in sorted(groups, key=lambda value: (len(value), value))
            for suffix in suffixes]


def expand_clean_compact_lists_provider_schema_v1(provider, *, keyed_schema=None):
    """Only canonical internal scalar refs; general local schema stays closed."""
    from ..redline import LLMUnavailable
    try:
        if (type(provider) is not dict
                or set(provider) != {'type', 'properties', 'required', 'additionalProperties', '$def'}
                or provider['type'] != 'object' or provider['additionalProperties'] is not False
                or type(provider['properties']) is not dict or type(provider['required']) is not list
                or list(provider['properties']) != sorted(provider['properties'])
                or provider['required'] != _compact_required_order(provider['properties']) or type(provider['$def']) is not dict):
            _invalid()
        definitions = provider['$def']
        if set(definitions) != {'l' + str(index) for index in range(len(definitions))}:
            _invalid()
        projected = {name: deepcopy(child) for name, child in provider.items() if name != '$def'}
        # The original local projection is unchanged; only the provider's
        # requested emission order differs. JSON object order is not a gate.
        projected['required'] = list(projected['properties'])
        groups, used = {}, set()
        for name, reference in provider['properties'].items():
            if type(name) is not str:
                _invalid()
            source, suffix = name.split('__', 1)
            if re.fullmatch(r'd(?:0|[1-9][0-9]*)', source) is None or suffix not in _COMPACT_SUFFIXES:
                _invalid()
            groups.setdefault(source, set()).add(suffix)
            if (type(reference) is not dict or set(reference) != {'$ref'} or type(reference['$ref']) is not str
                    or re.fullmatch(r'#/\$def/l(?:0|[1-9][0-9]*)', reference['$ref']) is None):
                _invalid()
            alias = reference['$ref'].removeprefix('#/$def/')
            leaf = definitions[alias]
            if type(leaf) is not dict or leaf.get('type') not in {'string', 'number'}:
                _invalid()
            _validate_server_schema(leaf)
            projected['properties'][name] = deepcopy(leaf)
            used.add(alias)
        if used != set(definitions) or any(suffixes != _COMPACT_SUFFIXES for suffixes in groups.values()):
            _invalid()
        _validate_server_schema(projected)
        if _compact_share(projected) != provider:
            _invalid()
        if keyed_schema is not None and projected != _compact_project(clean_compact_lists_schema_v1(keyed_schema)):
            _invalid()
        _canonical(provider)
        return projected
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, LLMUnavailable):
        _invalid()


def clean_compact_lists_provider_schema_v1(keyed_schema):
    projected = _compact_project(clean_compact_lists_schema_v1(keyed_schema))
    result = _compact_share(projected)
    if expand_clean_compact_lists_provider_schema_v1(result, keyed_schema=keyed_schema) != projected:
        _invalid()
    return result


def _compact_metadata(keyed_schema, bindings, references):
    clean_compact_lists_schema_v1(keyed_schema)
    common = keyed_schema['properties']['d']['allOf'][0]['additionalProperties']['properties']
    meanings = {'t': 'tickers', 's': 'sentiment', 'v': 'sentiment_score', 'e': 'events',
        'z': 'summary_zh', 'r': 'relevance', 'h': 'chain_ids', 'f': 'sector_etfs',
        'p__k': 'provenance.evidence_kind', 'p__o': 'provenance.origin_id',
        'p__n': 'provenance.origin_note', 'p__t': 'provenance.time_role'}
    claim_names = {'s': 'statement', 'q': 'quote_part_id', 'k': 'claim_key', 't': 'time_role'}
    rows = []
    for suffix in sorted(_COMPACT_SUFFIXES):
        if suffix in _COMPACT_LISTS:
            limits = {name: common[suffix][name] for name in ('minItems', 'maxItems', 'uniqueItems')}
            encoding, meaning = 'comma_list', meanings[suffix]
        else:
            if suffix.startswith('c__'):
                _, slot, field = suffix.split('__')
                meaning, encoding = 'claims[' + slot[1:] + '].' + claim_names[field], 'claim_leaf'
                leaf = common['c']['items']['properties'][field]
            else:
                meaning, encoding = meanings[suffix], 'empty_null' if suffix == 'p__o' else 'scalar'
                leaf = common['p']['properties'][suffix[-1]] if suffix.startswith('p__') else common[suffix]
            limits = {name: leaf[name] for name in ('minLength', 'maxLength') if name in leaf}
        rows.append([suffix, meaning, encoding, limits])
    return {'schema': CLEAN_COMPACT_LISTS_WIRE_V1,
        'mapping': {'columns': ['suffix', 'semantic_field', 'encoding', 'original_limits'], 'rows': rows},
        'source_bindings': deepcopy(bindings), 'output_source_refs': list(references),
        'required_fields_per_source': 28, 'required_scalar_fields': 28 * len(references),
        'key_rule': 'For each shown source_ref emit every provider-schema key exactly once: source_ref + __ + mapped suffix. The schema alone lists the complete required keys. No other keys, objects or arrays.',
        'list_rule': 'comma_list is empty string for []; otherwise comma-join complete original tokens in model-selected order. No empty tokens, spaces, trimming, sorting, deduplication or inferred items. Original item/count/uniqueness rules still apply.',
        'claim_rule': 'Four claims at s0..s3. Fill actual claims consecutively in model-selected order. Each unused trailing claim has s/q/k/t all empty strings; partial emptiness or holes are invalid. q uses only that source own allowed_quote_part_ids.',
        'origin_rule': 'Only empty string represents null origin_id. Any known origin remains its exact dN registry alias, never inferred from the alias itself.',
        'source_rule': 'The complete global registry preserves attribution for all sources. Only the shown documents have output keys; the registry does not mean other source bodies were shown.'}


def clean_compact_lists_payload_v1(payload, keyed_schema, *, decode=False):
    from . import required
    from ..redline.execution import RequiredModelFailed
    try:
        if decode:
            if type(payload) is not dict or payload['clean_wire']['schema'] != CLEAN_COMPACT_LISTS_WIRE_V1:
                _invalid()
            base = deepcopy(payload)
            base['clean_wire'] = required.clean_payload_wire_v2({'documents': [],
                'all_source_index': {'columns': ['id', 'title', 'url', 'published_at'], 'rows': []}})['clean_wire']
            restored = required.clean_payload_wire_v2(base, decode=True)
            if clean_compact_lists_payload_v1(restored, keyed_schema) != payload:
                _invalid()
            return restored
        base = required.clean_keyed_payload_v2(payload)
        required.clean_slots_payload_v1(payload, keyed_schema)
        bindings = base['clean_wire']['source_bindings']
        base['clean_wire'] = _compact_metadata(keyed_schema, bindings, [row[0] for row in bindings['rows']])
        return base
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, RequiredModelFailed):
        _invalid()


def clean_regeneration_feedback_v1(diagnostic):
    from ..redline.model_diagnostics import validate_model_diagnostic
    try:
        safe = validate_model_diagnostic(diagnostic)
        if safe['schema'] != 'tiger.model-failure.v1' or safe['code'] != 'OUTPUT_SCHEMA_MISMATCH':
            _invalid()
        return {name: safe[name] for name in ('code', 'schema_path', 'schema_keyword') if name in safe}
    except (KeyError, TypeError, ValueError):
        _invalid()


def _compact_feedback_suffix(feedback):
    if feedback is None:
        return ''
    if type(feedback) is not dict or set(feedback) not in ({'code'}, {'code', 'schema_path', 'schema_keyword'}):
        _invalid()
    if clean_regeneration_feedback_v1({'schema': 'tiger.model-failure.v1', 'provider': 'deepseek', **feedback}) != feedback:
        _invalid()
    return _COMPACT_REGEN_PREFIX + _COMPACT_REGEN_INSTRUCTION + _canonical(feedback)


def clean_logical_call_sha256(*, run_id, assessment, request, schema):
    import hashlib
    from .candidate_research import validate_assessment
    if type(run_id) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', run_id) is None:
        _invalid()
    value = {'schema': 'tiger.clean-logical-call.v1', 'run_id': run_id,
        'assessment': validate_assessment(assessment), 'output_policy_id': CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1,
        'output_codec_id': CLEAN_COMPACT_LISTS_WIRE_V1, 'regeneration_policy_id': CLEAN_REGENERATION_POLICY_V1,
        'request_sha256': hashlib.sha256(_canonical(request).encode()).hexdigest(),
        'schema_sha256': hashlib.sha256(_canonical(schema).encode()).hexdigest()}
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def build_clean_compact_lists_request(*, system, user, schema_hint, schema,
                                     max_tokens=32768, temperature=0.2, regeneration_feedback=None):
    if (type(system) is not str or not system.strip() or type(user) is not str or not user.strip()
            or type(schema_hint) is not dict or schema_hint
            or type(max_tokens) is not int or max_tokens != 32768
            or type(temperature) not in (int, float) or temperature != 0.2):
        _invalid()
    parameters = clean_compact_lists_provider_schema_v1(schema)
    sources = {name.split('__', 1)[0] for name in parameters['properties']}
    if len(sources) > 4:
        _invalid()
    return {'model': 'deepseek-flash', 'messages': [
        {'role': 'system', 'content': system + _COMPACT_QUARTET_INSTRUCTION + CLEAN_OUTPUT_INSTRUCTION + _compact_feedback_suffix(regeneration_feedback)},
        {'role': 'user', 'content': user}], 'max_tokens': 32768, 'temperature': 0.2, 'stream': False,
        'thinking': {'type': 'disabled'}, 'reasoning_effort': 'none',
        'tools': [{'type': 'function', 'function': {'name': CLEAN_OUTPUT_FUNCTION, 'strict': True,
            'description': COMPACT_DESCRIPTION, 'parameters': parameters}}],
        'tool_choice': {'type': 'function', 'function': {'name': CLEAN_OUTPUT_FUNCTION}}}


def clean_compact_lists_function_request_valid(request):
    """Finite dedicated prepayment boundary; historical tool flags stay exact."""
    from ..redline import LLMUnavailable
    try:
        if (type(request) is not dict or set(request) != {'model', 'messages', 'max_tokens', 'temperature',
                'stream', 'thinking', 'reasoning_effort', 'tools', 'tool_choice'}
                or request['model'] != 'deepseek-flash' or request['stream'] is not False
                or request['thinking'] != {'type': 'disabled'} or request['reasoning_effort'] != 'none'
                or type(request['max_tokens']) is not int or request['max_tokens'] != 32768
                or type(request['temperature']) not in (int, float) or request['temperature'] != 0.2
                or request['tool_choice'] != {'type': 'function', 'function': {'name': CLEAN_OUTPUT_FUNCTION}}
                or type(request['tools']) is not list or len(request['tools']) != 1
                or type(request['messages']) is not list or len(request['messages']) != 2):
            return False
        for message, role in zip(request['messages'], ('system', 'user')):
            if (type(message) is not dict or set(message) != {'role', 'content'}
                    or message['role'] != role or type(message['content']) is not str or not message['content'].strip()):
                return False
        system = request['messages'][0]['content']
        if _COMPACT_REGEN_PREFIX in system:
            base, feedback = system.rsplit(_COMPACT_REGEN_PREFIX, 1)
            if (not base.endswith(_COMPACT_QUARTET_INSTRUCTION + CLEAN_OUTPUT_INSTRUCTION) or not feedback.startswith(_COMPACT_REGEN_INSTRUCTION)):
                return False
            encoded = feedback[len(_COMPACT_REGEN_INSTRUCTION):]
            from .deepseek_required import _json_loads
            decoded = _json_loads(encoded)
            if _compact_feedback_suffix(decoded) != _COMPACT_REGEN_PREFIX + feedback:
                return False
        elif not system.endswith(_COMPACT_QUARTET_INSTRUCTION + CLEAN_OUTPUT_INSTRUCTION):
            return False
        tool = request['tools'][0]
        if type(tool) is not dict or set(tool) != {'type', 'function'} or tool['type'] != 'function':
            return False
        function = tool['function']
        if (type(function) is not dict or set(function) != {'name', 'strict', 'description', 'parameters'}
                or function['name'] != CLEAN_OUTPUT_FUNCTION or function['strict'] is not True
                or function['description'] != COMPACT_DESCRIPTION):
            return False
        expanded = expand_clean_compact_lists_provider_schema_v1(function['parameters'])
        if len(expanded['properties']) > 112:
            return False
        _canonical(request)
        return True
    except (KeyError, TypeError, ValueError, IndexError, RecursionError, OverflowError, LLMUnavailable):
        return False


def build_clean_compact_lists_safe_response(*, response_id, system_fingerprint, usage,
                                          tool_call_id, parsed=None, visible_arguments=None,
                                          visible_content=None):
    """Preserve validated visible content separately from semantic arguments.

    Transport and independent replay validate text safety before this pure
    projection. Historical callers retain the original null-content default.
    """
    if (parsed is None) == (visible_arguments is None):
        _invalid()
    if parsed is not None and type(parsed) is not dict:
        _invalid()
    if visible_arguments is not None and (type(visible_arguments) is not str or not visible_arguments.strip()):
        _invalid()
    if visible_content is not None and type(visible_content) is not str:
        _invalid()
    arguments = visible_arguments if visible_arguments is not None else _canonical(parsed)
    return {'id': response_id, 'model': 'deepseek-flash', 'system_fingerprint': system_fingerprint,
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': visible_content, 'tool_calls': [{'id': tool_call_id,
                'type': 'function', 'function': {'name': CLEAN_OUTPUT_FUNCTION, 'arguments': arguments}}]}}],
        'usage': deepcopy(usage)}
