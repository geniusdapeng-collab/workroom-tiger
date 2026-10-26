"""Report-only business measurements and forward-maturing numeric cohorts.

No research/risk inputs are changed here. A cohort counterfactual uses frozen
signal information, the real predecision account and the shared paper engine.
Individual candidate counterfactual returns must never be added together as an
achievable portfolio return or described as historical model-strategy alpha.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timezone
import math
from zoneinfo import ZoneInfo

from .opportunity_lifecycle import _digest, _plain, aware_time, iso_date, positive

SCHEMA = "tiger.business-evaluation.v2"
_NY = ZoneInfo("America/New_York")


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _account_metrics(state, kind, as_of):
    if not isinstance(state, dict):
        raise ValueError("Business evaluation account required")
    for key in ("positions", "pending", "closed", "equity_curve"):
        if not isinstance(state.get(key, []), list):
            raise ValueError("Business evaluation account collections invalid")
    today = as_of.astimezone(_NY).date()
    closed = state.get("closed", [])
    nets, grosses, frictions, rs = [], [], [], []
    cost_complete = True
    for row in closed:
        if not isinstance(row, dict) or not _finite(row.get("pnl_usd")):
            raise ValueError("Business evaluation closed PnL invalid")
        if row.get("account_kind", kind) != kind:
            raise ValueError("Business evaluation mixed account namespace")
        for name in ("entry_date", "exit_date"):
            if row.get(name) and iso_date(row[name]) > today:
                raise ValueError("Business evaluation future account outcome")
        if row.get("entry_date") and row.get("exit_date") and row["exit_date"] < row["entry_date"]:
            raise ValueError("Business evaluation reversed execution dates")
        nets.append(float(row["pnl_usd"]))
        gross, friction = row.get("gross_pnl"), row.get("friction_cost")
        if gross is None or friction is None:
            cost_complete = False
        elif (not _finite(gross) or not _finite(friction) or friction < 0
              or abs(gross - friction - row["pnl_usd"]) > max(.025, abs(gross) * 1e-9)):
            raise ValueError("Business evaluation gross-friction=net cost identity failed")
        else:
            grosses.append(float(gross))
            frictions.append(float(friction))
        r = row.get("net_r", row.get("r_multiple"))
        if _finite(r):
            rs.append(float(r))
    curve = state.get("equity_curve", [])
    dates, equities = [], []
    for point in curve:
        if not isinstance(point, dict) or not positive(point.get("equity")):
            raise ValueError("Business evaluation equity mark invalid")
        day = iso_date(point.get("date"))
        if day > today:
            raise ValueError("Business evaluation future equity mark")
        valuation = iso_date(point.get("valuation_date", point["date"]))
        if valuation > day:
            raise ValueError("Business evaluation future valuation date")
        if dates and day <= dates[-1]:
            raise ValueError("Business evaluation duplicate/reversed equity dates")
        dates.append(day)
        equities.append(float(point["equity"]))
    equity = equities[-1] if equities else None
    cash = state.get("cash")
    if not _finite(cash) or cash < 0:
        raise ValueError("Business evaluation cash invalid")
    if equity is not None and equity + .025 < cash:
        raise ValueError("Business evaluation cash exceeds marked equity")
    initial = state.get("initial_cash")
    peak, drawdown = None, None
    if equities:
        peak = float(initial) if positive(initial) else equities[0]
        drawdown = 0.
        for value in equities:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
    fills = []
    explicit = state.get("execution_fills")
    if isinstance(explicit, list):
        fills.extend(explicit)
    plans = state.get("entry_plans", {})
    if isinstance(plans, dict):
        for plan in plans.values():
            if isinstance(plan, dict):
                fills.extend(plan.get("fill_receipts", []))
    seen, buy_notional = {}, 0.
    for fill in fills:
        identity = fill.get("fill_id") if isinstance(fill, dict) else None
        if not isinstance(identity, str) or not identity:
            raise ValueError("Business evaluation fill identity invalid")
        if identity in seen:
            if seen[identity] != fill:
                raise ValueError("Business evaluation conflicting duplicate fill")
            continue
        seen[identity] = fill
        if (fill.get("execution_mode", kind) != kind or not positive(fill.get("price"))
                or type(fill.get("shares")) is not int or fill["shares"] <= 0
                or iso_date(fill.get("date")) > today):
            raise ValueError("Business evaluation dated fill invalid")
        buy_notional += fill["shares"] * fill["price"]
    sell_notional, sell_complete = 0., True
    for row in closed:
        if type(row.get("shares")) is not int or row["shares"] <= 0 or not positive(row.get("exit")):
            sell_complete = False
        else:
            sell_notional += row["shares"] * row["exit"]
    # Legacy accounts without fill receipts cannot silently become zero-turnover.
    no_trades = not closed and not state.get("positions") and not fills
    fills_complete = bool(fills) or no_trades
    avg_nav = sum(equities) / len(equities) if equities else None
    turnover = ((buy_notional + sell_notional) / (2 * avg_nav)
                if positive(avg_nav) and fills_complete and sell_complete else None)
    n = len(closed)
    return {"account_kind": kind, "n_closed": n, "n_positions": len(state.get("positions", [])),
        "n_pending": len(state.get("pending", [])), "pnl_net": sum(nets),
        "pnl_gross": sum(grosses) if cost_complete else None,
        "friction_total": sum(frictions) if cost_complete else None,
        "cost_unavailable_reason": None if cost_complete else "LEGACY_COST_FIELDS_INCOMPLETE",
        "win_rate": sum(value > 0 for value in nets) / n if n else None,
        "expectancy_r": sum(rs) / n if n and len(rs) == n else None,
        "profitability_established": False,
        "profitability_reason": "FORWARD_SAMPLE_AND_INDEPENDENT_VALIDATION_REQUIRED",
        "equity": equity, "cash": float(cash),
        "valuation_date": curve[-1].get("valuation_date", curve[-1]["date"]) if curve else None,
        "capital_utilization": max(0., (equity - cash) / equity) if positive(equity) else None,
        "capital_utilization_unavailable_reason": None if equity else "DATED_NAV_UNAVAILABLE",
        "return_from_initial": equity / initial - 1 if positive(equity) and positive(initial) else None,
        "max_drawdown": drawdown, "nav_observations": len(equities),
        "buy_notional": buy_notional if fills_complete else None,
        "sell_notional": sell_notional if sell_complete else None,
        "turnover": turnover, "turnover_convention": "(dated_buy_notional+dated_sell_notional)/(2*mean_observed_nav)",
        "turnover_unavailable_reason": None if turnover is not None else "DATED_FILL_OR_NAV_HISTORY_INCOMPLETE"}


def _opportunity_metrics(lifecycle, outcomes, as_of):
    rows = lifecycle.get("opportunities", [])
    by_id = {row["opportunity_id"]: row for row in rows}
    labels = lifecycle.get("cohort_outcomes", []) if outcomes is None else outcomes
    if not isinstance(labels, list):
        raise ValueError("Business evaluation outcomes list required")
    matured, pending, unresolved, eligible, captured, executed = [], [], [], [], [], []
    selected_outcomes, selected_positive, policy_rejected, execution_refused = [], [], [], []
    missed, constrained, seen = [], [], set()
    for label in labels:
        if not isinstance(label, dict) or label.get("opportunity_id") not in by_id:
            raise ValueError("Business evaluation unbound outcome")
        oid = label["opportunity_id"]
        if oid in seen:
            raise ValueError("Business evaluation duplicate outcome")
        seen.add(oid)
        if aware_time(label.get("outcome_as_of")) > as_of:
            raise ValueError("Business evaluation future outcome as_of")
        if type(label.get("horizon_complete")) is not bool:
            raise ValueError("Business evaluation outcome maturity boolean required")
        if not label["horizon_complete"]:
            (unresolved if label.get("status") == "unverifiable" else pending).append(label)
            continue
        if label.get("status") in {"policy_rejected", "execution_refused"}:
            (policy_rejected if label["status"] == "policy_rejected" else execution_refused).append(label)
            unresolved.append(label)
            continue
        if (type(label.get("tradeable")) is not bool or type(label.get("eligible_at_signal")) is not bool
                or not _finite(label.get("net_return"))
                or not isinstance(label.get("policy_hash"), str) or len(label["policy_hash"]) != 64
                or not isinstance(label.get("method"), str) or not label["method"]
                or not isinstance(label.get("evidence_ids"), list) or not label["evidence_ids"]
                or not positive(label.get("entry_price")) or not positive(label.get("exit_price"))
                or not _finite(label.get("cost_usd")) or label["cost_usd"] < 0):
            raise ValueError("Business evaluation matured outcome contract invalid")
        matured.append(label)
        source = by_id[oid]
        if source.get("qualified_at_signal") is True and label["eligible_at_signal"]:
            selected_outcomes.append(label)
            if label["net_return"] > 0:
                selected_positive.append(label)
        if label["tradeable"] and not label["eligible_at_signal"]:
            constrained.append({"opportunity_id": oid, "ticker": source["ticker"],
                "reasons": source.get("rejection_reasons", []), "net_return": label["net_return"]})
        if not label["tradeable"] or not label["eligible_at_signal"]:
            continue
        eligible.append(label)
        qualified = source.get("qualified_at_signal") is True
        if qualified:
            captured.append(oid)
        else:
            missed.append({"opportunity_id": oid, "ticker": source["ticker"],
                "reasons": source.get("rejection_reasons", []), "net_return": label["net_return"]})
        if any(event.get("kind") == "ACCOUNT_HELD" and event.get("opportunity_id") == oid
               for event in lifecycle.get("events", [])):
            executed.append(oid)
    return {"scope": "recorded_candidate_cohort", "recorded": len(rows),
        "matured_labels": len(matured), "pending_labels": len(pending), "unverifiable_or_refused": len(unresolved),
        "policy_rejected": len(policy_rejected), "execution_refused": len(execution_refused),
        "unlabeled": len(by_id.keys() - seen), "labeled_tradeable": len(eligible),
        "selected_capture_count": len(captured), "executed_capture_count": len(executed),
        "capture_rate": len(captured) / len(eligible) if eligible else None,
        "executed_capture_rate": len(executed) / len(eligible) if eligible else None,
        "selected_matured_count": len(selected_outcomes),
        "selected_positive_count": len(selected_positive),
        "selected_positive_rate": len(selected_positive) / len(selected_outcomes) if selected_outcomes else None,
        "unavailable_reason": None if eligible else "NO_MATURED_INDEPENDENT_LABELS",
        "missed": missed, "policy_constrained_positive_outcomes": constrained,
        "label_methods": sorted({row["method"] for row in matured}),
        "limitations": ["Recorded-candidate proxy, not whole-market opportunity recall.",
            "Shared numeric-policy counterfactual is not independent validation of the full required-model strategy.",
            "Candidate counterfactual returns are separate hypotheses and cannot be summed into portfolio profit."]}


def _latency(lifecycle):
    pairs, invalid = [], 0
    for item in lifecycle.get("opportunities", []):
        oid = item["opportunity_id"]
        triggers = [event for event in lifecycle.get("events", [])
                    if event.get("opportunity_id") == oid and event.get("kind") == "REQUALIFY"]
        checks = [row for row in lifecycle.get("requalifications", []) if row.get("opportunity_id") == oid]
        for check in checks:
            end = aware_time(check["observed_at"])
            previous = [aware_time(event["observed_at"]) for event in triggers if aware_time(event["observed_at"]) <= end]
            if not previous:
                continue
            elapsed = (end - max(previous)).total_seconds()
            if elapsed < 0:
                invalid += 1
            else:
                pairs.append(elapsed)
    observations = lifecycle.get("observations", [])
    return {"trigger_to_requalification_samples": len(pairs),
        "trigger_to_requalification_mean_s": sum(pairs) / len(pairs) if pairs else None,
        "trigger_to_requalification_max_s": max(pairs) if pairs else None,
        "unavailable_reason": None if pairs else "NO_BOUND_TRIGGER_REQUALIFICATION_PAIR",
        "invalid_pairs": invalid, "quote_attempts": sum(row.get("attempted", 0) for row in observations),
        "usable_quotes": sum(row.get("usable", 0) for row in observations),
        "quote_refusals": dict(Counter(row["reason"] for row in lifecycle.get("observation_refusals", [])))}


def build_business_evaluation(result, *, lifecycle: dict, account_state: dict,
                              as_of: str, run_id: str, outcomes=None) -> dict:
    now = aware_time(as_of)
    blob, lifecycle, state = _plain(result), deepcopy(lifecycle), deepcopy(account_state)
    if not isinstance(blob, dict) or not isinstance(lifecycle, dict) or not isinstance(run_id, str) or not run_id:
        raise ValueError("Business evaluation bound inputs required")
    raw = blob.get("raw") or {}
    scan = raw.get("scan_stats") or {}
    decisions = raw.get("execution_decisions") or []
    rejected = (raw.get("rejections") or {}).get("candidates", [])
    reasons = Counter(reason for row in rejected for reason in row.get("reasons", []))
    candidates = blob.get("watchlist") or []
    research = raw.get("candidate_research") or {}
    coverage = research.get("coverage") or {}
    funnel = {"universe": _count(scan.get("universe_size", scan.get("total"))),
        "data_covered": _count(scan.get("data_ok")), "hard_filter_pass": _count(scan.get("passed", scan.get("pass_hard"))),
        "scanned_selected": _count(scan.get("selected", scan.get("top_n"))),
        "tss_candidates": len(candidates),
        "numeric_approved": _count(raw["numeric_approved_count"]) if "numeric_approved_count" in raw else len(blob.get("picks") or []),
        "final_buy": sum(row.get("action") == "buy" for row in decisions),
        "final_watch": sum(row.get("action") == "watch" for row in decisions),
        "issuer_researched": len(coverage["researched_symbols"]) if isinstance(coverage.get("researched_symbols"), list) else None,
        "held": len(state.get("positions", [])), "pending": len(state.get("pending", []))}
    unknown = {key: "SOURCE_FIELD_UNAVAILABLE" for key, value in funnel.items() if value is None}
    return {"schema": SCHEMA, "run_id": run_id, "as_of": as_of,
        "funnel": funnel, "funnel_unavailable": unknown,
        "funnel_evidence": {"numeric_approved": "explicit_pre_model_risk_count" if "numeric_approved_count" in raw
                            else "legacy_final_picks_fallback_not_independent_pre_model_count"},
        "rejections": {"multi_label": True, "candidate_count": len(rejected),
                       "reason_counts": dict(sorted(reasons.items())), "candidates": deepcopy(rejected)},
        "research": {"schema": research.get("schema"), "coverage": deepcopy(coverage),
                     "counters": deepcopy(research.get("counters")),
                     "unavailable_reason": None if research else "CANDIDATE_RESEARCH_UNAVAILABLE"},
        "lifecycle": {"state_counts": dict(Counter(row["state"] for row in lifecycle.get("opportunities", []))),
                      "head": lifecycle.get("events", [])[-1].get("event_hash") if lifecycle.get("events") else None},
        "latency": _latency(lifecycle),
        "account": _account_metrics(state, lifecycle.get("account_kind", "paper"), now),
        "opportunities": _opportunity_metrics(lifecycle, outcomes, now),
        "evaluation_limits": ["Processing runs are not independent trading-day returns.",
            "Immature/unverifiable cohorts remain explicit and are revisited on later runs.",
            "Realized paper outcomes and actual manual execution evidence remain separate.",
            "This report cannot authorize an entry or change research/risk thresholds."]}


def _bar_matrix(market_bars, as_of):
    """Normalize only caller-provided, known-at daily bars; no I/O or future rows."""
    if not isinstance(market_bars, dict):
        raise ValueError("Cohort market bars mapping required")
    cutoff = as_of.astimezone(_NY)
    result = {}
    for symbol, values in market_bars.items():
        if hasattr(values, "iterrows"):
            items, history = [], []
            for index, series in values.sort_index().iterrows():
                day = index.date().isoformat()
                if iso_date(day) > cutoff.date():
                    continue
                row = {name: float(series[capital]) for name, capital in
                       (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"))}
                volume = series.get("Volume")
                history.append(row["close"] * float(volume) if _finite(volume) and volume >= 0 else None)
                row.update(date=day, adv=sum(history[-20:]) / 20 if len(history) >= 20 and all(value is not None for value in history[-20:]) else 0.)
                items.append(row)
        elif isinstance(values, dict):
            items = [values] if "date" in values else list(values.values())
        elif isinstance(values, (list, tuple)):
            items = values
        else:
            raise ValueError("Cohort bars collection invalid")
        by_date = {}
        for value in items:
            row = _plain(value)
            if not isinstance(row, dict):
                raise ValueError("Cohort market bar invalid")
            day = iso_date(row.get("date"))
            if day > cutoff.date():
                continue
            # A current session's daily candle is not a complete historical bar
            # before the normal close. Earlier sessions need no invented clock.
            if day == cutoff.date() and cutoff.time() < time(16, 0):
                if not row.get("closed_at") or aware_time(row["closed_at"]) > as_of:
                    continue
            normalized = {"date": day.isoformat(), **{key: row.get(key) for key in ("open", "high", "low", "close")},
                          "adv": row.get("adv", 0.)}
            if day.isoformat() in by_date and by_date[day.isoformat()] != normalized:
                raise ValueError("Cohort conflicting duplicate market bar")
            by_date[day.isoformat()] = normalized
        result[symbol] = by_date
    return result


def mature_cohorts(lifecycle_snapshot: dict, market_bars: dict, as_of: str,
                   policy_id: str = "whitepaper-execution-v2", *,
                   current_policy_hash: str | None = None) -> list[dict]:
    """Mature frozen candidate hypotheses through the shared execution engine.

    The predecision account remains intact for each independent hypothesis.
    Source-day quantitative permission is never fabricated on subsequent days:
    without a recorded subsequent full requalification no later tranche is
    confirmed. Missing paths stay pending, with no extrapolated return.
    """
    from .calendar import next_trading_day
    from .data_models import MRSResult, PipelineResult, TradePick
    from .execution_policy import (POLICY_ID, ExecutionPolicyError, allocate_entry,
                                    entry_gate_reasons, step_paper_account)

    if policy_id != POLICY_ID:
        raise ValueError("Cohort policy unsupported; use the frozen policy implementation")
    now = aware_time(as_of)
    matrix = _bar_matrix(market_bars, now)
    frozen = deepcopy(lifecycle_snapshot)
    runs = {row["run_id"]: row for row in frozen.get("research_runs", [])}
    prior = {row["opportunity_id"]: row for row in frozen.get("cohort_outcomes", [])}
    outcomes = []
    for opportunity in frozen.get("opportunities", []):
        oid, ticker = opportunity["opportunity_id"], opportunity["ticker"]
        if opportunity.get("source_run_id") is None:
            continue
        previous = prior.get(oid)
        if previous and previous.get("horizon_complete"):
            if aware_time(previous["outcome_as_of"]) > now:
                raise ValueError("Cohort prior outcome is in future")
            outcomes.append(previous)
            continue
        base = {"opportunity_id": oid, "symbol": ticker, "outcome_as_of": as_of,
            "method": "frozen-account-shared-numeric-policy-v2", "policy_id": policy_id,
            "scope": "per_candidate_numeric_policy_counterfactual", "horizon_complete": False,
            "eligible_at_signal": None, "tradeable": False, "net_return": None,
            "entry_price": None, "exit_price": None, "entry_date": None, "exit_date": None,
            "cost_usd": None, "evidence_ids": [],
            "evidence_basis": "Derived hashes bind caller-supplied bars; execution owner must separately bind actual provider IO receipts.",
            "limits": ["Not a historical required-model strategy backtest.",
                       "Separate candidate hypotheses are not summed as an achievable portfolio.",
                       "No later tranche without recorded current requalification."]}
        run = runs.get(opportunity["source_run_id"], {})
        inputs = run.get("cohort_inputs")
        if (not isinstance(inputs, dict) or inputs.get("schema") != "tiger.cohort-inputs.v2"
                or not all(isinstance(inputs.get(key), dict) for key in ("setups", "budget", "portfolio_snapshot", "portfolio_state"))
                or not isinstance(inputs["setups"].get(ticker), dict)):
            outcomes.append({**base, "status": "unverifiable", "reason": "FROZEN_COHORT_INPUTS_MISSING"})
            continue
        setup, budget = deepcopy(inputs["setups"][ticker]), deepcopy(inputs["budget"])
        policy_hash = inputs.get("policy_hash") or _digest({"policy_id": policy_id, "setup": setup, "budget": budget})
        base.update(policy_hash=policy_hash,
                    policy_hash_basis="frozen_engine_identity" if inputs.get("policy_hash") else "frozen_numeric_contract_not_engine_source")
        if inputs.get("policy_id", policy_id) != policy_id:
            outcomes.append({**base, "status": "unverifiable", "reason": "FROZEN_POLICY_VERSION_MISMATCH"})
            continue
        if inputs.get("policy_hash") and inputs["policy_hash"] != current_policy_hash:
            outcomes.append({**base, "status": "unverifiable",
                             "reason": "FROZEN_POLICY_IMPLEMENTATION_UNAVAILABLE"})
            continue
        reasons = entry_gate_reasons(setup)
        original_plan = opportunity.get("plan") or {}
        requested = original_plan.get("shares")
        allocation = allocate_entry(inputs["portfolio_snapshot"], setup, budget, requested)
        if reasons or not allocation["shares"]:
            outcomes.append({**base, "status": "policy_rejected", "horizon_complete": True,
                "eligible_at_signal": False, "reason": (reasons or [allocation["reason"]])[0],
                "reasons": reasons or [allocation["reason"]]})
            continue
        signal_date = iso_date(opportunity["signal_date"])
        if signal_date > now.astimezone(_NY).date():
            raise ValueError("Cohort future signal")
        pick = TradePick(ticker=ticker, tss_final=setup["tss_final"], tos=original_plan.get("tos", 0.),
            entry_template=setup["entry_template"], entry_price=setup["entry_price"], stop_price=setup["stop_price"],
            shares=allocation["shares"], risk_usd=allocation["risk_usd"], position_pct=allocation["position_pct"],
            chain=setup["risk_group"], sector=setup.get("sector", ""),
            time_stop_days=original_plan.get("time_stop_days", 0))
        source_result = PipelineResult(trade_date=signal_date.isoformat(), provider="cohort-counterfactual",
            action="BUY", mrs=MRSResult(setup["mrs_star"], 0., 1., setup["mrs_star"], position_cap=(0., budget["gross_cap"])),
            picks=[pick], raw={"execution_policy": policy_id, "account_currency": "USD", "entry_budget": budget,
                              "execution_setups": {ticker: setup}})
        state = deepcopy(inputs["portfolio_state"])
        evidence, last_closed, last_refusal, last_bar_day = [], None, None, None
        try:
            signal_bars = {symbol: rows[signal_date.isoformat()] for symbol, rows in matrix.items() if signal_date.isoformat() in rows}
            started = step_paper_account(state, signal_date.isoformat(), source_result, signal_bars)
            state = started["state"]
            target_plan = next((plan for plan in state.get("entry_plans", {}).values()
                                if plan["ticker"] == ticker and plan["signal_date"] == signal_date.isoformat()), None)
            if target_plan is None:
                last_refusal = next((row for row in started["refusals"] if row.get("ticker") == ticker), {"reason": "source_plan_unavailable"})
            day = next_trading_day(signal_date)
            pending_reason = "HORIZON_NOT_MATURED"
            while target_plan is not None and day <= now.astimezone(_NY).date():
                day_text = day.isoformat()
                bar = matrix.get(ticker, {}).get(day_text)
                if bar is None:
                    pending_reason = "MISSING_DAILY_MARKET_PATH"
                    break
                daily = {symbol: rows[day_text] for symbol, rows in matrix.items() if day_text in rows}
                # The original source setup is not fresh future-day evidence.
                current = PipelineResult(trade_date=day_text, provider="cohort-counterfactual", action="WAIT",
                                         raw={"execution_policy": policy_id, "account_currency": "USD"})
                advanced = step_paper_account(state, day_text, current, daily)
                state = advanced["state"]
                last_bar_day = day_text
                evidence.append("cohortbar_" + _digest({"date": day_text, "bars": daily})[:32])
                last_closed = next((row for row in advanced["closed"] if row.get("plan_id") == target_plan["plan_id"]), None)
                last_refusal = next((row for row in advanced["refusals"] if row.get("plan_id") == target_plan["plan_id"]), last_refusal)
                target_plan = state["entry_plans"][target_plan["plan_id"]]
                if last_closed is not None or target_plan["status"] == "cancelled":
                    break
                day = next_trading_day(day)
        except ExecutionPolicyError as exc:
            outcomes.append({**base, "status": "unverifiable", "reason": "SHARED_POLICY_REFUSED:" + exc.code,
                             "eligible_at_signal": True, "evidence_ids": evidence})
            continue
        if last_closed is not None:
            cost = last_closed["entry"] * last_closed["shares"]
            outcomes.append({**base, "status": "closed", "horizon_complete": True, "eligible_at_signal": True,
                "tradeable": last_closed["pnl_usd"] > 0, "net_return": last_closed["pnl_usd"] / cost,
                "entry_price": last_closed["entry"], "exit_price": last_closed["exit"],
                "entry_date": last_closed["entry_date"], "exit_date": last_closed["exit_date"],
                "cost_usd": last_closed["friction_cost"], "pnl_net": last_closed["pnl_usd"],
                "net_r": last_closed["net_r"], "reason": last_closed["reason"],
                "evidence_ids": evidence, "paper_execution": deepcopy(last_closed)})
        elif last_refusal and (target_plan is None or target_plan.get("status") == "cancelled"):
            outcomes.append({**base, "status": "execution_refused", "horizon_complete": True,
                "eligible_at_signal": True, "reason": last_refusal["reason"], "evidence_ids": evidence})
        else:
            outcomes.append({**base, "status": "continue_pending", "eligible_at_signal": True,
                "reason": pending_reason, "evidence_ids": evidence,
                "last_observed_bar": last_bar_day})
    return outcomes
