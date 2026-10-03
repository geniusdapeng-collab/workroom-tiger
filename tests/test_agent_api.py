"""Failure injection for the actual research facade; no fabricated success."""
import json
import os
from pathlib import Path

import pytest

from trading_system import agent_api as api, config
from trading_system.governance_bridge import FiveElementEvent, GovernanceBridge, RuleImpact


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    # In production this API runs once per child. Restore globals/cwd when its
    # real orchestration is called directly for controlled fault injection.
    monkeypatch.chdir(Path.cwd())
    for name in ("CACHE_DIR", "REPORTS_DIR", "CALIBRATION_SAMPLES_PATH",
                 "CALIBRATION_JOURNAL_PATH", "REVIEW_PROPOSALS_DIR"):
        monkeypatch.setattr(config, name, getattr(config, name))
    directory = tmp_path.resolve()
    kernel = Path(api.__file__).resolve().parent.parent
    workspace = api.workspace_at(str(directory / "workspace"), "local", kernel, create=True)
    return workspace, kernel


def request(key="faulttest001", **extra):
    return api.normalize_request({"operation": "pipeline", "mode": "daily",
                                  "environment": "simulation", "provider": "demo",
                                  "idempotencyKey": key, "topN": 3, "maxPicks": 2, **extra})


PROFILE_LIMITS = {"risk_r_pct": .004, "max_single_position_pct": .10, "gross_cap": .50}


def test_profile_risk_limits_are_normalized_and_part_of_the_bound_input():
    selected = request("profilebounds001", riskLimits=PROFILE_LIMITS)
    assert selected["riskLimits"] == PROFILE_LIMITS
    assert api.digest(api.canonical(selected)) != api.digest(api.canonical(request("profilebounds001")))


@pytest.mark.parametrize("limits", [None, [], {}, {**PROFILE_LIMITS, "unknown": .1},
                                   {**PROFILE_LIMITS, "risk_r_pct": 0},
                                   {**PROFILE_LIMITS, "risk_r_pct": True},
                                   {**PROFILE_LIMITS, "risk_r_pct": float("nan")},
                                   {**PROFILE_LIMITS, "risk_r_pct": .009},
                                   {**PROFILE_LIMITS, "max_single_position_pct": .21},
                                   {**PROFILE_LIMITS, "gross_cap": .91}])
def test_profile_risk_limits_cannot_be_missing_nonfinite_or_looser_than_the_kernel(limits):
    with pytest.raises(api.AgentError):
        request("profileinvalid001", riskLimits=limits)


def test_profile_risk_limits_bind_actual_pipeline_positions_and_result_sha(isolated):
    workspace, kernel = isolated
    selected = request("profileactual001", account=250_000, topN=10, maxPicks=3, riskLimits=PROFILE_LIMITS)
    receipt = api.run(selected, workspace, "local", kernel)
    assert receipt["status"] == "degraded", receipt.get("error")
    assert receipt["requestedRiskLimits"] == PROFILE_LIMITS
    artifact = next(item for item in receipt["artifacts"] if item["role"] == "pipeline-result")
    assert receipt["resultSha256"] == artifact["sha256"]
    result = api.read_json(api.artifact_path(api.job_at(workspace, "local", receipt["jobId"]), artifact["name"]))
    assert receipt["riskLimits"] == result["raw"]["risk_limits"]
    assert receipt["gateParams"] == result["raw"]["gate_params"]
    for name, limit in PROFILE_LIMITS.items():
        assert 0 < receipt["riskLimits"][name] <= limit
    assert result["picks"], "This fixture must exercise actual nonzero position sizing"
    for pick in result["picks"]:
        assert pick["shares"] * pick["entry_price"] <= selected["account"] * PROFILE_LIMITS["max_single_position_pct"] + 1e-8
        assert pick["risk_usd"] <= selected["account"] * PROFILE_LIMITS["risk_r_pct"] + 1e-8
    assert sum(pick["shares"] * pick["entry_price"] for pick in result["picks"]) <= selected["account"] * PROFILE_LIMITS["gross_cap"] + 1e-8
    # Even if the local completion checksum is refreshed, a receipt claiming
    # different risk limits must not validate against the actual result.
    job = api.job_at(workspace, "local", receipt["jobId"])
    original_receipt, original_completion = (job / "receipt.json").read_bytes(), (job / "completion.json").read_bytes()
    try:
        altered = dict(receipt)
        altered["riskLimits"] = {**receipt["riskLimits"], "gross_cap": receipt["riskLimits"]["gross_cap"] / 2}
        api.write_atomic(job / "receipt.json", altered)
        api.write_atomic(job / "completion.json", {"schemaVersion": "tiger.agent-completion/v1",
                                                   "receiptSha256": api.digest((job / "receipt.json").read_bytes())})
        with pytest.raises(api.AgentError, match="risk or result binding"):
            api.verified_job(job)
    finally:
        (job / "receipt.json").write_bytes(original_receipt)
        (job / "completion.json").write_bytes(original_completion)
    assert api.verified_job(job)["riskLimits"] == receipt["riskLimits"]


