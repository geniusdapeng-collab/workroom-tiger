"""收益稳定官按观测日期对齐，不能把不同市场的数组下标当成同一天。"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev

import pytest

from trading_system.portfolio import ReturnSteward


def _write_curve(root: Path, market: str, points: list[tuple[str, float]]) -> Path:
    directory = root / market
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sim_portfolio.json"
    path.write_text(json.dumps({
        "cash": points[-1][1] if points else 0,
        "equity_curve": [{"date": day, "equity": equity} for day, equity in points],
    }), encoding="utf-8")
    return directory


def _daily_points(values: list[float]) -> list[tuple[str, float]]:
    start = date(2026, 8, 1)
    return [((start + timedelta(days=i)).isoformat(), value)
            for i, value in enumerate(values)]


def test_unequal_end_dates_keep_latest_loss_and_disclose_stale_market(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100, 120, 80]))
    cn = _write_curve(tmp_path, "cn", _daily_points([100, 100]))

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    # 08-01: 200; 08-02: 220; 08-03: 180 (cn 延续 08-02 的 100)。
    assert report.days == 3
    assert report.total_return == -0.1
    assert report.max_drawdown == round(180 / 220 - 1, 4)
    assert report.days_below_high == 1
    assert report.verdict == "告警"
    assert any("cn" in note and "2026-08-02" in note and "1" in note
               and "滞后" in note for note in report.notes)


def test_missing_middle_dates_use_prior_value_and_only_observed_union_dates(tmp_path):
    us = _write_curve(tmp_path, "us", [
        ("2026-08-01", 100), ("2026-08-03", 60), ("2026-08-05", 100),
    ])
    cn = _write_curve(tmp_path, "cn", [
        ("2026-08-01", 100), ("2026-08-02", 140), ("2026-08-05", 100),
    ])

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    # 日期并集 01/02/03/05，净值 200/240/200/200；不造出 08-04 的观测。
    assert report.days == 4
    assert report.total_return == 0.0
    assert report.max_drawdown == round(200 / 240 - 1, 4)
    assert report.days_below_high == 2
    assert report.verdict == "告警"


def test_different_inception_dates_never_backfill_a_market_before_its_start(tmp_path):
    us = _write_curve(tmp_path, "us", [
        ("2026-08-01", 1000), ("2026-08-02", 1200), ("2026-08-04", 1600),
    ])
    cn = _write_curve(tmp_path, "cn", [
        ("2026-08-03", 100), ("2026-08-04", 110),
    ])

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    # 完整组合始于 08-03 的 1200 + 100，不能从 08-01 的单市余额推算收益。
    assert report.days == 2
    assert report.total_return == round(1710 / 1300 - 1, 4)
    assert report.max_drawdown == 0.0
    assert report.sharpe_rolling is None
    assert any("2026-08-03" in note and "起" in note for note in report.notes)


def test_disjoint_histories_carry_known_values_with_staleness_disclosure(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100, 120]))
    cn = _write_curve(tmp_path, "cn", [
        ("2026-08-05", 100), ("2026-08-06", 110),
    ])

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 2
    assert report.total_return == round(230 / 220 - 1, 4)
    assert any("us" in note and "2026-08-02" in note and "4" in note
               and "滞后" in note for note in report.notes)


def test_sharpe_and_underwater_days_use_the_aligned_series(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points(
        [100, 105, 103, 111, 107, 115, 110, 118, 112, 121, 116, 125]))
    cn = _write_curve(tmp_path, "cn", [
        ("2026-08-01", 100), ("2026-08-03", 95), ("2026-08-05", 105),
        ("2026-08-07", 90), ("2026-08-09", 100), ("2026-08-11", 95),
    ])
    expected = [200, 205, 198, 206, 212, 220, 200, 208, 212, 221, 211, 220]
    returns = [current / previous - 1 for previous, current
               in zip(expected, expected[1:])]

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 12
    assert report.total_return == 0.1
    assert report.max_drawdown == round(200 / 220 - 1, 4)
    assert report.sharpe_rolling == round(mean(returns) / pstdev(returns) * math.sqrt(252), 2)
    assert report.days_below_high == 2
    assert report.verdict == "关注"


def test_unique_out_of_order_dates_are_sorted_without_rewriting_input(tmp_path):
    points = _daily_points([100, 120, 90])
    us = _write_curve(tmp_path, "us", [points[2], points[0], points[1]])
    cn = _write_curve(tmp_path, "cn", _daily_points([100, 100, 100]))
    ledger = us / "sim_portfolio.json"
    before = ledger.read_bytes()

    report = ReturnSteward().assess({"cn": str(cn), "us": str(us)})

    assert report.days == 3
    assert report.total_return == -0.05
    assert report.max_drawdown == round(190 / 220 - 1, 4)
    assert report.days_below_high == 1
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("duplicate_value", [100, 150])
@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_dates_are_rejected_instead_of_guessing_a_balance(
        tmp_path, duplicate_value, reverse):
    points = [("2026-08-01", 100), ("2026-08-01", duplicate_value)]
    us = _write_curve(tmp_path, "us", list(reversed(points)) if reverse else points)

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 0
    assert report.verdict == "关注"
    assert report.sharpe_rolling is None
    assert any("us" in note and "重复日期" in note for note in report.notes)


@pytest.mark.parametrize("bad_date", [
    None, "", "2026-02-30", "20260801", "2026-W31-6", "2026-08-01T00:00:00",
])
def test_invalid_or_non_daily_dates_are_explicitly_unavailable(tmp_path, bad_date):
    us = _write_curve(tmp_path, "us", [(bad_date, 100), ("2026-08-02", 120)])

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 0
    assert report.verdict == "关注"
    assert any("us" in note and "日期" in note for note in report.notes)


@pytest.mark.parametrize("bad_equity", [None, True, "invalid", float("nan"), float("inf")])
def test_invalid_equity_cannot_leak_non_finite_statistics(tmp_path, bad_equity):
    us = _write_curve(tmp_path, "us", [("2026-08-01", 100), ("2026-08-02", bad_equity)])

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 0
    assert report.verdict == "关注"
    assert any("us" in note and "净值" in note for note in report.notes)
    json.dumps(report.to_dict(), allow_nan=False)


@pytest.mark.parametrize("contents", [
    "{broken", "[]", '{"equity_curve": {"date": "2026-08-01", "equity": 100}}',
    '{"equity_curve": [null]}', '{"equity_curve": [{"date": "2026-08-01"}]}',
])
def test_malformed_history_has_market_specific_diagnostic(tmp_path, contents):
    us = _write_curve(tmp_path, "us", [])
    (us / "sim_portfolio.json").write_text(contents, encoding="utf-8")

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 0
    assert report.verdict == "关注"
    assert any("us" in note for note in report.notes)


def test_unreadable_history_is_disclosed_without_hiding_other_markets(tmp_path, monkeypatch):
    us = _write_curve(tmp_path, "us", _daily_points([100, 110]))
    cn = _write_curve(tmp_path, "cn", _daily_points([100, 110]))
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if path == cn / "sim_portfolio.json":
            raise PermissionError("fixture access denied")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 2
    assert report.total_return == 0.1
    assert any("cn" in note and "未纳入" in note for note in report.notes)


@pytest.mark.parametrize("empty_history", [False, True])
def test_partial_market_coverage_is_disclosed_without_changing_verdict_policy(
        tmp_path, empty_history):
    us = _write_curve(tmp_path, "us", _daily_points([100 + i for i in range(25)]))
    cn = _write_curve(tmp_path, "cn", []) if empty_history else tmp_path / "cn"

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 25
    assert report.total_return == 0.24
    assert report.verdict == "稳定"  # 仍按现有风险阈值判断；覆盖不足另行披露。
    assert any("cn" in note and "未纳入" in note for note in report.notes)


def test_invalid_market_does_not_silently_remove_its_coverage(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100, 110]))
    cn = _write_curve(tmp_path, "cn", [("bad-date", 100)])

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 2
    assert report.total_return == 0.1
    assert any("cn" in note and "未纳入" in note and "日期" in note
               for note in report.notes)


def test_one_shared_observation_cannot_imply_return_or_sharpe_history(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100, 120, 80]))
    cn = _write_curve(tmp_path, "cn", [("2026-08-03", 100)])

    report = ReturnSteward().assess({"us": str(us), "cn": str(cn)})

    assert report.days == 1
    assert report.total_return == 0.0
    assert report.max_drawdown == 0.0
    assert report.sharpe_rolling is None
    assert report.verdict == "关注"
    assert any("样本 1 日" in note for note in report.notes)


def test_zero_starting_equity_is_unavailable_instead_of_dividing_by_zero(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([0, 100]))

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 2
    assert report.total_return == 0.0
    assert report.sharpe_rolling is None
    assert report.verdict == "关注"
    assert any("基准净值" in note for note in report.notes)


def test_total_loss_keeps_drawdown_alert_but_undefined_sharpe_is_omitted(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100] * 9 + [0, 0]))

    report = ReturnSteward().assess({"us": str(us)})

    assert report.days == 11
    assert report.total_return == -1.0
    assert report.max_drawdown == -1.0
    assert report.days_below_high == 2
    assert report.verdict == "告警"
    assert report.sharpe_rolling is None
    assert any("夏普" in note and "净值" in note for note in report.notes)


def test_no_requested_market_keeps_accumulating_report():
    report = ReturnSteward().assess({})
    assert report.days == 0
    assert report.verdict == "关注"
    assert "积累中" in report.notes[0]


def test_identical_calendars_keep_existing_return_and_thresholds(tmp_path):
    us = _write_curve(tmp_path, "us", _daily_points([100] * 24 + [90]))
    cn = _write_curve(tmp_path, "cn", _daily_points([100] * 24 + [90]))

    report = ReturnSteward(mdd_warn=0.12, mdd_alert=0.20).assess(
        {"us": str(us), "cn": str(cn)})

    assert report.days == 25
    assert report.total_return == -0.1
    assert report.max_drawdown == -0.1
    assert report.days_below_high == 1
    assert report.verdict == "稳定"
