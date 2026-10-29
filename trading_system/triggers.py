"""盘前 / 盘中触发器（P3）— 让系统从"日频批处理"变成"事件驱动"。

- premarket_plan():  盘前基于最新日报的 picks 生成开盘作战计划
                     （入场区 / 止损 / 2R 保护位 / 证伪条件 / 仓位）
- monitor_once():    盘中单轮巡检：对 watch 清单逐票取最新报价，
                     命中入场/止损/保护位则产出结构化警报
- monitor_loop():    定时轮询（默认 5 分钟），输出增量警报

警报等级：ENTRY（触发入场）/ STOP（触发止损证伪）/ PROTECT（浮盈≥2R 启动保护）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


def watch_from_result(result_json: str | Path,
                      only: list[str] | None = None, *,
                      account_state: dict | None = None) -> list[dict]:
    """Restore research WATCH and trusted holdings even when no picks were approved.

    Explicit roles keep an observation candidate from being mistaken for a
    position. No missing price is reconstructed from a model's free text.
    """
    from .ledger_io import read_json_strict
    from .opportunity_lifecycle import positive

    blob = read_json_strict(result_json)
    if not isinstance(blob, dict):
        raise ValueError("Trigger research result must be an object")
    watched, picks = blob.get("watchlist", []), blob.get("picks", [])
    if not isinstance(watched, list) or not isinstance(picks, list):
        raise ValueError("Trigger research collections invalid")
    final = {row.get("symbol"): row.get("action")
             for row in (blob.get("raw") or {}).get("execution_decisions", [])}
    rows = {}

    def add(p, role, *, held=False):
        ticker = p.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("Trigger ticker missing")
        if only and ticker not in only:
            return
        entry = p.get("entry_price") if held or role == "qualified" else p.get("key_level") or p.get("price")
        stop = p.get("stop") if held else p.get("stop_price")
        initial = p.get("initial_stop", stop)
        entry = float(entry) if positive(entry) else None
        stop = float(stop) if positive(stop) else None
        protect = (round(entry + config.PROFIT_PROTECT_R * (entry - initial), 8)
                   if positive(entry) and positive(initial) and initial < entry else None)
        rows[ticker] = {"ticker": ticker, "entry": entry, "stop": stop, "protect": protect,
            "initial_stop": initial if positive(initial) else None,
            "template": p.get("entry_template") or "无", "tss_final": p.get("tss_final"),
            "role": role, "risk_revalidation_required": not held}

    for p in watched:
        add(p, "watch")
    for p in picks:
        role = "qualified" if final.get(p.get("ticker"), "buy") == "buy" else "watch"
        add(p if role == "qualified" else {**p, "key_level": p.get("entry_price")}, role)
    state = account_state or {}
    if not isinstance(state, dict) or not isinstance(state.get("positions", []), list):
        raise ValueError("Trigger account holdings invalid")
    for p in state.get("positions", []):
        add(p, "held", held=True)
    return list(rows.values())


def premarket_plan(result) -> str:
    """盘前作战计划（文本）。"""
    lines = [f"# 盘前作战计划 — {result.trade_date}", ""]
    m = result.mrs
    if m:
        lines.append(f"市场状态: MRS*={m.mrs_star}（{m.regime}）｜ 系统指令: {result.action}")
        lines.append(f"总仓位上限: {m.position_cap[0]:.0%}-{m.position_cap[1]:.0%}")
        lines.append("")
    if not result.picks:
        lines.append("今日无放行标的 — 不开新仓，仅管理存量持仓。")
        return "\n".join(lines)
    lines.append("| 标的 | 模板 | 入场参考 | 止损 | 2R保护位 | 计划股数 | 风险$ |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in result.picks:
        r0 = p.entry_price - p.stop_price
        protect = p.entry_price + config.PROFIT_PROTECT_R * r0
        lines.append(f"| {p.ticker} | {p.entry_template or '-'} | {p.entry_price:.2f} "
                     f"| {p.stop_price:.2f} | {protect:.2f} | {p.shares} | {p.risk_usd:.0f} |")
    lines.append("")
    lines.append("执行纪律：")
    lines.append("- 首仓 40-50%：开盘 30 分钟内不追高，等回踩入场参考位附近企稳")
    lines.append("- 开盘跌破止损位的票直接放弃（结构证伪，不要捡便宜）")
    lines.append("- 重大事件日（财报/FOMC/CPI）隔夜仓位按 0.5-0.7 折扣")
    return "\n".join(lines)


def monitor_once(watch: list[dict], provider,
                 triggered: set | None = None, *, ledger=None,
                 observed_at: str | None = None, max_age_seconds: int = 300) -> list[dict]:
    """单轮巡检。triggered 用于跨轮去重（元素为 'TICKER:LEVEL'）。"""
    # v5.4：报价走降级链（yahoo 限流时 stooq/服务端通道接管），
    # 不再因单源失败静默丢警报；报价时效（实时/延时/收盘）随警报披露。
    from .pipeline import quote_with_fallback
    from .opportunity_lifecycle import positive, quote_refusal_reason
    if ledger is not None:
        timestamp = observed_at or datetime.now(timezone.utc).isoformat()
        symbols = list(dict.fromkeys([row["ticker"] for row in watch] + [
            row["ticker"] for row in ledger.snapshot()["opportunities"]
            if row["state"] in {"WATCH", "QUALIFIED", "INTENT_PENDING", "HELD", "EXIT_PENDING"}]))
        quotes = {symbol: quote_with_fallback(provider, symbol) for symbol in symbols}
        alerts = ledger.observe_quotes(quotes, observed_at=timestamp, max_age_seconds=max_age_seconds)
        for alert in alerts:
            alert["msg"] = f"{alert['ticker']} {alert['kind']}（需按当前风险/执行契约处理；未虚构成交）"
        return alerts
    triggered = triggered if triggered is not None else set()
    alerts: list[dict] = []
    for w in watch:
        q = quote_with_fallback(provider, w["ticker"])
        if not q or not q.get("price"):
            continue
        if q.get("stale"):
            # v6.1：陈旧报价（停牌/源不完整）不触发任何动作——
            # 用上周的价格判止损/入场，比不报警更危险
            logger.warning("%s 报价陈旧（%s），本轮跳过监控", w["ticker"], q.get("ts"))
            continue
        role = w.get("role")
        if role is not None:
            timestamp = observed_at or datetime.now(timezone.utc).isoformat()
            refusal = quote_refusal_reason(q, observed_at=timestamp, max_age_seconds=max_age_seconds)
            if refusal:
                logger.warning("%s 本轮监控拒绝：%s", w["ticker"], refusal)
                continue
        px = float(q["price"])
        for level, kind, msg in (
            ("protect", "PROTECT", f"浮盈达 2R 保护位 {w['protect']} → 止损上移至成本+0.5R"),
            ("stop", "STOP", f"触及止损 {w['stop']} → 结构证伪，离场"),
            ("entry", "ENTRY", f"回到入场参考 {w['entry']} 附近 → 按卡片分批建仓"),
        ):
            if role == "held" and kind == "ENTRY":
                continue
            if role in {"watch", "qualified"} and kind in {"STOP", "PROTECT"}:
                continue
            if not positive(w.get(level)):
                continue
            key = f"{w['ticker']}:{kind}"
            if key in triggered:
                continue
            hit = (px >= w[level]) if kind == "PROTECT" else (px <= w[level])
            if kind == "ENTRY":
                if not positive(w.get("entry")) or positive(w.get("stop")) and px <= w["stop"]:
                    continue
                hit = abs(px / w["entry"] - 1) <= 0.005
            if hit:
                triggered.add(key)
                qkind = q.get("kind", "")
                stale_note = ("" if qkind == "realtime"
                              else "，注意：价格为最近收盘价（非实时）" if qkind == "eod_close"
                              else "，注意：延时报价")
                emitted = "REQUALIFY" if kind == "ENTRY" and role in {"watch", "qualified"} else kind
                alerts.append({"ticker": w["ticker"], "kind": emitted, "price": px,
                               "price_kind": qkind,
                               "ts": q.get("ts"), "requires_requalification": emitted == "REQUALIFY",
                               "msg": f"{w['ticker']} {msg}（现价 {px}{stale_note}）"})
    return alerts


def monitor_loop(watch: list[dict], provider, interval_s: int = 300,
                 cycles: int = 12, on_alert=None) -> list[dict]:
    """定时轮询（默认 5 分钟 × 12 轮 = 1 小时）。on_alert 回调用于实时输出。"""
    triggered: set = set()
    all_alerts: list[dict] = []
    for i in range(cycles):
        alerts = monitor_once(watch, provider, triggered)
        for a in alerts:
            logger.info("[触发] %s", a["msg"])
            if on_alert:
                on_alert(a)
        all_alerts.extend(alerts)
        if i < cycles - 1:
            time.sleep(interval_s)
    return all_alerts
