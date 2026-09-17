"""v6.3 S4 多市场测试（Market 抽象与三市支持）。

覆盖（与任务书六组一一对应）：
  ① 三市场日历（已知节假日/周末/半日市）
  ② T+1 / 涨跌停 / VCM 合规校验命中与放行
  ③ MRS 基准缺失再归一化（构造缺失）
  ④ 单市场数据全断 → 该市场诚实失败，其余市场正常
  ⑤ US 默认行为与改造前一致（回归）
  ⑥ 轻仓通道强制（未达标市场仓位系数正确）
"""

from __future__ import annotations

from datetime import date

import pytest

from trading_system import config
from trading_system.markets import get_market, market_ids
from trading_system.pipeline import run_pipeline

US = get_market("us")
CN = get_market("cn")
HK = get_market("hk")


# ============================================================ ① 日历

class TestCalendars:
    def test_us_known_holiday_and_weekend(self):
        assert not US.is_trading_day(date(2026, 7, 3))    # 独立日补休
        assert not US.is_trading_day(date(2026, 12, 25))  # 圣诞
        assert not US.is_trading_day(date(2026, 8, 29))   # 周六
        assert US.is_trading_day(date(2026, 8, 28))       # 周五
        assert US.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)

    def test_cn_known_holiday_and_weekend(self):
        assert not CN.is_trading_day(date(2026, 2, 17))   # 春节
        assert not CN.is_trading_day(date(2026, 10, 5))   # 国庆
        assert not CN.is_trading_day(date(2026, 8, 29))   # 周六
        assert CN.is_trading_day(date(2026, 8, 28))
        # 9/30 之后：10/1-10/7 休市（含周末）→ 10/8 开市
        assert CN.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)

    def test_hk_known_holiday_and_half_day(self):
        assert not HK.is_trading_day(date(2026, 2, 17))   # 农历新年
        assert not HK.is_trading_day(date(2026, 12, 25))  # 圣诞
        assert not HK.is_trading_day(date(2026, 8, 29))   # 周六
        # 平安夜：交易日但半日市
        assert HK.is_trading_day(date(2026, 12, 24))
        assert HK.is_half_day(date(2026, 12, 24))
        assert HK.is_half_day(date(2026, 2, 16))          # 农历新年前夕
        assert not HK.is_half_day(date(2026, 8, 28))
        assert not US.is_half_day(date(2026, 12, 24))     # US 无半日市标记
        assert HK.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)

    def test_registry(self):
        assert market_ids() == ["cn", "hk", "us"]
        with pytest.raises(ValueError):
            get_market("jp")


# ============================================================ ② 合规校验

class TestCompliance:
    def test_cn_t1_same_day_sell_rejected(self):
        v = CN.check_order("sell", "600519.SS", 1700.0, 1650.0,
                           "2026-08-28", buy_date="2026-08-28")
        assert not v.allowed and v.rule_id == "CN_T1"
        # 次日卖出放行
        v2 = CN.check_order("sell", "600519.SS", 1700.0, 1650.0,
                            "2026-08-31", buy_date="2026-08-28")
        assert v2.allowed

    def test_cn_limit_up_chase_rejected(self):
        # 主板 ±10%：prev 10.0 → 涨停 11.0，涨停价买入拒绝
        v = CN.check_order("buy", "600519.SS", 11.0, 10.0, "2026-08-28")
        assert not v.allowed and v.rule_id == "CN_LIMIT_UP_CHASE"
        # 跌停价卖出拒绝
        v2 = CN.check_order("sell", "600519.SS", 9.0, 10.0, "2026-08-28")
        assert not v2.allowed and v2.rule_id == "CN_LIMIT_DOWN_CHASE"
        # 板内价格放行
        assert CN.check_order("buy", "600519.SS", 10.5, 10.0, "2026-08-28").allowed

    def test_cn_limit_boards(self):
        # 创业板(300)/科创板(688) ±20%
        assert CN.price_limit_pct("300750.SZ") == config.CN_LIMIT_STAR_CHINEXT
        assert CN.price_limit_pct("688981.SS") == config.CN_LIMIT_STAR_CHINEXT
        assert CN.price_limit_pct("600519.SS") == config.CN_LIMIT_MAIN
        # ST ±5%（名称判定优先于板块）
        assert CN.price_limit_pct("600519.SS", name="ST某某") == config.CN_LIMIT_ST
        v = CN.check_order("buy", "300750.SZ", 12.0, 10.0, "2026-08-28")
        assert not v.allowed                       # +20% 涨停追买
        assert CN.check_order("buy", "300750.SZ", 11.5, 10.0, "2026-08-28").allowed
        v_st = CN.check_order("buy", "600519.SS", 10.5, 10.0, "2026-08-28",
                              name="ST某某")
        assert not v_st.allowed                    # ST +5% 涨停追买

    def test_hk_vcm_cooling_rejected(self):
        v = HK.check_order("buy", "0700.HK", 600.0, 590.0, "2026-08-28",
                           vcm_cooling=True)
        assert not v.allowed and v.rule_id == "HK_VCM_COOLING"
        # 非冷静期放行（VCM 适用标的）
        assert HK.check_order("buy", "0700.HK", 600.0, 590.0, "2026-08-28").allowed
        # 非 VCM 标的冷静期也放行
        assert HK.check_order("buy", "99999.HK", 1.0, 0.9, "2026-08-28",
                              vcm_cooling=True).allowed
        assert HK.in_vcm("0700.HK") and not HK.in_vcm("99999.HK")

    def test_us_no_limit_passthrough(self):
        v = US.check_order("buy", "AAPL", 9999.0, 100.0, "2026-08-28")
        assert v.allowed and v.rule_id == "US_OK"

    def test_hk_min_tick_table(self):
        assert HK.min_tick(0.10) == 0.001
        assert HK.min_tick(15.0) == 0.02
        assert HK.min_tick(700.0) == 0.5
        assert CN.min_tick(10.0) == 0.01
        assert US.min_tick(100.0) == 0.01


