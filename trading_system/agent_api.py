"""Tiger-owned research API for the JSON CLI and stdio MCP launcher.

The launcher chooses the workspace and tenant. Requests choose only bounded
research inputs; they cannot choose commands, paths, approval or live trading.
Each idempotency key owns a fresh kernel working directory and accounting state.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import stat
import sys
import time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "tiger.agent-receipt/v1"
WORKSPACE_SCHEMA = "tiger.agent-workspace/v1"
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}\Z")
TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
MAX_INPUT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MODES = ("daily", "premarket", "intraday", "backtest", "tune", "review")
EMPLOYEES = ("scanner", "mrs", "risk", "review")
TERMINAL = {"succeeded", "degraded", "failed", "timed_out", "cancelled"}


class AgentError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


class DisabledModelClient:
    """An explicitly disabled model uses the kernel's honest passthrough path."""

    def describe(self) -> str:
        return "disabled-by-research-request"

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int = 1200, temperature: float = 0.2) -> dict:
        from .redline import LLMUnavailable
        raise LLMUnavailable("Model calls disabled by this research request")


class ConfiguredModelClient:
    """Use only an explicit launcher endpoint; never discover host keyfiles."""

    def __init__(self):
        backend = os.environ.get("LLM_BACKEND", "auto").lower()
        settings = []
        if backend in ("api", "auto"):
            settings.append((os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"),
                             os.environ.get("LLM_API_KEY", ""), "api"))
        if backend in ("local", "auto"):
            settings.append((os.environ.get("LLM_LOCAL_URL"), os.environ.get("LLM_LOCAL_MODEL"),
                             os.environ.get("LLM_LOCAL_API_KEY", ""), "local"))
        if backend in ("api", "local", "auto"):
            settings.append((os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_MODEL"),
                             os.environ.get("OPENAI_API_KEY", ""), "explicit-openai"))
        self.settings = next((item for item in settings if item[0] and item[1]), None)
        self._client = None

    def describe(self) -> str:
        return f"explicit-configured:{self.settings[3] if self.settings else 'unavailable'}"

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int = 1200, temperature: float = 0.2) -> dict:
        from .llm.client import OpenAICompatClient
        from .redline import LLMUnavailable
        if self.settings is None:
            raise LLMUnavailable("Explicit model endpoint and model are not configured for this research launcher")
        if self._client is None:
            base, model, key, name = self.settings
            self._client = OpenAICompatClient(base_url=base, model=model, api_key=key, name=name)
            # The legacy constructor falls back to LLM_API_KEY for an empty
            # argument. An explicit local/OPENAI endpoint must not inherit a
            # credential belonging to a different configured provider.
            self._client.api_key = key
        return self._client.complete_json(system=system, user=user, schema_hint=schema_hint,
                                          max_tokens=max_tokens, temperature=temperature)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def risk_maxima() -> dict:
    from . import config
    return {"risk_r_pct": config.RISK_R_PCT,
            "max_single_position_pct": config.MAX_SINGLE_POSITION_PCT,
            "gross_cap": max(row[3] for row in config.MRS_POSITION_CAP)}


def validate_risk_limits(value: Any) -> dict:
    maxima = risk_maxima()
    if not isinstance(value, dict) or set(value) != set(maxima):
        raise AgentError("INVALID_INPUT", "riskLimits requires exactly risk_r_pct, max_single_position_pct and gross_cap")
    for name, maximum in maxima.items():
        number = value[name]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not 0 < number <= maximum):
            raise AgentError("INVALID_INPUT", f"riskLimits.{name} must be finite, positive and no greater than the kernel limit")
    return dict(value)


def risk_snapshot(request: dict, limits: Any, params: Any) -> dict:
    """Verify a complete snapshot returned by the actual kernel consumer."""
    from .parameters import GateParams
    try:
        actual = validate_risk_limits(limits)
        snapshot = serializable(params)
        if not isinstance(snapshot, dict) or set(snapshot) != {field.name for field in fields(GateParams)}:
            raise ValueError("Incomplete parameter snapshot")
        GateParams(**snapshot)
        for name, number in actual.items():
            if number > request["riskLimits"][name] or snapshot.get(name) != number:
                raise ValueError("Effective limits do not match the bound request and parameters")
        source_only = request.get("mode") in ("intraday", "review") or request.get("employee") == "review"
        if not source_only and snapshot["max_picks"] > request["maxPicks"]:
            raise ValueError("Effective picks limit exceeds the request")
    except (AgentError, TypeError, ValueError):
        raise AgentError("RISK_LIMIT_MISMATCH", "Actual risk and parameter snapshots are incomplete or exceed the bound request") from None
    return {"riskLimits": actual, "gateParams": snapshot}


def normalize_request(value: Any) -> dict:
    if not isinstance(value, dict):
        raise AgentError("INVALID_INPUT", "Request must be a JSON object")
    required = {"operation", "environment", "idempotencyKey"}
    common = required | {"provider", "market", "universe", "topN", "maxPicks",
                         "account", "timeoutSeconds", "llmMode", "riskLimits"}
    operation = value.get("operation")
    mode = value.get("mode") if operation == "pipeline" else None
    employee = value.get("employee") if operation == "employee" else None
    if operation not in ("pipeline", "employee"):
        raise AgentError("INVALID_INPUT", "operation must be pipeline or employee")
    specific = {"mode"} if operation == "pipeline" else {"employee"}
    if operation == "pipeline" and mode not in MODES:
        raise AgentError("INVALID_INPUT", "Unsupported pipeline mode")
    if operation == "employee" and employee not in EMPLOYEES:
        raise AgentError("INVALID_INPUT", "Unsupported employee")
    if mode in ("backtest", "tune"):
        specific |= {"btDays"}
    if mode == "tune":
        specific |= {"trainDays", "testDays", "stepDays"}
    if mode == "intraday":
        specific |= {"sourceJob", "cycles", "intervalSeconds"}
    if mode == "review" or employee == "review":
        specific |= {"sourceJob", "reviewFrequency"}
    if set(value) - common - specific or not required.issubset(value):
        raise AgentError("INVALID_INPUT", "Unexpected or missing request fields")
    if value["environment"] not in ("simulation", "paper"):
        raise AgentError("INVALID_INPUT", "Only simulation and paper environments are allowed")
    key = value["idempotencyKey"]
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise AgentError("INVALID_INPUT", "idempotencyKey requires 8-80 safe ASCII characters")
    result = {"operation": operation, "environment": value["environment"],
              "idempotencyKey": key, **({"mode": mode} if mode else {"employee": employee})}
    result["riskLimits"] = validate_risk_limits(value.get("riskLimits", risk_maxima()))
    choices = {"provider": ("demo", "yahoo", "stooq", "tencent", "sina", "eastmoney"),
               "market": ("us", "cn", "hk"), "universe": ("core", "extended"),
               "llmMode": ("disabled", "configured")}
    defaults = {"provider": "demo", "market": "us", "universe": "core", "llmMode": "disabled"}
    for name, options in choices.items():
        item = value.get(name, defaults[name])
        if item not in options:
            raise AgentError("INVALID_INPUT", f"Unsupported {name}")
        result[name] = item
    # The existing historical engine is US-only. Do not label US frames CN/HK.
    if mode in ("backtest", "tune") and result["market"] != "us":
        raise AgentError("UNSUPPORTED_MARKET", "Historical research currently supports us only")
    numeric = {"topN": (1, 100, 20, True), "maxPicks": (1, 25, 5, True),
               "account": (100, 100_000_000, 100_000, False),
               "timeoutSeconds": (1, 900, 300, True)}
    if mode in ("backtest", "tune"):
        numeric["btDays"] = (5, 490, 260 if mode == "backtest" else 380, True)
    if mode == "tune":
        numeric |= {"trainDays": (20, 252, 126, True), "testDays": (5, 126, 63, True),
                    "stepDays": (5, 126, 63, True)}
    if mode == "intraday":
        numeric |= {"cycles": (1, 100, 1, True), "intervalSeconds": (0, 3600, 0, True)}
    for name, (low, high, default, integer) in numeric.items():
        item = value.get(name, default)
        if (isinstance(item, bool) or not isinstance(item, (int, float))
                or not math.isfinite(item) or not low <= item <= high
                or (integer and not isinstance(item, int))):
            raise AgentError("INVALID_INPUT", f"{name} is outside the supported finite range")
        result[name] = item
    if mode == "tune" and result["btDays"] < result["trainDays"] + result["testDays"]:
        raise AgentError("INVALID_INPUT", "btDays must cover trainDays plus testDays")
    if mode in ("intraday", "review") or employee == "review":
        source = value.get("sourceJob")
        if (not isinstance(source, dict) or set(source) != {"jobId", "resultSha256"}
                or not isinstance(source["jobId"], str) or not KEY_RE.fullmatch(source["jobId"])
                or not isinstance(source["resultSha256"], str)
                or not SHA_RE.fullmatch(source["resultSha256"])):
            raise AgentError("INVALID_INPUT", "sourceJob requires jobId and resultSha256")
        if source["jobId"] == key:
            raise AgentError("INVALID_INPUT", "A job cannot depend on itself")
        result["sourceJob"] = dict(source)
    if mode == "review" or employee == "review":
        frequency = value.get("reviewFrequency", "daily")
        if frequency not in ("daily", "weekly"):
            raise AgentError("INVALID_INPUT", "reviewFrequency must be daily or weekly")
        result["reviewFrequency"] = frequency
    return result


