"""MC017: USD capital must never be combined with unconverted native prices.

These tests use fixed local fixtures: no broker, FX feed, market-data network or LLM.
"""

from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.data_models import MRSResult, PipelineResult, SectorScore, StockCandidate, TradePick
from trading_system.journal import Journal
from trading_system.markets import get_market
from trading_system.simulator import Bar, SimEngine


def _pick(ticker="NVDA"):
    return TradePick(ticker=ticker, tss_final=8.0, tos=5.12, entry_template="A",
                     entry_price=100.0, stop_price=95.0, shares=160,
                     position_pct=0.16, risk_usd=800.0, chain="semis", sector="SMH")


def _mrs():
    return MRSResult(mrs_raw=8.0, delta=2.0, k=1.0, mrs_star=8.0,
                     regime="fixture", position_cap=(0.7, 0.9), allow_new_positions=True)


def _result(market="us", picks=()):
    return PipelineResult(trade_date="2026-08-28", provider="fixture", mrs=_mrs(),
                          action="BUY" if picks else "WAIT", picks=list(picks),
                          raw={"market": get_market(market).to_dict(), "account_usd": 100_000})


def _risk_context(market="us", ticker="NVDA", *, allowed=True):
    candidate = StockCandidate(ticker=ticker, chain_id="semis", price=100.0,
                               stop_price=95.0, key_level=99.0, tss_final=8.0,
                               tss=8.0, c_liq=1.0, atr_pct=0.03, entry_template="A")
    mrs = _mrs()
    mrs.allow_new_positions = allowed
    return {"mrs": mrs, "sectors": [SectorScore(etf="SMH", shs=8.0, breadth=70.0,
                                               in_main_pool=True)],
            "watchlist": [candidate], "chain_map": {}, "market_spec": get_market(market),
            "market_grad": {"graduated": True, "settled": 50, "required": 50},
            "market_data": {"stock_ohlcv": {}}, "trade_date": "2026-08-28"}


@pytest.mark.parametrize(("market", "ticker", "currency"), [
    ("cn", "600519.SS", "CNY"), ("hk", "0700.HK", "HKD"),
])
def test_risk_rejects_native_price_division_but_preserves_research(market, ticker, currency):
    context = _risk_context(market, ticker)
    before = deepcopy(context["watchlist"][0])
    picks = RiskManagerAgent(None, account_usd=100_000).execute(context)

    # The broken code emits 160 shares = 800 USD / 5 native currency units.
    assert picks == [], "unconverted native price gap must not size a USD account"
    assert context["action"] == "WAIT"
    assert context["watchlist"][0].price == before.price
    assert context["watchlist"][0].tss_final == before.tss_final
    assert context["watchlist"][0].tos == 5.12  # same strategy/ranking calculation
    policy = context["currency_policy"]
    assert policy["account_currency"] == "USD" and policy["quote_currency"] == currency
    assert policy["execution_blocked"] is True
    assert currency in context["market_view"] and "USD" in context["market_view"]
    assert "汇率" in context["market_view"]
    assert context["pick_rationale"] == {}


def test_us_sizing_values_are_unchanged():
    context = _risk_context()
    picks = RiskManagerAgent(None, account_usd=100_000).execute(context)
    assert len(picks) == 1
    pick = picks[0]
    assert (pick.shares, pick.entry_price, pick.stop_price, pick.risk_usd,
            pick.position_pct, pick.tos, pick.time_stop_days) == (160, 100, 95, 800, 0.16, 5.12, 7)
    assert context["action"] == "BUY"


@pytest.mark.parametrize("market", ["cn", "hk"])
def test_market_kill_switch_still_precedes_execution(market):
    context = _risk_context(market, allowed=False)
    assert RiskManagerAgent(None).execute(context) == []
    assert context["action"] == "AVOID"
    assert context["mrs"].allow_new_positions is False