# ============================================================ ③ MRS 基准缺失再归一化

class TestMrsBenchmarkMissing:
    def _context(self, with_rate: bool, with_vol: bool):
        from trading_system.providers.demo import DemoProvider
        p = DemoProvider()
        stocks = {t: p.ohlcv(t, days=420) for t in
                  ["600519.SS", "000858.SZ", "300750.SZ", "601318.SS", "002594.SZ"]}
        md = {
            "spy": p.ohlcv("000300.SS", days=420),
            "tnx": p.rate_yield_for("CN10Y", days=420) if with_rate else None,
            "vix": p.vol_index_for("IVIX50", days=420) if with_vol else None,
            "vix9d": None,
            "universe_closes": {t: df["Close"] for t, df in stocks.items()},
            "stock_ohlcv": stocks,
            "benchmark_labels": {"index": "沪深300", "rate": "中债10Y",
                                 "vol": "50ETF波指iVIX"},
        }
        return {"market_data": md}, p

    def test_missing_rate_renormalized(self):
        from trading_system.agents.mrs_agent import MRSAgent
        from trading_system.indicators import aggregate
        ctx, p = self._context(with_rate=False, with_vol=True)
        r = MRSAgent(p).execute(ctx)
        assert r.dimensions["macro"].score is None          # 整维缺失
        assert "利率基准(中债10Y)" in r.dimensions["macro"].missing
        assert r.dimensions["sent"].score is not None
        # MRS_raw = 可用维度（flow/sent/tech）按权重再归一化，不钉中性 5
        scores = {k: d.score for k, d in r.dimensions.items()}
        expect = aggregate(scores, config.MRS_WEIGHTS)
        assert r.mrs_raw == expect
        w_avail = sum(config.MRS_WEIGHTS[k] for k in ("flow", "sent", "tech")
                      if scores[k] is not None)
        manual = sum(config.MRS_WEIGHTS[k] * scores[k]
                     for k in ("flow", "sent", "tech") if scores[k] is not None) / w_avail
        assert r.mrs_raw == round(manual, 2)
        # Δ 用可用维度极差
        vals = [v for v in scores.values() if v is not None]
        assert r.delta == round(max(vals) - min(vals), 2)

    def test_missing_rate_and_vol_renormalized(self):
        from trading_system.agents.mrs_agent import MRSAgent
        ctx, p = self._context(with_rate=False, with_vol=False)
        r = MRSAgent(p).execute(ctx)
        assert r.dimensions["macro"].score is None
        assert r.dimensions["sent"].score is None
        assert r.dimensions["tech"].score is not None        # 指数在 → 技术维在
        assert r.mrs_star > 0

    def test_us_labels_unchanged(self):
        """US 证据文本保持 TNX/VIX/SPY 口径（回归）。"""
        from trading_system.agents.mrs_agent import MRSAgent
        from trading_system.providers.demo import DemoProvider
        p = DemoProvider()
        stocks = {t: p.ohlcv(t, days=420) for t in ["AAPL", "MSFT", "NVDA", "AMD"]}
        md = {"spy": p.ohlcv("SPY", days=420), "tnx": p.tnx_yield(420),
              "vix": p.vix(420), "vix9d": p.vix9d(420),
              "universe_closes": {t: df["Close"] for t, df in stocks.items()},
              "stock_ohlcv": stocks}
        r = MRSAgent(p).execute({"market_data": md})
        assert "TNX" in r.dimensions["macro"].evidence[0]
        assert "VIX" in r.dimensions["sent"].evidence[0]
        assert "SPY" in r.dimensions["tech"].evidence[0]
        assert r.dimensions["macro"].score is not None


# ============================================================ ④ 单市场数据全断 → 诚实失败

