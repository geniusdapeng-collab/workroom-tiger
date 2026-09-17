"""S3 数据层（Tiger Data Fabric）测试：可信度分级 / Evidence / 交叉验证 /
分层 TTL / 零基线 purge / AH 主题开关。全部离线，不依赖真实网络与 LLM。"""

from __future__ import annotations

import hashlib
import inspect
import time

import pytest

from trading_system import config
from trading_system.redline import Passthrough
from trading_system.search.credibility import (
    CrossValidator, SourceTier, content_hash, evidence_for, parse_published,
    scoring_docs, tier_for_source,
)
from trading_system.search.hub import SearchHub
from trading_system.search.models import CleanDocument, RawDocument
from trading_system.search.sources import DemoSearchSource


def _raw(doc_id: str, source: str, url: str = "", title: str = "t",
         content: str | None = None, published: str = "2026-07-30") -> RawDocument:
    return RawDocument(doc_id=doc_id, source=source, title=title,
                       url=url or f"u://{doc_id}",
                       content=content or f"content body for {doc_id}, long enough to pass",
                       published=published)


def _cd(doc_id: str, source: str, tickers: list[str], events: list[str],
        url: str = "", degraded: bool = False,
        published: str = "2026-07-30") -> CleanDocument:
    raw = _raw(doc_id, source, url=url, published=published)
    cd = CleanDocument(raw=raw, tickers=tickers, events=events, degraded=degraded)
    cd.evidence = evidence_for(raw)
    return cd


# ---------------------------------------------------------------- ① tier 标注
class TestSourceTier:
    def test_six_source_defaults(self):
        """现有 6 源默认档位：监管原文 T0 / 主流媒体 T1 / 社媒 T3。"""
        assert tier_for_source("edgar") == SourceTier.T0
        assert tier_for_source("federal_register") == SourceTier.T0
        assert tier_for_source("patentsview") == SourceTier.T0
        assert tier_for_source("kimi_search") == SourceTier.T1
        assert tier_for_source("google_news") == SourceTier.T1
        assert tier_for_source("reddit") == SourceTier.T3

    def test_domain_mapping_overrides_source_default(self):
        """域名映射表优先于源默认：同一源落地到社媒域即降级，落白名单域保持 T1。"""
        assert tier_for_source("kimi_search",
                               "https://www.reddit.com/r/stocks/x") == SourceTier.T3
        assert tier_for_source("kimi_search",
                               "https://www.reuters.com/markets/x") == SourceTier.T1
        assert tier_for_source("google_news",
                               "https://finance.yahoo.com/news/x") == SourceTier.T2
        assert tier_for_source("kimi_fetch",
                               "https://www.sec.gov/Archives/x") == SourceTier.T0
        # 未知域名回退源默认；未知源回退保守 T2
        assert tier_for_source("google_news", "https://unknown-blog.example/x") == SourceTier.T1
        assert tier_for_source("some_future_source") == SourceTier.T2

    def test_config_overridable(self, monkeypatch):
        """SOURCE_TIERS / DOMAIN_TIERS 进 config，运行时可覆盖。"""
        monkeypatch.setitem(config.SOURCE_TIERS, "reddit", "T2")
        assert tier_for_source("reddit") == SourceTier.T2
        monkeypatch.setitem(config.DOMAIN_TIERS, "reddit.com", "T1")
        assert tier_for_source("kimi_search", "https://reddit.com/x") == SourceTier.T1


# ---------------------------------------------------------------- ② Evidence
class TestEvidence:
    def test_fields_complete(self):
        raw = _raw("d1", "edgar", published="2026-07-30")
        ev = evidence_for(raw)
        assert ev.content == raw.content
        assert ev.source == "edgar"
        assert ev.tier == "T0"
        assert ev.published_at is not None           # ISO 日期已解析（Point-in-Time）
        assert ev.content_hash == hashlib.sha256(raw.content.encode()).hexdigest()[:16]
        assert ev.fetched_at == raw.fetched_at

    def test_content_hash_stable(self):
        """同内容同哈希；不同内容不同哈希；恒为 16 位。"""
        h1 = content_hash("same body")
        h2 = content_hash("same body")
        h3 = content_hash("other body")
        assert h1 == h2 and len(h1) == 16 and h1 != h3

    def test_published_at_formats(self):
        """ISO / RFC2822 / epoch 三种源格式均可解析；无法解析记 None（不编造）。"""
        assert parse_published("2026-07-30") is not None
        assert parse_published("Wed, 29 Jul 2026 07:28:00 GMT") is not None
        assert parse_published("1753812480.0") is not None
        assert parse_published("") is None
        assert parse_published("not-a-date") is None

    def test_cleaning_output_carries_evidence(self):
        """清洗管线输出（含红线透传路径）每条文档必须携带 Evidence。"""
        from trading_system.cleaning.pipeline import unwrap_cleaned
        docs = [_raw("a", "reddit", published=""), _raw("b", "google_news")]
        cleaned = unwrap_cleaned(Passthrough(payload=docs, origin="clean.llm_semantic",
                                             reason="test"))
        assert all(c.evidence is not None for c in cleaned)
        assert all(c.evidence.content_hash for c in cleaned)
        assert cleaned[0].evidence.published_at is None    # 缺发布时间记 None