@pytest.mark.parametrize("market", ["cn", "hk"])
def test_simulator_rejects_foreign_result_before_any_mutation_or_quote(tmp_path, market):
    path = tmp_path / "sim_portfolio.json"
    sim = SimEngine(str(path))
    sim.save()
    before_file, before_state = path.read_bytes(), deepcopy(sim.state)
    calls = []

    with pytest.raises(ValueError, match="USD.*汇率|汇率.*USD"):
        sim.step("2026-08-28", _result(market), lambda ticker: calls.append(ticker))

    assert sim.state == before_state and path.read_bytes() == before_file
    assert calls == []


@pytest.mark.parametrize("ticker", ["600519.SS", "300750.SZ", "0700.HK"])
def test_simulator_rejects_native_ticker_even_if_market_metadata_is_absent(tmp_path, ticker):
    path = tmp_path / "sim_portfolio.json"
    sim = SimEngine(str(path))
    result = _result(picks=[_pick(ticker)])
    result.raw = {}  # legacy API cannot smuggle native prices under the USD default
    before = deepcopy(sim.state)
    with pytest.raises(ValueError, match="USD.*汇率|汇率.*USD"):
        sim.step("2026-08-28", result, lambda _: None)
    assert not path.exists() and sim.state == before


@pytest.mark.parametrize("bucket", ["positions", "pending", "closed"])
def test_historical_foreign_simulator_rows_are_preserved_and_refused(tmp_path, bucket):
    path = tmp_path / "sim_portfolio.json"
    state = deepcopy(SimEngine(str(path)).state)
    state.pop("account_currency", None)  # original ledger schema has no currency field
    state[bucket] = [{"ticker": "0700.HK", "entry_price": 100.0, "shares": 160}]
    path.write_text(json.dumps(state, indent=3), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="USD.*汇率|汇率.*USD"):
        SimEngine(str(path))
    assert path.read_bytes() == before


def test_explicit_foreign_account_ledger_is_not_relabeled(tmp_path):
    path = tmp_path / "sim_portfolio.json"
    state = deepcopy(SimEngine(str(path)).state)
    state["account_currency"] = "HKD"
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="USD"):
        SimEngine(str(path))
    assert path.read_bytes() == before


def test_legacy_usd_ledger_arithmetic_and_idempotency_are_unchanged(tmp_path):
    path = tmp_path / "sim_portfolio.json"
    sim = SimEngine(str(path))
    sim.state.pop("account_currency", None)
    sim.save()
    sim = SimEngine(str(path))
    sim.step("2026-08-28", _result(picks=[_pick()]), lambda _: None)
    bar = Bar(open=100, high=102, low=98, close=101, date="2026-08-31")
    out = sim.step("2026-08-31", _result(), lambda _: bar)
    assert sim.state["cash"] == 83_984.0  # 100000 - 160 * (100 * 1.001)
    assert out["equity"] == 100_144.0
    assert sim.state["positions"][0]["shares"] == 160
    before = path.read_bytes()
    sim.step("2026-08-31", _result(), lambda _: bar)
    assert sim.state["cash"] == 83_984.0 and len(sim.state["positions"]) == 1
    assert len(sim.state["equity_curve"]) == 2
    # New metadata must not silently claim a migrated currency for a legacy ledger.
    assert "account_currency" not in sim.state
    assert json.loads(before)["cash"] == sim.state["cash"]


@pytest.mark.parametrize(("market", "ticker"), [("cn", "600519.SS"), ("hk", "0700.HK")])
def test_journal_public_entry_refuses_native_signals_without_writing(tmp_path, market, ticker):
    path = tmp_path / "journal.json"
    journal = Journal(path)
    with pytest.raises(ValueError, match="USD.*汇率|汇率.*USD"):
        journal.log_picks(_result(market, [_pick(ticker)]))
    assert not path.exists() and journal.records == []


