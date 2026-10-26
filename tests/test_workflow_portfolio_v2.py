"""Complete-bar and account membership boundaries, without provider calls."""
from copy import deepcopy

import pandas as pd
import pytest

from trading_system.data_models import StockCandidate
from trading_system.workflow_v2 import account_symbols, portfolio_inputs, include_management_candidates


def frame(day="2026-10-26", price=100.):
    return pd.DataFrame({"Open": [price], "High": [price+1], "Low": [price-1],
                         "Close": [price], "Volume": [1000000]}, index=pd.to_datetime([day]))


def test_pending_and_unfilled_plans_remain_in_data_request_without_duplicate_slots():
    state = {"cash": 100000, "positions": [{"ticker": "A"}],
             "pending": [{"ticker": "B"}], "entry_plans": {
                 "p": {"ticker": "B", "status": "partial"},
                 "q": {"ticker": "C", "status": "pending"},
                 "r": {"ticker": "D", "status": "closed"}}}
    assert account_symbols(state) == ["A", "B", "C"]


@pytest.mark.parametrize("state", [None, {"positions": {}}, {"pending": [None]},
                                     {"entry_plans": []}, {"positions": [{"ticker": "bad symbol"}]}])
def test_malformed_account_membership_is_not_an_empty_account(state):
    with pytest.raises(ValueError):
        account_symbols(state)


def test_portfolio_valuation_uses_same_complete_bar_not_last_price_or_fixed_initial_cash():
    state = {"cash": 50000, "initial_cash": 100000, "account_currency": "USD",
        "positions": [{"ticker": "A", "shares": 100, "entry_price": 80., "stop": 75.,
                       "last_price": 999., "sector": "XLK"}], "pending": []}
    before = deepcopy(state)
    value = portfolio_inputs(state, {"A": frame(price=120.)}, "2026-10-26")
    assert value["portfolio_snapshot"]["nav"] == 62000
    assert value["portfolio_marks"] == {"A": {"date": "2026-10-26", "price": 120.}}
    assert state == before
    assert value["portfolio_state"] == before


@pytest.mark.parametrize("bars", [{}, {"A": frame("2026-10-23")}, {"A": frame("2026-10-27")}])
def test_missing_stale_or_future_held_marks_block_sizing(bars):
    state = {"cash": 50000, "account_currency": "USD", "positions": [
        {"ticker": "A", "shares": 100, "entry_price": 80., "stop": 75.}], "pending": []}
    with pytest.raises(ValueError):
        portfolio_inputs(state, bars, "2026-10-26")


def test_held_candidate_outside_scan_is_scored_and_not_counted_as_new_scan_pick():
    selected = [StockCandidate("A", price=100.)]
    state = {"positions": [{"ticker": "B", "sector": "XLK", "chain": "logic"}],
             "pending": [], "entry_plans": {}}
    result = include_management_candidates(selected, state, {"B": frame(price=80.)})
    assert [row.ticker for row in result] == ["A", "B"]
    assert result[1].sector_etf == "XLK"
    assert result[1].price == 80.
    assert result[1].evidence == ["既有账户管理标的；扫描名额之外纳入当前评分，不能重复新开仓"]
    assert len(selected) == 1