def checked_path(path: Path, *, must_exist: bool = True) -> Path:
    """Reject symlinks in every component, including the workspace itself."""
    if not path.is_absolute() or ".." in path.parts:
        raise AgentError("UNSAFE_PATH", "An absolute canonical path is required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if must_exist:
                raise AgentError("NOT_FOUND", "Requested workspace or file does not exist") from None
            continue
        if stat.S_ISLNK(info.st_mode):
            raise AgentError("UNSAFE_PATH", "Symbolic links are not allowed in workspace paths")
    return path


def private_mkdir(path: Path) -> None:
    checked_path(path, must_exist=False)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(path)
    if not path.is_dir():
        raise AgentError("UNSAFE_PATH", "Expected a directory")


def write_atomic(path: Path, value: Any, *, text: bool = False) -> None:
    checked_path(path.parent)
    checked_path(path, must_exist=False)
    data = value.encode("utf-8") if text else canonical(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> Any:
    checked_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
        raise AgentError("INTEGRITY_ERROR", "Invalid or oversized JSON file")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: None)
    except (UnicodeError, json.JSONDecodeError):
        raise AgentError("INTEGRITY_ERROR", "JSON artifact cannot be decoded") from None


def workspace_at(raw: str, tenant: str, kernel: Path, *, create: bool) -> Path:
    if not TENANT_RE.fullmatch(tenant):
        raise AgentError("INVALID_TENANT", "Invalid launcher tenant")
    workspace = checked_path(Path(raw), must_exist=not create)
    if workspace == kernel or workspace in kernel.parents:
        raise AgentError("UNSAFE_WORKSPACE", "Kernel directory cannot be used as a workspace")
    if create:
        private_mkdir(workspace)
    marker = workspace / ".tiger-agent-workspace.json"
    expected = {"schemaVersion": WORKSPACE_SCHEMA, "productId": "workroom-tiger",
                "tenantId": tenant, "workspaceId": digest(str(workspace).encode())}
    if marker.exists():
        if read_json(marker) != expected:
            raise AgentError("UNSAFE_WORKSPACE", "Workspace belongs to another launcher identity")
    elif create:
        if list(workspace.iterdir()):
            raise AgentError("UNSAFE_WORKSPACE", "A new workspace must be an empty dedicated directory")
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise AgentError("IN_PROGRESS", "Workspace initialization is in progress") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(expected) + b"\n")
    else:
        raise AgentError("UNSAFE_WORKSPACE", "Workspace has not been initialized")
    return workspace


def job_at(workspace: Path, tenant: str, key: str) -> Path:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise AgentError("INVALID_INPUT", "Invalid jobId")
    return checked_path(workspace / "jobs" / tenant / key, must_exist=False)


def artifact_path(job: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or name.startswith(("/", "\\"))
            or "\\" in name or any(p in ("", ".", "..") for p in name.split("/"))
            or re.match(r"^[A-Za-z]:", name)):
        raise AgentError("INTEGRITY_ERROR", "Unsafe artifact name")
    path = checked_path(job / "artifacts" / name)
    if not path.is_file():
        raise AgentError("INTEGRITY_ERROR", "Artifact is not a regular file")
    return path


def verified_job(job: Path) -> dict:
    checked_path(job)
    if not (job / "completion.json").exists():
        if (job / "receipt.json").exists():
            raise AgentError("INTEGRITY_ERROR", "Terminal receipt is missing its completion checksum")
        if (job / "owner.json").exists():
            owner = read_json(job / "owner.json")
            if (not isinstance(owner, dict) or isinstance(owner.get("pid"), bool)
                    or not isinstance(owner.get("pid"), int) or owner["pid"] <= 0):
                raise AgentError("INTEGRITY_ERROR", "Invalid running job owner")
            try:
                os.kill(owner["pid"], 0)
            except ProcessLookupError:
                raise AgentError("ORPHANED_JOB", "Kernel process ended without a terminal completion receipt") from None
            return {"schemaVersion": SCHEMA, "jobId": job.name, "status": "running",
                    "integrityVerified": False, "receipt": {"synced": False, "scope": "local-kernel"}}
        raise AgentError("IN_PROGRESS", "Job initialization is in progress")
    completion = read_json(job / "completion.json")
    receipt_bytes = checked_path(job / "receipt.json").read_bytes()
    if not isinstance(completion, dict) or completion.get("receiptSha256") != digest(receipt_bytes):
        raise AgentError("INTEGRITY_ERROR", "Receipt checksum mismatch")
    receipt = read_json(job / "receipt.json")
    if (receipt.get("schemaVersion") != SCHEMA or receipt.get("jobId") != job.name
            or receipt.get("status") not in TERMINAL):
        raise AgentError("INTEGRITY_ERROR", "Invalid terminal receipt")
    envelope = read_json(job / "request.json")
    if digest(canonical(envelope.get("request"))) != receipt.get("inputSha256"):
        raise AgentError("INTEGRITY_ERROR", "Request checksum mismatch")
    for item in receipt.get("artifacts", []):
        path = artifact_path(job, item.get("name"))
        data = path.read_bytes()
        if len(data) != item.get("bytes") or digest(data) != item.get("sha256"):
            raise AgentError("INTEGRITY_ERROR", "Artifact checksum mismatch")
    if receipt["status"] in ("succeeded", "degraded"):
        evidence = result_evidence(job / "artifacts", envelope["request"], receipt["artifacts"])
        if (receipt.get("requestedRiskLimits") != envelope["request"]["riskLimits"]
                or any(receipt.get(name) != value for name, value in evidence.items())):
            raise AgentError("INTEGRITY_ERROR", "Receipt risk or result binding does not match the actual artifact")
    return receipt


