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
        {"target_symbols": ["AAA"]})


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


def test_current_paper_holding_ticker_is_accepted_but_conflicting_symbol_is_not():
    m = research_module()
    ctx = context(holdings=["AAA"])
    ctx["research_holdings"][0] = {"ticker": "AAA", "shares": 10, "sector": "XLK", "chain": "semis"}
    plan = m.build_research_plan(ctx, ASSESSMENT)
    assert plan["targets"][0]["symbol"] == "AAA"
    assert next(row for row in m.build_fact_evidence(ctx, plan) if row["scope"] == "sector")["facts"]["sector_etf"] == "XLK"
    ctx["research_holdings"][0]["symbol"] = "BBB"
    with pytest.raises(RequiredModelFailed):
        m.build_research_plan(ctx, ASSESSMENT)


def test_smaller_research_budget_cannot_silently_defer_numeric_approved_pick():
    m = research_module()
    with pytest.raises(RequiredModelFailed):
        m.build_research_plan(context(picks=["AAA", "BBB"]), ASSESSMENT,
                              budget=m.ResearchBudget(candidate_symbols=1))


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


def test_company_identity_cannot_borrow_another_issuers_scoped_source():
    original = public_document()
    foreign = RawDocument(original.doc_id, original.source, original.title, original.url,
        original.content, original.published, original.fetched_at, {"target_symbols": ["BBB"]})
    with pytest.raises(RequiredModelFailed):
        validate(event_thesis(foreign), [foreign])


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
            policy=SimpleNamespace(market_timezone="America/New_York"),
            admitted_at_utc=ASSESSMENT["admitted_at_utc"])
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
    answer = {"assessment": "SYNTHETIC_TEST 六链已评估；日期未知、同源转载和未具名消息不能算独立当前事实。",
              "evidence_ids": [public_document().doc_id], "alerts": []}
    result = required.risk_v2(SemanticStub(answer), [CleanDocument(public_document())])
    assert result.assessment == answer["assessment"] and result.alerts == []
    assert result.evidence_ids == answer["evidence_ids"]


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


def test_exact_quote_beyond_actually_presented_body_is_rejected():
    m = research_module()
    doc = public_document(content="SYNTHETIC_TEST prefix. " + "x" * 100 + " AAA reported orders increased")
    with pytest.raises(RequiredModelFailed):
        m.validate_thesis(event_thesis(doc), symbol="AAA", documents=[doc],
            market_snapshot=market(), fact_evidence=facts(), assessment=ASSESSMENT,
            presented_documents=[m.source_view(doc, characters=30)])


def test_v2_source_count_does_not_treat_syndicated_hosts_as_independent_origins():
    from trading_system.search.models import CleanDocumentV2
    from trading_system.search.credibility import CrossValidator, scoring_docs
    def observed(identity, host, origin):
        raw = RawDocument(identity, "deepseek_search", "SYNTHETIC_TEST", f"https://{host}/a",
            "SYNTHETIC_TEST AAA reported orders increased", "2026-10-26T12:00:00Z")
        return CleanDocumentV2(raw, provenance={"origin_id": origin, "evidence_kind": "media"},
            claims=[{"claim_key": "AAA orders 20261026", "quote": "AAA reported orders increased"}])
    docs = [observed("original", "first.example", "original"),
            observed("syndicated", "different.example", "original")]
    stats = CrossValidator(contract_version=2).validate(docs)
    assert stats["corroborated"] == 0 and stats["corroborated_claim_groups"] == 0
    assert scoring_docs(docs, contract_version=2) == docs
    docs.append(observed("independent", "third.example", "independent"))
    stats = CrossValidator(contract_version=2).validate(docs)
    assert stats["corroborated"] == 3 and stats["corroborated_claim_groups"] == 1
    docs[2].provenance["origin_id"] = None
    assert CrossValidator(contract_version=2).validate(docs)["corroborated"] == 0


def test_v2_unknown_heat_is_not_defaulted_or_formatted_as_a_number():
    from trading_system.tech_chain.agents import SentimentEvidenceRow, RiskAssessment
    row = SentimentEvidenceRow("memory", heat=None, sentiment_score=.5, degraded=False,
        key_drivers=["SYNTHETIC_TEST 热度未知"], evidence_ids=[])
    result = TechChainFusionAgent().execute([], [], {"memory": row},
        RiskAssessment("SYNTHETIC_TEST 风险覆盖有限", [], []))
    memory = next(row for row in result if row.chain_id == "memory")
    assert "tech.sentiment" in memory.degraded_components
    assert memory.to_dict()["semantic_evidence"]["sentiment"]["heat"] is None


