"""Fail-closed DeepSeek Flash transport with keyless, stage-bound evidence.

``deepseek-flash`` is the API ID currently documented as V4.1-Flash-1017;
it is not an immutable snapshot ID. Search is the provider's native Search
API, not the Responses ``web_search`` tool (which Flash ignores).
No raw response body, authorization header or unsafe source URL is persisted.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError, SSLError
from urllib3.util import Timeout

from ..redline import LLMUnavailable
from ..redline.model_diagnostics import RequiredModelUnavailable

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"
RELEASE = "DeepSeek-V4.1-Flash-1017"
SEARCH_MODE = "provider_native_search"
MAX_RESPONSE_BYTES = 1024 * 1024
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
RESPONSE_DEADLINE = 180.0
_URL = re.compile(r"https?://[^\s<>\"'`\[\]{}]+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_CREDENTIAL = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret|session(?:id)?)"
    r"\s*[:=]\s*(?:Bearer\s+)?[^\s,;<>\"']+")
_SENSITIVE_QUERY = frozenset({
    "accesstoken", "token", "key", "apikey", "signature", "password", "passwd",
    "session", "sessionid", "secret", "authorization", "auth", "credential",
    "credentials", "refreshtoken", "idtoken", "authtoken", "oauthtoken",
    "sessiontoken", "clientsecret", "accesskey", "accesskeyid", "securitytoken", "jwt",
})
_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "uniqueItems",
    "anyOf", "oneOf", "allOf", "title", "description", "$schema",
})
_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def _fail(code: str, *, schema_path: str | None = None,
          schema_keyword: str | None = None) -> None:
    if re.fullmatch(r"HTTP_[1-5][0-9]{2}", code):
        raise RequiredModelUnavailable("HTTP_ERROR", http_status=int(code[5:])) from None
    raise RequiredModelUnavailable(code, schema_path=schema_path,
                                   schema_keyword=schema_keyword) from None


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("INVALID_JSON_VALUE")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _finite_number(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (not isinstance(value, float) or math.isfinite(value)))


def _json_equal(left: object, right: object) -> bool:
    if _finite_number(left) and _finite_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    return left == right


def _check_schema(schema: object, depth: int = 0) -> None:
    """Validate the supported JSON Schema subset before accepting any instance."""
    if depth > 32 or not isinstance(schema, dict) or not schema or set(schema) - _SCHEMA_KEYS:
        _fail("UNSUPPORTED_OUTPUT_SCHEMA")
    _canonical(schema)
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if (not kinds or any(not isinstance(x, str) or x not in _TYPES for x in kinds)
                or len(set(kinds)) != len(kinds)):
            _fail("INVALID_SCHEMA_TYPE")
    for annotation in ("title", "description", "$schema"):
        if annotation in schema and not isinstance(schema[annotation], str):
            _fail("INVALID_SCHEMA_ANNOTATION")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict) or any(not isinstance(k, str) for k in props):
            _fail("INVALID_SCHEMA_PROPERTIES")
        for child in props.values():
            _check_schema(child, depth + 1)
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or any(not isinstance(x, str) for x in required)
                or len(set(required)) != len(required)):
            _fail("INVALID_SCHEMA_REQUIRED")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, bool):
            _check_schema(extra, depth + 1)
    if "items" in schema:
        _check_schema(schema["items"], depth + 1)
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _finite_number(schema[key]):
            _fail("INVALID_SCHEMA_BOUND")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            _fail("INVALID_SCHEMA_LENGTH")
    if "pattern" in schema:
        try:
            if not isinstance(schema["pattern"], str) or len(schema["pattern"]) > 1000:
                _fail("INVALID_SCHEMA_PATTERN")
            re.compile(schema["pattern"])
        except re.error:
            _fail("INVALID_SCHEMA_PATTERN")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _fail("INVALID_SCHEMA_UNIQUENESS")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        _fail("INVALID_SCHEMA_ENUM")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                _fail("INVALID_SCHEMA_COMBINATOR")
            for child in schema[key]:
                _check_schema(child, depth + 1)


def _matches_type(value: object, kind: str) -> bool:
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "number": lambda: _finite_number(value),
        "integer": lambda: _finite_number(value) and int(value) == value,
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[kind]()


def _matches(value: object, schema: dict, depth: int = 0) -> bool:
    if depth > 64:
        return False
    kind = schema.get("type")
    if kind is not None and not any(_matches_type(value, k) for k in
                                    (kind if isinstance(kind, list) else [kind])):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    if "enum" in schema and not any(_json_equal(value, v) for v in schema["enum"]):
        return False
    for key, accept in (("allOf", all), ("anyOf", any)):
        if key in schema and not accept(_matches(value, s, depth + 1) for s in schema[key]):
            return False
    if "oneOf" in schema and sum(_matches(value, s, depth + 1) for s in schema["oneOf"]) != 1:
        return False
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            return False
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                if not _matches(item, props[key], depth + 1):
                    return False
            elif extra is False or (isinstance(extra, dict) and not _matches(item, extra, depth + 1)):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        if schema.get("uniqueItems") and any(_json_equal(v, x) for i, v in enumerate(value)
                                             for x in value[i + 1:]):
            return False
        if "items" in schema and any(not _matches(v, schema["items"], depth + 1) for v in value):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if _finite_number(value):
        for key, invalid in (("minimum", lambda bound: value < bound),
                             ("maximum", lambda bound: value > bound),
                             ("exclusiveMinimum", lambda bound: value <= bound),
                             ("exclusiveMaximum", lambda bound: value >= bound)):
            if key in schema and invalid(schema[key]):
                return False
    return True


def _schema_child_path(path: str, segment: str | int) -> str | None:
    # Property names come only from the trusted schema. Never expose an extra
    # model-generated key, and mask schema names outside the fixed safe grammar.
    if type(segment) is int:
        component = str(segment)
    else:
        component = segment if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", segment) else "*"
    child = path + "/" + component
    return child if len(child) <= 512 else None


def _schema_mismatch(value: object, schema: dict, depth: int = 0,
                     path: str = "", *, trusted_properties=frozenset()) -> tuple[str, str]:
    """Locate a rejected instance without returning its keys, values or text.

    This runs only after the unchanged acceptance validator rejects the value.
    One uniquely failing allOf child may be located more precisely. A shared
    additionalProperties rule may name only keys declared by sibling schemas;
    arbitrary model keys and unresolved alternatives stay at the parent path.
    """
    if depth > 64:
        return path, "depth"
    kind = schema.get("type")
    if kind is not None and not any(_matches_type(value, k) for k in
                                    (kind if isinstance(kind, list) else [kind])):
        return path, "type"
    if "const" in schema and not _json_equal(value, schema["const"]):
        return path, "const"
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        return path, "enum"
    for key, accept in (("allOf", all), ("anyOf", any)):
        if key in schema and not accept(_matches(value, child, depth + 1) for child in schema[key]):
            if key == "allOf":
                failed = [child for child in schema[key] if not _matches(value, child, depth + 1)]
                if len(failed) == 1:
                    known = set(trusted_properties) | set(schema.get("properties", {}))
                    for child in schema[key]:
                        known.update(child.get("properties", {}))
                    return _schema_mismatch(value, failed[0], depth + 1, path,
                                            trusted_properties=frozenset(known))
            return path, key
    if "oneOf" in schema and sum(_matches(value, child, depth + 1) for child in schema["oneOf"]) != 1:
        return path, "oneOf"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return path, "type"
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return path, "required"
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                if not _matches(item, props[key], depth + 1):
                    child = _schema_child_path(path, key)
                    return ((path, "depth") if child is None else
                            _schema_mismatch(item, props[key], depth + 1, child))
            elif extra is False or (isinstance(extra, dict) and not _matches(item, extra, depth + 1)):
                if isinstance(extra, dict) and key in trusted_properties:
                    child = _schema_child_path(path, key)
                    return ((path, "depth") if child is None else
                            _schema_mismatch(item, extra, depth + 1, child))
                return path, "additionalProperties"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return path, "minItems"
        if len(value) > schema.get("maxItems", len(value)):
            return path, "maxItems"
        if schema.get("uniqueItems") and any(_json_equal(item, other)
                for index, item in enumerate(value) for other in value[index + 1:]):
            return path, "uniqueItems"
        if "items" in schema:
            for index, item in enumerate(value):
                if not _matches(item, schema["items"], depth + 1):
                    child = _schema_child_path(path, index)
                    return ((path, "depth") if child is None else
                            _schema_mismatch(item, schema["items"], depth + 1, child))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return path, "minLength"
        if len(value) > schema.get("maxLength", len(value)):
            return path, "maxLength"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return path, "pattern"
    if _finite_number(value):
        for key, invalid in (("minimum", lambda bound: value < bound),
                             ("maximum", lambda bound: value > bound),
                             ("exclusiveMinimum", lambda bound: value <= bound),
                             ("exclusiveMaximum", lambda bound: value >= bound)):
            if key in schema and invalid(schema[key]):
                return path, key
    # A conservative fixed fallback cannot expose an untrusted value even if a
    # future acceptance rule has not yet received a more specific diagnostic.
    return path, "type"


def validate_schema(value: object, schema: dict) -> None:
    """Validate the complete supported schema; unknown keywords never pass."""
    _check_schema(schema)
    _canonical(value)  # Reject non-JSON values, nonfinite numbers and deep cycles.
    if not _matches(value, schema):
        path, keyword = _schema_mismatch(value, schema)
        _fail("OUTPUT_SCHEMA_MISMATCH", schema_path=path, schema_keyword=keyword)


def _decoded_url_component(value: str) -> str | None:
    """Resolve nested percent escapes completely or reject ambiguous input."""
    for _ in range(8):
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded
    return value if unquote(value) == value else None


def sanitize_url(value: object, *, secret: str | None = None, _depth: int = 0) -> str | None:
    """Return a credential-free public HTTP(S) URL, otherwise reject it entirely."""
    if _depth > 4 or not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if secret and secret in value:
        return None
    value = html.unescape(value)
    decoded_url = _decoded_url_component(value)
    if decoded_url is None or secret and secret in decoded_url or _JWT.search(decoded_url):
        return None
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value or _JWT.search(value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80, 443)):
            return None
        host = parts.hostname.lower().rstrip(".")
        if (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
                or "." not in host and ":" not in host):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host) or host.startswith("0x"):
                return None
            encoded_host = host.encode("idna").decode("ascii")
            if len(encoded_host) > 253 or any(not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in encoded_host.split(".")):
                return None
        else:
            if not address.is_global:
                return None
        for key, item in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100):
            key, item = _decoded_url_component(key), _decoded_url_component(item)
            if key is None or item is None:
                return None
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if (normalized in _SENSITIVE_QUERY or normalized.startswith(("xamz", "xgoog"))
                    or _CREDENTIAL.search(item) or _JWT.search(item) or secret and secret in item):
                return None
            if any(sanitize_url(match.group(0), secret=secret, _depth=_depth + 1) is None
                   for match in _URL.finditer(item)):
                return None
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    except (ValueError, UnicodeError):
        return None


def _reject_constant(_value: str):
    _fail("NONFINITE_RESPONSE_JSON")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_RESPONSE_JSON_KEY")
        result[key] = value
    return result


def _json_loads(value: bytes | str) -> object:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "strict")
        return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        _fail("INVALID_RESPONSE_JSON")


def build_chat_request(*, system: str, user: str, schema_hint: dict,
                       schema: dict, max_tokens: int = 16384,
                       temperature: float = 0.2, reasoning_effort: str = "high",
                       thinking_mode: str = "enabled") -> dict:
    """Pure exact request framing, also used by the read-only proof replay."""
    return {"model": MODEL, "messages": [
        {"role": "system", "content": system + "\nReturn exactly one JSON object. JSON only."},
        {"role": "user", "content": user + "\nJSON schema:\n" +
         _canonical(schema).decode() + "\nJSON field guidance:\n" +
         _canonical(schema_hint).decode()}],
        "max_tokens": max_tokens, "temperature": temperature, "stream": False,
        "response_format": {"type": "json_object"}, "thinking": {"type": thinking_mode},
        "reasoning_effort": reasoning_effort}


def stage_reasoning_effort(stage: str, *, upgrade: bool = False) -> str:
    """Extraction gets bounded low effort; research and decisions retain high.

    Official low/high support was checked on 2026-10-27 at
    https://api-docs.deepseek.com/guides/thinking_mode/ . This is a versioned
    request policy, not evidence that lower effort improves semantic accuracy.
    """
    return "low" if upgrade and stage == "clean.llm_semantic" else "high"


def stage_thinking_mode(stage: str, *, upgrade: bool = False) -> str:
    """tiger.clean-wire.v1 disables thinking for v2 extraction only.

    This still requests a complete semantic answer from the same Flash model.
    It reserves the existing 16384 output tokens for that answer rather than
    explicit hidden thinking. Research, risk and final judgment retain enabled
    high thinking; the immutable v1 request default is also enabled/high.
    Official toggle/JSON documentation checked on 2026-10-27:
    https://api-docs.deepseek.com/guides/thinking_mode/
    https://api-docs.deepseek.com/guides/json_mode/
    This is a capacity policy, not evidence of improved semantic accuracy.
    """
    return "disabled" if upgrade and stage == "clean.llm_semantic" else "enabled"


class _BodyDeadline:
    """Interrupt the owned raw response at the remaining monotonic deadline.

    urllib3 read/total timeouts measure socket idle intervals, so blank-line
    keepalives alone cannot enforce a body deadline. Its public shutdown method
    interrupts a blocked read. Cancel and join before closing this response;
    never leave a callback that could act after cleanup. No exception text is
    retained, including errors caused by an already released connection.
    """

    def __init__(self, raw, deadline: float):
        self.expired = threading.Event()
        self.shutdown_failed = threading.Event()
        self._timer = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("RESPONSE_DEADLINE")
        shutdown = getattr(raw, "shutdown", None)
        if callable(shutdown):
            def interrupt():
                self.expired.set()
                try:
                    shutdown()
                except Exception:
                    # A released/closed connection has nothing left to read.
                    # The main thread still rejects this expired response.
                    self.shutdown_failed.set()
            self._timer = threading.Timer(remaining, interrupt)
            self._timer.daemon = True
            self._timer.start()

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join()


class DeepSeekRequiredClient:
    required = True

    def __init__(self, execution):
        if execution is None or not callable(getattr(execution, "call_io", None)):
            _fail("EXECUTION_REQUIRED")
        if (os.environ.get("LLM_BACKEND") != "api"
                or os.environ.get("LLM_BASE_URL") != BASE_URL
                or os.environ.get("LLM_MODEL") != MODEL):
            _fail("TRUSTED_CONFIGURATION_MISMATCH")
        key = os.environ.get("LLM_API_KEY", "")
        if (not key or len(key) > 4096 or key.strip() != key
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            _fail("API_KEY_REQUIRED")
        self.execution = execution
        self._api_key = key
        self.receipts: list[dict] = []
        self.search_receipts: list[dict] = []
        self.fetch_receipts: list[dict] = []
        self._remote_requests = 0
        self._used_call_ids: set[str] = set()
        self.budget = None
        if getattr(execution, "upgrade", False):
            from .request_budget import BudgetGuard
            self.budget = BudgetGuard.for_policy(getattr(getattr(execution, "policy", None), "budget_policy_id", None))

    def _admit_call(self, kind: str, call_id: str, request: dict, inputs: dict) -> None:
        if call_id in self._used_call_ids:
            _fail("RESPONSE_IDENTITY_REUSED_OR_UNSAFE")
        self._used_call_ids.add(call_id)
        if self.budget is not None:
            from .request_budget import BudgetRefused
            try:
                inputs["budget_reservation"] = self.budget.reserve(kind, call_id, request)
            except BudgetRefused:
                _fail("REQUEST_BUDGET_REFUSED")
            inputs["started_at_utc"] = datetime.now(timezone.utc).isoformat()

    def _settle_budget(self, call_id: str, usage: dict | None) -> None:
        if self.budget is not None:
            from .request_budget import BudgetRefused
            try:
                self.budget.settle(call_id, usage)
            except BudgetRefused:
                _fail("REQUEST_BUDGET_REFUSED")

    def _budget_proof(self, inputs: dict) -> dict:
        if self.budget is None:
            return {}
        end = datetime.now(timezone.utc)
        start = datetime.fromisoformat(inputs["started_at_utc"])
        return {"budget_reservation": inputs["budget_reservation"],
                "budget_after": self.budget.snapshot(),
                "started_at_utc": inputs["started_at_utc"],
                "finished_at_utc": end.isoformat(),
                "elapsed_ms": max(0.0, round((end - start).total_seconds() * 1000, 3))}

    def _record_call(self, method: str, inputs: dict, invoke, *, observation=None):
        def recorded_invoke():
            try:
                return invoke()
            except BaseException as error:
                if self.budget is not None:
                    self.budget.mark_unknown(inputs["call_id"])
                    if type(error) is RequiredModelUnavailable:
                        budget = self.budget.snapshot()
                        record = next(row for row in budget["records"]
                                      if row["reservation"]["call_id"] == inputs["call_id"])
                        error.bind_accounting({"schema": "tiger.model-call-accounting.v3" if self.budget.policy_id else "tiger.model-call-accounting.v2",
                            **({"budget_policy_id": self.budget.policy_id} if self.budget.policy_id else {}),
                            "call_id": inputs["call_id"], "request_sha256": inputs["request_sha256"],
                            "kind": record["reservation"]["kind"], "status": record["status"],
                            "usage": ({key: record["usage"][key] for key in
                                ("prompt_tokens", "completion_tokens", "total_tokens")}
                                if record["usage"] is not None else None),
                            **{key: budget[key] for key in ("counts", "actual_tokens", "reserved_tokens",
                                "estimated_charged_usd", "estimated_reserved_usd", "frozen")},
                            "finish_reason": (observation or {}).get("finish_reason")})
                raise
        try:
            return self.execution.call_io(method, inputs, recorded_invoke)
        except BaseException:
            if self.budget is not None:
                self.budget.mark_unknown(inputs["call_id"])
            raise

    def describe(self) -> dict:
        value = {"mode": "required", "enabled": True, "provider": "deepseek",
                "model": MODEL, "version": RELEASE, "release": RELEASE,
                "version_binding": "official-current-alias-mapping",
                "search": SEARCH_MODE, "remote_requests": self._remote_requests,
                "chat_calls": len(self.receipts), "search_calls": len(self.search_receipts),
                "fetch_calls": len(self.fetch_receipts)}
        if self.budget is not None:
            value["request_budget"] = self.budget.snapshot()
        return value

    def _stage(self) -> str:
        scope = getattr(self.execution, "_active", None)
        name = getattr(getattr(scope, "spec", None), "name", None)
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", name)
                or self._api_key in name):
            _fail("ACTIVE_STAGE_REQUIRED")
        return name

    def _text(self, value: str) -> str:
        def replace_url(match):
            raw = match.group(0)
            clean = raw.rstrip(".,;:!?)")
            suffix = raw[len(clean):]
            return (sanitize_url(clean, secret=self._api_key) or "[REDACTED_URL]") + suffix
        value = _URL.sub(replace_url, value)
        value = value.replace(self._api_key, "[REDACTED_SECRET]")
        value = _JWT.sub("[REDACTED_TOKEN]", value)
        return _CREDENTIAL.sub(lambda m: m.group(1) + "=[REDACTED_SECRET]", value)

    def _clean(self, value: object, depth: int = 0) -> object:
        if depth > 64:
            _fail("RESPONSE_DEPTH_LIMIT")
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, list):
            return [self._clean(v, depth + 1) for v in value]
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                _fail("INVALID_JSON_OBJECT_KEY")
            return {self._text(k): self._clean(v, depth + 1) for k, v in value.items()}
        return value

    def _post_json(self, path: str, request: dict) -> tuple[dict, str]:
        if path not in ("/chat/completions", "/v1/tools/search", "/v1/tools/fetch"):
            _fail("ENDPOINT_NOT_ALLOWED")
        if len(_canonical(request)) > MAX_RESPONSE_BYTES:
            _fail("REQUEST_TOO_LARGE")
        response = None
        body_deadline = None
        deadline = time.monotonic() + RESPONSE_DEADLINE

        def deadline_expired() -> bool:
            return (body_deadline is not None and body_deadline.expired.is_set()
                    or time.monotonic() >= deadline)

        self._remote_requests += 1
        try:
            response = requests.post(
                BASE_URL + path, json=request,
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "Accept-Encoding": "identity", "Authorization": "Bearer " + self._api_key},
                # A non-streaming Chat may legitimately wait beyond a native
                # Search/Fetch idle timeout. The existing 180-second acceptance
                # deadline remains; this does not grant another paid request.
                timeout=Timeout(total=RESPONSE_DEADLINE, connect=CONNECT_TIMEOUT,
                                read=RESPONSE_DEADLINE if path == "/chat/completions" else READ_TIMEOUT),
                allow_redirects=False, verify=True, stream=True)
            status = response.status_code
            if type(status) is not int or status != 200:
                _fail(f"HTTP_{status}" if type(status) is int and 100 <= status <= 599 else "HTTP_INVALID")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in ("", "identity"):
                _fail("UNEXPECTED_CONTENT_ENCODING")
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                _fail("UNEXPECTED_CONTENT_TYPE")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) > MAX_RESPONSE_BYTES):
                _fail("RESPONSE_TOO_LARGE")
            body_deadline = _BodyDeadline(response.raw, deadline)
            chunks: list[bytes] = []
            size = 0
            while True:
                if deadline_expired():
                    _fail("RESPONSE_DEADLINE")
                chunk = response.raw.read1(16384)
                if deadline_expired():
                    _fail("RESPONSE_DEADLINE")
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    _fail("INVALID_RESPONSE_BYTES")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    _fail("RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            raw = b"".join(chunks)
            obj = _json_loads(raw)
            if not isinstance(obj, dict):
                _fail("RESPONSE_NOT_OBJECT")
            return obj, hashlib.sha256(raw).hexdigest()
        except LLMUnavailable:
            raise
        except (requests.Timeout, ReadTimeoutError):
            _fail("RESPONSE_DEADLINE" if deadline_expired() else "NETWORK_TIMEOUT")
        except (requests.exceptions.SSLError, SSLError):
            _fail("RESPONSE_DEADLINE" if deadline_expired() else "NETWORK_TLS_ERROR")
        except (requests.exceptions.ChunkedEncodingError, ProtocolError):
            _fail("RESPONSE_DEADLINE" if deadline_expired() else "NETWORK_PROTOCOL_ERROR")
        except Exception:
            _fail("RESPONSE_DEADLINE" if deadline_expired() else "TRANSPORT_FAILURE")
        finally:
            if body_deadline is not None:
                body_deadline.close()
            if response is not None:
                try:
                    response.close()
                except Exception:
                    _fail("RESPONSE_CLOSE_FAILURE")

    @staticmethod
    def _identifier(value: object) -> str:
        if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
                or _JWT.search(value)):
            _fail("INVALID_RESPONSE_IDENTITY")
        return value

    @staticmethod
    def _usage(value: object) -> dict:
        if not isinstance(value, dict):
            _fail("USAGE_REQUIRED")
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            n = value.get(key)
            if type(n) is not int or n <= 0:
                _fail("INVALID_USAGE")
            result[key] = n
        if result["prompt_tokens"] + result["completion_tokens"] != result["total_tokens"]:
            _fail("INCONSISTENT_USAGE")
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if key in value:
                if type(value[key]) is not int or not 0 <= value[key] <= result["prompt_tokens"]:
                    _fail("INVALID_USAGE")
                result[key] = value[key]
        for parent, child, limit in (("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
                                     ("completion_tokens_details", "reasoning_tokens", "completion_tokens")):
            details = value.get(parent)
            if details is not None:
                if not isinstance(details, dict):
                    _fail("INVALID_USAGE")
                if child in details:
                    n = details[child]
                    if type(n) is not int or not 0 <= n <= result[limit]:
                        _fail("INVALID_USAGE")
                    result[parent] = {child: n}
        return result

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int = 16384, temperature: float = 0.2, schema: dict,
                      reasoning_effort: str = "high") -> dict:
        stage = self._stage()
        _check_schema(schema)
        if _canonical(self._clean(schema)) != _canonical(schema):
            _fail("UNSAFE_OUTPUT_SCHEMA")
        if (not isinstance(system, str) or not system.strip() or not isinstance(user, str) or not user.strip()
                or not isinstance(schema_hint, dict) or type(max_tokens) is not int
                or not 1 <= max_tokens <= 393216 or not _finite_number(temperature)
                or not 0 <= temperature <= 2 or type(reasoning_effort) is not str
                or reasoning_effort != stage_reasoning_effort(stage, upgrade=self.budget is not None)):
            _fail("INVALID_CHAT_REQUEST")
        request = build_chat_request(system=self._text(system), user=self._text(user),
            schema_hint=self._clean(schema_hint), schema=schema,
            max_tokens=max_tokens, temperature=temperature, reasoning_effort=reasoning_effort,
            thinking_mode=stage_thinking_mode(stage, upgrade=self.budget is not None))
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/chat/completions",
                  "request": request, "request_sha256": _hash(request), "output_schema": schema,
                  "schema_sha256": _hash(schema), "release": RELEASE}
        self._admit_call("chat", call_id, request, inputs)
        observation = {}

        def invoke():
            response, body_hash = self._post_json("/chat/completions", request)
            choices = response.get("choices")
            observed_finish = (choices[0].get("finish_reason") if isinstance(choices, list)
                               and len(choices) == 1 and isinstance(choices[0], dict) else None)
            observation["finish_reason"] = (observed_finish if observed_finish in
                ("stop", "length", "tool_calls", "content_filter") else
                "unknown" if observed_finish is None else "other")
            # Charge actual usage even if a later schema/identity check rejects
            # the answer. A failed answer must never erase its paid usage.
            usage = self._usage(response.get("usage"))
            self._settle_budget(call_id, usage)
            if response.get("model") != MODEL:
                _fail("RESPONSE_MODEL_MISMATCH")
            if response.get("object") != "chat.completion":
                _fail("RESPONSE_OBJECT_MISMATCH")
            response_id = self._identifier(response.get("id"))
            if self._api_key in response_id or any(r["response_id"] == response_id for r in self.receipts):
                _fail("RESPONSE_IDENTITY_REUSED_OR_UNSAFE")
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                _fail("INVALID_CHOICES")
            choice = choices[0]
            message = choice.get("message")
            if (type(choice.get("index")) is not int or choice["index"] != 0
                    or choice.get("finish_reason") != "stop" or not isinstance(message, dict)
                    or message.get("role") != "assistant" or message.get("tool_calls")
                    or message.get("function_call")):
                _fail("INCOMPLETE_MODEL_RESPONSE")
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                _fail("EMPTY_MODEL_OUTPUT")
            parsed = _json_loads(text)
            if not isinstance(parsed, dict) or not parsed:
                _fail("EMPTY_OR_NONOBJECT_MODEL_OUTPUT")
            parsed = self._clean(parsed)
            validate_schema(parsed, schema)
            fingerprint = response.get("system_fingerprint")
            if fingerprint is not None:
                fingerprint = self._identifier(fingerprint)
                if self._api_key in fingerprint:
                    _fail("UNSAFE_RESPONSE_FINGERPRINT")
            safe_response = {
                "id": response_id, "model": MODEL, "system_fingerprint": fingerprint,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": _canonical(parsed).decode("utf-8")}}],
                "usage": usage,
            }
            return {"schema": "tiger.deepseek-call.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "requested_model": MODEL, "response_id": response_id,
                    "model": MODEL, "release": RELEASE, "usage": usage,
                    "finish_reason": "stop", "request_sha256": inputs["request_sha256"],
                    "response_sha256": _hash(safe_response), "transport_response_sha256": body_hash,
                    "schema_sha256": inputs["schema_sha256"], "response": safe_response,
                    "system_fingerprint": fingerprint, "search_mode": SEARCH_MODE, "parsed": parsed,
                    **self._budget_proof(inputs)}

        proof = self._record_call("model.deepseek.chat", inputs, invoke, observation=observation)
        self.receipts.append({k: v for k, v in proof.items() if k not in ("schema", "parsed", "response")})
        return proof["parsed"]

    def _source_title(self, title: str) -> str:
        # Source formatting only: preserve the supplied words without markup,
        # control characters or repeated whitespace. Never invent or truncate.
        return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f<>]", " ", self._text(title)).split())

    def _document(self, *, url: str, title: str, content: str, published: object = None,
                  source: str) -> dict:
        title, content = self._source_title(title), self._text(content)
        if not title:
            _fail("SOURCE_TITLE_REQUIRED")
        if len(title) > 500:
            _fail("SOURCE_TITLE_LIMIT")
        if not content.strip():
            _fail("EMPTY_SEARCH_CONTENT")
        when = None
        if isinstance(published, str):
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    when = published
            except ValueError:
                when = None
        identity = {"url": url, "title": title, "content": content, "published": when}
        return {"id": "ds_" + _hash(identity)[:24], **identity, "source": source,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "content_hash": hashlib.sha256(content.encode()).hexdigest()}

    def search(self, query: str, count: int = 4, need_content: bool = True) -> list[dict]:
        stage = self._stage()
        if (not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128
                or type(count) is not int or not 1 <= count <= 50 or not isinstance(need_content, bool)):
            _fail("INVALID_SEARCH_REQUEST")
        query = self._text(query)
        if len(query) > 128:
            _fail("INVALID_SEARCH_REQUEST")
        request = {"query": query, "count": count, "need_content": need_content}
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-search-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/v1/tools/search",
                  "request": request, "request_sha256": _hash(request),
                  "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "search_mode": SEARCH_MODE}
        self._admit_call("search", call_id, request, inputs)

        def invoke():
            response, body_hash = self._post_json("/v1/tools/search", request)
            self._settle_budget(call_id, None)
            original = response.get("results")
            if not isinstance(original, list) or len(original) > count:
                _fail("INVALID_SEARCH_RESPONSE")
            results = []
            for item in original:
                if not isinstance(item, dict):
                    _fail("INVALID_SEARCH_RESULT")
                url = sanitize_url(item.get("url"), secret=self._api_key)
                if url is None:
                    continue
                title = item.get("title", "")
                title = self._source_title(title) if isinstance(title, str) else None
                content = item.get("content") or item.get("summary") or item.get("snippet")
                if (not title or len(title) > 500
                        or not isinstance(content, str) or not content.strip()):
                    continue
                document = self._document(url=url, title=title, content=content,
                               published=item.get("date"), source="deepseek_search")
                if self.budget is not None:
                    metadata = {key: item[key] for key in ("authority", "rerank_score", "site_name")
                                if key in item and (item[key] is None or type(item[key]) in (str, int, float))}
                    document["retrieval"] = self._clean(metadata)
                results.append(document)
            if not results and self.budget is None:
                _fail("NO_USABLE_SEARCH_RESULTS")
            return {"schema": "tiger.deepseek-search.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "method": "POST", "search_mode": SEARCH_MODE,
                    "query_sha256": inputs["query_sha256"], "request_sha256": inputs["request_sha256"],
                    "result_count": len(original), "usable_result_count": len(results),
                    "response_sha256": _hash({"results": results}),
                    "transport_response_sha256": body_hash, "results": results,
                    **self._budget_proof(inputs)}

        proof = self._record_call("model.deepseek.search", inputs, invoke)
        self.search_receipts.append({k: v for k, v in proof.items() if k not in ("schema", "results")})
        return proof["results"]

    def fetch(self, url: str) -> dict:
        stage = self._stage()
        safe_url = sanitize_url(url, secret=self._api_key)
        if safe_url is None:
            _fail("UNSAFE_FETCH_URL")
        request = {"url": safe_url}
        call_id = uuid.uuid4().hex
        inputs = {"schema": "tiger.deepseek-fetch-request.v1", "call_id": call_id, "stage": stage,
                  "provider": "deepseek", "method": "POST", "endpoint": BASE_URL + "/v1/tools/fetch",
                  "request": request, "request_sha256": _hash(request), "search_mode": SEARCH_MODE}
        self._admit_call("fetch", call_id, request, inputs)

        def invoke():
            response, body_hash = self._post_json("/v1/tools/fetch", request)
            self._settle_budget(call_id, None)
            if not isinstance(response.get("title"), str) or not isinstance(response.get("content"), str):
                _fail("INVALID_FETCH_RESPONSE")
            page = self._document(url=safe_url, title=response["title"], content=response["content"],
                                  source="deepseek_fetch")
            return {"schema": "tiger.deepseek-fetch.v1", "call_id": call_id, "stage": stage,
                    "provider": "deepseek", "method": "POST", "search_mode": SEARCH_MODE,
                    "request_sha256": inputs["request_sha256"], "response_sha256": _hash({"page": page}),
                    "transport_response_sha256": body_hash, "page": page,
                    **self._budget_proof(inputs)}

        proof = self._record_call("model.deepseek.fetch", inputs, invoke)
        self.fetch_receipts.append({k: v for k, v in proof.items() if k not in ("schema", "page")})
        return proof["page"]