_SECRETS: list[str] = []


def scrub_text(value: str) -> str:
    for secret in _SECRETS:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    value = re.sub(r"(https?://)[^\s/<>]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)([?&](?:api_?key|access_?token|token|password|secret)=)[^&\s<\"']+",
                   r"\1[REDACTED]", value)
    return value


def serializable(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: serializable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {scrub_text(str(k)): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serializable(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return serializable(value.item())
    return scrub_text(str(value))


def scrub_inplace(value: Any) -> Any:
    if is_dataclass(value):
        for field in fields(value):
            setattr(value, field.name, scrub_inplace(getattr(value, field.name)))
    elif isinstance(value, dict):
        for key in value:
            value[key] = scrub_inplace(value[key])
    elif isinstance(value, list):
        value[:] = [scrub_inplace(v) for v in value]
    elif isinstance(value, tuple):
        return tuple(scrub_inplace(v) for v in value)
    elif isinstance(value, str):
        return scrub_text(value)
    return value


def configure(job: Path) -> tuple[Path, Path, str]:
    artifacts, runtime = job / "artifacts", job / "_runtime"
    private_mkdir(artifacts)
    private_mkdir(runtime)
    for name in ("home", "cache", "config"):
        private_mkdir(runtime / name)
    os.chdir(runtime)
    from . import config
    # Set these before importing consumers with module-level default paths.
    config.CACHE_DIR = str(runtime / "cache")
    config.REPORTS_DIR = str(artifacts)
    config.CALIBRATION_SAMPLES_PATH = str(artifacts / "calibration_samples.json")
    config.CALIBRATION_JOURNAL_PATH = str(artifacts / "journal.json")
    config.REVIEW_PROPOSALS_DIR = str(artifacts / "review_proposals")
    configuration = {k: serializable(v) for k, v in vars(config).items()
                     if k.isupper() and not k.endswith(("_PATH", "_DIR"))}
    return artifacts, runtime, digest(canonical(configuration))


def emit_event(bridge, operation: str, request: dict, data: Any) -> None:
    from .governance_bridge import FiveElementEvent, RuleImpact
    record = bridge.emit(FiveElementEvent(
        who={"type": "agent", "id": "tiger.agent-api", "version": "1"},
        context={"channel": "agent-api", "stage": request["environment"],
                 "market": request["market"]},
        object={"type": "report", "id": request["idempotencyKey"]},
        decision={"action": operation, "basis": ["Research execution; no brokerage order or approval"],
                  "after": serializable(data)},
        rule_impact=[RuleImpact("R-T15", result="pass")]))
    if record is None:
        raise AgentError("GOVERNANCE_WRITE_FAILED", "Local governance event could not be written")


def daily(request: dict, artifacts: Path, bridge) -> tuple[Any, list, list, dict]:
    from .pipeline import _single_with_fallback, run_pipeline
    from .providers import get_provider
    from .journal import Journal
    from .simulator import Bar, SimEngine
    from .review.chief import ReviewChief
    from .redline import LLM_STEPS, STEP_REGISTRY
    from .report import render_markdown
    from .report_html import render_html
    result = run_pipeline(provider_name=request["provider"], universe_mode=request["universe"],
                          top_n=request["topN"], max_picks=request["maxPicks"],
                          account_usd=request["account"], use_tuned=False, market=request["market"],
                          run_state_dir=str(artifacts.parent / "_runtime"), ledger_dir=str(artifacts),
                          risk_limits=request["riskLimits"],
                          llm_client=DisabledModelClient() if request["llmMode"] == "disabled" else ConfiguredModelClient())
    risk_snapshot(request, result.raw.get("risk_limits"), result.raw.get("gate_params"))
    scrub_inplace(result)
    provider = get_provider(request["provider"])
    journal = Journal(artifacts / "journal.json")
    journal.log_picks(result, request["account"])
    journal.settle(provider, as_of=result.trade_date, risk_limits=result.raw["risk_limits"])
    journal.save()  # Empty signal days still have a real, reviewable accounting artifact.
    if (not journal.path.is_file()
            or canonical(read_json(journal.path)) != canonical(serializable(journal.records))):
        raise AgentError("INCOMPLETE_ARTIFACTS", "Signal journal did not persist the settled records")
    stats = journal.stats()
    if journal.last_failed:
        raise AgentError("SETTLEMENT_FAILED", "Signal settlement data is missing")
    sim = SimEngine(str(artifacts / "sim_portfolio.json"), initial_cash=request["account"])
    bars: dict = {}

    def get_bar(ticker: str):
        if ticker not in bars:
            df = _single_with_fallback(provider, "ohlcv", ticker, days=400)
            if df is not None:
                df = df[df.index.date <= datetime.strptime(result.trade_date, "%Y-%m-%d").date()]
            if df is None or len(df) == 0:
                raise AgentError("DATA_MISSING", "Simulation requires an actual price bar")
            row = df.iloc[-1]
            bars[ticker] = Bar(open=float(row["Open"]), high=float(row["High"]),
                               low=float(row["Low"]), close=float(row["Close"]),
                               adv=float((df["Close"] * df["Volume"]).tail(20).mean()),
                               date=df.index[-1].date().isoformat(),
                               prev_close=float(df["Close"].iloc[-2]) if len(df) >= 2 else None)
        return bars[ticker]

    sim_out = sim.step(result.trade_date, result, get_bar, max_new=request["maxPicks"])
    sim.save()
    chief = ReviewChief(out_dir=str(artifacts), journal_path=str(journal.path),
                        proposals_dir=str(artifacts / "review_proposals"), bridge=bridge)
    review = chief.daily(result.trade_date)
    if (not Path(review["memo_path"]).is_file()
            or review.get("calibration", {}).get("status") == "skipped"):
        raise AgentError("REVIEW_FAILED", "Daily review or calibration did not complete")
    trace = result.raw.get("redline", [])
    for record in trace:
        if record["step"] == "review.daily":
            record.update(status="executed", ms=round(review["ms"], 1), note="")
    expected = [step.name for step in STEP_REGISTRY]
    if ([record.get("step") for record in trace] != expected
            or any(record.get("status") not in ("executed", "passthrough") for record in trace)
            or any(record["status"] == "passthrough" and record["step"] not in LLM_STEPS for record in trace)
            or result.raw.get("calibration", {}).get("status") == "skipped"):
        raise AgentError("INCOMPLETE_PIPELINE", "Kernel trace is incomplete or contains a failed rule step")
    scrub_inplace(result)
    emitted = bridge.emit_from_result(result, sim_out)
    if emitted < len(expected) + 1:
        raise AgentError("GOVERNANCE_WRITE_FAILED", "Pipeline event sequence did not complete")
    stamp = result.trade_date.replace("-", "")
    write_atomic(artifacts / f"result_{stamp}.json", serializable(result))
    write_atomic(artifacts / f"日报_{stamp}.md", scrub_text(render_markdown(result)), text=True)
    bench = {}
    from .markets.registry import get_market
    benchmark_tickers = ("QQQ", "SPY") if request["market"] == "us" else (get_market(request["market"]).benchmarks["index"],)
    for ticker in benchmark_tickers:
        df = _single_with_fallback(provider, "ohlcv", ticker, days=400)
        bench[ticker] = [[str(d.date()), float(c)] for d, c in zip(df.index, df["Close"])]
    write_atomic(artifacts / f"日报_{stamp}.html",
                 scrub_text(render_html(result, stats, journal.records,
                                        sim={"state": sim.state, "stats": sim.stats()}, bench=bench)), text=True)
    if request.get("mode") == "premarket":
        from .triggers import premarket_plan
        write_atomic(artifacts / f"盘前计划_{stamp}.md", scrub_text(premarket_plan(result)), text=True)
    degraded = [record["step"] for record in trace if record["status"] == "passthrough"]
    if result.raw.get("docs_collected", 0) == 0:
        degraded.append("search.no_documents")
    return result, trace, degraded, {"tradeDate": result.trade_date, "action": result.action,
                                    "account": request["account"], "currency": get_market(request["market"]).currency,
                                    "picks": len(result.picks), "reviewMemo": Path(review["memo_path"]).name,
                                    "dataProvenance": serializable(result.raw.get("source_lineage", []))}


def source_input(workspace: Path, tenant: str, request: dict) -> tuple[Path, dict, dict]:
    source = request["sourceJob"]
    job = job_at(workspace, tenant, source["jobId"])
    receipt = verified_job(job)
    if (receipt.get("status") not in ("succeeded", "degraded")
            or receipt.get("operation") != "pipeline"
            or receipt.get("mode") not in ("daily", "premarket")):
        raise AgentError("INVALID_SOURCE_JOB", "Source must be a completed daily or premarket pipeline")
    for name in ("provider", "market", "environment"):
        if receipt.get(name) != request[name]:
            raise AgentError("INVALID_SOURCE_JOB", "Source provider, market and environment must match")
    item = next((a for a in receipt["artifacts"] if a["role"] == "pipeline-result"), None)
    if item is None or item["sha256"] != source["resultSha256"]:
        raise AgentError("INTEGRITY_ERROR", "Source result checksum does not match sourceJob")
    result = read_json(artifact_path(job, item["name"]))
    risk_snapshot(request, result.get("raw", {}).get("risk_limits"), result.get("raw", {}).get("gate_params"))
    return job, receipt, result


def research(request: dict, artifacts: Path, bridge) -> tuple[Any, list, list, dict]:
    from .providers import get_provider
    from .universe import load_universe
    from .backtest import GateParams, collect_day_frames, run_backtest, run_wfa
    from . import config
    provider = get_provider(request["provider"])
    started = time.monotonic()
    cache = artifacts.parent / "_runtime" / "cache"
    frames, panel, _ = collect_day_frames(provider, load_universe(request["universe"], cache_dir=cache),
                                         days=max(420, request["btDays"] + 310),
                                         signal_days=request["btDays"], top_n=request["topN"],
                                         use_cache=False, cache_dir=cache)
    trace = [{"step": "backtest.collect_day_frames", "status": "executed",
              "ms": round((time.monotonic() - started) * 1000, 1), "note": "Fresh point-in-time frames; cache disabled"}]
    if not frames:
        raise AgentError("DATA_MISSING", "No historical frames were produced")
    started = time.monotonic()
    if request["mode"] == "backtest":
        data = serializable(run_backtest(frames, panel, GateParams(max_picks=request["maxPicks"]),
                                         account_usd=request["account"], risk_limits=request["riskLimits"]))
        snapshot = risk_snapshot(request, {name: data.get("params", {}).get(name) for name in risk_maxima()},
                                 data.get("params"))
        trace.append({"step": "backtest.run_backtest", "status": "executed",
                      "ms": round((time.monotonic() - started) * 1000, 1), "note": "Historical engine; no current LLM input"})
        data["disclosures"] = ["Current universe and chain mappings imply survivorship and mapping bias",
                               f"Kernel trading cost: {config.COST_BPS} bps per side",
                               "No brokerage order or production parameter change"]
        report_name = "backtest"
    else:
        data = serializable(run_wfa(frames, panel, train=request["trainDays"],
                                    test=request["testDays"], step=request["stepDays"],
                                    base_params=GateParams(max_picks=request["maxPicks"]),
                                    account_usd=request["account"], risk_limits=request["riskLimits"]))
        if data.get("error") or not data.get("n_folds"):
            raise AgentError("WFA_FAILED", "WFA did not produce completed folds")
        snapshot = risk_snapshot(request, {name: data.get("base_params", {}).get(name) for name in risk_maxima()},
                                 data.get("base_params"))
        trace.append({"step": "backtest.run_wfa", "status": "executed",
                      "ms": round((time.monotonic() - started) * 1000, 1), "note": "Research only; active parameters unchanged"})
        eligible = (bool(data.get("recommended_params")) and data.get("dsr", 0) >= config.REVIEW_DSR_SIGNIFICANT
                    and data.get("oos_aggregate", {}).get("expectancy_r", 0) > 0)
        proposal = {"schemaVersion": "tiger.wfa-research/v1", "kind": "wfa_research",
                    "jobId": request["idempotencyKey"], "status": "pending_research_review",
                    "eligibleForApproval": eligible, "parametersApplied": False,
                    "researchSha256": digest(canonical(data)),
                    "recommendedParams": data.get("recommended_params", {}), "dsr": data.get("dsr"),
                    "oosAggregate": data.get("oos_aggregate", {}),
                    "approvalNote": "Research receipt is not an approval; a separate bound governance approval is required"}
        write_atomic(artifacts / "wfa_research_proposal.json", proposal)
        data["proposal"] = proposal
        # The kernel wording describes the legacy writer. This API never invokes it.
        data["recommended_note"] = "Research recommendation only; no active parameter file was written"
        report_name = "wfa"
    data.update(snapshot)
    write_atomic(artifacts / f"{report_name}.json", data)
    report_text = f"# Tiger {report_name} research\n\nProvider: {provider.name}\n\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    write_atomic(artifacts / f"{report_name}.md", report_text, text=True)
    write_atomic(artifacts / f"{report_name}.html",
                 "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                 "<title>Tiger research</title><style>body{margin:2rem auto;max-width:72rem;padding:0 1rem;font:16px system-ui;background:#10151e;color:#eef2f8}pre{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}</style>"
                 f"<h1>Tiger {report_name} research</h1><pre>{html.escape(report_text)}</pre></html>", text=True)
    emit_event(bridge, f"research.{request['mode']}", request, {"frames": len(frames), "parametersApplied": False})
    return data, trace, [], {"frames": len(frames), "parametersApplied": False,
                              "trades": data.get("n_trades", data.get("oos_aggregate", {}).get("trades", 0))}


def guard_employee_data(spec, benchmark, stocks: dict, macro: dict) -> tuple[Any, dict, dict, dict]:
    """Apply the kernel's time/coverage discipline to directed employee calls."""
    import pandas as pd
    from zoneinfo import ZoneInfo
    reference = spec.prev_trading_day(datetime.now(ZoneInfo(spec.timezone)).date())

    def visible(value):
        if (not isinstance(value, (pd.Series, pd.DataFrame))
                or not isinstance(value.index, pd.DatetimeIndex)
                or value.index.has_duplicates or not value.index.is_monotonic_increasing):
            raise AgentError("INVALID_MARKET_DATA", "Employee data requires unique ordered date observations")
        return value[value.index.date <= reference].copy()

    def valid_bars(value):
        if not isinstance(value, pd.DataFrame) or not {"Open", "High", "Low", "Close", "Volume"}.issubset(value):
            return False
        try:
            numbers = value[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            return (all(math.isfinite(float(v)) for v in numbers.to_numpy().ravel())
                    and bool((numbers[["Open", "High", "Low", "Close"]] > 0).all().all())
                    and bool((numbers["Volume"] >= 0).all())
                    and bool((numbers["High"] >= numbers[["Open", "Close", "Low"]].max(axis=1)).all())
                    and bool((numbers["Low"] <= numbers[["Open", "Close", "High"]].min(axis=1)).all()))
        except (TypeError, ValueError):
            return False

    benchmark = visible(benchmark)
    if len(benchmark) < 260 or not valid_bars(benchmark):
        raise AgentError("DATA_MISSING", "Employee benchmark requires 260 valid OHLCV observations")
    last = benchmark.index[-1].date()
    lag = (reference - last).days
    if lag > 3:
        raise AgentError("STALE_MARKET_DATA", "Employee benchmark is stale for this market")
    report = {"referenceDate": reference.isoformat(), "benchmarkLastBar": last.isoformat(),
              "benchmarkLagDays": lag, "removedStocks": {}, "missingMacro": []}
    cleaned = {}
    for ticker, frame in stocks.items():
        try:
            frame = visible(frame)
            if frame.empty or not valid_bars(frame):
                report["removedStocks"][ticker] = "missing-or-invalid-bars"
            elif (last - frame.index[-1].date()).days > 5:
                report["removedStocks"][ticker] = "stale-bars"
            else:
                cleaned[ticker] = frame
        except AgentError:
            report["removedStocks"][ticker] = "invalid-date-index"
    if not cleaned:
        raise AgentError("DATA_MISSING", "Employee has no valid current stock observations")
    for name, minimum in (("tnx", 100), ("vix", 30), ("vix9d", 1)):
        value = macro.get(name)
        try:
            value = visible(value)
            valid = (len(value) >= minimum and (reference - value.index[-1].date()).days <= 5
                     and all(math.isfinite(float(v)) for v in value)
                     and (name == "tnx" or all(float(v) > 0 for v in value)))
        except (AgentError, TypeError, ValueError):
            valid = False
        if not valid:
            if name != "vix9d" and spec.benchmark_hard_fail:
                raise AgentError("DATA_MISSING", "Employee requires complete current rate and volatility benchmarks")
            report["missingMacro"].append(name)
            value = None
        macro[name] = value
    return benchmark, cleaned, macro, report


def standalone_employee(request: dict, artifacts: Path, bridge) -> tuple[Any, list, list, dict]:
    from .agents import MRSAgent, UniverseScannerAgent, RiskManagerAgent
    from .providers import get_provider
    from .markets.registry import get_market
    from .pipeline import _batch_with_fallback, _single_with_fallback, _lineage, _visible_data
    from .universe import load_universe, load_market_universe
    provider = get_provider(request["provider"])
    employee = request["employee"]
    lineage_start = len(_lineage())
    if employee == "risk":
        # This employee needs MRS, sector, chain and TSS outputs. Prepare them
        # through the existing pipeline, then call the selected employee itself.
        result, trace, degraded, summary = daily(request, artifacts, bridge)
        spec = get_market(request["market"])
        from .events import EventCalendar
        quote_started = time.monotonic()
        stock_data = (_batch_with_fallback(provider, [c.ticker for c in result.watchlist], 420)
                      if result.watchlist else {})
        # This fetch happens after run_pipeline has reset its as-of context.
        # A provider may now return a newer bar than the bound daily result;
        # never let that bar become the compliance previous-close reference.
        import pandas as pd
        cutoff = datetime.strptime(result.trade_date, "%Y-%m-%d").date()
        for candidate in result.watchlist:
            frame = stock_data.get(candidate.ticker)
            if (not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex)
                    or not {"Open", "High", "Low", "Close"}.issubset(frame.columns)):
                raise AgentError("DATA_MISSING", "Risk employee requires dated actual OHLC observations")
            frame = _visible_data(frame[frame.index.date <= cutoff].copy())
            if (len(frame) < 2 or (cutoff - frame.index[-1].date()).days > 5
                    or not all(math.isfinite(float(v)) for v in frame[["Open", "High", "Low", "Close"]].to_numpy().ravel())):
                raise AgentError("DATA_MISSING", "Risk employee requires two current valid observations before its daily cutoff")
            stock_data[candidate.ticker] = frame
        trace.append({"step": "employee.risk.prepare_quotes", "status": "executed",
                      "ms": round((time.monotonic() - quote_started) * 1000, 1),
                      "note": "Actual recent OHLCV clipped to the verified daily date supplies compliance previous closes"})
        context = {"mrs": result.mrs, "sectors": result.sectors, "watchlist": result.watchlist,
                   "chain_map": {c.chain_id: c for c in result.chains}, "market_spec": spec,
                   "market_grad": spec.graduation(), "trade_date": result.trade_date,
                   "event_calendar": EventCalendar(), "market_data": {"stock_ohlcv": stock_data},
                   "risk_limits": request["riskLimits"]}
        from .parameters import GateParams
        context["gate_params"] = GateParams(**result.raw["gate_params"])
        started = time.monotonic()
        output = RiskManagerAgent(provider, account_usd=request["account"],
                                  max_picks=request["maxPicks"]).execute(context)
        trace.append({"step": "employee.risk.execute", "status": "executed",
                      "ms": round((time.monotonic() - started) * 1000, 1),
                      "note": "Prerequisite: full pipeline, journal, simulation and daily review"})
        data = {"employee": employee, "result": serializable(output),
                "action": context.get("action"), "notes": serializable(context.get("notes", [])),
                "quoteCutoffDate": result.trade_date,
                "prerequisites": ["pipeline.run_pipeline", "review.chief.daily"]}
        data.update(risk_snapshot(request, context.get("risk_limits"), context.get("gate_params")))
    else:
        spec = get_market(request["market"])
        cache = artifacts.parent / "_runtime" / "cache"
        universe = (load_universe(request["universe"], cache_dir=cache) if request["market"] == "us"
                    else load_market_universe(request["market"], request["universe"], cache_dir=cache)[0])
        bmk = spec.benchmarks
        started = time.monotonic()
        spy = _single_with_fallback(provider, "ohlcv", bmk["index"], days=420)
        if spy is None or len(spy) < 260:
            raise AgentError("DATA_MISSING", "Employee requires at least 260 benchmark bars")
        stocks = _batch_with_fallback(provider, universe, 420)
        md = {"spy": spy, "stock_ohlcv": stocks,
              "universe_closes": {t: df["Close"] for t, df in stocks.items()},
              "benchmark_labels": {k: bmk.get(f"{k}_label", bmk[k]) for k in ("index", "rate", "vol")}}
        for name, method, symbol in (("tnx", "rate_yield_for", bmk["rate"]),
                                      ("vix", "vol_index_for", bmk["vol"])):
            try:
                md[name] = _single_with_fallback(provider, method, symbol, days=420)
            except Exception:
                if spec.benchmark_hard_fail:
                    raise AgentError("DATA_MISSING", "Required market benchmark is unavailable") from None
                md[name] = None
        if bmk.get("vol_short"):
            try:
                md["vix9d"] = _single_with_fallback(provider, "vol_index_for", bmk["vol_short"], days=420)
            except Exception:
                md["vix9d"] = None
        else:
            md["vix9d"] = None
        spy, stocks, macro, freshness = guard_employee_data(spec, spy, stocks, md)
        md.update(macro)
        md.update(spy=spy, stock_ohlcv=stocks,
                  universe_closes={t: df["Close"] for t, df in stocks.items()}, freshness=freshness)
        from .parameters import GateParams
        params = GateParams(max_picks=request["maxPicks"], **request["riskLimits"])
        context = {"market_data": md, "market_spec": spec, "scan_filters": spec.scan_filters(),
                   "risk_limits": request["riskLimits"], "gate_params": params}
        trace = [{"step": "employee.prepare_data", "status": "executed",
                  "ms": round((time.monotonic() - started) * 1000, 1), "note": "Provider OHLCV and benchmark inputs"}]
        started = time.monotonic()
        agent = (UniverseScannerAgent(provider, universe, request["topN"])
                 if employee == "scanner" else MRSAgent(provider))
        output = agent.execute(context)
        trace.append({"step": f"employee.{employee}.execute", "status": "executed",
                      "ms": round((time.monotonic() - started) * 1000, 1), "note": agent.name})
        data = {"employee": employee, "result": serializable(output),
                "scanStats": context.get("scan_stats"), "freshness": freshness,
                "prerequisites": ["provider.ohlcv", "provider.benchmarks"]}
        data.update(risk_snapshot(request, context.get("risk_limits"), context.get("gate_params")))
        degraded = [f"employee.mrs.{name}.missing" for name, dim in getattr(output, "dimensions", {}).items()
                    if dim.score is None or dim.missing]
        if len(stocks) < len(universe):
            degraded.append("employee.data.partial_coverage")
        summary = {"employee": employee, "resultCount": len(output) if isinstance(output, list) else 1}
    provenance = serializable(_lineage()[lineage_start:])
    data["dataProvenance"] = provenance
    summary["dataProvenance"] = summary.get("dataProvenance", []) + provenance
    write_atomic(artifacts / "employee_result.json", data)
    write_atomic(artifacts / "employee_report.md", f"# Tiger employee: {employee}\n\n```json\n" + json.dumps(serializable(data), ensure_ascii=False, indent=2) + "\n```\n", text=True)
    emit_event(bridge, f"employee.{employee}", request, summary)
    return data, trace, degraded, summary


def source_operation(request: dict, workspace: Path, tenant: str, artifacts: Path,
                     runtime: Path, bridge) -> tuple[Any, list, list, dict]:
    source, receipt, result = source_input(workspace, tenant, request)
    started = time.monotonic()
    degraded = []
    if request.get("mode") == "intraday":
        from .markets.registry import get_market
        from .providers import get_provider
        from .triggers import monitor_loop, watch_from_result
        from .pipeline import _lineage, quote_with_fallback
        from .providers.base import is_synthetic
        import pandas as pd
        spec = get_market(request["market"])
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(spec.timezone)).date()
        source_date = datetime.strptime(result["trade_date"], "%Y-%m-%d").date()
        if source_date < spec.prev_trading_day(today) or source_date > today:
            raise AgentError("STALE_SOURCE_JOB", "Source daily result is not fresh for this market")
        item = next(a for a in receipt["artifacts"] if a["role"] == "pipeline-result")
        watch = watch_from_result(artifact_path(source, item["name"]))
        provider = get_provider(request["provider"])
        coverage = {"requested": 0, "ready": 0, "missing": 0, "stale": 0,
                    "invalid": 0, "metadataUnverified": 0, "errors": 0}
        observations = []
        lineage_start = len(_lineage())

        def verified_quote(selected, ticker):
            coverage["requested"] += 1
            observation = {"ticker": ticker}
            observations.append(observation)

            def reject_quote(reason):
                coverage[reason] += 1
                observation["status"] = reason
                return None

            try:
                quote = quote_with_fallback(selected, ticker)
            except Exception as error:
                observation["errorType"] = type(error).__name__
                return reject_quote("errors")
            if quote is None:
                return reject_quote("missing")
            if (not isinstance(quote, dict) or isinstance(quote.get("price"), bool)
                    or not isinstance(quote.get("price"), (int, float))
                    or not math.isfinite(quote["price"]) or quote["price"] <= 0):
                return reject_quote("invalid")
            observation.update(price=quote["price"], kind=quote.get("kind"), ts=quote.get("ts"))
            if quote.get("stale"):
                return reject_quote("stale")
            kind, stamp = quote.get("kind"), quote.get("ts")
            if (kind not in ("realtime", "realtime_delayed", "eod_close")
                    or not isinstance(stamp, str) or not stamp.strip()):
                return reject_quote("metadataUnverified")
            try:
                timestamp = pd.Timestamp(stamp)
                if pd.isna(timestamp):
                    return reject_quote("metadataUnverified")
                if kind == "eod_close":
                    if timestamp.date() < spec.prev_trading_day(today) or timestamp.date() > today:
                        return reject_quote("stale")
                else:
                    if timestamp.tzinfo is None:
                        # Demo documents its timestamp as the launcher's local
                        # clock. External naive timestamps lack a verified
                        # timezone and cannot prove intraday freshness.
                        if not is_synthetic(selected):
                            return reject_quote("metadataUnverified")
                        timestamp = timestamp.tz_localize(datetime.now().astimezone().tzinfo)
                    age = (datetime.now(timezone.utc) - timestamp.to_pydatetime()).total_seconds()
                    maximum_age = 900 if kind == "realtime" else 3600
                    if age < -300 or age > maximum_age:
                        return reject_quote("stale")
            except (ValueError, TypeError, OverflowError):
                return reject_quote("metadataUnverified")
            coverage["ready"] += 1
            observation["status"] = "ready"
            return quote

        alerts = monitor_loop(watch, provider, interval_s=request["intervalSeconds"],
                              cycles=request["cycles"], quote_fetcher=verified_quote)
        if coverage["requested"] and not coverage["ready"]:
            raise AgentError("QUOTE_DATA_MISSING", "No watch quote had verifiable usable price and time metadata",
                             details={"quoteCoverage": coverage, "observations": serializable(observations)})
        if coverage["ready"] < coverage["requested"]:
            degraded.append("intraday.quote.partial_coverage")
        data = {"sourceJob": request["sourceJob"], "watch": serializable(watch),
                "alerts": serializable(alerts), "cycles": request["cycles"],
                "quoteCoverage": coverage, "quoteObservations": serializable(observations),
                "dataProvenance": serializable(_lineage()[lineage_start:]),
                "disclosure": "Price alerts are research output; no brokerage action is executed"}
        step = "triggers.monitor_loop"
        name = "intraday"
    else:
        from .review.chief import ReviewChief
        journal = next((a for a in receipt["artifacts"] if a["role"] == "signal-journal"), None)
        if journal is None:
            raise AgentError("INVALID_SOURCE_JOB", "Source signal journal is missing")
        records = read_json(artifact_path(source, journal["name"]))
        if not isinstance(records, list):
            raise AgentError("INVALID_SOURCE_JOB", "Source signal journal is invalid")
        copy = runtime / "source-journal.json"
        write_atomic(copy, records)
        chief = ReviewChief(out_dir=str(artifacts), journal_path=str(copy),
                            proposals_dir=str(artifacts / "review_proposals"), bridge=bridge)
        frequency = request["reviewFrequency"]
        output = chief.daily(result["trade_date"], include_weekly=False) if frequency == "daily" else chief.weekly()
        if output.get("calibration", {}).get("status") == "skipped":
            raise AgentError("REVIEW_FAILED", "Review calibration failed")
        data = {"sourceJob": request["sourceJob"], "frequency": frequency,
                "result": serializable(output), "sourceJournalUnchanged": True,
                "approvalsExecuted": False, "parametersApplied": False}
        if "memo_path" in data["result"]:
            data["result"]["memo_path"] = Path(output["memo_path"]).name
        step = f"review.chief.{frequency}"
        name = "review"
    # Verify original source artifacts again after the read-only operation.
    verified_job(source)
    data.update(risk_snapshot(request, result.get("raw", {}).get("risk_limits"),
                              result.get("raw", {}).get("gate_params")))
    trace = [{"step": "source.verify_sha256", "status": "executed", "ms": 0,
              "note": "All source artifacts verified before and after execution"},
             {"step": step, "status": "executed", "ms": round((time.monotonic() - started) * 1000, 1), "note": ""}]
    write_atomic(artifacts / f"{name}.json", serializable(data))
    write_atomic(artifacts / f"{name}.md", f"# Tiger {name}\n\n```json\n" + json.dumps(serializable(data), ensure_ascii=False, indent=2) + "\n```\n", text=True)
    emit_event(bridge, f"agent.{name}", request, {"sourceJob": request["sourceJob"], "readOnlySource": True})
    return data, trace, degraded, {"sourceJob": request["sourceJob"], "alerts": len(data.get("alerts", [])),
                                  **({"quoteCoverage": data["quoteCoverage"]} if name == "intraday" else {}),
                                  "approvalsExecuted": False, "parametersApplied": False}


def finish_artifacts(artifacts: Path, required_actions: list[str]) -> list[dict]:
    from .governance_bridge import GovernanceBridge, GENESIS_HASH, event_hash
    ledger = artifacts / "governance_events.jsonl"
    ok, _ = GovernanceBridge.verify_chain(str(ledger))
    if not ok:
        raise AgentError("INTEGRITY_ERROR", "Original governance event chain did not verify")
    actions = [json.loads(line)["payload"]["decision"]["action"]
               for line in ledger.read_text(encoding="utf-8").splitlines()]
    if any(action not in actions for action in required_actions):
        raise AgentError("INCOMPLETE_GOVERNANCE", "A required operation event is missing")
    # Remove secrets before publishing and re-compute the chain over those
    # exact published payloads. The chain is then verified independently.
    if ledger.exists():
        previous = GENESIS_HASH
        lines = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            payload = serializable(event["payload"])
            event = {"payload": payload, "prev_hash": previous, "hash": event_hash(previous, payload)}
            previous = event["hash"]
            lines.append(json.dumps(event, ensure_ascii=False))
        write_atomic(ledger, "\n".join(lines) + "\n", text=True)
    ok, _ = GovernanceBridge.verify_chain(str(ledger))
    if not ok:
        raise AgentError("INTEGRITY_ERROR", "Local governance event chain did not verify")
    inventory = []
    for path in sorted(artifacts.rglob("*")):
        checked_path(path)
        if path.is_dir() or path.name.endswith(".lock"):
            continue
        if not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise AgentError("INTEGRITY_ERROR", "Invalid or oversized output artifact")
        content = path.read_text(encoding="utf-8")
        if path != ledger:
            content = scrub_text(content)
            if path.suffix == ".json":
                # RFC JSON output: dataclass NaN/Infinity is represented as null.
                content = canonical(serializable(json.loads(content, parse_constant=lambda _: None))).decode() + "\n"
            write_atomic(path, content, text=True)
        data = path.read_bytes()
        name = path.relative_to(artifacts).as_posix()
        role = ("pipeline-result" if name.startswith("result_") and name.endswith(".json")
                else "employee-result" if name == "employee_result.json"
                else "research-result" if name in ("backtest.json", "wfa.json")
                else "source-result" if name in ("intraday.json", "review.json")
                else "signal-journal" if name == "journal.json"
                else "governance-events" if path == ledger else "output")
        inventory.append({"name": name, "role": role, "bytes": len(data), "sha256": digest(data),
                          "mediaType": {".json": "application/json", ".jsonl": "application/x-ndjson",
                                        ".html": "text/html", ".md": "text/markdown"}.get(path.suffix, "text/plain")})
    return inventory


def result_evidence(artifacts: Path, request: dict, inventory: list) -> dict:
    if request["operation"] == "employee" and request.get("employee") != "review":
        role = "employee-result"
    elif request.get("mode") in ("daily", "premarket"):
        role = "pipeline-result"
    elif request.get("mode") in ("backtest", "tune"):
        role = "research-result"
    else:
        role = "source-result"
    results = [item for item in inventory if item.get("role") == role]
    if len(results) != 1:
        raise AgentError("INCOMPLETE_ARTIFACTS", "Exactly one canonical result artifact is required")
    artifact = results[0]
    data = read_json(artifact_path(artifacts.parent, artifact["name"]))
    if role == "pipeline-result":
        limits, params = data.get("raw", {}).get("risk_limits"), data.get("raw", {}).get("gate_params")
    else:
        limits, params = data.get("riskLimits"), data.get("gateParams")
    return {"resultSha256": artifact["sha256"],
            "resultArtifact": {"name": artifact["name"], "role": role},
            **risk_snapshot(request, limits, params)}


def terminal_receipt(job: Path, request: dict, started: str, *, status: str,
                     trace: list | None = None, degraded: list | None = None,
                     artifacts: list | None = None, summary: dict | None = None,
                     config_digest: str | None = None, kernel_digest: str | None = None,
                     error: dict | None = None, result_binding: dict | None = None) -> dict:
    verified = status in ("succeeded", "degraded")
    receipt = {"schemaVersion": SCHEMA, "jobId": job.name, "idempotencyKey": job.name,
               "inputSha256": digest(canonical(request)), "operation": request["operation"],
               "requestedRiskLimits": request["riskLimits"],
               **({"mode": request["mode"]} if "mode" in request else {"employee": request["employee"]}),
               "environment": request["environment"], "provider": request["provider"], "market": request["market"],
               "dataMode": "synthetic" if request["provider"] == "demo" else "external-unverified",
               "status": status, "startedAt": started, "finishedAt": now(),
               "exitCode": 0 if status == "succeeded" else 10 if status == "degraded" else 1,
               "sourceCommit": os.environ.get("TIGER_SOURCE_COMMIT") or None,
               "kernelDigest": kernel_digest, "configDigest": config_digest,
               "stepTrace": trace or [], "degradedSteps": degraded or [],
               "artifacts": artifacts or [], "summary": serializable(summary or {}),
               "integrityVerified": verified, "governanceSynced": False,
               "receipt": {"synced": verified, "scope": "local-kernel",
                           "meaning": "Local artifacts and local governance hash chain verified; no server synchronization"},
               "permissions": {"brokerOrders": False, "approvals": False, "parameterApplication": False}}
    if result_binding:
        receipt.update(result_binding)
    if error:
        receipt["error"] = error
    write_atomic(job / "receipt.json", receipt)
    write_atomic(job / "completion.json", {"schemaVersion": "tiger.agent-completion/v1",
                                           "receiptSha256": digest((job / "receipt.json").read_bytes())})
    return receipt


def run(request: dict, workspace: Path, tenant: str, kernel: Path) -> dict:
    job = job_at(workspace, tenant, request["idempotencyKey"])
    private_mkdir(job.parent)
    try:
        job.mkdir(mode=0o700)
    except FileExistsError:
        checked_path(job)
        if not (job / "request.json").exists():
            raise AgentError("IN_PROGRESS", "Job initialization is in progress") from None
        envelope = read_json(job / "request.json")
        if envelope.get("inputSha256") != digest(canonical(request)):
            raise AgentError("IDEMPOTENCY_CONFLICT", "Idempotency key was used with different inputs") from None
        receipt = verified_job(job)
        if receipt["status"] == "running":
            raise AgentError("IN_PROGRESS", "Identical job is still running") from None
        return {**receipt, "replayed": True}
    started = now()
    write_atomic(job / "request.json", {"request": request, "inputSha256": digest(canonical(request))})
    write_atomic(job / "owner.json", {"pid": os.getpid(), "startedAt": started,
                                     "inputSha256": digest(canonical(request))})
    trace, degraded, summary = [], [], {}
    configuration = None
    kernel_digest = None
    try:
        kernel_digest = digest(b"".join(digest(path.read_bytes()).encode()
                                      for path in sorted((kernel / "trading_system").rglob("*.py"))))
        artifacts, runtime, configuration = configure(job)
        from .governance_bridge import GovernanceBridge
        bridge = GovernanceBridge(str(artifacts / "governance_events.jsonl"),
                                  tenant_id=tenant, workspace_id=digest(str(workspace).encode())[:24])
        if request.get("mode") in ("intraday", "review") or request.get("employee") == "review":
            _, trace, degraded, summary = source_operation(request, workspace, tenant, artifacts, runtime, bridge)
        elif request["operation"] == "employee":
            _, trace, degraded, summary = standalone_employee(request, artifacts, bridge)
        elif request["mode"] in ("daily", "premarket"):
            _, trace, degraded, summary = daily(request, artifacts, bridge)
        else:
            _, trace, degraded, summary = research(request, artifacts, bridge)
        required_files = []
        mode, employee = request.get("mode"), request.get("employee")
        if mode in ("daily", "premarket") or employee == "risk":
            stamp = summary["tradeDate"].replace("-", "")
            required_files += [f"result_{stamp}.json", f"日报_{stamp}.md", f"日报_{stamp}.html",
                               "journal.json", "sim_portfolio.json", f"复盘_{stamp}.md", "calibration_samples.json"]
            if mode == "premarket":
                required_files.append(f"盘前计划_{stamp}.md")
        if request["operation"] == "employee" and employee != "review":
            required_files += ["employee_result.json", "employee_report.md"]
        if mode in ("backtest", "tune"):
            name = "wfa" if mode == "tune" else "backtest"
            required_files += [f"{name}.json", f"{name}.md", f"{name}.html"]
            if mode == "tune":
                required_files.append("wfa_research_proposal.json")
        if mode in ("intraday", "review") or employee == "review":
            name = "intraday" if mode == "intraday" else "review"
            required_files += [f"{name}.json", f"{name}.md"]
        for name in required_files:
            path = checked_path(artifacts / name, must_exist=False)
            if not path.is_file() or path.stat().st_size == 0:
                raise AgentError("INCOMPLETE_ARTIFACTS", "A required output artifact is missing or empty")
        write_atomic(artifacts / "execution_trace.json", {"steps": trace, "degradedSteps": degraded})
        emit_event(bridge, "agent.completed", request, {"status": "degraded" if degraded else "succeeded"})
        required_actions = ["agent.completed"]
        if request.get("mode") in ("daily", "premarket") or request.get("employee") == "risk":
            from .redline import STEP_REGISTRY
            required_actions += [f"pipeline.step.{step.name}" for step in STEP_REGISTRY]
            required_actions += ["review.daily", "gate.l4"]
        if request.get("mode") == "review" or request.get("employee") == "review":
            required_actions += [f"review.{request['reviewFrequency']}"]
        inventory = finish_artifacts(artifacts, required_actions)
        binding = result_evidence(artifacts, request, inventory)
        configuration = digest(canonical({"kernelConfigDigest": configuration,
                                          "requestedRiskLimits": request["riskLimits"],
                                          "gateParams": binding["gateParams"]}))
        return terminal_receipt(job, request, started, status="degraded" if degraded else "succeeded",
                                trace=serializable(trace), degraded=degraded, artifacts=inventory,
                                summary=summary, config_digest=configuration, kernel_digest=kernel_digest,
                                result_binding=binding)
    except Exception as error:
        code = error.code if isinstance(error, AgentError) else "KERNEL_FAILED"
        return terminal_receipt(job, request, started, status="failed", trace=serializable(trace),
                                degraded=degraded, config_digest=configuration, kernel_digest=kernel_digest,
                                error={"code": code, "message": str(error) if isinstance(error, AgentError)
                                       else f"Kernel execution failed ({type(error).__name__}); no successful completion receipt",
                                       **({"details": serializable(error.details)}
                                          if isinstance(error, AgentError) and error.details is not None else {})})


def interrupt(request: dict, workspace: Path, tenant: str, expected_pid: int, status: str,
              reason: str | None = None) -> dict:
    job = job_at(workspace, tenant, request["idempotencyKey"])
    if (job / "completion.json").exists():
        return verified_job(job)
    owner = read_json(job / "owner.json")
    if owner.get("pid") != expected_pid or owner.get("inputSha256") != digest(canonical(request)):
        raise AgentError("OWNERSHIP_MISMATCH", "Interrupted job owner did not match")
    return terminal_receipt(job, request, owner["startedAt"], status=status,
                            error={"code": (reason or status).upper(),
                                   "message": "Kernel process stopped before completion" if reason != "termination_failed"
                                   else "Kernel stopped; process tree termination could not be verified"})


def main() -> int:
    global _SECRETS
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--tenant", default="local")
    parser.add_argument("--get")
    parser.add_argument("--interrupted", choices=("timed_out", "cancelled", "failed"))
    parser.add_argument("--stop-reason", choices=("timed_out", "cancelled", "output_limit", "input_failure", "termination_failed"))
    parser.add_argument("--expected-pid", type=int)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    _SECRETS = sorted({v for k, v in os.environ.items()
                       if re.search(r"(?i)(secret|token|key|password)", k) and len(v) >= 6}, key=len, reverse=True)
    try:
        kernel = checked_path(Path(__file__).absolute().parent.parent)
        workspace = workspace_at(args.workspace, args.tenant, kernel, create=not args.get and not args.interrupted)
        if args.get:
            result = verified_job(job_at(workspace, args.tenant, args.get))
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise AgentError("INPUT_TOO_LARGE", "Request exceeds 64 KiB")
            try:
                request = normalize_request(json.loads(raw, parse_constant=lambda _: float("nan")))
            except (UnicodeError, json.JSONDecodeError):
                raise AgentError("INVALID_INPUT", "Request is not valid JSON") from None
            # Libraries cannot pollute the launcher's machine-readable stdout.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = (interrupt(request, workspace, args.tenant, args.expected_pid, args.interrupted, args.stop_reason)
                          if args.interrupted else run(request, workspace, args.tenant, kernel))
        sys.stdout.write(json.dumps(serializable(result), ensure_ascii=False, allow_nan=False) + "\n")
        return 0
    except Exception as error:
        code = error.code if isinstance(error, AgentError) else "API_FAILED"
        message = str(error) if isinstance(error, AgentError) else f"Agent API failed ({type(error).__name__})"
        sys.stdout.write(json.dumps({"status": "failed", "error": {"code": code, "message": scrub_text(message)}},
                                   ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
