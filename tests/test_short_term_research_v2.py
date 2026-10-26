"""Business-facing research v2 regressions; every input is SYNTHETIC_TEST.

No provider, model, broker, account or website call is allowed by these tests.
The tests intentionally precede the implementation. Contracts cover normal,
boundary, error, authority and legacy cases rather than only JSON shape.
"""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from trading_system.agent_evidence import normalize
from trading_system.data_models import StockCandidate, TradePick
from trading_system.llm import required
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from trading_system.search.models import CleanDocument, RawDocument
from trading_system.tech_chain.agents import RiskAlert, SentimentRow
from trading_system.tech_chain.fusion import TechChainFusionAgent


ASSESSMENT = {"run_date": "2026-10-27", "market_timezone": "America/New_York",
              "admitted_at_utc": "2026-10-27T14:00:00+00:00"}


def research_module():
    return importlib.import_module("trading_system.llm.candidate_research")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def public_document(identity="ds_test_primary", *, published="2026-10-26T12:00:00+00:00",
                    content="SYNTHETIC_TEST AAA reported orders increased; this is a fixture, not actual news."):
    return RawDocument(identity, "deepseek_search", "SYNTHETIC_TEST AAA company release",
        "https://issuer.example/aaa/release", content, published, 1793102400.0,
        {"target_symbol": "AAA"})


def candidate(symbol="AAA"):
    return StockCandidate(ticker=symbol, sector_etf="XLK", chain_id="semis",
        price=100, key_level=101, stop_price=95, tss_final=8, entry_template="A",
        evidence=["SYNTHETIC_TEST 已观察到结构回踩；不是当前行情。"])


def approved(symbol="AAA"):
    return TradePick(symbol, 8, 512, "A", 100, 95, 100, .1, 500,
                     chain="semis", sector="XLK", time_stop_days=7)


def context(*, picks=(), watch=(), holdings=()):
    return {"picks": [approved(x) for x in picks],
            "watchlist": [candidate(x) for x in watch],
            "research_holdings": [{"symbol": x, "shares": 10,
                "known_at": ASSESSMENT["admitted_at_utc"]} for x in holdings],
            "trade_date": ASSESSMENT["run_date"], "action": "BUY" if picks else "WAIT",
            "mrs": {"mrs_star": 8, "evidence": ["SYNTHETIC_TEST"]},
            "sectors": [], "chains": [], "tech_signals": [], "narrative_llm": {},
            "semantic_evidence": {}, "rejections": [], "entry_budget": {}}


def market():
    return {"id": "md_AAA_20261026_" + "1" * 24, "symbol": "AAA",
            "as_of": "2026-10-26", "close": 100.0, "series_sha256": "1" * 64}


def facts():
    result = []
    for scope in ("market", "sector", "chain", "stock", "plan"):
        entry = {"scope": scope, "symbol": "AAA", "source_stage": "SYNTHETIC_TEST",
                 "known_at": ASSESSMENT["admitted_at_utc"], "facts": {"fixture": scope}}
        result.append({"id": "fx_" + canonical_hash(entry)[:24], **entry})
    return result


def thesis(*, strategy="technical_flow_v2", status="sufficient"):
    ids = {x["scope"]: x["id"] for x in facts()}
    return {"schema_version": 2, "symbol": "AAA", "strategy": strategy, "status": status,
        "issuer_name": None, "issuer_identity_evidence_ids": [],
        "five_part": {scope: {"text": "SYNTHETIC_TEST 具体已知事实、交易含义和未知边界。",
            "evidence_ids": [market()["id"] if scope == "stock" else ids[scope]]}
            for scope in ("market", "sector", "chain", "stock", "plan")},
        "claims": [],
        "event_risk": {"status": "unknown", "text": "SYNTHETIC_TEST 已检索但事件日期未核实，不能视为无事件。",
                       "source_ids": []},
        "entry_triggers": ["SYNTHETIC_TEST 全部既有数值闸门和结构条件复核后再入场。"],
        "falsification": ["SYNTHETIC_TEST 跌破原结构止损位则撤销。"],
        "risk_notes": ["SYNTHETIC_TEST 未知公司事件仍可能导致跳空。"],
        "gaps": [] if status == "sufficient" else ["SYNTHETIC_TEST 缺少可核验的当前证据。"],
        "followup": {"needed": False, "question": None, "fetch_source_id": None}}


