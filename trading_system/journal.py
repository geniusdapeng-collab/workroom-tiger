"""信号日记 — 胜率追踪与自我迭代闭环的核心。

每日运行后：
  1. log_picks()   把风控放行的标的落账（信号日、入场参考、止损、模板、三分数）
  2. settle()      对到期未平仓记录做结算：与回测完全一致的出场规则
                   （次日开盘入场 / 止损优先 / 2R 盈利保护 / 7 日时间止损）
  3. stats()       滚动胜率 / 期望值 / 分模板表现 → 写入日报

这套账本是"胜率可验证、可提升"的事实来源：系统每天的信号都会被
真实结算，日积月累形成样本，配合 WFA 调参形成迭代闭环。
"""

from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np

from . import config
from .ledger_io import (ConcurrentUpdateError, file_digest, file_transaction,
                        read_json_strict, validate_finite_json, write_json_atomic)
from .parameters import (GateParams, risk_limits_for, tighten_risk_limits,
                         validate_risk_limits)
from .position_sizing import integer_capacity

logger = logging.getLogger(__name__)


class Journal:
    def __init__(self, path: str | Path = "reports/journal.json"):
        self.path = Path(path)
        with file_transaction(self.path):
            self.records: list[dict] = self._load()
            self._version = file_digest(self.path)
        self._snapshot = deepcopy(self.records)

    def _load(self) -> list[dict]:
        try:
            records = read_json_strict(self.path)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"信号日记 {self.path} JSON 损坏，已停止以保留原账本") from exc
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or not {"date", "ticker", "status"}.issubset(record)
            or not all(isinstance(record[key], str) and record[key]
                       for key in ("date", "ticker", "status"))
            for record in records
        ):
            raise ValueError(f"信号日记 {self.path} 结构无效，已停止以保留原账本")
        try:
            self._validate_records(records)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"信号日记 {self.path} 记录无效，原账本保留") from exc
        return records

    def save(self) -> None:
        with file_transaction(self.path):
            if file_digest(self.path) != self._version:
                raise ConcurrentUpdateError(f"信号日记版本已变化，拒绝陈旧保存: {self.path}")
            self._save_locked()

    def _save_locked(self) -> None:
        self._validate_records(self.records)
        write_json_atomic(self.path, self.records)
        self._version = file_digest(self.path)
        self._snapshot = deepcopy(self.records)

    @staticmethod
    def _validate_records(records) -> None:
        validate_finite_json(records)
        if not isinstance(records, list):
            raise ValueError("Journal records must be an array")
        keys = []
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get("ticker"), str)
                    or not record["ticker"] or record.get("status") not in ("open", "closed", "void")):
                raise ValueError("Journal record shape/status is invalid")
            date.fromisoformat(record["date"])
            if "gate_params" in record:
                if not isinstance(record["gate_params"], dict):
                    raise ValueError("Journal parameter snapshot must be an object")
                GateParams(**record["gate_params"])
            if "time_stop_days" in record:
                value = record["time_stop_days"]
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 252:
                    raise ValueError("Journal holding limit is invalid")
            for field in ("risk_limits", "execution_risk_limits", "pending_risk_limits"):
                if field in record:
                    validate_risk_limits(record[field], complete=True)
            for field in ("planned_shares", "filled_shares"):
                if field in record and record[field] is not None:
                    value = record[field]
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise ValueError("Journal share snapshot must be a positive integer")
            for field in ("account_equity", "planned_risk_usd", "filled_risk_usd"):
                if field in record and record[field] is not None:
                    value = record[field]
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) or value <= 0):
                        raise ValueError("Journal account/risk snapshot must be finite and positive")
            if "risk_size_ratio" in record:
                value = record["risk_size_ratio"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 < value <= 1):
                    raise ValueError("Journal risk size ratio must be in (0,1]")
            if "gross_cap" in record:
                value = record["gross_cap"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError("Journal gross cap must be a finite fraction")
            keys.append((record["date"], record["ticker"]))
        if len(keys) != len(set(keys)):
            raise ValueError("Journal signal date/ticker identities must be unique")

    def _refresh_locked(self) -> None:
        version = file_digest(self.path)
        if version != self._version:
            if self.records != self._snapshot:
                raise ConcurrentUpdateError(f"信号日记存在未保存修改和并发更新: {self.path}")
            self.records = self._load()
            self._version = version
            self._snapshot = deepcopy(self.records)

    # ------------------------------------------------------------

    def log_picks(self, result, account_usd: float = 100_000) -> int:
        with file_transaction(self.path):
            self._refresh_locked()
            before = deepcopy(self.records)
            try:
                added = self._log_picks(result, account_usd)
                if added:
                    self._save_locked()
                return added
            except Exception:
                self.records = before
                raise

    def _log_picks(self, result, account_usd: float) -> int:
        """把本次放行的 picks 落账（同日同票去重）。返回新增条数。"""
        from .providers.base import source_label

        raw = getattr(result, "raw", {})
        source = ("demo" if raw.get("data_synthetic") is True else
                  source_label(getattr(result, "provider", "")))
        market_data = raw.get("market", {})
        params = tighten_risk_limits(GateParams(**raw.get("gate_params", {})), raw.get("risk_limits"))
        account_usd = raw.get("account_usd", account_usd)
        if (isinstance(account_usd, bool) or not isinstance(account_usd, (int, float))
                or not math.isfinite(account_usd) or account_usd <= 0):
            raise ValueError("Journal account equity must be finite and positive")
        market = market_data.get("market_id", "us")
        from .markets import get_market
        currency = get_market(market).currency
        existing = {(r["date"], r["ticker"]) for r in self.records}
        added = 0
        for p in result.picks:
            key = (result.trade_date, p.ticker)
            if key in existing:
                continue
            self.records.append({
                "date": result.trade_date,
                "ticker": p.ticker,
                "mode": "标准做多" if "标准" in p.card else "轻仓试错",
                "template": p.entry_template or "无",
                "sector": p.sector or "",
                "chain": p.chain or "",
                "entry_ref": p.entry_price,
                "stop": p.stop_price,
                "tss_final": p.tss_final,
                "tos": p.tos,
                "time_stop_days": getattr(p, "time_stop_days", 0) or 0,   # v6.0 ATR 档位
                "gate_params": asdict(params),
                "risk_limits": risk_limits_for(params),
                "gross_cap": min(raw.get("gross_cap", params.gross_cap), params.gross_cap),
                "account_equity": account_usd,
                "risk_size_ratio": raw.get("pick_rationale", {}).get(p.ticker, {}).get("size_ratio", 1.0),
                "planned_shares": p.shares, "planned_risk_usd": p.risk_usd,
                "mrs_star": result.mrs.mrs_star if result.mrs else None,
                "action": result.action,
                "source": source,
                "market": market, "currency": currency,
                "status": "open",
                "entry": None, "entry_date": None,
                "exit": None, "exit_date": None, "r": None, "win": None,
            })
            added += 1
            existing.add(key)
        return added

    # ------------------------------------------------------------

    def settle(self, provider, horizon_days: int = 30, *, as_of: str | None = None,
               risk_limits: dict | None = None) -> int:
        validate_risk_limits(risk_limits)
        with file_transaction(self.path):
            self._refresh_locked()
            before = deepcopy(self.records)
            try:
                settled = self._settle(provider, horizon_days, as_of=as_of, risk_limits=risk_limits)
                if self.records != before:
                    self._save_locked()
                return settled
            except Exception:
                self.records = before
                raise

    def _settle(self, provider, horizon_days: int, *, as_of: str | None,
                risk_limits: dict | None = None) -> int:
        """结算 open 记录：模拟与回测一致的出场规则。返回本次结算条数。"""
        today = date.fromisoformat(as_of) if as_of else datetime.now().date()
        settled = 0
        self.last_failed: list[str] = []     # v6.1：行情缺失导致无法结算的标的（披露用）
        frames: dict[str, object] = {}

        def frame_for(ticker):
            if ticker not in frames:
                frame = provider.ohlcv(ticker, days=min(90, horizon_days * 3))
                frames[ticker] = frame[frame.index.date <= today] if frame is not None else None
            return frames[ticker]

        def exposure_check(record, entry_day, shares, entry_net, gross_cap):
            used = 0.0
            chain_used = 0.0
            for other in self.records:
                if (other is record or other.get("market", "us") != record.get("market", "us")
                        or not other.get("entry_date") or other["entry_date"] > entry_day
                        or other.get("status") == "void"
                        or other.get("exit_date") and other["exit_date"] < entry_day):
                    continue
                quantity = other.get("filled_shares")
                if quantity is None:
                    return False, f"{other['ticker']} 历史成交数量缺失，无法核算总敞口"
                if record.get("chain") and other.get("chain") == record["chain"]:
                    risk = other.get("filled_risk_usd")
                    if risk is None:
                        return False, f"{other['ticker']} 历史成交风险缺失，无法核算产业链风险"
                    chain_used += risk
                try:
                    frame = frame_for(other["ticker"])
                except Exception:
                    self.last_failed.append(other["ticker"])
                    return False, f"{other['ticker']} 持仓行情拉取失败，无法核算总敞口"
                if frame is None or frame.empty:
                    return False, f"{other['ticker']} 入场时点持仓行情缺失，无法核算总敞口"
                exact = frame[frame.index.date == date.fromisoformat(entry_day)]
                if len(exact) == 1:
                    mark = float(exact["Open"].iloc[0])
                else:
                    prior = frame[frame.index.date < date.fromisoformat(entry_day)]
                    if prior.empty:
                        return False, f"{other['ticker']} 入场时点价格缺失，无法核算总敞口"
                    mark = float(prior["Close"].iloc[-1])
                if not math.isfinite(mark) or mark <= 0:
                    return False, f"{other['ticker']} 持仓估值无效，无法核算总敞口"
                used += quantity * mark
            if (record.get("chain") and chain_used + shares * (entry_net - record["stop"])
                    > record["account_equity"] * config.MAX_CHAIN_RISK_PCT + 1e-9):
                return False, "入场后该产业链累计止损风险超过记录账户净值的产业链上限"
            permitted = record["account_equity"] * gross_cap
            if used + shares * entry_net > permitted + 1e-9:
                return False, f"入场后总敞口超过记录账户净值的 {gross_cap:.0%}"
            return True, ""

        for rec in sorted(self.records, key=lambda item: (item["date"], item["ticker"])):
            if rec["status"] != "open":
                continue
            sig_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
            age = (today - sig_date).days
            if age <= 0:                     # 只结算信号日之后的观察
                continue
            try:
                df = frame_for(rec["ticker"])
            except Exception:
                # v6.1：不再静默悬挂——结算失败必须可追踪（日报披露，
                # 否则"信号落账后永远没有下文"，胜率样本悄悄失真）
                self.last_failed.append(rec["ticker"])
                continue
            if df is None or len(df) == 0:
                self.last_failed.append(rec["ticker"])
                continue
            df = df[(df.index.date >= sig_date) & (df.index.date <= today)]
            if len(df) < 1:
                continue
            previous = deepcopy(rec)
            try:
                self._settle_one(rec, df, as_of=today.isoformat(), risk_limits=risk_limits,
                                 _exposure_check=exposure_check)
            except (ValueError, TypeError, KeyError) as exc:
                rec.clear()
                rec.update(previous)
                self.last_failed.append(rec["ticker"])
                logger.warning("信号 %s 行情无法结算，原记录保留: %s", rec["ticker"], exc)
                continue
            if rec["status"] == "closed":
                settled += 1
        if self.last_failed:
            logger.warning("信号日记: %d 个标的行情缺失无法结算（悬挂待下轮）: %s",
                           len(self.last_failed), self.last_failed)
        return settled

    @staticmethod
    def _settle_one(rec: dict, df, *, as_of: str | None = None,
                    risk_limits: dict | None = None, _exposure_check=None) -> None:
        """单条结算（v6.0：统一调用 exit_engine.simulate_trade——与回测同一实现，
        含交易成本净口径；时间止损按落账时的 ATR 档位，缺省全局默认）。

        未满时间止损且未触止损 → 标记 open（浮盈亏按最新收盘估算 r_live）。
        """
        from .exit_engine import cost_adj_buy, simulate_trade
        signal_date = date.fromisoformat(rec["date"])
        limit = date.fromisoformat(as_of) if as_of else datetime.now().date()
        df = df[(df.index.date >= signal_date) & (df.index.date <= limit)].sort_index()
        if df.empty:
            return
        if df.index.has_duplicates:
            raise ValueError("Duplicate journal market observations")

        stop0 = float(rec["stop"])
        params = GateParams(**rec.get("gate_params", {}))
        time_stop = int(rec.get("time_stop_days") or params.time_stop)
        o, h, l_, c = (df["Open"].values, df["High"].values,
                       df["Low"].values, df["Close"].values)
        dates = [d.date() for d in df.index]

        # 入场时预算取原计划与当前限制的较严值；已经成交的数量保持不可变。
        from .markets import get_market
        market = get_market(rec.get("market", "us"))
        valid_entries = [i for i, day in enumerate(dates)
                         if day > signal_date and market.is_trading_day(day)
                         and all(math.isfinite(float(v[i])) and float(v[i]) > 0
                                 for v in (o, h, l_, c))]
        if rec.get("entry_date"):
            valid_entries = [i for i in valid_entries if str(dates[i]) == rec["entry_date"]]
        if not valid_entries:
            return
        # v6.1 公司行动检测（必须先于引擎判定：拆股重定基后开盘价必然
        # "跌破止损"，会被通用作废逻辑拦截而丢失拆股语义）
        ref = float(rec.get("entry_ref") or 0)
        signal_i = next((i for i, day in enumerate(dates) if day == signal_date), None)
        if ref <= 0 and signal_i is not None:
            ref = float(c[signal_i])
        if ref > 0:
            open_next = float(o[valid_entries[0]])
            if open_next > 0 and abs(open_next / ref - 1.0) > 0.35:
                rec.update(status="void", entry=None, entry_date=None,
                           exit=None, exit_date=None, r=None, win=None,
                           note=f"开盘 {open_next:.2f} 与落账参考 {ref:.2f} 偏离超 35%，"
                                "疑似拆股/公司行动，信号作废待人工复核（不计入胜率）")
                return
        effective = tighten_risk_limits(params, rec.get("risk_limits"),
                                        rec.get("pending_risk_limits"), risk_limits)
        quantity_known = all(rec.get(name) is not None for name in
                             ("planned_shares", "planned_risk_usd", "account_equity"))
        for entry_i in valid_entries:
            res = simulate_trade(o, h, l_, c, entry_i, stop0, time_stop=time_stop,
                                 protect_r=params.profit_protect_r, cost_bps=params.cost_bps,
                                 dates=dates, market=market, ticker=rec["ticker"])
            if res is None:
                continue
            if res.void:
                rec.update(status="void", entry=None, entry_date=None,
                           exit=None, exit_date=None, r=None, win=None,
                           note="开盘即破止损位，信号作废（未成交，不计入胜率）")
                return
            if rec.get("entry_date") or not quantity_known:
                break
            entry_net = cost_adj_buy(float(o[entry_i]), params.cost_bps)
            per_share = entry_net - stop0
            ratio = rec.get("risk_size_ratio", 1.0)
            shares = min(rec["planned_shares"], integer_capacity(rec["planned_risk_usd"], per_share),
                         integer_capacity(rec["account_equity"] * effective.risk_r_pct * ratio, per_share),
                         integer_capacity(rec["account_equity"] * effective.max_single_position_pct, entry_net))
            if shares <= 0:
                rec.update(status="void", note="风险预算不足以成交一股（未成交，不计入胜率）")
                return
            gross_cap = min(rec.get("gross_cap", effective.gross_cap), effective.gross_cap)
            permitted, reason = (_exposure_check(rec, str(dates[entry_i]), shares, entry_net, gross_cap)
                                  if _exposure_check is not None else (True, ""))
            if not permitted:
                rec.update(entry_blocked=reason, entry_deferred_date=str(dates[entry_i]),
                           pending_risk_limits=risk_limits_for(effective))
                continue
            rec.update(filled_shares=shares, filled_risk_usd=shares * per_share,
                       execution_risk_limits=risk_limits_for(effective))
            rec.pop("entry_blocked", None)
            break
        else:
            return
        entry_raw = float(o[entry_i])
        if not quantity_known:
            rec.update(filled_shares=None, filled_risk_usd=None,
                       execution_note="历史记录缺少账户或计划数量；仅可回放单股R，实际成交量未记录")
        rec.update(entry=entry_raw, entry_date=str(dates[entry_i]),
                   initial_stop=stop0, effective_stop=res.stop,
                   protected=bool(res.protected))
        if res.exit_i >= 0:
            rec.update(status="closed", exit=res.exit_price,
                       exit_date=str(dates[res.exit_i]), r=round(res.r, 3),
                       win=bool(res.r > 0), note=res.reason)
            return
        # 仍未到期：记录浮动 R（净口径）
        rec["r_live"] = round(res.r, 3)
        rec["protected"] = bool(res.protected)
        rec["entry_net"] = cost_adj_buy(entry_raw, params.cost_bps)
        if res.blocked_reason:
            rec["blocked_exit"] = res.blocked_reason

    # ------------------------------------------------------------

    def stats(self) -> dict:
        closed = [r for r in self.records if r["status"] == "closed" and r.get("r") is not None]
        open_n = sum(1 for r in self.records if r["status"] == "open")
        rs = [r["r"] for r in closed]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]

        def agg(sub):
            if not sub:
                return {"n": 0, "win_rate": 0.0, "avg_r": 0.0}
            w = sum(1 for x in sub if x > 0)
            return {"n": len(sub), "win_rate": round(w / len(sub), 3),
                    "avg_r": round(float(np.mean(sub)), 3)}

        by_template: dict[str, list[float]] = {}
        for r in closed:
            by_template.setdefault(r.get("template") or "无", []).append(r["r"])
        return {
            "closed": len(closed), "open": open_n,
            "win_rate": round(len(wins) / len(rs), 3) if rs else 0.0,
            "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
                if losses and sum(losses) != 0 else (float("inf") if wins else 0.0),
            "total_r": round(sum(rs), 2),
            "last20": agg(rs[-20:]),
            "last50": agg(rs[-50:]),
            "by_template": {k: agg(v) for k, v in sorted(by_template.items())},
        }