def test_v2_sector_preserves_full_caveat_and_unknown_score_without_changing_v1():
    import pandas as pd
    from trading_system.agents.sector_agent import SectorAgent, SectorEvidenceScore
    index = pd.date_range("2026-01-01", periods=130)
    close = pd.Series([100 + n for n in range(130)], index=index, dtype=float)
    frame = pd.DataFrame({"Close": close})
    caveat = "SYNTHETIC_TEST " + "共同原始来源有限，旧资料不能作为当前公司指引。" * 12
    narrative = {"XLK": {"score": None, "bias": "unknown", "tone": "unknown",
                          "evidence": caveat, "evidence_ids": ["original"]}}
    result = SectorAgent(None)._score_sector("XLK", frame, close, None, {"AAA": close},
        {"research_contract_version": 2, "narrative_llm": narrative})
    assert isinstance(result, SectorEvidenceScore) and result.factors["narr"] is None
    assert result.semantic_evidence["evidence"] == caveat
    assert caveat in " ".join(result.evidence)
    legacy = SectorAgent(None)._score_sector("XLK", frame, close, None, {"AAA": close},
        {"narrative_llm": {"XLK": {**narrative["XLK"], "score": 5}}})
    assert not isinstance(legacy, SectorEvidenceScore)


class SuccessfulResearchClient:
    """Frozen synthetic model answers, generated from the presented test facts."""
    def __init__(self, *, fetch=False):
        self.execution = SemanticStub({}).execution
        self.calls, self.payloads = [], []
        self.want_fetch = fetch

    def document(self, symbol, source="deepseek_search", *, url=None):
        m = research_module()
        identity = {"url": url or f"https://issuer.example/{symbol}",
            "title": f"SYNTHETIC_TEST {symbol} release", "content": f"SYNTHETIC_TEST {symbol} public evidence",
            "published": None if source == "deepseek_fetch" else "2026-10-26T12:00:00Z"}
        return {"id": "ds_" + m.digest(identity)[:24], **identity, "source": source,
            "fetched_at": ASSESSMENT["admitted_at_utc"],
            "content_hash": hashlib.sha256(identity["content"].encode()).hexdigest()}

    def search(self, query, **kwargs):
        symbol = query.split('"')[1]
        self.calls.append(("search", symbol))
        return [self.document(symbol)]

    def fetch(self, url):
        symbol = url.rsplit("/", 1)[1]
        self.calls.append(("fetch", symbol))
        return self.document(symbol, "deepseek_fetch", url=url)

    def complete_json(self, **kwargs):
        payload = json.loads(kwargs["user"].split("\n", 1)[1])
        self.payloads.append(payload)
        self.calls.append(("chat", payload["phase"]))
        result = []
        for target in payload["targets"]:
            value = thesis()
            symbol = target["symbol"]
            value["symbol"] = symbol
            md = next(row for row in payload["market_evidence"] if row["symbol"] == symbol)
            local = {row["scope"]: row["id"] for row in payload["fact_evidence"] if row["symbol"] == symbol}
            for scope in value["five_part"]:
                value["five_part"][scope]["evidence_ids"] = [md["id"] if scope == "stock" else local[scope]]
            if self.want_fetch and payload["phase"] == "initial":
                own = next(row for row in payload["documents"] if symbol in row["target_symbols"])
                value["followup"] = {"needed": True, "question": None, "fetch_source_id": own["id"]}
            result.append(value)
        return {"coverage_summary": "SYNTHETIC_TEST 所有目标已逐项研究，技术机会不伪造公司新闻。", "theses": result}


