"""Actual TSS liquidity outputs must survive the v2 risk boundary.

Prices, ADV and account values are synthetic.  The TSS mapping, risk agent,
ranking and account allocator are production code; no provider/model/account
persistence is involved.
"""

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system import config
from trading_system.agents.risk_manager_agent import RiskManagerAgent
from trading_system.agents.tss_agent import TSSAgent
from trading_system.data_models import MRSResult, SectorScore, StockCandidate


DAY = "2026-10-26"


def _candidate(ticker="NVDA", *, adv=100_000_000.0, coefficient=None):
    return StockCandidate(
        ticker=ticker, chain_id="semis", price=100.0, adv_usd=adv,
        atr_pct=0.02, tss=8.0, tss_final=8.0,
        c_liq=TSSAgent._c_liq(adv) if coefficient is None else coefficient,
        entry_template="A", key_level=100.0, stop_price=95.0,
    )


def _context(*candidates, policy="whitepaper-execution-v2"):
    return {
        "execution_policy": policy,
        "trade_date": DAY,
        "mrs": MRSResult(7.0, 0.0, 1.0, 7.0, position_cap=(0.4, 0.7)),
        "sectors": [SectorScore("SMH", 8.0, breadth=70.0, in_main_pool=True)],
        "chain_map": {}, "watchlist": list(candidates),
        "market_data": {"stock_ohlcv": {
            row.ticker: pd.DataFrame({"Close": [row.price]}, index=pd.to_datetime([DAY]))
            for row in candidates
        }},
        "portfolio_state": {"cash": 100_000.0, "positions": [], "pending": []},
        "portfolio_marks": {}, "portfolio_valuation_date": DAY,
    }


def _run(context, *, max_picks=config.MAX_PICKS_DEFAULT):
    return RiskManagerAgent(SimpleNamespace(name="offline-no-provider-methods"),
                            max_picks=max_picks).execute(context)


@pytest.mark.parametrize("adv,coefficient", [
    (19_999_999.0, 0.5), (20_000_000.0, 0.8), (50_000_000.0, 0.9),
    (100_000_000.0, 1.0), (200_000_000.0, 1.05), (500_000_000.0, 1.1),
])
def test_actual_tss_coefficient_survives_v2_risk_with_original_tos_and_sizing(adv, coefficient):
    candidate = _candidate(adv=adv)
    assert candidate.c_liq == coefficient
    context = _context(candidate)
    state_before = deepcopy(context["portfolio_state"])
    picks = _run(context)
    assert len(picks) == 1
    assert picks[0].tos == pytest.approx(7.0 * 8.0 * 8.0 * coefficient / 100)
    assert picks[0].shares == 160
    assert picks[0].risk_usd == 800.0
    assert context["rejections"]["candidates"][0]["status"] == "accepted"
    assert context["portfolio_state"] == state_before


@pytest.mark.parametrize("coefficient", [
    float("nan"), float("inf"), float("-inf"), -1.0, 0.0, 0.49, 1.100001,
])
def test_out_of_contract_or_nonfinite_coefficient_is_rejected(coefficient):
    candidate = _candidate(coefficient=coefficient)
    context = _context(candidate)
    assert _run(context) == []
    assert context["rejections"]["candidates"][0]["reasons"] == ["liquidity_coefficient_invalid"]


@pytest.mark.parametrize("coefficient", [True, False, "1.1", None])
def test_malformed_coefficient_is_rejected_without_sizing_or_type_error(coefficient):
    candidate = _candidate()
    candidate.c_liq = coefficient
    context = _context(candidate)
    assert _run(context) == []
    assert context["rejections"]["candidates"][0]["reasons"] == ["liquidity_coefficient_invalid"]
    assert candidate.tos == 0.0


@pytest.mark.parametrize("coefficient", [2 ** 1024, -(2 ** 1024)], ids=["huge_positive", "huge_negative"])
def test_outside_range_integer_is_rejected_before_any_float_conversion(coefficient):
    candidate = _candidate(coefficient=coefficient)
    context = _context(candidate)
    assert _run(context) == []
    assert context["rejections"]["candidates"][0]["reasons"] == ["liquidity_coefficient_invalid"]
    assert candidate.tos == 0.0


def test_high_liquidity_keeps_original_tos_ranking_under_position_limit():
    low = _candidate("AMD", adv=100_000_000.0)
    high = _candidate("NVDA", adv=500_000_000.0)
    context = _context(low, high)
    picks = _run(context, max_picks=1)
    assert [row.ticker for row in picks] == ["NVDA"]
    rejected = next(row for row in context["rejections"]["candidates"] if row["ticker"] == "AMD")
    assert rejected["status"] == "rejected"
    assert rejected["reasons"] == ["position_slots_exhausted"]


@pytest.mark.parametrize("gate", ["shock", "market", "score", "stop", "breadth", "account"])
def test_valid_high_liquidity_never_overrides_existing_hard_risk_gates(gate):
    candidate = _candidate(adv=500_000_000.0)
    context = _context(candidate)
    if gate == "shock":
        context["mrs"].shock = True
    elif gate == "market":
        context["mrs"].allow_new_positions = False
    elif gate == "score":
        candidate.tss_final = 7.0
    elif gate == "stop":
        candidate.stop_price = candidate.price
    elif gate == "breadth":
        context["sectors"][0].breadth = 30.0
        candidate.key_level = 99.0
    elif gate == "account":
        context["portfolio_state"] = None
    assert _run(context) == []
    reasons = context["rejections"]["candidates"][0]["reasons"]
    assert reasons
    assert "liquidity_coefficient_invalid" not in reasons


@pytest.mark.parametrize("adv", [100_000_000.0, 200_000_000.0, 500_000_000.0])
def test_legacy_risk_path_preserves_original_tss_tos_and_quantity(adv):
    candidate = _candidate(adv=adv)
    context = _context(candidate, policy="legacy")
    picks = _run(context)
    assert len(picks) == 1
    assert picks[0].tos == round(7.0 * 8.0 * 8.0 * TSSAgent._c_liq(adv) / 100, 2)
    assert picks[0].shares == 160
    assert picks[0].risk_usd == 800.0