def validate(value, documents=()):
    return research_module().validate_thesis(value, symbol="AAA", documents=list(documents),
        market_snapshot=market(), fact_evidence=facts(), assessment=ASSESSMENT)


def event_thesis(document):
    item = thesis(strategy="news_event_v2")
    item["issuer_name"] = "SYNTHETIC_TEST AAA"
    item["issuer_identity_evidence_ids"] = [document.doc_id]
    item["claims"] = [{"claim_id": "claim-1", "statement": "SYNTHETIC_TEST AAA订单增加。",
        "source_id": document.doc_id, "quote": "AAA reported orders increased",
        "origin_id": document.doc_id, "evidence_kind": "primary", "time_role": "current"}]
    item["five_part"]["stock"]["evidence_ids"].append(document.doc_id)
    return item


def test_issuer_plan_is_dated_bounded_deduplicated_and_does_not_rewrite_risk():
    original = context(picks=["AAA"], watch=["AAA", "BBB"], holdings=["CCC", "AAA"])
    before = normalize(original)
    plan = research_module().build_research_plan(original, ASSESSMENT)
    assert [x["symbol"] for x in plan["targets"]] == ["AAA", "BBB", "CCC"]
    assert set(plan["targets"][0]["roles"]) == {"approved", "watch", "holding"}
    for row in plan["targets"]:
        query = row["initial_query"]
        assert '"' + row["symbol"] + '"' in query
        assert "after:" in query and "before:2026-10-28" in query and len(query) <= 128
    assert plan["assessment"] == ASSESSMENT and not plan["deferred"]
    assert normalize(original) == before


def test_ten_candidates_do_not_evict_ten_distinct_current_holdings():
    plan = research_module().build_research_plan(context(
        picks=["A" + chr(65 + i) for i in range(10)],
        watch=["WATCH"], holdings=["H" + chr(65 + i) for i in range(10)]), ASSESSMENT)
    assert len(plan["targets"]) == 20
    assert sum("holding" in x["roles"] for x in plan["targets"]) == 10
    assert plan["deferred"] == [{"symbol": "WATCH", "roles": ["watch"],
                                 "reason": "candidate_limit"}]


def test_more_than_ten_holdings_or_missing_snapshot_fails_before_any_research():
    m = research_module()
    with pytest.raises(RequiredModelFailed):
        m.build_research_plan(context(holdings=["H" + chr(65 + i) for i in range(11)]), ASSESSMENT)
    missing = context(watch=["AAA"])
    del missing["research_holdings"]
    with pytest.raises(RequiredModelFailed):
        m.build_research_plan(missing, ASSESSMENT)


@pytest.mark.parametrize("symbol", ["aaa", "AAA;rm", "AAA\nBBB", "", "<AAA>"])
def test_invalid_issuer_identity_is_rejected_without_query_repair(symbol):
    with pytest.raises(RequiredModelFailed):
        research_module().build_research_plan(context(watch=[symbol]), ASSESSMENT)


def test_technical_flow_does_not_need_company_good_news_but_still_needs_numeric_approval():
    m = research_module()
    value = thesis()
    validate(value)
    assert m.evidence_allows_buy(value, numeric_approved=True) is True
    assert m.evidence_allows_buy(value, numeric_approved=False) is False
    value["status"], value["gaps"] = "insufficient", ["SYNTHETIC_TEST 尚缺必要结构证据。"]
    validate(value)
    assert m.evidence_allows_buy(value, numeric_approved=True) is False


@pytest.mark.parametrize("published", ["", "2026-09-01T12:00:00+00:00", "2026-10-27T14:00:01+00:00"])
def test_news_event_rejects_unknown_old_and_same_day_future_catalyst(published):
    document = public_document(published=published)
    with pytest.raises(RequiredModelFailed):
        validate(event_thesis(document), [document])


def test_news_event_accepts_a_bound_current_original_disclosure():
    document = public_document()
    value = event_thesis(document)
    validate(value, [document])
    assert research_module().evidence_allows_buy(value, numeric_approved=True)