def test_historical_native_journal_is_preserved(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text('[{"date":"2026-08-28","ticker":"0700.HK","status":"open"}]',
                    encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="USD.*汇率|汇率.*USD"):
        Journal(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(("market", "currency"), [("cn", "CNY"), ("hk", "HKD")])
def test_trade_sentence_uses_native_currency_without_relabeling_risk(market, currency):
    from trading_system.report_html import _decision_card
    result = _result(market, [_pick()])
    rendered = _decision_card(result, result.picks[0])
    assert f"{currency} 100.00 附近买" in rendered
    assert f"{currency} 95.00" in rendered
    assert "$100.00 附近买" not in rendered
    assert "$800" in rendered  # risk_usd stays USD; never multiply by a guessed FX rate


@pytest.mark.parametrize("market", ["cn", "hk"])
def test_daily_keeps_research_success_but_never_writes_accounting(tmp_path, monkeypatch, market):
    import main as cli
    import trading_system.providers as providers
    import trading_system.state as state

    result = _result(market)
    result.watchlist = [StockCandidate(ticker="0700.HK" if market == "hk" else "600519.SS",
                                      price=100, stop_price=95, tss_final=8)]
    monkeypatch.setattr(cli, "run_pipeline", lambda **_: result)
    monkeypatch.setattr(providers, "get_provider", lambda *_: SimpleNamespace(synthetic=False))
    monkeypatch.setattr(state, "purge_run_state", lambda: {})
    args = SimpleNamespace(out=str(tmp_path), universe="core", universe_file=None,
                           top=10, picks=3, account=100_000, use_tuned=False,
                           market=market, demo=False, mode="daily", html=True, quiet=True)
    cli._daily(args, "fixture")

    assert (tmp_path / "result_20260828.json").exists()
    assert (tmp_path / "日报_20260828.md").exists()
    html = (tmp_path / "日报_20260828.html").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "result_20260828.json").read_text(encoding="utf-8"))
    assert saved["raw"]["accounting"]["status"] == "blocked_currency"
    assert saved["raw"]["accounting"]["execution_blocked"] is True
    assert "汇率" in html and "USD" in html
    for filename in ("journal.json", "sim_portfolio.json", "governance_events.jsonl"):
        assert not (tmp_path / filename).exists()
    assert result.watchlist[0].price == 100


@pytest.mark.parametrize("market", [
    {}, {"market_id": "us", "currency": ""},
    {"market_id": "us", "currency": "HKD"},
    {"market_id": "cn", "currency": "USD"},
    {"market_id": "unknown", "currency": "USD"},
])
def test_explicit_ambiguous_or_conflicting_market_cannot_use_legacy_usd_default(tmp_path, market):
    result = _result(picks=[_pick()])
    result.raw["market"] = market
    sim = SimEngine(str(tmp_path / "sim.json"))
    before = deepcopy(sim.state)
    with pytest.raises(ValueError, match="USD"):
        sim.step("2026-08-28", result, lambda _: None)
    assert sim.state == before
    journal = Journal(tmp_path / "journal.json")
    with pytest.raises(ValueError, match="USD"):
        journal.log_picks(result)
    assert journal.records == [] and not journal.path.exists()


@pytest.mark.parametrize("declaration", [
    {"account_currency": "HKD"},
    {"currency_policy": {"quote_currency": "CNY", "execution_blocked": False}},
    {"currency_policy": {"account_currency": "HKD", "execution_blocked": False}},
    {"currency_policy": {"execution_blocked": True, "reason": "币种待核实"}},
    {"accounting": {"execution_blocked": True, "reason": "旧账本币种待核实"}},
])
def test_explicit_currency_declarations_and_blocked_receipts_cannot_be_ignored(tmp_path, declaration):
    result = _result(picks=[_pick()])
    result.raw.update(declaration)
    with pytest.raises(ValueError, match="USD"):
        SimEngine(str(tmp_path / "sim.json")).step("2026-08-28", result, lambda _: None)
    with pytest.raises(ValueError, match="USD"):
        Journal(tmp_path / "journal.json").log_picks(result)
    assert not (tmp_path / "sim.json").exists() and not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("operation", ["save", "stats", "settle", "log_picks"])
