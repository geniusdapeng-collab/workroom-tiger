"""Strict, ordered daily execution and eagerly persisted evidence boundaries.

The runner owns admission, storage, process supervision and account publication.
This module cannot publish an account or certify its own persisted evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, time
from pathlib import Path
from threading import get_ident
from time import perf_counter
from typing import Any, Callable, Mapping, Protocol

from .model_diagnostics import (MODEL_IO_METHODS, model_failure_diagnostic,
                                model_failure_accounting)


class EvidenceWriteError(RuntimeError):
    """Evidence could not be durably recorded; never a provider fallback."""


class ExecutionContractError(RuntimeError):
    """An execution attempted to violate its fixed stage/IO contract."""


class StrictDataUnavailable(RuntimeError):
    code = "DATA_UNAVAILABLE"


class StrictObservationError(StrictDataUnavailable):
    """Provider observation dates violate the frozen market-date boundary."""


class StrictDataStale(RuntimeError):
    code = "DATA_STALE"


class StrictSourceSynthetic(ExecutionContractError):
    code = "SOURCE_SYNTHETIC"


class RequiredModelFailed(ExecutionContractError):
    code = "MODEL_REQUIRED_FAILED"


REQUIRED_PROFILE = "us-public-model-required-paper-v1"
REQUIRED_POLICY_ID = "tiger.public-model-required-paper-policy.v1"
REQUIRED_MODEL_STAGES = ("clean.llm_semantic", "tech.sentiment", "tech.risk",
                         "sector.narrative", "decision.debate")
UPGRADE_PROFILE = "us-public-model-required-paper-v2"
PREMARKET_PROFILE = "us-public-model-required-premarket-paper-v2"
INTRADAY_PROFILE = "us-public-model-required-intraday-paper-v2"
UPGRADE_POLICY_ID = "tiger.public-model-required-paper-policy.v2"
UPGRADE_POLICIES = {
    UPGRADE_PROFILE: (UPGRADE_POLICY_ID, "daily"),
    PREMARKET_PROFILE: ("tiger.public-model-required-premarket-paper-policy.v2", "premarket"),
    INTRADAY_PROFILE: ("tiger.public-model-required-intraday-paper-policy.v2", "intraday"),
}
UPGRADE_POLICY_IDS = frozenset(row[0] for row in UPGRADE_POLICIES.values())
MODEL_POLICY_IDS = UPGRADE_POLICY_IDS | {REQUIRED_POLICY_ID}
UPGRADE_MODEL_STAGES = REQUIRED_MODEL_STAGES[:-1] + ("research.candidates", "decision.debate")


def complete_daily_cutoff(run_date: date, admitted_at_utc: str,
                          market_timezone: str = "America/New_York") -> date:
    """Conservative known-at daily boundary, shared by engine and verifier."""
    from zoneinfo import ZoneInfo
    from ..calendar import is_trading_day, prev_trading_day
    try:
        if market_timezone != "America/New_York":
            raise ValueError("Market timezone differs")
        instant = datetime.fromisoformat(admitted_at_utc)
        if instant.utcoffset() is None:
            raise ValueError("Admission clock is naive")
        local = instant.astimezone(ZoneInfo(market_timezone))
        if local.date() != run_date:
            raise ValueError("Admission clock differs")
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError("Upgrade requires its exact aware admission clock") from exc
    cutoff = run_date
    # Early closes are treated conservatively: regular 16:00 is the boundary.
    if is_trading_day(cutoff) and local.time() < time(16):
        cutoff -= timedelta(days=1)
    return prev_trading_day(cutoff)


PUBLIC_PROVIDER_NAMES = ("yahoo", "stooq", "official", "tencent", "sina", "eastmoney")
PUBLIC_SEARCH_SOURCE_NAMES = (
    "google_news", "reddit", "edgar", "patentsview", "federal_register",
    "eastmoney_news", "sina_flash",
)


@dataclass(frozen=True)
class ExecutionPolicy:
    policy_id: str
    model_mode: str = "disabled"
    provider_names: tuple[str, ...] = PUBLIC_PROVIDER_NAMES
    search_source_names: tuple[str, ...] = PUBLIC_SEARCH_SOURCE_NAMES
    market_timezone: str = "America/New_York"
    budget_policy_id: str | None = None
    clean_output_policy_id: str | None = None

    def __post_init__(self):
        if self.budget_policy_id is not None:
            from ..llm.request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4
            if (type(self.budget_policy_id) is not str or self.policy_id not in UPGRADE_POLICY_IDS
                    or self.budget_policy_id not in (BUDGET_POLICY_V3, BUDGET_POLICY_V4)):
                raise ExecutionContractError("Unsupported versioned request budget policy")
            if self.budget_policy_id == BUDGET_POLICY_V4:
                from ..llm.clean_output import CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1
                if self.clean_output_policy_id != CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1:
                    raise ExecutionContractError("Expanded budget requires the compact clean output policy")
        if self.clean_output_policy_id is not None:
            from ..llm.clean_output import (CLEAN_OUTPUT_POLICY_V1,
                                           CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                                           CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1,
                                           CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1)
            from ..llm.request_budget import BUDGET_POLICY_V3, BUDGET_POLICY_V4
            if (type(self.clean_output_policy_id) is not str
                    or self.policy_id not in UPGRADE_POLICY_IDS
                    or self.clean_output_policy_id not in (CLEAN_OUTPUT_POLICY_V1,
                                                          CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                                                          CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1,
                                                          CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1)
                    or self.budget_policy_id not in (BUDGET_POLICY_V3, BUDGET_POLICY_V4)
                    or self.budget_policy_id == BUDGET_POLICY_V4 and self.clean_output_policy_id != CLEAN_OUTPUT_COMPACT_REGEN_POLICY_V1):
                raise ExecutionContractError("Unsupported versioned clean output policy")
        required = self.policy_id in MODEL_POLICY_IDS
        if not self.policy_id or self.model_mode != ("enabled" if required else "disabled"):
            raise ExecutionContractError("Strict public paper requires a disabled model policy")
        if self.market_timezone != "America/New_York":
            raise ExecutionContractError("Strict public paper has a fixed market timezone")
        for values, allowed, label in (
            (self.provider_names, PUBLIC_PROVIDER_NAMES, "provider"),
            (self.search_source_names, ("deepseek_search",) if required else PUBLIC_SEARCH_SOURCE_NAMES, "search source"),
        ):
            if (not values or len(set(values)) != len(values)
                    or any(name not in allowed for name in values)):
                raise ExecutionContractError(f"Invalid public {label} allowlist")

    def to_dict(self):
        """Omit absent selectors to preserve exact historical policy bytes."""
        value = asdict(self)
        if self.budget_policy_id is None:
            value.pop("budget_policy_id")
        if self.clean_output_policy_id is None:
            value.pop("clean_output_policy_id")
        return value


@dataclass(frozen=True)
class RuntimePaths:
    work_dir: Path
    reports_dir: Path
    journal_path: Path
    sim_path: Path
    calibration_path: Path
    options_history_dir: Path
    governance_path: Path
    proposals_dir: Path
    calendar_override_path: Path

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, Path(getattr(self, name)).resolve())
        for name in self.__dataclass_fields__:
            if name != "work_dir":
                value = getattr(self, name)
                if value == self.work_dir or not value.is_relative_to(self.work_dir):
                    raise ExecutionContractError(f"Runtime path outside unique work directory: {name}")

    def to_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class DailyStageSpec:
    name: str
    driver: str
    owner: str
    allow_passthrough: bool = False


def daily_stage_specs(model_required: bool = False, upgrade: bool = False) -> tuple[DailyStageSpec, ...]:
    """The sole strict daily order: 3 setup + 20 registered pipeline + 16 tail."""
    from . import DEFERRED_STEPS, pipeline_registry
    setup = (
        DailyStageSpec("daily.preflight", "rules", "main"),
        DailyStageSpec("state.reset", "rules", "state"),
        DailyStageSpec("pipeline.prepare", "rules", "pipeline"),
    )
    pipeline = tuple(DailyStageSpec(s.name, s.driver, s.owner, s.driver == "llm" and not model_required)
                     for s in pipeline_registry(upgrade) if s.name not in DEFERRED_STEPS)
    tail = tuple(DailyStageSpec(name, "rules", owner) for name, owner in (
        ("accounting.validate", "main"), ("report.initial", "report"),
        ("journal.log", "journal"), ("journal.settle", "journal"),
        ("journal.stats", "journal"), ("paper.step", "simulator"),
        ("paper.persist", "simulator"), ("governance.initialize", "governance_bridge"),
        ("review.daily", "review.chief"), ("governance.emit", "governance_bridge"),
        ("governance.verify", "governance_bridge"), ("report.final_json", "report"),
        ("report.plan", "triggers"), ("report.markdown", "report"),
        ("report.html", "report_html"), ("daily.result", "main"),
    ))
    if upgrade:
        expanded = []
        for spec in tail:
            expanded.append(spec)
            if spec.name == "paper.persist":
                expanded.append(DailyStageSpec("journal.execution_sync", "rules", "journal"))
            if spec.name == "governance.verify":
                expanded += [DailyStageSpec("opportunity.lifecycle", "rules", "opportunity_lifecycle"),
                             DailyStageSpec("business.evaluate", "rules", "business_evaluation")]
        tail = tuple(expanded)
    return setup + pipeline + tail


def daily_stage_contract(model_required: bool = False, upgrade: bool = False) -> list[dict]:
    return [asdict(spec) for spec in daily_stage_specs(model_required, upgrade)]


class EvidenceStore(Protocol):
    def snapshot(self, *, stage: str, role: str, value: object) -> Mapping[str, object]: ...
    def append_event(self, event: Mapping[str, object]) -> Mapping[str, object]: ...


class _LegacyScope:
    def output(self, value: object) -> None:
        return None


@contextmanager
def optional_step(execution, name: str, inputs: object):
    if execution is None:
        yield _LegacyScope()
    else:
        with execution.step(name, inputs) as scope:
            yield scope


@dataclass
class _IOHandle:
    scope: "StageScope"
    index: int
    method: str
    input_ref: dict
    completed: bool = False


class StageScope:
    def __init__(self, execution: "ExecutionContext", spec: DailyStageSpec,
                 input_ref: dict, index: int):
        self.execution = execution
        self.spec = spec
        self.input_ref = input_ref
        self.index = index
        self.output_ref: dict | None = None
        self.reason = ""
        self.status = "running"
        self.started = perf_counter()
        self.elapsed_ms = 0.0
        self._handles: list[_IOHandle] = []
        self.io_calls: list[dict] = []

    def _check_active(self):
        self.execution._check_thread()
        self.execution._check_healthy()
        if self.execution._active is not self or self.status != "running":
            self.execution._reject("Evidence operation requires the current active stage")

    def output(self, value: object) -> None:
        self._check_active()
        if self.output_ref is not None:
            self.execution._reject("A stage must emit exactly one output")
        if any(not handle.completed for handle in self._handles):
            self.execution._reject("Stage output has unfinished IO calls")
        self.output_ref = self.execution._snapshot(self.spec.name, "output", {
            "schema": "tiger.stage-output.v1", "value": value,
            "io_calls": sorted(self.io_calls, key=lambda call: call["index"]),
        })

    def passthrough(self, reason: str) -> None:
        self._check_active()
        if not self.spec.allow_passthrough:
            self.execution._reject(f"Non-LLM stage cannot pass through: {self.spec.name}")
        if not isinstance(reason, str) or not reason.strip():
            self.execution._reject("Passthrough requires an explicit reason")
        self.reason = reason.strip()


class ExecutionContext:
    def __init__(self, *, run_id: str, run_date: date, paths: RuntimePaths,
                 policy: ExecutionPolicy, store: EvidenceStore,
                 admitted_at_utc: str | None = None):
        if not isinstance(run_id, str) or not run_id:
            raise ExecutionContractError("run_id is required")
        if not isinstance(run_date, date) or isinstance(run_date, datetime):
            raise ExecutionContractError("run_date must be the frozen admission-market date")
        self.run_id, self.run_date = run_id, run_date
        self.paths, self.policy, self.store = paths, policy, store
        self.model_required = policy.policy_id in MODEL_POLICY_IDS
        self.upgrade = policy.policy_id in UPGRADE_POLICY_IDS
        self.admitted_at_utc = admitted_at_utc
        self.workflow_phase = next((row[1] for row in UPGRADE_POLICIES.values()
                                    if row[0] == policy.policy_id), "daily")
        self.daily_bars_cutoff = run_date
        if self.upgrade:
            self.daily_bars_cutoff = complete_daily_cutoff(run_date, admitted_at_utc,
                                                         policy.market_timezone)
        self._specs = daily_stage_specs(self.model_required, self.upgrade)
        self._cursor = 0
        self._sequence = 0
        self._owner_thread = get_ident()
        self._active: StageScope | None = None
        self._failed = False
        self._contract_failure: str | None = None
        self._observation_failure: StrictObservationError | None = None
        self._previous_stage_ref: dict | None = None
        self._records: list[dict] = []

    def _reject(self, message: str):
        self._contract_failure = message
        self._failed = True
        raise ExecutionContractError(message)

    def _check_thread(self):
        if get_ident() != self._owner_thread:
            self._reject("Only the execution owner thread may write evidence")

    def _check_healthy(self):
        if not self._failed:
            return
        if self._contract_failure is not None:
            raise ExecutionContractError(self._contract_failure)
        if self._observation_failure is not None:
            raise self._observation_failure
        raise EvidenceWriteError("An earlier evidence operation failed")

    def _snapshot(self, stage: str, role: str, value: object) -> dict:
        self._check_thread()
        try:
            ref = dict(self.store.snapshot(stage=stage, role=role, value=value))
            if not all(key in ref for key in ("path", "sha256", "bytes", "schema")):
                raise ValueError("Snapshot store returned an incomplete reference")
            return ref
        except EvidenceWriteError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise EvidenceWriteError("Could not persist execution snapshot") from exc

    def _event(self, scope: StageScope, kind: str, status: str,
               error_type: str | None = None) -> dict:
        self._check_thread()
        event = {
            "schema": "tiger.execution-event.v1", "run_id": self.run_id,
            "sequence": self._sequence + 1, "stage_index": scope.index,
            "stage": scope.spec.name, "kind": kind, "status": status,
            "input_ref": scope.input_ref, "output_ref": scope.output_ref,
            "previous_stage_ref": self._previous_stage_ref,
            "reason": scope.reason or None, "error_type": error_type,
        }
        try:
            ref = dict(self.store.append_event(event))
        except EvidenceWriteError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise EvidenceWriteError("Could not persist execution event") from exc
        self._sequence += 1
        return ref

    @contextmanager
    def step(self, name: str, inputs: object):
        self._check_thread()
        if self._failed:
            self._reject("A failed execution cannot start another stage")
        if self._active is not None:
            self._reject("Stages cannot overlap")
        if self._cursor >= len(self._specs) or self._specs[self._cursor].name != name:
            expected = self._specs[self._cursor].name if self._cursor < len(self._specs) else "<end>"
            self._reject(f"Expected stage {expected}, received {name}")
        spec = self._specs[self._cursor]
        input_ref = self._snapshot(name, "input", inputs)
        scope = StageScope(self, spec, input_ref, self._cursor + 1)
        self._active = scope
        try:
            self._event(scope, "begin", "running")
            try:
                yield scope
                self._check_healthy()
                if scope.output_ref is None:
                    self._reject(f"Stage {name} did not persist an output")
                if self._contract_failure is not None:
                    raise ExecutionContractError(self._contract_failure)
                if self._failed:
                    raise EvidenceWriteError("An earlier evidence operation failed")
            except BaseException as original:
                self._failed = True
                scope.elapsed_ms = (perf_counter() - scope.started) * 1000
                scope.status = "failed"
                try:
                    self._event(scope, "end", "failed", type(original).__name__)
                except EvidenceWriteError as evidence_error:
                    raise evidence_error from original
                self._record(scope)
                raise
            scope.elapsed_ms = (perf_counter() - scope.started) * 1000
            status = "passthrough" if scope.reason else "completed"
            self._event(scope, "end", status)
            scope.status = status
            self._record(scope)
            self._previous_stage_ref = scope.output_ref
            self._cursor += 1
        finally:
            self._active = None

    def _record(self, scope: StageScope):
        self._records.append({
            "stage": scope.spec.name, "stage_index": scope.index,
            "status": scope.status, "ms": round(scope.elapsed_ms, 3),
            "reason": scope.reason, "input_ref": scope.input_ref,
            "output_ref": scope.output_ref,
        })

    def begin_io(self, method: str, inputs: object) -> _IOHandle:
        self._check_thread()
        scope = self._active
        if scope is None:
            self._reject("IO evidence requires an active stage")
        scope._check_active()
        if scope.output_ref is not None:
            self._reject("Cannot start IO after stage output")
        if not isinstance(method, str) or not method or any(c in method for c in "\r\n"):
            self._reject("IO method must be a controlled nonempty name")
        handle = _IOHandle(scope, len(scope._handles) + 1, method,
                           self._snapshot(scope.spec.name, "input", inputs))
        scope._handles.append(handle)
        return handle

    def finish_io(self, handle: _IOHandle, *, output: object = None,
                  error: BaseException | None = None,
                  outcome_observed: bool = True) -> None:
        self._check_thread()
        if not isinstance(handle, _IOHandle) or handle.scope is not self._active:
            self._reject("IO handle belongs to another stage")
        scope = handle.scope
        scope._check_active()
        if handle.completed or scope.output_ref is not None:
            self._reject("IO may finish exactly once before stage output")
        value = ({"error_type": type(error).__name__, "message": "External call failed",
                  "outcome_observed": outcome_observed}
                 if error is not None else output)
        if error is not None and self.model_required and handle.method in MODEL_IO_METHODS:
            diagnostic = model_failure_diagnostic(error)
            if diagnostic is not None:
                value["diagnostic"] = diagnostic
                if self.upgrade:
                    accounting = model_failure_accounting(error)
                    if accounting is not None:
                        value["accounting"] = accounting
        ref = self._snapshot(scope.spec.name, "output", value)
        scope.io_calls.append({"index": handle.index, "method": handle.method,
                               "input_ref": handle.input_ref,
                               "error_ref" if error is not None else "output_ref": ref})
        handle.completed = True

    def call_io(self, method: str, inputs: object, fn: Callable[[], Any]) -> Any:
        handle = self.begin_io(method, inputs)
        try:
            result = fn()
        except BaseException as original:
            try:
                self.finish_io(handle, error=original)
            except EvidenceWriteError as evidence_error:
                raise evidence_error from original
            raise
        self.finish_io(handle, output=result)
        return result

    def mark_passthrough(self, name: str, reason: str) -> None:
        if self._active is None or self._active.spec.name != name:
            self._reject("Passthrough may mark only its active matching stage")
        self._active.passthrough(reason)

    def assert_pipeline_complete(self) -> None:
        from . import DEFERRED_STEPS, pipeline_registry
        required = [s.name for s in pipeline_registry(self.upgrade) if s.name not in DEFERRED_STEPS]
        actual = [r["stage"] for r in self._records if r["stage"] in required]
        if self._failed or actual != required:
            self._reject("Pipeline subsequence is incomplete")

    def assert_complete(self) -> None:
        self._check_thread()
        if self._failed or self._active is not None or self._cursor != len(self._specs):
            self._reject("Daily execution is not complete")

    def summary(self) -> list[dict]:
        return [dict(record) for record in self._records]


def market_observation(value, *, run_date: date, market_timezone: str):
    """Validate all observed rows, then present date consumers with NY local time.

    Naive daily indices are market-date labels and retain their dates. Aware
    indices denote instants: convert to the admission market before comparing
    or removing the timezone. The raw observation is persisted by the caller
    before this projection; neither its index nor its prices are mutated.
    Option expiry fields are contract dates, not observation dates.
    """
    import pandas as pd

    if isinstance(value, (pd.DataFrame, pd.Series)):
        if value.empty:
            return value
        if not isinstance(value.index, pd.DatetimeIndex) or value.index.hasnans:
            raise StrictObservationError("Provider observation has unverifiable dates")
        index = value.index
        if index.tz is not None:
            index = index.tz_convert(market_timezone).tz_localize(None)
        if any(day > run_date for day in index.date):
            raise StrictObservationError("Provider observation is later than the frozen market date")
        if value.index.tz is not None:
            projected = value.copy()
            projected.index = index
            return projected
        return value
    if isinstance(value, dict):
        projected = {key: market_observation(item, run_date=run_date,
                                             market_timezone=market_timezone)
                     for key, item in value.items()}
        return value if all(projected[key] is item for key, item in value.items()) else projected
    return value


class RecordingProvider:
    """Record public provider calls without changing their internal algorithms."""
    def __init__(self, provider, execution: ExecutionContext):
        from ..providers.base import is_synthetic
        if is_synthetic(provider):
            raise StrictSourceSynthetic("Synthetic provider is forbidden by strict public policy")
        if provider.name not in execution.policy.provider_names:
            raise ExecutionContractError("Provider is outside strict public policy")
        if provider.name == "stooq" and getattr(getattr(provider, "_http", None),
                                                "allow_challenge", True):
            raise ExecutionContractError("Strict Stooq provider must refuse access challenges")
        self._provider = provider
        self.execution = execution
        self.name = provider.name
        self.synthetic = False

    def __getattr__(self, name: str):
        value = getattr(self._provider, name)
        if not callable(value) or name.startswith("_"):
            return value
        def invoke(*args, **kwargs):
            raw = self.execution.call_io(
                f"provider.{self.name}.{name}",
                {"provider": self.name, "method": name, "args": args, "kwargs": kwargs},
                lambda: value(*args, **kwargs),
            )
            try:
                observed = market_observation(raw, run_date=self.execution.run_date,
                                              market_timezone=self.execution.policy.market_timezone)
                if self.execution.upgrade:
                    import pandas as pd
                    def completed_daily(value):
                        if isinstance(value, (pd.DataFrame, pd.Series)):
                            return value[value.index.date <= self.execution.daily_bars_cutoff]
                        if isinstance(value, dict):
                            return {key: completed_daily(item) for key, item in value.items()}
                        return value
                    return completed_daily(observed)
                return observed
            except StrictObservationError as error:
                # A caught boundary violation still poisons this stage before
                # another provider, ledger write, or output can be started.
                self.execution._observation_failure = error
                self.execution._failed = True
                raise
        return invoke


def record_provider(provider, execution: ExecutionContext | None):
    if execution is None:
        return provider
    if isinstance(provider, RecordingProvider):
        if provider.execution is not execution:
            raise ExecutionContractError("Provider belongs to another execution")
        return provider
    return RecordingProvider(provider, execution)
