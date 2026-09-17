"""搜索源实现 — 横向多源 + 纵向穿透。

横向（广度）：
  - KimiSearchSource：agent-gw search（全网实时搜索，多语言查询词）
  - GoogleNewsSource：Google News RSS（多语言主流媒体）
  - RedditSource：Reddit 公开 JSON（小众论坛：r/wallstreetbets 等）
  - FetchSource：agent-gw fetch（指定 URL 正文抓取，财报电话会页面等）

纵向（深度，竞品看不见的"蛛丝马迹"）：
  - EDGARSource：SEC 官方全文检索 API（8-K 供应链扰动、10-Q 风险因子变动）
  - PatentsViewSource：USPTO 专利 API（专利动态）
  - FederalRegisterSource：美国联邦公报 API（政策微调：出口管制/补贴/关税）

所有源统一接口 search(query, limit) -> list[RawDocument]；
单个源失败只影响自身（SearchHub 熔断），不阻塞其他源。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Protocol

from .models import RawDocument

log = logging.getLogger("search.sources")

_UA = {"User-Agent": "ai-stock-trading-system/5.0 (research; contact: owner)"}


class SearchSource(Protocol):
    name: str

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        ...


def _http_json(url: str, timeout: float, headers: dict | None = None):
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _http_text(url: str, timeout: float, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ---------------------------------------------------------------- agent-gw
class KimiSearchSource:
    """agent-gw search：全网实时搜索（主力源，已实测可用）。"""
    name = "kimi_search"

    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout
        self._client = None

    def _gw(self):
        if self._client is None:
            from agent_gw import AgentGwClient
            self._client = AgentGwClient(timeout=self.timeout)
        return self._client

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        resp = self._gw().search(query, timeout=self.timeout)
        results = (resp or {}).get("search_results") or []
        docs = []
        for item in results[:limit]:
            content = str(item.get("content") or "")
            title = str(item.get("title") or "") or content[:60].replace("\n", " ")
            url = str(item.get("url") or "")
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, url, title, content),
                source=self.name, title=title, url=url,
                content=content[:4000], published=str(item.get("published") or ""),
                meta={"authority": item.get("authority", "")},
            ))
        return docs


class FetchSource:
    """agent-gw fetch：按 URL 抓正文（财报电话会记录页、深度文章）。"""
    name = "kimi_fetch"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._client = None

    def fetch_doc(self, url: str) -> RawDocument | None:
        if self._client is None:
            from agent_gw import AgentGwClient
            self._client = AgentGwClient(timeout=self.timeout)
        resp = self._client.fetch(url, as_markdown=True, timeout=self.timeout)
        content = ""
        if isinstance(resp, dict):
            content = str(resp.get("content") or resp.get("markdown") or "")
        if not content:
            return None
        title = content.splitlines()[0][:80] if content else url
        return RawDocument(doc_id=RawDocument.make_id(self.name, url, title, content),
                           source=self.name, title=title, url=url,
                           content=content[:6000], published="")

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        # query 形如 "fetch:<url> <url2> ..."
        urls = [u for u in query.replace("fetch:", "").split() if u.startswith("http")]
        docs = []
        for u in urls[:limit]:
            try:
                d = self.fetch_doc(u)
                if d:
                    docs.append(d)
            except Exception as e:
                log.info("fetch %s 失败: %s", u, e)
        return docs


# ---------------------------------------------------------------- 主流新闻
class GoogleNewsSource:
    """Google News RSS（免 key，多语言由查询词决定）。"""
    name = "google_news"

    def __init__(self, timeout: float = 15.0, lang: str = "en-US", country: str = "US"):
        self.timeout = timeout
        self.lang = lang
        self.country = country

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        q = urllib.parse.quote(query)
        url = (f"https://news.google.com/rss/search?q={q}"
               f"&hl={self.lang}&gl={self.country}&ceid={self.country}:{self.lang.split('-')[0]}")
        xml = _http_text(url, self.timeout)
        root = ET.fromstring(xml)
        docs = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub = item.findtext("pubDate") or ""
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            content = f"{title}\n{desc.strip()}"
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, link, title, content),
                source=self.name, title=title, url=link,
                content=content[:1500], published=pub))
            if len(docs) >= limit:
                break
        return docs


class RedditSource:
    """Reddit 公开 JSON（小众论坛情绪：默认 wallstreetstocks/stocks/wallstreetbets）。"""
    name = "reddit"

    def __init__(self, timeout: float = 15.0,
                 subreddits: tuple[str, ...] = ("stocks", "wallstreetbets", "investing")):
        self.timeout = timeout
        self.subreddits = subreddits

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        q = urllib.parse.quote(query)
        docs = []
        per = max(1, limit // len(self.subreddits))
        for sub in self.subreddits:
            url = (f"https://www.reddit.com/r/{sub}/search.json"
                   f"?q={q}&restrict_sr=1&sort=new&limit={per}")
            try:
                data = _http_json(url, self.timeout)
            except Exception as e:
                log.info("reddit r/%s 失败: %s", sub, e)
                continue
            for child in (data.get("data", {}).get("children") or []):
                d = child.get("data", {})
                title = str(d.get("title") or "")
                body = str(d.get("selftext") or "")[:800]
                link = "https://www.reddit.com" + str(d.get("permalink") or "")
                content = f"{title}\n{body}\n[score={d.get('score')} comments={d.get('num_comments')}]"
                docs.append(RawDocument(
                    doc_id=RawDocument.make_id(self.name, link, title, content),
                    source=self.name, title=title, url=link,
                    content=content[:1500], published=str(d.get("created_utc") or ""),
                    meta={"subreddit": sub, "score": d.get("score", 0)}))
        return docs[:limit]


# ---------------------------------------------------------------- 纵向穿透
class EDGARSource:
    """SEC EDGAR 全文检索（免 key 官方 API）：8-K 供应链/客户集中扰动、
    10-Q 风险因子措辞变动、S-1 重大合同——财报电话会之外的一手信息。"""
    name = "edgar"

    def __init__(self, timeout: float = 15.0, forms: tuple[str, ...] = ("8-K", "10-Q")):
        self.timeout = timeout
        self.forms = forms

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        q = urllib.parse.quote(f'"{query}"')
        forms = ",".join(self.forms)
        url = (f"https://efts.sec.gov/LATEST/search-index?q={q}"
               f"&forms={forms}&dateRange=custom")
        # 官方全文检索端点（近两年）
        url = f"https://efts.sec.gov/LATEST/search-index?q={q}&forms={forms}"
        docs = []
        try:
            data = _http_json(url, self.timeout,
                              headers={"User-Agent": "AI-Stock-Trading-System admin@localhost"})
        except Exception:
            # 兜底：标准端点
            url2 = (f"https://efts.sec.gov/LATEST/search-index?q={q}")
            data = _http_json(url2, self.timeout,
                              headers={"User-Agent": "AI-Stock-Trading-System admin@localhost"})
        for hit in (data.get("hits", {}).get("hits") or [])[:limit]:
            src = hit.get("_source", {})
            title = f"{src.get('display_names', ['?'])[0]} {src.get('form', '')}"
            adsh = src.get("adsh", "")
            filed = src.get("file_date", "")
            link = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={src.get('ciks', [''])[0]}"
            content = json.dumps({"form": src.get("form"), "items": src.get("items"),
                                  "file_date": filed, "adsh": adsh}, ensure_ascii=False)
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, adsh, title, content),
                source=self.name, title=title, url=link, content=content,
                published=filed, meta={"form": src.get("form"), "adsh": adsh}))
        return docs


class PatentsViewSource:
    """USPTO PatentsView 专利 API（免 key 旧版端点）：专利动态纵向穿透。"""
    name = "patentsview"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        q = urllib.parse.quote(
            json.dumps({"_text_any": {"patent_title": query}}))
        f = urllib.parse.quote(json.dumps(
            ["patent_number", "patent_title", "patent_date", "assignee_organization"]))
        url = (f"https://api.patentsview.org/patents/query"
               f"?q={q}&f={f}&o={urllib.parse.quote(json.dumps({'per_page': limit}))}")
        data = _http_json(url, self.timeout)
        docs = []
        for p in (data.get("patents") or [])[:limit]:
            title = str(p.get("patent_title") or "")
            org = ""
            ass = p.get("assignees") or p.get("assignee_organization") or ""
            if isinstance(ass, list) and ass:
                org = str(ass[0].get("assignee_organization", ""))
            elif isinstance(ass, str):
                org = ass
            content = f"Patent {p.get('patent_number')} | {title} | {org} | {p.get('patent_date')}"
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, str(p.get("patent_number")), title, content),
                source=self.name, title=f"[专利] {title}",
                url=f"https://patents.google.com/patent/US{p.get('patent_number')}",
                content=content, published=str(p.get("patent_date") or ""),
                meta={"assignee": org}))
        return docs


class FederalRegisterSource:
    """美国联邦公报 API（免 key）：政策微调——出口管制、实体清单、补贴规则。"""
    name = "federal_register"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        q = urllib.parse.quote(query)
        url = (f"https://www.federalregister.gov/api/v1/documents.json"
               f"?conditions[term]={q}&per_page={limit}&order=newest"
               f"&fields[]=title&fields[]=publication_date&fields[]=html_url"
               f"&fields[]=abstract&fields[]=agencies&fields[]=type")
        data = _http_json(url, self.timeout)
        docs = []
        for d in (data.get("results") or [])[:limit]:
            title = str(d.get("title") or "")
            agencies = ", ".join(a.get("name", "") for a in (d.get("agencies") or []))
            content = f"[{d.get('type')}] {title}\n机构: {agencies}\n{d.get('abstract') or ''}"
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, str(d.get("html_url") or ""), title, content),
                source=self.name, title=f"[公报] {title}",
                url=str(d.get("html_url") or ""), content=content[:2000],
                published=str(d.get("publication_date") or ""),
                meta={"agencies": agencies, "type": d.get("type")}))
        return docs


# ---------------------------------------------------------------- 离线演示
class DemoSearchSource:
    """离线确定性合成文档（demo 模式专用，保证 demo 全链路可跑可复现）。"""
    name = "demo"

    def __init__(self, seed: int = 42):
        self.seed = seed

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        import hashlib
        docs = []
        templates = [
            ("{q} supply update: channel inventory normalization continues, "
             "spot prices firming across DRAM/NAND segments.", "supply_chain"),
            ("{q} earnings call takeaway: management guided cautiously, "
             "citing mixed enterprise demand and AI capex tailwinds.", "earnings"),
            ("Analyst note: {q} momentum builds as hyperscaler orders accelerate; "
             "foundry utilization rates trending higher QoQ.", "capacity"),
            ("Filing watch: {q} 8-K discloses updated risk factors around "
             "customer concentration and export licensing.", "policy"),
        ]
        for i, (tmpl, ev) in enumerate(templates[:limit]):
            content = tmpl.format(q=query)
            h = hashlib.sha1(f"{self.seed}|{query}|{i}".encode()).hexdigest()[:16]
            docs.append(RawDocument(
                doc_id=h, source=self.name,
                title=f"[demo] {query} — {ev}", url=f"demo://{h}",
                content=content, published="2026-07-30",
                meta={"event_hint": ev}))
        return docs


def default_sources(demo: bool = False) -> list[SearchSource]:
    """生产默认源集群；demo 用确定性源。"""
    if demo:
        return [DemoSearchSource()]
    return [
        KimiSearchSource(),          # 主力：全网实时（多语言查询词）
        GoogleNewsSource(),          # 主流新闻
        RedditSource(),              # 小众论坛
        EDGARSource(),               # 纵向：SEC 披露
        PatentsViewSource(),         # 纵向：专利
        FederalRegisterSource(),     # 纵向：政策微调
        EastmoneyNewsSource(),       # v3：东财中文资讯（可达性强化）
        SinaFlashSource(),           # v3：新浪 7×24 快讯（可达性强化）
    ]


# ---------------------------------------------------------------- v3 可达性强化
class EastmoneyNewsSource:
    """东方财富资讯搜索（免费，T2 聚合门户）：中文财经新闻检索，
    覆盖美股/宏观/A股/港股——在 google_news/reddit 不可达的网络环境下
    提供真实新闻流（v3 数据层强化，实测 2026-08-30 可达且新鲜）。"""
    name = "eastmoney_news"

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        param = json.dumps({
            "uid": "", "keyword": query, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default",
                                           "sort": "time", "pageIndex": 1,
                                           "pageSize": limit}},
        }, ensure_ascii=False)
        url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
               + urllib.parse.quote(param))
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
        m = re.search(r"cb\((.*)\)\s*$", raw, re.S)
        if not m:
            raise RuntimeError("eastmoney_news 返回非 JSONP 内容（疑似被封禁）")
        data = json.loads(m.group(1))
        items = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
        docs = []
        for it in items[:limit]:
            title = re.sub(r"</?em>", "", str(it.get("title") or ""))
            content = re.sub(r"</?em>", "", str(it.get("content") or ""))[:500]
            art_url = str(it.get("url") or
                           f"https://so.eastmoney.com/news/s?keyword={urllib.parse.quote(query)}")
            published = str(it.get("date") or "")
            if not title:
                continue
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, art_url, title, content),
                source=self.name, title=title, url=art_url, content=content,
                published=published, meta={"code": it.get("code")}))
        return docs


class SinaFlashSource:
    """新浪财经 7×24 快讯（免费，T2）：zhibo 实时快讯流，按查询词过滤。
    快讯为中文，英文主题词命中少时如实返回空集（不编造、不凑数）。"""
    name = "sina_flash"

    def __init__(self, timeout: float = 12.0, zhibo_id: int = 152):
        self.timeout = timeout
        self.zhibo_id = zhibo_id

    def search(self, query: str, limit: int = 8) -> list[RawDocument]:
        url = (f"https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id={self.zhibo_id}"
               f"&page=1&page_size=50")
        data = _http_json(url, self.timeout,
                          headers={"Referer": "https://finance.sina.com.cn"})
        items = (((data.get("result") or {}).get("data") or {})
                 .get("feed") or {}).get("list") or []
        q = query.lower()
        docs = []
        for it in items:
            text = str(it.get("rich_text") or "")
            if not text:
                continue
            if q and q not in text.lower():
                continue
            title = re.sub(r"【|】", "", text)[:60]
            art_url = str(it.get("docurl") or "https://zhibo.sina.com.cn/")
            published = str(it.get("create_time") or "")
            docs.append(RawDocument(
                doc_id=RawDocument.make_id(self.name, art_url, title, text[:500]),
                source=self.name, title=title, url=art_url, content=text[:500],
                published=published, meta={"id": it.get("id")}))
            if len(docs) >= limit:
                break
        return docs