def test_mutated_journal_cannot_bypass_public_currency_boundary(tmp_path, operation):
    path = tmp_path / "journal.json"
    journal = Journal(path)
    journal.save()
    before_file = path.read_bytes()
    journal.records.append({"date": "2026-08-28", "ticker": "0700.HK", "status": "open"})
    before_records = deepcopy(journal.records)
    provider = SimpleNamespace(ohlcv=lambda *_args, **_kwargs: pytest.fail("must reject before provider"))
    with pytest.raises(ValueError, match="USD"):
        if operation == "settle":
            journal.settle(provider)
        elif operation == "log_picks":
            journal.log_picks(_result(picks=[_pick()]))
        else:
            getattr(journal, operation)()
    assert journal.records == before_records and path.read_bytes() == before_file


@pytest.mark.parametrize("operation", ["save", "stats", "step"])
def test_mutated_simulator_cannot_bypass_public_currency_boundary(tmp_path, operation):
    path = tmp_path / "sim.json"
    sim = SimEngine(str(path))
    sim.save()
    before_file = path.read_bytes()
    sim.state["pending"].append({"ticker": "600519.SS"})
    before_state = deepcopy(sim.state)
    with pytest.raises(ValueError, match="USD"):
        if operation == "step":
            sim.step("2026-08-28", _result(), lambda _: pytest.fail("must reject before provider"))
        else:
            getattr(sim, operation)()
    assert sim.state == before_state and path.read_bytes() == before_file


@pytest.mark.parametrize("declaration", [
    {"account_currency": "CNY"}, {"quote_currency": "HKD"},
    {"market_id": "cn"}, {"market": {"market_id": "hk", "currency": "HKD"}},
])
def test_explicit_foreign_journal_metadata_is_preserved_and_refused(tmp_path, declaration):
    path = tmp_path / "journal.json"
    path.write_text(json.dumps([{"date": "2026-08-28", "ticker": "NVDA", "status": "open",
                                **declaration}]), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="USD"):
        Journal(path)
    assert path.read_bytes() == before


def test_direct_result_currency_declaration_cannot_be_ignored(tmp_path):
    result = _result(picks=[_pick()])
    result.account_currency = "HKD"
    with pytest.raises(ValueError, match="USD"):
        SimEngine(str(tmp_path / "sim.json")).step("2026-08-28", result, lambda _: None)
    with pytest.raises(ValueError, match="USD"):
        Journal(tmp_path / "journal.json").log_picks(result)
    assert not (tmp_path / "sim.json").exists() and not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("declaration", [
    {"currency_policy": {"quote_currency": "HKD", "execution_blocked": False}},
    {"market": get_market("us").to_dict(), "market_id": "hk"},
])
def test_nested_or_conflicting_ledger_declarations_are_not_ignored(tmp_path, declaration):
    path = tmp_path / "sim.json"
    state = deepcopy(SimEngine(str(path)).state)
    state.update(declaration)
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="USD"):
        SimEngine(str(path))
    assert path.read_bytes() == before


def test_fresh_usd_accounting_records_declare_units(tmp_path):
    sim = SimEngine(str(tmp_path / "sim.json"))
    assert sim.state["account_currency"] == "USD"
    journal = Journal(tmp_path / "journal.json")
    journal.log_picks(_result(picks=[_pick()]))
    assert journal.records[0]["account_currency"] == "USD"
    assert journal.records[0]["quote_currency"] == "USD"


def test_currency_boundary_does_not_skip_market_compliance_checks():
    import pandas as pd
    context = _risk_context("cn", "600519.SS")
    context["watchlist"][0].price = 110
    context["market_data"]["stock_ohlcv"]["600519.SS"] = pd.DataFrame({"Close": [100, 110]})
    assert RiskManagerAgent(None).execute(context) == []
    assert context["compliance"][0]["rule_id"] == "CN_LIMIT_UP_CHASE"
    assert context["compliance"][0]["allowed"] is False
    assert context["currency_policy"]["execution_blocked"] is True