# ---------------------------------------------------------------- ③ 交叉验证
class TestCrossValidator:
    def test_single_source_event_downgraded(self):
        """单源关键事件：未达 ≥2 源 → corroborated=False（不删除，仅降级）。"""
        docs = [_cd("a", "kimi_search", ["AAA"], ["earnings"])]
        stats = CrossValidator().validate(docs)
        assert docs[0].corroborated is False
        assert stats["key_event_docs"] == 1
        assert stats["corroborated"] == 0
        assert stats["downgraded"] == 1

    def test_two_sources_with_t2_or_better_pass(self):
        """双源（含 ≤T2）同实体同事件 → 通过。"""
        docs = [_cd("a", "kimi_search", ["AAA"], ["earnings beat"]),
                _cd("b", "edgar", ["AAA"], ["earnings"]), ]
        stats = CrossValidator().validate(docs)
        assert all(d.corroborated for d in docs)
        assert stats["corroborated"] == 2 and stats["downgraded"] == 0

    def test_two_t3_sources_fail(self):
        """双社媒源（无 ≤T2）→ 不通过：社媒单独互证不算数。"""
        docs = [_cd("a", "reddit", ["AAA"], ["supply_chain disruption"]),
                _cd("b", "kimi_search", ["AAA"], ["supply_chain"],
                    url="https://www.reddit.com/r/wallstreetbets/x")]
        stats = CrossValidator().validate(docs)
        assert all(not d.corroborated for d in docs)
        assert stats["downgraded"] == 2

    def test_non_key_event_untouched(self):
        """非关键事件文档不需交叉验证，保持 corroborated=True。"""
        docs = [_cd("a", "reddit", ["AAA"], ["random rumor"])]
        stats = CrossValidator().validate(docs)
        assert docs[0].corroborated is True
        assert stats["key_event_docs"] == 0

    def test_llm_annotation_missing_all_downgraded(self):
        """LLM 语义标注缺失（degraded）：全部记未验证并透传披露，不阻塞管线。"""
        docs = [_cd("a", "edgar", ["AAA"], ["earnings"], degraded=True),
                _cd("b", "kimi_search", ["AAA"], ["earnings"], degraded=True),
                _cd("c", "reddit", [], [], degraded=True)]
        stats = CrossValidator().validate(docs)
        assert all(not d.corroborated for d in docs)
        assert stats["llm_missing"] == 3
        assert stats["downgraded"] == 3
        assert scoring_docs(docs) == []                 # 打分输入集合为空

    def test_scoring_docs_filters_downgraded_only(self):
        """叙事/舆情打分输入只使用 corroborated=True 集合；降级文档保留在全集。"""
        docs = [_cd("a", "kimi_search", ["AAA"], ["earnings"]),
                _cd("b", "edgar", ["AAA"], ["earnings"]),
                _cd("c", "reddit", ["BBB"], ["policy export control"])]
        CrossValidator().validate(docs)
        kept = scoring_docs(docs)
        assert {d.raw.doc_id for d in kept} == {"a", "b"}
        assert len(docs) == 3                            # 透传纪律：不删除

    def test_missing_published_at_counted(self):
        docs = [_cd("a", "kimi_search", ["AAA"], ["earnings"], published="")]
        stats = CrossValidator().validate(docs)
        assert stats["missing_published_at"] == 1


# ---------------------------------------------------------------- ④ 分层 TTL
class _CountingSource:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def search(self, query: str, limit: int = 8):
        self.calls += 1
        return [_raw(f"{self.name}-{self.calls}", self.name)]


class TestTieredTTL:
    def test_category_ttl_expiry(self):
        """news 类短 TTL 过期重取；announcement 类长 TTL 同期内仍命中。"""
        src = _CountingSource()
        hub = SearchHub(sources=[src], use_disk_cache=False,
                        ttl_tiers={"news": 0.2, "announcement": 3600})
        hub.search("q1", category="news")
        hub.search("q1", category="news")
        assert src.calls == 1                            # TTL 内零网络
        hub.search("q2", category="announcement")
        assert src.calls == 2
        time.sleep(0.25)                                 # 超过 news TTL，远低于 announcement TTL
        hub.search("q1", category="news")
        assert src.calls == 3                            # news 已过期 → 重取
        hub.search("q2", category="announcement")
        assert src.calls == 3                            # announcement 仍命中

    def test_cache_key_has_category_dimension(self):
        """同一查询在不同类别下是不同缓存项。"""
        src = _CountingSource()
        hub = SearchHub(sources=[src], use_disk_cache=False,
                        ttl_tiers={"news": 3600, "announcement": 3600})
        hub.search("same query", category="news")
        hub.search("same query", category="announcement")
        assert src.calls == 2

    def test_default_ttl_fallback(self):
        """未分类类别回退默认 TTL（构造参数 ttl）。"""
        src = _CountingSource()
        hub = SearchHub(sources=[src], use_disk_cache=False, ttl=3600)
        hub.search("q", category="uncategorized")
        hub.search("q", category="uncategorized")
        assert src.calls == 1


