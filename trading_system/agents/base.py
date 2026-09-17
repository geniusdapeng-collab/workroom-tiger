"""Agent 基类。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..providers.base import DataProvider

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: str = "Base"
    layer: int = 0

    def __init__(self, provider: DataProvider):
        self.provider = provider
        self.log = logging.getLogger(f"agent.{self.name}")

    @abstractmethod
    def execute(self, context: dict):
        """执行并返回结构化结果（写入 context）。"""
