"""Prepaid, bounded I/O for the versioned whitepaper research workflow.

Only text Chat Completions, an explicitly selected closed clean Responses
request, native Search and native Fetch are admitted. The
entire serialized text request is counted in UTF-8 bytes (a conservative token
bound), with an additional framing allowance and the full output limit. Actual
usage releases unused reservation; unknown usage freezes further paid calls.
Prices are a frozen upper estimate, not a provider invoice or an FX conversion.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time


class BudgetRefused(RuntimeError):
    code = "REQUEST_BUDGET_REFUSED"


LIMITS = {
    "max_chat_calls": 40, "max_tokens": 300000,
    "max_search_calls": 60, "max_fetch_calls": 20,
    "max_remote_requests": 120, "max_cost_usd": 1.0,
    "deadline_seconds": 2400,
}
LEGACY_BUDGET_POLICY_ID = "tiger.budget-policy.v2"
BUDGET_POLICY_V3 = "tiger.budget-policy.v3"


def policy_limits(policy_id=None) -> dict:
    """Closed source policy registry; reported numeric limits cannot select it."""
    if policy_id is None or type(policy_id) is str and policy_id == LEGACY_BUDGET_POLICY_ID:
        return deepcopy(LIMITS)
    if type(policy_id) is str and policy_id == BUDGET_POLICY_V3:
        return {**LIMITS, "max_tokens": 600000}
    raise BudgetRefused("INVALID_BUDGET_POLICY")


PRICING = {
    "observed_date": "2026-10-27",
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    "model": "deepseek-flash", "token_upper_usd_per_million": 1.2,
    "search_upper_usd_per_request": 0.00075,
    "fetch_upper_usd_per_request": 0.00075,
    "basis": "Peak output price applied to all tokens; every native request charged conservatively.",
    "invoice": False,
}


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise BudgetRefused("INVALID_BUDGET_REQUEST") from exc


def token_upper_bound(request: dict, *, allow_clean_function: bool = False,
                      allow_clean_responses: bool = False,
                      allow_clean_slots: bool = False) -> tuple[int, int]:
    """Text and opted-in fixed clean protocols; no image/file/tool loops."""
    if (not isinstance(request, dict) or type(allow_clean_function) is not bool
            or type(allow_clean_responses) is not bool or type(allow_clean_slots) is not bool):
        raise BudgetRefused("INVALID_BUDGET_REQUEST")
    if allow_clean_slots:
        from .clean_output import clean_slots_responses_request_valid
        if clean_slots_responses_request_valid(request):
            return len(_canonical(request)) + 4096 + 128, request["max_output_tokens"]
        if any(key in request for key in ("input", "instructions", "max_output_tokens", "text")):
            raise BudgetRefused("UNBOUNDED_CHAT_REQUEST")
    if allow_clean_responses:
        from .clean_output import clean_responses_request_valid
        if clean_responses_request_valid(request):
            return len(_canonical(request)) + 4096 + 128, request["max_output_tokens"]
        if any(key in request for key in ("input", "instructions", "max_output_tokens", "text")):
            raise BudgetRefused("UNBOUNDED_CHAT_REQUEST")
    messages, output = request.get("messages"), request.get("max_tokens")
    bounded_function = False
    if allow_clean_function:
        from .clean_output import clean_function_request_valid
        bounded_function = clean_function_request_valid(request)
    if (not isinstance(messages, list) or not messages
            or type(output) is not int or not 1 <= output <= 393216
            or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                   or m["role"] not in {"system", "user"}
                   or not isinstance(m["content"], str) for m in messages)
            or (any(k in request for k in ("tools", "tool_choice", "stream_options")) and not bounded_function)
            or request.get("stream", False) is not False):
        raise BudgetRefused("UNBOUNDED_CHAT_REQUEST")
    # JSON escaping and role/content framing are included, then generously
    # over-reserved. No character/token average or hand-entered estimate is used.
    return len(_canonical(request)) + 4096 + 64 * len(messages), output


class BudgetGuard:
    @classmethod
    def for_policy(cls, policy_id=None, *, clean_output_policy_id=None, clock=time.monotonic):
        """Select only a reviewed version. Legacy constructors remain 300k."""
        limits = policy_limits(policy_id)
        return cls(**limits, clock=clock,
                   _policy_id=BUDGET_POLICY_V3 if policy_id == BUDGET_POLICY_V3 else None,
                   _clean_output_policy_id=clean_output_policy_id)

    def __init__(self, *, max_chat_calls=40, max_tokens=300000,
                 max_search_calls=60, max_fetch_calls=20,
                 max_remote_requests=120, max_cost_usd=1.0,
                 deadline_seconds=2400, clock=time.monotonic, _policy_id=None,
                 _clean_output_policy_id=None):
        if _clean_output_policy_id is not None:
            from .clean_output import (CLEAN_OUTPUT_POLICY_V1, CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                                       CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1)
            if (type(_clean_output_policy_id) is not str or _clean_output_policy_id not in
                    (CLEAN_OUTPUT_POLICY_V1, CLEAN_OUTPUT_RESPONSES_POLICY_V1, CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1)
                    or _policy_id != BUDGET_POLICY_V3):
                raise BudgetRefused("INVALID_BUDGET_POLICY")
        configured = dict(max_chat_calls=max_chat_calls, max_tokens=max_tokens,
                          max_search_calls=max_search_calls, max_fetch_calls=max_fetch_calls,
                          max_remote_requests=max_remote_requests, max_cost_usd=max_cost_usd,
                          deadline_seconds=deadline_seconds)
        ceiling = policy_limits(_policy_id)
        if _policy_id is not None and (_policy_id != BUDGET_POLICY_V3 or configured != ceiling):
            raise BudgetRefused("INVALID_BUDGET_LIMIT")
        for key, value in configured.items():
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0 or value > ceiling[key]
                    or key not in {"max_cost_usd", "deadline_seconds"} and type(value) is not int):
                raise BudgetRefused("INVALID_BUDGET_LIMIT")
        if not callable(clock):
            raise BudgetRefused("INVALID_BUDGET_CLOCK")
        self.limits, self._clock, self._started = configured, clock, clock()
        self.policy_id = _policy_id
        self.clean_output_policy_id = _clean_output_policy_id
        self._records = {}
        self._frozen = False

    def reserve(self, kind: str, call_id: str, request: dict) -> dict:
        if kind not in {"chat", "search", "fetch"} or not isinstance(call_id, str) or not re.fullmatch(r"[a-f0-9]{32}", call_id):
            raise BudgetRefused("INVALID_BUDGET_IDENTITY")
        request_hash = hashlib.sha256(_canonical(request)).hexdigest()
        old = self._records.get(call_id)
        if old:
            if old["reservation"]["kind"] != kind or old["reservation"]["request_sha256"] != request_hash:
                raise BudgetRefused("BUDGET_IDEMPOTENCY_CONFLICT")
            return deepcopy(old["reservation"])
        if self._frozen:
            raise BudgetRefused("UNKNOWN_USAGE_FREEZES_BUDGET")
        if self._clock() - self._started >= self.limits["deadline_seconds"]:
            raise BudgetRefused("BUDGET_DEADLINE")
        snapshot = self.snapshot()
        if (sum(snapshot["counts"].values()) >= self.limits["max_remote_requests"]
                or snapshot["counts"][kind] >= self.limits[f"max_{kind}_calls"]):
            raise BudgetRefused("REQUEST_COUNT_LIMIT")
        if self.clean_output_policy_id is None:
            input_bound, output_bound = token_upper_bound(request) if kind == "chat" else (0, 0)
        else:
            from .clean_output import (CLEAN_OUTPUT_POLICY_V1, CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                                       CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1)
            input_bound, output_bound = token_upper_bound(request,
                allow_clean_function=self.clean_output_policy_id == CLEAN_OUTPUT_POLICY_V1,
                allow_clean_responses=self.clean_output_policy_id == CLEAN_OUTPUT_RESPONSES_POLICY_V1,
                allow_clean_slots=self.clean_output_policy_id == CLEAN_OUTPUT_RESPONSES_SLOTS_POLICY_V1) if kind == "chat" else (0, 0)
        total = input_bound + output_bound
        cost = total * PRICING["token_upper_usd_per_million"] / 1000000 if kind == "chat" else PRICING[f"{kind}_upper_usd_per_request"]
        if snapshot["actual_tokens"] + snapshot["reserved_tokens"] + total > self.limits["max_tokens"]:
            raise BudgetRefused("TOKEN_BUDGET_LIMIT")
        if snapshot["estimated_charged_usd"] + snapshot["estimated_reserved_usd"] + cost > self.limits["max_cost_usd"] + 1e-12:
            raise BudgetRefused("COST_BUDGET_LIMIT")
        reservation = {
            "schema": "tiger.request-budget-reservation.v3" if self.policy_id else "tiger.request-budget-reservation.v2", "call_id": call_id,
            "kind": kind, "sequence": len(self._records) + 1,
            "request_sha256": request_hash,
            "input_token_upper_bound": input_bound, "output_token_upper_bound": output_bound,
            "token_upper_bound": total, "estimated_cost_upper_usd": cost,
            "counts_before": snapshot["counts"], "actual_tokens_before": snapshot["actual_tokens"],
            "reserved_tokens_before": snapshot["reserved_tokens"],
        }
        if self.policy_id is not None:
            reservation["policy_id"] = self.policy_id
        self._records[call_id] = {"reservation": reservation, "status": "reserved", "usage": None}
        return deepcopy(reservation)

    def settle(self, call_id: str, usage: dict | None) -> None:
        record = self._records.get(call_id)
        if record is None:
            raise BudgetRefused("UNRESERVED_IO")
        if record["status"] == "settled":
            if _canonical(record["usage"]) != _canonical(usage):
                raise BudgetRefused("BUDGET_SETTLEMENT_CONFLICT")
            return
        if record["status"] == "unknown":
            raise BudgetRefused("UNKNOWN_USAGE_CANNOT_BE_ERASED")
        reservation = record["reservation"]
        if reservation["kind"] == "chat":
            if (not isinstance(usage, dict)
                    or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
                    or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
                    or usage["prompt_tokens"] > reservation["input_token_upper_bound"]
                    or usage["completion_tokens"] > reservation["output_token_upper_bound"]):
                self.mark_unknown(call_id)
                raise BudgetRefused("USAGE_EXCEEDS_ADMITTED_BOUND")
        elif usage is not None:
            raise BudgetRefused("UNEXPECTED_NATIVE_TOKEN_USAGE")
        record.update(status="settled", usage=deepcopy(usage))

    def mark_unknown(self, call_id: str) -> None:
        if call_id not in self._records:
            raise BudgetRefused("UNRESERVED_IO")
        if self._records[call_id]["status"] != "settled":
            self._records[call_id]["status"] = "unknown"
            self._frozen = True

    def snapshot(self) -> dict:
        counts = {kind: 0 for kind in ("chat", "search", "fetch")}
        actual, reserved, charged_cost, reserved_cost = 0, 0, 0.0, 0.0
        for record in self._records.values():
            reservation = record["reservation"]
            counts[reservation["kind"]] += 1
            if record["status"] == "settled":
                total = record["usage"]["total_tokens"] if record["usage"] is not None else 0
                actual += total
                charged_cost += (total * PRICING["token_upper_usd_per_million"] / 1000000
                                 if reservation["kind"] == "chat" else reservation["estimated_cost_upper_usd"])
            else:
                reserved += reservation["token_upper_bound"]
                reserved_cost += reservation["estimated_cost_upper_usd"]
        return {"schema": "tiger.request-budget.v3" if self.policy_id else "tiger.request-budget.v2",
                **({"policy_id": self.policy_id} if self.policy_id else {}), "limits": deepcopy(self.limits),
                "pricing": deepcopy(PRICING), "counts": counts, "actual_tokens": actual,
                "reserved_tokens": reserved, "estimated_charged_usd": charged_cost,
                "estimated_reserved_usd": reserved_cost, "frozen": self._frozen,
                "records": deepcopy(list(self._records.values()))}
