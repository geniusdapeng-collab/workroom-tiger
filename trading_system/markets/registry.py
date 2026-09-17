"""市场注册表（v6.3 S4）— market_id → MarketSpec 的唯一解析出口。"""

from __future__ import annotations

from .base import ComplianceVerdict, MarketSpec
from .cn import CNMarket
from .hk import HKMarket
from .us import USMarket

_MARKETS: dict[str, MarketSpec] = {
    "us": USMarket(),
    "cn": CNMarket(),
    "hk": HKMarket(),
}


def get_market(market_id: str = "us") -> MarketSpec:
    """解析市场规格；未知市场立即失败（不静默回退，D2 诚实失败）。"""
    mid = (market_id or "us").lower()
    if mid not in _MARKETS:
        raise ValueError(f"未知市场: {market_id}（已注册: {sorted(_MARKETS)}）")
    return _MARKETS[mid]


def market_ids() -> list[str]:
    return sorted(_MARKETS)


def all_markets() -> dict[str, MarketSpec]:
    return dict(_MARKETS)


__all__ = ["get_market", "market_ids", "all_markets",
           "MarketSpec", "ComplianceVerdict"]
