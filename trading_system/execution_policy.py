"""Versioned, pure paper execution policy shared by live replay and evaluation.

Research creates a fully reserved plan. Only dated observations can turn part of
that plan into a paper receipt. This module performs no I/O and never accepts a
real broker fill. Historical v1 ledgers keep their original execution engine.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
import hashlib
import json
import math

from . import config
from .calendar import next_trading_day, prev_trading_day
from .currency import require_usd_result, require_usd_sim_state, usd_execution_policy
from .gate import pass_gates

POLICY_ID = "whitepaper-execution-v2"
LEGACY_POLICY_ID = "legacy-execution-v1"


class ExecutionPolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _number(value, *, positive=False):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _day(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ExecutionPolicyError("observation_date_invalid") from None
    return value


def _hash(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError):
        raise ExecutionPolicyError("noncanonical_account_or_plan") from None
    return hashlib.sha256(raw).hexdigest()


def _group(record):
    group = record.get("risk_group") or record.get("chain") or (
        "sector:" + record["sector"] if record.get("sector") else "unmapped")
    if not isinstance(group, str) or not group:
        raise ExecutionPolicyError("risk_group_invalid")
    return group


def _quantity(value):
    return type(value) is int and value > 0


def portfolio_snapshot(state: dict, marks: dict, as_of: str) -> dict:
    """Value an exact account using same-date marks, including unfilled plans.

    Pending rows linked to an entry plan are a projection of that plan, so they
    cannot reserve its first tranche twice. Unknown valuations fail closed.
    """
    as_of = _day(as_of)
    if not isinstance(state, dict) or not isinstance(marks, dict):
        raise ExecutionPolicyError("account_state_invalid")
    require_usd_sim_state(state, scope="v2 portfolio snapshot")
    if not _number(state.get("cash")):
        raise ExecutionPolicyError("cash_invalid")
    for key in ("positions", "pending"):
        if not isinstance(state.get(key, []), list):
            raise ExecutionPolicyError("account_records_invalid")
    plans = state.get("entry_plans", {})
    if not isinstance(plans, dict):
        raise ExecutionPolicyError("entry_plans_invalid")
    gross = held_risk = reserved_cash = reserved_risk = 0.
    groups, held, reservations, symbols = {}, {}, {}, set()

    def mark(symbol):
        row = marks.get(symbol)
        if not isinstance(row, dict) or row.get("date") != as_of or not _number(row.get("price"), positive=True):
            raise ExecutionPolicyError("holdings_valuation_unavailable:" + str(symbol))
        return float(row["price"])

    for pos in state.get("positions", []):
        if (not isinstance(pos, dict) or not isinstance(pos.get("ticker"), str)
                or not pos["ticker"] or not _quantity(pos.get("shares"))
                or not _number(pos.get("entry_price"), positive=True)
                or not _number(pos.get("stop"), positive=True)):
            raise ExecutionPolicyError("position_invalid")
        symbol, shares = pos["ticker"], pos["shares"]
        if symbol in held:
            raise ExecutionPolicyError("duplicate_position")
        px, group = mark(symbol), _group(pos)
        value = shares * px
        risk = shares * max(0., max(px, pos["entry_price"]) - pos["stop"])
        gross += value
        held_risk += risk
        groups[group] = groups.get(group, 0.) + risk
        held[symbol] = {"market_value": value, "risk": risk, "shares": shares,
                        "entry_price": pos["entry_price"], "group": group,
                        "plan_id": pos.get("plan_id")}
        symbols.add(symbol)

    def reserve(identity, row, shares):
        nonlocal reserved_cash, reserved_risk
        if (not isinstance(row.get("ticker"), str) or not row["ticker"]
                or type(shares) is not int or shares < 0):
            raise ExecutionPolicyError("pending_quantity_invalid")
        if not shares:
            return
        symbol, group = row["ticker"], _group(row)
        px = mark(symbol)
        ref, stop = row.get("entry_ref"), row.get("initial_stop", row.get("stop_price"))
        if not _number(ref, positive=True) or not _number(stop, positive=True) or stop >= ref:
            raise ExecutionPolicyError("pending_price_invalid")
        price = max(px, ref)
        cash, risk = shares * price, shares * max(0., price - stop)
        reservations[identity] = {"cash": cash, "risk": risk, "group": group,
                                  "shares": shares, "ticker": symbol}
        reserved_cash += cash
        reserved_risk += risk
        groups[group] = groups.get(group, 0.) + risk
        symbols.add(symbol)

    for identity, plan in plans.items():
        if not isinstance(plan, dict) or plan.get("plan_id") != identity:
            raise ExecutionPolicyError("entry_plan_identity_invalid")
        _validate_plan(plan)
        if plan.get("status") in {"cancelled", "closed", "filled"}:
            continue
        if (plan.get("schema") != "tiger.tranche-plan.v2"
                or plan.get("policy") != POLICY_ID
                or type(plan.get("remaining_shares")) is not int
                or plan["remaining_shares"] < 0):
            raise ExecutionPolicyError("entry_plan_invalid")
        reserve(identity, plan, plan["remaining_shares"])
    for pending in state.get("pending", []):
        if not isinstance(pending, dict):
            raise ExecutionPolicyError("pending_invalid")
        identity = pending.get("plan_id")
        if identity:
            if identity not in plans:
                raise ExecutionPolicyError("pending_plan_missing")
            if pending.get("ticker") != plans[identity]["ticker"]:
                raise ExecutionPolicyError("pending_plan_symbol_conflict")
            # A linked row is a view only, including closed/cancelled plans.
            continue
        pending_id = "pending:" + _hash(pending)
        if pending_id in reservations:
            raise ExecutionPolicyError("duplicate_pending_commitment")
        reserve(pending_id, pending, pending.get("shares"))
    nav = float(state["cash"]) + gross
    if not _number(nav, positive=True):
        raise ExecutionPolicyError("portfolio_nav_invalid")
    return {"schema": "tiger.portfolio-snapshot.v2", "known_at": as_of,
            "nav": nav, "cash": float(state["cash"]), "gross": gross,
            "reserved_cash": reserved_cash, "reserved_risk": reserved_risk,
            "held_risk": held_risk, "group_risk": dict(sorted(groups.items())),
            "committed_symbols": sorted(symbols), "held": held,
            "reservations": reservations,
            "state_hash": _hash({"state": state, "marks": marks, "known_at": as_of})}


def entry_gate_reasons(setup: dict, *, thresholds: dict | None = None) -> list[str]:
    if not isinstance(setup, dict):
        return ["entry_setup_missing"]
    reasons = []
    if setup.get("schema") != "tiger.entry-setup.v2": reasons.append("entry_setup_schema_invalid")
    if not isinstance(setup.get("symbol"), str) or not setup["symbol"]:
        reasons.append("symbol_invalid")
    elif usd_execution_policy(tickers=[setup["symbol"]])["execution_blocked"]:
        reasons.append("currency_boundary")
    try:
        _day(setup.get("known_at"))
    except ExecutionPolicyError:
        reasons.append("setup_date_invalid")
    if setup.get("execution_mode") != "paper": reasons.append("paper_only")
    if setup.get("entry_template") not in {"A", "B", "C"}: reasons.append("entry_template_missing")
    for key in ("entry_price", "stop_price"):
        if not _number(setup.get(key), positive=True): reasons.append("invalid_" + key)
    if (_number(setup.get("stop_price"), positive=True) and _number(setup.get("entry_price"), positive=True)
            and setup["stop_price"] >= setup["entry_price"]):
        reasons.append("invalid_entry_or_stop")
    for key in ("mrs_star", "shs", "tss_final"):
        if not _number(setup.get(key)) or setup[key] > 10: reasons.append("invalid_" + key)
    for key in ("in_main", "in_sub", "chain_hot", "shock", "allow_new_positions"):
        if type(setup.get(key)) is not bool: reasons.append("invalid_" + key)
    if setup.get("shock") is True: reasons.append("shock_blocks_new_positions")
    if setup.get("allow_new_positions") is not True: reasons.append("new_positions_disabled")
    breadth = setup.get("breadth")
    if breadth is not None and (not _number(breadth) or breadth > 100):
        reasons.append("invalid_breadth")
    if setup.get("in_main") and (breadth is None or not _number(breadth)
                                or breadth < config.BREADTH_HEALTHY):
        reasons.append("main_pool_breadth_unproven")
    if (breadth is not None and _number(breadth) and breadth < config.BREADTH_NO_CHASE
            and (not _number(setup.get("key_level"), positive=True)
                 or (_number(setup.get("entry_price"), positive=True) and setup["entry_price"] > setup["key_level"]))):
        reasons.append("sector_breadth_no_chase")
    if not _number(setup.get("size_ratio"), positive=True) or setup["size_ratio"] > 1:
        reasons.append("size_ratio_invalid")
    if not _number(setup.get("signal_adv_usd")): reasons.append("signal_adv_invalid")
    if not isinstance(setup.get("risk_group"), str) or not setup["risk_group"]:
        reasons.append("risk_group_invalid")
    if any(reason.startswith("invalid_") for reason in reasons): return list(dict.fromkeys(reasons))
    limits = thresholds or {}
    if setup.get("mrs_star", -1) < limits.get("mrs_block", config.MRS_GATE_BLOCK):
        reasons.append("market_absolute_block")
    decision = pass_gates(setup.get("mrs_star", 0), setup.get("shs", 0), setup.get("tss_final", 0),
        setup.get("in_main", False), setup.get("in_sub", False), setup.get("chain_hot", False),
        **{key: limits[key] for key in ("mrs_gate", "shs_sub", "tss_gate", "light_tss", "mrs_light_lo") if key in limits})
    if not decision.passed:
        reasons.append("numeric_gates_failed")
        if not thresholds:
            from .activity import gate_reasons
            reasons.extend(gate_reasons(setup.get("mrs_star", 0), setup.get("shs", 0), setup.get("tss_final", 0),
                in_main=setup.get("in_main", False), in_sub=setup.get("in_sub", False), chain_hot=setup.get("chain_hot", False)))
    # The lower-size legal channel must never receive a standard risk amount.
    if (decision.passed and not decision.standard and _number(setup.get("size_ratio"), positive=True)
            and setup["size_ratio"] > max(config.LIGHT_PROBE["size_ratio"])):
        reasons.append("probe_size_exceeded")
    return list(dict.fromkeys(reasons))


def _budget_reason(snapshot, setup, budget):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "tiger.portfolio-snapshot.v2":
        return "portfolio_snapshot_missing"
    if not isinstance(budget, dict) or budget.get("schema") != "tiger.entry-budget.v2" or budget.get("execution_policy") != POLICY_ID:
        return "signal_budget_missing"
    if budget.get("snapshot_hash") != snapshot.get("state_hash"): return "snapshot_hash_mismatch"
    if budget.get("known_at") != snapshot.get("known_at") or setup.get("known_at") != snapshot.get("known_at"):
        return "valuation_date_mismatch"
    problem = _budget_limits_reason(budget)
    if problem: return problem
    market_cap = next((row[3] for row in config.MRS_POSITION_CAP
                       if row[0] <= setup["mrs_star"] < row[1]), 0.)
    if budget["gross_cap"] > market_cap + 1e-12:
        return "market_gross_budget_exceeded"
    return ""


def _budget_limits_reason(budget):
    if (not isinstance(budget, dict) or budget.get("schema") != "tiger.entry-budget.v2"
            or budget.get("execution_policy") != POLICY_ID): return "signal_budget_missing"
    identity = budget.get("snapshot_hash")
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        return "snapshot_hash_invalid"
    for name, maximum in (("gross_cap", max(row[3] for row in config.MRS_POSITION_CAP)),
                          ("single_cap", config.MAX_SINGLE_POSITION_PCT),
                          ("risk_pct", config.RISK_R_PCT),
                          ("group_risk_pct", config.MAX_CHAIN_RISK_PCT),
                          ("portfolio_risk_pct", config.MAX_PORTFOLIO_RISK_PCT_V2)):
        if not _number(budget.get(name), positive=True) or budget[name] > maximum + 1e-12:
            return "signal_budget_invalid"
    if type(budget.get("max_positions")) is not int or not 1 <= budget["max_positions"] <= config.MAX_POSITIONS_V2:
        return "signal_budget_invalid"
    return ""


def allocate_entry(snapshot: dict, setup: dict, budget: dict, requested_shares: int | None = None,
                   *, exclude_plan_id: str | None = None) -> dict:
    refused = lambda code: {"shares": 0, "reason": code, "snapshot_hash": snapshot.get("state_hash") if isinstance(snapshot, dict) else None}
    reasons = entry_gate_reasons(setup)
    if reasons: return refused(reasons[0])
    problem = _budget_reason(snapshot, setup, budget)
    if problem: return refused(problem)
    if requested_shares is not None and not _quantity(requested_shares): return refused("signal_quantity_invalid")
    symbol, group, price, stop = setup["symbol"], setup["risk_group"], setup["entry_price"], setup["stop_price"]
    reservations = snapshot["reservations"]
    own = reservations.get(exclude_plan_id, {}) if exclude_plan_id else {}
    if exclude_plan_id and (not own or own.get("ticker") != symbol): return refused("own_reservation_missing")
    held = snapshot["held"].get(symbol, {})
    if symbol in snapshot["committed_symbols"] and not own: return refused("already_committed")
    if not own and len(snapshot["committed_symbols"]) >= budget["max_positions"]: return refused("position_slots_exhausted")
    nav, risk_ps = snapshot["nav"], price - stop
    reserved_cash = max(0., snapshot["reserved_cash"] - own.get("cash", 0.))
    reserved_risk = max(0., snapshot["reserved_risk"] - own.get("risk", 0.))
    group_before = snapshot["group_risk"].get(group, 0.) - (own.get("risk", 0.) if own.get("group") == group else 0.)
    single_reserved = sum(row["cash"] for key, row in reservations.items()
                          if row["ticker"] == symbol and key != exclude_plan_id)
    risk_cap = min(budget["risk_pct"], config.RISK_R_PCT * setup["size_ratio"])
    limits = {
        "cash": (snapshot["cash"] - reserved_cash) / price,
        "gross": (nav * budget["gross_cap"] - snapshot["gross"] - reserved_cash) / price,
        "single_position": (nav * budget["single_cap"] - held.get("market_value", 0.) - single_reserved) / price,
        "single_risk": (nav * risk_cap - held.get("risk", 0.)) / risk_ps,
        "group_risk": (nav * budget["group_risk_pct"] - group_before) / risk_ps,
        "portfolio_risk": (nav * budget["portfolio_risk_pct"] - snapshot["held_risk"] - reserved_risk) / risk_ps,
    }
    quantities = {key: max(0, math.floor(value + 1e-10)) for key, value in limits.items()}
    if requested_shares is not None: quantities["requested"] = requested_shares
    shares = min(quantities.values())
    if shares <= 0: return refused(min(quantities, key=quantities.get) + "_budget_exhausted")
    cost, risk = shares * price, shares * risk_ps
    return {"shares": shares, "reason": "accepted", "risk_usd": risk,
            "position_pct": (held.get("market_value", 0.) + cost) / nav,
            "nav": nav, "group_risk_after": (group_before + risk) / nav,
            "gross_after": (snapshot["gross"] + reserved_cash + cost) / nav,
            "portfolio_risk_after": (snapshot["held_risk"] + reserved_risk + risk) / nav,
            "snapshot_hash": snapshot["state_hash"], "risk_group": group}


def _compact_plan_budget(budget: dict) -> dict:
    """Keep numeric admission facts without recursively embedding old plans.

    The exact full budget and source account remain in this run's sealed cohort
    evidence. A durable plan binds those inputs by hash and retains the actual
    dated snapshot and limits needed for execution. Existing full-source plans
    remain valid; their original frozen contracts are never rewritten on read.
    """
    if not isinstance(budget, dict):
        raise ExecutionPolicyError("signal_budget_missing")
    out = deepcopy(budget)
    for key in ("source_budget_sha256", "source_account_state_sha256"):
        identity = out.get(key)
        if identity is not None and (not isinstance(identity, str) or len(identity) != 64
                or identity == "0" * 64 or any(char not in "0123456789abcdef" for char in identity)):
            raise ExecutionPolicyError("plan_source_hash_invalid")
    if "source_account_state" in out:
        source = out.pop("source_account_state")
        account_hash = _hash(source)
        if out.get("source_account_state_sha256", account_hash) != account_hash:
            raise ExecutionPolicyError("plan_source_account_hash_conflict")
        out["source_account_state_sha256"] = account_hash
        out["source_budget_sha256"] = _hash(budget)
    elif "source_budget_sha256" not in out:
        out["source_budget_sha256"] = _hash(budget)
    return out


def _tranche_quantities(total):
    raw = [total * weight for weight in (.4, .4, .2)]
    counts = [math.floor(value) for value in raw]
    for index in sorted(range(3), key=lambda i: (-(raw[i] - counts[i]), i))[:total - sum(counts)]:
        counts[index] += 1
    return counts


def create_tranche_plan(pick, frozen_budget: dict, signal_date: str, *, setup: dict | None = None) -> dict:
    pick = asdict(pick) if is_dataclass(pick) else deepcopy(pick)
    signal_date = _day(signal_date)
    if not isinstance(pick, dict) or not _quantity(pick.get("shares")): raise ExecutionPolicyError("signal_quantity_invalid")
    if (entry_gate_reasons(setup) or not isinstance(setup.get("known_at"), str)
            or setup["known_at"] > signal_date
            or pick.get("ticker") != setup.get("symbol")
            or pick.get("entry_template") != setup.get("entry_template")
            or pick.get("entry_price") != setup.get("entry_price")
            or pick.get("stop_price") != setup.get("stop_price")):
        raise ExecutionPolicyError("frozen_setup_invalid")
    if (not isinstance(frozen_budget, dict) or frozen_budget.get("schema") != "tiger.entry-budget.v2"
            or frozen_budget.get("execution_policy") != POLICY_ID
            or frozen_budget.get("known_at") != setup["known_at"]):
        raise ExecutionPolicyError("signal_budget_missing")
    problem = _budget_limits_reason(frozen_budget)
    if problem: raise ExecutionPolicyError(problem)
    market_cap = next((row[3] for row in config.MRS_POSITION_CAP
                       if row[0] <= setup["mrs_star"] < row[1]), 0.)
    if frozen_budget["gross_cap"] > market_cap + 1e-12:
        raise ExecutionPolicyError("market_gross_budget_exceeded")
    # The source account contains earlier plans. Storing it inside every new
    # plan would copy all old frozen budgets twice and grow account history
    # exponentially. Compact only that durable reference, not the cohort proof.
    frozen_budget = _compact_plan_budget(frozen_budget)
    total = pick["shares"]
    deadline = pick.get("time_stop_days") or config.TIME_STOP_DAYS[1]
    if type(deadline) is not int or not 5 <= deadline <= 7:
        raise ExecutionPolicyError("plan_time_stop_invalid")
    counts = _tranche_quantities(total)
    frozen = {"pick": pick, "setup": setup, "budget": frozen_budget, "signal_date": signal_date}
    identity = _hash(frozen)
    return {"schema": "tiger.tranche-plan.v2", "policy": POLICY_ID, "plan_id": identity,
            "ticker": pick["ticker"], "signal_date": signal_date, "valuation_date": setup["known_at"],
            "first_execution_date": next_trading_day(date.fromisoformat(signal_date)).isoformat(),
            "total_shares": total, "filled_shares": 0, "remaining_shares": total,
            "risk_group": setup["risk_group"], "sector": setup.get("sector", ""),
            "chain": pick.get("chain", ""), "initial_stop": pick["stop_price"],
            "entry_ref": pick["entry_price"], "entry_template": pick["entry_template"],
            "time_stop_days": deadline,
            "budget": deepcopy(frozen_budget), "setup": deepcopy(setup), "status": "pending",
            "tranches": [{"index": i, "weight": (.4, .4, .2)[i], "shares": count,
                          "filled_shares": 0, "status": "skipped" if not count else "pending"}
                         for i, count in enumerate(counts)], "fill_receipts": [], "frozen_contract": deepcopy(frozen)}


def _validate_plan(plan):
    if (not isinstance(plan, dict) or plan.get("schema") != "tiger.tranche-plan.v2"
            or plan.get("policy") != POLICY_ID or plan.get("status") not in {"pending", "partial", "filled", "cancelled", "closed"}
            or not _quantity(plan.get("total_shares"))
            or any(type(plan.get(key, 0)) is not int or plan.get(key, 0) < 0
                   for key in ("filled_shares", "remaining_shares", "cancelled_shares"))
            or plan["filled_shares"] + plan["remaining_shares"] + plan.get("cancelled_shares", 0) != plan["total_shares"]
            or not isinstance(plan.get("tranches"), list) or len(plan["tranches"]) != 3
            or not isinstance(plan.get("fill_receipts"), list)):
        raise ExecutionPolicyError("entry_plan_invalid")
    frozen = plan.get("frozen_contract")
    try:
        if (not isinstance(frozen, dict) or set(frozen) != {"pick", "setup", "budget", "signal_date"}
                or not isinstance(frozen["pick"], dict) or not isinstance(frozen["setup"], dict)
                or not isinstance(frozen["budget"], dict) or _hash(frozen) != plan.get("plan_id")):
            raise ValueError
        pick, setup = frozen["pick"], frozen["setup"]
        immutable = {"ticker": pick["ticker"], "total_shares": pick["shares"],
            "initial_stop": pick["stop_price"], "entry_ref": pick["entry_price"],
            "entry_template": pick["entry_template"], "setup": setup, "budget": frozen["budget"],
            "signal_date": _day(frozen["signal_date"]), "valuation_date": _day(setup["known_at"]),
            "first_execution_date": next_trading_day(date.fromisoformat(frozen["signal_date"])).isoformat(),
            "risk_group": setup["risk_group"], "sector": setup.get("sector", ""),
            "chain": pick.get("chain", ""),
            "time_stop_days": pick.get("time_stop_days") or config.TIME_STOP_DAYS[1]}
        if any(plan.get(key) != value for key, value in immutable.items()):
            raise ValueError
        if (type(plan["time_stop_days"]) is not int or not 5 <= plan["time_stop_days"] <= 7
                or not _number(plan["entry_ref"], positive=True) or not _number(plan["initial_stop"], positive=True)
                or plan["initial_stop"] >= plan["entry_ref"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ExecutionPolicyError("frozen_plan_contract_changed") from None
    total, filled, remaining = plan["total_shares"], plan["filled_shares"], plan["remaining_shares"]
    cancelled = plan.get("cancelled_shares", 0)
    status = plan["status"]
    if ((status == "pending" and (filled or cancelled or remaining != total))
            or (status == "partial" and (not 0 < filled < total or not remaining or cancelled))
            or (status == "filled" and (remaining or cancelled or filled != total))
            or (status in {"cancelled", "closed"} and remaining)
            or (status == "cancelled" and not cancelled)
            or (status == "closed" and not filled)):
        raise ExecutionPolicyError("entry_plan_status_conflict")
    active_remaining = cancelled_by_tranche = filled_by_tranche = 0
    earlier_unfilled = False
    counts = _tranche_quantities(total)
    for index, row in enumerate(plan["tranches"]):
        if (not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index
                or row.get("weight") != (.4, .4, .2)[index] or type(row.get("shares")) is not int
                or row["shares"] != counts[index] or type(row.get("filled_shares")) is not int
                or not 0 <= row["filled_shares"] <= row["shares"]):
            raise ExecutionPolicyError("entry_plan_tranches_conflict")
        quantity, executed, row_status = row["shares"], row["filled_shares"], row.get("status")
        if quantity == 0:
            if executed or row_status != "skipped":
                raise ExecutionPolicyError("entry_plan_tranches_conflict")
            continue
        if earlier_unfilled and executed:
            raise ExecutionPolicyError("entry_plan_tranches_conflict")
        earlier_unfilled = earlier_unfilled or executed < quantity
        expected = ("filled" if executed == quantity else "cancelled" if status in {"cancelled", "closed"}
                    else "partial" if executed else "pending")
        if row_status != expected:
            raise ExecutionPolicyError("entry_plan_tranches_conflict")
        filled_by_tranche += executed
        if row_status == "cancelled": cancelled_by_tranche += quantity - executed
        elif row_status != "filled": active_remaining += quantity - executed
    if (filled_by_tranche != filled or active_remaining != remaining or cancelled_by_tranche != cancelled):
        raise ExecutionPolicyError("entry_plan_tranches_conflict")
    fill_ids, intent_ids, receipt_shares, previous_date = set(), set(), 0, ""
    for row in plan["fill_receipts"]:
        if (not isinstance(row, dict) or row.get("execution_mode") != "paper"
                or row.get("plan_id") != plan["plan_id"] or not _quantity(row.get("shares"))
                or not isinstance(row.get("fill_id"), str) or not row["fill_id"] or row["fill_id"] in fill_ids
                or not isinstance(row.get("intent_id"), str) or not row["intent_id"] or row["intent_id"] in intent_ids
                or not _number(row.get("price"), positive=True) or row["price"] <= plan["initial_stop"]
                or not _number(row.get("friction", 0))):
            raise ExecutionPolicyError("entry_plan_receipts_conflict")
        observed = _day(row.get("date"))
        if observed < plan["first_execution_date"] or observed <= previous_date:
            raise ExecutionPolicyError("entry_plan_receipts_conflict")
        previous_date = observed
        fill_ids.add(row["fill_id"]); intent_ids.add(row["intent_id"])
        receipt_shares += row["shares"]
    if receipt_shares != filled:
        raise ExecutionPolicyError("entry_plan_receipts_conflict")


def next_tranche(plan: dict, snapshot: dict, quote: dict, fresh_setup: dict) -> dict:
    _validate_plan(plan)
    refused = lambda code: {"shares": 0, "reason": code, "plan_id": plan.get("plan_id")}
    if plan.get("policy") != POLICY_ID or plan.get("status") in {"cancelled", "closed", "filled"}: return refused("plan_terminal")
    observed = quote.get("known_at") if isinstance(quote, dict) else None
    if (not isinstance(quote, dict) or observed != quote.get("date") or observed != snapshot.get("known_at")
            or fresh_setup.get("known_at") != observed or not _number(quote.get("price"), positive=True)
            or not isinstance(quote.get("evidence_id"), str) or not quote["evidence_id"]):
        return refused("quote_evidence_invalid")
    if observed < plan["first_execution_date"]: return refused("execution_not_due")
    next_row = next((row for row in plan["tranches"] if row["filled_shares"] < row["shares"]), None)
    if next_row is None: return refused("plan_filled")
    index = next_row["index"]
    if not plan["fill_receipts"]:
        if observed > plan["first_execution_date"]: return refused("execution_window_missed")
        if quote.get("kind") != "open": return refused("opening_observation_required")
    else:
        last = plan["fill_receipts"][-1]
        if observed <= last["date"]: return refused("later_confirmation_required")
        if quote.get("kind") != "confirmation": return refused("later_confirmation_required")
        if quote["price"] <= max(last["price"], plan.get("entry_vwap", last["price"])): return refused("averaging_down_forbidden")
        if (fresh_setup.get("mrs_star", -1) < plan["setup"]["mrs_star"]
                or fresh_setup.get("shs", -1) < plan["setup"]["shs"]
                or (plan["setup"]["in_main"] and not fresh_setup.get("in_main"))):
            return refused("market_or_sector_degraded")
    setup = {**fresh_setup, "entry_price": quote["price"], "stop_price": plan["initial_stop"]}
    reasons = entry_gate_reasons(setup)
    if reasons: return refused(reasons[0])
    # Budget ceilings remain frozen; only its account identity/date is rebound
    # after a fresh full-account valuation. No client-provided enlarged cap.
    budget = {**plan["budget"], "known_at": observed, "snapshot_hash": snapshot["state_hash"]}
    wanted = next_row["shares"] - next_row["filled_shares"]
    allocation = allocate_entry(snapshot, setup, budget, wanted, exclude_plan_id=plan["plan_id"])
    if not allocation["shares"]: return refused(allocation["reason"])
    fields = {"plan_id": plan["plan_id"], "tranche_index": index, "shares": allocation["shares"],
              "price_limit": quote["price"], "known_at": observed, "quote_evidence_id": quote["evidence_id"],
              "snapshot_hash": snapshot["state_hash"], "execution_mode": "paper", "reason": "accepted"}
    return {**fields, "intent_id": _hash(fields)}


def apply_paper_fill(plan: dict, intent: dict, receipt: dict) -> dict:
    """Return the new plan after an admitted, unique paper receipt."""
    _validate_plan(plan)
    if not isinstance(receipt, dict) or not isinstance(intent, dict): raise ExecutionPolicyError("fill_receipt_invalid")
    for old in plan.get("fill_receipts", []):
        if old.get("fill_id") == receipt.get("fill_id"):
            if old == receipt: return deepcopy(plan)
            raise ExecutionPolicyError("fill_identity_conflict")
        if old.get("intent_id") == receipt.get("intent_id"):
            raise ExecutionPolicyError("intent_already_filled")
    if (plan.get("status") in {"cancelled", "closed", "filled"}
            or plan.get("policy") != POLICY_ID or intent.get("execution_mode") != "paper"
            or receipt.get("execution_mode") != "paper"
            or receipt.get("intent_id") != intent.get("intent_id")
            or receipt.get("plan_id") != plan.get("plan_id") or intent.get("plan_id") != plan.get("plan_id")
            or receipt.get("date") != intent.get("known_at")
            or not isinstance(receipt.get("fill_id"), str) or not receipt["fill_id"]
            or not _quantity(receipt.get("shares")) or receipt["shares"] > intent.get("shares", 0)
            or not _number(receipt.get("price"), positive=True) or receipt["price"] > intent.get("price_limit", 0)
            or receipt["price"] <= plan["initial_stop"] or not _number(receipt.get("friction", 0))):
        raise ExecutionPolicyError("fill_receipt_invalid")
    out = deepcopy(plan)
    index = intent.get("tranche_index")
    if type(index) is not int or not 0 <= index < 3: raise ExecutionPolicyError("fill_tranche_invalid")
    tranche = out["tranches"][index]
    if receipt["shares"] > tranche["shares"] - tranche["filled_shares"]: raise ExecutionPolicyError("tranche_overfill")
    quantity = receipt["shares"]
    cost = out.get("entry_vwap", 0.) * out["filled_shares"] + receipt["price"] * quantity
    out["filled_shares"] += quantity
    out["remaining_shares"] -= quantity
    out["entry_vwap"] = cost / out["filled_shares"]
    tranche["filled_shares"] += quantity
    tranche["status"] = "filled" if tranche["filled_shares"] == tranche["shares"] else "partial"
    out["status"] = "filled" if not out["remaining_shares"] else "partial"
    out["fill_receipts"].append(deepcopy(receipt))
    _validate_plan(out)
    return out


def _bar_dict(value):
    if value is None: return None
    row = asdict(value) if is_dataclass(value) else deepcopy(value)
    if not isinstance(row, dict): raise ExecutionPolicyError("market_bar_invalid")
    _day(row.get("date"))
    values = [row.get(key) for key in ("open", "high", "low", "close")]
    if (any(not _number(value, positive=True) for value in values)
            or row["high"] < max(row["open"], row["low"], row["close"])
            or row["low"] > min(row["open"], row["high"], row["close"])):
        raise ExecutionPolicyError("market_bar_invalid")
    if not _number(row.get("adv", 0)): raise ExecutionPolicyError("market_adv_invalid")
    return row


def _result_setup(result, ticker):
    raw = result.raw
    row = (raw.get("execution_setups") or {}).get(ticker)
    if row is None:
        detail = (raw.get("pick_rationale") or {}).get(ticker, {})
        row = detail.get("execution_setup") or detail.get("entry_setup")
    return deepcopy(row) if isinstance(row, dict) and row.get("schema") == "tiger.entry-setup.v2" else None


def _cancel(plan, reason, *, closed=False):
    plan["cancelled_shares"] = plan.get("cancelled_shares", 0) + plan["remaining_shares"]
    plan["remaining_shares"] = 0
    plan["status"] = "closed" if closed else "cancelled"
    plan["terminal_reason"] = reason
    for row in plan["tranches"]:
        if row["filled_shares"] < row["shares"]: row["status"] = "cancelled"
    plan.pop("confirmation", None)


def step_paper_account(state: dict, trade_date: str, result, bars: dict) -> dict:
    """Pure T+1 replay. Opening fills precede today's exits and new research.

    Daily additions require a complete confirmation frozen on a PREVIOUS bar.
    A later intraday caller may use next_tranche with a dated observed quote;
    daily OHLC never invents the order of intrabar events or fills at its high.
    """
    from .exit_engine import evaluate_day
    from .simulator import friction_bps

    _day(trade_date)
    require_usd_result(result, scope="v2 paper result")
    require_usd_sim_state(state, scope="v2 paper account")
    if result.raw.get("execution_policy") != POLICY_ID: raise ExecutionPolicyError("v2_policy_required")
    if not _number(state.get("cash")): raise ExecutionPolicyError("cash_invalid")
    if any(not isinstance(state.get(key, []), list) for key in ("positions", "pending", "closed")):
        raise ExecutionPolicyError("account_records_invalid")
    if any(row.get("plan_id") is None for row in state.get("pending", [])):
        raise ExecutionPolicyError("legacy_pending_requires_legacy_policy")
    observed = {symbol: _bar_dict(bar) for symbol, bar in bars.items() if bar is not None}
    if any(bar["date"] > trade_date for bar in observed.values()): raise ExecutionPolicyError("future_market_bar")
    s = deepcopy(state)
    s.setdefault("entry_plans", {})
    s.setdefault("closed", [])
    s.setdefault("pending", [])
    s.setdefault("equity_curve", [])
    s.setdefault("ops_log", [])
    s["started"] = s.get("started") or trade_date
    fills, closed, refusals, events, ops = [], [], [], [], []

    def marks_for(day, column):
        return {symbol: {"date": bar["date"], "price": bar[column]}
                for symbol, bar in observed.items() if bar["date"] == day}

    # Opening admission cannot consume research produced at today's close.
    for identity, plan in list(s["entry_plans"].items()):
        _validate_plan(plan)
        if plan["status"] in {"cancelled", "closed", "filled"}: continue
        bar = observed.get(plan["ticker"])
        if bar is None: continue
        day = bar["date"]
        if day < plan["first_execution_date"]: continue
        if not plan["fill_receipts"] and day > plan["first_execution_date"]:
            refusals.append({"ticker": plan["ticker"], "plan_id": identity, "date": day,
                             "shares": 0, "reason": "execution_window_missed"})
            _cancel(plan, "execution_window_missed")
            continue
        first = not plan["fill_receipts"]
        if first and bar["open"] <= plan["initial_stop"]:
            refusals.append({"ticker": plan["ticker"], "plan_id": identity, "date": day,
                             "shares": 0, "reason": "opening_invalidates_initial_stop"})
            _cancel(plan, "opening_invalidates_initial_stop")
            continue
        existing = next((row for row in s["positions"] if row["ticker"] == plan["ticker"]), None)
        if not first and existing is not None and bar["open"] <= existing["stop"]:
            # The prior protective stop is already known at this opening. Do
            # not add just to immediately stop those new shares. Original
            # shares exit in the later loop; no cash is released early here.
            refusals.append({"ticker": plan["ticker"], "plan_id": identity, "date": day,
                             "shares": 0, "reason": "current_stop_blocks_add"})
            plan.pop("confirmation", None)
            plan["last_add_attempt"] = day
            continue
        if not first and plan["fill_receipts"][-1]["date"] >= day: continue
        if not first and plan.get("last_add_attempt") == day: continue
        confirmation = plan.get("confirmation") if not first else None
        if not first:
            previous_day = prev_trading_day(date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if (not isinstance(confirmation, dict) or confirmation.get("known_at") != previous_day
                    or confirmation.get("research_date", confirmation.get("known_at")) != previous_day):
                if confirmation is not None:
                    refusals.append({"ticker": plan["ticker"], "plan_id": identity, "date": day,
                                     "shares": 0, "reason": "previous_complete_day_confirmation_required"})
                    plan.pop("confirmation", None)
                continue
            # A confirmation admits one later opening attempt. It cannot also
            # fund another tranche after a newer research round was empty.
            plan.pop("confirmation", None)
        evidence = plan["setup"] if first else confirmation.get("setup")
        if not evidence: continue
        bps = friction_bps(plan["setup"]["signal_adv_usd"])
        net = round(bar["open"] * (1 + bps / 10_000), 4)
        fresh = {**evidence, "known_at": day, "evidence_known_at": evidence["known_at"], "entry_price": net}
        try:
            snap = portfolio_snapshot(s, marks_for(day, "open"), day)
        except ExecutionPolicyError as exc:
            refusals.append({"ticker": plan["ticker"], "plan_id": identity, "date": day,
                             "shares": 0, "reason": exc.code})
            if first: _cancel(plan, exc.code)
            continue
        quote = {"date": day, "known_at": day, "price": net,
                 "kind": "open" if first else "confirmation",
                 "evidence_id": _hash({"ticker": plan["ticker"], "bar": bar, "kind": "opening"})}
        intent = next_tranche(plan, snap, quote, fresh)
        plan["last_add_attempt"] = day
        if not intent["shares"]:
            refusals.append({"ticker": plan["ticker"], "date": day, **intent})
            if first: _cancel(plan, intent["reason"])
            continue
        receipt = {"fill_id": "paper:" + intent["intent_id"], "intent_id": intent["intent_id"],
                   "plan_id": identity, "date": day, "shares": intent["shares"],
                   "price": net, "friction": intent["shares"] * (net - bar["open"]),
                   "execution_mode": "paper", "entry_raw": bar["open"], "friction_bps": bps}
        next_plan = apply_paper_fill(plan, intent, receipt)
        s["entry_plans"][identity] = next_plan
        cost = receipt["shares"] * net
        if cost > s["cash"] + 1e-8: raise AssertionError("v2 fill exceeded available cash")
        s["cash"] = round(s["cash"] - cost, 8)
        pos = next((row for row in s["positions"] if row["ticker"] == plan["ticker"]), None)
        if pos is None:
            pos = {"ticker": plan["ticker"], "shares": 0, "entry_price": 0.,
                   "entry_raw": 0., "entry_date": day, "initial_stop": plan["initial_stop"],
                   "stop": plan["initial_stop"], "risk_group": plan["risk_group"],
                   "chain": plan["chain"], "sector": plan["sector"], "plan_id": identity,
                   "execution_policy": POLICY_ID, "time_stop_days": plan["time_stop_days"],
                   "bars_held": 0, "protected": False, "entry_friction_cost": 0.,
                   "friction_bps": bps, "source": getattr(result, "provider", "")}
            sector_bar = observed.get(plan["sector"])
            if sector_bar and sector_bar["date"] == day: pos["sector_entry_price"] = sector_bar["open"]
            s["positions"].append(pos)
        old = pos["shares"]
        if pos.get("plan_id") != identity: raise ExecutionPolicyError("position_plan_conflict")
        pos["shares"] += receipt["shares"]
        pos["entry_price"] = (pos["entry_price"] * old + cost) / pos["shares"]
        pos["entry_raw"] = (pos["entry_raw"] * old + bar["open"] * receipt["shares"]) / pos["shares"]
        pos["entry_friction_cost"] += receipt["friction"]
        pos["initial_risk_usd"] = pos["shares"] * (pos["entry_price"] - pos["initial_stop"])
        pos["risk_usd"] = pos["initial_risk_usd"]
        pos["last_fill_price"] = net
        fills.append(receipt)
        ops.append(f"纸盘第 {intent['tranche_index'] + 1} 段买入 {plan['ticker']} {receipt['shares']} 股")

    # Distinct observed complete bars advance the original holding clock.
    kept = []
    for pos in s["positions"]:
        bar = observed.get(pos["ticker"])
        if bar is None or bar["date"] < pos["entry_date"] or bar["date"] <= pos.get("last_bar_date", ""):
            kept.append(pos)
            continue
        days = pos.get("bars_held", 0) + 1
        initial_stop = pos.get("initial_stop", pos["stop"])
        sector_return = None
        sector_bar = observed.get(pos.get("sector"))
        if sector_bar and sector_bar["date"] == bar["date"] and _number(pos.get("sector_entry_price"), positive=True):
            sector_return = sector_bar["close"] / pos["sector_entry_price"] - 1
        policy = pos.get("execution_policy", LEGACY_POLICY_ID)
        action = evaluate_day(bar["open"], bar["high"], bar["low"], bar["close"],
            pos["entry_price"], pos["stop"], days, time_stop=pos.get("time_stop_days") or 7,
            policy_id=policy, initial_stop=initial_stop, protected=pos.get("protected", False),
            sector_return=sector_return)
        if action.action == "exit":
            bps = friction_bps(bar["adv"]) if bar.get("adv", 0) > 0 else pos.get("friction_bps", config.COST_BPS)
            net = round(action.exit_price * (1 - bps / 10_000), 4)
            quantity = pos["shares"]
            pnl = round((net - pos["entry_price"]) * quantity, 8)
            gross = round((action.exit_price - pos.get("entry_raw", pos["entry_price"])) * quantity, 8)
            friction = round(gross - pnl, 8)
            initial_risk = pos.get("initial_risk_usd", pos.get("risk_usd", quantity * (pos["entry_price"] - initial_stop)))
            record = {"ticker": pos["ticker"], "plan_id": pos.get("plan_id"), "execution_policy": policy,
                      "entry_date": pos["entry_date"], "exit_date": bar["date"], "entry": pos["entry_price"],
                      "exit": net, "shares": quantity, "pnl_usd": pnl, "gross_pnl": gross,
                      "friction_cost": friction, "entry_friction_cost": pos.get("entry_friction_cost", 0.),
                      "exit_friction_cost": (action.exit_price - net) * quantity,
                      "r_multiple": pnl / initial_risk, "net_r": pnl / initial_risk,
                      "gross_r": gross / initial_risk, "initial_risk_usd": initial_risk,
                      "initial_stop": initial_stop, "entry_raw": pos.get("entry_raw", pos["entry_price"]),
                      "exit_raw": action.exit_price, "reason": action.reason, "days": days,
                      "source": getattr(result, "provider", ""), "account_kind": "paper"}
            s["cash"] = round(s["cash"] + quantity * net, 8)
            s["closed"].append(record)
            closed.append(record)
            if pos.get("plan_id") in s["entry_plans"]: _cancel(s["entry_plans"][pos["plan_id"]], action.reason, closed=True)
            ops.append(f"纸盘卖出 {pos['ticker']} {quantity} 股：{action.reason}")
            continue
        if action.action == "protect":
            pos["stop"] = max(pos["stop"], action.next_stop if policy == POLICY_ID else pos["entry_price"])
            pos["protected"] = True
            events.append({"kind": "PROTECT_APPLIED", "ticker": pos["ticker"], "date": bar["date"],
                           "stop": pos["stop"], "account_kind": "paper"})
        pos.update(last_bar_date=bar["date"], bars_held=days, last_price=bar["close"],
                   peak_price=max(pos.get("peak_price", 0.), bar["high"]))
        kept.append(pos)
    s["positions"] = kept

    # Today's close can authorize a NEXT opening only, never today's fill.
    for plan in s["entry_plans"].values():
        if plan["status"] not in {"partial", "pending"} or not plan["fill_receipts"]: continue
        if "tranche_confirmations" in result.raw:
            confirmations = result.raw["tranche_confirmations"]
            if not isinstance(confirmations, dict):
                raise ExecutionPolicyError("tranche_confirmations_invalid")
            setup = deepcopy(confirmations.get(plan["ticker"]))
        else:
            setup = _result_setup(result, plan["ticker"])
        bar = observed.get(plan["ticker"])
        if bar is None:
            continue
        existing_confirmation = plan.get("confirmation", {})
        research_date = getattr(result, "trade_date", trade_date)
        qualified = (setup and not entry_gate_reasons(setup)
                     and setup["known_at"] == bar["date"] == research_date)
        if qualified:
            if existing_confirmation.get("known_at", "") < setup["known_at"]:
                plan["confirmation"] = {"known_at": setup["known_at"], "research_date": research_date,
                    "setup": setup, "evidence_id": _hash({"setup": setup, "bar": bar,
                                                            "research_date": research_date})}
        elif bar["date"] > existing_confirmation.get("known_at", ""):
            # A new complete observation without current qualification revokes
            # older permission. Re-reading the exact same complete bar does not
            # erase a confirmation already frozen on that bar.
            plan.pop("confirmation", None)

    # NAV is based on dated close marks, never an implicit old mark or cost.
    valuation_dates = {observed[pos["ticker"]]["date"] for pos in s["positions"] if pos["ticker"] in observed}
    valuation_date = max(valuation_dates) if valuation_dates else trade_date
    close_marks = marks_for(valuation_date, "close")
    if s["positions"]:
        valued = portfolio_snapshot({**s, "pending": [], "entry_plans": {}}, close_marks, valuation_date)
        equity = valued["nav"]
    else:
        equity = s["cash"]
    if result.action != "AVOID" and getattr(getattr(result, "mrs", None), "allow_new_positions", True):
        committed = {row["ticker"] for row in s["positions"]} | {
            row["ticker"] for row in s["entry_plans"].values() if row["status"] in {"pending", "partial"}}
        for pick in result.picks:
            if pick.ticker in committed: continue
            setup = _result_setup(result, pick.ticker)
            if setup is None: raise ExecutionPolicyError("new_signal_setup_missing")
            budget = result.raw.get("entry_budget")
            if not isinstance(budget, dict): raise ExecutionPolicyError("signal_budget_missing")
            # Re-evaluate after the earlier opening/exits using the same ceilings.
            # Never enlarge the already approved signal quantity.
            assessment = setup["known_at"]
            candidate_marks = marks_for(assessment, "close")
            candidate_marks[pick.ticker] = {"date": assessment, "price": setup["entry_price"]}
            for plan in s["entry_plans"].values():
                if plan["status"] in {"pending", "partial"} and plan["ticker"] not in candidate_marks:
                    raise ExecutionPolicyError("existing_plan_valuation_unavailable")
            snap = portfolio_snapshot(s, candidate_marks, assessment)
            rebound = {**budget, "known_at": assessment, "snapshot_hash": snap["state_hash"],
                       "admission_snapshot_hash": budget.get("admission_snapshot_hash", budget.get("snapshot_hash")),
                       "execution_snapshot": deepcopy(snap)}
            allocation = allocate_entry(snap, setup, rebound, pick.shares)
            if not allocation["shares"]:
                refusals.append({"ticker": pick.ticker, "date": trade_date, **allocation})
                continue
            pick_data = asdict(pick) if is_dataclass(pick) else dict(pick)
            pick_data.update(shares=allocation["shares"], risk_usd=allocation["risk_usd"],
                             position_pct=allocation["position_pct"])
            plan = create_tranche_plan(pick_data, rebound, trade_date, setup=setup)
            s["entry_plans"][plan["plan_id"]] = plan
            committed.add(pick.ticker)
            ops.append(f"登记 {pick.ticker} 三段纸盘计划，等候下一交易日开盘")
    s["pending"] = [{"ticker": plan["ticker"], "plan_id": plan["plan_id"],
                     "shares": plan["remaining_shares"], "entry_ref": plan["entry_ref"],
                     "stop_price": plan["initial_stop"], "risk_group": plan["risk_group"],
                     "signal_date": plan["signal_date"], "entry_budget": plan["budget"],
                     "execution_policy": POLICY_ID}
                    for plan in s["entry_plans"].values() if plan["status"] in {"pending", "partial"}]
    curve = {"date": trade_date, "equity": round(equity, 8), "valuation_date": valuation_date}
    if s["equity_curve"] and s["equity_curve"][-1]["date"] == trade_date: s["equity_curve"][-1] = curve
    else: s["equity_curve"].append(curve)
    if not ops:
        # Exact replay leaves the previous receipt/log unchanged.
        if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date:
            ops = s["ops_log"][-1]["ops"]
        else: ops = ["今日无新增成交；风险纪律优先"]
    if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date:
        ops = list(dict.fromkeys(s["ops_log"][-1]["ops"] + ops))
    log = {"date": trade_date, "ops": ops, "equity": round(equity, 8), "execution_policy": POLICY_ID}
    if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date: s["ops_log"][-1] = log
    else: s["ops_log"].append(log)
    return {"state": s, "fills": fills, "closed": closed, "refusals": refusals,
            "events": events, "equity": equity, "marks": close_marks, "ops": ops,
            "fill_checks": refusals, "valuation_date": valuation_date,
            "execution_policy": POLICY_ID}