# ---------------------------------------------------------------- ⑤ 零基线 purge
class TestZeroBaselinePurge:
    def test_purge_covers_new_caches_two_runs(self, tmp_path):
        """模拟两轮运行：新缓存（cache/credibility + 分层搜索缓存）每轮必清，
        第二轮无残留。"""
        from trading_system.state import purge_run_state
        for run in (1, 2):
            sdir = tmp_path / "cache" / "search"
            cdir = tmp_path / "cache" / "credibility"
            sdir.mkdir(parents=True, exist_ok=True)
            cdir.mkdir(parents=True, exist_ok=True)
            (sdir / "abc123.json").write_text("[]")
            (cdir / "cross_validation.json").write_text("{}")
            report = purge_run_state(base_dir=str(tmp_path))
            assert not (tmp_path / "cache" / "search").exists(), f"run{run} 搜索缓存残留"
            assert not (tmp_path / "cache" / "credibility").exists(), f"run{run} 可信度缓存残留"
            assert report["cache/credibility"].startswith("removed")
        # 第三轮：无文件可清，如实报 absent
        report = purge_run_state(base_dir=str(tmp_path))
        assert report["cache/credibility"] == "absent"

    def test_whitelist_untouched_by_new_targets(self):
        """新缓存不得进入白名单（会计台账仅五个：journal/小G/调优/校准/治理事件账）。"""
        from trading_system.state import PURGE_TARGETS, WHITELIST
        assert "cache/credibility" in PURGE_TARGETS
        assert set(WHITELIST) == {"journal.json", "sim_portfolio.json",
                                  "tuned_params.json", "calibration_samples.json",
                                  # S5：治理五元事件账（哈希链留痕旁路，跨轮累计，不进决策输入）
                                  "governance_events.jsonl"}


# ---------------------------------------------------------------- ⑥ AH 主题开关
class TestAHTopics:
    def test_default_disabled(self):
        assert config.AH_TOPICS_ENABLED is False
        assert config.AH_TOPICS                          # 主题词与源路由已配置

    def test_disabled_no_network_calls(self):
        """开关关闭：AH 主题整体缺席 active_topics，gather 不产生任何调用。"""
        src = _CountingSource()
        hub = SearchHub(sources=[src], use_disk_cache=False)
        hub.register_topic_set("ah", config.AH_TOPICS,
                               enabled=config.AH_TOPICS_ENABLED)
        assert hub.active_topics() == {}
        docs = hub.gather_topic_sets(limit_per_query=2, deep=False)
        assert docs == [] and src.calls == 0

    def test_enabled_routes_to_configured_sources(self):
        """开关打开（模拟 S4）：AH 主题按配置源路由发起查询。"""
        src = _CountingSource()
        hub = SearchHub(sources=[src], use_disk_cache=False)
        hub.register_topic_set("ah", config.AH_TOPICS, enabled=True)
        topics = hub.active_topics()
        assert len(topics) == len(config.AH_TOPICS)
        # 源路由生效：AH 主题查询只走配置的源（本测试源不在路由内 → 零调用）
        docs = hub.gather_topic_sets(limit_per_query=2, deep=False)
        assert docs == [] and src.calls == 0
        # 路由内包含的源会被调用
        hub2 = SearchHub(sources=[DemoSearchSource()], use_disk_cache=False)
        hub2.register_topic_set("ah", {"hk": {"query": "港股通 南向资金",
                                              "sources": ["demo"]}}, enabled=True)
        docs2 = hub2.gather_topic_sets(limit_per_query=2, deep=False)
        assert docs2

    def test_topic_set_registry_matches_chain_sector_pattern(self):
        """注册机制与 CHAIN_TOPICS/SECTOR_TOPICS 同款：{id: query} 无路由。"""
        hub = SearchHub(sources=[DemoSearchSource()], use_disk_cache=False)
        hub.register_topic_set("chains", {"memory": "DRAM NAND prices"})
        hub.register_topic_set("sectors", {"macro": "FOMC CPI Treasury"})
        topics = hub.active_topics()
        assert topics["memory"] == ("DRAM NAND prices", None)
        assert topics["macro"] == ("FOMC CPI Treasury", None)


# ---------------------------------------------------------------- 静态审查
def test_credibility_has_no_llm_path():
    """红线 D9：可信度/交叉验证为纯规则实现——模块内禁止任何 LLM 调用。"""
    import trading_system.search.credibility as cred
    src = inspect.getsource(cred)
    for banned in ["complete_json", "LLMClient", "llm_guard", "chat_completion"]:
        assert banned not in src, f"credibility 疑似引入 LLM 路径: {banned}"
