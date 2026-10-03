"""Data validation and deterministic diagnostics at external-data boundaries.

External exception messages and response bodies are deliberately excluded from
diagnostics: both may contain request credentials or private request contents.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class InvalidJSON(ValueError):
    """A response is not a JSON tree containing only finite numbers."""


def ensure_finite_tree(value) -> None:
    """Reject non-JSON types, cycles and non-finite numbers at any depth."""
    active: set[int] = set()

    def visit(node):
        if node is None or isinstance(node, (str, bool, int)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise InvalidJSON("Non-finite JSON number")
            return
        if not isinstance(node, (dict, list)):
            raise InvalidJSON("Invalid JSON value type")
        identity = id(node)
        if identity in active:
            raise InvalidJSON("Cyclic JSON container")
        active.add(identity)
        try:
            if isinstance(node, dict):
                if any(not isinstance(key, str) for key in node):
                    raise InvalidJSON("Invalid JSON object key")
                children = node.values()
            else:
                children = node
            for child in children:
                visit(child)
        finally:
            active.remove(identity)

    try:
        visit(value)
    except RecursionError:
        raise InvalidJSON("JSON nesting exceeds validation limit") from None


def strict_json_loads(text: str | bytes):
    """Parse JSON without NaN/Infinity extensions or float-overflow values."""
    def reject_constant(_value):
        raise InvalidJSON("Non-finite JSON number")

    try:
        value = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError:
        # Keep the standard type for callers that intentionally accept a JSON
        # object wrapped in model prose, but discard the original document.
        raise json.JSONDecodeError("Invalid JSON response", "", 0) from None
    except InvalidJSON:
        raise InvalidJSON("Non-finite JSON number") from None
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise InvalidJSON("Invalid JSON response") from None
    ensure_finite_tree(value)
    return value


def finite_timestamp(value) -> float | None:
    """Normalize a numeric timestamp without accepting bools or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def http_status(value) -> int | None:
    """Keep only an actual HTTP status integer; never stringify remote values."""
    return value if type(value) is int and 100 <= value <= 599 else None


@dataclass(frozen=True)
class SafeDiagnostic:
    category: str
    status: int | None = None

    @property
    def summary(self) -> str:
        return self.category + (f" (HTTP {self.status})" if self.status is not None else "")


_ERROR_CATEGORIES = {
    "InvalidJSON", "JSONDecodeError", "HTTPError", "URLError",
    "Timeout", "TimeoutError", "ConnectTimeout", "ReadTimeout",
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "PermissionError", "ModuleNotFoundError", "ImportError", "OSError",
    "ValueError", "TypeError", "RuntimeError", "LLMUnavailable", "SearchUnavailable",
}


def safe_diagnostic(error: Exception) -> SafeDiagnostic:
    """Keep an allowlisted exception category and, where available, HTTP status."""
    category = next((cls.__name__ for cls in type(error).__mro__
                     if cls.__name__ in _ERROR_CATEGORIES), "UpstreamError")
    try:
        status = http_status(getattr(error, "status_code", None))
        if status is None:
            status = http_status(getattr(error, "status", None))
        if status is None:
            status = http_status(getattr(error, "code", None))
        if status is None:
            response = getattr(error, "response", None)
            status = http_status(getattr(response, "status_code", None))
    except Exception:
        # An exception's properties are also untrusted. The original failure
        # remains visible through its category even if status access fails.
        status = None
    return SafeDiagnostic(category, status)


def safe_origin(url: str) -> str:
    """Describe only an HTTP(S) origin, excluding credentials, path and query."""
    if not isinstance(url, str) or any(ord(c) < 32 for c in url) or "\\" in url:
        return "configured-endpoint"
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return "configured-endpoint"
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.:-]+", host):
            return "configured-endpoint"
        port = parts.port
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port is not None else "")
    except (ValueError, UnicodeError):
        return "configured-endpoint"
