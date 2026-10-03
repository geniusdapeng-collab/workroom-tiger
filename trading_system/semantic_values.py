"""Shape and numeric guards at each model-output consumer boundary."""

from __future__ import annotations

import math

from .redline import LLMUnavailable


def finite_number(value, lo: float, hi: float) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not lo <= number <= hi:
        return None
    return number


def object_rows(payload, key: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key, []), list):
        raise LLMUnavailable("Model output container does not match the required schema")
    return [row for row in payload.get(key, []) if isinstance(row, dict)]


def text_list(value, maximum: int) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return [item for item in value[:maximum] if item.strip()]
