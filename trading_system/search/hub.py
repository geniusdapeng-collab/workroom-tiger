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

import copy
import hashlib
import json
import logging
import os
import time
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Iterable

from .. import config
from ..data_safety import ensure_finite_tree, finite_timestamp, safe_diagnostic, strict_json_loads
from .models import RawDocument, SearchBatch, SearchUnavailable
from .sources import SearchSource, default_sources

log = logging.getLogger("search.hub")

CACHE_DIR = os.path.join("cache", "search")
TTL_SECONDS = 6 * 3600          # 内存/磁盘缓存 6 小时（未分类查询回退，见 config.TTL_DEFAULT）
WALL_CLOCK_BUDGET = 45.0        # 单批搜索总墙钟（秒）
BREAKER_FAILS = 3               # 连续失败熔断阈值
BREAKER_COOLDOWN = 900.0        # 熔断冷却（秒）
CACHE_VERSION = 2


@dataclass
class _CacheEntry:
    created_at: float
    docs: list[RawDocument]
    source_stats: dict[str, dict]


class _Breaker:
    def __init__(self):
        self.fails: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}
        self._lock = threading.RLock()

    def allow(self, name: str) -> bool:
        with self._lock:
            if name not in self.opened_at:
                return True
            if time.time() - self.opened_at[name] > BREAKER_COOLDOWN:
                self.opened_at.pop(name, None)
                self.fails.pop(name, None)
                return True
            return False

    def report(self, name: str, ok: bool) -> None:
        with self._lock:
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
        self.demo = bool(demo)
        self.ttl = ttl
        self.budget = budget
        self.max_workers = max_workers
        self.use_disk_cache = use_disk_cache
        # S3 分层 TTL：缓存按类别取 TTL（config.TTL_TIERS，可注入覆盖便于测试）
        self.ttl_tiers = dict(config.TTL_TIERS if ttl_tiers is None else ttl_tiers)
        self._mem: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.RLock()
        self._source_scopes: dict[int, str] = {}
        self._breaker = _Breaker()
        # 主题集注册表（CHAIN_TOPICS/SECTOR_TOPICS 同款模式）：
        # name -> {"topics": {id: query}, "enabled": bool,
        #          "routing": {id: [源名...] | None}}
        self._topic_sets: dict[str, dict] = {}
        if use_disk_cache:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
            except OSError as error:
                self.use_disk_cache = False
                log.info("搜索磁盘缓存不可用，使用内存缓存: %s", safe_diagnostic(error).summary)

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
        value = finite_timestamp(self.ttl_tiers.get(category, self.ttl))
        return max(0.0, value) if value is not None else 0.0

    def _source_identity(self, source: SearchSource) -> dict:
        identity = {"name": source.name,
                    "type": f"{type(source).__module__}.{type(source).__qualname__}"}
        configured_identity = getattr(source, "cache_identity", None)
        if callable(configured_identity):
            try:
                value = configured_identity()
                ensure_finite_tree(value)
                identity["configuration"] = value
                return identity
            except Exception as error:
                log.info("源 %s 缓存配置不可用，隔离当前实例: %s",
                         source.name, safe_diagnostic(error).summary)
        # Unknown/custom adapters must opt in to a stable disk-cache identity.
        # Their private config/credentials are never inspected or serialized.
        with self._cache_lock:
            identity["instance_scope"] = self._source_scopes.setdefault(id(source), uuid.uuid4().hex)
        return identity

    def _cache_key(self, query: str, limit: int, category: str = "news",
                   sources: list[str] | None = None) -> str:
        selected = [self._source_identity(source) for source in self.sources
                    if sources is None or source.name in sources]
        namespace = {
            "version": CACHE_VERSION, "query": query, "limit": limit,
            "category": category, "demo": self.demo,
            "sources": sorted(selected, key=lambda item: json.dumps(item, sort_keys=True)),
            "routing": sorted(set(sources)) if sources is not None else None,
            "ttl": self._ttl_for(category), "disk": self.use_disk_cache,
            "budget": self.budget, "workers": self.max_workers,
        }
        encoded = json.dumps(namespace, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()[:32]

    @staticmethod
    def _validated_docs(docs) -> list[RawDocument]:
        if not isinstance(docs, list) or any(not isinstance(doc, RawDocument) for doc in docs):
            raise SearchUnavailable("invalid-response") from None
        for doc in docs:
            ensure_finite_tree(asdict(doc))
        return docs

    @staticmethod
    def _validated_stats(stats, docs: list[RawDocument]) -> dict[str, dict]:
        if not isinstance(stats, dict) or not stats:
            raise ValueError("Missing search cache source statistics")
        ensure_finite_tree(stats)
        observed: dict[str, int] = {}
        for doc in docs:
            if not isinstance(doc.source, str):
                raise ValueError("Invalid cached document source")
            observed[doc.source] = observed.get(doc.source, 0) + 1
        for name, stat in stats.items():
            if (not isinstance(name, str) or not isinstance(stat, dict) or
                    stat.get("ok") is not True or set(stat) - {"ok", "n", "ms"} or
                    type(stat.get("n")) is not int or stat["n"] < 0 or
                    stat["n"] != observed.get(name, 0)):
                raise ValueError("Invalid search cache source statistics")
            if "ms" in stat and (type(stat["ms"]) not in (int, float) or
                                  finite_timestamp(stat["ms"]) is None or stat["ms"] < 0):
                raise ValueError("Invalid search cache source duration")
        if set(observed) - set(stats):
            raise ValueError("Missing cached document source statistics")
        return stats

    def _cache_entry_get(self, key: str, category: str = "news") -> _CacheEntry | None:
        ttl, now = self._ttl_for(category), time.time()
        if ttl <= 0:
            return None
        with self._cache_lock:
            hit = self._mem.get(key)
            if hit and 0 <= now - hit.created_at < ttl:
                return copy.deepcopy(hit)
            self._mem.pop(key, None)
        if not self.use_disk_cache:
            return None
        try:
            path = os.path.join(CACHE_DIR, f"{key}.json")
            file_time = os.stat(path).st_mtime
            with open(path, encoding="utf-8") as stream:
                raw = strict_json_loads(stream.read())
            if (not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or
                    raw.get("key") != key or not isinstance(raw.get("docs"), list) or
                    not isinstance(raw.get("source_stats"), dict)):
                raise ValueError("Invalid search cache envelope")
            original_time = finite_timestamp(raw.get("created_at"))
            if original_time is None:
                raise ValueError("Invalid search cache creation timestamp")
            # Disk reads retain the original clock. A conservatively older file
            # mtime also shortens the lifetime; reads never refresh the TTL.
            created_at = min(original_time, file_time)
            if not 0 <= now - created_at < ttl:
                return None
            docs = self._validated_docs([RawDocument(**record) for record in raw["docs"]])
            stats = self._validated_stats(raw["source_stats"], docs)
            entry = _CacheEntry(created_at, docs, stats)
            with self._cache_lock:
                self._mem[key] = copy.deepcopy(entry)
            return entry
        except FileNotFoundError:
            return None  # Ordinary miss or another reader's concurrent purge.
        except Exception as error:
            log.info("搜索磁盘缓存读取失败，重新查询: %s", safe_diagnostic(error).summary)
            return None

    def _cache_get(self, key: str, category: str = "news") -> list[RawDocument] | None:
        entry = self._cache_entry_get(key, category)
        return entry.docs if entry is not None else None

    def _cache_put(self, key: str, docs: list[RawDocument],
                   source_stats: dict[str, dict] | None = None) -> None:
        try:
            docs = self._validated_docs(docs)
            if source_stats is None:
                source_stats = {}
                for doc in docs:
                    stat = source_stats.setdefault(doc.source, {"ok": True, "n": 0})
                    stat["n"] += 1
            stats = self._validated_stats(source_stats, docs)
        except Exception as error:
            log.info("搜索缓存内容无效，跳过缓存: %s", safe_diagnostic(error).summary)
            return
        entry = _CacheEntry(time.time(), copy.deepcopy(docs), copy.deepcopy(stats))
        with self._cache_lock:
            self._mem[key] = entry
        if self.use_disk_cache:
            temp_path = None
            try:
                envelope = {"version": CACHE_VERSION, "key": key,
                            "created_at": entry.created_at,
                            "docs": [asdict(doc) for doc in entry.docs],
                            "source_stats": entry.source_stats}
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR,
                                                  prefix=f"{key}-", suffix=".tmp", delete=False) as stream:
                    temp_path = stream.name
                    json.dump(envelope, stream, ensure_ascii=False, allow_nan=False)
                os.replace(temp_path, os.path.join(CACHE_DIR, f"{key}.json"))
                temp_path = None
            except Exception as error:
                log.info("搜索磁盘缓存写入失败: %s", safe_diagnostic(error).summary)
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except FileNotFoundError:
                        temp_path = None
                    except OSError as error:
                        log.info("搜索临时缓存清理失败: %s", safe_diagnostic(error).summary)

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
        key = self._cache_key(query, limit, category, sources)
        cached = self._cache_entry_get(key, category)
        if cached is not None:
            stats = cached.source_stats
            stats["cache"] = {"ok": True, "n": len(cached.docs), "ms": 0}
            return SearchBatch(query=query, docs=cached.docs, source_stats=stats)
        selected = [s for s in self.sources if sources is None or s.name in sources]
        active, skipped = [], []
        for source in selected:
            (active if self._breaker.allow(source.name) else skipped).append(source)
        if skipped:
            log.info("[搜索熔断] 冷却中跳过: %s", [s.name for s in skipped])
        docs: list[RawDocument] = []
        stats: dict[str, dict] = {s.name: {"ok": False, "n": 0, "err": "circuit-open"}
                                  for s in skipped}
        deadline = time.monotonic() + self.budget
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            futs = {pool.submit(s.search, query, limit): s for s in active}
            try:
                for fut in as_completed(futs, timeout=max(0.0, deadline - time.monotonic())):
                    src = futs[fut]
                    try:
                        got = self._validated_docs(fut.result())
                        self._breaker.report(src.name, True)
                        stats[src.name] = {"ok": True, "n": len(got)}
                        docs.extend(got)
                    except Exception as e:
                        self._breaker.report(src.name, False)
                        diagnostic = safe_diagnostic(e)
                        partial = []
                        detail = diagnostic.summary
                        if isinstance(e, SearchUnavailable):
                            try:
                                partial = self._validated_docs(e.documents)
                                detail = e.safe_summary()
                            except Exception as invalid:
                                detail = "invalid partial source response: " + safe_diagnostic(invalid).summary
                        stats[src.name] = {"ok": False, "n": len(partial), "err": detail}
                        if diagnostic.status is not None:
                            stats[src.name]["http_status"] = diagnostic.status
                        docs.extend(partial)
                        log.info("源 %s 失败: %s", src.name, detail)
            except TimeoutError:
                # 总墙钟截止：未完成的慢源记超时失败并取消，绝不阻塞整体
                for fut, src in futs.items():
                    if src.name not in stats:
                        fut.cancel()
                        self._breaker.report(src.name, False)
                        stats[src.name] = {"ok": False, "n": 0,
                                           "err": "wall-clock timeout"}
                        log.warning("源 %s 超时被墙钟切断", src.name)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # A cached partial/failed batch must never turn into a healthy receipt.
        # Failed batches are retried (subject to the same circuit breaker).
        if stats and all(stat["ok"] is True for stat in stats.values()) and self._ttl_for(category) > 0:
            self._cache_put(key, docs, stats)
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
