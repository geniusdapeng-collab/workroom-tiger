"""MC206 linked rules: dated mainland equity limits and exact tick rounding."""
from decimal import Decimal, ROUND_HALF_UP

import pytest

from trading_system.markets import get_market


CN = get_market("cn")


@pytest.mark.parametrize("ticker", ["600006.SS", "000001.SZ"])
def test_mc206_sample_current_mainboard_st_uses_ten_percent(ticker):
    assert CN.check_order("buy", ticker, 10.6, 10, "2026-07-06", name="ST夹具").allowed
    assert CN.check_order("sell", ticker, 9.4, 10, "2026-07-06",
                          buy_date="2026-07-03", name="ST夹具").allowed


def test_mc206_property_mainboard_st_preserves_the_effective_date_boundary():
    assert CN.price_limit_pct("600006.SS", "ST夹具", trade_date="2026-07-03") == .05
    assert CN.price_limit_pct("600006.SS", "ST夹具", trade_date="2026-07-06") == .10
    assert not CN.check_order("buy", "600006.SS", 10.5, 10,
                              "2026-07-03", name="ST夹具").allowed
    assert CN.check_order("buy", "600006.SS", 10.5, 10,
                         "2026-07-06", name="ST夹具").allowed
    assert not CN.check_order("buy", "600006.SS", 11, 10,
                              "2026-07-06", name="ST夹具").allowed


@pytest.mark.parametrize("ticker", ["300750.SZ", "301697.SZ", "688981.SS"])
def test_mc206_property_twenty_percent_boards_keep_their_rule_when_st_named(ticker):
    assert CN.check_order("buy", ticker, 11.5, 10, "2026-09-30", name="*ST夹具").allowed
    assert not CN.check_order("buy", ticker, 12, 10, "2026-09-30", name="*ST夹具").allowed


def test_mc206_sample_limit_prices_round_exact_half_cent_up():
    assert CN.limit_prices("600006.SS", 10.15) == (11.17, 9.14)


@pytest.mark.parametrize("ticker,name,day,ratio", [
    ("600006.SS", "ST夹具", "2026-07-03", ".05"),
    ("600006.SS", "ST夹具", "2026-07-06", ".10"),
    ("688981.SS", "*ST夹具", "2026-09-30", ".20"),
])
def test_mc206_property_limit_price_matches_decimal_tick_formula(ticker, name, day, ratio):
    for previous in ("0.15", "1.05", "3.35", "10.15", "18.25", "99.95", "100.00"):
        expected = tuple(float((Decimal(previous) * (Decimal(1) + side * Decimal(ratio)))
                               .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for side in (1, -1))
        assert CN.limit_prices(ticker, float(previous), name, trade_date=day) == expected


@pytest.mark.parametrize("previous,expected", [
    (.01, (.02, .01)), (.04, (.05, .03)), (.05, (.06, .04)),
])
def test_mc206_property_small_price_limits_move_at_least_one_tick(previous, expected):
    assert CN.limit_prices("600006.SS", previous, trade_date="2026-07-06") == expected
