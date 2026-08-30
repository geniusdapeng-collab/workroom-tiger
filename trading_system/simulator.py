"""小虎模拟盘记账引擎（老虎全球资产管理 · 全AI掌控模拟盘）。

定位：公开验证 AI 投资能力的 Paper Trading 台账。每日跟随系统全链路产出记账：
  - 信号节拍：T 日收盘后产生信号 → **T+1 日开盘价成交**（无未来函数，
    公开战绩抗质疑）；
  - 出场纪律：止损（盘中触及按止损价，跳空低开按开盘价）/ 时间止损
    （7 个交易日未推进）/ 盈利保护（浮盈 ≥2R 止损上移至成本线）；
  - AVOID 日：禁止新开仓（pending 不落），现持仓继续按出场纪律管理；
    HOLD（轻仓试探区）日出战的轻仓信号照常登记——与 journal 落账口径互为镜像
    （白皮书 §9.1：HOLD=轻仓试探区"只做最高质量结构"；§15.2：小G 与 journal 镜像）；
  - 零基线兼容：台账 sim_portfolio.json 属会计账（白名单保留），
    行情一律当日实时拉取，绝不复用历史缓存。

台账 JSON 结构（全可序列化）：
  cash / positions[] / pending[] / closed[] / equity_curve[] / ops_log[]
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from . import config

log = logging.getLogger(__name__)

TIME_STOP_DAYS = 7          # 时间止损：7 个交易日未推进
PROFIT_PROTECT_R = 2.0      # 盈利保护：浮盈 ≥2R 止损移成本线
SIM_FILENAME = "sim_portfolio.json"


@dataclass
class Bar:
    """单根日 K（来自四环行情链的最新完整交易日）。

    adv: 20 日平均成交额（USD），用于 ADV 分档滑点（v6.3 摩擦模型）；
    adv≤0 表示未知（旧台账/外部注入），回落 v6.0 合并口径 COST_BPS。
    """
    open: float
    high: float
    low: float
    close: float
    adv: float = 0.0


def friction_bps(adv: float) -> float:
    """单边摩擦成本（bp）= ADV 分档滑点 + 单边佣金（v6.3，防滑点断崖）。

    - adv > 0：按 config.SIM_SLIPPAGE_BY_ADV 分档（ADV 越小滑点越大）
      + config.SIM_COMMISSION_BPS（与 v6.0 回测净口径一致的单边 10bp）；
    - adv ≤ 0（未知）：回落 v6.0 合并口径 config.COST_BPS（10bp），
      保证旧台账与未携带 ADV 的 Bar 口径不变。
    """
    if adv and adv > 0:
        for lo, slip in config.SIM_SLIPPAGE_BY_ADV:
            if adv >= lo:
                return slip + config.SIM_COMMISSION_BPS
    return config.COST_BPS


class SimEngine:
    def __init__(self, path: str, initial_cash: float = 100_000.0):
        self.path = path
        self.initial_cash = initial_cash
        self.state = self._load() or {
            "started": None, "initial_cash": initial_cash,
            "cash": initial_cash, "positions": [], "pending": [],
            "closed": [], "equity_curve": [], "ops_log": [],
        }

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> dict | None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------- 主节拍
    def step(self, trade_date: str, result, get_bar, max_new: int = 8) -> dict:
        """每日记账：先结算昨日信号（今日开盘价成交）→ 出场纪律 → 记净值
        → 登记今日新信号（明日成交）。

        result: PipelineResult（今日全链路产出）
        get_bar: callable(ticker) -> Bar | None（最新完整日 K，四环链实时拉取）
        """
        s = self.state
        s["started"] = s["started"] or trade_date
        ops: list[str] = []

        # ---------- 1. 昨日信号成交（T+1 开盘价，无未来函数） ----------
        still_pending = []
        for p in s["pending"]:
            if p.get("signal_date") and p["signal_date"] >= trade_date:
                still_pending.append(p)          # 当日信号次日才成交（同日重跑幂等）
                continue
            bar = get_bar(p["ticker"])
            if bar is None or bar.open <= 0:
                still_pending.append(p)          # 无行情，顺延一日
                continue
            fill = round(bar.open, 2)
            if fill <= p["stop_price"]:
                ops.append(f"⚠️ {p['ticker']} 开盘价直接跌破止损 {p['stop_price']:.2f}，"
                           "风险模型失效，信号作废")
                continue
            risk_ps = abs(fill - p["stop_price"])
            if risk_ps <= 0:
                continue
            shares = min(p["shares"], int(p["risk_usd"] / risk_ps))
            # v6.3：成交价 = 开盘价 × (1 + 单边摩擦)——ADV 分档滑点 + 单边佣金
            # （保守摩擦口径，防滑点断崖；ADV 未知时回落 v6.0 合并 10bp）
            bps = friction_bps(bar.adv)
            fill_net = round(fill * (1 + bps / 10_000), 4)
            cost = shares * fill_net
            if shares <= 0 or cost > s["cash"]:
                ops.append(f"⚠️ {p['ticker']} 开盘价漂移后风险/现金超限，放弃该信号")
                continue
            # v6.0 总敞口执行（白皮书§4.4）：计划投入后敞口超 MRS* 上限则顺延
            cap = (result.mrs.position_cap[1] if getattr(result, "mrs", None) else 1.0)
            invested_now = sum(x["shares"] * x["entry_price"] for x in s["positions"])
            equity_est = s["cash"] + invested_now
            if cap < 1.0 and equity_est > 0 and (invested_now + cost) / equity_est > cap:
                still_pending.append(p)
                ops.append(f"⏸️ {p['ticker']} 敞口将达 {(invested_now + cost) / equity_est:.0%}"
                           f" 超上限 {cap:.0%}，顺延（仓位是风险预算，不是观点表达）")
                continue
            s["cash"] = round(s["cash"] - cost, 2)
            s["positions"].append({
                "ticker": p["ticker"], "shares": shares, "entry_price": fill_net,
                "stop": p["stop_price"], "entry_date": trade_date,
                "chain": p.get("chain", ""), "sector": p.get("sector", ""),
                "risk_usd": round(shares * risk_ps, 2),
                "time_stop_days": p.get("time_stop_days", 0),
                "peak_price": fill, "note": p.get("note", ""),
                # v6.3 三栏台账：毛口径入场价与入场摩擦档位（出场摩擦按当时 ADV 重定档）
                "entry_raw": fill, "friction_bps": bps,
            })
            ops.append(f"🟢 买入 {p['ticker']} {shares} 股 @ {fill:.2f}"
                       f"（开盘价成交，止损 {p['stop_price']:.2f}）")
        s["pending"] = still_pending

        # ---------- 2. 出场纪律 ----------
        kept = []
        for pos in s["positions"]:
            bar = get_bar(pos["ticker"])
            if bar is None:
                kept.append(pos)
                continue
            # v6.0：出场判定统一调用 exit_engine.evaluate_day（与 journal/回测同一套
            # 规则语义，含 ATR 档位化时间止损）；卖出按净价（v6.3 ADV 分档摩擦）。
            from .exit_engine import evaluate_day
            days = _trading_days(pos["entry_date"], trade_date, s["equity_curve"])
            act = evaluate_day(bar.open, bar.high, bar.low, bar.close,
                               pos["entry_price"], pos["stop"], days,
                               time_stop=pos.get("time_stop_days") or TIME_STOP_DAYS)
            exit_price, reason = None, ""
            if act.action == "exit":
                exit_price, reason = act.exit_price, act.reason
            elif act.action == "protect":
                pos["stop"] = pos["entry_price"]
                ops.append(f"🛡️ {pos['ticker']} 浮盈达 2R，止损上移至成本线 {pos['stop']:.2f}")
            if exit_price is not None:
                # v6.3：卖出净价 = 毛价 × (1 − 单边摩擦)；出场 ADV 未知时沿用入场档位
                exit_raw = exit_price
                bps_out = (friction_bps(bar.adv) if bar.adv and bar.adv > 0
                           else float(pos.get("friction_bps") or friction_bps(0.0)))
                exit_price = round(exit_raw * (1 - bps_out / 10_000), 4)
                proceeds = exit_price * pos["shares"]
                pnl = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
                r_mult = round(pnl / max(pos["risk_usd"], 1e-9), 2)
                # v6.3 三栏台账：毛收益 / 摩擦成本 / 净收益（恒等式 毛−摩擦=净）
                entry_raw = float(pos.get("entry_raw") or pos["entry_price"])
                gross_pnl = round((exit_raw - entry_raw) * pos["shares"], 2)
                friction = max(0.0, round(gross_pnl - pnl, 2))   # 摩擦永不为负
                gross_r = round(gross_pnl / max(pos["risk_usd"], 1e-9), 2)
                s["cash"] = round(s["cash"] + proceeds, 2)
                s["closed"].append({
                    "ticker": pos["ticker"], "entry_date": pos["entry_date"],
                    "exit_date": trade_date, "entry": pos["entry_price"],
                    "exit": exit_price, "shares": pos["shares"],
                    "pnl_usd": pnl, "r_multiple": r_mult, "reason": reason,
                    "days": _trading_days(pos["entry_date"], trade_date, s["equity_curve"]),
                    "gross_pnl": gross_pnl, "gross_r": gross_r,
                    "friction_cost": friction, "net_r": r_mult,
                })
                ops.append(f"🔴 卖出 {pos['ticker']} {pos['shares']} 股 @ {exit_price:.2f}"
                           f"（{reason}，{'盈' if pnl >= 0 else '亏'} ${abs(pnl):,.0f}，{r_mult}R）")
            else:
                pos["peak_price"] = max(pos["peak_price"], bar.high)
                kept.append(pos)
        s["positions"] = kept

        # ---------- 3. 记净值 ----------
        equity = s["cash"]
        marks: dict[str, float] = {}
        for pos in s["positions"]:
            bar = get_bar(pos["ticker"])
            px = bar.close if bar else pos["entry_price"]
            marks[pos["ticker"]] = px
            equity += pos["shares"] * px
        equity = round(equity, 2)
        if not s["equity_curve"] or s["equity_curve"][-1]["date"] != trade_date:
            s["equity_curve"].append({"date": trade_date, "equity": equity})
        else:
            s["equity_curve"][-1]["equity"] = equity

        # ---------- 4. 登记今日新信号（明日开盘价成交） ----------
        # v3 镜像修复（2026-08-30 真实运行暴露）：旧口径仅 BUY/LIGHT 日登记，
        # 导致 HOLD（轻仓试探区）日 journal 落账 4 笔而 sim 按兵不动——两本账
        # 不再镜像（白皮书 §15.2）。L4 闸门已把五态语义编码进 picks 本身，
        # 故 HOLD/BUY/LIGHT 日只要存在放行 picks 即与 journal 同口径登记；
        # 唯独 AVOID 是白皮书绝对禁止项（MRS*<4 不开新仓），防御性拒绝登记。
        if result.picks and result.action != "AVOID":
            held = ({p["ticker"] for p in s["positions"]}
                    | {p["ticker"] for p in s["pending"]})
            added = 0
            for pick in result.picks[:max_new]:
                if pick.ticker in held:
                    continue                      # 同名标的已有敞口，不重复登记
                s["pending"].append({
                    "ticker": pick.ticker, "shares": pick.shares,
                    "stop_price": pick.stop_price, "risk_usd": pick.risk_usd,
                    "chain": pick.chain, "sector": pick.sector,
                    "signal_date": trade_date,
                    "time_stop_days": getattr(pick, "time_stop_days", 0) or 0,
                    "note": f"{pick.entry_template or '标准'}｜质量 {pick.tss_final}/10",
                })
                held.add(pick.ticker)
                added += 1
            if added:
                ops.append(f"📋 新信号 {added} 只登记在册，"
                           "明日开盘价成交（无未来函数）")
        if not ops:
            ops.append("😴 今日按兵不动——纪律优先，空仓也是一种仓位")

        entry = {"date": trade_date, "ops": ops, "equity": equity}
        if s["ops_log"] and s["ops_log"][-1]["date"] == trade_date:
            s["ops_log"][-1] = entry               # 同日重跑幂等覆盖
        else:
            s["ops_log"].append(entry)
        self.save()
        return {"equity": equity, "ops": ops, "marks": marks}

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict:
        s = self.state
        closed = s["closed"]
        n = len(closed)
        wins = [c for c in closed if c["pnl_usd"] > 0]
        gross_w = sum(c["pnl_usd"] for c in wins)
        gross_l = abs(sum(c["pnl_usd"] for c in closed if c["pnl_usd"] <= 0))
        curve = [e["equity"] for e in s["equity_curve"]]
        peak, max_dd = self.initial_cash, 0.0
        for e in curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak if peak else 0.0)
        equity = curve[-1] if curve else self.initial_cash
        # v6.3 三栏汇总：毛收益 / 摩擦成本 / 净收益（旧台账缺字段时摩擦按 0 计）
        gross_pnl = round(sum(c.get("gross_pnl", c["pnl_usd"]) for c in closed), 2)
        net_pnl = round(sum(c["pnl_usd"] for c in closed), 2)
        friction_total = round(sum(c.get("friction_cost", 0.0) for c in closed), 2)
        return {
            "equity": equity, "cash": s["cash"],
            "invested": round(equity - s["cash"], 2),
            "cum_return": (equity / self.initial_cash - 1.0) if self.initial_cash else 0.0,
            "pnl_gross": gross_pnl, "pnl_net": net_pnl,
            "friction_total": friction_total,
            "n_closed": n, "win_rate": (len(wins) / n) if n else None,
            "expectancy_r": (sum(c["r_multiple"] for c in closed) / n) if n else None,
            "profit_factor": (round(gross_w / gross_l, 2) if gross_l > 0 else None) if n else None,
            "max_drawdown": max_dd, "days": len(s["equity_curve"]),
        }


def _trading_days(d0: str, d1: str, curve: list[dict]) -> int:
    """持仓交易日数：用净值曲线日期序列数（真实记账日）。"""
    dates = [e["date"] for e in curve]
    try:
        return max(1, len([d for d in dates if d0 < d <= d1]))
    except Exception:
        return 1