def test_actual_research_orchestration_is_complete_deterministic_and_risk_preserving():
    import pandas as pd
    m = research_module()
    ctx = context(picks=["AAA"], watch=["BBB", "CCC"], holdings=["DDD"])
    frame = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))
    ctx["market_data"] = {"stock_ohlcv": {symbol: frame for symbol in ("AAA", "BBB", "CCC", "DDD")}}
    before = normalize(ctx)
    client = SuccessfulResearchClient(fetch=True)
    result = m.research_candidates(client, ctx, [])
    assert result["coverage"]["researched_symbols"] == ["AAA", "BBB", "CCC", "DDD"]
    assert result["counters"] == {"search": 4, "fetch": 4, "chat": 4}
    assert [row["phase"] for row in result["model_rounds"]] == ["initial", "initial", "final", "final"]
    assert normalize(ctx) == before
    for raw in result["documents"]:
        if raw.source == "deepseek_fetch":
            assert raw.published == "" and raw.meta["search_source_id"]
    assert len(result["source_index"]) == 8
    assert all("content" not in row and row["presentations"] for row in result["source_index"])
    replay = m.research_candidates(SuccessfulResearchClient(fetch=True), ctx, [])
    assert normalize(result) == normalize(replay)


@pytest.mark.parametrize("question", ['site:other.example', 'before:2030-01-01', '"BBB" company', 'AAA\nBBB'])
def test_supplement_question_cannot_override_symbol_or_date_query_scope(question):
    m = research_module()
    client = FailingNativeClient("search")
    preliminary = {"theses": [{"symbol": "AAA", "followup": {
        "needed": True, "question": question, "fetch_source_id": None}}]}
    with pytest.raises(RequiredModelFailed):
        m.execute_research_followups(client, m.build_research_plan(context(watch=["AAA"]), ASSESSMENT),
            preliminary, [public_document()])
    assert client.calls == []


def test_model_budget_context_does_not_repeat_account_history_or_hide_its_identity():
    m = research_module()
    ctx = context(watch=["AAA"])
    ctx["entry_budget"] = {"schema": "tiger.entry-budget.v2", "known_at": "2026-10-26",
        "snapshot_hash": "a" * 64, "gross_cap": .5, "risk_pct": .008,
        "source_snapshot": {"nav": 120000, "cash": 80000, "state_hash": "a" * 64},
        "source_account_state": {"closed": [{"private_old_operation": "SYNTHETIC_TEST"}] * 100,
                                 "ops": [{"history": "SYNTHETIC_TEST"}] * 100}}
    snapshot = copy.deepcopy(ctx)
    result = m.build_fact_evidence(ctx, m.build_research_plan(ctx, ASSESSMENT))
    budget = next(row for row in result if row["scope"] == "plan")["facts"]["entry_budget"]
    assert budget["current_portfolio"]["nav"] == 120000
    assert budget["limits"]["gross_cap"] == .5 and budget["source_account_state_sha256"]
    assert "private_old_operation" not in m.canonical(result) and '"closed"' not in m.canonical(result)
    assert ctx == snapshot and "not model input" in budget["input_scope"]


def summary_client():
    from trading_system.llm.request_budget import BudgetGuard
    from trading_system.redline.execution import UPGRADE_MODEL_STAGES
    client = SimpleNamespace(receipts=[], search_receipts=[], fetch_receipts=[], budget=BudgetGuard())
    for number, stage in enumerate(UPGRADE_MODEL_STAGES, 1):
        identity = f"{number:032x}"
        request = {"messages": [{"role": "user", "content": "SYNTHETIC_TEST"}], "max_tokens": 16}
        reservation = client.budget.reserve("chat", identity, request)
        usage = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
        client.budget.settle(identity, usage)
        client.receipts.append({"stage": stage, "call_id": identity,
            "request_sha256": reservation["request_sha256"], "response_id": "SYNTHETIC_" + identity,
            "provider": "deepseek", "model": "deepseek-flash", "release": "DeepSeek-V4.1-Flash-1017",
            "finish_reason": "stop", "usage": usage})
    for number in range(14):
        identity = f"{100 + number:032x}"
        reservation = client.budget.reserve("search", identity, {"query": f"SYNTHETIC_TEST {number}"})
        client.budget.settle(identity, None)
        client.search_receipts.append({"call_id": identity, "request_sha256": reservation["request_sha256"]})
    return client


def test_v2_summary_requires_six_stages_and_exact_fully_settled_budget():
    client = summary_client()
    summary = required.model_summary_v2(client)
    assert summary["call_count"] == 6 and summary["search_count"] == 14 and summary["fetch_count"] == 0
    assert summary["budget"]["actual_tokens"] == 36 and summary["all_required_completed"] is True
    client.budget.reserve("fetch", "f" * 32, {"url": "https://issuer.example/synthetic"})
    with pytest.raises(RequiredModelFailed):
        required.model_summary_v2(client)


