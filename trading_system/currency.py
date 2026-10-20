"""Currency boundary for the existing USD risk budget and paper ledgers.

Market research keeps native prices. There is no FX conversion contract, so a
non-USD or explicitly ambiguous currency cannot enter sizing or accounting.
Metadata-free legacy inputs retain the historical USD contract, but known CN/HK
symbols and explicit currency declarations always override that compatibility.
This module neither converts prices nor relabels or migrates old ledger rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .markets import all_markets

ACCOUNT_CURRENCY = "USD"
_MARKET_CURRENCIES = {mid: spec.currency for mid, spec in all_markets().items()}


class CurrencyBoundaryError(ValueError):
    """The research result remains usable, but USD execution must stop."""

    def __init__(self, policy: dict, scope: str):
        self.policy = {**policy, "scope": scope, "execution_blocked": True}
        super().__init__(f"{scope}：{policy['reason']}")


def _code(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _native_currency(ticker: object) -> str | None:
    """Only reject known native-market symbols; never invent a conversion."""
    symbol = _code(ticker)
    if symbol.endswith((".SS", ".SZ")):
        return "CNY"
    if symbol.endswith(".HK"):
        return "HKD"
    return None


def _blocked(policy: dict, detail: str) -> dict:
    return {**policy, "execution_blocked": True,
            "reason": (f"USD 账户执行已暂停：{detail}；尚无可核验的汇率换算，"
                       "仅保留原币行情与研究，不计算可执行仓位、不更新交易账本。")}


def usd_execution_policy(market=None, *, tickers: Iterable[object] = ()) -> dict:
    """Return the USD boundary without changing market data or strategy scores.

    ``market=None`` is the pre-multi-market API's documented USD contract. An
    explicit market object must supply its quote currency; an empty declaration
    cannot silently become USD. Known native symbols are checked even when
    metadata is missing or claims USD.
    """
    if market is None:
        market_id, quote, basis = "", ACCOUNT_CURRENCY, "legacy_usd_contract"
    else:
        values = market if isinstance(market, Mapping) else {
            "market_id": getattr(market, "market_id", ""),
            "currency": getattr(market, "currency", ""),
        }
        market_id = str(values.get("market_id") or "").lower().strip()
        quote, basis = _code(values.get("currency")), "market_metadata"
    policy = {"account_currency": ACCOUNT_CURRENCY, "quote_currency": quote or None,
              "currency_basis": basis, "fx_conversion": "unsupported",
              "execution_blocked": False, "reason": ""}
    if not quote:
        return _blocked(policy, "行情报价币种未明确")
    if market_id and market_id not in _MARKET_CURRENCIES:
        return _blocked(policy, "市场身份无法核实")
    if market_id and _MARKET_CURRENCIES[market_id] != quote:
        return _blocked(policy, f"市场 {market_id} 与声明报价币种 {quote} 冲突")
    if quote != ACCOUNT_CURRENCY:
        return _blocked(policy, f"行情报价为 {quote}，账户与风险预算为 USD")
    native = sorted({currency for ticker in tickers
                     if (currency := _native_currency(ticker)) is not None})
    if native:
        return _blocked(policy, f"标的包含 {'/'.join(native)} 原币报价，不能作为 USD 价格")
    return policy


def _assert_declarations(values: Mapping, scope: str) -> None:
    """Check explicit result/ledger units without adding metadata to old data."""
    for key in ("account_currency", "quote_currency", "currency"):
        if key in values and _code(values[key]) != ACCOUNT_CURRENCY:
            currency = _code(values[key]) or "未知"
            raise CurrencyBoundaryError(_blocked(usd_execution_policy(),
                f"{key} 声明为 {currency}，与 USD 账本不兼容"), scope)
    markets = []
    if "market" in values:
        markets.append(values["market"])
    if "market_id" in values:
        market_id = str(values["market_id"]).lower().strip()
        markets.append({"market_id": market_id,
                        "currency": _MARKET_CURRENCIES.get(market_id, "")})
    for market in markets:
        policy = usd_execution_policy(market if market is not None else {})
        if policy["execution_blocked"]:
            raise CurrencyBoundaryError(policy, scope)
    if values.get("execution_blocked"):
        raise CurrencyBoundaryError(_blocked(usd_execution_policy(),
            "输入已声明币种执行边界受阻"), scope)
    for key in ("currency_policy", "accounting"):
        if key in values:
            declaration = values[key]
            if not isinstance(declaration, Mapping):
                raise CurrencyBoundaryError(_blocked(usd_execution_policy(),
                    f"{key} 币种声明无效"), scope)
            _assert_declarations(declaration, scope)


def require_usd_records(records: Iterable[Mapping], *, scope: str) -> None:
    """Reject known foreign history before reads used as stats or any mutation."""
    for record in records:
        if not isinstance(record, Mapping):
            continue  # Structural validation is the ledger's own responsibility.
        _assert_declarations(record, scope)
        policy = usd_execution_policy(tickers=[record.get("ticker")])
        if policy["execution_blocked"]:
            raise CurrencyBoundaryError(policy, scope)


def require_usd_sim_state(state: Mapping, *, scope: str) -> None:
    _assert_declarations(state, scope)
    for bucket in ("positions", "pending", "closed"):
        require_usd_records(state.get(bucket, []), scope=scope)


def require_usd_result(result, *, scope: str = "交易结果") -> dict:
    """Preflight public accounting APIs, including metadata-free old callers."""
    raw = getattr(result, "raw", {})
    if not isinstance(raw, Mapping):
        raise CurrencyBoundaryError(_blocked(usd_execution_policy(), "币种元数据无效"), scope)
    picks = getattr(result, "picks", [])
    market = raw.get("market") if "market" in raw else None
    # An explicitly null market declaration is unknown, not the legacy API.
    policy = usd_execution_policy({} if "market" in raw and market is None else market,
                                  tickers=[getattr(pick, "ticker", None) for pick in picks])
    if policy["execution_blocked"]:
        raise CurrencyBoundaryError(policy, scope)
    if hasattr(result, "__dict__"):
        _assert_declarations(vars(result), scope)
    _assert_declarations(raw, scope)
    require_usd_records((vars(pick) for pick in picks if hasattr(pick, "__dict__")),
                        scope=scope)
    return policy