@pytest.mark.parametrize("fault", ["missing-limit", "missing-parameter", "above-request", "parameter-mismatch"])
def test_incomplete_or_unbound_actual_risk_snapshot_fails_closed(fault):
    from trading_system.parameters import GateParams
    selected = request("snapshotguard001", riskLimits=PROFILE_LIMITS)
    limits, params = dict(PROFILE_LIMITS), api.serializable(GateParams(max_picks=2, **PROFILE_LIMITS))
    if fault == "missing-limit":
        limits.pop("gross_cap")
    elif fault == "missing-parameter":
        params.pop("cost_bps")
    elif fault == "above-request":
        limits["risk_r_pct"] *= 2
        params["risk_r_pct"] *= 2
    else:
        params["gross_cap"] /= 2
    with pytest.raises(api.AgentError, match="snapshots"):
        api.risk_snapshot(selected, limits, params)


def event(bridge, action="agent.completed"):
    result = bridge.emit(FiveElementEvent(
        who={"type": "agent", "id": "task-owned-fault-fixture"},
        context={"channel": "test", "stage": "simulation"},
        object={"type": "report", "id": "fixture"},
        decision={"action": action, "basis": ["Controlled real ledger event"]},
        rule_impact=[RuleImpact("R-T15", result="pass")]))
    assert result is not None


def test_disabled_model_never_constructs_default_client(isolated, monkeypatch):
    from trading_system import pipeline

    def forbidden():
        pytest.fail("disabled model must not discover SDK, keyfile or local endpoint")

    monkeypatch.setattr(pipeline, "default_client", forbidden)
    workspace, kernel = isolated
    receipt = api.run(request(), workspace, "local", kernel)
    assert receipt["status"] == "degraded"
    assert receipt["receipt"]["synced"] is True
    assert len(receipt["stepTrace"]) == 21
    assert any("disabled by this research request" in step.get("note", "") for step in receipt["stepTrace"])