@pytest.mark.parametrize("change", ["missing_stage", "duplicate_response", "request_hash", "frozen", "extra_paid_io"])
def test_v2_summary_rejects_success_labels_without_complete_identity_and_budget(change):
    client = summary_client()
    if change == "missing_stage": client.receipts[0]["stage"] = "other"
    elif change == "duplicate_response": client.receipts[0]["response_id"] = client.receipts[1]["response_id"]
    elif change == "request_hash": client.search_receipts[0]["request_sha256"] = "0" * 64
    elif change == "frozen": client.budget._frozen = True
    else:
        client.budget.reserve("fetch", "f" * 32, {"url": "https://issuer.example/synthetic"})
        client.budget.settle("f" * 32, None)
    with pytest.raises(RequiredModelFailed):
        required.model_summary_v2(client)


def test_saturated_research_payload_preserves_each_issuer_and_prioritizes_requested_fetch():
    m = research_module()
    plan = m.build_research_plan(context(watch=["AAA", "BBB", "CCC"]), ASSESSMENT)
    docs = [RawDocument(f"SYNTHETIC_{symbol}_{number}", "deepseek_search", "SYNTHETIC_TEST long evidence",
        f"https://issuer.example/{symbol}/{number}", symbol + "x" * 23997,
        "2026-10-26T12:00:00Z", 1793109600., {"target_symbols": [symbol]})
        for symbol in ("AAA", "BBB", "CCC") for number in range(3)]
    fetched = RawDocument("SYNTHETIC_FETCH_CCC", "deepseek_fetch", "SYNTHETIC_TEST requested original",
        "https://issuer.example/CCC/original", "CCC" + "y" * 19997, "", 1793109600.,
        {"target_symbols": ["CCC"], "search_source_id": "SYNTHETIC_CCC_0"})
    initial = m.build_research_payload(plan, ["AAA", "BBB", "CCC"], docs, [], [], phase="initial")
    for symbol in ("AAA", "BBB", "CCC"):
        assert sum(row["presented_content_characters"] for row in initial["documents"]
                   if symbol in row["target_symbols"]) > 0
    final = m.build_research_payload(plan, ["AAA", "BBB", "CCC"], [*docs, fetched], [], [],
        phase="final", priority_source_ids=[fetched.doc_id])
    page = next(row for row in final["documents"] if row["id"] == fetched.doc_id)
    assert page["presented_content_characters"] == 20000 and page["content_complete"] is True
    assert sum(row["presented_content_characters"] for row in final["documents"]) <= 60000
    for symbol in ("AAA", "BBB", "CCC"):
        assert sum(row["presented_content_characters"] for row in final["documents"]
                   if symbol in row["target_symbols"]) > 0


def test_actual_saturated_followup_round_delivers_requested_fetch_for_all_three_issuers():
    import pandas as pd
    m = research_module()
    class SaturatedClient(SuccessfulResearchClient):
        def document(self, symbol, source="deepseek_search", *, url=None):
            identity = {"url": url or f"https://issuer.example/{symbol}/original",
                "title": f"SYNTHETIC_TEST {symbol} long release",
                "content": symbol + ("y" * 19997 if source == "deepseek_fetch" else "x" * 23997),
                "published": None if source == "deepseek_fetch" else "2026-10-26T12:00:00Z"}
            return {"id": "ds_" + m.digest(identity)[:24], **identity, "source": source,
                "fetched_at": ASSESSMENT["admitted_at_utc"],
                "content_hash": hashlib.sha256(identity["content"].encode()).hexdigest()}

        def search(self, query, **kwargs):
            symbol = query.split('"')[1]
            self.calls.append(("search", symbol))
            return [self.document(symbol, url=f"https://issuer.example/{symbol}/{index}") for index in range(3)]

        def fetch(self, url):
            symbol = url.split("/")[-2]
            self.calls.append(("fetch", symbol))
            return self.document(symbol, "deepseek_fetch", url=url)

        def complete_json(self, **kwargs):
            out = super().complete_json(**kwargs)
            payload = self.payloads[-1]
            for value in out["theses"]:
                if payload["phase"] == "initial":
                    own = next(row for row in payload["documents"] if value["symbol"] in row["target_symbols"])
                    value["claims"] = [{"claim_id": "observed-initial", "statement": "SYNTHETIC_TEST 已读事实及反证不得因补证被遗忘。",
                        "source_id": own["id"], "quote": own["content"][:20], "origin_id": None,
                        "evidence_kind": "media", "time_role": "background"}]
                else:
                    initial = next(row for row in payload["preliminary"] if row["symbol"] == value["symbol"])
                    value["claims"] = copy.deepcopy(initial["claims"])
            return out

    ctx = context(watch=["AAA", "BBB", "CCC"])
    frame = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))
    ctx["market_data"] = {"stock_ohlcv": {symbol: frame for symbol in ("AAA", "BBB", "CCC")}}
    client = SaturatedClient(fetch=True)
    result = m.research_candidates(client, ctx, [])
    final = client.payloads[-1]
    assert final["phase"] == "final" and result["counters"] == {"search": 3, "fetch": 3, "chat": 2}
    assert sum(row["presented_content_characters"] for row in final["documents"]) == 60000
    pages = [row for row in final["documents"] if row["source"] == "deepseek_fetch"]
    assert len(pages) == 3 and all(row["presented_content_characters"] == 20000 and row["content_complete"] for row in pages)
    for symbol in ("AAA", "BBB", "CCC"):
        views = m.presented_views_for_symbol(result, symbol)
        assert sum(row["presented_content_characters"] for row in views) == 120000
        original_id = next(row for row in result["theses"] if row["symbol"] == symbol)["claims"][0]["source_id"]
        original = next(row for row in views if row["id"] == original_id)
        assert original["presented_content_characters"] == 20000 and original["content_complete"] is False


