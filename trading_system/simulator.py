"""Transactional paper-trading accounting, with exchange-aware execution.

Signals are registered after the close and can fill only on a later session.
Every fill obeys today's risk gate; every sale obeys the market fence. Prices
and cash are in the recorded account currency, never silently converted.
"""

from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from . import config
from .exit_engine import evaluate_day, trading_days_held
from .ledger_io import (ConcurrentUpdateError, file_digest, file_transaction,
                        read_json_strict, validate_finite_json, write_json_atomic)
from .markets import get_market
from .parameters import (GateParams, risk_limits_for, tighten_risk_limits,
                         validate_risk_limits)
from .position_sizing import integer_capacity

log = logging.getLogger(__name__)
TIME_STOP_DAYS = 7
PROFIT_PROTECT_R = 2.0
SIM_FILENAME = "sim_portfolio.json"


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    adv: float = 0.0
    date: str | None = None
    prev_close: float | None = None
    vcm_cooling: bool = False


def friction_bps(adv: float, *, fallback_bps: float | None = None) -> float:
    fallback = config.COST_BPS if fallback_bps is None else fallback_bps
    if (isinstance(fallback, bool) or not isinstance(fallback, (int, float))
            or not math.isfinite(fallback) or not 0 <= fallback < 10_000):
        raise ValueError("Fallback transaction cost must be finite valid basis points")
    if isinstance(adv, (int, float)) and not isinstance(adv, bool) and math.isfinite(adv) and adv > 0:
        for lo, slip in config.SIM_SLIPPAGE_BY_ADV:
            if adv >= lo:
                return slip + config.SIM_COMMISSION_BPS
    return fallback


