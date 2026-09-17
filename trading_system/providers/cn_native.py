"""CN/HK 标的符号 → 各免费源原生符号的翻译层（v3.3 多市场数据修复）。

背景：A股/港股真实实测（2026-08-30）暴露——内核基准符号用 Yahoo 风格
（000300.SS / 0700.HK / HSI.HK），而中文免费源各有原生格式：
  腾讯：sh000300 / sz399001 / hk00700 / hkHSI（K 线需 Referer: gu.qq.com）
  新浪：sh000300 / sz399001（CN_MarketDataService，需 Referer: finance.sina.com.cn）

统一约定：Yahoo 风格为内核单一口径（ markets/*.py、config.MARKET_BENCHMARKS ），
各 provider 在本模块翻译，绝不把源特定格式泄漏到决策层。
"""

from __future__ import annotations

import re

_CN_RE = re.compile(r"^(\d{6})\.(SS|SH|SZ)$", re.I)
_HK_STOCK_RE = re.compile(r"^(\d{4,5})\.HK$", re.I)
_HK_INDEX_RE = re.compile(r"^([A-Z]+)\.HK$", re.I)


def cn_native(ticker: str) -> str | None:
    """A股 Yahoo 风格 → 中文源原生（sh/sz 前缀）。非 A股返回 None。

    000300.SS → sh000300 ｜ 399001.SZ → sz399001 ｜ 600519.SS → sh600519
    """
    m = _CN_RE.match(ticker or "")
    if not m:
        return None
    code, exch = m.group(1), m.group(2).upper()
    return ("sz" if exch == "SZ" else "sh") + code


def hk_native(ticker: str) -> str | None:
    """港股 Yahoo 风格 → 中文源原生（hk 前缀）。非港股返回 None。

    0700.HK → hk00700 ｜ 00700.HK → hk00700 ｜ HSI.HK → hkHSI ｜ HSTECH.HK → hkHSTECH
    """
    t = (ticker or "").upper()
    m = _HK_STOCK_RE.match(t)
    if m:
        return "hk" + m.group(1).zfill(5)
    m = _HK_INDEX_RE.match(t)
    if m:
        return "hk" + m.group(1)
    return None
