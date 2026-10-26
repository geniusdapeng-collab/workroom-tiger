"""Independent integration regressions; every input is SYNTHETIC_TEST.

Numerical stage fixtures deliberately exercise a legal BUY. They do not claim
that these scores, quotes, model answers or paper plans came from live markets.
The actual canonical risk allocator, 43-stage orchestration and ledgers execute.
"""
from datetime import datetime, timezone

import pytest


def legal_buy_fixture(tmp_path, monkeypatch):
    import trading_system.pipeline as pipeline
    from trading_system.agents.mrs_agent import MRSAgent
    from trading_system.agents.sector_agent import SectorAgent
    from trading_system.agents.tss_agent import TSSAgent
    from test_short_term_core_v2 import upgrade_fixture

    def answer(stage, value):
        if stage == "research.candidates":
            for thesis in value["theses"]:
                thesis.update(status="sufficient", gaps=[])
        elif stage == "decision.debate":
            value["action"] = "BUY"
            for decision in value["decisions"]:
                decision["action"] = "buy" if decision["symbol"] == "NVDA" else "watch"
                decision["thesis"].update(status="sufficient", gaps=[])
        return value

    execution, root, args, provider, denied, attempts = upgrade_fixture(tmp_path, monkeypatch,
        phase="intraday", admitted="2026-10-20T13:45:00+00:00",
        ready="2026-10-20T13:51:00+00:00", mutate_answer=answer, followup=False)
    monkeypatch.setattr("trading_system.universe.load_full_universe", lambda: (["NVDA"], "nasdaqtrader"))
    args.top = 4
    original_batch = provider.ohlcv_batch
    def batch(symbols, days=400):
        values = original_batch(symbols, days)
        if "NVDA" in symbols:
            values["NVDA"] = provider.ohlcv("NVDA", days)
        return values
    monkeypatch.setattr(provider, "ohlcv_batch", batch)
    def quote(symbol):
        frame = provider.ohlcv(symbol, 420)
        frame = frame[frame.index.date <= execution.daily_bars_cutoff]
        return {"price": float(frame["Close"].iloc[-1]), "ts": "2026-10-20T13:51:00+00:00",
                "kind": "realtime", "stale": False, "evidence_id": "SYNTHETIC_TEST_quote_" + symbol}
    monkeypatch.setattr(provider, "quote", quote)
    class MarketClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 20, 13, 51, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr(pipeline, "datetime", MarketClock)
    original_mrs, original_sector, original_tss = MRSAgent.execute, SectorAgent.execute, TSSAgent.execute
    def market(agent, context):
        value = original_mrs(agent, context)
        value.mrs_star, value.mrs_raw, value.position_cap = 7., 7., (0., .7)
        value.allow_new_positions, value.shock = True, False
        return value
    def sector(agent, context):
        values = original_sector(agent, context)
        for value in values:
            if value.etf in {"XLK", "SMH"}:
                value.shs, value.breadth, value.in_main_pool = 8., 80., True
        return values
    def candidates(agent, context):
        values = original_tss(agent, context)
        for value in values:
            if value.ticker == "NVDA":
                value.entry_template, value.tss_final = "A", 8.
                value.key_level, value.stop_price = value.price, value.price * .95
        return values
    monkeypatch.setattr(MRSAgent, "execute", market)
    monkeypatch.setattr(SectorAgent, "execute", sector)
    monkeypatch.setattr(TSSAgent, "execute", candidates)
    return execution, root, args, denied, attempts


def test_legal_buy_fresh_quote_reuses_committed_account_plan_without_duplicate_proposal(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    import main
    from trading_system.agent_evidence import verify_events
    from trading_system.ledger_io import read_json_strict
    from trading_system.llm.required_proof_v2 import verify_required_model_v2
    from trading_system.redline.execution import daily_stage_contract
    execution, root, args, denied, _ = legal_buy_fixture(tmp_path, monkeypatch)
    source = Path(__file__).resolve().parents[1] / "trading_system"
    destination = root / "source/trading_system"
    destination.mkdir(parents=True)
    for name in ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py", "gate.py", "currency.py", "calendar.py"):
        shutil.copy2(source / name, destination / name)
    output = main._daily(args, "yahoo", execution=execution)
    assert verify_events(root, root.name, daily_stage_contract(True, True))["stage_count"] == 43
    assert output.result.action == "BUY"
    assert [pick.ticker for pick in output.result.picks] == ["NVDA"]
    pending = output.sim_state["pending"]
    assert [row["ticker"] for row in pending] == ["NVDA"]
    ledger = output.result.raw["opportunity_lifecycle"]
    opportunity = next(row for row in ledger["opportunities"] if row["ticker"] == "NVDA")
    assert opportunity["state"] == "INTENT_PENDING"
    assert opportunity["account_plan_id"] == pending[0]["plan_id"]
    assert ledger["intents"] == []
    assert any(row["ticker"] == "NVDA" and row["kind"] == "REQUALIFY"
               for row in output.result.raw["lifecycle_observation"]["alerts"])
    result = read_json_strict(output.report_paths["json"])
    admission = {"clock": {"run_date": execution.run_date.isoformat(),
        "market_timezone": "America/New_York", "admitted_at_utc": execution.admitted_at_utc,
        "deadline_at_utc": "2026-10-20T14:25:00+00:00"}}
    assert verify_required_model_v2(root, result, admission) == result["raw"]["model"]
    assert denied == []