def test_risk_known_native_ticker_cannot_bypass_via_missing_market_spec():
    context = _risk_context(ticker="0700.HK")
    context.pop("market_spec")
    assert RiskManagerAgent(None).execute(context) == []
    assert context["currency_policy"]["execution_blocked"] is True


@pytest.mark.parametrize("blocked_ledger", ["sim_portfolio.json", "journal.json"])
def test_daily_preflights_both_ledgers_before_mutating_either(tmp_path, monkeypatch, blocked_ledger):
    import main as cli
    import trading_system.providers as providers
    import trading_system.state as state

    result = _result(picks=[_pick()])
    path = tmp_path / blocked_ledger
    if blocked_ledger == "sim_portfolio.json":
        bad = deepcopy(SimEngine(str(path)).state)
        bad["closed"] = [{"ticker": "0700.HK"}]
        path.write_text(json.dumps(bad), encoding="utf-8")
        (tmp_path / "journal.json").write_text("[]", encoding="utf-8")
    else:
        path.write_text('[{"date":"2026-08-28","ticker":"0700.HK","status":"open"}]',
                        encoding="utf-8")
        SimEngine(str(tmp_path / "sim_portfolio.json")).save()
    (tmp_path / "governance_events.jsonl").write_text("preserve-events\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(cli, "run_pipeline", lambda **_: result)
    monkeypatch.setattr(providers, "get_provider", lambda *_: SimpleNamespace(synthetic=False))
    monkeypatch.setattr(state, "purge_run_state", lambda: {})
    args = SimpleNamespace(out=str(tmp_path), universe="core", universe_file=None, top=10,
                           picks=3, account=100_000, use_tuned=False, market="us", demo=False,
                           mode="premarket", html=True, quiet=True)
    cli._daily(args, "fixture")
    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())
    saved = json.loads((tmp_path / "result_20260828.json").read_text(encoding="utf-8"))
    assert saved["raw"]["accounting"]["status"] == "blocked_currency"
    assert saved["picks"] == [] and saved["action"] == "WAIT"
    plan = (tmp_path / "盘前计划_20260828.md").read_text(encoding="utf-8")
    assert "汇率" in plan and "USD" in plan
    assert not (tmp_path / "复盘_20260828.md").exists()


@pytest.mark.parametrize("market", ["cn", "hk"])
def test_pipeline_research_reports_explicit_currency_boundary(market, monkeypatch):
    from trading_system.calibration import CalibrationLayer
    from trading_system.pipeline import run_pipeline
    from trading_system.report import render_markdown
    from trading_system.report_html import render_html

    calls = []
    monkeypatch.setattr(CalibrationLayer, "run", lambda _: calls.append("calibration") or {})
    result = run_pipeline(provider_name="demo", universe_mode="core", market=market,
                          top_n=5, max_picks=3)
    assert result.watchlist and result.mrs is not None
    assert result.raw["account_currency"] == "USD"
    assert result.raw["currency_policy"]["execution_blocked"] is True
    assert result.raw["currency_policy"]["quote_currency"] == get_market(market).currency
    assert result.picks == [] and result.raw["pick_rationale"] == {}
    assert calls == [], "blocked research must not update accounting-derived samples"
    assert "汇率" in render_markdown(result)
    html = render_html(result)
    assert "汇率" in html and "USD" in html
    assert "既有持仓继续按出场规则管理" not in html
    assert "既有持仓按出场规则管理" not in html
    assert "0 笔已结算" not in html


@pytest.mark.parametrize("operation", ["collect_samples", "run", "save_samples", "summarize"])
@pytest.mark.parametrize("identity", [{"ticker": "0700.HK"},
                                     {"ticker": "NVDA", "quote_currency": "HKD"}])
