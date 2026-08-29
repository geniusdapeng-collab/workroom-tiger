"""搜索数据模型。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RawDocument:
    """一个搜索源返回的原始文档（未清洗）。"""
    doc_id: str          # 内容哈希（去重键）
    source: str          # 来源标识：kimi_search / edgar / google_news / reddit / patentsview / federal_register / demo
    title: str
    url: str
    content: str
    published: str       # ISO 日期或 ""
    fetched_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)   # 源特定字段（如 EDGAR form 类型）

    @staticmethod
    def make_id(source: str, url: str, title: str, content: str) -> str:
        h = hashlib.sha1(f"{source}|{url}|{title}|{content[:256]}".encode("utf-8", "ignore"))
        return h.hexdigest()[:16]


@dataclass
class CleanDocument:
    """清洗后的文档：规则字段 + LLM 语义标注（degraded=True 表示语义标注缺失）。"""
    raw: RawDocument
    tickers: list[str] = field(default_factory=list)
    sentiment: str | None = None            # bullish/bearish/neutral/mixed
    sentiment_score: float | None = None    # -1..1
    events: list[str] = field(default_factory=list)
    summary_zh: str | None = None
    relevance: float | None = None
    degraded: bool = False                  # True = LLM 语义标注缺失（透传）


@dataclass
class SearchBatch:
    """一次搜索任务的产出。"""
    query: str
    docs: list[RawDocument]
    source_stats: dict[str, dict] = field(default_factory=dict)  # source -> {ok, n, ms, err}