def _positive(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


class SimEngine:
    def __init__(self, path: str, initial_cash: float = 100_000.0):
        if not _positive(initial_cash):
            raise ValueError("Initial cash must be finite and positive")
        self.path = Path(path)
        self.initial_cash = initial_cash
        with file_transaction(self.path):
            loaded = self._load()
            self.state = loaded if loaded is not None else {
                "started": None, "initial_cash": initial_cash,
                "cash": initial_cash, "positions": [], "pending": [],
                "closed": [], "equity_curve": [], "ops_log": [],
            }
            self._version = file_digest(self.path)
        self._snapshot = deepcopy(self.state)

    def _load(self) -> dict | None:
        try:
            state = read_json_strict(self.path)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"模拟盘账本 {self.path} JSON 损坏，已停止以保留原账本") from exc
        self._validate_state(state, self.path)
        return state

    @staticmethod
    def _validate_state(state, path):
        validate_finite_json(state)
        required = {"started", "initial_cash", "cash", "positions", "pending",
                    "closed", "equity_curve", "ops_log"}
        if (not isinstance(state, dict) or not required.issubset(state)
                or any(not isinstance(state[key], list)
                       for key in ("positions", "pending", "closed", "equity_curve", "ops_log"))
                or state["started"] is not None and not isinstance(state["started"], str)
                or not _positive(state["initial_cash"])
                or isinstance(state["cash"], bool) or not isinstance(state["cash"], (int, float))
                or state["cash"] < 0):
            raise ValueError(f"模拟盘账本 {path} 结构无效，已停止以保留原账本")
        for key in ("positions", "pending", "closed", "equity_curve", "ops_log"):
            if any(not isinstance(rec, dict) for rec in state[key]):
                raise ValueError(f"模拟盘账本 {path} 的 {key} 记录无效")
        for key in ("positions", "pending"):
            for rec in state[key]:
                stop_key = "stop" if key == "positions" else "stop_price"
                if (not isinstance(rec.get("ticker"), str) or not rec["ticker"]
                        or isinstance(rec.get("shares"), bool) or not isinstance(rec.get("shares"), int)
                        or rec["shares"] <= 0 or not _positive(rec.get(stop_key))
                        or not _positive(rec.get("risk_usd"))):
                    raise ValueError(f"模拟盘账本 {path} 的 {key} 风险字段无效")
                day = rec.get("entry_date" if key == "positions" else "signal_date")
                if day is not None:
                    date.fromisoformat(day)
                if key == "positions" and not _positive(rec.get("entry_price")):
                    raise ValueError(f"模拟盘账本 {path} 入场价无效")
                if "time_stop_days" in rec:
                    value = rec["time_stop_days"]
                    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 252:
                        raise ValueError(f"模拟盘账本 {path} 持仓期限无效")
                for field in ("profit_protect_r", "initial_r", "initial_stop"):
                    if field in rec and not _positive(rec[field]):
                        raise ValueError(f"模拟盘账本 {path} 的 {field} 参数无效")
                for field in ("cost_bps", "friction_bps"):
                    if field in rec:
                        value = rec[field]
                        if (isinstance(value, bool) or not isinstance(value, (int, float))
                                or not math.isfinite(value) or not 0 <= value < 10_000):
                            raise ValueError(f"模拟盘账本 {path} 的 {field} 参数无效")
                if "protected" in rec and not isinstance(rec["protected"], bool):
                    raise ValueError(f"模拟盘账本 {path} 盈利保护状态无效")
                if "gate_params" in rec:
                    if not isinstance(rec["gate_params"], dict):
                        raise ValueError("Paper parameter snapshot must be an object")
                    GateParams(**rec["gate_params"])
                if "risk_limits" in rec:
                    validate_risk_limits(rec["risk_limits"], complete=True)
                for field in ("account_equity", "risk_budget_usd"):
                    if field in rec and not _positive(rec[field]):
                        raise ValueError(f"Paper {field} must be finite and positive")
                if "risk_size_ratio" in rec and not (
                        _positive(rec["risk_size_ratio"]) and rec["risk_size_ratio"] <= 1):
                    raise ValueError("Paper risk size ratio must be in (0,1]")
                if "gross_cap" in rec and not (
                        not isinstance(rec["gross_cap"], bool)
                        and isinstance(rec["gross_cap"], (int, float))
                        and math.isfinite(rec["gross_cap"]) and 0 <= rec["gross_cap"] <= 1):
                    raise ValueError("Paper gross cap must be a valid fraction")
        for point in state["equity_curve"]:
            date.fromisoformat(point["date"])
            if not _positive(point.get("equity")):
                raise ValueError(f"模拟盘账本 {path} 净值无效")
        dates = [point["date"] for point in state["equity_curve"]]
        if dates != sorted(set(dates)):
            raise ValueError(f"模拟盘账本 {path} 净值日期重复或逆序")
        for key in ("positions", "pending"):
            tickers = [record["ticker"] for record in state[key]]
            if len(tickers) != len(set(tickers)):
                raise ValueError(f"模拟盘账本 {path} 的 {key} 标的重复")
        for record in state["closed"]:
            if any(not isinstance(record.get(key), (int, float))
                   or isinstance(record.get(key), bool) or not math.isfinite(record[key])
                   for key in ("pnl_usd", "r_multiple")):
                raise ValueError(f"模拟盘账本 {path} 已平仓收益字段无效")
            if "risk_limits" in record:
                validate_risk_limits(record["risk_limits"], complete=True)

    def save(self) -> None:
        with file_transaction(self.path):
            if file_digest(self.path) != self._version:
                raise ConcurrentUpdateError(f"模拟盘版本已变化，拒绝陈旧保存: {self.path}")
            self._save_locked()

    def _save_locked(self) -> None:
        self._validate_state(self.state, self.path)
        write_json_atomic(self.path, self.state, indent=1)
        self._version = file_digest(self.path)
        self._snapshot = deepcopy(self.state)

    def _refresh_locked(self) -> None:
        version = file_digest(self.path)
        if version != self._version:
            if self.state != self._snapshot:
                raise ConcurrentUpdateError(f"模拟盘存在未保存修改和并发更新: {self.path}")
            loaded = self._load()
            if loaded is None:
                raise ConcurrentUpdateError(f"模拟盘账本被并发删除: {self.path}")
            self.state = loaded
            self._version = version
            self._snapshot = deepcopy(loaded)

    def step(self, trade_date: str, result, get_bar, max_new: int = 8) -> dict:
        with file_transaction(self.path):
            self._refresh_locked()
            self._validate_state(self.state, self.path)
            before = deepcopy(self.state)
            try:
                out = self._step(trade_date, result, get_bar, max_new)
                self._save_locked()
                return out
            except Exception:
                self.state = before
                raise

    def _step(self, trade_date: str, result, get_bar, max_new: int) -> dict:
        date.fromisoformat(trade_date)
        if isinstance(max_new, bool) or not isinstance(max_new, int) or max_new < 0:
            raise ValueError("max_new must be a non-negative integer")
        from .providers.base import source_label
        raw = getattr(result, "raw", {}) or {}
        source = ("demo" if raw.get("data_synthetic") is True else
                  source_label(getattr(result, "provider", "")))
        spec = get_market((raw.get("market") or {}).get("market_id", "us"))
        s = self.state
        if s.get("market") not in (None, spec.market_id) or s.get("currency") not in (None, spec.currency):
            raise ValueError("Account market/currency differs from this run; use separate ledgers")
        if s["equity_curve"] and s["equity_curve"][-1]["date"] > trade_date:
            raise ValueError("Refusing to replay an earlier date into the live paper ledger")
        s.update(market=spec.market_id, currency=spec.currency)
        s["started"] = s["started"] or trade_date
        ops: list[str] = []
        cache: dict[str, Bar | None] = {}

        def bar_for(ticker: str) -> Bar | None:
            if ticker in cache:
                return cache[ticker]
            bar = get_bar(ticker)
            if bar is not None:
                prices = (bar.open, bar.high, bar.low, bar.close)
                if (not all(_positive(v) for v in prices)
                        or bar.low > min(bar.open, bar.close)
                        or bar.high < max(bar.open, bar.close)
                        or bar.date is not None and bar.date != trade_date):
                    ops.append(f"⚠️ {ticker} 日K日期或OHLC无效，未成交、未消费出场状态")
                    bar = None
            cache[ticker] = bar
            return bar

        mrs = getattr(result, "mrs", None)
        score = getattr(mrs, "mrs_star", None)
        params = tighten_risk_limits(GateParams(**raw.get("gate_params", {})),
                                      raw.get("risk_limits"))
        can_buy = (getattr(result, "action", "AVOID") != "AVOID"
                   and getattr(mrs, "allow_new_positions", True)
                   and not getattr(mrs, "shock", False)
                   and _positive(score) and params.mrs_block <= score <= 10)
        cap = raw.get("gross_cap", getattr(mrs, "position_cap", (0, 1))[1])
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 0 <= cap <= 1:
            raise ValueError("Gross cap must be finite and in [0,1]")
        cap = min(cap, params.gross_cap)
        held = {p["ticker"] for p in s["positions"]}
        still_pending = []
        for p in s["pending"]:
            if (not can_buy or not spec.is_trading_day(trade_date)
                    or not p.get("signal_date") or p["signal_date"] >= trade_date):
                still_pending.append(p)
                if not can_buy:
                    ops.append(f"⏸️ {p['ticker']} 今日禁开仓或非交易日，挂单保留未成交")
                continue
            if p["ticker"] in held:
                ops.append(f"⚠️ {p['ticker']} 已有持仓，重复挂单取消")
                continue
            if len(s["positions"]) >= params.max_picks:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 本轮持仓数达到 {params.max_picks}，挂单顺延")
                continue
            bar = bar_for(p["ticker"])
            if bar is None:
                still_pending.append(p)
                continue
            fill = float(bar.open)
            if fill <= p["stop_price"]:
                ops.append(f"⚠️ {p['ticker']} 开盘价跌破止损，信号作废")
                continue
            prev = bar.prev_close if bar.prev_close is not None else 0.0
            verdict = spec.check_order("buy", p["ticker"], fill, prev, trade_date,
                                       vcm_cooling=bar.vcm_cooling)
            if not verdict.allowed:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 买入被市场围栏拒绝：{verdict.reason}")
                continue
            fallback_cost = p.get("cost_bps", params.cost_bps)
            bps = friction_bps(bar.adv, fallback_bps=fallback_cost)
            fill_net = fill * (1 + bps / 10_000)
            risk_ps = fill_net - p["stop_price"]
            original_params = GateParams(**p.get("gate_params", {}))
            original_limits = risk_limits_for(tighten_risk_limits(
                original_params, p.get("risk_limits")))
            effective = tighten_risk_limits(params, original_limits)
            invested = sum(x["shares"] * (bar_for(x["ticker"]).open
                           if bar_for(x["ticker"]) else x.get("last_close", x["entry_price"]))
                           for x in s["positions"])
            equity_est = s["cash"] + invested
            planned_equity = p.get("account_equity", s["initial_cash"])
            ratio = p.get("risk_size_ratio", 1.0)
            risk_budget = min(p["risk_usd"],
                              equity_est * effective.risk_r_pct * ratio,
                              planned_equity * original_limits["risk_r_pct"] * ratio)
            single_budget = min(equity_est * effective.max_single_position_pct,
                                planned_equity * original_limits["max_single_position_pct"])
            shares = min(p["shares"], integer_capacity(risk_budget, risk_ps),
                         integer_capacity(single_budget, fill_net))
            cost = shares * fill_net
            if shares <= 0 or cost > s["cash"]:
                ops.append(f"⚠️ {p['ticker']} 开盘漂移后风险或现金超限，信号取消")
                continue
            effective_cap = min(cap, effective.gross_cap, p.get("gross_cap", effective.gross_cap))
            if equity_est <= 0 or (invested + cost) / equity_est > effective_cap + 1e-9:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 成交后总敞口超 {effective_cap:.0%}，挂单顺延")
                continue
            chain = p.get("chain", "")
            chain_used = sum(x["risk_usd"] for x in s["positions"]
                             if x.get("chain", "") == chain)
            chain_budget = min(equity_est, planned_equity) * config.MAX_CHAIN_RISK_PCT
            if chain and chain_used + shares * risk_ps > chain_budget + 1e-9:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 成交后该产业链累计止损风险超限，挂单顺延")
                continue
            s["cash"] = round(s["cash"] - cost, 4)
            s["positions"].append({
                "ticker": p["ticker"], "shares": shares, "entry_price": fill_net,
                "stop": p["stop_price"], "initial_stop": p["stop_price"],
                "initial_r": risk_ps, "protected": False, "entry_date": trade_date,
                "signal_date": p["signal_date"], "chain": p.get("chain", ""),
                "sector": p.get("sector", ""), "risk_usd": shares * risk_ps,
                "time_stop_days": p.get("time_stop_days") or params.time_stop, "peak_price": fill,
                "profit_protect_r": p.get("profit_protect_r", params.profit_protect_r),
                "cost_bps": fallback_cost,
                "source": p.get("source", source), "entry_raw": fill,
                "friction_bps": bps, "market": spec.market_id, "currency": spec.currency,
                "risk_limits": risk_limits_for(effective), "gate_params": asdict(effective),
                "account_equity": equity_est, "risk_size_ratio": ratio,
                "risk_budget_usd": risk_budget,
            })
            held.add(p["ticker"])
            ops.append(f"🟢 买入 {p['ticker']} {shares} 股 @ {fill:.3f}（{spec.currency}，次日开盘价成交）")
        s["pending"] = still_pending

        kept = []
        for pos in s["positions"]:
            bar = bar_for(pos["ticker"])
            if (bar is None or not spec.is_trading_day(trade_date)
                    or pos.get("last_evaluated_date") == trade_date):
                kept.append(pos)
                continue
            if pos.get("market") not in (None, spec.market_id):
                raise ValueError("Position market differs from account market")
            pos.setdefault("initial_r", pos["risk_usd"] / pos["shares"])
            pos.setdefault("initial_stop", pos["entry_price"] - pos["initial_r"])
            pos.setdefault("protected", pos["stop"] > pos["initial_stop"])
            days = trading_days_held(pos["entry_date"], trade_date, spec)
            act = evaluate_day(bar.open, bar.high, bar.low, bar.close,
                               pos["entry_price"], pos["stop"], days,
                               time_stop=pos.get("time_stop_days") or params.time_stop,
                               protect_r=pos.get("profit_protect_r", params.profit_protect_r),
                               initial_stop=pos["initial_stop"], initial_r=pos["initial_r"],
                               protected=pos["protected"])
            pos["last_evaluated_date"] = trade_date
            pos["last_close"] = bar.close
            pos["stop"], pos["protected"] = act.new_stop, act.protected
            if act.action == "protect":
                protect_r = pos.get("profit_protect_r", params.profit_protect_r)
                ops.append(f"🛡️ {pos['ticker']} 盘中达{protect_r:g}R，下一根K止损上移至+0.5R {pos['stop']:.3f}")
            if act.action == "exit":
                prev = bar.prev_close if bar.prev_close is not None else 0.0
                verdict = spec.check_order("sell", pos["ticker"], act.exit_price, prev,
                                           trade_date, buy_date=pos["entry_date"],
                                           vcm_cooling=bar.vcm_cooling)
                if not verdict.allowed:
                    pos["blocked_exit"] = verdict.reason
                    ops.append(f"⏸️ {pos['ticker']} 应出场但未可成交：{verdict.reason}")
                    pos["previous_close"] = bar.close
                    kept.append(pos)
                    continue
                exit_raw = act.exit_price
                bps_out = (friction_bps(bar.adv) if isinstance(bar.adv, (int, float))
                           and math.isfinite(bar.adv) and bar.adv > 0 else
                           float(pos.get("friction_bps", pos.get("cost_bps", params.cost_bps))))
                exit_net = exit_raw * (1 - bps_out / 10_000)
                pnl = (exit_net - pos["entry_price"]) * pos["shares"]
                gross = (exit_raw - float(pos.get("entry_raw") or pos["entry_price"])) * pos["shares"]
                r_mult = pnl / pos["risk_usd"]
                s["cash"] = round(s["cash"] + exit_net * pos["shares"], 4)
                s["closed"].append({
                    "ticker": pos["ticker"], "signal_date": pos.get("signal_date"),
                    "entry_date": pos["entry_date"], "exit_date": trade_date,
                    "entry": pos["entry_price"], "exit": exit_net, "shares": pos["shares"],
                    "pnl_usd": round(pnl, 2), "r_multiple": round(r_mult, 4),
                    "reason": act.reason, "days": days, "gross_pnl": round(gross, 2),
                    "gross_r": round(gross / pos["risk_usd"], 4),
                    "friction_cost": round(gross - pnl, 2), "net_r": round(r_mult, 4),
                    "source": pos.get("source", source), "market": spec.market_id,
                    "currency": spec.currency,
                    "risk_limits": pos.get("risk_limits", risk_limits_for(params)),
                })
                ops.append(f"🔴 卖出 {pos['ticker']} {pos['shares']} 股 @ {exit_net:.3f}（{act.reason}，{r_mult:.2f}R）")
            else:
                pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), bar.high)
                pos["previous_close"] = bar.close
                kept.append(pos)
        s["positions"] = kept

        equity = s["cash"]
        marks: dict[str, float] = {}
        for pos in s["positions"]:
            bar = bar_for(pos["ticker"]) if spec.is_trading_day(trade_date) else None
            px = bar.close if bar else pos.get("last_close", pos["entry_price"])
            marks[pos["ticker"]] = px
            equity += pos["shares"] * px
        equity = round(equity, 4)
        if spec.is_trading_day(trade_date):
            point = {"date": trade_date, "equity": equity, "marks": marks,
                     "currency": spec.currency}
            if not s["equity_curve"] or s["equity_curve"][-1]["date"] != trade_date:
                s["equity_curve"].append(point)
            else:
                s["equity_curve"][-1] = point
        if can_buy and getattr(result, "picks", []):
            held = ({p["ticker"] for p in s["positions"]}
                    | {p["ticker"] for p in s["pending"]}
                    | {p["ticker"] for p in s["closed"] if p["exit_date"] == trade_date})
            for pick in result.picks[:max_new]:
                if pick.ticker in held:
                    continue
                rationale = raw.get("pick_rationale", {}).get(pick.ticker, {})
                s["pending"].append({
                    "ticker": pick.ticker, "shares": pick.shares,
                    "stop_price": pick.stop_price, "risk_usd": pick.risk_usd,
                    "chain": pick.chain, "sector": pick.sector,
                    "entry_ref": getattr(pick, "entry_price", 0.0),
                    "signal_date": trade_date,
                    "time_stop_days": getattr(pick, "time_stop_days", 0) or 0,
                    "profit_protect_r": params.profit_protect_r, "cost_bps": params.cost_bps,
                    "source": source, "market": spec.market_id, "currency": spec.currency,
                    "gate_params": asdict(params), "risk_limits": risk_limits_for(params),
                    "gross_cap": cap,
                    "account_equity": rationale.get("account", raw.get("account_usd", equity)),
                    "risk_size_ratio": rationale.get("size_ratio", 1.0),
                    "note": f"{pick.entry_template or '标准'}｜质量 {pick.tss_final}/10",
                })
                held.add(pick.ticker)
                ops.append(f"📋 新信号 {pick.ticker} 登记，下一交易日才能成交")
        if not ops:
            ops.append("😴 今日按兵不动——纪律优先，空仓也是一种仓位")
        entry = {"date": trade_date, "ops": ops, "equity": equity, "source": source}
        if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date:
            entry["ops"] = list(dict.fromkeys(s["ops_log"][-1]["ops"] + ops))
            s["ops_log"][-1] = entry
        else:
            s["ops_log"].append(entry)
        return {"equity": equity, "ops": ops, "marks": marks, "currency": spec.currency}

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict:
        s = self.state
        closed = s["closed"]
        n = len(closed)
        wins = [c for c in closed if c["pnl_usd"] > 0]
        gross_w = sum(c["pnl_usd"] for c in wins)
        gross_l = abs(sum(c["pnl_usd"] for c in closed if c["pnl_usd"] <= 0))
        curve = [e["equity"] for e in s["equity_curve"]]
        peak, max_dd = s["initial_cash"], 0.0
        for e in curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak if peak else 0.0)
        equity = curve[-1] if curve else s["initial_cash"]
        # v6.3 三栏汇总：毛收益 / 摩擦成本 / 净收益（旧台账缺字段时摩擦按 0 计）
        gross_pnl = round(sum(c.get("gross_pnl", c["pnl_usd"]) for c in closed), 2)
        net_pnl = round(sum(c["pnl_usd"] for c in closed), 2)
        friction_total = round(sum(c.get("friction_cost", 0.0) for c in closed), 2)
        return {
            "equity": equity, "cash": s["cash"],
            "invested": round(equity - s["cash"], 2),
            "cum_return": (equity / s["initial_cash"] - 1.0) if s["initial_cash"] else 0.0,
            "pnl_gross": gross_pnl, "pnl_net": net_pnl,
            "friction_total": friction_total,
            "n_closed": n, "win_rate": (len(wins) / n) if n else None,
            "expectancy_r": (sum(c["r_multiple"] for c in closed) / n) if n else None,
            "profit_factor": (round(gross_w / gross_l, 2) if gross_l > 0 else None) if n else None,
            "max_drawdown": max_dd, "days": len(s["equity_curve"]),
        }



def _trading_days(d0: str, d1: str, curve: list[dict] | None = None,
                  market: str = "us") -> int:
    """Compatibility entry point; holding age comes from the exchange calendar."""
    return trading_days_held(d0, d1, get_market(market))
