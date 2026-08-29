"""Demo 模式端到端测试：证明“总是同样几只股票”已修复。

断言：
  1. 流水线在 demo 模式完整跑通
  2. 扫描候选不是固定清单，且数量 > 10（v3 只有 10 只硬编码）
  3. 候选评分有区分度（不是同一分数）
  4. MRS/SHS/ICS/TSS 结构化输出齐全
"""

from trading_system.pipeline import run_pipeline


def test_demo_pipeline_end_to_end():
    r = run_pipeline(provider_name="demo", universe_mode="extended", top_n=30)
    assert r.mrs is not None
    assert 0 <= r.mrs.mrs_star <= 10
    assert len(r.sectors) > 0
    assert len(r.chains) > 0
    # 扫描产出候选且远超 v3 的固定 10 只池
    assert len(r.watchlist) >= 15
    tickers = [c.ticker for c in r.watchlist]
    assert len(set(tickers)) == len(tickers)
    # 评分有区分度
    tss_vals = [c.tss_final for c in r.watchlist]
    assert max(tss_vals) - min(tss_vals) > 0.5
    # 扫描初排 Top10 按 rank 降序（TSS 层会按 TSS 重排 watchlist）
    top10_scores = [s for _, s in r.raw["scan_stats"]["top10"]]
    assert top10_scores == sorted(top10_scores, reverse=True)
    # watchlist 按 TSS_final 降序
    tss_order = [c.tss_final for c in r.watchlist]
    assert tss_order == sorted(tss_order, reverse=True)


def test_demo_scan_not_hardcoded():
    """扫描器候选来自全池排序，而非固定名单。"""
    r1 = run_pipeline(provider_name="demo", universe_mode="extended", top_n=30)
    r2 = run_pipeline(provider_name="demo", universe_mode="core", top_n=30)
    t1 = {c.ticker for c in r1.watchlist}
    t2 = {c.ticker for c in r2.watchlist}
    assert t1 != t2  # 不同池 → 不同候选