def test_quote_cannot_be_borrowed_from_another_document():
    original = public_document(content="SYNTHETIC_TEST AAA company identifier only, no orders statement.")
    neighboring = public_document("ds_neighbor", content="SYNTHETIC_TEST AAA reported orders increased in another report.")
    with pytest.raises(RequiredModelFailed):
        validate(event_thesis(original), [original, neighboring])


@pytest.mark.parametrize("change", ["source", "origin", "symbol", "market", "fact", "five_part", "invalidation"])
def test_thesis_cannot_invent_identity_market_fact_or_omit_business_reason(change):
    document = public_document()
    value = event_thesis(document)
    snapshot = market()
    if change == "source": value["claims"][0]["source_id"] = "ds_invented"
    elif change == "origin": value["claims"][0]["origin_id"] = "ds_invented_origin"
    elif change == "symbol": value["symbol"] = "BBB"
    elif change == "market": value["five_part"]["stock"]["evidence_ids"] = ["md_BBB_20261026_" + "1" * 24]
    elif change == "fact": value["five_part"]["market"]["evidence_ids"] = ["fx_" + "0" * 24]
    elif change == "five_part": del value["five_part"]["chain"]
    else: value["falsification"] = []
    with pytest.raises(RequiredModelFailed):
        validate(value, [document])


def test_unknown_event_is_never_silently_changed_to_no_event():
    value = thesis()
    before = copy.deepcopy(value)
    validate(value)
    assert value == before and value["event_risk"]["status"] == "unknown"


class SemanticStub:
    """Only returns an explicit synthetic answer; no HTTP implementation."""
    def __init__(self, answer):
        self.answer = answer
        self.execution = SimpleNamespace(run_date=date(2026, 10, 27),
            policy=SimpleNamespace(market_timezone="America/New_York"))
        self.requests = []

    def complete_json(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.answer)


def test_sentiment_evidence_ids_and_unabridged_drivers_survive_v2_projection():
    from trading_system.tech_chain.universe import TECH_SUBCHAINS
    ids = [public_document().doc_id]
    answer = {"chains": [{"chain_id": cid, "heat": 4, "sentiment_score": 0,
        "narrative_change": "stable", "key_drivers": ["SYNTHETIC_TEST 转载不是独立来源，旧资料不是当前催化。"],
        "evidence_ids": ids} for cid in TECH_SUBCHAINS]}
    result = required.sentiment_v2(SemanticStub(answer), [CleanDocument(public_document())])
    assert result["memory"].evidence_ids == ids
    assert result["memory"].key_drivers == answer["chains"][0]["key_drivers"]


def test_risk_coverage_and_caveats_survive_even_when_no_alert_exists():
    answer = {"assessment": "SYNTHETIC_TEST 六链已评估；日期未知、同源转载和未具名消息不能算独立当前事实。", "alerts": []}
    result = required.risk_v2(SemanticStub(answer), [CleanDocument(public_document())])
    assert result.assessment == answer["assessment"] and result.alerts == []


def test_fusion_preserves_semantic_provenance_without_changing_numeric_weights():
    agents = importlib.import_module("trading_system.tech_chain.agents")
    row = agents.SentimentEvidenceRow(chain_id="memory", heat=8, sentiment_score=.5,
        narrative_change="improving", key_drivers=["SYNTHETIC_TEST 原始来源有限。"],
        evidence_ids=["ds_test_primary"], degraded=False)
    alert = RiskAlert(7, "memory", "upstream", "capacity", "SYNTHETIC_TEST 风险",
        transmission=["SYNTHETIC_TEST 供应影响订单"], evidence_ids=["ds_test_primary"])
    risk = agents.RiskAssessment("SYNTHETIC_TEST 未具名消息仍需核验。", [alert], ["ds_test_primary"])
    result = TechChainFusionAgent().execute([], [], {"memory": row}, risk)
    new = next(x for x in result if x.chain_id == "memory")
    old = next(x for x in TechChainFusionAgent().execute([], [], {"memory": SentimentRow(
        chain_id="memory", heat=8, sentiment_score=.5, narrative_change="improving",
        key_drivers=row.key_drivers, degraded=False)}, [alert]) if x.chain_id == "memory")
    assert new.prosperity == old.prosperity and new.bonus_hint == old.bonus_hint
    projected = new.to_dict()["semantic_evidence"]
    assert projected["sentiment"]["evidence_ids"] == ["ds_test_primary"]
    assert projected["sentiment"]["key_drivers"] == row.key_drivers
    assert projected["risk"]["assessment"] == risk.assessment
    assert projected["risk"]["alerts"][0]["evidence_ids"] == ["ds_test_primary"]


