"""Report trust boundaries: hostile inputs remain text in the rendered document."""
from __future__ import annotations

from html.parser import HTMLParser

import pytest

from trading_system.data_models import (
    ChainLink, ChainState, DimensionScore, MRSResult, PipelineResult,
    SectorScore, StockCandidate, TradePick,
)
from trading_system.report_html import _kv, _sim_tab, _lifecycle_card, render_html


class Document(HTMLParser):
    def __init__(self, markup: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.text: list[str] = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)


def _result(text: str) -> PipelineResult:
    candidate = StockCandidate(text, sector_etf=text, chain_id=text,
                               price=100, key_level=100, stop_plan=text,
                               entry_template=text, s_options=None)
    pick = TradePick(text, 8, 8, text, 100, 95, 10, 0.01, 50,
                     chain=text, sector=text, card=text)
    market = MRSResult(8, 0, 1, 8, regime=text,
                       dimensions={text: DimensionScore(text, None)},
                       position_cap=(0.4, 0.7))
    chain = ChainState(text, text, 8, text, leading_link=text,
                       links={text: ChainLink(text, [])}, rotation_signal=text)
    return PipelineResult(
        text, text, mrs=market, watchlist=[candidate], picks=[pick],
        sectors=[SectorScore(text, 8, factors={text: 8})], chains=[chain],
        action="BUY", market_view=text, notes=[text],
        raw={"pick_rationale": {text: {"mode": text, "entry": 100, "stop": 95,
                                     "shares": 10, "r_usd": 50}},
             "data_coverage": text, "elapsed_s": text,
             "redline": [{"step": text, "status": "passthrough", "ms": text,
                          "note": text}]},
    )


def _journal(text: str):
    return [{"date": text, "ticker": text, "status": "open", "mode": text,
             "entry_ref": 100, "stop": 95, "template": text, "sector": text,
             "chain": text, "tss_final": 8, "mrs_star": 8}]


def _simulation(text: str):
    return {
        "state": {
            "equity_curve": [], "ops_log": [{"date": text, "ops": [text]}],
            "positions": [{"ticker": text, "shares": 10, "entry_price": 100,
                           "stop": 95, "risk_usd": 50, "entry_date": text,
                           "chain": text}],
            "pending": [],
            "closed": [{"ticker": text, "entry_date": text, "exit_date": text,
                        "entry": 100, "exit": 110, "shares": 10,
                        "pnl_usd": 100, "gross_r": 2, "friction_cost": 0,
                        "r_multiple": 2, "days": 1, "reason": text}],
        },
        "stats": {"cum_return": 0, "days": 1, "equity": 100_000,
                  "cash": 99_000, "invested": 1_000, "n_closed": 1,
                  "win_rate": 1, "max_drawdown": 0, "expectancy_r": 2,
                  "profit_factor": None},
    }


@pytest.mark.parametrize("text", [
    "<img src=x onerror=window.__tiger_injected=1>",
    "</script><script>window.__tiger_injected=1</script>",
    "<svg/onload=window.__tiger_injected=1>",
    "' autofocus onfocus='window.__tiger_injected=1",
    '研发 & 云 <AI> "中文"',
])
def test_untrusted_fields_are_text_across_report_surfaces(text):
    markup = render_html(_result(text), journal_entries=_journal(text),
                         sim=_simulation(text))
    document = Document(markup)
    assert sum(tag == "script" for tag, _ in document.tags) == 1
    assert not any("__tiger_injected" in (value or "")
                   for _, attrs in document.tags for name, value in attrs
                   if name.startswith("on") or name in {"src", "href"})
    assert text in "".join(document.text), "input must remain readable, escaped once"
    assert sum(tag == "svg" for tag, _ in document.tags) >= 3
    assert any(tag == "table" for tag, _ in document.tags)
    assert any(tag == "button" for tag, _ in document.tags)


def test_no_picks_watchlist_is_also_escaped():
    text = "<img src=x onerror=window.__tiger_injected=1>"
    result = _result(text)
    result.picks = []
    document = Document(render_html(result))
    assert text in "".join(document.text)
    assert not any("__tiger_injected" in (value or "")
                   for _, attrs in document.tags for name, value in attrs
                   if name.startswith("on") or name in {"src", "href"})


def test_key_value_builder_accepts_text_only():
    text = '研发 & 云 <AI> "中文"'
    document = Document(_kv(text, text))
    assert "".join(document.text).count(text) == 2
    assert [tag for tag, _ in document.tags] == ["div", "b"]


