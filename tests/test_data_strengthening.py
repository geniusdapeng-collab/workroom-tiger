"""v3 数据层强化测试：东财/新浪新闻源 + FRED 宏观包（网络全部 mock）。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from trading_system.search.sources import EastmoneyNewsSource, SinaFlashSource
from trading_system.providers.official import OfficialMacroProvider


EM_JSONP = 'cb(' + json.dumps({
    "code": 0,
    "result": {"cmsArticleWebOld": [
        {"date": "2026-08-28 23:06:16", "code": "X1",
         "title": "<em>美股</em>科技股走强", "content": "亚马逊领涨<em>美股</em>科技板块",
         "url": "https://finance.eastmoney.com/a/1.html"},
        {"date": "2026-08-28 20:00:00", "code": "X2",
         "title": "美联储官员讲话", "content": "利率路径仍不确定", "url": ""},
    ]},
}, ensure_ascii=False) + ')'

SINA_FEED = {
    "result": {"status": {"code": 0}, "data": {"feed": {"list": [
        {"id": 1, "rich_text": "【美股收盘：纳指涨1.2%】科技股普涨", "create_time": "2026-08-29 06:00:00", "docurl": "https://finance.sina.com.cn/1"},
        {"id": 2, "rich_text": "【A股早盘】沪指高开0.3%", "create_time": "2026-08-30 09:30:00", "docurl": ""},
    ]}}},
}


def test_eastmoney_news_parses(monkeypatch):
    class R:
        def read(self):
            return EM_JSONP.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R())
    docs = EastmoneyNewsSource().search("美股", limit=8)
    assert len(docs) == 2
    assert docs[0].source == "eastmoney_news"
    assert "<em>" not in docs[0].title          # 高亮标签已清除
    assert docs[0].published == "2026-08-28 23:06:16"
    assert docs[1].url.startswith("https://so.eastmoney.com/")  # 缺 URL 兜底检索页


def test_eastmoney_news_bad_payload_raises(monkeypatch):
    class R:
        def read(self):
            return b"<html>blocked</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="非 JSONP"):
        EastmoneyNewsSource().search("美股")


def test_sina_flash_filters_by_query(monkeypatch):
    monkeypatch.setattr("trading_system.search.sources._http_json",
                        lambda *a, **k: SINA_FEED)
    docs = SinaFlashSource().search("美股", limit=8)
    assert len(docs) == 1 and "纳指" in docs[0].title
    # 无命中主题词 → 如实空集（不凑数）
    assert SinaFlashSource().search("semiconductor", limit=8) == []


def test_new_sources_registered_with_tiers():
    from trading_system.search.sources import default_sources
    from trading_system import config
    names = [s.name for s in default_sources(demo=False)]
    assert "eastmoney_news" in names and "sina_flash" in names
    assert config.SOURCE_TIERS["eastmoney_news"] == "T2"
    assert config.SOURCE_TIERS["sina_flash"] == "T2"


def test_fred_macro_pack_config():
    from trading_system import config
    assert "T10Y2Y" in config.FRED_MACRO_SERIES
    assert "DGS2" in config.FRED_MACRO_SERIES


def test_fred_series_generic(monkeypatch):
    csv_text = "DATE,T10Y2Y\n" + "\n".join(
        f"2026-08-{d:02d},0.4{d % 10}" for d in range(1, 29)) + "\n2026-08-28,.\n"

    class R:
        status_code = 200
        text = csv_text

        def raise_for_status(self):
            return None

    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    s = OfficialMacroProvider().fred_series("T10Y2Y", days=400)
    assert isinstance(s, pd.Series) and len(s) == 28
    assert float(s.iloc[-1]) == pytest.approx(0.48)  # d=28 → 0.4(28%10=8)
    assert "fred_series" in OfficialMacroProvider.CAPABILITIES
