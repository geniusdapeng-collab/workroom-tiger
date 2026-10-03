"""Immutable run parameters and validation, shared by replay and approval I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace

from . import config


RISK_LIMIT_FIELDS = ("risk_r_pct", "max_single_position_pct", "gross_cap")


def _risk_baseline() -> dict[str, float]:
    return {"risk_r_pct": config.RISK_R_PCT,
            "max_single_position_pct": config.MAX_SINGLE_POSITION_PCT,
            "gross_cap": max(row[3] for row in config.MRS_POSITION_CAP)}


@dataclass(frozen=True)
class GateParams:
    mrs_block: float = field(default_factory=lambda: config.MRS_GATE_BLOCK)
    mrs_gate: float = field(default_factory=lambda: config.OPEN_LONG["mrs"])
    mrs_light_lo: float = field(default_factory=lambda: config.LIGHT_PROBE["mrs_lo"])
    shs_main: float = field(default_factory=lambda: config.SHS_MAIN_POOL)
    shs_sub: float = field(default_factory=lambda: config.SHS_SUB_POOL)
    tss_gate: float = field(default_factory=lambda: config.OPEN_LONG["tss"])
    light_tss: float = field(default_factory=lambda: config.LIGHT_PROBE["tss"])
    light_size: float = field(default_factory=lambda: sum(config.LIGHT_PROBE["size_ratio"]) / 2)
    max_picks: int = field(default_factory=lambda: config.MAX_PICKS_DEFAULT)
    time_stop: int = field(default_factory=lambda: config.TIME_STOP_DAYS[1])
    profit_protect_r: float = field(default_factory=lambda: config.PROFIT_PROTECT_R)
    cost_bps: float = field(default_factory=lambda: config.COST_BPS)
    use_ics: bool = True
    risk_r_pct: float = field(default_factory=lambda: config.RISK_R_PCT)
    max_single_position_pct: float = field(default_factory=lambda: config.MAX_SINGLE_POSITION_PCT)
    gross_cap: float = field(default_factory=lambda: max(row[3] for row in config.MRS_POSITION_CAP))

    def __post_init__(self):
        scores = ("mrs_block", "mrs_gate", "mrs_light_lo", "shs_main", "shs_sub",
                  "tss_gate", "light_tss")
        for name in scores:
            value = getattr(self, name)
            if not _finite(value) or not 0 <= value <= 10:
                raise ValueError(f"{name} must be finite and in [0,10]")
        if self.mrs_gate < self.mrs_block or self.mrs_light_lo < self.mrs_block:
            raise ValueError("Entry thresholds cannot bypass the block threshold")
        if self.mrs_light_lo > self.mrs_gate or self.shs_sub > self.shs_main:
            raise ValueError("Light/sub thresholds cannot exceed standard/main thresholds")
        if not _finite(self.light_size) or not 0 < self.light_size <= 1:
            raise ValueError("light_size must be in (0,1]")
        if not _finite(self.profit_protect_r) or self.profit_protect_r <= 0:
            raise ValueError("profit_protect_r must be finite and positive")
        if not _finite(self.cost_bps) or not 0 <= self.cost_bps < 10_000:
            raise ValueError("cost_bps must be finite and in [0,10000)")
        for name, maximum in (("max_picks", 100), ("time_stop", 252)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [1,{maximum}]")
        if not isinstance(self.use_ics, bool):
            raise ValueError("use_ics must be boolean")
        validate_risk_limits({name: getattr(self, name) for name in RISK_LIMIT_FIELDS},
                             complete=True)


def _finite(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def validate_risk_limits(limits: object, *, complete: bool = False) -> dict[str, float]:
    """Accept only tighter positive policy limits; never change global defaults."""
    if limits is None and not complete:
        return {}
    if not isinstance(limits, dict):
        raise ValueError("Risk limits must be a parameter object")
    names = set(RISK_LIMIT_FIELDS)
    if not set(limits).issubset(names) or complete and set(limits) != names:
        raise ValueError("Risk limits must contain only the three supported policy limits")
    baseline = _risk_baseline()
    for name, value in limits.items():
        if not _finite(value) or not 0 < value <= baseline[name]:
            raise ValueError(f"{name} must be finite, positive and no greater than {baseline[name]}")
    return {name: float(value) for name, value in limits.items()}


def risk_limits_for(params: GateParams) -> dict[str, float]:
    if not isinstance(params, GateParams):
        raise ValueError("Execution parameters must be GateParams")
    return {name: float(getattr(params, name)) for name in RISK_LIMIT_FIELDS}


def tighten_risk_limits(params: GateParams | None = None, *overlays: object) -> GateParams:
    """Combine approved, customer and original-plan limits by taking their minima."""
    params = GateParams() if params is None else params
    effective = risk_limits_for(params)
    for limits in overlays:
        for name, value in validate_risk_limits(limits).items():
            effective[name] = min(effective[name], value)
    return replace(params, **effective)


def validate_tuned_params(params: object) -> dict:
    """Validate a complete override before any caller can use one field."""
    if not isinstance(params, dict) or not params:
        raise ValueError("Recommended parameters must be a non-empty object")
    allowed = {item.name for item in fields(GateParams)}
    if not set(params).issubset(allowed):
        raise ValueError("Unknown parameter names in recommendation")
    validated = dict(params)
    replace(GateParams(), **validated)
    return validated


def holding_limit(atr_pct: float, params: GateParams) -> int:
    """Retain ATR tiers by default; an explicit tuned time stop overrides them."""
    if not _finite(atr_pct) or atr_pct < 0:
        raise ValueError("ATR fraction must be finite and non-negative")
    if params.time_stop != config.TIME_STOP_DAYS[1]:
        return params.time_stop
    return next((days for cap, days in config.TIME_STOP_BY_ATR if atr_pct <= cap),
                params.time_stop)