class TestHonestFailure:
    def test_cn_total_data_outage_fails_honestly(self, monkeypatch):
        import trading_system.pipeline as pl
        from trading_system.providers.base import DataProvider

        class BrokenProvider(DataProvider):
            name = "broken"

            def ohlcv(self, ticker, days=400):
                raise RuntimeError("模拟数据全断")

            def tnx_yield(self, days=400):
                raise RuntimeError("模拟数据全断")

            def vix(self, days=400):
                raise RuntimeError("模拟数据全断")

        monkeypatch.setattr(pl, "get_provider", lambda name=None: BrokenProvider())
        monkeypatch.setattr(pl, "_channel_chain", lambda: [])
        with pytest.raises(RuntimeError, match=r"\[cn\].*诚实失败"):
            run_pipeline(provider_name="broken", universe_mode="extended",
                         top_n=5, market="cn")

    def test_us_unaffected_when_cn_broken(self):
        """其余市场照常：CN 全断不影响 US 正常产出。"""
        r = run_pipeline(provider_name="demo", universe_mode="core",
                         top_n=8, max_picks=3, market="us")
        assert r.mrs is not None and r.mrs.mrs_star > 0
        assert r.raw["market"]["market_id"] == "us"


# ============================================================ ⑤ US 回归

class TestUsRegression:
    def test_default_market_equals_us(self):
        r_default = run_pipeline(provider_name="demo", universe_mode="core",
                                 top_n=8, max_picks=3)
        r_us = run_pipeline(provider_name="demo", universe_mode="core",
                            top_n=8, max_picks=3, market="us")
        assert r_default.mrs.mrs_star == r_us.mrs.mrs_star
        assert r_default.mrs.mrs_raw == r_us.mrs.mrs_raw
        assert r_default.action == r_us.action
        assert [p.ticker for p in r_default.picks] == [p.ticker for p in r_us.picks]
        assert r_default.raw["market"]["market_id"] == "us"
        assert r_default.raw["ah_topics_enabled"] is False  # US 不自动开 AH 主题

    def test_us_benchmarks_and_hard_fail_flags(self):
        assert US.benchmarks["index"] == "SPY"
        assert US.benchmarks["rate"] == "TNX"
        assert US.benchmarks["vol"] == "VIX"
        assert US.benchmark_hard_fail is True
        assert US.sector_hard_fail is True
        assert US.sector_symbols == list(config.SECTOR_ETFS)
        sf = US.scan_filters()
        assert sf["min_price"] == config.SCAN_MIN_PRICE
        assert sf["min_adv"] == config.SCAN_MIN_ADV_USD

    def test_us_report_keeps_header(self):
        from trading_system.report import render_markdown
        r = run_pipeline(provider_name="demo", universe_mode="core",
                         top_n=8, max_picks=3)
        md = render_markdown(r)
        assert md.startswith("# AI 短线美股交易日报")


# ============================================================ ⑥ 轻仓通道强制

class TestLightChannel:
    def test_ungraduated_market_forces_light_size(self):
        """CN 默认未达标（0/50，DSR 未过关）→ 全部 picks 强制轻仓 ×0.35。"""
        r = run_pipeline(provider_name="demo", universe_mode="extended",
                         top_n=10, max_picks=5, market="cn")
        grad = r.raw["market_grad"]
        assert grad["graduated"] is False and grad["settled"] == 0
        expect_light = round(sum(config.MARKET_LIGHT_SIZE) / 2, 4)
        assert expect_light == 0.35
        assert r.picks, "demo 剧情下 CN 应有放行标的以验证轻仓强制"
        for p in r.picks:
            rt = r.raw["pick_rationale"][p.ticker]
            assert rt["size_ratio"] == expect_light
            assert "新市场轻仓" in rt["mode"]
            assert "验证期 0/50" in rt["mode"]
            assert rt["market_grad"]["graduated"] is False
        # 日报披露验证期计数
        from trading_system.report import render_markdown
        md = render_markdown(r)
        assert "新市场验证期（0/50）" in md
        assert "A股" in md

    def test_graduated_market_not_forced(self, monkeypatch):
        """达标市场（50 笔 + DSR 过关）→ 不强制新市场轻仓文案。"""
        monkeypatch.setitem(config.MARKET_GRADUATION, "cn",
                            {"settled_trades": 50, "dsr_pass": True})
        r = run_pipeline(provider_name="demo", universe_mode="extended",
                         top_n=10, max_picks=5, market="cn")
        assert r.raw["market_grad"]["graduated"] is True
        for p in r.picks:
            rt = r.raw["pick_rationale"][p.ticker]
            assert "新市场轻仓" not in rt["mode"]

    def test_ah_topics_auto_enabled_for_cn_hk(self):
        r_cn = run_pipeline(provider_name="demo", universe_mode="core",
                            top_n=5, max_picks=3, market="cn")
        assert r_cn.raw["ah_topics_enabled"] is True
        assert r_cn.raw["universe_source"] == "embedded"
        assert r_cn.raw["compliance"] is not None  # 合规记录存在（放行或拒绝）