def partial_news_context():
    import pandas as pd
    m = research_module()
    ctx = context(picks=["AAA"])
    ctx["market_data"] = {"stock_ohlcv": {"AAA": pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-26"]))}}
    plan = m.build_research_plan(ctx, ASSESSMENT)
    fact_rows = m.build_fact_evidence(ctx, plan)
    md = m.market_snapshots(ctx["market_data"], ["AAA"], {key: ASSESSMENT[key] for key in ("run_date", "market_timezone")})[0]
    doc = public_document(content="SYNTHETIC_TEST AAA reported orders increased " + "x" * 19950)
    initial = event_thesis(doc)
    initial["strategy"] = "technical_flow_v2"
    for scope, part in initial["five_part"].items():
        part["evidence_ids"] = [md["id"] if scope == "stock" else next(row["id"] for row in fact_rows if row["scope"] == scope)]
    shown = m.source_view(doc, characters=5000)
    m.validate_thesis(initial, symbol="AAA", documents=[doc], market_snapshot=md, fact_evidence=fact_rows,
                      assessment=ASSESSMENT, presented_documents=[shown])
    ctx["candidate_research"] = {"schema": m.RESULT_SCHEMA, "assessment": ASSESSMENT, "plan": plan,
        "documents": [doc], "source_index": m.build_source_index([doc], {"AAA": [shown]}),
        "theses": [initial], "market_evidence": [md], "fact_evidence": fact_rows,
        "coverage_summary": "SYNTHETIC_TEST technical research only read a partial body",
        "coverage": {"requested_symbols": ["AAA"], "selected_symbols": ["AAA"], "researched_symbols": ["AAA"],
            "sufficient_symbols": ["AAA"], "insufficient_symbols": [], "contradictory_symbols": [], "deferred": []}}
    return ctx, doc


def test_final_decision_cannot_upgrade_partial_source_into_complete_news_event_evidence():
    ctx, doc = partial_news_context()
    final_thesis = copy.deepcopy(ctx["candidate_research"]["theses"][0])
    final_thesis["strategy"] = "news_event_v2"
    md = ctx["candidate_research"]["market_evidence"][0]
    answer = {"action": "BUY", "action_summary": "SYNTHETIC_TEST final attempt",
        "decisions": [{"symbol": "AAA", "action": "buy", "action_summary": "SYNTHETIC_TEST final attempt",
            "why_this": "SYNTHETIC_TEST", "why_now": "SYNTHETIC_TEST", "entry_condition": "SYNTHETIC_TEST",
            "invalidation": "SYNTHETIC_TEST", "risk_note": "SYNTHETIC_TEST",
            "source_ids": [md["id"], doc.doc_id], "thesis": final_thesis}]}
    with pytest.raises(RequiredModelFailed):
        required.decision_v2(SemanticStub(answer), ctx, [], 100000.)


def test_final_presentations_are_reconstructed_from_exact_raw_prefix_and_bound_sha():
    m = research_module()
    ctx, _ = partial_news_context()
    research = ctx["candidate_research"]
    views = m.presented_views_for_symbol(research, "AAA")
    assert len(views) == 1 and views[0]["presented_content_characters"] == 5000
    assert views[0]["content_complete"] is False and len(views[0]["content"]) == 5000
    research["source_index"][0]["presentations"][0]["presented_content_sha256"] = "0" * 64
    with pytest.raises(RequiredModelFailed):
        m.presented_views_for_symbol(research, "AAA")


@pytest.mark.parametrize("change", ["count", "complete", "original_hash", "missing_presentation", "unknown_symbol"])
def test_final_presentation_metadata_cannot_enlarge_or_invent_a_delivery(change):
    m = research_module()
    ctx, _ = partial_news_context()
    research = ctx["candidate_research"]
    source = research["source_index"][0]
    if change == "count": source["presentations"][0]["presented_content_characters"] += 1
    elif change == "complete": source["presentations"][0]["content_complete"] = True
    elif change == "original_hash": source["original_content_sha256"] = "0" * 64
    elif change == "missing_presentation": source["presentations"] = []
    else: source["presentations"][0]["symbol"] = "BBB"
    with pytest.raises(RequiredModelFailed):
        m.presented_views_for_symbol(research, "AAA")


def test_full_current_original_remains_valid_after_final_presentation_reconstruction():
    m = research_module()
    ctx, doc = partial_news_context()
    research = ctx["candidate_research"]
    shown = m.source_view(doc)
    research["source_index"] = m.build_source_index([doc], {"AAA": [shown]})
    final = copy.deepcopy(research["theses"][0])
    final["strategy"] = "news_event_v2"
    m.validate_thesis(final, symbol="AAA", documents=[doc], market_snapshot=research["market_evidence"][0],
        fact_evidence=research["fact_evidence"], assessment=ASSESSMENT,
        presented_documents=m.presented_views_for_symbol(research, "AAA"))


def test_model_and_public_fact_calendar_excludes_private_override_path_before_identity_hash():
    from trading_system.events import EventCalendar
    from trading_system.publication_projection_v2 import public_research
    m = research_module()
    ctx = context(watch=["AAA"])
    calendar = EventCalendar.__new__(EventCalendar)
    calendar._macro, calendar._earnings = {"2026-10-28": "FOMC"}, {"AAA": "2026-11-01"}
    calendar.override_path = "/SYNTHETIC_PRIVATE/run/work/events_calendar.json"
    ctx["event_calendar"] = calendar
    plan = m.build_research_plan(ctx, ASSESSMENT)
    fact_rows = m.build_fact_evidence(ctx, plan)
    research = {"schema": m.RESULT_SCHEMA, "assessment": ASSESSMENT, "plan": plan,
        "theses": [], "coverage": {}, "counters": {}, "coverage_summary": "SYNTHETIC_TEST",
        "fact_evidence": fact_rows, "market_evidence": [], "source_index": []}
    assert "SYNTHETIC_PRIVATE" not in m.canonical(public_research(research))
    event_fact = next(row for row in fact_rows if row["scope"] == "plan")
    assert set(event_fact["facts"]["event_calendar"]) == {"macro", "earnings", "earnings_source"}
    assert event_fact["id"] == "fx_" + m.digest({key: value for key, value in event_fact.items() if key != "id"})[:24]
    assert calendar.override_path.startswith("/SYNTHETIC_PRIVATE")


@pytest.mark.parametrize("overall_ids", [[], ["SYNTHETIC_SOME_OVERALL_REFERENCE"]])
def test_v2_no_risk_alert_or_numeric_coverage_cannot_create_maximum_prosperity_bonus(overall_ids):
    from trading_system.tech_chain.agents import RiskAssessment
    result = TechChainFusionAgent().execute([], [], {},
        RiskAssessment("SYNTHETIC_TEST risk is unknown or no alert was found; no numeric safety score supplied", [], overall_ids))
    assert all(row.prosperity is None and row.bonus_hint == 1 and "tech.risk" in row.degraded_components for row in result)
    assert all(row.to_dict()["semantic_evidence"]["risk"]["assessment"] for row in result)


class BoundedCleanClient:
    """Synthetic output-capacity boundary, not a diagnosis of a paid failure.

    The real failure recorded INCOMPLETE_MODEL_RESPONSE without its precise
    finish reason. This stub tests that a smaller per-call output obligation
    still processes every complete input and preserves cross-batch origins.
    """
    def __init__(self, *, fail_call=None):
        self.execution = SemanticStub({}).execution
        self.requests = []
        self.fail_call = fail_call

    def complete_json(self, **kwargs):
        payload = json.loads(kwargs["user"].split("\n", 1)[1])
        self.requests.append(copy.deepcopy(kwargs))
        if len(payload["documents"]) > 4 or len(self.requests) == self.fail_call:
            raise LLMUnavailable("SYNTHETIC_TEST incomplete bounded semantic response")
        return {"documents": [{
            "id": row["id"], "tickers": ["AAA"], "sentiment": "mixed",
            "sentiment_score": 0, "events": ["other"],
            "summary_zh": "SYNTHETIC_TEST 保留本篇限定、反证和未知日期，未声称外部事实已核实。",
            "relevance": .5, "chain_ids": [], "sector_etfs": [],
            "provenance": {"evidence_kind": "media",
                "origin_id": payload["all_source_index"][-1]["id"],
                "origin_note": "SYNTHETIC_TEST 原始来源可以处于另一批，归属不能因减批丢失。",
                "time_role": "unknown"},
            "claims": [{"statement": f"SYNTHETIC_TEST 本篇绑定事实{number}。",
                "quote": row["content"][number:number + 40],
                "claim_key": f"SYNTHETIC_TEST {row['id']} fact {number}",
                "time_role": "unknown"} for number in range(4)],
        } for row in payload["documents"]]}


@pytest.mark.parametrize("count", [0, 1, 4, 5, 8, 56])
def test_v2_clean_smaller_output_batches_preserve_every_full_body_and_global_source(count):
    documents = [RawDocument(f"synthetic-clean-{number}", "deepseek_search",
        f"SYNTHETIC_TEST source {number}", f"https://issuer.example/{number}",
        f"SYNTHETIC_TEST unique source {number}. " + "完整正文及反证。" * 1500,
        "", 1793102400., {"native_content_characters": 20000})
        for number in range(count)]
    before = normalize(documents)
    client = BoundedCleanClient()
    cleaned = required.clean_v2(client, documents)
    assert [row.raw.doc_id for row in cleaned] == [row.doc_id for row in documents]
    assert len(client.requests) == max(1, (count + 3) // 4)
    payloads = [json.loads(row["user"].split("\n", 1)[1]) for row in client.requests]
    presented = [row for payload in payloads for row in payload["documents"]]
    assert len(presented) == count and all(len(row["documents"]) <= 4 for row in payloads)
    for actual, original, semantic in zip(presented, documents, cleaned):
        assert actual["content"] == original.content
        assert actual["presented_content_characters"] == len(original.content)
        assert actual["content_complete"] is True
        assert actual["provider_content_complete"] is False
        assert semantic.raw is original and len(semantic.claims) == 4
        assert semantic.provenance["origin_id"] == documents[-1].doc_id
    expected_ids = [row.doc_id for row in documents]
    assert all([row["id"] for row in payload["all_source_index"]] == expected_ids for payload in payloads)
    assert all(row["max_tokens"] == 16384 for row in client.requests)
    assert normalize(documents) == before
    if count == 56:
        from trading_system.llm.request_budget import LIMITS
        budget = research_module().ResearchBudget()
        # 14 cleaning + at most 14 issuer initial/followup + sentiment,
        # risk, narrative and final decision. Token admission remains separate.
        assert len(client.requests) + budget.max_model_calls + 4 == 32 <= LIMITS["max_chat_calls"]
        assert LIMITS["max_tokens"] == 300000


def test_v2_clean_failed_later_batch_is_not_retried_or_returned_as_partial_success():
    documents = [public_document(f"synthetic-clean-{number}") for number in range(8)]
    client = BoundedCleanClient(fail_call=2)
    with pytest.raises(RequiredModelFailed):
        required.clean_v2(client, documents)
    assert len(client.requests) == 2
    assert [[row["id"] for row in json.loads(request["user"].split("\n", 1)[1])["documents"]]
            for request in client.requests] == [
                [row.doc_id for row in documents[:4]], [row.doc_id for row in documents[4:]]]
