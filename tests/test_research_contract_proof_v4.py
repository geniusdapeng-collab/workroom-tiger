"""Two real v4 semantic stages and production replay; SYNTHETIC_TEST only.

Only requests.post is replaced at the provider boundary. Production request
builders, response parsing, reservations, receipts, research/final validators
and ReplayClient run unchanged. This is a bounded two-stage contract test, not
a 43-stage verifier, publication receipt or evidence of real trading research.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from trading_system.agent_evidence import EvidenceInvalid, normalize
from trading_system.data_models import StockCandidate
from trading_system.llm.candidate_research import canonical, research_candidates
from trading_system.llm.clean_output import (
    CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, build_clean_compact_lists_request,
)
from trading_system.llm.deepseek_required import DeepSeekRequiredClient, validate_schema
from trading_system.llm.request_budget import BUDGET_POLICY_V4, BudgetGuard, token_upper_bound
from trading_system.llm.required import (
    clean_keyed_schema_v2, decision_v2, final_identity_values_v2,
    final_payload_wire_v2,
)
from trading_system.llm.required_proof_v2 import ReplayClient, _restore
from trading_system.llm.required_schemas import clean_schema_v2
from trading_system.redline.execution import ExecutionPolicy, UPGRADE_POLICY_ID


ADMITTED = "2026-10-28T16:00:00+00:00"
STAGES = ("research.candidates", "decision.debate")
KEY = "SYNTHETIC_TEST-only-not-a-real-credential"


def h(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write(path, value):
    path.write_text(canonical(value) + "\n")


def frame_parts(body):
    user, rest = body["messages"][1]["content"].split("\nJSON schema:\n", 1)
    schema, guidance = rest.split("\nJSON field guidance:\n", 1)
    task, payload = user.split("\n", 1)
    return task, json.loads(payload), json.loads(schema), json.loads(guidance)


class RecordingExecution:
    """Capture actual client call_io inputs/results without a daily runner."""
    upgrade = True
    run_id = "SYNTHETIC_TEST-research-contract-proof-v4"
    run_date = date(2026, 10, 28)
    admitted_at_utc = ADMITTED
    deadline_at_utc = "2026-10-28T18:00:00+00:00"

    def __init__(self):
        self.policy = ExecutionPolicy(UPGRADE_POLICY_ID, model_mode="enabled",
            provider_names=("yahoo",), search_source_names=("deepseek_search",),
            budget_policy_id=BUDGET_POLICY_V4,
            clean_output_policy_id=CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1)
        self.calls = {stage: [] for stage in STAGES}
        self._active = None

    def set_stage(self, stage):
        assert stage in STAGES
        self._active = SimpleNamespace(spec=SimpleNamespace(name=stage))

    def call_io(self, method, inputs, invoke):
        request = deepcopy(inputs)
        proof = invoke()
        self.calls[self._active.spec.name].append({"method": method,
            "request": request, "proof": deepcopy(proof)})
        return proof


class Response:
    def __init__(self, value):
        self.body = json.dumps(value, ensure_ascii=False).encode()
        self.raw = io.BytesIO(self.body)
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.closed = False

    def close(self):
        self.closed = True
        self.raw.close()


def research_answer(payload):
    """Predetermined synthetic model answer; never edits a production answer."""
    initial = payload["phase"] == "initial"
    source = next(row for row in payload["documents"]
                  if row["source"] == ("deepseek_search" if initial else "deepseek_fetch"))
    facts = {row["scope"]: row["id"] for row in payload["fact_evidence"]}
    market = payload["market_evidence"][0]
    selected = next(row for row in payload["research_quote_selection"]["sources"] if row["source_id"] == source["id"])
    claim = {"claim_id": "SYNTHETIC_TEST-claim" if initial else "SYNTHETIC_TEST-fetch-claim",
        "statement": "SYNTHETIC_TEST observed fixture text only.",
        "source_id": source["id"], "quote_part_id": selected["quote_parts"]["rows"][0][0], "origin_id": source["id"],
        "evidence_kind": "primary", "time_role": "current"}
    if not initial:
        claim["date_source_id"] = source["search_source_id"]
    thesis = {"schema_version": 2, "symbol": "AAA", "strategy": "news_event_v2",
        "status": "sufficient", "issuer_name": "SYNTHETIC_TEST issuer",
        "issuer_identity_evidence_ids": [source["id"]],
        "five_part": {scope: {"text": "SYNTHETIC_TEST bounded fixture basis.",
            "evidence_ids": [market["id"] if scope == "stock" else identity]}
            for scope, identity in facts.items()},
        "claims": [claim], "event_risk": {"status": "unknown",
            "text": "SYNTHETIC_TEST no claim about actual event risk.", "source_ids": []},
        "entry_triggers": ["SYNTHETIC_TEST no real entry is authorized."],
        "falsification": ["SYNTHETIC_TEST cancel if the fixture evidence changes."],
        "risk_notes": ["SYNTHETIC_TEST offline test only."], "gaps": [],
        "followup": {"needed": initial, "question": None,
                     "fetch_source_id": source["id"] if initial else None}}
    if not initial:
        retained = payload["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]
        thesis["claims"].append({"retained_initial_claim_ref": retained["retained_initial_claim_ref"],
            "statement": "SYNTHETIC_TEST model revisits the retained initial interpretation."})
    return {"coverage_summary": "SYNTHETIC_TEST bounded issuer fixture.", "theses": [thesis]}


def decision_answer(payload):
    identities = [row[1] for row in payload["final_wire"]["identity_index"]["rows"]]
    restored = final_payload_wire_v2({key: value for key, value in payload.items()
                                     if key != "assessment"}, decode=True)
    answer = {"action": "WAIT", "action_summary": "SYNTHETIC_TEST watch only.",
        "decisions": [{"symbol": market["symbol"], "action": "watch",
            "action_summary": "SYNTHETIC_TEST wait.", "why_this": "SYNTHETIC_TEST candidate.",
            "why_now": "SYNTHETIC_TEST frozen time.", "entry_condition": "SYNTHETIC_TEST no entry.",
            "invalidation": "SYNTHETIC_TEST changed evidence.", "risk_note": "SYNTHETIC_TEST offline.",
            "market_evidence_id": market["id"], "article_source_ids": [],
            "thesis": {"reuse_thesis": market["symbol"]}} for market in restored["market_evidence"]]}
    return final_identity_values_v2(answer, identities)


def ownership_growth_answer(payload):
    from test_short_term_research_v2 import thesis
    rows = []
    shared = next(row for row in payload["documents"] if row["title"] == "SYNTHETIC_TEST issuer source")
    for target in payload["targets"]:
        symbol = target["symbol"]
        row = thesis()
        row["symbol"] = symbol
        for scope in row["five_part"]:
            identity = (next(item["id"] for item in payload["market_evidence"] if item["symbol"] == symbol)
                if scope == "stock" else next(item["id"] for item in payload["fact_evidence"]
                    if item["symbol"] == symbol and item["scope"] == scope))
            row["five_part"][scope]["evidence_ids"] = [identity]
        if payload["phase"] == "initial":
            row["claims"] = [{"claim_id": "SYNTHETIC_TEST-" + symbol,
                "statement": "SYNTHETIC_TEST explicit ordinary observed fact.",
                "source_id": shared["id"], "quote_part_id": "p0", "origin_id": shared["id"],
                "evidence_kind": "primary", "time_role": "current"}]
            if symbol == "BBB":
                row["followup"] = {"needed": True, "question": "SYNTHETIC_TEST shared source", "fetch_source_id": None}
        else:
            ref = payload["research_quote_selection"]["retained_initial_claims"][0]["claims"][0]["retained_initial_claim_ref"]
            row["claims"] = [{"retained_initial_claim_ref": ref,
                "statement": "SYNTHETIC_TEST reconsider retained original fact."}]
        rows.append(row)
    return {"coverage_summary": "SYNTHETIC_TEST shared source ownership fixture.", "theses": rows}


@pytest.fixture
def recorded_v4(tmp_path, monkeypatch, request):
    import trading_system.llm.deepseek_required as transport

    execution = RecordingExecution()
    ownership_growth = getattr(request, "param", {}).get("ownership_growth", False)
    instant = datetime.fromisoformat(ADMITTED) + timedelta(seconds=1)

    class TransportClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromtimestamp(instant.timestamp(), timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(transport, "datetime", TransportClock)
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": KEY}.items():
        monkeypatch.setenv(key, value)
    attempts, responses = [], []

    def post(url, **kwargs):
        stage = execution._active.spec.name
        assert url in {"https://api.deepseek.com" + path for path in
                       ("/v1/tools/search", "/v1/tools/fetch", "/chat/completions")}
        assert kwargs["headers"]["Authorization"] == "Bearer " + KEY
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        body = kwargs["json"]
        attempts.append({"stage": stage, "endpoint": url, "request_sha256": h(body)})
        if url.endswith("/search"):
            payload = {"results": [{"url": "https://issuer.example/synthetic-release",
                "title": "SYNTHETIC_TEST issuer source", "content": "SYNTHETIC_TEST observed search text.",
                "date": "2026-10-27T16:00:00+00:00"}]}
            if ownership_growth and '"BBB" stock' in body["query"]:
                payload["results"][0].update(url="https://issuer.example/bbb",
                    title="SYNTHETIC_TEST BBB source", content="SYNTHETIC_TEST original BBB evidence.")
        elif url.endswith("/fetch"):
            payload = {"title": "SYNTHETIC_TEST fetched page",
                "content": "SYNTHETIC_TEST observed fetched source text."}
        else:
            _, inputs, _, _ = frame_parts(body)
            answer = ((ownership_growth_answer(inputs) if ownership_growth else research_answer(inputs))
                      if stage == STAGES[0] else decision_answer(inputs))
            payload = {"id": "SYNTHETIC_TEST-response-" + str(len(attempts)), "model": "deepseek-flash",
                "object": "chat.completion", "system_fingerprint": "fp-SYNTHETIC_TEST",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
        response = Response(payload)
        responses.append(response)
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.sessions.Session, "request",
        lambda *args, **kwargs: pytest.fail("SYNTHETIC_TEST forbids real Session transport"))
    symbols = ["AAA", "BBB"] if ownership_growth else ["AAA"]
    context = {"trade_date": execution.run_date, "picks": [],
        "watchlist": [StockCandidate(symbol, sector_etf="XLK", chain_id="SYNTHETIC_TEST") for symbol in symbols],
        "research_holdings": [], "mrs": None, "sectors": [], "chains": [],
        "action": "WAIT", "market_data": {"stock_ohlcv": {symbol: pd.DataFrame(
            {"Close": [100.]}, index=pd.to_datetime(["2026-10-27"])) for symbol in symbols}}}
    inputs = {"context": normalize(context), "execution": {
        "run_id": execution.run_id, "run_date": execution.run_date.isoformat(),
        "admitted_at_utc": execution.admitted_at_utc, "deadline_at_utc": execution.deadline_at_utc,
        "policy": execution.policy.to_dict()}, "reference_capital_usd": 100000.}
    write(tmp_path / "inputs.json", inputs)
    client = DeepSeekRequiredClient(execution)
    execution.set_stage(STAGES[0])
    research = research_candidates(client, context, [])
    context["candidate_research"] = research
    execution.set_stage(STAGES[1])
    decision = decision_v2(client, context, [], inputs["reference_capital_usd"])
    write(tmp_path / "records.json", execution.calls)
    write(tmp_path / "outputs.json", {"research": research, "decision": decision, "context": context})
    write(tmp_path / "receipts.json", {"chat": client.receipts, "search": client.search_receipts,
        "fetch": client.fetch_receipts, "budget": client.budget.snapshot(), "mock_http": attempts})
    assert len(attempts) == (6 if ownership_growth else 5) and all(response.closed for response in responses)
    assert KEY not in canonical(execution.calls)
    return tmp_path


def replay_inputs(root, records, monkeypatch):
    """Reconstruct from disk, with initialization and transport forbidden."""
    def forbidden(*args, **kwargs):
        pytest.fail("SYNTHETIC_TEST replay must not create a transport or consult HTTP")

    monkeypatch.setattr(DeepSeekRequiredClient, "__init__", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    for name in ("LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    inputs = json.loads((root / "inputs.json").read_text())
    state = inputs["execution"]
    policy = {**state["policy"], **{key: tuple(state["policy"][key])
        for key in ("provider_names", "search_source_names")}}
    execution = SimpleNamespace(**{key: state[key] for key in
        ("run_id", "admitted_at_utc", "deadline_at_utc")},
        run_date=date.fromisoformat(state["run_date"]), policy=ExecutionPolicy(**policy), upgrade=True)
    context = _restore(inputs["context"], native=True)
    replay = ReplayClient(execution, records)
    replay.set_stage(STAGES[0])
    research = research_candidates(replay, context, [])
    context["candidate_research"] = research
    replay.set_stage(STAGES[1])
    decision = decision_v2(replay, context, [], inputs["reference_capital_usd"])
    replay.finish()
    return replay, normalize({"research": research, "decision": decision, "context": context})


def test_two_real_stages_bind_v4_guide_schema_receipts_and_independent_replay(recorded_v4, monkeypatch):
    root = recorded_v4
    records = json.loads((root / "records.json").read_text())
    expected = json.loads((root / "outputs.json").read_text())
    receipts = json.loads((root / "receipts.json").read_text())
    chats = [row for stage in STAGES for row in records[stage] if row["method"] == "model.deepseek.chat"]
    assert len(chats) == 3
    phases = []
    for row in chats:
        request, proof = row["request"], row["proof"]
        _, payload, schema, guidance = frame_parts(request["request"])
        assert guidance == {} and schema == request["output_schema"]
        assert request["schema"] == "tiger.deepseek-request.v1" and proof["schema"] == "tiger.deepseek-call.v1"
        assert request["schema_sha256"] == proof["schema_sha256"] == h(schema)
        assert request["request_sha256"] == proof["request_sha256"] == h(request["request"])
        assert request["request"]["max_tokens"] == proof["budget_reservation"]["output_token_upper_bound"] == 65536
        assert request["request"]["reasoning_effort"] == "high"
        assert request["request"]["thinking"] == {"type": "enabled"}
        assert "contains" in canonical(schema)
        if row["request"]["stage"] == STAGES[1]:
            assert proof["parsed"]["decisions"][0]["thesis"] == {"reuse_thesis": "AAA"}
            expected_market_ref = next(item["id"] for item in payload["market_evidence"]
                                       if item["symbol"] == "AAA")
            assert proof["parsed"]["decisions"][0]["market_evidence_id"] == expected_market_ref
            assert proof["parsed"]["decisions"][0]["article_source_ids"] == []
            assert "source_ids" not in proof["parsed"]["decisions"][0]
            payload = final_payload_wire_v2({key: value for key, value in payload.items()
                                            if key != "assessment"}, decode=True)
        guide = payload["request_bindings"]
        assert guide["schema"] == "tiger.thesis-request-bindings.v1"
        assert guide["current_max_age_seconds"] == 604800
        assert guide["assessment"]["admitted_at_utc"] == ADMITTED
        phases.append(guide["phase"])
    assert phases == ["initial", "final", "decision"]
    replay, actual = replay_inputs(root, records, monkeypatch)
    assert actual == expected
    assert replay.receipts == receipts["chat"]
    assert replay.search_receipts == receipts["search"] and replay.fetch_receipts == receipts["fetch"]
    assert replay.budget.snapshot() == receipts["budget"]
    assert receipts["budget"]["counts"] == {"chat": 3, "search": 1, "fetch": 1}
    assert receipts["budget"]["actual_tokens"] == 600 and receipts["budget"]["reserved_tokens"] == 0
    assert expected["decision"]["model_reference_usage"][0]["source_symbol"] == "AAA"
    assert expected["decision"]["model_output"]["action"] == "WAIT"
    assert expected["research"]["counters"] == {"chat": 2, "search": 1, "fetch": 1}
    initial, final = expected["research"]["model_rounds"]
    assert "quote_part_id" in initial["selection_output"]["theses"][0]["claims"][0]
    retained_wire = final["selection_output"]["theses"][0]["claims"][1]
    assert set(retained_wire) == {"retained_initial_claim_ref", "statement"}
    initial_claim = initial["output"]["theses"][0]["claims"][0]
    retained_claim = final["output"]["theses"][0]["claims"][1]
    assert {key: value for key, value in initial_claim.items() if key != "statement"} == {
        key: value for key, value in retained_claim.items() if key != "statement"}
    assert initial_claim["statement"] != retained_claim["statement"]
    assert "date_source_id" not in retained_claim
    assert final["claim_reference_usage"][1]["initial_claim_sha256"] == h(initial_claim)

    # The unchanged clean protocol remains a separate 32768/none contract.
    ids = ["SYNTHETIC_TEST-clean-source"]
    semantic = clean_schema_v2(ids, ["SYNTHETIC_TEST_chain"], ["XLK"],
        quote_part_ids={ids[0]: ["p0"]}, all_doc_ids=ids)
    clean = build_clean_compact_lists_request(system="SYNTHETIC_TEST clean contract",
        user="SYNTHETIC_TEST complete source", schema_hint={},
        schema=clean_keyed_schema_v2(semantic, ids))
    assert clean["max_tokens"] == 32768 and clean["reasoning_effort"] == "none"
    assert clean["thinking"] == {"type": "disabled"}
    assert token_upper_bound(clean, allow_clean_compact_regeneration=True)[1] == 32768
    write(root / "replay-observation.json", {"synthetic": True, "activated": False,
        "replay_equal": True, "phases": phases, "actual_http_transport_calls": 0,
        "mock_http_calls": len(receipts["mock_http"]), "budget": replay.budget.snapshot(),
        "clean_control": {"max_tokens": clean["max_tokens"], "reasoning_effort": clean["reasoning_effort"]}})


@pytest.mark.parametrize("recorded_v4", [{"ownership_growth": True}], indirect=True)
def test_merged_source_ownership_growth_passes_production_decision_and_disk_replay(recorded_v4, monkeypatch):
    root = recorded_v4
    records = json.loads((root / "records.json").read_text())
    expected = json.loads((root / "outputs.json").read_text())
    receipts = json.loads((root / "receipts.json").read_text())
    research = expected["research"]
    shared = next(row for row in research["source_index"] if row["title"] == "SYNTHETIC_TEST issuer source")
    assert shared["target_symbols"] == ["AAA", "BBB"]
    initial = frame_parts(next(row["request"]["request"] for row in records[STAGES[0]]
        if row["method"] == "model.deepseek.chat"))[1]
    shown = next(row for row in initial["documents"] if row["id"] == shared["id"])
    assert shown["target_symbols"] == ["AAA"]
    assert research["counters"] == {"chat": 2, "search": 3, "fetch": 0}
    assert {row["symbol"] for row in expected["decision"]["model_output"]["decisions"]} == {"AAA", "BBB"}
    replay, actual = replay_inputs(root, records, monkeypatch)
    assert actual == expected and replay.budget.snapshot() == receipts["budget"]
    assert replay.receipts == receipts["chat"] and replay.search_receipts == receipts["search"]


def coherently_forge(records, stage, phase, component):
    """Rehash every request/schema/reservation and cumulative budget snapshot."""
    forged = deepcopy(records)
    target = next(row for row in forged[stage] if row["method"] == "model.deepseek.chat"
        and frame_parts(row["request"]["request"])[1]["request_bindings"]["phase"] == phase)
    request, proof = target["request"], target["proof"]
    task, payload, schema, guidance = frame_parts(request["request"])
    before = h(request["request"])
    if component == "guide":
        assert stage == STAGES[0]
        payload["request_bindings"]["current_max_age_seconds"] = 604799
    elif component == "quote_part":
        assert stage == STAGES[0] and phase == "initial"
        payload["research_quote_selection"]["sources"][0]["quote_parts"]["rows"][0][1] += " forged"
    elif component == "retained_version":
        assert stage == STAGES[0] and phase == "final"
        payload["research_quote_selection"]["retained_initial_claims"][0]["initial_thesis_sha256"] = "0" * 64
    else:
        assert component == "schema" and stage == STAGES[1]
        thesis = schema["properties"]["decisions"]["items"]["properties"]["thesis"]["oneOf"][0]
        stock = thesis["allOf"][1]["oneOf"][0]["properties"]["five_part"]["properties"]["stock"]
        stock["properties"]["evidence_ids"]["contains"] = {"type": "string"}
    request["request"]["messages"][1]["content"] = (task + "\n" + canonical(payload)
        + "\nJSON schema:\n" + canonical(schema) + "\nJSON field guidance:\n" + canonical(guidance))
    request["output_schema"] = schema
    request["schema_sha256"] = proof["schema_sha256"] = h(schema)
    # The altered schema still admits the recorded answer. Rejection must be
    # caused by independently reconstructed input, not this fixture's output.
    validate_schema(proof["parsed"], schema)
    assert h(request["request"]) != before
    guard = BudgetGuard.for_policy(BUDGET_POLICY_V4,
        clean_output_policy_id=CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1, clock=lambda: 0.)
    for name in STAGES:
        for row in forged[name]:
            req, item = row["request"], row["proof"]
            kind = row["method"].rsplit(".", 1)[1]
            req["request_sha256"] = item["request_sha256"] = h(req["request"])
            reservation = guard.reserve(kind, req["call_id"], req["request"])
            req["budget_reservation"] = deepcopy(reservation)
            item["budget_reservation"] = deepcopy(reservation)
            guard.settle(req["call_id"], item["usage"] if kind == "chat" else None)
            item["budget_after"] = guard.snapshot()
            if kind == "chat":
                _, _, framed_schema, _ = frame_parts(req["request"])
                assert framed_schema == req["output_schema"]
                assert req["schema_sha256"] == item["schema_sha256"] == h(framed_schema)
                assert item["response_sha256"] == h(item["response"])
                validate_schema(item["parsed"], framed_schema)
            assert req["request_sha256"] == reservation["request_sha256"] == item["request_sha256"]
    return forged


@pytest.mark.parametrize("stage,phase,component", [
    ("research.candidates", "initial", "guide"),
    ("research.candidates", "initial", "quote_part"),
    ("research.candidates", "final", "retained_version"),
    ("decision.debate", "decision", "schema"),
])
def test_rehashed_contract_forgery_is_rejected_by_production_replay(
        recorded_v4, monkeypatch, stage, phase, component):
    root = recorded_v4
    records = json.loads((root / "records.json").read_text())
    forged = coherently_forge(records, stage, phase, component)
    write(root / "forged-records.json", forged)
    # Re-read the coherently rewritten evidence rather than share live objects.
    forged = json.loads((root / "forged-records.json").read_text())
    with pytest.raises(EvidenceInvalid, match="request_identity_or_payload") as caught:
        replay_inputs(root, forged, monkeypatch)
    assert caught.value.stage == stage
    write(root / "rejection.json", {"synthetic": True, "component": component,
        "phase": phase, "stage": caught.value.stage, "reason": str(caught.value),
        "request_schema_and_all_budget_hashes_recomputed": True,
        "altered_schema_accepts_recorded_output": True, "activated": False})
