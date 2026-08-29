"""数据供应商工厂。

provider 优先级：命令行 --provider > 环境变量 TS_PROVIDER > config 默认。
真实降级链：yahoo → stooq → agentgw（服务端通道，限流根修，需 SDK 凭证）；
demo 用于离线演示与测试（确定性合成数据）。
"""

from __future__ import annotations

import os

from .base import DataProvider


def get_provider(name: str | None = None) -> DataProvider:
    name = (name or os.environ.get("TS_PROVIDER") or "yahoo").lower()
    if name == "demo":
        from .demo import DemoProvider
        # v6.0：demo 剧本可经 TS_DEMO_SCENARIO 指定（normal|riskoff），
        # 测试/演示用确定性夹具，不再依赖种子运气
        return DemoProvider(os.environ.get("TS_DEMO_SCENARIO", "normal"))
    if name == "stooq":
        from .stooq import StooqProvider
        return StooqProvider()
    if name == "agentgw":
        from .agentgw import AgentGwProvider
        return AgentGwProvider()
    if name == "tencent":
        from .tencent import TencentProvider
        return TencentProvider()
    if name == "sina":
        from .sina import SinaProvider
        return SinaProvider()
    if name == "eastmoney":
        from .eastmoney import EastMoneyProvider
        return EastMoneyProvider()
    from .yahoo import YahooProvider
    return YahooProvider()


__all__ = ["get_provider", "DataProvider"]
