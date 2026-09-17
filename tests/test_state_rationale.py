"""v5.2 零基线纪律 + 决策依据透传 测试。

锁定四条用户硬性约束：
  1. purge_run_state 每轮清除上一轮残留（搜索缓存/全市场清单缓存）
  2. 白名单文件（journal.json 会计台账 / tuned_params.json）绝不被清除
  3. pipeline 默认不加载上一轮 WFA 调优参数（use_tuned=False 零基线）
  4. 放行标的的判定要素（pick_rationale）结构化透传，报告层无需反推
"""

from __future__ import annotations

import json
import os

import pytest

from trading_system.state import purge_run_state


# ---------------------------------------------------------------- 零基线
def test_purge_removes_stale_cache(tmp_path):
    stale_dir = tmp_path / "cache" / "search"
    stale_dir.mkdir(parents=True)
    (stale_dir / "stale_a.json").write_text("{}")
    (stale_dir / "stale_b.json").write_text("{}")
    (tmp_path / "cache" / "universe_full.json").write_text('{"ts":1,"tickers":[]}')
    rep = purge_run_state(str(tmp_path))
    assert not stale_dir.exists()
    assert not (tmp_path / "cache" / "universe_full.json").exists()
    assert rep[os.path.join("cache", "search")].startswith("removed")


def test_purge_keeps_whitelist(tmp_path):
    """会计台账与调优产物即使同名出现在目标路径附近也绝不触碰。"""
    (tmp_path / "cache" / "search").mkdir(parents=True)
    # 白名单文件恰好也叫这两个名字时（极端情况）必须跳过
    (tmp_path / "cache" / "journal.json").write_text("[]")
    (tmp_path / "cache" / "tuned_params.json").write_text("{}")
    purge_run_state(str(tmp_path))
    assert (tmp_path / "cache" / "journal.json").exists()
    assert (tmp_path / "cache" / "tuned_params.json").exists()


def test_purge_idempotent_on_clean(tmp_path):
    rep = purge_run_state(str(tmp_path))
    assert all(v == "absent" for v in rep.values())


# ---------------------------------------------------------------- 调优参数默认不加载
def test_pipeline_does_not_autoload_tuned(monkeypatch):
    """零基线：默认（use_tuned=False）pipeline 绝不读取上一轮 tuned_params.json。"""
    calls = {"n": 0}
    import trading_system.backtest as bt
    orig = bt.apply_tuned_params

    def spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(bt, "apply_tuned_params", spy)
    from trading_system.pipeline import run_pipeline
    res = run_pipeline(provider_name="demo", universe_mode="core",
                       top_n=8, max_picks=3, trade_date="2026-07-30")
    assert calls["n"] == 0, "默认路径禁止加载历史调优参数"
    assert res.raw["account_usd"] == 100_000

    res2 = run_pipeline(provider_name="demo", universe_mode="core",
                        top_n=8, max_picks=3, trade_date="2026-07-30",
                        use_tuned=True)
    assert calls["n"] == 1, "显式 use_tuned=True 才允许加载"


# ---------------------------------------------------------------- 决策依据透传
def test_pick_rationale_transparent():
    """每个放行标的必须有完整判定要素：三闸门、R反推、TOS、通道。"""
    from trading_system.pipeline import run_pipeline
    res = run_pipeline(provider_name="demo", universe_mode="core",
                       top_n=8, max_picks=3, trade_date="2026-07-30")
    ra = res.raw.get("pick_rationale", {})
    assert set(ra) == {p.ticker for p in res.picks}, "放行标的与依据必须一一对应"
    for t, info in ra.items():
        assert info["mode"] in ("标准做多", "轻仓试错")
        g = info["gate"]
        for gate in ("mrs", "shs", "tss"):
            assert g[gate]["ok"], f"{t} 被放行但 {gate} 门未过——逻辑矛盾"
        # R 反推一致性：r_usd = account × r_pct × size_ratio
        assert info["r_usd"] == pytest.approx(
            info["account"] * info["r_pct"] * info["size_ratio"], rel=1e-2)
        # 股数反推一致性
        assert info["shares"] == int(info["r_usd"] / info["risk_per_share"]) or \
               info["position_capped"], f"{t} 股数与风险预算不一致且未标注截断"


def test_html_rationale_sections():
    """HTML 决策卡必须含五段业务依据 + 失效条件（自包含，无外部依赖）。"""
    from trading_system.pipeline import run_pipeline
    from trading_system.report_html import render_html
    res = run_pipeline(provider_name="demo", universe_mode="core",
                       top_n=8, max_picks=3, trade_date="2026-07-30")
    html_text = render_html(res)
    # 内嵌 AI 美术为 base64 数据载荷（可能随机撞上短 token），剥离后再做纪律断言
    import re as _re
    html_text = _re.sub(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+", "IMG", html_text)
    for kw in ("市场环境体检", "板块选择理由", "产业链景气", "个股质地",
               "交易计划与风控", "失效与离场条件", "系统审计底稿",
               "单笔最大亏损", "今日优先级", "主线地位"):
        assert kw in html_text, f"决策依据缺少: {kw}"
    # 业务语言纪律：不出现内部公式与核心参数
    for leak in ("MRS_raw", "TOS", "仓位系数", "bonus_hint"):
        assert leak not in html_text, f"内部信息外泄: {leak}"
    assert "http://" not in html_text.replace("http://www.w3.org", "") \
           and "<script src" not in html_text, "报告必须自包含"
