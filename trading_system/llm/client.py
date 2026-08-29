"""LLM 客户端抽象 — 所有 LLM 驱动环节的唯一入口。

实现：
  - KimiGatewayClient：通过 agent-gw SDK 调 Kimi chat_completion
    （生产环境用有权限的 KIMI_API_KEY；沙箱默认 key 无 chat 权限时会
     抛 QuotaExceededError → 统一转为 LLMUnavailable → 红线透传兜底）。

红线 2：本模块没有、也不允许有任何"规则回退"实现（例如关键词打分、
正则情感分析）。LLM 不可用的唯一出路是抛 LLMUnavailable。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Protocol

from ..redline import LLMUnavailable

log = logging.getLogger("llm.client")

DEFAULT_MODEL = os.environ.get("KIMI_CHAT_MODEL", "kimi-k2.5")


class LLMClient(Protocol):
    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int = 1200, temperature: float = 0.2) -> dict:
        """请求 LLM 输出 JSON（按 schema_hint 约定结构），解析后返回 dict。
        任何失败（网络/权限/解析）都必须抛 LLMUnavailable。"""
        ...


class KimiGatewayClient:
    """agent-gw chat_completion 实现。"""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 45.0,
                 max_retries: int = 2):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None

    def _gw(self):
        if self._client is None:
            try:
                from agent_gw import AgentGwClient
            except ImportError as e:
                raise LLMUnavailable(f"agent-gw SDK 未安装: {e}")
            try:
                self._client = AgentGwClient(timeout=self.timeout)
            except Exception as e:  # 无配置文件 / 无 key
                raise LLMUnavailable(f"agent-gw 初始化失败: {e}")
        return self._client

    def complete_json(self, *, system: str, user: str, schema_hint: dict,
                      max_tokens: int = 1200, temperature: float = 0.2) -> dict:
        prompt = (
            f"{user}\n\n"
            "严格只输出 JSON（不要 markdown 代码块、不要解释），结构遵循："
            f"{json.dumps(schema_hint, ensure_ascii=False)}"
        )
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._gw().chat_completion(
                    model=self.model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=temperature,
                )
                text = self._extract_text(resp)
                return self._parse_json(text)
            except LLMUnavailable:
                raise
            except Exception as e:
                last_err = e
                log.info("LLM 第 %d 次尝试失败: %s", attempt + 1, e)
        raise LLMUnavailable(f"chat_completion 失败: {last_err}")

    @staticmethod
    def _extract_text(resp: Any) -> str:
        try:
            choices = resp.get("choices") if isinstance(resp, dict) else None
            if choices is None and hasattr(resp, "choices"):
                choices = resp.choices
            ch0 = choices[0]
            msg = ch0.get("message") if isinstance(ch0, dict) else ch0.message
            content = msg.get("content") if isinstance(msg, dict) else msg.content
            if isinstance(content, list):  # 分段 content
                content = "".join(seg.get("text", "") if isinstance(seg, dict)
                                  else getattr(seg, "text", "") for seg in content)
            if not content:
                raise ValueError("空 content")
            return str(content)
        except Exception as e:
            raise LLMUnavailable(f"响应结构无法解析: {e}")

    @staticmethod
    def _parse_json(text: str) -> dict:
        t = text.strip()
        t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, flags=re.DOTALL)
            if not m:
                raise LLMUnavailable("LLM 输出非 JSON")
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError as e:
                raise LLMUnavailable(f"LLM JSON 解析失败: {e}")
        if not isinstance(obj, dict):
            raise LLMUnavailable("LLM 输出不是 JSON 对象")
        return obj


def default_client() -> LLMClient:
    """生产默认客户端。调用方必须捕获 LLMUnavailable → 红线透传。"""
    return KimiGatewayClient()
