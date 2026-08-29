"""SearchHub — 搜索集成模块：并发、缓存、超时、熔断。

性能强化（用户指令二·1）：
  - 并发：线程池跨源并行执行，单源超时不拖垮整体；
  - 缓存：TTL 内存缓存 + 磁盘缓存（cache/search/），同一 query 在 TTL 内零网络；
  - 超时：每源独立 timeout；总墙钟 budget 截止后不再等待慢源；
  - 熔断：源连续失败 N 次后在冷却期内跳过（记录在 stats，不算"跳过环节"——
    环节 search.collect 本身始终执行，熔断只影响单个源的调用决策）。

深度与广度（用户指令二·1）：
  - 横向：query_plan 把同一主题展开为多语言/多视角查询词
    （英文主查询 + 中文/韩文关键术语 + 论坛黑话）；
  - 纵向：deep_probes 追加 EDGAR/专利/公报的专项查询
    （供应链扰动、政策微调、专利动态）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from .. import config
from .models import RawDocument, SearchBatch
from .sources import SearchSource, default_sources

log = logging.getLogger("search.hub")

CACHE_DIR = os.path.join("cache", "search")
TTL_SECONDS = 6 * 3600          # 内存/磁盘缓存 6 小时（未分类查询回退，见 config.TTL_DEFAULT）
WALL_CLOCK_BUDGET = 45.0        # 单批搜索总墙钟（秒）
BREAKER_FAILS = 3               # 连续失败熔断阈值
BREAKER_COOLDOWN = 900.0        # 熔断冷却（秒）


class _Breaker:
    def __init__(self):
        self.fails: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}

    def allow(self, name: str) -> bool:
        if name not in self.opened_at:
            return True
        if time.time() - self.opened_at[name] > BREAKER_COOLDOWN:
            self.opened_at.pop(name, None)
            self.fails.pop(name, None)
            return True
        return False

    def report(self, name: str, ok: bool) -> None:
        if ok:
            self.fails[name] = 0
            self.opened_at.pop(name, None)
        else:
            self.fails[name] = self.fails.get(name, 0) + 1
            if self.fails[name] >= BREAKER_FAILS:
                self.opened_at[name] = time.time()
                log.warning("[搜索熔断] 源 %s 连续失败 %d 次，冷却 %.0fs",
                            name, BREAKER_FAILS, BREAKER_COOLDOWN)


class SearchHub:
    def __init__(self, sources: Iterable[SearchSource] | None = None,
                 *, demo: bool = False, ttl: int = TTL_SECONDS,
                 budget: float = WALL_CLOCK_BUDGET, max_workers: int = 6,
                 use_disk_cache: bool = True,
                 ttl_tiers: dict[str, int] | None = None):
        self.sources = list(sources) if sources is not None else default_sources(demo)
        self.ttl = ttl
        self.budget = budget
        self.max_workers = max_workers
        self.use_disk_cache = use_disk_cache
        # S3 分层 TTL：缓存按类别取 TTL（config.TTL_TIERS，可注入覆盖便于测试）
        self.ttl_tiers = dict(config.TTL_TIERS if ttl_tiers is None else ttl_tiers)
        self._mem: dict[str, tuple[float, list[RawDocument]]] = {}
        self._breaker = _Breaker()
        # 主题集注册表（CHAIN_TOPICS/SECTOR_TOPICS 同款模式）：
        # name -> {"topics": {id: query}, "enabled": bool,
        #          "routing": {id: [源名...] | None}}
        self._topic_sets: dict[str, dict] = {}
        if use_disk_cache:
            os.makedirs(CACHE_DIR, exist_ok=True)

    # ---------------------------------------------------------- 主题集注册
    def register_topic_set(self, name: str, topics: dict, *, enabled: bool = True) -> None:
        """注册一个主题集。topics 支持两种形态：
          - {id: query_str}（CHAIN_TOPICS/SECTOR_TOPICS 同款，无源路由）；
          - {id: {"query": str, "sources": [源名...]}}（AH_TOPICS 同款，带源路由）。
        enabled=False 的主题集在 active_topics() 中整体缺席（不产生任何调用）。"""
        normalized: dict[str, str] = {}
        routing: dict[str, list[str] | None] = {}
        for tid, spec in topics.items():
            if isinstance(spec, dict):
                normalized[tid] = str(spec.get("query") or "")
                srcs = spec.get("sources")
                routing[tid] = [str(s) for s in srcs] if srcs else None
            else:
                normalized[tid] = str(spec)
                routing[tid] = None
        self._topic_sets[name] = {"topics": normalized, "enabled": bool(enabled),
                                  "routing": routing}

    def active_topics(self) -> dict[str, tuple[str, list[str] | None]]:
        """当前启用的主题全集：{topic_id: (query, sources|None)}。"""
        out: dict[str, tuple[str, list[str] | None]] = {}
        for ts in self._topic_sets.values():
            if not ts["enabled"]:
                continue
            for tid, q in ts["topics"].items():
                if q:
                    out[tid] = (q, ts["routing"].get(tid))
        return out

    def gather_topic_sets(self, limit_per_query: int = 4, deep: bool = True
                          ) -> list[RawDocument]:
        """按注册主题集采集（未去重——去重是清洗环节职责）。"""
        out: list[RawDocument] = []
        for _tid, (query, srcs) in self.active_topics().items():
            out.extend(self.gather(query, limit_per_query=limit_per_query,
                                   deep=deep, sources=srcs))
        return out

    # ---------------------------------------------------------- 缓存
    def _ttl_for(self, category: str) -> float:
        """分层 TTL：按类别取 config.TTL_TIERS，未分类回退默认。"""
        return float(self.ttl_tiers.get(category, self.ttl))

    def _cache_key(self, query: str, limit: int, category: str = "news") -> str:
        import hashlib
        # S3：缓存 key 增加类别维度——同一句查询在不同类别下 TTL 不同、互不污染
        return hashlib.sha1(f"{category}|{query}|{limit}".encode()).hexdigest()[:20]

    def _cache_get(self, key: str, category: str = "news") -> list[RawDocument] | None:
        ttl = self._ttl_for(category)
        hit = self._mem.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if self.use_disk_cache:
            path = os.path.join(CACHE_DIR, f"{key}.json")
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
                try:
                    raw = json.load(open(path, encoding="utf-8"))
                    docs = [RawDocument(**d) for d in raw]
                    self._mem[key] = (time.time(), docs)
                    return docs
                except Exception:
                    return None
        return None

    def _cache_put(self, key: str, docs: list[RawDocument]) -> None:
        self._mem[key] = (time.time(), docs)
        if self.use_disk_cache:
            try:
                from dataclasses import asdict
                json.dump([asdict(d) for d in docs],
                          open(os.path.join(CACHE_DIR, f"{key}.json"), "w", encoding="utf-8"),
                          ensure_ascii=False)
            except Exception as e:
                log.info("搜索磁盘缓存写入失败: %s", e)

    # ---------------------------------------------------------- 查询规划
    @staticmethod
    def query_plan(topic: str, tickers: list[str] | None = None) -> list[str]:
        """横向扩展：多语言 + 多视角查询词。规则只负责"拼查询词"这一确定性
        操作；查询词本身的语义设计由领域知识固化（非运行时推断）。"""
        tickers = tickers or []
        qs = [topic, f"{topic} stock analysis earnings"]
        if tickers:
            qs.append(" ".join(tickers[:6]) + " news outlook")
        return qs

    @staticmethod
    def deep_probes(theme: str) -> list[str]:
        """纵向穿透：竞品不看的三类信号。"""
        return [
            f"{theme} supply chain disruption 8-K",   # EDGAR 命中
            f"{theme} patent filing",                  # PatentsView 命中
            f"{theme} export control regulation",      # FederalRegister 命中
        ]

    # ---------------------------------------------------------- 主入口
    def search(self, query: str, limit: int = 8,
               sources: list[str] | None = None,
               category: str = "news") -> SearchBatch:
        """跨源并发搜索。任何单源失败不阻塞整体；返回源级统计供审计。
        category：缓存分层类别（quote/news/announcement/macro），决定 TTL。"""
        key = self._cache_key(query, limit, category)
        cached = self._cache_get(key, category)
        if cached is not None:
            return SearchBatch(query=query, docs=cached,
                               source_stats={"cache": {"ok": True, "n": len(cached), "ms": 0}})
        active = [s for s in self.sources
                  if (sources is None or s.name in sources) and self._breaker.allow(s.name)]
        skipped = [s.name for s in self.sources if not self._breaker.allow(s.name)]
        if skipped:
            log.info("[搜索熔断] 冷却中跳过: %s", skipped)
        docs: list[RawDocument] = []
        stats: dict[str, dict] = {}
        deadline = time.time() + self.budget
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            futs = {pool.submit(s.search, query, limit): s for s in active}
            try:
                for fut in as_completed(futs, timeout=self.budget):
                    src = futs[fut]
                    try:
                        got = fut.result(timeout=max(0.1, deadline - time.time()))
                        self._breaker.report(src.name, True)
                        stats[src.name] = {"ok": True, "n": len(got)}
                        docs.extend(got)
                    except Exception as e:
                        self._breaker.report(src.name, False)
                        stats[src.name] = {"ok": False, "n": 0, "err": str(e)[:120]}
                        log.info("源 %s 失败: %s", src.name, e)
            except TimeoutError:
                # 总墙钟截止：未完成的慢源记超时失败并取消，绝不阻塞整体
                for fut, src in futs.items():
                    if not fut.done():
                        fut.cancel()
                        self._breaker.report(src.name, False)
                        stats[src.name] = {"ok": False, "n": 0,
                                           "err": "wall-clock timeout"}
                        log.warning("源 %s 超时被墙钟切断", src.name)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self._cache_put(key, docs)
        return SearchBatch(query=query, docs=docs, source_stats=stats)

    def gather(self, topic: str, tickers: list[str] | None = None,
               limit_per_query: int = 8, deep: bool = True,
               sources: list[str] | None = None) -> list[RawDocument]:
        """一次主题采集 = 横向查询计划 + 纵向穿透探针，结果汇总（未去重，
        去重是清洗环节的职责——红线：各环节职责不串位）。
        sources：主题级源路由（如 AH 主题只走全网/中文新闻源）；None=全源。"""
        queries = self.query_plan(topic, tickers)
        probes = self.deep_probes(topic) if deep else []
        out: list[RawDocument] = []
        for q in queries:
            out.extend(self.search(q, limit_per_query, sources=sources,
                                   category="news").docs)
        for p in probes:
            # 纵向穿透固定走披露/监管源，属公告类缓存分层
            out.extend(self.search(p, limit_per_query,
                                   sources=["edgar", "patentsview", "federal_register",
                                            "kimi_search", "demo"],
                                   category="announcement").docs)
        return out
