"""Evidence-bound opportunity accounting; never a broker or a risk engine.

Research generations and subsequent observations are durable accounting facts.
An entry alert requests current research/risk requalification. Even an accepted
requalification is a pending intent requiring a fresh execution quote/budget;
only a trusted paper/manual account snapshot establishes a fill or closure.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from . import config
from .currency import require_usd_sim_state
from .ledger_io import read_json_strict, write_json_atomic

SCHEMA = "tiger.opportunity-ledger.v2"
_NY = ZoneInfo("America/New_York")
_STATES = {"WATCH", "QUALIFIED", "INTENT_PENDING", "HELD", "EXIT_PENDING",
           "CLOSED", "INVALIDATED", "EXPIRED", "SUPERSEDED"}
_TERMINAL = {"CLOSED", "INVALIDATED", "EXPIRED", "SUPERSEDED"}
# NYSE official core hours and the published 2026-2027 early-close calendar,
# reviewed 2026-10-27: https://www.nyse.com/markets/hours-calendars
# This action gate does not invent future calendars or extend entry/stop
# execution into premarket/after-hours. Such quotes remain observed refusals.
_SESSION_YEARS = frozenset({2026, 2027})
_EARLY_CLOSES = frozenset({"2026-11-27", "2026-12-24", "2027-11-26"})


def aware_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Lifecycle aware timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Lifecycle timestamp invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Lifecycle timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def iso_date(value: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("Lifecycle valuation date invalid")
    return date.fromisoformat(value)


def positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    # Optional numerical observations can be unavailable. Preserve that fact
    # as null in accounting, rather than emitting invalid JSON NaN/Infinity.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Lifecycle {label} required")
    return value


def _strings(value, label: str, *, empty=True) -> list[str]:
    if (not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)
            or not empty and not value or len(set(value)) != len(value)):
        raise ValueError(f"Lifecycle {label} invalid")
    return list(value)


def _symbol(value) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", value) is None:
        raise ValueError("Lifecycle ticker invalid")
    return value


def _levels(entry, stop) -> dict:
    entry = float(entry) if positive(entry) else None
    stop = float(stop) if positive(stop) else None
    if entry is not None and stop is not None and stop >= entry:
        stop = None
    protect = (round(entry + config.PROFIT_PROTECT_R * (entry - stop), 8)
               if entry is not None and stop is not None else None)
    return {"entry": entry, "stop": stop, "protect": protect}


def _session_refusal(instant):
    from .calendar import is_trading_day
    local = instant.astimezone(_NY)
    if local.year not in _SESSION_YEARS:
        return "QUOTE_SESSION_CALENDAR_UNSUPPORTED"
    if not is_trading_day(local.date()):
        return "QUOTE_MARKET_CLOSED"
    close = time(13) if local.date().isoformat() in _EARLY_CLOSES else time(16)
    if not time(9, 30) <= local.time() < close:
        return "QUOTE_OUTSIDE_REGULAR_SESSION"
    return None


def quote_refusal_reason(quote, *, observed_at: str, max_age_seconds: int = 300):
    """Shared intraday freshness guard; a provider's 'realtime' label is insufficient."""
    aware_time(observed_at)
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise ValueError("Lifecycle quote age budget invalid")
    if not isinstance(quote, dict):
        return "QUOTE_MISSING"
    try:
        now = aware_time(quote.get("observed_at", observed_at))
    except ValueError:
        return "QUOTE_OBSERVATION_UNVERIFIABLE"
    if not positive(quote.get("price")):
        return "QUOTE_PRICE_INVALID"
    if quote.get("stale"):
        return "QUOTE_STALE"
    if quote.get("kind") not in {"realtime", "realtime_delayed", "delayed"}:
        return "QUOTE_NOT_INTRADAY"
    try:
        ts = aware_time(quote.get("ts"))
    except ValueError:
        return "QUOTE_TIME_UNVERIFIABLE"
    age = (now - ts).total_seconds()
    if age < 0:
        return "QUOTE_FUTURE"
    if age > max_age_seconds:
        return "QUOTE_TOO_OLD"
    refusal = _session_refusal(now)
    if refusal:
        return refusal
    refusal = _session_refusal(ts)
    return "QUOTE_TIME_" + refusal.removeprefix("QUOTE_") if refusal else None


