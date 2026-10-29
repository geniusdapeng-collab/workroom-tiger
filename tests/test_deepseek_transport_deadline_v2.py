"""Exercise actual Requests/urllib3 reads against a loopback-only HTTP peer."""
from __future__ import annotations

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError, SSLError

import trading_system.llm.deepseek_required as transport
from trading_system.redline.model_diagnostics import RequiredModelUnavailable
from test_deepseek_required import KEY, SCHEMA, RecordingExecution, Response, chat_response


@contextmanager
def local_peer(monkeypatch, *, delay=0.0, keepalive=False, partial=False):
    """No external I/O; do not log request headers, bodies or exception text."""
    observed = {"requests": 0, "responses": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            observed["requests"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                if keepalive or partial:
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                if keepalive:
                    until = time.monotonic() + delay
                    while time.monotonic() < until:
                        time.sleep(0.015)
                        self.wfile.write(b"\n")
                        self.wfile.flush()
                else:
                    time.sleep(delay)
                self.wfile.write(json.dumps(chat_response()).encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # The test deliberately closes expired reads. This peer has no
                # further work to perform after that expected disconnect.
                observed["peer_disconnected"] = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_post = requests.post

    def post(_url, **kwargs):
        response = original_post(f"http://127.0.0.1:{server.server_port}/", **kwargs)
        observed["responses"].append(response)
        return response

    monkeypatch.setattr(requests, "post", post)
    try:
        yield observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def client(monkeypatch):
    for name, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": transport.BASE_URL,
                        "LLM_MODEL": transport.MODEL, "LLM_API_KEY": KEY}.items():
        monkeypatch.setenv(name, value)
    execution = RecordingExecution()
    execution.upgrade = True
    return transport.DeepSeekRequiredClient(execution)


def test_chat_waits_past_native_idle_limit_inside_unchanged_total_deadline(client, monkeypatch):
    monkeypatch.setattr(transport, "READ_TIMEOUT", 0.12)
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.8)
    with local_peer(monkeypatch, delay=0.3) as observed:
        result = client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                                      reasoning_effort="low")
    assert result == {"score": 7, "reason": "evidence"}
    assert observed["requests"] == 1
    assert len(client.receipts) == 1 and observed["responses"][0].raw.closed
    assert client.budget.snapshot()["records"][0]["status"] == "settled"


@pytest.mark.parametrize("keepalive,partial", [(True, False), (False, True)])
def test_body_deadline_interrupts_keepalive_and_blocked_reads(client, monkeypatch, keepalive, partial):
    monkeypatch.setattr(transport, "READ_TIMEOUT", 0.12)
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.2)
    with local_peer(monkeypatch, delay=0.7, keepalive=keepalive, partial=partial) as observed:
        started = time.monotonic()
        with pytest.raises(RequiredModelUnavailable) as caught:
            client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                                  reasoning_effort="low")
        elapsed = time.monotonic() - started
        assert caught.value.diagnostic["code"] == "RESPONSE_DEADLINE"
        assert 0.15 <= elapsed < 0.5
    assert observed["requests"] == 1 and not client.receipts
    assert observed["responses"][0].raw.closed
    assert caught.value.accounting["status"] == "unknown"
    assert caught.value.accounting["frozen"] is True
    assert caught.value.accounting["usage"] is None


def test_native_search_retains_shorter_idle_timeout_without_retry(client, monkeypatch):
    monkeypatch.setattr(transport, "READ_TIMEOUT", 0.12)
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.8)
    client.execution._active.spec.name = "search.collect"
    with local_peer(monkeypatch, delay=0.3) as observed:
        with pytest.raises(RequiredModelUnavailable) as caught:
            client.search("public evidence")
    assert caught.value.diagnostic["code"] == "NETWORK_TIMEOUT"
    assert observed["requests"] == 1 and not client.search_receipts
    assert observed["responses"][0].raw.closed
    assert caught.value.accounting["counts"] == {"chat": 0, "search": 1, "fetch": 0}
    assert caught.value.accounting["status"] == "unknown"


@pytest.mark.parametrize("error,code", [
    (ReadTimeoutError(None, None, KEY + " private-response"), "NETWORK_TIMEOUT"),
    (ProtocolError(KEY + " private-response"), "NETWORK_PROTOCOL_ERROR"),
    (SSLError(KEY + " private-response"), "NETWORK_TLS_ERROR"),
])
def test_raw_urllib3_failures_have_closed_safe_categories(client, monkeypatch, error, code):
    response = Response(chat_response())

    class FailingRaw:
        def read1(self, _size):
            raise error

    response.raw = FailingRaw()
    seen = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: seen.append(1) or response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                              reasoning_effort="low")
    assert caught.value.diagnostic == {
        "schema": "tiger.model-failure.v1", "provider": "deepseek", "code": code}
    assert response.closed and seen == [1]
    assert caught.value.accounting["status"] == "unknown"
    serialized = json.dumps([client.execution.calls, caught.value.diagnostic,
                             caught.value.accounting, str(caught.value)])
    assert KEY not in serialized and "private-response" not in serialized


def test_success_cancels_deadline_interrupt_and_leaves_no_live_watchdog(client, monkeypatch):
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.4)
    response = Response(chat_response())
    shutdowns = []
    response.raw.shutdown = lambda: shutdowns.append(1)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    assert client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                                reasoning_effort="low")["score"] == 7
    time.sleep(0.45)
    assert shutdowns == [] and response.closed


@pytest.mark.parametrize("error", [ReadTimeoutError, ProtocolError, SSLError])
def test_deadline_shutdown_takes_priority_over_resulting_transport_exception(client, monkeypatch, error):
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.04)

    class BlockedRaw:
        def __init__(self):
            self.interrupted = threading.Event()
            self.shutdowns = 0

        def read1(self, _size):
            assert self.interrupted.wait(0.8), "Local deadline did not interrupt the fixture"
            if error is ReadTimeoutError:
                raise error(None, None, KEY)
            raise error(KEY)

        def shutdown(self):
            self.shutdowns += 1
            self.interrupted.set()

    response = Response(chat_response())
    response.raw = BlockedRaw()
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                              reasoning_effort="low")
    assert caught.value.diagnostic["code"] == "RESPONSE_DEADLINE"
    assert caught.value.accounting["status"] == "unknown"
    assert response.raw.shutdowns == 1 and response.closed


@pytest.mark.parametrize("error", [ReadTimeoutError, ProtocolError, SSLError])
def test_monotonic_deadline_does_not_depend_on_timer_thread_scheduling(client, monkeypatch, error):
    monkeypatch.setattr(transport, "RESPONSE_DEADLINE", 0.015)

    class DelayedRaw:
        def read1(self, _size):
            time.sleep(0.04)
            if error is ReadTimeoutError:
                raise error(None, None, KEY)
            raise error(KEY)

    # This deliberately supplies no shutdown callback, modelling an interrupt
    # that has not run yet. The monotonic acceptance boundary still applies.
    response = Response(chat_response())
    response.raw = DelayedRaw()
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(RequiredModelUnavailable) as caught:
        client.complete_json(system="s", user="u", schema_hint={}, schema=SCHEMA,
                              reasoning_effort="low")
    assert caught.value.diagnostic["code"] == "RESPONSE_DEADLINE"
    assert caught.value.accounting["frozen"] is True and response.closed
