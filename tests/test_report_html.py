"""HTML 日报生成器测试（v5.1）。"""

from __future__ import annotations

from trading_system.pipeline import run_pipeline
from trading_system.report_html import render_html, _result_from_json
from trading_system.report import to_json


def test_html_contains_all_sections(tmp_path):
    r = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
    html_text = render_html(r, None, [])
    for section in ["市场环境体检", "板块热度地图", "产业链景气周期",
                    "科技赛道景气扫描", "今日选股漏斗", "个股精选清单",
                    "交易计划与风控纪律", "系统执行自检", "胜率追踪",
                    "数据缺失披露"]:
        assert section in html_text, f"缺章节 {section}"
    # v5.3 保密纪律：投资者版报告不得外显内部推导公式与核心参数
    for leak in ["MRS_raw", "× k=", "TOS=", "TOS 排序", "SHS≥7.5", "≥7.2",
                 "bonus_hint", "×1.", "仓位系数",
                 "S_structure", "S_momentum", "TSS=", "0.4/0.4/0.2", "→10分"]:
        assert leak not in html_text, f"内部信息外泄: {leak}"
    # 投资人视角（v5.6）：英文 jargon 不得出现在面向投资者的报告里
    for jargon in ["memory", "logic", "foundry", "ai_model", "ai_app",
                   "upstream", "midstream", "downstream", "semis", "ai_compute",
                   "透传:", "provider=", "layer1.", "tech.sentiment"]:
        assert jargon not in html_text, f"英文/内部 jargon 外显: {jargon}"
    # 中文赛道名与板块名在
    for zh in ["存储产业链", "逻辑芯片链", "晶圆代工链", "AI 模型/云链",
               "半导体（SMH）" if "SMH" in html_text else "半导体"]:
        assert zh in html_text, f"缺中文名 {zh}"
    # 三一级页签 + 决策日报二级栏目 + 美股定位 + 个股深度报告 + 历史报告（v5.8）
    for tab in ["tab-daily", "tab-sim", "tab-philosophy",
                "btn-daily", "btn-sim", "btn-philosophy",
                "daily-pane-today", "daily-pane-stock", "daily-pane-history",
                "daily-btn-today", "daily-btn-stock", "daily-btn-history",
                "showDaily", "subbtn"]:
        assert tab in html_text, f"缺页签 {tab}"
    for must in ["汇聚顶级基金经理思想 × AI 能力的美股交易系统",
                 "投资标的：<b", "美股", "决策日报", "今日决策报告",
                 "个股深度报告", "策略验证", "小G纯AI模拟盘",
                 "核心交易理念", "showStock", "深度数据档案"]:
        assert must in html_text, f"缺要素 {must}"
    assert "<svg" in html_text                      # 雷达/传导图在
    assert "polygon" in html_text                   # 雷达图在
    assert "http" not in html_text.split("<style>")[0].split("<head>")[1] \
        if "<head>" in html_text else True          # 头部无外部资源


def test_html_self_contained_no_external_assets(tmp_path):
    r = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
    html_text = render_html(r, None, [])
    # 内嵌 AI 美术为 base64 数据载荷（不透明二进制），剥离后再做外链纪律断言
    import re as _re
    visible = _re.sub(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+", "IMG", html_text)
    assert "cdn" not in visible.lower()
    assert "src=\"http" not in visible
    assert "src='http" not in visible
    assert "link rel" not in visible


def test_json_roundtrip(tmp_path):
    r = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
    path = to_json(r, str(tmp_path))
    r2 = _result_from_json(path)
    html_text = render_html(r2, None, [])
    assert r2.mrs.mrs_star == r.mrs.mrs_star
    assert "polygon" in html_text                   # 反序列化后雷达仍在