class OpportunityLedger:
    @staticmethod
    def initial_state(account_kind="paper"):
        if account_kind not in {"paper", "manual"}:
            raise ValueError("Lifecycle account kind invalid")
        return {"schema": SCHEMA, "account_kind": account_kind, "revision": 0,
            "research_runs": [], "opportunities": [], "intents": [], "events": [],
            "requalifications": [], "account_snapshots": [], "observation_refusals": [],
            "observations": [], "cohort_outcomes": []}

    def __init__(self, path: str | Path, *, account_kind: str = "paper"):
        if account_kind not in {"paper", "manual"}:
            raise ValueError("Lifecycle account kind invalid")
        self.path = Path(path)
        self.account_kind = account_kind
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self._head = None
            self._state = self.initial_state(account_kind)
            self._dirty = True
        else:
            self._state = read_json_strict(self.path)
            self._validate(self._state)
            if self._state["account_kind"] != account_kind:
                raise ValueError("Lifecycle account namespace mismatch")
            self._head = hashlib.sha256(raw).hexdigest()
            self._dirty = False

    @staticmethod
    def _validate(state):
        fields = {"schema", "account_kind", "revision", "research_runs", "opportunities",
                  "intents", "events", "requalifications", "account_snapshots",
                  "observation_refusals", "observations", "cohort_outcomes"}
        if (not isinstance(state, dict) or set(state) != fields or state["schema"] != SCHEMA
                or state["account_kind"] not in {"paper", "manual"}
                or type(state["revision"]) is not int or state["revision"] < 0
                or any(not isinstance(state[name], list) for name in fields - {"schema", "account_kind", "revision"})):
            raise ValueError("Lifecycle ledger structure invalid; original preserved")
        _digest(state)  # Strict JSON and finite values, including nested facts.
        ids = set()
        for item in state["opportunities"]:
            required = {"opportunity_id", "ticker", "source_run_id", "signal_date", "observed_at", "expires_at",
                        "state", "qualified_at_signal", "levels", "rejection_reasons", "source_ids", "filled_shares"}
            if (not isinstance(item, dict) or not required.issubset(item) or not item.get("opportunity_id")
                    or item["opportunity_id"] in ids or item.get("state") not in _STATES
                    or not isinstance(item.get("levels"), dict) or set(item["levels"]) != {"entry", "stop", "protect"}
                    or any(value is not None and not positive(value) for value in item["levels"].values())
                    or type(item["qualified_at_signal"]) is not bool
                    or type(item["filled_shares"]) is not int or item["filled_shares"] < 0):
                raise ValueError("Lifecycle opportunity structure invalid")
            _symbol(item.get("ticker"))
            aware_time(item["observed_at"])
            aware_time(item["expires_at"])
            if item["signal_date"] is not None:
                iso_date(item["signal_date"])
            _strings(item["rejection_reasons"], "rejection reasons")
            _strings(item["source_ids"], "source ids")
            ids.add(item["opportunity_id"])
        runs = [item.get("run_id") for item in state["research_runs"] if isinstance(item, dict)]
        if len(runs) != len(state["research_runs"]) or any(not item for item in runs) or len(set(runs)) != len(runs):
            raise ValueError("Lifecycle research run collision")
        for item in state["research_runs"]:
            if re.fullmatch(r"[0-9a-f]{64}", item.get("body_hash", "")) is None:
                raise ValueError("Lifecycle research binding invalid")
            aware_time(item.get("observed_at"))
            aware_time(item.get("expires_at"))
            iso_date(item.get("signal_date"))
        previous = None
        for index, event in enumerate(state["events"], 1):
            if not isinstance(event, dict):
                raise ValueError("Lifecycle event invalid")
            core = {key: value for key, value in event.items() if key != "event_hash"}
            if (core.get("sequence") != index or core.get("previous_hash") != previous
                    or event.get("event_hash") != _digest(core)):
                raise ValueError("Lifecycle event chain invalid")
            aware_time(core.get("observed_at"))
            previous = event["event_hash"]

    def snapshot(self) -> dict:
        return deepcopy(self._state)

    def _event(self, kind, observed_at, opportunity_id=None, **payload):
        event = {"sequence": len(self._state["events"]) + 1,
                 "previous_hash": self._state["events"][-1]["event_hash"] if self._state["events"] else None,
                 "kind": kind, "observed_at": observed_at, "opportunity_id": opportunity_id,
                 "payload": payload}
        event["event_hash"] = _digest(event)
        self._state["events"].append(event)
        self._dirty = True
        return event["event_hash"]

    def _account_committed(self, item):
        return bool(item.get("account_plan_id") or item.get("filled_shares", 0) > 0
            or any(event["kind"] == "ACCOUNT_INTENT_PENDING"
                and event["opportunity_id"] == item["opportunity_id"] for event in self._state["events"]))

    def _cancel_intents(self, item, *, cause, observed_at, by_run=None):
        cancelled = []
        for intent in self._state["intents"]:
            if (intent["opportunity_id"] == item["opportunity_id"]
                    and intent["status"] == "pending_execution_recheck"):
                intent.update(status="cancelled_by_" + cause, cancelled_at=observed_at)
                if by_run is not None:
                    intent["superseded_by_run"] = by_run
                cancelled.append(intent["intent_id"])
        return cancelled

    def record_research(self, result, *, run_id: str, observed_at: str, expires_at: str,
                        legacy_watch_scope: bool = False) -> dict:
        _text(run_id, "source run")
        observed, expiry = aware_time(observed_at), aware_time(expires_at)
        blob = _plain(result)
        if not isinstance(blob, dict):
            raise ValueError("Lifecycle research result required")
        signal_date = iso_date(blob.get("trade_date"))
        if signal_date > observed.astimezone(_NY).date() or expiry <= observed:
            raise ValueError("Lifecycle research/expiry date invalid")
        raw = blob.get("raw") or {}
        watched, picks = blob.get("watchlist", []), blob.get("picks", [])
        decisions = raw.get("execution_decisions", [])
        rejected = (raw.get("rejections") or {}).get("candidates", [])
        if any(not isinstance(items, list) for items in (watched, picks, decisions, rejected)):
            raise ValueError("Lifecycle research collections invalid")
        reduced = {"trade_date": blob["trade_date"], "action": blob.get("action"),
            "watchlist": watched, "picks": picks, "decisions": decisions,
            "rejections": rejected, "cohort_inputs": raw.get("cohort_inputs"),
            "observed_at": observed_at, "expires_at": expires_at}
        by_watch = {_symbol(item.get("ticker")): item for item in watched}
        by_pick = {_symbol(item.get("ticker")): item for item in picks}
        by_decision = {_symbol(item.get("symbol") or item.get("ticker")): item for item in decisions}
        by_reject = {_symbol(item.get("ticker")): item for item in rejected}
        if any(len(items) != len(mapping) for items, mapping in
               ((watched, by_watch), (picks, by_pick), (decisions, by_decision), (rejected, by_reject))):
            raise ValueError("Lifecycle duplicate research ticker")
        deferred = set()
        if not legacy_watch_scope:
            research = raw.get("candidate_research")
            if research is not None and not isinstance(research, dict):
                raise ValueError("Lifecycle candidate research invalid")
            coverage = ((research or {}).get("coverage") or {})
            deferred_rows = coverage.get("deferred") or []
            if not isinstance(deferred_rows, list):
                raise ValueError("Lifecycle deferred research collection invalid")
            for item in deferred_rows:
                if not isinstance(item, dict):
                    raise ValueError("Lifecycle deferred research row invalid")
                deferred.add(_symbol(item.get("symbol") or item.get("ticker")))
            for ticker, item in by_reject.items():
                if "candidate_limit" in (item.get("reasons") or []):
                    deferred.add(ticker)
            if deferred.intersection(by_pick):
                raise ValueError("Lifecycle deferred symbol cannot also be an approved pick")
            reduced["deferred_symbols"] = sorted(deferred)
        digest = _digest(reduced)
        existing = next((item for item in self._state["research_runs"] if item["run_id"] == run_id), None)
        if existing:
            if existing["body_hash"] != digest:
                raise ValueError("Lifecycle source run collision: changed research")
            return self.snapshot()
        symbols = list(dict.fromkeys([*by_watch, *by_pick, *by_decision]))
        if not legacy_watch_scope:
            symbols = [ticker for ticker in symbols if ticker not in deferred]
        rows = []
        for ticker in symbols:
            candidate, pick, decision = by_watch.get(ticker, {}), by_pick.get(ticker), by_decision.get(ticker, {})
            approved = pick is not None and decision.get("action", "buy") == "buy"
            entry = pick.get("entry_price") if pick else candidate.get("key_level") or candidate.get("price")
            stop = pick.get("stop_price") if pick else candidate.get("stop_price")
            reasons = _strings(by_reject.get(ticker, {}).get("reasons", []), "rejection reasons")
            projected_sources = []
            for field in ("sources", "market_evidence"):
                records = decision.get(field, [])
                if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                    raise ValueError("Lifecycle projected source evidence invalid")
                projected_sources.extend(_text(record.get("id"), "projected source id") for record in records)
            if len(set(projected_sources)) != len(projected_sources):
                raise ValueError("Lifecycle duplicate projected source evidence")
            sources = _strings(decision.get("source_ids", projected_sources), "source evidence")
            if projected_sources and set(sources) != set(projected_sources):
                raise ValueError("Lifecycle conflicting projected source evidence")
            rows.append({"opportunity_id": "opp_" + _digest([run_id, ticker])[:32], "ticker": ticker,
                "source_run_id": run_id, "signal_date": blob["trade_date"],
                "observed_at": observed_at, "expires_at": expires_at,
                "state": "QUALIFIED" if approved else "WATCH", "qualified_at_signal": approved,
                "decision": decision.get("action", "watch"), "levels": _levels(entry, stop),
                "initial_stop": float(stop) if positive(stop) else None,
                "rejection_reasons": reasons, "source_ids": sources,
                "entry_template": (pick or candidate).get("entry_template", ""),
                "plan": deepcopy(pick), "filled_shares": 0, "pending_protection_stop": None})
        # Validate the complete generation before changing any prior history.
        _digest(rows)
        for old in self._state["opportunities"]:
            # A risk-approved proposal is not an account commitment. Once a
            # new research generation replaces it, its prior risk/quote binding
            # cannot keep authorizing an unexecuted pending intent indefinitely.
            # Account facts, including legacy pending rows without a plan_id,
            # remain authoritative and retain their own independent generation.
            if (old["ticker"] in symbols and old["state"] in {"WATCH", "QUALIFIED", "INTENT_PENDING"}
                    and not self._account_committed(old)):
                old["state"] = "SUPERSEDED"
                cancelled = self._cancel_intents(old, cause="new_research", observed_at=observed_at, by_run=run_id)
                self._event("SUPERSEDED", observed_at, old["opportunity_id"], by_run=run_id,
                            cancelled_intent_ids=cancelled)
        run = {"run_id": run_id, "body_hash": digest,
            "observed_at": observed_at, "signal_date": blob["trade_date"], "expires_at": expires_at,
            "cohort_inputs": deepcopy(raw.get("cohort_inputs"))}
        if not legacy_watch_scope:
            run["deferred_symbols"] = sorted(deferred)
        self._state["research_runs"].append(run)
        self._state["opportunities"].extend(rows)
        for item in rows:
            self._event("RESEARCH_RECORDED", observed_at, item["opportunity_id"], state=item["state"])
        self._dirty = True
        return self.snapshot()

    def _opportunity(self, opportunity_id):
        item = next((row for row in self._state["opportunities"] if row["opportunity_id"] == opportunity_id), None)
        if item is None:
            raise ValueError("Lifecycle opportunity unknown")
        return item

    def requalify(self, opportunity_id: str, *, run_id: str, observed_at: str,
                  accepted: bool, reasons: list[str], risk_evidence_ids: list[str],
                  account_snapshot: dict, plan: dict | None = None,
                  assessment_date: str | None = None) -> dict:
        now = aware_time(observed_at)
        _text(run_id, "requalification run")
        item = self._opportunity(opportunity_id)
        if type(accepted) is not bool:
            raise ValueError("Lifecycle requalification boolean required")
        reasons = _strings(reasons, "risk reasons")
        refs = _strings(risk_evidence_ids, "risk evidence", empty=not accepted)
        if now < aware_time(item["observed_at"]):
            raise ValueError("Lifecycle requalification precedes research")
        if not isinstance(account_snapshot, dict):
            raise ValueError("Lifecycle account snapshot required")
        valuation = iso_date(account_snapshot.get("known_at"))
        if (valuation > now.astimezone(_NY).date()
                or assessment_date is not None and valuation != iso_date(assessment_date)):
            raise ValueError("Lifecycle assessment/valuation date mismatch")
        state_hash = account_snapshot.get("state_hash")
        if not isinstance(state_hash, str) or re.fullmatch(r"[0-9a-f]{64}", state_hash) is None:
            raise ValueError("Lifecycle account state hash required")
        if account_snapshot.get("frozen_at") is not None and aware_time(account_snapshot["frozen_at"]) > now:
            raise ValueError("Lifecycle future account snapshot")
        if accepted:
            if _session_refusal(now):
                raise ValueError("Lifecycle entry intent requires a supported regular market session")
            if (item["state"] in _TERMINAL | {"HELD", "EXIT_PENDING"}
                    or self._account_committed(item) or now > aware_time(item["expires_at"])):
                raise ValueError("Lifecycle expired/held opportunity cannot create entry intent")
            if (not isinstance(plan, dict) or plan.get("ticker", plan.get("symbol")) != item["ticker"]
                    or type(plan.get("shares")) is not int or plan["shares"] <= 0
                    or not positive(plan.get("entry_price")) or not positive(plan.get("stop_price"))
                    or plan["stop_price"] >= plan["entry_price"] or plan.get("entry_template") not in {"A", "B", "C"}
                    or not positive(account_snapshot.get("nav"))):
                raise ValueError("Lifecycle executable plan invalid")
        core = _plain({"opportunity_id": opportunity_id, "run_id": run_id, "observed_at": observed_at,
            "accepted": accepted, "reasons": reasons, "risk_evidence_ids": refs,
            "account_snapshot": account_snapshot, "plan": plan, "assessment_date": assessment_date})
        digest = _digest(core)
        previous = next((row for row in self._state["requalifications"]
                         if row["run_id"] == run_id and row["opportunity_id"] == opportunity_id), None)
        if previous:
            if previous["body_hash"] != digest:
                raise ValueError("Lifecycle conflicting requalification")
            return deepcopy(item)
        if accepted:
            intent_id = "intent_" + _digest([opportunity_id, run_id, digest])[:32]
            self._state["intents"].append({**deepcopy(plan), "intent_id": intent_id, "opportunity_id": opportunity_id,
                "ticker": item["ticker"], "qualification_run_id": run_id, "created_at": observed_at,
                "account_state_hash": state_hash, "valuation_date": valuation.isoformat(),
                "account_frozen_at": account_snapshot.get("frozen_at"), "account_kind": self.account_kind,
                "requires_execution_recheck": True, "risk_evidence_ids": refs,
                "status": "pending_execution_recheck"})
            item["state"] = "INTENT_PENDING"
            item["plan"] = deepcopy(plan)
            item["levels"] = _levels(plan["entry_price"], plan["stop_price"])
        else:
            if item["state"] not in {"HELD", "EXIT_PENDING"} | _TERMINAL:
                item["state"] = "INTENT_PENDING" if self._account_committed(item) else "WATCH"
            for intent in self._state["intents"]:
                if intent["opportunity_id"] == opportunity_id and intent["status"] == "pending_execution_recheck":
                    intent["status"] = "cancelled_by_current_risk"
        item["rejection_reasons"] = reasons
        self._state["requalifications"].append({**core, "body_hash": digest})
        self._event("REQUALIFIED" if accepted else "RISK_REJECTED", observed_at, opportunity_id,
                    run_id=run_id, reasons=reasons, risk_evidence_ids=refs)
        return deepcopy(item)

    def observation_symbols(self, run_id: str) -> list[str]:
        """Current research plus all account commitments; old uncommitted WATCH stays out."""
        _text(run_id, "observation run")
        selected = {}
        def priority(item):
            return 3 if item["state"] in {"HELD", "EXIT_PENDING"} else 2 if self._account_committed(item) else 1
        for item in self._state["opportunities"]:
            if item["state"] in _TERMINAL:
                continue
            if (item["state"] in {"HELD", "EXIT_PENDING"} or self._account_committed(item)
                    or item["source_run_id"] == run_id):
                previous = selected.get(item["ticker"])
                if previous is None or priority(item) >= priority(previous):
                    selected[item["ticker"]] = item
        return sorted(selected)

    def observe_quotes(self, quotes, *, observed_at: str, max_age_seconds: int = 300,
                       symbols: list[str] | None = None,
                       legacy_shape: bool = False) -> list[dict]:
        now = aware_time(observed_at)
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
            raise ValueError("Lifecycle quote age budget invalid")
        if isinstance(quotes, list):
            quotes = {item["ticker"]: item for item in quotes}
        if not isinstance(quotes, dict):
            raise ValueError("Lifecycle quotes mapping required")
        allowed = None
        if symbols is not None:
            if legacy_shape:
                raise ValueError("Legacy lifecycle shape cannot add an observation scope")
            if not isinstance(symbols, list):
                raise ValueError("Lifecycle observation symbols invalid")
            allowed = {_symbol(ticker) for ticker in symbols}
        current = {}
        def priority(item):
            return 3 if item["state"] in {"HELD", "EXIT_PENDING"} else 2 if self._account_committed(item) else 1
        for item in self._state["opportunities"]:
            if item["state"] in _TERMINAL:
                continue
            previous = current.get(item["ticker"])
            if previous is None or priority(item) >= priority(previous):
                current[item["ticker"]] = item
        alerts, refused, usable, protection_unverified = [], [], 0, []
        for ticker, item in current.items():
            held = item["state"] in {"HELD", "EXIT_PENDING"}
            committed = self._account_committed(item)
            if not held and not committed and now > aware_time(item["expires_at"]):
                item["state"] = "EXPIRED"
                cancelled = self._cancel_intents(item, cause="expiry", observed_at=observed_at)
                self._event("EXPIRED", observed_at, item["opportunity_id"], cancelled_intent_ids=cancelled)
                continue
            if allowed is not None and ticker not in allowed:
                continue
            q = quotes.get(ticker)
            reason = quote_refusal_reason(q, observed_at=observed_at, max_age_seconds=max_age_seconds)
            if reason:
                quote_observed = (q.get("observed_at", observed_at)
                                  if isinstance(q, dict) else observed_at)
                if legacy_shape:
                    refused.append({"ticker": ticker, "reason": reason,
                                    "observed_at": observed_at})
                else:
                    refused.append({"ticker": ticker, "reason": reason,
                                    "observed_at": quote_observed,
                                    "quote_at": q.get("ts") if isinstance(q, dict) else None,
                                    "protection_unverified": held})
                if held and not legacy_shape:
                    protection_unverified.append(ticker)
                    evidence_id = q.get("evidence_id") if isinstance(q, dict) else None
                    dedup = _digest([item["opportunity_id"], "PROTECTION_UNVERIFIED",
                                     reason, evidence_id, quote_observed])
                    if not any(event["payload"].get("observation_key") == dedup
                               for event in self._state["events"]):
                        self._event("PROTECTION_UNVERIFIED", observed_at, item["opportunity_id"],
                                    observation_key=dedup, reason=reason,
                                    quote_observed_at=quote_observed)
                continue
            usable += 1
            price, levels = float(q["price"]), item["levels"]
            stop, entry, protect = levels.get("stop"), levels.get("entry"), levels.get("protect")
            kind, extra = None, {}
            if held and positive(stop) and price <= stop:
                kind = "STOP"
            elif held and positive(protect) and price >= protect:
                initial_stop = item.get("initial_stop")
                proposed = entry + .5 * (entry - initial_stop) if positive(entry) and positive(initial_stop) and initial_stop < entry else None
                if positive(proposed) and proposed > max(stop or 0, item.get("pending_protection_stop") or 0):
                    kind, extra = "PROTECT", {"proposed_stop": round(proposed, 8)}
            elif not held and positive(stop) and price <= stop:
                if committed:
                    kind, extra = "REQUALIFY", {"reason": "ACCOUNT_PENDING_STOP_BREACHED"}
                else:
                    item["state"] = "INVALIDATED"
                    cancelled = self._cancel_intents(item, cause="structure", observed_at=observed_at)
                    self._event("INVALIDATED", observed_at, item["opportunity_id"], reason="STRUCTURAL_STOP_BREACHED",
                                cancelled_intent_ids=cancelled)
            elif not held and positive(entry) and abs(price / entry - 1) <= .005:
                kind = "REQUALIFY"
            if kind is None:
                continue
            evidence_id = q.get("evidence_id") or "quote_" + _digest([ticker, q["ts"], price])[:24]
            dedup = _digest([item["opportunity_id"], kind, levels, evidence_id])
            if any(event["payload"].get("observation_key") == dedup for event in self._state["events"]):
                continue
            if kind == "STOP":
                item["state"] = "EXIT_PENDING"
            elif kind == "PROTECT":
                item["pending_protection_stop"] = extra["proposed_stop"]
            alert = {"opportunity_id": item["opportunity_id"], "ticker": ticker, "kind": kind,
                "price": price, "quote_at": q["ts"],
                "observed_at": q.get("observed_at", observed_at),
                "price_kind": q["kind"], "evidence_id": evidence_id,
                "requires_requalification": kind == "REQUALIFY", "account_kind": self.account_kind, **extra}
            self._event(kind, observed_at, item["opportunity_id"], observation_key=dedup,
                        **{key: value for key, value in alert.items()
                           if key not in {"kind", "observed_at", "opportunity_id"}})
            alerts.append(alert)
        for refusal in refused:
            if refusal not in self._state["observation_refusals"]:
                self._state["observation_refusals"].append(refusal)
                self._dirty = True
        attempted = len(current) if allowed is None else len(current.keys() & allowed)
        observation = {"observed_at": observed_at, "attempted": attempted, "usable": usable,
                       "refused": len(refused), "alerts": len(alerts)}
        if not legacy_shape:
            observation.update(protection_unverified=protection_unverified,
                scope="current_research_plus_account_commitments")
        if observation not in self._state["observations"]:
            self._state["observations"].append(observation)
            self._dirty = True
        return deepcopy(alerts)

    def sync_account(self, account_state: dict, *, snapshot_id: str, observed_at: str,
                     account_kind: str = "paper") -> dict:
        now = aware_time(observed_at)
        if account_kind != self.account_kind:
            raise ValueError("Lifecycle account namespace mismatch")
        _text(snapshot_id, "account snapshot id")
        state = _plain(account_state)
        if not isinstance(state, dict) or any(not isinstance(state.get(key, []), list) for key in ("positions", "pending", "closed")):
            raise ValueError("Lifecycle account structure invalid")
        plans = state.get("entry_plans", {})
        if not isinstance(plans, dict):
            raise ValueError("Lifecycle account plan collection invalid")
        for identity, plan in plans.items():
            _text(identity, "account plan identity")
            if not isinstance(plan, dict) or plan.get("status") not in {"pending", "partial", "filled", "cancelled", "closed"}:
                raise ValueError("Lifecycle account plan invalid")
            _symbol(plan.get("ticker"))
        require_usd_sim_state(state, scope="opportunity lifecycle accounting")
        digest = _digest(state)
        existing = next((row for row in self._state["account_snapshots"] if row["snapshot_id"] == snapshot_id), None)
        if existing:
            if existing["body_hash"] != digest:
                raise ValueError("Lifecycle account snapshot collision")
            return self.snapshot()
        for kind in ("positions", "pending", "closed"):
            for item in state.get(kind, []):
                _symbol(item.get("ticker"))
                if type(item.get("shares")) is not int or item["shares"] <= 0:
                    raise ValueError("Lifecycle account quantity invalid")
                if account_kind == "manual":
                    _strings(item.get("execution_evidence_ids"), "manual execution evidence", empty=False)
                if kind == "positions" and (not positive(item.get("entry_price")) or not positive(item.get("stop"))):
                    raise ValueError("Lifecycle held prices invalid")
                for field in ("entry_date", "exit_date", "signal_date"):
                    if item.get(field) and iso_date(item[field]) > now.astimezone(_NY).date():
                        raise ValueError("Lifecycle future account fact")
        # Transactional copy: an invalid later position cannot partially change
        # an earlier holding or reset a clock before rejection.
        before, was_dirty = self._state, self._dirty
        self._state = deepcopy(before)
        try:
            held_symbols = {row["ticker"] for row in state.get("positions", [])}
            for kind, target in (("pending", "INTENT_PENDING"), ("positions", "HELD"), ("closed", "CLOSED")):
                for fact in state.get(kind, []):
                    ticker = fact["ticker"]
                    matching = [row for row in self._state["opportunities"] if row["ticker"] == ticker]
                    identity = fact.get("plan_id")
                    item = next((row for row in reversed(matching)
                                 if identity and row.get("account_plan_id") == identity), None)
                    if item is None and fact.get("entry_date"):
                        item = next((row for row in reversed(matching)
                                     if row.get("entry_date") == fact["entry_date"]
                                     and (not identity or not row.get("account_plan_id"))), None)
                    if kind == "closed" and item and ticker in held_symbols:
                        same_position = next((pos for pos in state.get("positions", [])
                            if pos["ticker"] == ticker and (identity and pos.get("plan_id") == identity
                                or not identity and pos.get("entry_date") == fact.get("entry_date"))), None)
                        if same_position:
                            continue  # A partial sale never closes remaining shares.
                    if item is None and kind != "closed":
                        plans = state.get("entry_plans", {})
                        signal = fact.get("signal_date") or (plans.get(identity, {}).get("signal_date")
                            if isinstance(plans, dict) else None)
                        item = next((row for row in reversed(matching)
                            if row["state"] not in _TERMINAL | {"HELD", "EXIT_PENDING"}
                            and (row.get("signal_date") == signal if signal
                                 else row.get("signal_date") and fact.get("entry_date")
                                 and row["signal_date"] < fact["entry_date"])), None)
                    if item is None:
                        oid = "opp_" + _digest([account_kind, ticker, fact.get("plan_id"), fact.get("entry_date"), fact.get("signal_date")])[:32]
                        item = {"opportunity_id": oid, "ticker": ticker, "source_run_id": None,
                            "signal_date": fact.get("signal_date") or fact.get("entry_date"),
                            "observed_at": observed_at, "expires_at": observed_at,
                            "state": target, "qualified_at_signal": False, "decision": "account_fact",
                            "levels": {"entry": None, "stop": None, "protect": None}, "initial_stop": None,
                            "rejection_reasons": [], "source_ids": [], "entry_template": "",
                            "plan": None, "filled_shares": 0, "pending_protection_stop": None}
                        if any(row["opportunity_id"] == oid for row in self._state["opportunities"]):
                            item = self._opportunity(oid)
                        else:
                            self._state["opportunities"].append(item)
                    if kind == "positions":
                        previous_stop = item["levels"].get("stop") if item["state"] in {"HELD", "EXIT_PENDING"} else None
                        if positive(previous_stop) and fact["stop"] < previous_stop:
                            raise ValueError("Lifecycle account stop regression")
                        initial_stop = fact.get("initial_stop")
                        if not positive(initial_stop):
                            initial_stop = fact["stop"] if fact["stop"] < fact["entry_price"] else None
                        levels = _levels(fact["entry_price"], initial_stop)
                        levels["stop"] = float(fact["stop"])
                        item.update(levels=levels, initial_stop=initial_stop, filled_shares=fact["shares"],
                                    account_plan_id=fact.get("plan_id"), entry_date=fact.get("entry_date"))
                        if item.get("pending_protection_stop") and fact["stop"] >= item["pending_protection_stop"]:
                            item["pending_protection_stop"] = None
                    elif kind == "closed":
                        item.update(filled_shares=0, exit_date=fact.get("exit_date"), closed_fact=deepcopy(fact))
                    item["account_plan_id"] = identity or item.get("account_plan_id")
                    item["state"] = target
                    self._event("ACCOUNT_" + target, observed_at, item["opportunity_id"],
                                snapshot_id=snapshot_id, account_kind=account_kind, fact_hash=_digest(fact))
            for identity, plan in plans.items():
                if plan["status"] != "cancelled" or plan["ticker"] in held_symbols:
                    continue
                if any(fact.get("plan_id") == identity for fact in state.get("pending", [])):
                    raise ValueError("Lifecycle cancelled account plan is still pending")
                for item in self._state["opportunities"]:
                    if (item.get("account_plan_id") != identity or item["state"] in _TERMINAL
                            or item["filled_shares"] > 0):
                        continue
                    if item["ticker"] != plan["ticker"]:
                        raise ValueError("Lifecycle cancelled account plan identity conflicts")
                    item["state"] = "INVALIDATED"
                    cancelled = self._cancel_intents(item, cause="account_cancellation", observed_at=observed_at)
                    self._event("ACCOUNT_CANCELLED", observed_at, item["opportunity_id"],
                        snapshot_id=snapshot_id, account_kind=account_kind, plan_id=identity,
                        fact_hash=_digest(plan), reason=plan.get("terminal_reason") or plan.get("cancel_reason") or "ACCOUNT_PLAN_CANCELLED",
                        cancelled_intent_ids=cancelled)
            self._state["account_snapshots"].append({"snapshot_id": snapshot_id,
                "body_hash": digest, "observed_at": observed_at, "account_kind": account_kind})
            self._dirty = True
        except Exception:
            self._state = before
            self._dirty = was_dirty
            raise
        return self.snapshot()

    def record_outcomes(self, outcomes: list[dict], *, observed_at: str) -> dict:
        now = aware_time(observed_at)
        if not isinstance(outcomes, list):
            raise ValueError("Lifecycle outcomes list required")
        updated = deepcopy(self._state["cohort_outcomes"])
        known = {row["opportunity_id"]: row for row in updated}
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                raise ValueError("Lifecycle outcome invalid")
            self._opportunity(outcome.get("opportunity_id"))
            if aware_time(outcome.get("outcome_as_of")) > now:
                raise ValueError("Lifecycle future outcome")
            _digest(outcome)
            previous = known.get(outcome["opportunity_id"])
            if previous and previous.get("horizon_complete") and previous != outcome:
                raise ValueError("Lifecycle terminal outcome immutable")
            if previous and aware_time(previous["outcome_as_of"]) > aware_time(outcome["outcome_as_of"]):
                raise ValueError("Lifecycle outcome as_of regressed")
            known[outcome["opportunity_id"]] = deepcopy(outcome)
        new = list(known.values())
        if new != self._state["cohort_outcomes"]:
            self._state["cohort_outcomes"] = new
            self._event("COHORT_OUTCOMES_RECORDED", observed_at, outcomes_hash=_digest(new))
        return self.snapshot()

    def save(self) -> None:
        self._validate(self._state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock the parent directory so no extra durable account file or stale
        # lock inode is introduced. The head check also rejects stale objects.
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                current = hashlib.sha256(self.path.read_bytes()).hexdigest()
            except FileNotFoundError:
                current = None
            if current != self._head:
                raise ValueError("Lifecycle ledger revision conflict; reload before saving")
            if not self._dirty:
                return
            updated = deepcopy(self._state)
            updated["revision"] += 1
            write_json_atomic(self.path, updated)
            self._state = updated
            self._head = hashlib.sha256(self.path.read_bytes()).hexdigest()
            self._dirty = False
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