def test_raw_journal_and_simulation_values_cannot_create_nodes():
    text = "<img src=x onerror=window.__tiger_injected=1>"
    entry = _journal("AAPL")[0]
    entry.update(entry_ref=text, stop=text, note=text)
    sim = _simulation("AAPL")
    sim["state"]["closed"][0]["gross_r"] = text
    document = Document(_lifecycle_card(entry) + _sim_tab(sim))
    assert text in "".join(document.text)
    assert not any(tag == "img" for tag, _ in document.tags)


def test_risk_disclosure_uses_configuration_and_avoids_loss_guarantees(monkeypatch):
    from trading_system import config
    monkeypatch.setattr(config, "RISK_R_PCT", 0.006)
    monkeypatch.setattr(config, "MAX_SINGLE_POSITION_PCT", 0.15)
    markup = render_html(_result("AAPL"), sim=_simulation("2026-10-02"))
    assert "0.6%" in markup and "15%" in markup
    assert "实际亏损可能因跳空、滑点与费用超过计划" in markup
    assert "从此不亏钱" not in markup
    assert "单笔最大亏损锁定" not in markup


@pytest.mark.parametrize("market_score,recorded", [(5.6, True), (8.0, False)])
def test_gate_table_displays_recorded_decisions_without_recomputing_thresholds(market_score, recorded):
    from trading_system.report_html import _decision_card
    result = _result("AAPL")
    result.mrs.mrs_star = market_score
    result.raw["pick_rationale"]["AAPL"]["gate"] = {
        key: {"ok": recorded} for key in ("mrs", "shs", "tss")
    }
    markup = _decision_card(result, result.picks[0])
    table = markup.split("<table>", 1)[1].split("</table>", 1)[0]
    assert table.count("✓") == (3 if recorded else 0)
    assert table.count("✗") == (0 if recorded else 3)


def test_missing_gate_record_is_disclosed_instead_of_inventing_passes():
    from trading_system.report_html import _decision_card
    result = _result("AAPL")
    markup = _decision_card(result, result.picks[0])
    table = markup.split("<table>", 1)[1].split("</table>", 1)[0]
    assert "✓" not in table
    assert table.count("未记录") == 3


def test_effective_exit_parameters_and_entry_day_are_disclosed():
    from dataclasses import asdict
    from trading_system.parameters import GateParams
    from trading_system.report_html import _decision_card
    result = _result("AAPL")
    result.raw["gate_params"] = asdict(GateParams(time_stop=3, profit_protect_r=3))
    result.picks[0].time_stop_days = 3
    markup = _decision_card(result, result.picks[0])
    assert "入场日起第 3 个交易日" in markup
    assert "3R 保护 115.00" in markup
    assert "下一根" in markup
    assert "两倍风险" not in markup
    assert "5–7 个交易日" not in markup


@pytest.mark.parametrize("snapshot", [
    {"profit_protect_r": float("nan")},
    {"time_stop": -1},
    {"unknown": 3},
    "not a parameter object",
])
def test_invalid_effective_parameter_snapshot_fails_closed(snapshot):
    result = _result("AAPL")
    result.raw["gate_params"] = snapshot
    with pytest.raises(ValueError):
        render_html(result)


def test_missing_dimensions_use_exclusion_and_renormalization_language():
    from trading_system.report_html import _dim_reading
    assert "剔除" in _dim_reading("macro", None)
    assert "归一化" in _dim_reading("macro", None)
    assert "中性处理" not in render_html(_result("AAPL"))


def test_market_and_amount_currency_come_from_this_run():
    result = _result("AAPL")
    result.raw["market"] = {"market_id": "hk", "name": "香港证券市场", "short_name": "港股", "currency": "HKD"}
    result.raw["account_currency"] = "HKD"
    markup = render_html(result)
    assert "本报告市场：<b style='color:#ffd700'>港股</b>" in markup
    assert "本轮金额币种：HKD" in markup


def test_paper_ledger_initial_cash_and_currency_are_preserved_in_the_view():
    simulation = _simulation("2026-10-02")
    simulation["state"].update(currency="HKD", initial_cash=25_000,
                               equity_curve=[{"date": "2026-10-02", "equity": 26_000}])
    markup = _sim_tab(simulation)
    assert "初始资金 HKD 25,000" in markup
    assert "初始 HKD 25,000" in markup
    assert "data-currency='HKD '" in markup
    assert "$100,000" not in markup
    assert "$50" not in markup


def test_run_and_paper_ledger_currency_conflict_fails_closed():
    result = _result("AAPL")
    result.raw["account_currency"] = "CNY"
    simulation = _simulation("2026-10-02")
    simulation["state"]["currency"] = "HKD"
    with pytest.raises(ValueError):
        render_html(result, sim=simulation)