def test_native_calibration_input_is_refused_before_derived_history_write(tmp_path, operation, identity):
    from trading_system.calibration import CalibrationLayer

    records = [{"date": "2026-08-28", "status": "closed", "tss_final": 8,
                "mrs_star": 7, "r": 1.0, "win": True, **identity}]
    journal_path, samples_path = tmp_path / "journal.json", tmp_path / "samples.json"
    journal_path.write_text(json.dumps(records), encoding="utf-8")
    samples_path.write_text('[{"ticker":"USD_HISTORY","r":2.0}]', encoding="utf-8")
    before = [journal_path.read_bytes(), samples_path.read_bytes(), deepcopy(records)]
    layer = CalibrationLayer(str(journal_path), str(samples_path))
    with pytest.raises(ValueError, match="USD"):
        if operation == "run":
            layer.run()
        else:
            getattr(layer, operation)(records)
    assert [journal_path.read_bytes(), samples_path.read_bytes(), records] == before


def test_us_pipeline_discloses_native_calibration_history_without_rewriting_it(tmp_path, monkeypatch):
    from trading_system import config
    from trading_system.pipeline import run_pipeline

    journal_path, samples_path = tmp_path / "journal.json", tmp_path / "samples.json"
    journal_path.write_text(json.dumps([{"date": "2026-08-28", "ticker": "0700.HK",
                                        "status": "closed", "r": 1, "tss_final": 8}]),
                            encoding="utf-8")
    samples_path.write_text('[{"ticker":"USD_HISTORY","r":2.0}]', encoding="utf-8")
    before = [journal_path.read_bytes(), samples_path.read_bytes()]
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(samples_path))
    result = run_pipeline(provider_name="demo", universe_mode="core", top_n=5)
    assert result.watchlist and result.raw["currency_policy"]["execution_blocked"] is False
    assert result.raw["calibration"]["status"] == "skipped"
    assert "汇率" in result.raw["calibration"]["reason"]
    assert [journal_path.read_bytes(), samples_path.read_bytes()] == before


@pytest.mark.parametrize("blocked_ledger", ["sim_portfolio.json", "journal.json"])
def test_daily_history_preflight_stops_pipeline_calibration_write(tmp_path, monkeypatch, blocked_ledger):
    import main as cli
    from trading_system import config, state

    journal_path, sim_path = tmp_path / "journal.json", tmp_path / "sim_portfolio.json"
    samples_path = tmp_path / "samples.json"
    journal_path.write_text("[]", encoding="utf-8")
    sim = SimEngine(str(sim_path))
    sim.save()
    if blocked_ledger == "journal.json":
        journal_path.write_text(json.dumps([{"date": "2026-08-28", "ticker": "0700.HK",
                                            "status": "closed", "r": 1.0, "source": "demo"}]),
                                encoding="utf-8")
    else:
        state_data = deepcopy(sim.state)
        state_data["closed"] = [{"ticker": "0700.HK", "source": "demo"}]
        sim_path.write_text(json.dumps(state_data), encoding="utf-8")
    samples_path.write_text('[{"ticker":"USD_HISTORY","r":2.0}]', encoding="utf-8")
    before = [p.read_bytes() for p in (journal_path, sim_path, samples_path)]
    monkeypatch.setattr(config, "CALIBRATION_JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(config, "CALIBRATION_SAMPLES_PATH", str(samples_path))
    monkeypatch.setattr(state, "purge_run_state", lambda: {})
    args = SimpleNamespace(out=str(tmp_path), universe="core", universe_file=None, top=5,
                           picks=3, account=100_000, use_tuned=False, market="us", demo=True,
                           mode="daily", html=False, quiet=True)
    cli._daily(args, "demo")
    assert [p.read_bytes() for p in (journal_path, sim_path, samples_path)] == before
    saved = json.loads(next(tmp_path.glob("result_*.json")).read_text())
    assert saved["raw"]["accounting"]["status"] == "blocked_currency"
    assert saved["raw"]["calibration"]["status"] == "skipped"
    assert saved["picks"] == [] and saved["watchlist"]
