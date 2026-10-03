"""MC-130 regressions: isolated clocks, sources and synthetic diagnostics.

No host credentials, model calls, broker actions or public network are used.
The canary is generated at runtime. RED logs may expose that synthetic value;
GREEN diagnostics must contain none of it. No real credential is read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import traceback
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import requests


PATENT_ENDPOINT = "https://search.patentsview.org/api/v1/patent/"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    # Replace the mapping instead of reading any host credential values.
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)
    attempted = []

    def blocked(*args, **kwargs):
        attempted.append("unstubbed-network")
        raise AssertionError("External network is forbidden in MC-130 regressions")

    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr("urllib.request.urlopen", blocked)
    yield
    assert attempted == [], "A test attempted an unstubbed external call"


@pytest.fixture
def canary():
    return hashlib.sha256(os.urandom(32)).hexdigest()


def _doc(source="fixture", marker="doc", published="2026-10-02", **kwargs):
    from trading_system.search.models import RawDocument
    return RawDocument(marker, source, marker, f"https://example.invalid/{marker}",
                       f"A real fixture document body for {marker}.", published, **kwargs)


class _Source:
    def __init__(self, name="fixture", marker="doc", error=None):
        self.name, self.marker, self.error = name, marker, error
        self.calls = 0

    def cache_identity(self):
        return {"marker": self.marker}

    def search(self, query, limit=8):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [_doc(self.name, f"{self.marker}-{i}") for i in range(limit)]


def _hub(monkeypatch, tmp_path, sources, **kwargs):
    from trading_system.search import hub
    monkeypatch.setattr(hub, "CACHE_DIR", str(tmp_path / "search-cache"))
    return hub.SearchHub(sources=sources, ttl_tiers={}, **kwargs)


def test_cache_demo_and_real_sources_are_isolated(monkeypatch, tmp_path):
    from trading_system.search import hub
    monkeypatch.setattr(hub, "CACHE_DIR", str(tmp_path / "search-cache"))
    demo = hub.SearchHub(demo=True, ttl_tiers={})
    assert {d.source for d in demo.search("same-query").docs} == {"demo"}
    real = _Source("edgar", "real")
    batch = hub.SearchHub(sources=[real], ttl_tiers={}).search("same-query", 2)
    assert {d.source for d in batch.docs} == {"edgar"}
    assert real.calls == 1


def test_cache_source_routing_and_empty_selection(monkeypatch, tmp_path):
    a, b = _Source("a"), _Source("b")
    h = _hub(monkeypatch, tmp_path, [a, b])
    assert {d.source for d in h.search("route", sources=["a"]).docs} == {"a"}
    assert {d.source for d in h.search("route", sources=["b"]).docs} == {"b"}
    assert h.search("route", sources=[]).docs == []
    assert h.search("route", sources=["unknown"]).docs == []
    assert a.calls == b.calls == 1


def test_cache_equivalent_source_selection_reuses_success(monkeypatch, tmp_path):
    a, b = _Source("a"), _Source("b")
    h = _hub(monkeypatch, tmp_path, [a, b], use_disk_cache=False)
    first = h.search("route", sources=["a", "b"])
    second = h.search("route", sources=["b", "a", "a"])
    assert a.calls == b.calls == 1
    assert first.source_stats["a"] == second.source_stats["a"]
    assert first.source_stats["b"] == second.source_stats["b"]
    assert second.source_stats["cache"]["ok"] is True


def test_cache_source_configuration_and_mutation_are_isolated(monkeypatch, tmp_path):
    a, b = _Source("fixture", "config-a"), _Source("fixture", "config-b")
    first = _hub(monkeypatch, tmp_path, [a]).search("same", 1)
    h = _hub(monkeypatch, tmp_path, [b])
    second = h.search("same", 1)
    assert first.docs[0].doc_id != second.docs[0].doc_id
    b.marker = "config-c"
    assert h.search("same", 1).docs[0].doc_id == "config-c-0"
    assert b.calls == 2


def test_cache_builtin_source_configuration_isolated(monkeypatch, tmp_path):
    from trading_system.search.sources import GoogleNewsSource
    a = GoogleNewsSource(lang="en-US", country="US")
    b = GoogleNewsSource(lang="zh-CN", country="CN")
    monkeypatch.setattr(a, "search", lambda *args: [_doc("google_news", "english")])
    monkeypatch.setattr(b, "search", lambda *args: [_doc("google_news", "chinese")])
    assert _hub(monkeypatch, tmp_path, [a]).search("same").docs[0].doc_id == "english"
    assert _hub(monkeypatch, tmp_path, [b]).search("same").docs[0].doc_id == "chinese"


@pytest.mark.parametrize("dimension", ["demo", "disk", "ttl", "budget"],
                         ids=["demo-mode", "disk-mode", "ttl-policy", "deadline-policy"])
def test_cache_namespace_contains_execution_configuration(monkeypatch, tmp_path, dimension):
    s = _Source()
    h = _hub(monkeypatch, tmp_path, [s])
    baseline = h._cache_key("query", 8)
    if dimension == "demo":
        other = _hub(monkeypatch, tmp_path, [s], demo=True)
    elif dimension == "disk":
        other = _hub(monkeypatch, tmp_path, [s], use_disk_cache=False)
    elif dimension == "ttl":
        other = _hub(monkeypatch, tmp_path, [s], ttl=1)
    else:
        other = _hub(monkeypatch, tmp_path, [s], budget=1)
    assert baseline != other._cache_key("query", 8)


def test_cache_limit_category_and_delimiter_boundaries(monkeypatch, tmp_path):
    s = _Source()
    h = _hub(monkeypatch, tmp_path, [s], use_disk_cache=False)
    assert len(h.search("query", 1).docs) == 1
    assert len(h.search("query", 3).docs) == 3
    h.search("query", 3, category="announcement")
    assert s.calls == 3
    assert h._cache_key("c", 8, "a|b") != h._cache_key("b|c", 8, "a")


def test_cache_success_replay_preserves_source_health_on_disk(monkeypatch, tmp_path):
    first_source = _Source()
    first = _hub(monkeypatch, tmp_path, [first_source]).search("cached", 2)
    next_source = _Source()
    second = _hub(monkeypatch, tmp_path, [next_source]).search("cached", 2)
    assert next_source.calls == 0
    assert second.source_stats["fixture"] == first.source_stats["fixture"]
    assert second.source_stats["cache"]["ok"] is True
    assert [d.doc_id for d in first.docs] == [d.doc_id for d in second.docs]


@pytest.mark.parametrize("partial", [False, True], ids=["total-failure", "partial-failure"])
def test_cache_does_not_promote_failed_sources(monkeypatch, tmp_path, partial):
    bad = _Source("bad", error=ConnectionError("fixture failure"))
    sources = [bad, _Source("good")] if partial else [bad]
    h = _hub(monkeypatch, tmp_path, sources)
    for _ in range(2):
        batch = h.search("same", 1)
        assert batch.source_stats["bad"]["ok"] is False
        assert "cache" not in batch.source_stats
        assert len(batch.docs) == int(partial)
    assert bad.calls == 2
    assert not list((tmp_path / "search-cache").glob("*.json"))


def test_cache_circuit_open_is_disclosed_and_recovery_is_not_cached(monkeypatch, tmp_path):
    from trading_system.search import hub
    bad = _Source("bad", error=ConnectionError("fixture failure"))
    h = _hub(monkeypatch, tmp_path, [bad], use_disk_cache=False)
    for i in range(hub.BREAKER_FAILS):
        assert h.search(f"q-{i}").source_stats["bad"]["ok"] is False
    skipped = h.search("skipped")
    assert skipped.source_stats["bad"]["ok"] is False
    assert "circuit" in skipped.source_stats["bad"]["err"]
    bad.error = None
    opened = h._breaker.opened_at["bad"]
    monkeypatch.setattr(hub.time, "time", lambda: opened + hub.BREAKER_COOLDOWN + 1)
    assert h.search("skipped", 1).source_stats["bad"]["ok"] is True
    assert bad.calls == hub.BREAKER_FAILS + 1


def test_cache_disk_load_preserves_original_expiry(monkeypatch, tmp_path):
    from trading_system.search import hub
    h = _hub(monkeypatch, tmp_path, [], ttl=100)
    key = h._cache_key("expiry", 8)
    h._cache_put(key, [_doc()])
    h._mem.clear()
    path = next((tmp_path / "search-cache").glob("*.json"))
    original = time.time() - 90
    os.utime(path, (original, original))
    assert h._cache_get(key) is not None
    monkeypatch.setattr(hub.time, "time", lambda: original + 101)
    assert h._cache_get(key) is None


@pytest.mark.parametrize("payload", ["[]", "{}", "not-json", '{"created_at":NaN}'],
                         ids=["legacy-array", "missing-envelope", "invalid-json", "nonfinite-cache"])
def test_cache_corrupt_disk_entry_is_a_miss(monkeypatch, tmp_path, payload):
    h = _hub(monkeypatch, tmp_path, [])
    key = h._cache_key("corrupt", 8)
    (tmp_path / "search-cache" / f"{key}.json").write_text(payload)
    assert h._cache_get(key) is None


def test_cache_atomic_writes_allow_concurrent_readers(monkeypatch, tmp_path):
    s = _Source()
    h = _hub(monkeypatch, tmp_path, [s])
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda _: h.search("concurrent", 1), range(24)))
    assert all(b.docs[0].doc_id == "doc-0" for b in batches)
    paths = list((tmp_path / "search-cache").glob("*.json"))
    assert len(paths) == 1
    assert isinstance(json.loads(paths[0].read_text()), dict)
    assert not list((tmp_path / "search-cache").glob("*.tmp"))


def test_cache_wall_clock_deadline_keeps_partial_results(monkeypatch, tmp_path):
    released = threading.Event()

    class Slow(_Source):
        def search(self, query, limit=8):
            released.wait(2)
            return [_doc("slow")]

    h = _hub(monkeypatch, tmp_path, [Slow("slow"), _Source("fast")],
             budget=0.05, use_disk_cache=False)
    started = time.monotonic()
    try:
        batch = h.search("deadline", 1)
        assert time.monotonic() - started < 0.5
        assert {d.source for d in batch.docs} == {"fast"}
        assert batch.source_stats["slow"]["ok"] is False
        assert "timeout" in batch.source_stats["slow"]["err"]
        assert h._cache_get(h._cache_key("deadline", 1)) is None
    finally:
        released.set()


@pytest.mark.parametrize("url,expected", [
    ("https://sec.gov@evil.invalid/a", "T3"),
    (urllib.parse.urlunsplit(("https", "sec.gov:password@evil.invalid", "/a", "", "")), "T3"),
    (urllib.parse.urlunsplit(("https", "user:password@sec.gov:443", "/a", "", "")), "T0"),
    ("https://WWW.SEC.GOV.:443/a", "T0"),
    ("https://sec.gov.evil.invalid/a", "T3"),
    ("https://evil.invalid/?next=https://sec.gov/a", "T3"),
    ("https://[::1]:443/a", "T3"),
    ("https://[broken/a", "T3"),
    ("https://sec.gov:bad/a", "T3"),
    ("//sec.gov/a", "T3"),
    ("https://evil.invalid\\@sec.gov/a", "T3"),
    ("https://evil\x00.sec.gov/a", "T3"),
], ids=["userinfo-host", "userinfo-port", "real-host", "case-dot-port", "suffix-spoof",
        "query-spoof", "ipv6", "malformed-ipv6", "invalid-port", "relative-url",
        "ambiguous-backslash", "control-character"])
def test_domain_uses_valid_actual_hostname(url, expected):
    from trading_system.search.credibility import tier_for_source
    assert tier_for_source("reddit", url).name == expected


@pytest.mark.parametrize("value", ["NaN", "nan", "Infinity", "-Infinity", "1e400", "-1e400",
                                   float("nan"), float("inf"), float("-inf"), None, True, [], {}],
                         ids=["nan-text", "nan-lower", "positive-infinity", "negative-infinity",
                              "overflow", "negative-overflow", "nan-number", "inf-number",
                              "negative-inf-number", "missing", "boolean", "array", "object"])
def test_published_rejects_nonfinite_and_wrong_types(value):
    from trading_system.search.credibility import parse_published
    assert parse_published(value) is None


@pytest.mark.parametrize("value,expected", [
    ("2024-01-01", 1704067200.0),
    ("2024-01-01T08:00:00+08:00", 1704067200.0),
    ("Mon, 01 Jan 2024 00:00:00 GMT", 1704067200.0),
    ("1704067200.0", 1704067200.0),
    (1704067200, 1704067200.0),
    ("", None), ("2024-02-30", None),
], ids=["iso-date", "iso-offset", "rfc-date", "epoch-text", "epoch-number", "empty", "bad-date"])
def test_published_valid_formats_and_missing_disclosure(value, expected):
    from trading_system.search.credibility import parse_published
    assert parse_published(value) == expected


def test_published_nonfinite_is_disclosed_as_missing():
    from trading_system.search.credibility import CrossValidator
    from trading_system.search.models import CleanDocument
    docs = [CleanDocument(_doc(published="1e400"), degraded=True)]
    stats = CrossValidator().validate(docs)
    assert docs[0].evidence.published_at is None
    assert stats["missing_published_at"] == 1


@pytest.mark.parametrize("where", ["fetched", "metadata", "evidence-published", "evidence-fetched"])
def test_finite_document_and_evidence_boundaries(where):
    from trading_system.search.models import Evidence
    with pytest.raises(ValueError):
        if where == "fetched":
            _doc(fetched_at=float("nan"))
        elif where == "metadata":
            _doc(meta={"nested": [{"score": float("inf")}]})
        elif where == "evidence-published":
            Evidence("body", "fixture", published_at=float("inf"))
        else:
            Evidence("body", "fixture", fetched_at=float("nan"))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"],
                         ids=["nan", "infinity", "negative-infinity", "overflow", "negative-overflow"])
@pytest.mark.parametrize("wrapper", [False, True], ids=["plain-json", "wrapped-json"])
def test_finite_llm_nested_json_is_required(literal, wrapper):
    from trading_system.llm.client import KimiGatewayClient
    from trading_system.redline import LLMUnavailable
    text = '{"nested":[{"value":' + literal + '}]}'
    if wrapper:
        text = "Model response:\n```json\n" + text + "\n```"
    with pytest.raises(LLMUnavailable):
        KimiGatewayClient._parse_json(text)


def test_finite_llm_valid_json_and_string_values_are_preserved():
    from trading_system.llm.client import KimiGatewayClient
    payload = {"nested": [None, True, 1, -2.5, {"literal": "NaN"}], "count": 0}
    assert KimiGatewayClient._parse_json("```json\n" + json.dumps(payload) + "\n```") == payload


def test_finite_llm_failure_only_passthrough_with_trace():
    from trading_system.llm.client import KimiGatewayClient
    from trading_system.redline import ExecutionTracer, Passthrough, llm_guard
    payload = [_doc()]
    tracer = ExecutionTracer()
    with tracer.step("clean.llm_semantic"):
        result = llm_guard("clean.llm_semantic",
                           lambda: KimiGatewayClient._parse_json('{"score":1e400}'),
                           payload, tracer)
    assert isinstance(result, Passthrough) and result.payload is payload
    assert tracer.records[0].status == "passthrough"
    assert "LLM不可用" in tracer.records[0].note


@pytest.mark.parametrize("location", ["userinfo", "query", "fragment", "path"])
def test_diagnostic_llm_describe_keeps_only_safe_origin(canary, location):
    from trading_system.llm.client import OpenAICompatClient
    urls = {"userinfo": urllib.parse.urlunsplit(("https", f"user:{canary}@example.invalid:8443", "/v1", "", "")),
            "query": f"https://example.invalid/v1?token={canary}",
            "fragment": f"https://example.invalid/v1#{canary}",
            "path": f"https://example.invalid/{canary}/v1"}
    client = OpenAICompatClient(urls[location], canary, "fixture-model")
    text = client.describe()
    assert canary not in text
    assert "example.invalid" in text and "fixture-model" in text


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_diagnostic_llm_http_failure_does_not_read_remote_body(monkeypatch, canary, caplog, status):
    from trading_system.llm.client import OpenAICompatClient
    from trading_system.redline import LLMUnavailable

    class Response:
        status_code = status

        @property
        def text(self):
            raise AssertionError(canary)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    client = OpenAICompatClient("https://example.invalid/v1", canary, "fixture", max_retries=0)
    with caplog.at_level(logging.INFO), pytest.raises(LLMUnavailable) as caught:
        client.complete_json(system="fixture", user="fixture", schema_hint={})
    diagnostic = str(caught.value) + caplog.text + "".join(traceback.format_exception(caught.value))
    assert canary not in diagnostic and str(status) in diagnostic


@pytest.mark.parametrize("backend", ["api", "gateway"])
def test_diagnostic_llm_connection_retry_does_not_leak(monkeypatch, canary, caplog, backend):
    from trading_system.llm.client import KimiGatewayClient, OpenAICompatClient
    from trading_system.redline import LLMUnavailable
    calls = []

    def failure(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("Remote authorization echo " + canary)

    if backend == "api":
        monkeypatch.setattr(requests, "post", failure)
        client = OpenAICompatClient("https://example.invalid/v1", canary, "fixture", max_retries=2)
    else:
        client = KimiGatewayClient(max_retries=2)
        monkeypatch.setattr(client, "_gw", lambda: SimpleNamespace(chat_completion=failure))
    with caplog.at_level(logging.INFO), pytest.raises(LLMUnavailable) as caught:
        client.complete_json(system="fixture", user="fixture", schema_hint={})
    diagnostic = str(caught.value) + caplog.text + "".join(traceback.format_exception(caught.value))
    assert canary not in diagnostic and "ConnectionError" in diagnostic
    assert len(calls) == 3


def test_diagnostic_gateway_initialization_failure_is_safe(monkeypatch, canary):
    import sys
    from trading_system.llm.client import KimiGatewayClient
    from trading_system.redline import LLMUnavailable

    def failure(**kwargs):
        raise RuntimeError("Gateway config echo " + canary)

    monkeypatch.setitem(sys.modules, "agent_gw", SimpleNamespace(AgentGwClient=failure))
    with pytest.raises(LLMUnavailable) as caught:
        KimiGatewayClient()._gw()
    assert canary not in "".join(traceback.format_exception(caught.value))


def test_diagnostic_invalid_llm_json_does_not_echo_input(canary):
    from trading_system.llm.client import KimiGatewayClient
    from trading_system.redline import LLMUnavailable
    with pytest.raises(LLMUnavailable) as caught:
        KimiGatewayClient._parse_json('{"echo":' + canary + '}')
    assert canary not in "".join(traceback.format_exception(caught.value))


def test_finite_llm_request_schema_is_validated_before_network(monkeypatch):
    from trading_system.llm.client import OpenAICompatClient
    from trading_system.redline import LLMUnavailable
    calls = []
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: calls.append(1))
    client = OpenAICompatClient("https://example.invalid/v1", "fixture", "fixture", max_retries=0)
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="fixture", user="fixture", schema_hint={"score": float("nan")})
    assert calls == []


def test_diagnostic_search_http_failure_preserves_status(monkeypatch, tmp_path, canary, caplog):
    error = urllib.error.HTTPError("https://example.invalid/" + canary, 403,
                                   "Remote body " + canary, {}, None)
    h = _hub(monkeypatch, tmp_path, [_Source(error=error)], use_disk_cache=False)
    with caplog.at_level(logging.INFO):
        batch = h.search("query")
    text = json.dumps(batch.source_stats) + caplog.text
    assert canary not in text and "403" in text
    assert batch.source_stats["fixture"]["ok"] is False


def test_diagnostic_search_failure_and_disk_failure_are_safe(monkeypatch, tmp_path, canary, caplog):
    from trading_system.search import hub
    bad = _hub(monkeypatch, tmp_path, [_Source(error=ConnectionError(canary))], use_disk_cache=False)
    good = _hub(monkeypatch, tmp_path, [_Source()])

    def fail_replace(*args, **kwargs):
        raise PermissionError(canary)

    monkeypatch.setattr(hub.os, "replace", fail_replace)
    with caplog.at_level(logging.INFO):
        bad_batch = bad.search("bad")
        good_batch = good.search("good", 1)
    assert canary not in json.dumps(bad_batch.source_stats) + caplog.text
    assert good_batch.source_stats["fixture"]["ok"] is True
    assert not list((tmp_path / "search-cache").glob("*.tmp"))


def test_diagnostic_reddit_partial_failure_is_not_healthy(monkeypatch, tmp_path, canary, caplog):
    from trading_system.search import sources

    def response(url, *args, **kwargs):
        if "/good/" in url:
            return {"data": {"children": [{"data": {
                "title": "fixture title", "selftext": "real fixture body",
                "permalink": "/r/good/fixture", "created_utc": 1704067200,
            }}]}}
        raise ConnectionError(canary)

    monkeypatch.setattr(sources, "_http_json", response)
    source = sources.RedditSource(subreddits=("good", "bad"))
    with caplog.at_level(logging.INFO):
        batch = _hub(monkeypatch, tmp_path, [source], use_disk_cache=False).search("query", 2)
    assert len(batch.docs) == 1
    assert batch.source_stats["reddit"]["ok"] is False
    assert batch.source_stats["reddit"]["n"] == 1
    assert canary not in json.dumps(batch.source_stats) + caplog.text


def test_diagnostic_fetch_failure_does_not_echo_url_credentials(monkeypatch, tmp_path, canary, caplog):
    from trading_system.search.sources import FetchSource
    source = FetchSource()

    def failure(url):
        raise ConnectionError(canary)

    monkeypatch.setattr(source, "fetch_doc", failure)
    with caplog.at_level(logging.INFO):
        batch = _hub(monkeypatch, tmp_path, [source], use_disk_cache=False).search(
            "fetch:" + urllib.parse.urlunsplit(("https", f"user:{canary}@example.invalid", "/a", f"token={canary}", "")))
    assert batch.source_stats["kimi_fetch"]["ok"] is False
    assert canary not in json.dumps(batch.source_stats) + caplog.text


@pytest.mark.parametrize("missing", ["key", "endpoint", "both"])
def test_patent_requires_explicit_key_and_endpoint(monkeypatch, tmp_path, missing):
    from trading_system.search import sources
    if missing != "key" and missing != "both":
        monkeypatch.setenv("PATENTSVIEW_API_KEY", "fixture-value")
    if missing != "endpoint" and missing != "both":
        monkeypatch.setenv("PATENTSVIEW_API_URL", PATENT_ENDPOINT)
    batch = _hub(monkeypatch, tmp_path, [sources.PatentsViewSource()], use_disk_cache=False).search("query")
    assert batch.docs == [] and batch.source_stats["patentsview"]["ok"] is False
    assert "config" in batch.source_stats["patentsview"]["err"]


def _patent_payload(rows=None):
    rows = [{"patent_id": "10905426", "patent_title": "Fixture patent title",
             "patent_date": "2021-02-02",
             "assignees": [{"assignee_organization": "Fixture organization"}]}] if rows is None else rows
    return {"error": False, "count": len(rows), "total_hits": len(rows), "patents": rows}


def test_patent_modern_request_and_response_contract(monkeypatch, canary):
    from trading_system.search import sources
    seen = {}
    monkeypatch.setenv("PATENTSVIEW_API_KEY", canary)
    monkeypatch.setenv("PATENTSVIEW_API_URL", PATENT_ENDPOINT)

    def response(url, timeout, headers=None):
        seen.update(url=url, timeout=timeout, headers=headers)
        return _patent_payload()

    monkeypatch.setattr(sources, "_http_json", response)
    docs = sources.PatentsViewSource(timeout=2).search("memory chips", 1)
    parts = urllib.parse.urlsplit(seen["url"])
    params = urllib.parse.parse_qs(parts.query)
    assert parts.scheme + "://" + parts.netloc + parts.path == PATENT_ENDPOINT
    assert seen["headers"]["X-Api-Key"] == canary and seen["timeout"] == 2
    assert json.loads(params["o"][0]) == {"size": 1}
    fields = json.loads(params["f"][0])
    assert "patent_id" in fields and "assignees.assignee_organization" in fields
    assert "patent_number" not in fields
    assert json.loads(params["q"][0]) == {"_text_any": {"patent_title": "memory chips"}}
    assert len(docs) == 1 and "10905426" in docs[0].content
    assert docs[0].published == "2021-02-02" and docs[0].meta["assignee"] == "Fixture organization"


@pytest.mark.parametrize("payload", [
    {"error": True, "patents": []}, {}, {"error": False},
    {"error": False, "patents": None}, {"error": False, "patents": {}},
    {"error": False, "patents": [{}]},
    {"error": False, "patents": [{"patent_id": "fixture"}]},
    "<html>Open Data Portal</html>",
], ids=["remote-error", "empty-object", "missing-results", "null-results", "object-results",
        "empty-record", "missing-fields", "html-response"])
def test_patent_invalid_responses_are_unavailable(monkeypatch, tmp_path, payload):
    from trading_system.search import sources
    monkeypatch.setenv("PATENTSVIEW_API_KEY", "fixture-value")
    monkeypatch.setenv("PATENTSVIEW_API_URL", PATENT_ENDPOINT)
    monkeypatch.setattr(sources, "_http_json", lambda *args, **kwargs: payload)
    batch = _hub(monkeypatch, tmp_path, [sources.PatentsViewSource()], use_disk_cache=False).search("query")
    assert batch.docs == [] and batch.source_stats["patentsview"]["ok"] is False


def test_patent_authenticated_empty_results_are_valid(monkeypatch):
    from trading_system.search import sources
    monkeypatch.setenv("PATENTSVIEW_API_KEY", "fixture-value")
    monkeypatch.setenv("PATENTSVIEW_API_URL", PATENT_ENDPOINT)
    monkeypatch.setattr(sources, "_http_json", lambda *args, **kwargs: _patent_payload([]))
    assert sources.PatentsViewSource().search("query") == []


def test_patent_limit_capped_to_documented_maximum(monkeypatch):
    from trading_system.search import sources
    monkeypatch.setenv("PATENTSVIEW_API_KEY", "fixture-value")
    monkeypatch.setenv("PATENTSVIEW_API_URL", PATENT_ENDPOINT)
    seen = []
    monkeypatch.setattr(sources, "_http_json", lambda url, *args, **kwargs: seen.append(url) or _patent_payload([]))
    sources.PatentsViewSource().search("query", 2000)
    assert json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)["o"][0]) == {"size": 1000}


@pytest.mark.parametrize("text", ['{"value":NaN}', '{"nested":[{"value":1e400}]}', "<html>portal</html>"])
def test_finite_source_http_json_rejects_invalid_payload(monkeypatch, text):
    from trading_system.search import sources

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return text.encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError):
        sources._http_json("https://example.invalid/fixture", 1)


def test_diagnostic_http_status_rejects_untrusted_integer_subclass(canary):
    from trading_system.data_safety import http_status, safe_diagnostic

    class HostileStatus(int):
        def __str__(self):
            return canary

    error = RuntimeError("fixture")
    error.status_code = HostileStatus(403)
    assert http_status(error.status_code) is None
    assert canary not in safe_diagnostic(error).summary


def test_diagnostic_normalized_source_failure_preserves_http_status(monkeypatch, tmp_path):
    from trading_system.search.models import SearchUnavailable
    error = SearchUnavailable("remote-error", status=429)
    batch = _hub(monkeypatch, tmp_path, [_Source(error=error)], use_disk_cache=False).search("query")
    assert batch.source_stats["fixture"]["http_status"] == 429


@pytest.mark.parametrize("where", ["document-fetched", "evidence-fetched", "evidence-published"])
def test_finite_stored_timestamps_require_numeric_values(where):
    from trading_system.search.models import Evidence
    with pytest.raises(ValueError):
        if where == "document-fetched":
            _doc(fetched_at="1704067200")
        elif where == "evidence-fetched":
            Evidence("body", "fixture", fetched_at="1704067200")
        else:
            Evidence("body", "fixture", published_at="1704067200")


@pytest.mark.parametrize("backend", ["api", "gateway"])
@pytest.mark.parametrize("argument,value", [
    ("temperature", "0.2"), ("temperature", True),
    ("max_tokens", float("nan")), ("max_tokens", float("inf")),
    ("max_tokens", True), ("max_tokens", "1200"), ("max_tokens", 0), ("max_tokens", -1),
], ids=["text-temperature", "boolean-temperature", "nan-tokens", "infinite-tokens",
        "boolean-tokens", "text-tokens", "zero-tokens", "negative-tokens"])
def test_finite_llm_request_numbers_are_validated_before_transport(monkeypatch, backend, argument, value):
    from trading_system.llm.client import KimiGatewayClient, OpenAICompatClient
    from trading_system.redline import LLMUnavailable
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})

    if backend == "api":
        client = OpenAICompatClient("https://example.invalid/v1", "fixture", "fixture", max_retries=0)
        monkeypatch.setattr(requests, "post", transport)
    else:
        client = KimiGatewayClient(max_retries=0)
        monkeypatch.setattr(client, "_gw", lambda: SimpleNamespace(chat_completion=lambda **kwargs: (
            calls.append(1) or {"choices": [{"message": {"content": "{}"}}]})))
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="fixture", user="fixture", schema_hint={}, **{argument: value})
    assert calls == []


def test_finite_local_probe_rejects_nested_nonfinite_payload(monkeypatch):
    from trading_system.llm.client import LocalAgentClient
    from trading_system.redline import LLMUnavailable
    monkeypatch.setattr(LocalAgentClient, "_cache", None)
    payload = {"models": [{"name": "fixture-model", "details": {"score": float("nan")}}], "data": []}
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: payload))
    with pytest.raises(LLMUnavailable):
        LocalAgentClient.detect()


@pytest.mark.parametrize("adapter", ["search", "fetch"])
def test_diagnostic_sdk_source_direct_failure_is_normalized(monkeypatch, canary, adapter):
    import sys
    from trading_system.search import sources
    from trading_system.search.models import SearchUnavailable

    def failure(**kwargs):
        raise ConnectionError("SDK echo " + canary)

    monkeypatch.setitem(sys.modules, "agent_gw", SimpleNamespace(AgentGwClient=failure))
    with pytest.raises(SearchUnavailable) as caught:
        if adapter == "search":
            sources.KimiSearchSource().search("fixture query")
        else:
            sources.FetchSource().fetch_doc("https://example.invalid/fixture")
    assert canary not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("adapter", ["search", "fetch"])
def test_finite_sdk_source_payload_is_validated(monkeypatch, adapter):
    from trading_system.search import sources
    from trading_system.search.models import SearchUnavailable
    source = sources.KimiSearchSource() if adapter == "search" else sources.FetchSource()
    payload = ({"search_results": [{"content": "valid body", "score": float("nan")}]} if adapter == "search"
               else {"content": "valid body", "details": [float("inf")]})
    source._client = SimpleNamespace(search=lambda *args, **kwargs: payload,
                                     fetch=lambda *args, **kwargs: payload)
    with pytest.raises(SearchUnavailable):
        source.search("fixture query") if adapter == "search" else source.fetch_doc("https://example.invalid/fixture")


@pytest.mark.parametrize("helper", ["_http_json", "_http_text"])
def test_diagnostic_source_request_construction_is_safe(monkeypatch, canary, helper):
    from trading_system.search import sources
    from trading_system.search.models import SearchUnavailable

    def failure(*args, **kwargs):
        raise ValueError("Request echo " + canary)

    monkeypatch.setattr("urllib.request.Request", failure)
    with pytest.raises(SearchUnavailable) as caught:
        getattr(sources, helper)("https://example.invalid/fixture", 1)
    assert canary not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("suffix", ["?", "#", "?#"])
def test_patent_empty_endpoint_delimiters_do_not_discard_query(monkeypatch, suffix):
    from trading_system.search import sources
    seen = []
    monkeypatch.setattr(sources, "_http_json", lambda url, *args, **kwargs: seen.append(url) or _patent_payload([]))
    sources.PatentsViewSource(api_key="fixture", endpoint=PATENT_ENDPOINT + suffix).search("fixture query", 1)
    parts = urllib.parse.urlsplit(seen[0])
    assert parts.fragment == ""
    assert "q" in urllib.parse.parse_qs(parts.query)
    assert parts.path == "/api/v1/patent/"


@pytest.mark.parametrize("endpoint", [
    "http://example.invalid/api/", "https://user:fixture@example.invalid/api/",
    "https://example.invalid/api/?token=fixture", "https://example.invalid/api/#fixture",
    "https://example.invalid:bad/api/", "https://[broken/api/",
    "https://example.invalid\\@trusted.invalid/api/", "https://example.invalid/\x00/api/",
], ids=["plain-http", "userinfo", "query", "fragment", "invalid-port", "invalid-ipv6",
        "backslash", "control-character"])
def test_patent_invalid_endpoint_fails_before_transport(endpoint):
    from trading_system.search.sources import PatentsViewSource
    from trading_system.search.models import SearchUnavailable
    with pytest.raises(SearchUnavailable, match="endpoint"):
        PatentsViewSource(api_key="fixture", endpoint=endpoint).search("fixture query")


@pytest.mark.parametrize("field,value", [
    ("patent_id", "../fixture"), ("patent_id", 10905426),
    ("patent_date", "2024-02-30"), ("patent_date", ""),
    ("patent_title", None), ("assignees", {}),
    ("assignees", [{"assignee_organization": 123}]), ("assignees", [None]),
], ids=["unsafe-id", "numeric-id", "invalid-date", "missing-date", "missing-title",
        "invalid-assignee-collection", "invalid-assignee-name", "invalid-assignee-record"])
def test_patent_record_validation_after_valid_envelope(monkeypatch, field, value):
    from trading_system.search import sources
    from trading_system.search.models import SearchUnavailable
    payload = _patent_payload()
    payload["patents"][0][field] = value
    monkeypatch.setattr(sources, "_http_json", lambda *args, **kwargs: payload)
    with pytest.raises(SearchUnavailable):
        sources.PatentsViewSource(api_key="fixture", endpoint=PATENT_ENDPOINT).search("query")


@pytest.mark.parametrize("value", [None, True, "1", -1, 0, 2], ids=["missing", "boolean", "text", "negative", "short", "long"])
def test_patent_count_must_match_response_records(monkeypatch, value):
    from trading_system.search import sources
    from trading_system.search.models import SearchUnavailable
    payload = _patent_payload()
    payload["count"] = value
    monkeypatch.setattr(sources, "_http_json", lambda *args, **kwargs: payload)
    with pytest.raises(SearchUnavailable):
        sources.PatentsViewSource(api_key="fixture", endpoint=PATENT_ENDPOINT).search("query")


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_patent_http_failure_is_safe_and_not_cached(monkeypatch, tmp_path, canary, caplog, status):
    from trading_system.search.sources import PatentsViewSource
    error = urllib.error.HTTPError("https://example.invalid/" + canary, status,
                                   "Remote echo " + canary, {}, None)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    source = PatentsViewSource(api_key=canary, endpoint=PATENT_ENDPOINT)
    h = _hub(monkeypatch, tmp_path, [source])
    with caplog.at_level(logging.INFO):
        batch = h.search("query")
    assert batch.docs == [] and batch.source_stats["patentsview"]["ok"] is False
    assert batch.source_stats["patentsview"]["http_status"] == status
    assert canary not in json.dumps(batch.source_stats) + caplog.text
    assert not list((tmp_path / "search-cache").glob("*.json"))


def test_patent_credential_scope_is_isolated_without_persisting_key(monkeypatch, tmp_path, canary):
    from trading_system.search import sources
    first = sources.PatentsViewSource(api_key=canary, endpoint=PATENT_ENDPOINT)
    other_canary = f"SYNTHETIC_OTHER_{canary}"
    second = sources.PatentsViewSource(api_key=other_canary, endpoint=PATENT_ENDPOINT)
    h = _hub(monkeypatch, tmp_path, [first])
    assert h._cache_key("query", 1) != _hub(monkeypatch, tmp_path, [second])._cache_key("query", 1)
    monkeypatch.setattr(sources, "_http_json", lambda *args, **kwargs: _patent_payload())
    assert len(h.search("query", 1).docs) == 1
    assert all(canary not in path.read_text() for path in (tmp_path / "search-cache").glob("*.json"))


@pytest.mark.parametrize("value", [-1, True, "1", float("nan")], ids=["negative", "boolean", "text", "nan"])
def test_patent_invalid_limit_is_rejected_without_transport(value):
    from trading_system.search.sources import PatentsViewSource
    with pytest.raises(ValueError):
        PatentsViewSource(api_key="fixture", endpoint=PATENT_ENDPOINT).search("query", value)


def test_patent_zero_limit_does_not_call_transport():
    from trading_system.search.sources import PatentsViewSource
    assert PatentsViewSource(api_key="fixture", endpoint=PATENT_ENDPOINT).search("query", 0) == []


def test_cache_returns_independent_document_and_health_snapshots(monkeypatch, tmp_path):
    h = _hub(monkeypatch, tmp_path, [_Source()], use_disk_cache=False)
    first = h.search("query", 1)
    first.docs[0].meta["mutation"] = "external"
    first.source_stats["fixture"]["ok"] = False
    replay = h.search("query", 1)
    assert "mutation" not in replay.docs[0].meta and replay.source_stats["fixture"]["ok"] is True
    replay.source_stats["fixture"]["ok"] = False
    assert h.search("query", 1).source_stats["fixture"]["ok"] is True


@pytest.mark.parametrize("fault", ["missing-health", "wrong-count", "wrong-source", "nonfinite-ms", "boolean-count"],
                         ids=["missing-health", "wrong-count", "wrong-source", "nonfinite-ms", "boolean-count"])
def test_cache_poisoned_health_envelope_is_a_miss(monkeypatch, tmp_path, fault):
    h = _hub(monkeypatch, tmp_path, [_Source()])
    h.search("query", 1)
    h._mem.clear()
    path = next((tmp_path / "search-cache").glob("*.json"))
    payload = json.loads(path.read_text())
    if fault == "missing-health":
        payload["source_stats"] = {}
    elif fault == "wrong-count":
        payload["source_stats"]["fixture"]["n"] = 99
    elif fault == "wrong-source":
        payload["source_stats"] = {"foreign": {"ok": True, "n": 1}}
    elif fault == "nonfinite-ms":
        payload["source_stats"]["fixture"]["ms"] = float("nan")
    else:
        payload["source_stats"]["fixture"]["n"] = True
    path.write_text(json.dumps(payload))
    assert h._cache_get(h._cache_key("query", 1)) is None
