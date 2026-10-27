"""SYNTHETIC_TEST safe provider claims; mocked HTTP and bounded local clocks."""
from copy import deepcopy
import json
import threading
import time

import pytest

from trading_system.llm import deepseek_required as transport
from trading_system.redline.model_diagnostics import (
    RequiredModelUnavailable, validate_model_diagnostic, validate_model_failure_accounting,
)
from test_deepseek_required import configured, Response, KEY, SCHEMA
from test_clean_responses_output_v1 import responses_client, keyed_contract, call, POLICY


def interruptible_error(*, body=None, payload=None, status=400, headers=None):
    response = Response(payload, body=body, status=status, headers=headers)
    response.raw.shutdown = lambda: None
    return response


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("error,category", [
    ({"param": "text.format.schema.properties.d", "message": KEY}, "schema"),
    ({"param": "tool_choice", "message": KEY}, "tool_choice"),
    ({"param": "model", "message": KEY}, "model"),
    ({"param": "temperature", "message": KEY}, "parameters"),
    ({"code": "invalid_json_schema", "message": KEY}, "schema"),
    ({"code": "invalid_tool_choice", "message": KEY}, "tool_choice"),
    ({"code": "invalid_model", "message": KEY}, "model"),
    ({"code": "invalid_parameter", "message": KEY}, "parameters"),
    ({"message": "SYNTHETIC_REJECTED_HTTP_BODY Invalid schema " + KEY}, "schema"),
    ({"message": "SYNTHETIC_REJECTED_HTTP_BODY Invalid tool_choice " + KEY}, "tool_choice"),
    ({"message": "SYNTHETIC_REJECTED_HTTP_BODY Invalid parameter " + KEY}, "parameters"),
    ({"message": "SYNTHETIC_REJECTED_HTTP_BODY Invalid model " + KEY}, "model"),
    ({"type": "invalid_request_error", "message": KEY}, "request"),
    ({"message": KEY + " https://example.com/?token=private-response"}, "unknown"),
    ({"message": "SYNTHETIC_REJECTED_HTTP_BODY schema model parameters"}, "unknown"),
])
def test_http_error_retains_only_fixed_provider_claim_and_unknown_accounting(configured, monkeypatch, status, error, category):
    *_, schema, _ = keyed_contract()
    response = interruptible_error(payload={"error": error}, status=status)
    seen = []
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: (seen.append(1), response)[1])
    client = responses_client(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(client, schema)
    assert response.closed and seen == [1] and client.receipts == []
    diagnostic = caught.value.diagnostic
    assert diagnostic == {"schema": "tiger.model-failure.v2", "provider": "deepseek", "code": "HTTP_ERROR",
        "http_status": status, "provider_error": {"schema": "tiger.responses-provider-error.v1",
            "category": category, "body_state": "parsed"}}
    assert validate_model_diagnostic(diagnostic) == diagnostic
    accounting = caught.value.accounting
    assert accounting["schema"] == "tiger.model-call-accounting.v4" and accounting["output_policy_id"] == POLICY
    assert accounting["response_status"] == "unknown" and accounting["status"] == "unknown"
    assert accounting["native_usage"] is None and accounting["usage"] is None and accounting["frozen"] is True
    assert accounting["actual_tokens"] == 0 and accounting["reserved_tokens"] > 16384
    with pytest.raises(RequiredModelUnavailable, match="REQUEST_BUDGET_REFUSED"):
        call(client, schema)
    assert seen == [1]
    serialized = json.dumps([configured.calls, diagnostic, accounting, str(caught.value)])
    assert all(secret not in serialized for secret in (KEY, "private-response", "SYNTHETIC_REJECTED_HTTP_BODY"))


@pytest.mark.parametrize("body,headers,state", [
    (b'{"error":{"message":"schema","message":"duplicate"}}', {}, "malformed"),
    (b'{"error":{"code":NaN}}', {}, "malformed"),
    (b'{"error":{"code":1e999}}', {}, "malformed"),
    (b'\xff', {}, "malformed"),
    (b'[]', {}, "malformed"),
    (b'{"error":"wrong-type"}', {}, "malformed"),
    (b'{"error":{"message":{"private":"raw"}}}', {}, "malformed"),
    (b'{"error":{"code":["private"]}}', {}, "malformed"),
    (b'{"error":{"message":null}}', {}, "malformed"),
    (b'{"error":{"unreviewed":"private"}}', {}, "malformed"),
    (b'{"error":{}}', {}, "malformed"),
    (b'{"error":{"message":"schema"},"unexpected":"private"}', {}, "malformed"),
    (b'{' + b'x' * 16384, {}, "too_large"),
    (b'{}', {"Content-Length": "16385"}, "too_large"),
    (b'{}', {"Content-Length": "999999999999999999999"}, "too_large"),
    (b'{}', {"Content-Length": "invalid"}, "malformed"),
    (b'{}', {"Content-Encoding": "gzip"}, "unavailable"),
    (b'{}', {"Content-Type": "text/html"}, "unavailable"),
])
def test_error_body_limits_and_strict_json_never_release_or_expose_body(configured, monkeypatch, body, headers, state):
    *_, schema, _ = keyed_contract()
    response = interruptible_error(body=body, headers=headers)
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    assert caught.value.diagnostic["http_status"] == 400 and response.closed
    assert caught.value.diagnostic["provider_error"] == {
        "schema": "tiger.responses-provider-error.v1", "category": "unknown", "body_state": state}
    assert caught.value.accounting["status"] == "unknown" and caught.value.accounting["frozen"] is True
    assert "private" not in json.dumps(caught.value.diagnostic)


def test_error_diagnostic_never_reads_a_body_without_an_owned_interrupt(configured, monkeypatch):
    *_, schema, _ = keyed_contract()
    response = Response(body=KEY.encode(), status=400)
    reads = []

    class Uninterruptible:
        def read1(self, _size):
            reads.append(1)
            raise AssertionError("An unbounded read must never start")

    response.raw = Uninterruptible()
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    assert reads == [] and response.closed
    assert caught.value.diagnostic["provider_error"]["body_state"] == "unavailable"


def test_error_body_read_is_actually_byte_bounded(configured, monkeypatch):
    *_, schema, _ = keyed_contract()
    response = interruptible_error(body=b"x" * 100000)
    raw = response.raw
    reads = []

    class Counted:
        def shutdown(self):
            return None

        def read1(self, size):
            value = raw.read1(size)
            reads.append((size, len(value)))
            return value

    response.raw = Counted()
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    assert sum(size for _, size in reads) == 16385 and max(size for size, _ in reads) <= 4096
    assert caught.value.diagnostic["provider_error"]["body_state"] == "too_large" and response.closed


def test_error_observation_interrupts_blocked_read_and_cancels_its_watchdog(configured, monkeypatch):
    *_, schema, _ = keyed_contract()
    monkeypatch.setattr(transport, "RESPONSES_ERROR_DEADLINE", .04)
    response = interruptible_error(body=b"unused")

    class Blocked:
        def __init__(self):
            self.event = threading.Event()
            self.shutdowns = 0

        def read1(self, _size):
            assert self.event.wait(.8), "Diagnostic deadline failed to interrupt"
            raise OSError(KEY + " private-response")

        def shutdown(self):
            self.shutdowns += 1
            self.event.set()

    response.raw = Blocked()
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    started = time.monotonic()
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    assert .025 <= time.monotonic() - started < .5
    assert response.closed and response.raw.shutdowns == 1
    assert caught.value.diagnostic["code"] == "HTTP_ERROR" and caught.value.diagnostic["http_status"] == 400
    assert caught.value.diagnostic["provider_error"]["body_state"] == "unavailable"
    time.sleep(.06)
    assert response.raw.shutdowns == 1


def test_error_successful_observation_leaves_no_live_deadline_callback(configured, monkeypatch):
    *_, schema, _ = keyed_contract()
    monkeypatch.setattr(transport, "RESPONSES_ERROR_DEADLINE", .03)
    response = interruptible_error(payload={"error": {"param": "text.format.schema"}})
    shutdowns = []
    response.raw.shutdown = lambda: shutdowns.append(1)
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    assert caught.value.diagnostic["provider_error"]["category"] == "schema"
    time.sleep(.05)
    assert shutdowns == [] and response.closed


@pytest.mark.parametrize("path", ["/chat/completions", "/beta/chat/completions", "/v1/tools/search", "/v1/tools/fetch"])
def test_legacy_endpoints_keep_original_http_diagnostic_without_body_read(configured, monkeypatch, path):
    response = interruptible_error(payload={"error": {"message": KEY}})
    reads = []

    class ForbiddenRead:
        def shutdown(self):
            reads.append("shutdown")

        def read1(self, _size):
            reads.append("read")
            raise AssertionError("Historical endpoints never read HTTP error bodies")

    response.raw = ForbiddenRead()
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    client = transport.DeepSeekRequiredClient(configured)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client._post_json(path, {})
    assert caught.value.diagnostic == {"schema": "tiger.model-failure.v1", "provider": "deepseek",
        "code": "HTTP_ERROR", "http_status": 400}
    assert reads == [] and response.closed


@pytest.mark.parametrize("mutation", ["category", "body_state", "schema", "extra", "status", "code"])
def test_safe_provider_diagnostic_rejects_unregistered_fields_and_labels(mutation):
    diagnostic = {"schema": "tiger.model-failure.v2", "provider": "deepseek", "code": "HTTP_ERROR",
        "http_status": 400, "provider_error": {"schema": "tiger.responses-provider-error.v1",
            "category": "schema", "body_state": "parsed"}}
    if mutation == "category": diagnostic["provider_error"]["category"] = KEY
    elif mutation == "body_state": diagnostic["provider_error"]["body_state"] = "malformed"
    elif mutation == "schema": diagnostic["provider_error"]["schema"] = "unregistered"
    elif mutation == "extra": diagnostic["provider_error"]["message"] = KEY
    elif mutation == "status": diagnostic["http_status"] = 500
    else: diagnostic["code"] = "TRANSPORT_FAILURE"
    with pytest.raises(ValueError):
        validate_model_diagnostic(diagnostic)


class EqualityString(str):
    """An in-process forged label must not survive as its underlying text."""
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__


@pytest.mark.parametrize("field", ["schema", "provider", "detail_schema", "category", "body_state"])
def test_new_provider_error_labels_require_exact_strings_not_forged_equality(field):
    diagnostic = {"schema": "tiger.model-failure.v2", "provider": "deepseek", "code": "HTTP_ERROR",
        "http_status": 400, "provider_error": {"schema": "tiger.responses-provider-error.v1",
            "category": "schema", "body_state": "parsed"}}
    if field in ("schema", "provider"):
        diagnostic[field] = EqualityString(KEY)
    else:
        diagnostic["provider_error"]["schema" if field == "detail_schema" else field] = EqualityString(KEY)
    with pytest.raises(ValueError):
        validate_model_diagnostic(diagnostic)


@pytest.mark.parametrize("field", ["schema", "budget_policy_id", "output_policy_id", "response_status"])
def test_native_failure_accounting_labels_require_exact_strings(configured, monkeypatch, field):
    *_, schema, _ = keyed_contract()
    response = interruptible_error(payload={"error": {"message": "SYNTHETIC_REJECTED_HTTP_BODY"}})
    monkeypatch.setattr(transport.requests, "post", lambda *a, **kw: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        call(responses_client(configured), schema)
    accounting = caught.value.accounting
    accounting[field] = EqualityString(KEY)
    with pytest.raises(ValueError):
        validate_model_failure_accounting(accounting)