def test_legacy_dataclass_normalization_has_no_new_default_fields():
    original = normalize(SentimentRow(chain_id="memory"))
    assert set(original["fields"]) == {"chain_id", "heat", "sentiment_score",
        "narrative_change", "key_drivers", "degraded"}
    original_doc = normalize(CleanDocument(public_document()))
    assert set(original_doc["fields"]) == {"raw", "tickers", "sentiment", "sentiment_score",
        "events", "summary_zh", "relevance", "degraded", "evidence", "corroborated"}


class FailingNativeClient:
    """No HTTP; fail one declared native action and record any attempted retry."""
    def __init__(self, failure):
        self.failure = failure
        self.calls = []
        self.search_receipts = []
        self.fetch_receipts = []
        self.receipts = []

    def search(self, query, **kwargs):
        self.calls.append(("search", query))
        if self.failure == "search":
            raise LLMUnavailable("SYNTHETIC_TEST supplemental search failed")
        raise AssertionError("SYNTHETIC_TEST unexpected native action")

    def fetch(self, url):
        self.calls.append(("fetch", url))
        if self.failure == "fetch":
            raise LLMUnavailable("SYNTHETIC_TEST fetch failed")
        raise AssertionError("SYNTHETIC_TEST unexpected native action")


@pytest.mark.parametrize("failure", ["search", "fetch"])
def test_supplement_or_fetch_failure_is_not_converted_to_completed_watch(failure):
    m = research_module()
    plan = m.build_research_plan(context(watch=["AAA"]), ASSESSMENT)
    doc = public_document()
    preliminary = {"theses": [{"symbol": "AAA", "followup": {
        "needed": True,
        "question": "SYNTHETIC_TEST 核实原始披露日期" if failure == "search" else None,
        "fetch_source_id": doc.doc_id if failure == "fetch" else None}}]}
    client = FailingNativeClient(failure)
    with pytest.raises(RequiredModelFailed):
        m.execute_research_followups(client, plan, preliminary, [doc])
    assert len(client.calls) == 1 and client.calls[0][0] == failure
    assert not client.receipts


def test_fetch_cannot_follow_a_url_or_identity_invented_by_model():
    m = research_module()
    plan = m.build_research_plan(context(watch=["AAA"]), ASSESSMENT)
    client = FailingNativeClient("fetch")
    preliminary = {"theses": [{"symbol": "AAA", "followup": {"needed": True,
        "question": None, "fetch_source_id": "ds_invented"}}]}
    with pytest.raises(RequiredModelFailed):
        m.execute_research_followups(client, plan, preliminary, [public_document()])
    assert client.calls == []


def test_duplicate_request_identity_fails_before_second_native_http(monkeypatch):
    """Root-owned transport regression; this test performs only mocked I/O."""
    import trading_system.llm.deepseek_required as transport
    from test_deepseek_required import RecordingExecution
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "SYNTHETIC_TEST-key"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(transport.uuid, "uuid4", lambda: SimpleNamespace(hex="1" * 32))
    calls = []
    def mocked_post(client, path, body):
        calls.append(path)
        result = {"results": [{"url": "https://issuer.example/release",
            "title": "SYNTHETIC_TEST source", "content": "SYNTHETIC_TEST public evidence content",
            "date": "2026-10-26T12:00:00Z"}]}
        return result, canonical_hash(result)
    monkeypatch.setattr(transport.DeepSeekRequiredClient, "_post_json", mocked_post)
    client = transport.DeepSeekRequiredClient(RecordingExecution("research.candidates"))
    client.search('"AAA" stock after:2026-09-27 before:2026-10-28')
    with pytest.raises(LLMUnavailable):
        client.search('"BBB" stock after:2026-09-27 before:2026-10-28')
    assert calls == ["/v1/tools/search"]
