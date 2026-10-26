"""Pure account/data integration for the versioned whitepaper workflow.

Accounting inputs are explicit. No cached last price, initial reference capital,
or empty-account substitute can stand in for the current dated portfolio.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

from .data_models import StockCandidate
from .execution_policy import portfolio_snapshot


def execution_policy_identity() -> dict:
    root = Path(__file__).resolve().parent
    names = ("execution_policy.py", "exit_engine.py", "simulator.py", "config.py",
             "gate.py", "currency.py", "calendar.py")
    files = [{"path": "trading_system/" + name,
              "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
             for name in names]
    value = {"schema": "tiger.execution-policy-source.v2",
             "execution_policy": "whitepaper-execution-v2", "files": files}
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return value


def account_symbols(state: dict) -> list[str]:
    if not isinstance(state, dict):
        raise ValueError("Explicit portfolio state required")
    collections = []
    for name in ("positions", "pending"):
        rows = state.get(name, [])
        if not isinstance(rows, list):
            raise ValueError("Portfolio membership collection invalid")
        collections.extend(rows)
    plans = state.get("entry_plans", {})
    if not isinstance(plans, dict):
        raise ValueError("Portfolio entry plans invalid")
    for plan in plans.values():
        if not isinstance(plan, dict) or plan.get("status") not in {"pending", "partial", "filled", "cancelled", "closed"}:
            raise ValueError("Portfolio plan state invalid")
        if plan["status"] in {"pending", "partial"}:
            collections.append(plan)
    symbols = []
    for row in collections:
        symbol = row.get("ticker") if isinstance(row, dict) else None
        if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", symbol) is None:
            raise ValueError("Portfolio ticker invalid")
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def portfolio_inputs(state: dict, stock_data: dict, valuation_date: str) -> dict:
    marks = {}
    for symbol in account_symbols(state):
        frame = stock_data.get(symbol)
        if frame is None or not len(frame) or "Close" not in frame:
            raise ValueError("Current account valuation missing: " + symbol)
        day, price = frame.index[-1].date().isoformat(), float(frame["Close"].iloc[-1])
        if day != valuation_date or not math.isfinite(price) or price <= 0:
            raise ValueError("Account mark is stale, future or invalid: " + symbol)
        marks[symbol] = {"date": day, "price": price}
    snapshot = portfolio_snapshot(state, marks, valuation_date)
    return {"portfolio_state": deepcopy(state), "portfolio_marks": marks,
            "portfolio_snapshot": snapshot}


def include_management_candidates(candidates: list[StockCandidate], state: dict,
                                  stock_data: dict) -> list[StockCandidate]:
    result, selected = list(candidates), {row.ticker for row in candidates}
    facts = {row["ticker"]: row for row in [*state.get("positions", []), *state.get("pending", [])]}
    for symbol in account_symbols(state):
        if symbol in selected:
            continue
        frame = stock_data.get(symbol)
        if frame is None or not len(frame):
            raise ValueError("Management candidate market data missing: " + symbol)
        row, close = facts.get(symbol, {}), frame["Close"]
        price = float(close.iloc[-1])
        adv = float((close * frame["Volume"]).tail(20).mean()) if "Volume" in frame else 0.
        if not math.isfinite(price) or price <= 0 or not math.isfinite(adv) or adv < 0:
            raise ValueError("Management candidate market data invalid: " + symbol)
        result.append(StockCandidate(ticker=symbol, sector_etf=row.get("sector", ""),
            chain_id=row.get("chain", ""), price=price, adv_usd=adv,
            evidence=["既有账户管理标的；扫描名额之外纳入当前评分，不能重复新开仓"]))
        selected.add(symbol)
    return result
