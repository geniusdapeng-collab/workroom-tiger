"""Shared integer-share sizing: discount risk, cap notional, report actual risk."""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import config
from .parameters import validate_risk_limits


@dataclass(frozen=True)
class PositionSize:
    shares: int
    notional: float
    position_pct: float
    risk: float
    budget: float
    capped: bool


def integer_capacity(allowance: float, per_share: float) -> int:
    """Floor a share budget without losing one share to float roundoff.

    A budget produced as ``shares * per_share`` can divide back to
    ``shares - one ulp``. Only arithmetic error within four budget ulps is
    accepted; a material excess still reduces the share count.
    """
    if (isinstance(allowance, bool) or isinstance(per_share, bool)
            or not math.isfinite(allowance) or allowance < 0
            or not math.isfinite(per_share) or per_share <= 0):
        raise ValueError("Share allowance and unit price must be finite and valid")
    count = math.floor(allowance / per_share)
    following = (count + 1) * per_share
    if following - allowance <= 4 * math.ulp(allowance):
        count += 1
    return count


def size_position(account: float, entry: float, stop: float,
                  size_ratio: float = 1.0, *, risk_r_pct: float | None = None,
                  max_single_position_pct: float | None = None) -> PositionSize:
    for name, value in (("account", account), ("entry", entry), ("stop", stop),
                        ("size_ratio", size_ratio)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if stop >= entry or size_ratio > 1:
        raise ValueError("Long stop must be below entry; size_ratio cannot exceed one")
    risk_r_pct = config.RISK_R_PCT if risk_r_pct is None else risk_r_pct
    max_single_position_pct = (config.MAX_SINGLE_POSITION_PCT
                               if max_single_position_pct is None else max_single_position_pct)
    validate_risk_limits({"risk_r_pct": risk_r_pct,
                          "max_single_position_pct": max_single_position_pct})
    budget = account * risk_r_pct * size_ratio
    uncapped = integer_capacity(budget, entry - stop)
    max_shares = integer_capacity(account * max_single_position_pct, entry)
    shares = min(uncapped, max_shares)
    notional = shares * entry
    return PositionSize(shares, notional, notional / account,
                        shares * (entry - stop), budget, shares < uncapped)
