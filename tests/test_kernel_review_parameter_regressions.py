"""MC202 linked contract: review consumes the signal's immutable parameters."""
from dataclasses import asdict

import pytest

from trading_system.parameters import GateParams
from trading_system.review.attribution import attribute_record, detect_violations


def _record(params, *, mrs=8, r=0):
    return {"date": "2026-09-28", "ticker": "AAPL", "status": "open", "entry": 100,
            "entry_ref": 100, "sector": "SMH", "chain": "semis", "protected": False,
            "mrs_star": mrs, "r_live": r, "gate_params": asdict(params)}


@pytest.mark.parametrize("protect,r,expected", [(1.5, 1.75, True), (3, 2.5, False)])
def test_mc202_review_violation_uses_signal_protection_threshold(protect, r, expected):
    record = _record(GateParams(profit_protect_r=protect), r=r)
    assert ("V6" in detect_violations(record)) == expected


def test_mc202_review_violation_uses_signal_mrs_block_threshold():
    record = _record(GateParams(mrs_block=5.5), mrs=5.4)
    assert "V1" in detect_violations(record)


def test_mc202_review_market_band_uses_signal_standard_threshold():
    record = _record(GateParams(mrs_gate=7), mrs=6.5)
    assert attribute_record(record)["signal_layer"]["mrs_band"] == "轻仓区(4-7)"


def test_mc202_review_legacy_parameters_keep_the_default_contract():
    record = _record(GateParams(), mrs=6.5, r=2.1)
    record.pop("gate_params")
    assert detect_violations(record) == ["V6"]
    assert attribute_record(record)["signal_layer"]["mrs_band"] == "可交易(6-8)"