def test_philosophy_uses_the_current_market_and_effective_exit_policy():
    from dataclasses import asdict
    from trading_system.parameters import GateParams
    result = _result("0700.HK")
    result.raw.update(
        market={"market_id": "hk", "short_name": "港股", "currency": "HKD"},
        account_currency="HKD",
        gate_params=asdict(GateParams(time_stop=3, profit_protect_r=3)),
    )
    markup = render_html(result)
    philosophy = markup.split("<div id='tab-philosophy'", 1)[1].split("<div class='footer'", 1)[0]
    assert "投资标的：<b style='color:#1c2a10'>港股</b>" in philosophy
    assert "本轮入场日起第 3 个交易日" in philosophy
    assert "本轮浮盈达到 3R" in philosophy
    assert "按盘中高点计算" in philosophy
    assert "下一根" in philosophy
    assert "10 万美元虚拟资金" not in philosophy
    assert "浮盈达两倍风险必须锁利" not in philosophy


def test_light_permission_does_not_turn_into_failed_gate_in_the_summary():
    result = _result("AAPL")
    result.action = "LIGHT"
    result.mrs.mrs_star = 5.6
    result.mrs.allow_new_positions = True
    result.raw["pick_rationale"]["AAPL"]["gate"] = {
        key: {"ok": True} for key in ("mrs", "shs", "tss")
    }
    markup = render_html(result)
    summary = markup.split("<h2>今日决策</h2>", 1)[1].split("<h2>一、", 1)[0]
    assert "轻仓试错" in summary
    assert "没过关" not in summary


def test_standalone_report_does_not_publish_a_missing_pdf_attachment_link():
    document = Document(render_html(_result("AAPL")))
    assert not any(tag == "a" and (value or "").endswith(".pdf")
                   for tag, attrs in document.tags for name, value in attrs
                   if name == "href")


def test_friction_disclosure_uses_commission_and_this_run_fallback(monkeypatch):
    from trading_system import config
    from trading_system.parameters import GateParams
    monkeypatch.setattr(config, "SIM_COMMISSION_BPS", 15.0)
    markup = _sim_tab(_simulation("2026-10-02"), params=GateParams(cost_bps=41))
    assert "单边15bp" in markup
    assert "无成交额数据时本轮兜底单边41bp" in markup
    assert "单边10bp" not in markup


def test_stop_disclosure_obeys_market_sell_barriers():
    markup = render_html(_result("0700.HK"))
    assert "市场卖出许可" in markup
    assert "无条件离场" not in markup
    assert "跌破就走，不商量" not in markup


def test_report_consumes_the_actual_tightened_customer_risk_snapshot():
    result = _result("AAPL")
    result.raw["risk_limits"] = {
        "risk_r_pct": 0.004, "max_single_position_pct": 0.10, "gross_cap": 0.50,
    }
    markup = render_html(result, sim=_simulation("2026-10-02"))
    risk_box = markup.split("🛡️ 风控边界", 1)[1].split("📜 公开验证章程", 1)[0]
    assert "0.4%" in risk_box
    assert "10%" in risk_box
    assert "50%" in risk_box
    philosophy = markup.split("<div id='tab-philosophy'", 1)[1]
    assert "单票计划投入不超过账户 10%" in philosophy
    assert "单票计划投入不超过账户 20%" not in philosophy


@pytest.mark.parametrize("snapshot", [
    {"risk_r_pct": 0.004, "max_single_position_pct": 0.10},
    {"risk_r_pct": True, "max_single_position_pct": 0.10, "gross_cap": 0.50},
    {"risk_r_pct": 0.004, "max_single_position_pct": 0.10, "gross_cap": float("nan")},
    {"risk_r_pct": 0.004, "max_single_position_pct": 0.10, "gross_cap": 0.50, "extra": 1},
    "not a risk snapshot",
])
def test_invalid_actual_risk_snapshot_fails_closed(snapshot):
    result = _result("AAPL")
    result.raw["risk_limits"] = snapshot
    with pytest.raises(ValueError):
        render_html(result)


def test_report_rejects_contradictory_run_risk_snapshots():
    result = _result("AAPL")
    result.raw["gate_params"] = {"risk_r_pct": 0.006}
    result.raw["risk_limits"] = {
        "risk_r_pct": 0.004, "max_single_position_pct": 0.10, "gross_cap": 0.50,
    }
    with pytest.raises(ValueError):
        render_html(result)


def test_legacy_missing_risk_snapshot_is_disclosed_as_current_defaults():
    markup = render_html(_result("AAPL"), sim=_simulation("2026-10-02"))
    risk_box = markup.split("🛡️ 风控边界", 1)[1].split("📜 公开验证章程", 1)[0]
    assert "本轮风险限制未记录" in risk_box
    assert "当前默认配置" in risk_box


def test_report_does_not_claim_unrecorded_wfa_statistics():
    markup = render_html(_result("AAPL"))
    assert "折内夏普 3.92" not in markup
    assert "受审快照" in markup
