"""Recorded orchestration for opportunity continuity and report-only evaluation.

Every clock, quote, ledger load and write belongs to the caller's strict stage.
An observed level is a requalification request. A provisional intent still needs
an execution-time quote/account check; this module never places a broker order.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, time, timezone
from html import escape
from zoneinfo import ZoneInfo

from .business_evaluation import build_business_evaluation, mature_cohorts
from .calendar import next_trading_day
from .execution_policy import allocate_entry, portfolio_snapshot
from .ledger_io import read_json_strict, write_json_atomic
from .opportunity_lifecycle import OpportunityLedger, _digest, quote_refusal_reason


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock(execution, purpose):
    return execution.call_io("clock.observe", {"purpose": purpose}, utc_now)


def _load(execution):
    path = execution.paths.reports_dir / "opportunity_ledger.json"
    loaded = []
    def operation():
        ledger = OpportunityLedger(path)
        loaded.append(ledger)
        return ledger.snapshot()
    execution.call_io("opportunity.load", {"path": path}, operation)
    return loaded[0]


def _save(execution, ledger):
    def operation():
        ledger.save()
        value = read_json_strict(ledger.path)
        if value != ledger.snapshot():
            raise OSError("Opportunity ledger readback differs")
        return value
    return execution.call_io("opportunity.save", {"path": ledger.path,
        "before": ledger.snapshot()}, operation)


def _artifact(execution, name, value):
    path = execution.paths.reports_dir / name
    root = execution.paths.work_dir.resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Workflow artifact outside its isolated run")
    def operation():
        write_json_atomic(path, value)
        actual = read_json_strict(path)
        if actual != value:
            raise OSError("Workflow artifact readback differs")
        return actual
    execution.call_io("workflow.artifact.write", {"path": path, "value": value}, operation)
    return str(path)


def _current_snapshot(result, state):
    valuation = result.raw["portfolio_snapshot"]["known_at"]
    marks = deepcopy(result.raw["portfolio_marks"])
    for symbol, setup in result.raw.get("execution_setups", {}).items():
        if setup.get("known_at") == valuation:
            marks.setdefault(symbol, {"date": valuation, "price": setup["entry_price"]})
    return portfolio_snapshot(state, marks, valuation)


def _evidence_refs(execution, quote):
    refs = ["snapshot:" + row["output_ref"]["sha256"] for row in execution.summary()
            if row["stage"] in {"layer4.risk", "decision.debate", "paper.persist"}
            and row.get("status") == "completed" and isinstance(row.get("output_ref"), dict)]
    refs.append(quote.get("evidence_id") or "quote:" + _digest(quote))
    return list(dict.fromkeys(refs))


def opportunity_stage(execution, result, account_state: dict, get_quote) -> dict:
    ledger = _load(execution)
    ready = _clock(execution, "research_result_ready")
    expires = datetime.combine(next_trading_day(result.trade_date), time(16),
                              ZoneInfo("America/New_York")).isoformat()
    ledger.record_research(result, run_id=execution.run_id, observed_at=ready, expires_at=expires)
    ledger.sync_account(account_state, snapshot_id=execution.run_id + ":paper-after",
                        observed_at=ready, account_kind="paper")
    current = ledger.snapshot()
    symbols = list(dict.fromkeys(row["ticker"] for row in current["opportunities"]
        if row["state"] not in {"CLOSED", "INVALIDATED", "EXPIRED", "SUPERSEDED"}))
    quotes = {symbol: execution.call_io("opportunity.quote", {"symbol": symbol},
                                        lambda symbol=symbol: get_quote(symbol)) for symbol in symbols}
    observed = _clock(execution, "quotes_observed_after_research")
    alerts = ledger.observe_quotes(quotes, observed_at=observed)
    snapshot = _current_snapshot(result, account_state)
    snapshot["frozen_at"] = observed
    approved = {pick.ticker: pick for pick in result.picks}
    decisions = {row["symbol"]: row for row in result.raw["execution_decisions"]}
    rechecks = []
    for alert in alerts:
        if alert["kind"] != "REQUALIFY":
            continue
        symbol = alert["ticker"]
        pick, quote = approved.get(symbol), quotes.get(symbol)
        commitments = [identity for identity, row in snapshot["reservations"].items()
                       if row["ticker"] == symbol]
        if commitments:
            # paper.step has already persisted the account's pending plan.
            # A later fresh observation is a request to recheck that same
            # commitment, never authority to create a second proposal. Keep
            # the bound account fact and alert; the shared fill-time policy
            # still validates its quote, structure and portfolio budget.
            rechecks.append({"opportunity_id": alert["opportunity_id"], "symbol": symbol,
                "accepted": False, "status": "account_execution_recheck_required",
                "reasons": [alert.get("reason") or "ACCOUNT_ENTRY_ALREADY_PENDING"],
                "account_plan_ids": commitments, "allocation": None,
                "current_research_approved": pick is not None and decisions.get(symbol, {}).get("action") == "buy",
                "risk_evidence_ids": _evidence_refs(execution, quote),
                "requires_execution_recheck": True})
            continue
        reasons, plan, allocation = [], None, None
        if pick is None or decisions.get(symbol, {}).get("action") != "buy":
            reasons.append("CURRENT_COMPLETE_MODEL_AND_NUMERIC_APPROVAL_REQUIRED")
        else:
            # This is an explicit provisional check using the completed model
            # and a fresh quote. Its account valuation date stays visible; a
            # pending intent never claims a seconds-fresh complete portfolio.
            refusal = quote_refusal_reason(quote, observed_at=observed)
            if refusal:
                reasons.append(refusal)
            else:
                setup = deepcopy(result.raw["execution_setups"][symbol])
                setup["entry_price"] = quote["price"]
                budget = {**result.raw["entry_budget"], "snapshot_hash": snapshot["state_hash"],
                          "execution_snapshot": snapshot,
                          "admission_snapshot_hash": result.raw["entry_budget"]["snapshot_hash"]}
                own = next((identity for identity, row in snapshot["reservations"].items()
                            if row["ticker"] == symbol), None)
                allocation = allocate_entry(snapshot, setup, budget, pick.shares, exclude_plan_id=own)
                if not allocation["shares"]:
                    reasons.append(allocation["reason"])
                else:
                    plan = {**asdict(pick), "shares": allocation["shares"],
                            "entry_price": quote["price"], "risk_usd": allocation["risk_usd"],
                            "position_pct": allocation["position_pct"],
                            "quote_evidence_id": alert["evidence_id"],
                            "qualification_known_at": observed,
                            "execution_policy": "whitepaper-execution-v2"}
        accepted = not reasons
        ledger.requalify(alert["opportunity_id"], run_id=execution.run_id,
            observed_at=observed, accepted=accepted, reasons=reasons,
            risk_evidence_ids=_evidence_refs(execution, quote) if isinstance(quote, dict) else [],
            account_snapshot=snapshot, plan=plan, assessment_date=snapshot["known_at"])
        rechecks.append({"opportunity_id": alert["opportunity_id"], "symbol": symbol,
                        "accepted": accepted, "reasons": reasons, "allocation": allocation,
                        "requires_execution_recheck": True})
    value = _save(execution, ledger)
    result.raw["opportunity_lifecycle"] = value
    result.raw["lifecycle_observation"] = {"research_ready_at": ready, "observed_at": observed,
        "alerts": alerts, "requalifications": rechecks,
        "execution_boundary": "Provisional paper/manual coordination only; no broker submission or invented execution."}
    result.raw["current_portfolio_snapshot"] = snapshot
    artifact = _artifact(execution, "opportunity-lifecycle.json", value)
    return {"lifecycle": value, "quotes": quotes, "alerts": alerts,
            "requalifications": rechecks, "current_portfolio_snapshot": snapshot,
            "research_ready_at": ready, "observed_at": observed, "artifact": artifact}


def evaluation_stage(execution, result, account_state: dict, get_history) -> dict:
    ledger = _load(execution)
    as_of = _clock(execution, "business_evaluation")
    snapshot = ledger.snapshot()
    # Outcome prices are fetched only now, after the entire current decision.
    # Neither these historical outcomes nor old model scores enter research.
    symbols = set()
    terminal = {row["opportunity_id"] for row in snapshot["cohort_outcomes"] if row.get("horizon_complete")}
    for row in snapshot["opportunities"]:
        if (row.get("source_run_id") is not None and row["source_run_id"] != execution.run_id
                and row["opportunity_id"] not in terminal
                and row.get("signal_date") and row["signal_date"] < execution.run_date.isoformat()):
            symbols.add(row["ticker"])
            frozen = next((run.get("cohort_inputs") for run in snapshot["research_runs"]
                           if run["run_id"] == row["source_run_id"]), None)
            if isinstance(frozen, dict):
                before = frozen.get("portfolio_state", {})
                symbols.update(item["ticker"] for item in before.get("positions", []) + before.get("pending", []))
                symbols.update(item["ticker"] for item in before.get("entry_plans", {}).values()
                               if item["status"] in {"pending", "partial"})
                for setup in frozen.get("setups", {}).values():
                    if setup.get("sector"):
                        symbols.add(setup["sector"])
    bars = {symbol: execution.call_io("evaluation.history", {"symbol": symbol},
                                      lambda symbol=symbol: get_history(symbol)) for symbol in sorted(symbols)}
    outcomes = mature_cohorts(snapshot, bars, as_of,
        current_policy_hash=result.raw.get("policy_identity", {}).get("sha256"))
    ledger.record_outcomes(outcomes, observed_at=as_of)
    final = _save(execution, ledger)
    evaluation = build_business_evaluation(result, lifecycle=final,
        account_state=account_state, as_of=as_of, run_id=execution.run_id)
    result.raw["opportunity_lifecycle"], result.raw["business_evaluation"] = final, evaluation
    artifacts = {"lifecycle": _artifact(execution, "opportunity-lifecycle.json", final),
                 "evaluation": _artifact(execution, "business-evaluation.json", evaluation)}
    return {"evaluation": evaluation, "lifecycle": final, "market_bars": bars,
            "outcomes": outcomes, "as_of": as_of, "artifacts": artifacts}


def render_workflow_markdown(result) -> str:
    raw = result.raw
    research, evaluation = raw["candidate_research"], raw["business_evaluation"]
    lines = ["## 十二、逐股研究与执行依据", "", research["coverage_summary"], "",
        f"- 研究日：{research['assessment']['run_date']}（纽约）；实际完成时间：{raw['lifecycle_observation']['research_ready_at']}",
        f"- 实际研究 {len(research['coverage']['researched_symbols'])} 只；明确延后 {len(research['coverage']['deferred'])} 只。", ""]
    titles = {"market": "市场环境", "sector": "板块方向", "chain": "产业链位置",
              "stock": "个股证据", "plan": "执行计划"}
    for thesis in research["theses"]:
        lines.extend([f"### {thesis['symbol']} · {thesis['status']}", ""])
        for key, title in titles.items():
            part = thesis["five_part"][key]
            lines.append(f"- {title}：{part['text']}（依据：{', '.join(part['evidence_ids'])}）")
        lines.append(f"- 事件排查：{thesis['event_risk']['text']}（{thesis['event_risk']['status']}）")
        for key, title in (("entry_triggers", "再次开门条件"), ("falsification", "撤销条件"),
                           ("risk_notes", "风险"), ("gaps", "未解决信息")):
            lines.extend(f"- {title}：{item}" for item in thesis[key])
        for claim in thesis["claims"]:
            lines.append(f"- 事实：{claim['statement']}；原句：{claim['quote']}；来源 {claim['source_id']}；"
                         f"原始出处 {claim['origin_id'] or '未知'}；时间角色 {claim['time_role']}")
        lines.append("")
    lines.extend(["## 十三、观察、持仓与分笔状态", "",
        "触价只请求重新复核；待执行意图仍须执行前复核报价和全账户预算。纸盘成交与真实人工执行账分开。", "",
        f"- 本轮报价检查：{raw['lifecycle_observation']['observed_at']}",
        f"- 状态分布：{evaluation['lifecycle']['state_counts']}",
        f"- 本轮触发 {len(raw['lifecycle_observation']['alerts'])} 条，复核 {len(raw['lifecycle_observation']['requalifications'])} 条。", "",
        "## 十四、业务效果追踪", "",
        f"- 决策漏斗：{evaluation['funnel']}",
        f"- 纸盘已结算 {evaluation['account']['n_closed']} 笔；净损益 ${evaluation['account']['pnl_net']:.2f}。",
        f"- 机会成熟标签 {evaluation['opportunities']['matured_labels']}；待成熟 {evaluation['opportunities']['pending_labels']}；"
        f"不可核实或拒绝 {evaluation['opportunities']['unverifiable_or_refused']}。",
        "- 没有成熟独立样本时，机会捕获率、胜率与盈利能力保留未知；几次程序完成不等于几天独立收益。",
        "- 数字规则的逐候选反事实回放不能相加成组合利润，也不能证明历史完整模型策略的超额收益。", ""])
    return "\n".join(lines)


def render_workflow_html(result) -> str:
    # Keep untrusted source/model prose as text. The canonical structured JSON
    # stays separately available; no script or raw HTML is interpreted here.
    text = escape(render_workflow_markdown(result))
    return ('<section id="whitepaper-workflow-v2" style="max-width:1100px;margin:40px auto;'
            'padding:28px;border:1px solid #cbd5e1;border-radius:16px;background:#f8fafc;color:#0f172a">'
            '<h2>逐股研究、执行连续性与效果追踪</h2>'
            '<pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.8">'
            + text + '</pre></section>')
