"""NarrativeAgent — 板块叙事兑现维度（SHS·S_narr）的 LLM 驱动实现。

v4.1 之前：EPS 修正/指引属免费源缺失维度，s_narr=None → 剔除再归一化。
v5.0：按"凡涉及语义理解一律 LLM"的普查结论升级为 LLM 驱动——
从搜索/清洗后的财报电话会、研报、新闻文本中推理各板块的
盈利修正方向（eps_revision_bias）与指引语气（guidance_tone），
合成 narrative_score 0..10 注入 SHS。

红线 2：本 Agent 只有 LLM 一条产出路径；LLM 不可用 → llm_guard 透传
（SectorAgent 收到 None，维持"缺失因子再归一化"，绝不用规则估算叙事）。
"""

from __future__ import annotations

import logging

from ..llm.client import LLMClient
from ..llm.prompts import NARRATIVE_SCHEMA, NARRATIVE_USER_TMPL, SYSTEM_ANALYST
from ..redline import ExecutionTracer, Passthrough, llm_guard
from ..search.models import CleanDocument
from ..semantic_values import finite_number, object_rows, text_list

log = logging.getLogger("agents.narrative")


class NarrativeAgent:
    name = "Narrative-Agent"
    layer = 2

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def execute(self, docs: list[CleanDocument], etfs: list[str],
                tracer: ExecutionTracer | None = None
                ) -> dict[str, dict] | Passthrough:
        """返回 {etf: {"score": 0..10, "bias": ..., "tone": ..., "evidence": ...}}；
        LLM 不可用/无产出 → Passthrough。"""
        def _run() -> dict[str, dict]:
            payload = "\n\n".join(
                f"<doc id=\"{c.raw.doc_id}\" source=\"{c.raw.source}\" "
                f"tickers=\"{','.join(c.tickers)}\">{c.raw.content[:900]}</doc>"
                for c in docs[:24])
            out = self.llm.complete_json(
                system=SYSTEM_ANALYST,
                user=NARRATIVE_USER_TMPL.format(etfs=" ".join(etfs), docs=payload),
                schema_hint=NARRATIVE_SCHEMA, max_tokens=2000)
            result: dict[str, dict] = {}
            for item in object_rows(out, "sectors"):
                if not isinstance(item.get("etf"), str):
                    continue
                etf = item["etf"].upper()
                if etf not in etfs:
                    continue
                score = finite_number(item.get("narrative_score"), 0.0, 10.0)
                evidence = text_list(item.get("evidence"), 2)
                bias = item.get("eps_revision_bias", "unknown")
                tone = item.get("guidance_tone", "unknown")
                if (score is None or evidence is None
                        or bias not in ("up", "flat", "down", "unknown")
                        or tone not in ("positive", "neutral", "negative", "unknown")):
                    continue
                result[etf] = {
                    "score": score,
                    "bias": bias,
                    "tone": tone,
                    "evidence": "; ".join(evidence),
                }
            return result or None

        return llm_guard("sector.narrative", _run, fallback_payload=docs, tracer=tracer)
