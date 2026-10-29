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
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config
from .currency import ACCOUNT_CURRENCY, require_usd_records, require_usd_result
from .ledger_io import write_json_atomic
from .redline.execution import EvidenceWriteError, ExecutionContractError, StrictObservationError

logger = logging.getLogger(__name__)


class Journal:
    def __init__(self, path: str | Path = "reports/journal.json"):
        self.path = Path(path)
        self.records: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            records = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"信号日记 {self.path} JSON 损坏，已停止以保留原账本") from exc
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or not {"date", "ticker", "status"}.issubset(record)
            or not all(isinstance(record[key], str) and record[key]
                       for key in ("date", "ticker", "status"))
            for record in records
        ):
            raise ValueError(f"信号日记 {self.path} 结构无效，已停止以保留原账本")
        require_usd_records(records, scope=f"信号日记 {self.path}")
        return records

    def save(self) -> None:
        require_usd_records(self.records, scope=f"信号日记 {self.path}")
        write_json_atomic(self.path, self.records)

    # ------------------------------------------------------------

    def log_picks(self, result, account_usd: float = 100_000) -> int:
        """把本次放行的 picks 落账（同日同票去重）。返回新增条数。"""
        from .providers.base import source_label

        require_usd_result(result, scope="信号日记输入")
        require_usd_records(self.records, scope=f"信号日记 {self.path}")
        if getattr(result, "raw", {}).get("execution_policy") == "whitepaper-execution-v2":
            return self._log_v2(result)
        source = source_label(getattr(result, "provider", ""))   # v6.5 来源留痕
        existing = {(r["date"], r["ticker"]) for r in self.records}
        added = 0
        for p in result.picks:
            key = (result.trade_date, p.ticker)
            if key in existing:
                continue
            self.records.append({
                "date": result.trade_date,
                "ticker": p.ticker,
                "account_currency": ACCOUNT_CURRENCY,
                "quote_currency": ACCOUNT_CURRENCY,
                "mode": "标准做多" if "标准" in p.card else "轻仓试错",
                "template": p.entry_template or "无",
                "sector": p.sector or "",
                "chain": p.chain or "",
                "entry_ref": p.entry_price,
                "stop": p.stop_price,
                "tss_final": p.tss_final,
                "tos": p.tos,
                "time_stop_days": getattr(p, "time_stop_days", 0) or 0,   # v6.0 ATR 档位
                "mrs_star": result.mrs.mrs_star if result.mrs else None,
                "action": result.action,
                "source": source,
                "status": "open",
                "entry": None, "entry_date": None,
                "exit": None, "exit_date": None, "r": None, "win": None,
            })
            added += 1
        if added:
            self.save()
        return added

    # ------------------------------------------------------------

    def settle(self, provider, horizon_days: int = 30, *, as_of_date=None) -> int:
        """结算 open 记录：模拟与回测一致的出场规则。返回本次结算条数。"""
        require_usd_records(self.records, scope=f"信号日记 {self.path}")
        today = as_of_date or datetime.now().date()
        settled = 0
        changed = False
        self.last_failed: list[str] = []     # v6.1：行情缺失导致无法结算的标的（披露用）
        for rec in self.records:
            if rec.get("execution_policy") == "whitepaper-execution-v2" or rec["status"] != "open":
                continue
            sig_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
            age = (today - sig_date).days
            if age < 1:                      # 信号日不得当日入场
                continue
            try:
                df = provider.ohlcv(rec["ticker"], days=min(90, horizon_days * 3))
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception:
                # v6.1：不再静默悬挂——结算失败必须可追踪（日报披露，
                # 否则"信号落账后永远没有下文"，胜率样本悄悄失真）
                self.last_failed.append(rec["ticker"])
                continue
            if df is None or len(df) == 0:
                self.last_failed.append(rec["ticker"])
                continue
            # 保留信号前的覆盖证据；先裁去未来行，不能用未来行情结算。
            df = df[df.index.date <= today]
            if len(df) < 1:
                continue
            if df.index.min().date() > sig_date:
                # 滚动历史已越过信号日：无法证明哪一根才是真实 T+1，等待补齐。
                self.last_failed.append(rec["ticker"])
                continue
            previous = rec.copy()
            self._settle_one(rec, df)
            if rec["status"] == "closed":
                settled += 1
            if rec != previous:                 # 入场日期/浮动状态同样属于会计事实
                changed = True
        if settled or changed:
            self.save()
        if self.last_failed:
            logger.warning("信号日记: %d 个标的行情缺失无法结算（悬挂待下轮）: %s",
                           len(self.last_failed), self.last_failed)
        return settled

    @staticmethod
    def _settle_one(rec: dict, df) -> None:
        """单条结算（v6.0：统一调用 exit_engine.simulate_trade——与回测同一实现，
        含交易成本净口径；时间止损按落账时的 ATR 档位，缺省全局默认）。

        未满时间止损且未触止损 → 标记 open（浮盈亏按最新收盘估算 r_live）。
        """
        from .exit_engine import cost_adj_buy, simulate_trade

        if df is None or len(df) == 0:
            return
        df = df.sort_index()
        dates = [d.date() for d in df.index]
        signal_date = datetime.strptime(rec["date"], "%Y-%m-%d").date()
        if dates[0] > signal_date:
            return                              # 历史窗口不覆盖信号，不捏造入场
        entry_i = next((i for i, day in enumerate(dates) if day > signal_date), None)
        if entry_i is None:
            return                              # 只有信号日或更早数据，尚不可成交

        stop0 = float(rec["stop"])
        time_stop = int(rec.get("time_stop_days") or config.TIME_STOP_DAYS[1])
        o, h, l_, c = (df["Open"].values, df["High"].values,
                       df["Low"].values, df["Close"].values)
        # v6.1 公司行动检测（必须先于引擎判定：拆股重定基后开盘价必然
        # "跌破止损"，会被通用作废逻辑拦截而丢失拆股语义）
        signal_i = next((i for i, day in enumerate(dates) if day == signal_date), None)
        ref = (float(c[signal_i]) if signal_i is not None and float(c[signal_i]) > 0
               else float(rec.get("entry_ref") or 0))
        if ref > 0 and entry_i < len(o):
            open_next = float(o[entry_i])
            if open_next > 0 and abs(open_next / ref - 1.0) > 0.35:
                rec.update(status="void", entry=None, entry_date=None,
                           exit=None, exit_date=None, r=None, win=None,
                           note=f"开盘 {open_next:.2f} 与落账参考 {ref:.2f} 偏离超 35%，"
                                "疑似拆股/公司行动，信号作废待人工复核（不计入胜率）")
                return
        res = simulate_trade(o, h, l_, c, entry_i, stop0, time_stop=time_stop)
        if res is None:
            return
        if res.void:
            # 白皮书 §15：开盘价直接跌破止损的信号【作废】——小G模拟盘同样跳过。
            rec.update(status="void", entry=None, entry_date=None,
                       exit=None, exit_date=None, r=None, win=None,
                       note="开盘即破止损位，信号作废（未成交，不计入胜率）")
            return
        entry_raw = float(o[entry_i])
        rec.update(entry=round(entry_raw, 2), entry_date=str(dates[entry_i]))
        if res.exit_i >= 0:
            rec.update(status="closed", exit=round(res.exit_price, 2),
                       exit_date=str(dates[res.exit_i]), r=round(res.r, 3),
                       win=bool(res.r > 0), note=res.reason)
            return
        # 仍未到期：记录浮动 R（净口径）
        rec["r_live"] = round(res.r, 3)
        rec["protected"] = bool(res.protected)
        rec["entry_net"] = round(cost_adj_buy(entry_raw), 2)   # 含成本入场参考

    def _log_v2(self, result):
        """Research is a pending proposal. Only execution receipts can fill it."""
        from .execution_policy import _compact_plan_budget
        records = deepcopy(self.records)
        existing = {(row["date"], row["ticker"]) for row in records}
        added = 0
        for pick in result.picks:
            key = (result.trade_date, pick.ticker)
            if key in existing:
                continue
            records.append({"date": result.trade_date, "ticker": pick.ticker,
                "account_currency": ACCOUNT_CURRENCY, "quote_currency": ACCOUNT_CURRENCY,
                "execution_policy": "whitepaper-execution-v2", "account_kind": "paper",
                "template": pick.entry_template, "sector": pick.sector, "chain": pick.chain,
                "entry_ref": pick.entry_price, "stop": pick.stop_price,
                "tss_final": pick.tss_final, "tos": pick.tos,
                "mrs_star": result.mrs.mrs_star if result.mrs else None,
                "time_stop_days": pick.time_stop_days, "action": result.action,
                "source": result.provider, "status": "pending", "entry": None,
                "entry_date": None, "exit": None, "exit_date": None, "r": None,
                "win": None, "proposed_shares": pick.shares,
                "entry_budget": _compact_plan_budget(result.raw.get("entry_budget"))})
            existing.add(key)
            added += 1
        if added:
            require_usd_records(records, scope=f"信号日记 {self.path}")
            write_json_atomic(self.path, records)
            self.records = records
        return added

    def sync_execution(self, sim_state: dict, *, as_of: str) -> int:
        """Mirror authoritative paper plans/fills/closes; never invent a fill."""
        from .currency import require_usd_sim_state
        from .execution_policy import POLICY_ID
        require_usd_sim_state(sim_state, scope="信号日记纸盘回执")
        datetime.strptime(as_of, "%Y-%m-%d")
        records = deepcopy(self.records)
        changed = 0
        for identity, plan in sim_state.get("entry_plans", {}).items():
            if plan.get("policy") != POLICY_ID:
                continue
            key = (plan["signal_date"], plan["ticker"])
            rec = next((row for row in records if (row["date"], row["ticker"]) == key), None)
            if rec is None:
                rec = {"date": key[0], "ticker": key[1], "status": "pending",
                       "execution_policy": POLICY_ID, "account_kind": "paper",
                       "account_currency": ACCOUNT_CURRENCY, "quote_currency": ACCOUNT_CURRENCY,
                       "template": plan["entry_template"], "entry_ref": plan["entry_ref"],
                       "stop": plan["initial_stop"], "entry": None, "entry_date": None,
                       "exit": None, "exit_date": None, "r": None, "win": None}
                records.append(rec)
            if rec.get("execution_policy") != POLICY_ID:
                raise ValueError("Legacy journal row cannot be silently overwritten by v2")
            previous = deepcopy(rec)
            rec.update(plan_id=identity, fill_receipts=deepcopy(plan["fill_receipts"]),
                       execution_policy=POLICY_ID, initial_stop=plan["initial_stop"])
            pos = next((row for row in sim_state["positions"] if row.get("plan_id") == identity), None)
            closes = [row for row in sim_state["closed"] if row.get("plan_id") == identity]
            if len(closes) > 1:
                raise ValueError("Duplicate paper close receipt")
            if closes:
                closed = closes[0]
                if closed["exit_date"] > as_of:
                    raise ValueError("Future paper close receipt")
                if not math.isclose(closed["gross_pnl"] - closed["friction_cost"], closed["pnl_usd"], abs_tol=.011):
                    raise ValueError("Paper cost identity does not reconcile")
                rec.update(status="closed", entry=closed["entry"], entry_date=closed["entry_date"],
                    exit=closed["exit"], exit_date=closed["exit_date"], shares=closed["shares"],
                    pnl_usd=closed["pnl_usd"], gross_pnl=closed["gross_pnl"],
                    friction_cost=closed["friction_cost"], r=closed["r_multiple"],
                    win=closed["pnl_usd"] > 0, note=closed["reason"],
                    initial_risk_usd=closed["initial_risk_usd"])
            elif pos is not None:
                if pos["entry_date"] > as_of:
                    raise ValueError("Future paper fill receipt")
                rec.update(status="open", entry=pos["entry_price"], entry_date=pos["entry_date"],
                           shares=pos["shares"], protected=pos.get("protected", False),
                           current_stop=pos["stop"], initial_risk_usd=pos["initial_risk_usd"])
            elif plan["status"] in {"cancelled", "closed"}:
                rec.update(status="void", note=plan.get("terminal_reason", "paper_plan_cancelled"))
            else:
                rec["status"] = "pending"
            changed += rec != previous
        if records != self.records:
            require_usd_records(records, scope=f"信号日记 {self.path}")
            write_json_atomic(self.path, records)
            self.records = records
        return changed

    # ------------------------------------------------------------

    def stats(self) -> dict:
        require_usd_records(self.records, scope=f"信号日记 {self.path}")
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
            "pending": sum(1 for r in self.records if r["status"] == "pending"),
            "win_rate": round(len(wins) / len(rs), 3) if rs else 0.0,
            "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
                if losses and sum(losses) != 0 else (float("inf") if wins else 0.0),
            "total_r": round(sum(rs), 2),
            "last20": agg(rs[-20:]),
            "last50": agg(rs[-50:]),
            "by_template": {k: agg(v) for k, v in sorted(by_template.items())},
        }