def test_configured_but_missing_endpoint_is_degraded_without_host_discovery(isolated, monkeypatch):
    from trading_system import pipeline
    for name in list(os.environ):
        if name.startswith(("LLM_", "OPENAI_", "KIMI_")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(pipeline, "default_client", lambda: pytest.fail("configured launcher must use only explicit endpoint settings"))
    workspace, kernel = isolated
    receipt = api.run(request("missingmodel001", llmMode="configured"), workspace, "local", kernel)
    assert receipt["status"] == "degraded"
    assert any("Explicit model endpoint" in step.get("note", "") for step in receipt["stepTrace"])


def test_explicit_local_model_does_not_inherit_another_provider_credential(monkeypatch):
    import requests
    from trading_system.redline import LLMUnavailable
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.setenv("LLM_LOCAL_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("LLM_LOCAL_MODEL", "task-owned-explicit-model")
    monkeypatch.delenv("LLM_LOCAL_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "task-owned-other-provider-secret")
    calls = []

    def failing_endpoint(url, **kwargs):
        calls.append((url, kwargs))
        return type("Response", (), {"status_code": 503, "text": "task-owned failure"})()

    monkeypatch.setattr(requests, "post", failing_endpoint)
    client = api.ConfiguredModelClient()
    with pytest.raises(LLMUnavailable):
        client.complete_json(system="fixture", user="fixture", schema_hint={})
    assert calls[0][0] == "http://127.0.0.1:9999/v1/chat/completions"
    assert "Authorization" not in calls[0][1]["headers"]
    assert "task-owned-other-provider-secret" not in json.dumps(calls)


def test_missing_signal_journal_cannot_claim_complete_pipeline(isolated, monkeypatch):
    from trading_system.journal import Journal
    monkeypatch.setattr(Journal, "_save_locked", lambda self: None)
    workspace, kernel = isolated
    receipt = api.run(request("missingjournal001"), workspace, "local", kernel)
    assert receipt["status"] == "failed"
    assert receipt["integrityVerified"] is False
    assert receipt["receipt"]["synced"] is False
    assert receipt["error"]["code"] == "INCOMPLETE_ARTIFACTS"


def test_report_write_failure_is_failed_and_does_not_echo_secret(isolated, monkeypatch):
    from trading_system import report_html
    canary = "task-owned-render-secret-912741"

    def failing_renderer(*args, **kwargs):
        raise OSError(f"Injected disk failure echoed {canary}")

    monkeypatch.setattr(report_html, "render_html", failing_renderer)
    workspace, kernel = isolated
    receipt = api.run(request("reportfailure001"), workspace, "local", kernel)
    assert receipt["status"] == "failed"
    assert receipt["integrityVerified"] is False
    assert canary not in json.dumps(receipt)
    assert api.verified_job(api.job_at(workspace, "local", receipt["jobId"]))["status"] == "failed"


def test_governance_write_failure_is_terminal_failed(isolated, monkeypatch):
    monkeypatch.setattr(GovernanceBridge, "emit", lambda self, event: None)
    workspace, kernel = isolated
    receipt = api.run(request("eventfailure001"), workspace, "local", kernel)
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "GOVERNANCE_WRITE_FAILED"
    assert receipt["receipt"]["synced"] is False


def test_broken_original_governance_chain_is_not_repaired(tmp_path):
    artifacts = tmp_path.resolve() / "artifacts"
    api.private_mkdir(artifacts)
    ledger = artifacts / "governance_events.jsonl"
    event(GovernanceBridge(str(ledger)))
    record = json.loads(ledger.read_text().splitlines()[0])
    record["payload"]["decision"]["action"] = "tampered-action"
    original_bad = json.dumps(record) + "\n"
    ledger.write_text(original_bad)
    with pytest.raises(api.AgentError, match="Original governance"):
        api.finish_artifacts(artifacts, ["agent.completed"])
    assert ledger.read_text() == original_bad


def test_missing_required_governance_operation_is_rejected(tmp_path):
    artifacts = tmp_path.resolve() / "artifacts"
    api.private_mkdir(artifacts)
    event(GovernanceBridge(str(artifacts / "governance_events.jsonl")))
    with pytest.raises(api.AgentError, match="required operation"):
        api.finish_artifacts(artifacts, ["agent.completed", "review.daily"])


def test_artifact_symlink_and_traversal_are_rejected(tmp_path):
    directory = tmp_path.resolve()
    job = directory / "job"
    api.private_mkdir(job / "artifacts")
    outside = directory / "outside.json"
    outside.write_text('{"taskOwned":true}')
    (job / "artifacts" / "escape.json").symlink_to(outside)
    with pytest.raises(api.AgentError, match="Symbolic links"):
        api.artifact_path(job, "escape.json")
    for name in ("../outside.json", "/outside.json", "C:\\outside.json", "folder//file.json"):
        with pytest.raises(api.AgentError):
            api.artifact_path(job, name)


def test_interrupt_cannot_finalize_another_process_job(isolated):
    workspace, kernel = isolated
    normalized = request("ownership001")
    job = api.job_at(workspace, "local", normalized["idempotencyKey"])
    api.private_mkdir(job)
    api.write_atomic(job / "owner.json", {"pid": os.getpid(), "startedAt": api.now(),
                                         "inputSha256": api.digest(api.canonical(normalized))})
    with pytest.raises(api.AgentError, match="owner did not match"):
        api.interrupt(normalized, workspace, "local", os.getpid() + 100_000, "cancelled")
    assert not (job / "completion.json").exists()


@pytest.mark.parametrize("extra", [
    {"environment": "live"}, {"approve": True}, {"apply": True}, {"tenantId": "other"},
    {"out": "reports"}, {"command": "python"}, {"provider": None}, {"topN": True},
    {"account": float("nan")}, {"account": float("inf")}, {"idempotencyKey": "../escape"},
    {"mode": "review-approve"}, {"mode": "backtest", "market": "hk"},
])
def test_python_boundary_rejects_unsafe_requests(extra):
    with pytest.raises(api.AgentError):
        request(**extra)


def test_nonempty_workspace_preserves_original_ledger(tmp_path):
    directory = tmp_path.resolve() / "existing"
    directory.mkdir()
    ledger = directory / "journal.json"
    original = '[{"customer":"task-owned-marker"}]\n'
    ledger.write_text(original)
    kernel = Path(api.__file__).resolve().parent.parent
    with pytest.raises(api.AgentError, match="empty dedicated"):
        api.workspace_at(str(directory), "local", kernel, create=True)
    assert ledger.read_text() == original
    assert not (directory / ".tiger-agent-workspace.json").exists()


@pytest.mark.parametrize("fault", ["old-benchmark", "invalid-ohlc", "duplicate-date", "missing-rate"])
def test_directed_employee_refuses_invalid_or_stale_baselines(fault):
    import pandas as pd
    from trading_system.markets import get_market
    from trading_system.providers.demo import DemoProvider
    provider = DemoProvider()
    benchmark = provider.ohlcv("SPY", 420)
    stocks = {"AAPL": provider.ohlcv("AAPL", 420)}
    macro = {"tnx": provider.tnx_yield(420), "vix": provider.vix(420), "vix9d": provider.vix9d(420)}
    if fault == "old-benchmark":
        benchmark.index -= pd.Timedelta(days=20)
    elif fault == "invalid-ohlc":
        benchmark.loc[benchmark.index[-2], "High"] = benchmark["Low"].iloc[-2] / 2
    elif fault == "duplicate-date":
        benchmark.index = list(benchmark.index[:-1]) + [benchmark.index[-2]]
    else:
        macro["tnx"] = None
    with pytest.raises(api.AgentError):
        api.guard_employee_data(get_market("us"), benchmark, stocks, macro)


def test_employee_filters_stale_stocks_future_bars_and_missing_optional_macro():
    import pandas as pd
    from trading_system.markets import get_market
    from trading_system.providers.demo import DemoProvider
    provider = DemoProvider()
    fresh = provider.ohlcv("AAPL", 420)
    stale = provider.ohlcv("MSFT", 420)
    stale.index -= pd.Timedelta(days=30)
    future = fresh.tail(1).copy()
    future.index += pd.Timedelta(days=365)
    benchmark = pd.concat([provider.ohlcv("000300.SS", 420), future])
    macro = {"tnx": None, "vix": None, "vix9d": None}
    clean_benchmark, stocks, clean_macro, report = api.guard_employee_data(
        get_market("cn"), benchmark, {"AAPL": pd.concat([fresh, future]), "MSFT": stale}, macro)
    assert list(stocks) == ["AAPL"]
    assert stocks["AAPL"].index.max() <= pd.Timestamp(report["referenceDate"])
    assert clean_benchmark.index.max() <= pd.Timestamp(report["referenceDate"])
    assert report["removedStocks"] == {"MSFT": "stale-bars"}
    assert report["missingMacro"] == ["tnx", "vix", "vix9d"]
    assert clean_macro == {"tnx": None, "vix": None, "vix9d": None}


def test_risk_employee_refetch_cannot_reintroduce_future_bars(isolated, monkeypatch):
    import pandas as pd
    from trading_system import pipeline
    from trading_system.agents.risk_manager_agent import RiskManagerAgent
    original_batch, original_execute = pipeline._batch_with_fallback, RiskManagerAgent.execute
    observed = []

    def future_after_pipeline(provider, tickers, days, label="batch"):
        data = original_batch(provider, tickers, days, label=label)
        if pipeline._RUN_DIAGNOSTICS.get() is None:
            altered = {}
            for ticker, frame in data.items():
                first, second = frame.tail(1).copy(), frame.tail(1).copy()
                first.index += pd.Timedelta(days=364)
                second.index += pd.Timedelta(days=365)
                altered[ticker] = pd.concat([frame, first, second])
            return altered
        return data

    def actual_risk(self, context):
        if pipeline._RUN_DIAGNOSTICS.get() is None:
            observed.append((context["trade_date"], context["market_data"]["stock_ohlcv"]))
        return original_execute(self, context)

    monkeypatch.setattr(pipeline, "_batch_with_fallback", future_after_pipeline)
    monkeypatch.setattr(RiskManagerAgent, "execute", actual_risk)
    workspace, kernel = isolated
    selected = api.normalize_request({"operation": "employee", "employee": "risk",
                                      "environment": "simulation", "idempotencyKey": "riskfuture001",
                                      "provider": "demo", "topN": 3, "maxPicks": 2})
    receipt = api.run(selected, workspace, "local", kernel)
    assert receipt["status"] == "degraded", receipt.get("error")
    assert observed
    for trade_date, frames in observed:
        assert frames
        assert all(frame.index.max().date() <= pd.Timestamp(trade_date).date() for frame in frames.values())


@pytest.fixture(scope="module")
def quote_source(tmp_path_factory):
    """Use an actual daily job and its published SHA for every quote fault."""
    patch = pytest.MonkeyPatch()
    patch.chdir(Path.cwd())
    for name in ("CACHE_DIR", "REPORTS_DIR", "CALIBRATION_SAMPLES_PATH",
                 "CALIBRATION_JOURNAL_PATH", "REVIEW_PROPOSALS_DIR"):
        patch.setattr(config, name, getattr(config, name))
    kernel = Path(api.__file__).resolve().parent.parent
    workspace = api.workspace_at(str(tmp_path_factory.mktemp("quote-source").resolve() / "workspace"),
                                 "local", kernel, create=True)
    try:
        receipt = api.run(request("quotesource001", topN=10, maxPicks=3), workspace, "local", kernel)
        assert receipt["status"] == "degraded", receipt.get("error")
        result = next(item for item in receipt["artifacts"] if item["role"] == "pipeline-result")
        blob = api.read_json(api.artifact_path(api.job_at(workspace, "local", receipt["jobId"]), result["name"]))
        assert len(blob["picks"]) >= 2, "Partial quote coverage requires two actual released picks"
        yield workspace, kernel, {"jobId": receipt["jobId"], "resultSha256": result["sha256"]}, blob
    finally:
        patch.undo()


@pytest.mark.parametrize("fault", ["missing", "stale", "invalid-price", "missing-metadata", "old-realtime"])
def test_intraday_cannot_succeed_when_every_actual_watch_quote_is_unusable(quote_source, monkeypatch, fault):
    from datetime import datetime, timedelta, timezone
    from trading_system import pipeline
    workspace, kernel, source, blob = quote_source

    def quote(provider, ticker):
        pick = next(item for item in blob["picks"] if item["ticker"] == ticker)
        value = {"price": pick["entry_price"] + (pick["entry_price"] - pick["stop_price"]),
                 "ts": datetime.now(timezone.utc).isoformat(), "kind": "realtime"}
        if fault == "missing":
            return None
        if fault == "stale":
            value["stale"] = True
        elif fault == "invalid-price":
            value["price"] = float("nan")
        elif fault == "missing-metadata":
            value.pop("ts")
        else:
            value["ts"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        return value

    monkeypatch.setattr(pipeline, "quote_with_fallback", quote)
    receipt = api.run(request(f"quote{fault.replace('-', '')}001", mode="intraday", sourceJob=source),
                      workspace, "local", kernel)
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "QUOTE_DATA_MISSING"
    assert receipt["integrityVerified"] is False


@pytest.mark.parametrize("partial", [False, True])
def test_intraday_distinguishes_healthy_zero_alerts_from_partial_coverage(quote_source, monkeypatch, partial):
    from datetime import datetime, timezone
    from trading_system import pipeline
    workspace, kernel, source, blob = quote_source
    ready = blob["picks"][0]["ticker"]

    def quote(provider, ticker):
        if partial and ticker != ready:
            return None
        pick = next(item for item in blob["picks"] if item["ticker"] == ticker)
        return {"price": pick["entry_price"] + (pick["entry_price"] - pick["stop_price"]),
                "ts": datetime.now(timezone.utc).isoformat(), "kind": "realtime"}

    monkeypatch.setattr(pipeline, "quote_with_fallback", quote)
    receipt = api.run(request(f"quotecoverage{int(partial)}001", mode="intraday", sourceJob=source),
                      workspace, "local", kernel)
    assert receipt["status"] == ("degraded" if partial else "succeeded")
    path = next(item["name"] for item in receipt["artifacts"] if item["name"] == "intraday.json")
    data = api.read_json(api.artifact_path(api.job_at(workspace, "local", receipt["jobId"]), path))
    assert data["alerts"] == []
    assert data["quoteCoverage"]["requested"] == len(blob["picks"])
    assert data["quoteCoverage"]["ready"] == (1 if partial else len(blob["picks"]))
    assert data["quoteCoverage"]["missing"] == (len(blob["picks"]) - 1 if partial else 0)
    assert receipt["summary"]["quoteCoverage"] == data["quoteCoverage"]


def test_source_with_looser_risk_limits_cannot_be_reused_under_a_stricter_profile(quote_source):
    workspace, kernel, source, _ = quote_source
    selected = request("sourcestrict001", mode="intraday", sourceJob=source, riskLimits=PROFILE_LIMITS)
    receipt = api.run(selected, workspace, "local", kernel)
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "RISK_LIMIT_MISMATCH"
    assert receipt["receipt"]["synced"] is False
