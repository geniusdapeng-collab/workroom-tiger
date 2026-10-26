"""Full 43-stage synthetic integration, explicitly without live API evidence."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import uuid

import pytest

import main
from trading_system.agent_evidence import EvidenceStore, verify_events
from trading_system.ledger_io import read_json_strict
from trading_system.llm.deepseek_required import DeepSeekRequiredClient
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import (ExecutionContext, ExecutionPolicy,
    UPGRADE_POLICIES, UPGRADE_MODEL_STAGES, daily_stage_contract, RequiredModelFailed)
from test_strict_daily_core import daily_fixture, stage_value


ADMITTED = "2026-10-20T20:00:00+00:00"
READY = "2026-10-20T20:06:00+00:00"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _thesis(symbol, payload, *, followup=False):
    facts = {row["scope"]: row["id"] for row in payload["fact_evidence"] if row["symbol"] == symbol}
    market = next(row for row in payload["market_evidence"] if row["symbol"] == symbol)
    sources = [row for row in payload.get("documents", []) if symbol in row.get("target_symbols", [])]
    return {"schema_version": 2, "symbol": symbol, "strategy": "technical_flow_v2",
        "status": "insufficient", "issuer_name": None, "issuer_identity_evidence_ids": [],
        "five_part": {scope: {"text": "SYNTHETIC_TEST 依据已经观察的事实，尚缺当前完整入场许可。",
            "evidence_ids": [market["id"] if scope == "stock" else identity]}
            for scope, identity in facts.items()}, "claims": [],
        "event_risk": {"status": "unknown", "text": "SYNTHETIC_TEST 事件时间仍未知，不等于没有事件。", "source_ids": []},
        "entry_triggers": ["SYNTHETIC_TEST 等待数值闸门及证据完整后重新复核。"],
        "falsification": ["SYNTHETIC_TEST 结构失效则取消。"],
        "risk_notes": ["SYNTHETIC_TEST 仅离线夹具，不能当成真实研究。"],
        "gaps": ["SYNTHETIC_TEST 尚缺真实市场与公司研究。"],
        "followup": {"needed": bool(followup and sources), "question": None,
                     "fetch_source_id": sources[0]["id"] if followup and sources else None}}


def _answer(stage, payload, schema, *, followup=True):
    props = schema["properties"]
    if stage == "clean.llm_semantic":
        return {"documents": [{"id": row["id"], "tickers": [], "sentiment": "neutral",
            "sentiment_score": 0., "events": [], "summary_zh": "SYNTHETIC_TEST 完整摘要；来源归属与当前含义尚不能核实。",
            "relevance": .5, "chain_ids": [], "sector_etfs": [], "claims": [],
            "provenance": {"evidence_kind": "unknown", "origin_id": None,
                           "origin_note": "SYNTHETIC_TEST 不编造独立原始来源。", "time_role": "unknown"}}
            for row in payload["documents"]]}
    if stage == "tech.sentiment":
        return {"chains": [{"chain_id": chain, "heat": None, "sentiment_score": None,
            "narrative_change": "unknown", "key_drivers": ["SYNTHETIC_TEST 未核实，不补中性分。"],
            "evidence_ids": []} for chain in payload["chains"]]}
    if stage == "tech.risk":
        return {"assessment": "SYNTHETIC_TEST 所有风险结论保留未知限定，未生成预警不等于没有风险。",
                "evidence_ids": [], "alerts": []}
    if stage == "sector.narrative":
        return {"sectors": [{"etf": etf, "narrative_score": None, "eps_revision_bias": "unknown",
            "guidance_tone": "unknown", "evidence": ["SYNTHETIC_TEST 未获有据叙事，剔除该维度。"],
            "evidence_ids": []} for etf in payload["etfs"]]}
    if stage == "research.candidates":
        return {"coverage_summary": "SYNTHETIC_TEST 对全部目标做了定向检索与受控补读，结论不足。",
                "theses": [_thesis(row["symbol"], payload,
                    followup=followup and payload["phase"] == "initial") for row in payload["targets"]]}
    assert stage == "decision.debate"
    theses = {row["symbol"]: row for row in payload["candidate_research"]["theses"]}
    symbols = [row["ticker"] for row in payload["approved_picks"] + payload["watch_candidates"]]
    market = {row["symbol"]: row for row in payload["market_evidence"]}
    return {"action": "AVOID" if payload["numeric_action"] == "AVOID" else "WAIT",
        "action_summary": "SYNTHETIC_TEST 未获完整买入许可，继续观察，不构成真实建议。",
        "decisions": [{"symbol": symbol, "action": "watch", "action_summary": "SYNTHETIC_TEST 等待复核。",
            "why_this": "SYNTHETIC_TEST 当前量化候选，尚不足入场。",
            "why_now": "SYNTHETIC_TEST 当前闸门和证据未同时齐备。",
            "entry_condition": "SYNTHETIC_TEST 全部条件满足后重新研究。",
            "invalidation": "SYNTHETIC_TEST 原结构失效则取消。",
            "risk_note": "SYNTHETIC_TEST 仅测试，不能据此成交。",
            "source_ids": [market[symbol]["id"]], "thesis": theses[symbol]} for symbol in symbols]}


def upgrade_fixture(tmp_path, monkeypatch, *, phase="daily", failure_stage=None,
                    mutate_answer=None, followup=True, admitted=ADMITTED, ready=READY):
    original, root, args, fixture, denied = daily_fixture(tmp_path, monkeypatch)
    profile = next(key for key, value in UPGRADE_POLICIES.items() if value[1] == phase)
    execution = ExecutionContext(run_id=root.name, run_date=original.run_date, paths=original.paths,
        policy=ExecutionPolicy(UPGRADE_POLICIES[profile][0], model_mode="enabled",
            provider_names=("yahoo",), search_source_names=("deepseek_search",)),
        store=EvidenceStore(root, root.name), admitted_at_utc=admitted)
    args.mode, args.universe, args.universe_file, args.picks = phase, "full", None, 10
    monkeypatch.setattr("trading_system.universe.load_full_universe", lambda: (["AAA", "BBB", "CCC"], "nasdaqtrader"))
    monkeypatch.setattr("trading_system.workflow_runtime_v2.utc_now", lambda: ready)
    transport_instant = datetime.fromisoformat(admitted) + timedelta(minutes=5)
    class TransportClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromtimestamp(transport_instant.timestamp(), timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr("trading_system.llm.deepseek_required.datetime", TransportClock)
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
                       "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "SYNTHETIC_TEST-secret-only"}.items():
        monkeypatch.setenv(key, value)
    attempts = []
    def post(client, path, request):
        stage = client.execution._active.spec.name
        attempts.append((stage, path))
        client._remote_requests += 1
        if stage == failure_stage:
            raise LLMUnavailable("SYNTHETIC_TEST required stage failure")
        if path == "/v1/tools/search":
            fingerprint = hashlib.sha256(request["query"].encode()).hexdigest()
            payload = {"results": [{"url": "https://issuer.example/fixture/" + fingerprint,
                "title": "SYNTHETIC_TEST observed source " + fingerprint,
                "content": "SYNTHETIC_TEST source text with explicitly unknown implications. " + fingerprint,
                "date": "2026-10-19T12:00:00+00:00"}]}
        elif path == "/v1/tools/fetch":
            payload = {"title": "SYNTHETIC_TEST original page", "content": "SYNTHETIC_TEST full returned page; publication date is not provided."}
        else:
            framed = request["messages"][1]["content"]
            user, rest = framed.split("\nJSON schema:\n", 1)
            schema = json.loads(rest.split("\nJSON field guidance:\n", 1)[0])
            inputs = json.loads(user.split("\n", 1)[1])
            answer = _answer(stage, inputs, schema, followup=followup)
            if mutate_answer:
                answer = mutate_answer(stage, answer)
            payload = {"id": "fixture-" + uuid.uuid4().hex, "model": "deepseek-flash",
                "object": "chat.completion", "system_fingerprint": "fp-fixture",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}
        return payload, _hash(payload)
    monkeypatch.setattr(DeepSeekRequiredClient, "_post_json", post)
    return execution, root, args, fixture, denied, attempts


@pytest.mark.parametrize("phase", ["daily", "premarket", "intraday"])
def test_all43_stages_produce_bound_research_lifecycle_and_evaluation(tmp_path, monkeypatch, phase):
    execution, root, args, _, denied, attempts = upgrade_fixture(tmp_path, monkeypatch, phase=phase)
    output = main._daily(args, "yahoo", execution=execution)
    checked = verify_events(root, execution.run_id, daily_stage_contract(True, True))
    assert checked["stage_count"] == 43 and checked["degraded"] == []
    result = output.result
    assert result.raw["workflow_phase"] == phase
    assert result.raw["execution_policy"] == "whitepaper-execution-v2"
    assert set(result.raw["model"]["required_stages"]) == set(UPGRADE_MODEL_STAGES)
    assert result.raw["model"]["budget"]["reserved_tokens"] == 0
    assert result.raw["model"]["fetch_count"] == len([row for row in attempts if row[1].endswith("fetch")])
    assert result.raw["model"]["fetch_count"] > 0
    assert result.raw["business_evaluation"]["account"]["profitability_established"] is False
    assert result.raw["business_evaluation"]["account"]["n_closed"] == 0
    assert result.raw["opportunity_lifecycle"] == read_json_strict(execution.paths.reports_dir / "opportunity_ledger.json")
    assert result.raw["business_evaluation"] == read_json_strict(execution.paths.reports_dir / "business-evaluation.json")
    assert read_json_strict(output.report_paths["json"])["raw"]["candidate_research"]["schema"] == "tiger.candidate-research.v2"
    assert stage_value(execution, root, "journal.execution_sync")["records"] == []
    assert denied == []


def test_premarket_excludes_partial_daily_candle_and_keeps_held_research_and_current_nav(tmp_path, monkeypatch):
    from trading_system.ledger_io import write_json_atomic
    execution, root, args, fixture, denied, _ = upgrade_fixture(tmp_path, monkeypatch,
        phase="premarket", admitted="2026-10-20T13:00:00+00:00", ready="2026-10-20T13:06:00+00:00")
    account = read_json_strict(execution.paths.sim_path)
    account["cash"] = 90000.
    account["positions"] = [{"ticker": "AAA", "shares": 100, "entry_price": 90.,
        "entry_date": "2026-10-19", "stop": 85., "last_bar_date": "2026-10-19", "bars_held": 1,
        "risk_usd": 500., "source": "SYNTHETIC_TEST", "chain": "", "sector": ""}]
    write_json_atomic(execution.paths.sim_path, account)
    actual = main._daily(args, "yahoo", execution=execution)
    value = actual.result
    assert value.raw["daily_bars_cutoff"] == "2026-10-19"
    assert value.raw["freshness"]["benchmark_last_bar"] == "2026-10-19"
    assert value.raw["portfolio_snapshot"]["nav"] != 100000.
    assert value.raw["portfolio_snapshot"]["nav"] > 90000.
    assert value.raw["candidate_research"]["coverage"]["researched_symbols"]
    target = next(row for row in value.raw["candidate_research"]["plan"]["targets"] if row["symbol"] == "AAA")
    assert "holding" in target["roles"] and target["holding_facts"]["shares"] == 100
    assert all(row["as_of"] <= "2026-10-19" for row in value.raw["candidate_research"]["market_evidence"])
    assert actual.sim_state["positions"][0]["shares"] == 100
    assert actual.sim_state["equity_curve"][-1]["valuation_date"] == "2026-10-19"
    assert denied == []


@pytest.mark.parametrize("stage", ["research.candidates", "decision.debate"])
def test_new_required_stage_failure_never_reaches_account_commit(tmp_path, monkeypatch, stage):
    execution, root, args, _, denied, _ = upgrade_fixture(tmp_path, monkeypatch, failure_stage=stage)
    before = execution.paths.sim_path.read_bytes()
    with pytest.raises(RequiredModelFailed):
        main._daily(args, "yahoo", execution=execution)
    assert execution.summary()[-1]["stage"] == stage
    assert execution.summary()[-1]["status"] == "failed"
    assert execution.paths.sim_path.read_bytes() == before
    assert not (root / "receipt.json").exists()
    assert denied == []
