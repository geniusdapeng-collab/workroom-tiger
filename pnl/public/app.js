"use strict";

(() => {
  const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const MARKET_PRICE = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 });
  const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });
  const PERCENT = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const DATE_TIME = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
  const NEW_YORK_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ACTION_NAMES = { buy: "买入指令", sell: "卖出指令", hold: "继续持有", watch: "暂不买入" };
  const REQUIRED_MODEL_STAGES = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"];
  const RESEARCH_MODEL_STAGES = ["clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "research.candidates", "decision.debate"];
  const THESIS_PARTS = [["market", "市场环境"], ["sector", "板块主线"], ["chain", "产业链传导"], ["stock", "这家公司的机会"], ["plan", "交易计划与边界"]];
  const STRATEGY_NAMES = { technical_flow_v2: "技术与资金型", news_event_v2: "新闻事件型" };
  const RESEARCH_STATUS = { sufficient: "所需证据已具备", insufficient: "证据仍有缺口", contradictory: "证据存在冲突" };
  const LIFECYCLE_NAMES = { WATCH: "观察中", QUALIFIED: "本轮获准候选", INTENT_PENDING: "执行前待复核", HELD: "模拟账户持有", EXIT_PENDING: "模拟退出待执行", CLOSED: "模拟持仓已结束", INVALIDATED: "原结构已失效", EXPIRED: "已过有效期", SUPERSEDED: "已被新研究接替" };
  const REJECTION_NAMES = { entry_template_missing: "尚未形成可执行的入场结构", sector_evidence_missing: "板块依据不足", existing_or_pending_position: "已有持仓或待执行计划，不能重复新开", candidate_market_evidence_unavailable: "候选的当前行情证据不足", sector_breadth_no_chase: "板块上涨家数不足，禁止追价", main_pool_breadth_unproven: "主线板块的上涨家数尚未被证实", numeric_gates_failed: "市场、板块与个股数值条件未同时达标", market_absolute_block: "市场环境禁止新开仓", shock_blocks_new_positions: "冲击状态禁止新开仓", new_positions_disabled: "当前禁止新增持仓", currency_boundary: "超出本轮美元账户边界", currency_blocked: "超出本轮美元账户边界", risk_budget_below_one_share: "剩余风险预算不足一股", position_budget_below_one_share: "剩余仓位额度不足一股", position_slots_exhausted: "可用持仓名额已用完", group_risk_budget_exhausted: "同组风险额度已用完", gross_budget_exhausted: "总仓位额度已用完", compliance_rejected: "既有合规条件未通过", portfolio_snapshot_missing: "缺少可核验的账户估值", valuation_date_mismatch: "账户与计划估值日期不一致", snapshot_hash_mismatch: "计划引用的账户版本不一致" };
  const rejectionName = (reason) => REJECTION_NAMES[reason] || reason;
  let dashboard = null;
  let range = "all";
  let historyExpanded = false;
  let requestPending = false;
  let lastAttemptAt = 0;

  const byId = (id) => document.getElementById(id);
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const money = (value) => finite(value) ? USD.format(value) : "—";
  const number = (value) => finite(value) ? NUMBER.format(value) : "—";
  const percent = (value, signed = false) => finite(value) ? `${signed && value > 0 ? "+" : ""}${PERCENT.format(Object.is(value, -0) ? 0 : value)}%` : "—";
  const validDate = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
  const dateTime = (value) => validDate(value) ? `${DATE_TIME.format(new Date(value))}（北京时间）` : "截至时间未提供";
  const dateOnly = (value) => validDate(value) ? DATE.format(new Date(value)) : "—";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function setMetric(id, text, numericValue) {
    const node = byId(id);
    node.textContent = text;
    node.classList.toggle("is-positive", finite(numericValue) && numericValue > 0);
    node.classList.toggle("is-negative", finite(numericValue) && numericValue < 0);
  }

  function emptyRow(targetId, columns, message) {
    const row = element("tr");
    const cell = element("td", "table-empty");
    cell.colSpan = columns;
    cell.append(element("span", "table-empty-message", message));
    row.append(cell);
    byId(targetId).replaceChildren(row);
  }

  function validateDashboard(data) {
    if (!data || data.schemaVersion !== 1 || !["awaiting_first_record", "active"].includes(data.status)) throw new Error("INVALID_SCHEMA");
    if (!["history", "records"].every((key) => Array.isArray(data[key]))) throw new Error("INVALID_HISTORY");
    if (data.research !== undefined) {
      if (!data.research || !Array.isArray(data.research.history)) throw new Error("INVALID_RESEARCH");
      for (const research of [...data.research.history, ...(data.research.latest === null ? [] : [data.research.latest])]) {
        if (!research || !validDate(research.asOf) || !validDate(research.dataAsOf) || research.executionMode !== "paper" || !["disabled", "enabled", "passthrough"].includes(research.modelMode) || !["completed", "completed_degraded"].includes(research.status) || !finite(research.universeSize) || !Array.isArray(research.decisions)) throw new Error("INVALID_RESEARCH_RECORD");
        validateDecisions(research.decisions);
      }
    }
    if (data.status === "awaiting_first_record") {
      if (data.latest !== null || data.baseline !== null || data.metrics !== null || data.history.length) throw new Error("INVALID_EMPTY_STATE");
      return data;
    }
    const latest = data.latest;
    if (!latest || !validDate(latest.asOf) || !latest.account || latest.account.currency !== "USD" || !finite(latest.account.nav) || latest.account.nav <= 0 || !finite(latest.account.cash)) throw new Error("INVALID_ACCOUNT");
    if (!data.baseline || !validDate(data.baseline.date) || !finite(data.baseline.nav) || data.baseline.nav <= 0 || !data.metrics || !["returnPct", "drawdownPct", "recordCount"].every((key) => finite(data.metrics[key]))) throw new Error("INVALID_METRICS");
    for (const key of ["positions", "trades", "decisions"]) if (!Array.isArray(latest[key])) throw new Error("INVALID_ENTRIES");
    for (const row of data.history) if (!validDate(row.asOf) || !["nav", "returnPct", "drawdownPct"].every((key) => finite(row[key]))) throw new Error("INVALID_HISTORY_ENTRY");
    for (const row of latest.positions) if (typeof row.symbol !== "string" || !["quantity", "avgCost", "price", "marketValue", "weight", "unrealizedPnl"].every((key) => finite(row[key])) || row.weight < 0 || row.weight > 1) throw new Error("INVALID_POSITION");
    for (const row of latest.trades) if (typeof row.symbol !== "string" || !["buy", "sell"].includes(row.side) || !validDate(row.executedAt) || !["quantity", "price", "fees"].every((key) => finite(row[key]))) throw new Error("INVALID_TRADE");
    validateDecisions(latest.decisions);
    return data;
  }

  function validateDecisions(decisions) {
    for (const row of decisions) if (typeof row.symbol !== "string" || !Object.hasOwn(ACTION_NAMES, row.action) || !validDate(row.decidedAt) || typeof row.thesis !== "string" || !finite(row.targetWeight) || row.targetWeight < 0 || row.targetWeight > 1) throw new Error("INVALID_DECISION");
  }

  const plainText = (value) => typeof value === "string" && value.trim().length > 0;
  const positiveInteger = (value) => Number.isSafeInteger(value) && value > 0;
  function sourceUrl(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }

  function calendarDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000-")) return false;
    const stamp = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value;
  }

  function marketEvidenceValid(record, decision) {
    if (!validDate(record.asOf) || !Array.isArray(decision.market_evidence) || decision.market_evidence.length !== 1) return false;
    const snapshot = decision.market_evidence[0];
    const fields = ["id", "symbol", "as_of", "close", "series_sha256"];
    if (!snapshot || Array.isArray(snapshot) || typeof snapshot !== "object"
      || Object.keys(snapshot).length !== fields.length || !fields.every((key) => Object.hasOwn(snapshot, key))
      || snapshot.symbol !== decision.symbol || !calendarDate(snapshot.as_of)
      || !finite(snapshot.close) || snapshot.close <= 0
      || typeof snapshot.series_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(snapshot.series_sha256) || /^0+$/.test(snapshot.series_sha256)) return false;
    const parts = Object.fromEntries(NEW_YORK_DATE.formatToParts(new Date(record.asOf)).map(({ type, value }) => [type, value]));
    const assessmentDate = `${parts.year}-${parts.month}-${parts.day}`;
    const expectedId = `md_${snapshot.symbol}_${snapshot.as_of.replaceAll("-", "")}_${snapshot.series_sha256.slice(0, 24)}`;
    return snapshot.as_of <= assessmentDate && snapshot.id === expectedId;
  }

  function modelQualified(record) {
    const model = record?.model;
    const stages = record?.schemaVersion === 3 ? RESEARCH_MODEL_STAGES : REQUIRED_MODEL_STAGES;
    return record?.modelMode === "enabled" && record.status === "completed"
      && model?.provider === "deepseek" && model.model === "deepseek-flash" && model.release === "DeepSeek-V4.1-Flash-1017"
      && model.required === true && model.all_required_completed === true && positiveInteger(model.call_count)
      && Array.isArray(model.response_ids) && model.response_ids.length === model.call_count && model.response_ids.every(plainText)
      && new Set(model.response_ids).size === model.call_count
      && Array.isArray(model.required_stages) && model.required_stages.length === stages.length
      && new Set(model.required_stages).size === model.required_stages.length
      && model.stage_counts && Object.keys(model.stage_counts).length === stages.length
      && stages.every((stage) => model.required_stages.includes(stage) && positiveInteger(model.stage_counts[stage]))
      && Object.values(model.stage_counts).reduce((sum, count) => sum + count, 0) === model.call_count
      && model.search_mode === "provider_native_search"
      && (record.schemaVersion === 3 ? model.research_contract === "tiger.short-term-research.v2" && settledBudgetValid(model) : model.search_count === 14);
  }

  const recordObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const strings = (value, maximum = 1000) => Array.isArray(value) && value.length <= maximum && value.every(plainText) && new Set(value).size === value.length;
  const nonnegativeInteger = (value) => Number.isSafeInteger(value) && value >= 0;
  function settledBudgetValid(model) {
    const budget = model.budget;
    if (!recordObject(budget)) return false;
    const current = budget.schema === "tiger.request-budget.v3" && budget.policy_id === "tiger.budget-policy.v3";
    const expanded = budget.schema === "tiger.request-budget.v4" && budget.policy_id === "tiger.budget-policy.v4";
    const versioned = current || expanded;
    const archived = budget.schema === "tiger.request-budget.v2" && !Object.hasOwn(budget, "policy_id");
    const limits = { max_chat_calls: expanded ? 160 : 40, max_tokens: expanded ? 20000000 : current ? 600000 : 300000, max_search_calls: 60,
      max_fetch_calls: 20, max_remote_requests: expanded ? 240 : 120, max_cost_usd: expanded ? 30 : 1, deadline_seconds: expanded ? 7200 : 2400 };
    if (expanded && (model.clean_output_policy_id !== "tiger.clean-output.compact-lists-regeneration.v1"
      || model.clean_output_codec_id !== "tiger.clean-compact-lists-wire.v1"
      || model.regeneration_policy_id !== "tiger.model-regeneration.clean-schema.v1"
      || !nonnegativeInteger(model.rejected_call_count) || model.rejected_call_count > 56
      || !positiveInteger(model.accepted_call_count) || model.accepted_call_count + model.rejected_call_count !== model.call_count
      || model.rejected_call_count > model.stage_counts["clean.llm_semantic"] - model.rejected_call_count)) return false;
    if ((!versioned && !archived) || !recordObject(budget.limits)
      || Object.keys(budget.limits).length !== Object.keys(limits).length
      || Object.entries(limits).some(([key, value]) => budget.limits[key] !== value) || budget.frozen !== false
      || budget.reserved_tokens !== 0 || budget.estimated_reserved_usd !== 0
      || !nonnegativeInteger(budget.actual_tokens) || budget.actual_tokens > limits.max_tokens
      || !finite(budget.estimated_charged_usd) || budget.estimated_charged_usd < 0 || budget.estimated_charged_usd > limits.max_cost_usd + 1e-9
      || model.call_count > limits.max_chat_calls || !nonnegativeInteger(model.search_count) || model.search_count < 14 || model.search_count > 60
      || !nonnegativeInteger(model.fetch_count) || model.fetch_count > 20
      || model.call_count + model.search_count + model.fetch_count > limits.max_remote_requests
      || !recordObject(budget.counts) || budget.counts.chat !== model.call_count || budget.counts.search !== model.search_count || budget.counts.fetch !== model.fetch_count
      || !Array.isArray(budget.records) || budget.records.length !== model.call_count + model.search_count + model.fetch_count) return false;
    const ids = new Set();
    const counts = { chat: 0, search: 0, fetch: 0 };
    for (const item of budget.records) {
      const reservation = item?.reservation;
      if (item?.status !== "settled" || !reservation || !plainText(reservation.call_id) || ids.has(reservation.call_id)
        || reservation.schema !== (expanded ? "tiger.request-budget-reservation.v4" : current ? "tiger.request-budget-reservation.v3" : "tiger.request-budget-reservation.v2")
        || (versioned ? reservation.policy_id !== budget.policy_id : Object.hasOwn(reservation, "policy_id"))
        || !Object.hasOwn(counts, reservation.kind)) return false;
      ids.add(reservation.call_id); counts[reservation.kind] += 1;
    }
    return Object.entries(counts).every(([key, value]) => value === budget.counts[key]);
  }

  function thesisValid(thesis, symbol, research, market) {
    if (!recordObject(thesis) || thesis.schema_version !== 2 || thesis.symbol !== symbol
      || !Object.hasOwn(STRATEGY_NAMES, thesis.strategy) || !Object.hasOwn(RESEARCH_STATUS, thesis.status)
      || !(thesis.issuer_name === null || plainText(thesis.issuer_name))
      || !recordObject(thesis.five_part) || Object.keys(thesis.five_part).length !== THESIS_PARTS.length
      || !strings(thesis.issuer_identity_evidence_ids, 8)
      || !["entry_triggers", "falsification", "risk_notes", "gaps"].every((key) => strings(thesis[key], 8))
      || !thesis.entry_triggers.length || !thesis.falsification.length || !thesis.risk_notes.length
      || thesis.status !== "sufficient" && !thesis.gaps.length
      || thesis.followup?.needed !== false || thesis.followup.question !== null || thesis.followup.fetch_source_id !== null
      || !["checked", "unknown"].includes(thesis.event_risk?.status) || !plainText(thesis.event_risk.text)
      || !strings(thesis.event_risk.source_ids, 8) || !Array.isArray(thesis.claims) || thesis.claims.length > 8) return false;
    const sources = new Map(research.source_index.map((source) => [source.id, source]));
    const facts = new Map(research.fact_evidence.map((fact) => [fact.id, fact]));
    const allIds = new Set([...sources.keys(), ...facts.keys(), market.id]);
    if ([...thesis.issuer_identity_evidence_ids, ...thesis.event_risk.source_ids].some((id) => !sources.has(id))) return false;
    if (thesis.issuer_identity_evidence_ids.some((id) => !Array.isArray(sources.get(id).target_symbols) || !sources.get(id).target_symbols.includes(symbol))) return false;
    for (const [key] of THESIS_PARTS) {
      const part = thesis.five_part[key];
      const expected = [...facts.values()].filter((row) => row.scope === key && row.symbol === symbol).map((row) => row.id);
      if (key === "stock") expected.push(market.id);
      if (!plainText(part?.text) || !strings(part.evidence_ids, 12) || !part.evidence_ids.length
        || part.evidence_ids.some((id) => !allIds.has(id)) || !part.evidence_ids.some((id) => expected.includes(id))) return false;
    }
    if (!thesis.five_part.stock.evidence_ids.includes(market.id)) return false;
    const claims = new Set();
    let primaryCurrent = false;
    for (const claim of thesis.claims) {
      const source = sources.get(claim?.source_id);
      if (!source || !plainText(claim.claim_id) || claims.has(claim.claim_id) || !plainText(claim.statement) || !plainText(claim.quote)
        || !["primary", "media", "commentary", "unknown"].includes(claim.evidence_kind) || !["current", "background", "unknown"].includes(claim.time_role)
        || !(claim.origin_id === null || sources.has(claim.origin_id))) return false;
      claims.add(claim.claim_id);
      let publication = source.published_at;
      if (claim.date_source_id !== undefined && claim.date_source_id !== null) {
        const dateSource = sources.get(claim.date_source_id);
        if (!dateSource || dateSource.url !== source.url || source.search_source_id !== dateSource.id) return false;
        publication = dateSource.published_at;
      }
      if (claim.time_role === "current") {
        const age = Date.parse(research.assessment.admitted_at_utc) - Date.parse(publication);
        if (!validDate(publication) || age < 0 || age > 7 * 86400000) return false;
        if (claim.evidence_kind === "primary" && claim.origin_id === source.id && source.content_complete
          && thesis.issuer_identity_evidence_ids.includes(source.id)) primaryCurrent = true;
      }
    }
    return thesis.strategy !== "news_event_v2" || thesis.status !== "sufficient" || (primaryCurrent && plainText(thesis.issuer_name));
  }

  function upgradeResearchValid(record) {
    const research = record.candidate_research;
    const lifecycle = record.opportunity_lifecycle;
    const evaluation = record.business_evaluation;
    const snapshot = record.portfolio_snapshot;
    if (!research || research.schema !== "tiger.candidate-research.v2" || !recordObject(research.assessment)
      || !calendarDate(research.assessment.run_date) || research.assessment.market_timezone !== "America/New_York" || !validDate(research.assessment.admitted_at_utc)
      || Date.parse(research.assessment.admitted_at_utc) > Date.parse(record.asOf)
      || !recordObject(research.plan) || !Array.isArray(research.plan.targets) || research.plan.targets.length > 20
      || !strings(research.plan.requested_symbols) || !Array.isArray(research.plan.deferred)
      || !Array.isArray(research.theses) || research.theses.length !== research.plan.targets.length
      || !recordObject(research.coverage) || !recordObject(research.counters) || !plainText(research.coverage_summary)
      || !["search", "fetch", "chat"].every((key) => nonnegativeInteger(research.counters[key]))
      || research.counters.search + 14 !== record.model.search_count || research.counters.fetch !== record.model.fetch_count
      || research.counters.chat !== record.model.stage_counts["research.candidates"]
      || !Array.isArray(research.source_index) || !Array.isArray(research.fact_evidence) || !Array.isArray(research.market_evidence)
      || !["daily", "premarket", "intraday"].includes(record.workflow_phase) || record.execution_policy !== "whitepaper-execution-v2"
      || !snapshot || snapshot.schema !== "tiger.portfolio-snapshot.v2" || !calendarDate(snapshot.known_at) || !finite(snapshot.nav) || snapshot.nav <= 0
      || record.current_portfolio_snapshot !== undefined && (!recordObject(record.current_portfolio_snapshot)
        || record.current_portfolio_snapshot.schema !== "tiger.portfolio-snapshot.v2"
        || !calendarDate(record.current_portfolio_snapshot.known_at) || !finite(record.current_portfolio_snapshot.nav) || record.current_portfolio_snapshot.nav <= 0)
      || !lifecycle || lifecycle.schema !== "tiger.opportunity-ledger.v2" || lifecycle.account_kind !== "paper" || !nonnegativeInteger(lifecycle.revision)
      || !["opportunities", "intents", "events", "cohort_outcomes"].every((key) => Array.isArray(lifecycle[key]))
      || !evaluation || evaluation.schema !== "tiger.business-evaluation.v2" || evaluation.run_id !== record.runId || !validDate(evaluation.as_of)
      || evaluation.account?.account_kind !== "paper" || !recordObject(evaluation.funnel) || !recordObject(evaluation.opportunities)
      || !["n_closed", "n_positions", "n_pending"].every((key) => nonnegativeInteger(evaluation.account[key]))
      || !["pnl_net", "cash"].every((key) => finite(evaluation.account[key]))
      || !["matured_labels", "pending_labels", "unverifiable_or_refused", "unlabeled"].every((key) => nonnegativeInteger(evaluation.opportunities[key]))) return false;
    const sourceIds = new Set();
    for (const source of research.source_index) {
      if (!source || !plainText(source.id) || sourceIds.has(source.id) || !plainText(source.title) || !sourceUrl(source.url)
        || !(source.published_at === null || validDate(source.published_at)) || !finite(source.fetched_at) || source.fetched_at < 0 || source.fetched_at * 1000 > Date.parse(record.asOf)
        || !nonnegativeInteger(source.full_content_characters) || !nonnegativeInteger(source.presented_content_characters)
        || source.presented_content_characters > source.full_content_characters || typeof source.content_complete !== "boolean"
        || source.content_complete !== (source.presented_content_characters === source.full_content_characters)
        || Object.hasOwn(source, "content") || !Array.isArray(source.presentations)) return false;
      sourceIds.add(source.id);
    }
    const market = new Map(research.market_evidence.map((row) => [row.symbol, row]));
    const targetSymbols = research.plan.targets.map((row) => row.symbol);
    if (research.plan.targets.some((row) => !plainText(row.symbol) || !strings(row.roles, 3) || !row.roles.length || row.roles.some((role) => !["approved", "watch", "holding"].includes(role)))
      || new Set(targetSymbols).size !== targetSymbols.length || new Set(research.theses.map((row) => row.symbol)).size !== research.theses.length
      || !["requested_symbols", "selected_symbols", "researched_symbols", "sufficient_symbols", "insufficient_symbols", "contradictory_symbols"].every((key) => strings(research.coverage[key]))
      || !Array.isArray(research.coverage.deferred)
      || research.coverage.researched_symbols.length !== targetSymbols.length
      || targetSymbols.some((symbol) => !research.coverage.researched_symbols.includes(symbol))) return false;
    for (const thesis of research.theses) if (!targetSymbols.includes(thesis.symbol) || !market.has(thesis.symbol)
      || !marketEvidenceValid(record, { symbol: thesis.symbol, market_evidence: [market.get(thesis.symbol)] })
      || !thesisValid(thesis, thesis.symbol, research, market.get(thesis.symbol))) return false;
    return lifecycle.opportunities.every((row) => plainText(row?.ticker) && Object.hasOwn(LIFECYCLE_NAMES, row.state) && recordObject(row.levels)
      && ["entry", "stop", "protect"].every((key) => row.levels[key] === null || finite(row.levels[key]) && row.levels[key] > 0)
      && nonnegativeInteger(row.filled_shares) && strings(row.rejection_reasons));
  }

  function structuredDecisionsValid(record) {
    if (!plainText(record?.summary) || !Array.isArray(record.executionDecisions)) return false;
    const upgrade = record.schemaVersion === 3;
    if (upgrade && !upgradeResearchValid(record)) return false;
    const seen = new Set();
    let totalWeight = 0;
    for (const decision of record.executionDecisions) {
      if (!decision || typeof decision.symbol !== "string" || !/^[A-Z0-9][A-Z0-9.^=-]{0,19}$/.test(decision.symbol)
        || seen.has(decision.symbol) || !["buy", "watch"].includes(decision.action)
        || !finite(decision.target_weight) || decision.target_weight < 0 || decision.target_weight > 1
        || !["action_summary", "why_this", "why_now", "entry_condition", "invalidation", "risk_note", "valid_for"].every((key) => plainText(decision[key]))
        || !["entry_price", "stop_price", "shares", "risk_usd", "time_stop_days", "reference_capital_usd"].every((key) => decision[key] === null || (finite(decision[key]) && decision[key] >= 0))
        || !marketEvidenceValid(record, decision)
        || !Array.isArray(decision.sources) || decision.sources.length > (upgrade ? 16 : 8)
        || new Set(decision.sources.map((source) => source?.id)).size !== decision.sources.length
        || decision.sources.some((source) => !source || !plainText(source.id) || source.id.startsWith("md_") || !plainText(source.title) || !sourceUrl(source.url))) return false;
      if (decision.action === "watch" && (decision.target_weight !== 0
        || ["entry_price", "stop_price", "shares", "risk_usd", "time_stop_days"].some((key) => decision[key] !== null))) return false;
      if (upgrade && !thesisValid(decision.thesis, decision.symbol, record.candidate_research, decision.market_evidence[0])) return false;
      if (decision.action === "buy" && ((!upgrade && !decision.sources.length) || upgrade && decision.thesis.status !== "sufficient" || !(decision.target_weight > 0) || !(decision.entry_price > 0)
        || !(decision.stop_price > 0) || !(decision.stop_price < decision.entry_price)
        || !positiveInteger(decision.shares) || !(decision.risk_usd > 0) || !(decision.reference_capital_usd > 0))) return false;
      seen.add(decision.symbol);
      totalWeight += decision.target_weight;
    }
    return totalWeight <= 1.000001;
  }

  function expiryTimestamp(value) {
    // Only an explicit ISO expiry can be interpreted here. Session descriptions
    // remain verbatim; the display never derives an expiry from a trading phrase.
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
      && validDate(value) ? Date.parse(value) : null;
  }
  const expiredAt = (value) => expiryTimestamp(value) !== null && expiryTimestamp(value) < Date.now();

  function renderChartState(title, description, loading = false) {
    const container = byId("performance-chart");
    container.classList.toggle("has-chart", false);
    const state = element("div", "chart-state");
    state.append(element(loading ? "span" : "div", loading ? "loading-ring" : "chart-empty-symbol", loading ? undefined : "∅"));
    state.firstChild.setAttribute("aria-hidden", "true");
    state.append(element("p", "", title));
    if (description) state.append(element("span", "", description));
    container.replaceChildren(state);
    container.setAttribute("aria-label", `${title}。${description || ""}`);
  }

  function renderAwaiting(error = false) {
    for (const id of ["metric-nav", "metric-return", "metric-drawdown"]) setMetric(id, "—");
    byId("metric-days").replaceChildren(document.createTextNode("—"), element("small", "", "天"));
    byId("nav-note").textContent = "来自真实账户快照";
    byId("return-note").textContent = "自首笔真实记录起";
    byId("drawdown-note").textContent = "记录期内净值高点至低点";
    byId("days-note").textContent = "第一天，从真实开始";
    byId("baseline-date").textContent = error ? "基准日期暂不可用" : "基准日期待建立";
    byId("stock-weight").textContent = "—";
    byId("cash-weight").textContent = "—";
    byId("position-count").textContent = error ? "持仓数据暂不可用" : "持仓记录待入账";
    byId("allocation-bar").replaceChildren();
    byId("allocation-bar").setAttribute("aria-label", "资产配置暂无数据");
    byId("history-toggle").hidden = true;
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = true; });
    byId("account-status").textContent = error ? "账簿暂不可用" : "待首笔真实记录";
    byId("account-status").className = `state-label${error ? " is-error" : ""}`;
    byId("as-of").textContent = error ? "当前无法获取账户数据" : "真实记录入账后开始计时";
    byId("chart-subtitle").textContent = "每一个数据点，对应一笔真实账户记录。";
    renderChartState(error ? "账簿暂时未能读取" : "首笔真实账户记录待入账", error ? "连接恢复后重新读取，请稍后重试。" : "基准尚未建立，业绩从第一笔真实记录开始。" );
    emptyRow("history-body", 5, error ? "历史账页暂不可用" : "第一张账页，等待真实记录。");
    emptyRow("positions-body", 6, error ? "持仓数据暂不可用" : "首笔真实账户快照入账后公开持仓。");
    byId("history-count").textContent = error ? "/ —" : "/ 0 笔";
    byId("positions-as-of").textContent = error ? "当前无法获取账户快照" : "截至时间待更新";
    byId("decisions-list").replaceChildren(element("p", "journal-empty", error ? "决策记录暂不可用。" : "暂无已公开的研究决策。"));
    byId("trades-list").replaceChildren(element("p", "journal-empty", error ? "成交记录暂不可用。" : "暂无已入账的真实成交。"));
  }

  function renderActive(data) {
    const { latest, metrics, baseline } = data;
    const start = Date.parse(`${dateOnly(baseline.date)}T00:00:00Z`);
    const end = Date.parse(`${dateOnly(latest.asOf)}T00:00:00Z`);
    const days = Math.max(1, Math.floor((end - start) / 86400000) + 1);
    byId("account-status").textContent = data.history.length === 1 ? "真实账户 · 基准已建立" : "真实账户 · 持续记账";
    byId("account-status").className = "state-label is-active";
    byId("as-of").textContent = `截至 ${dateTime(latest.asOf)}`;
    setMetric("metric-nav", money(latest.account.nav));
    setMetric("metric-return", percent(metrics.returnPct, true), metrics.returnPct);
    const maxDrawdown = data.history.reduce((maximum, record) => Math.min(maximum, record.drawdownPct), 0);
    setMetric("metric-drawdown", percent(maxDrawdown), maxDrawdown);
    byId("metric-days").replaceChildren(document.createTextNode(number(days)), element("small", "", "天"));
    byId("days-note").textContent = `始于 ${dateOnly(baseline.date)}`;
    byId("nav-note").textContent = `账户记账币种 · ${latest.account.currency}`;
    byId("return-note").textContent = data.history.length === 1 ? "首日建立基准，收益从此累计" : "已剔除记录期内净出入金影响";
    byId("drawdown-note").textContent = data.history.length === 1 ? "首日基准，尚无跨日回撤" : "逐期收益净值的最大回落";
    byId("baseline-date").textContent = `基准 ${dateOnly(baseline.date)} · ${money(baseline.nav)}`;
    byId("history-count").textContent = `/ ${number(metrics.recordCount)} 笔`;
    byId("positions-as-of").textContent = dateTime(latest.asOf);
    byId("position-count").textContent = latest.positions.length ? `${latest.positions.length} 只股票持仓` : "当前账户未持有股票";
    byId("methodology-text").textContent = "首笔真实账户记录建立基准。相邻快照先从本期净资产中扣除本期净入金，计算单期收益，再逐期复合；资金流采用期末口径，不把入金算成盈利。";
    byId("methodology-detail").textContent = "收益包含已记录的费用；最大回撤按剔除资金流影响后的收益净值计算。样本不足时不展示年化收益或胜率。";
    renderAllocation(latest);
    renderPositions(latest.positions);
    renderTrades(latest.trades);
    renderHistory();
    renderChart();
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = false; });
  }

  function renderAllocation(latest) {
    const positionValue = latest.positions.reduce((total, row) => total + row.marketValue, 0);
    const stockWeight = positionValue / latest.account.nav;
    const cashWeight = latest.account.cash / latest.account.nav;
    byId("stock-weight").textContent = percent(stockWeight * 100);
    byId("cash-weight").textContent = percent(cashWeight * 100);
    const bar = byId("allocation-bar");
    const svg = svgElement("svg", { viewBox: "0 0 1000 5", preserveAspectRatio: "none", "aria-hidden": "true" });
    bar.replaceChildren(svg);
    bar.setAttribute("aria-label", `股票持仓 ${percent(stockWeight * 100)}，现金 ${percent(cashWeight * 100)}`);
    let offset = 0;
    for (const [weight, color] of [[stockWeight, "#fa6a32"], [cashWeight, "#abb59d"]]) {
      if (weight <= 0) continue;
      const segmentWidth = Math.max(0, Math.min(weight, 1)) * 1000;
      svg.append(svgElement("rect", { x: offset, y: 0, width: segmentWidth, height: 5, fill: color }));
      offset += segmentWidth;
    }
  }

  function renderPositions(positions) {
    if (!positions.length) {
      emptyRow("positions-body", 6, "当前账户未持有股票。现金同样是账户配置的一部分。" );
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...positions].sort((a, b) => b.weight - a.weight)) {
      const row = element("tr");
      const asset = element("td");
      asset.append(element("span", "asset-symbol", item.symbol), element("span", "asset-name", item.name || item.symbol));
      const price = element("td", "numeric", money(item.price));
      price.append(element("span", "price-subvalue", `成本 ${money(item.avgCost)}`));
      const weightCell = element("td", "numeric");
      const weight = element("span", "weight-cell");
      const track = svgElement("svg", { class: "weight-track", viewBox: "0 0 42 3", preserveAspectRatio: "none" });
      track.setAttribute("aria-hidden", "true");
      track.append(svgElement("rect", { width: Math.min(42, Math.max(0, item.weight * 42)), height: 3, fill: "#fa6a32" }));
      weight.append(track, document.createTextNode(percent(item.weight * 100)));
      weightCell.append(weight);
      row.append(asset, element("td", "numeric", number(item.quantity)), price, element("td", "numeric", money(item.marketValue)), weightCell, element("td", `numeric${item.unrealizedPnl > 0 ? " is-positive" : item.unrealizedPnl < 0 ? " is-negative" : ""}`, `${item.unrealizedPnl > 0 ? "+" : ""}${money(item.unrealizedPnl)}`));
      fragment.append(row);
    }
    byId("positions-body").replaceChildren(fragment);
  }

  function renderHistory() {
    const history = [...dashboard.history].sort((a, b) => Date.parse(b.asOf) - Date.parse(a.asOf));
    const rows = historyExpanded ? history : history.slice(0, 7);
    const dayCounts = new Map();
    for (const record of history) { const day = dateOnly(record.asOf); dayCounts.set(day, (dayCounts.get(day) || 0) + 1); }
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
      const row = element("tr");
      const date = element("td", "table-date", dateOnly(item.asOf));
      const sameDayCount = dayCounts.get(dateOnly(item.asOf));
      if (sameDayCount > 1) date.append(element("small", "", `${DATE_TIME.format(new Date(item.asOf)).split(" ").pop()} 更新`));
      const status = element("td", "record-column");
      status.append(element("span", "record-label", item === history[history.length - 1] ? "首笔基准" : "账户记录"));
      row.append(date, element("td", "numeric", money(item.nav)), element("td", `numeric${item.returnPct > 0 ? " is-positive" : item.returnPct < 0 ? " is-negative" : ""}`, percent(item.returnPct, true)), element("td", "numeric", percent(item.drawdownPct)), status);
      fragment.append(row);
    }
    byId("history-body").replaceChildren(fragment);
    const toggle = byId("history-toggle");
    toggle.hidden = history.length <= 7;
    toggle.textContent = historyExpanded ? "收起历史记录 ↑" : `展开全部 ${history.length} 笔记录 ↓`;
    toggle.setAttribute("aria-expanded", String(historyExpanded));
  }

  function renderResearch(data) {
    const research = data.research?.latest;
    const stamp = byId("research-stamp");
    const qualified = modelQualified(research) && structuredDecisionsValid(research);
    renderHistoricalResearch(data, qualified ? research : null);
    if (!qualified) {
      renderInstructionState(research ? "unqualified" : "pending",
        research ? "等待满足要求的模型指令" : "等待新一轮模型指令",
        research ? "已有运行记录未满足本轮必需模型与完整指令的核验条件，已收进历史记录。当前不据此发出新的落单指令。" : "实际模型完成分析并通过核验后，交易指令将在这里公开。没有完整产出时不以规则结果替代。" );
      return;
    }

    const status = byId("instruction-status");
    const allExpired = research.executionDecisions.length > 0 && research.executionDecisions.every((item) => expiredAt(item.valid_for));
    status.textContent = allExpired ? "历史指令 · 已过有效期" : "必需模型与搜索 · 已核验";
    status.className = `instruction-status ${allExpired ? "is-pending" : "is-verified"}`;
    const proof = byId("model-proof");
    proof.hidden = false;
    proof.replaceChildren();
    const identity = element("div", "model-identity");
    identity.append(element("span", "model-proof-label", "本轮实际模型"), element("strong", "", "DeepSeek"), element("span", "model-release", research.model.release), element("span", "model-api-name", research.model.model));
    const facts = element("div", "model-proof-facts");
    for (const [value, label] of [[research.model.call_count, "次真实模型调用"], [research.model.required_stages.length, "个必需模型阶段完成"], [research.model.search_count, "次模型原生搜索"]]) {
      const fact = element("div");
      fact.append(element("strong", "", number(value)), element("span", "", label));
      facts.append(fact);
    }
    proof.append(identity, facts);
    if (research.schemaVersion === 3) proof.append(element("p", "model-budget-note", `原生原页读取 ${number(research.model.fetch_count)} 次 · 本轮模型用量 ${number(research.model.budget.actual_tokens)} tokens · 已结算，无悬而未决的预算请求`));
    stamp.hidden = false;
    stamp.replaceChildren(element("span", "", `指令生成 ${dateTime(research.asOf)}`), element("span", "", `行情截至 ${dateOnly(research.dataAsOf)} · 本轮扫描 ${number(research.universeSize)} 只标的`));
    const summary = byId("instruction-summary");
    summary.hidden = false;
    summary.replaceChildren(element("p", "instruction-summary-label", allExpired ? "当时的总体指令" : "本轮总体指令"), element("p", "instruction-summary-text", research.summary));
    if (allExpired) summary.append(element("p", "instruction-summary-note", "以下指令已过其明确有效期，保留当时的判断用于复核；等待新的已核验模型指令。"));
    renderExecutionDecisions(research);
    renderUpgradePanels(research);
  }

  function renderInstructionState(kind, title, description) {
    const status = byId("instruction-status");
    status.textContent = kind === "error" ? "本轮指令暂不可核验" : kind === "unqualified" ? "历史运行不作本轮指令" : "等待必需模型完成";
    status.className = `instruction-status ${kind === "error" ? "is-error" : "is-pending"}`;
    for (const id of ["model-proof", "research-stamp", "instruction-summary", "research-v2-panels"]) {
      byId(id).hidden = true;
      byId(id).replaceChildren();
    }
    const empty = element("div", `instruction-empty${kind === "error" ? " is-error" : ""}`);
    empty.append(element("strong", "", title), element("p", "", description));
    byId("decisions-list").replaceChildren(empty);
  }

  function addInstructionReason(container, label, value, className = "") {
    const section = element("div", `instruction-reason${className ? ` ${className}` : ""}`);
    section.append(element("h5", "", label), element("p", "", value));
    container.append(section);
  }

  function renderExecutionDecisions(research) {
    const list = byId("decisions-list");
    if (!research.executionDecisions.length) {
      const empty = element("div", "instruction-empty");
      empty.append(element("strong", "", "本轮没有新增单票落单指令"), element("p", "", "执行团队按上方总体指令行动。账户已有持仓与已回报成交，分别查看持仓记录和实际执行回报。"));
      list.replaceChildren(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of research.executionDecisions) {
      const expired = expiredAt(item.valid_for);
      const article = element("article", `instruction-entry${expired ? " is-expired" : ""}`);
      const header = element("div", "instruction-entry-head");
      const identity = element("div", "instruction-symbol");
      identity.append(element("span", `action-badge action-${item.action}`, ACTION_NAMES[item.action]), element("h4", "", item.symbol));
      header.append(identity, element("span", "instruction-target", `目标仓位 ${percent(item.target_weight * 100)}`));
      article.append(header, element("p", "instruction-headline", item.action_summary));
      if (research.schemaVersion === 3) article.append(thesisBadges(item.thesis));
      if (expired) article.append(element("p", "instruction-expired-note", "已过明确有效期 · 等待新指令"));

      const reasons = element("div", "instruction-reasons");
      addInstructionReason(reasons, "为什么选它", item.why_this);
      addInstructionReason(reasons, "为什么现在", item.why_now);
      article.append(reasons);

      const market = item.market_evidence[0];
      const provenance = element("div", "instruction-market-evidence");
      provenance.append(element("h5", "", "行情依据"), element("p", "instruction-market-facts", `${market.symbol} · ${market.as_of} 交易日 · 收盘 ${MARKET_PRICE.format(market.close)}`));
      provenance.append(element("p", "instruction-market-note", "模型本轮引用的已冻结行情快照，非实时价格。"));
      provenance.append(element("p", "instruction-news-note", research.schemaVersion === 3
        ? item.sources.length ? `公开资料引用 ${item.sources.length} 项 · 发布日期与原始归属逐项披露` : "本条没有公司新闻引用 · 技术与资金依据不等于公司事件已经排除"
        : item.sources.length ? `公司新闻引用 ${item.sources.length} 篇 · 可展开下方来源查看` : "本条未引用公司新闻"));
      article.append(provenance);

      const metrics = element("dl", `instruction-metrics${item.action === "watch" ? " is-observation" : ""}`);
      for (const [label, value] of [["入场参考", money(item.entry_price)], ["认错线 / 止损", money(item.stop_price)], ["计划股数", item.shares === null ? "—" : `${number(item.shares)} 股`]]) {
        const metric = element("div");
        metric.append(element("dt", "", label), element("dd", "", value));
        metrics.append(metric);
      }
      article.append(metrics);
      addInstructionReason(article, "执行办法", item.entry_condition, "instruction-execution");
      addInstructionReason(article, "何时撤销 / 离场", item.invalidation, "instruction-invalidation");
      addInstructionReason(article, "主要风险", item.risk_note, "instruction-risk");
      if (research.schemaVersion === 3) article.append(renderThesis(item.thesis, research.candidate_research, "展开五段依据、事件排雷与反证"));

      const validity = element("div", "instruction-validity");
      validity.append(element("span", "", "有效范围"), element("p", "", expiryTimestamp(item.valid_for) === null ? item.valid_for : `截至 ${dateTime(item.valid_for)}`));
      article.append(validity);
      const evidence = element("details", "instruction-evidence");
      const evidenceSummary = element("summary", "", "查看来源与核验记录");
      evidence.append(evidenceSummary);
      const numeric = element("p", "instruction-numeric-note", `计划风险 ${money(item.risk_usd)} · 计划资金基准 ${money(item.reference_capital_usd)} · 时间纪律 ${item.time_stop_days === null ? "—" : `${number(item.time_stop_days)} 个交易日`}`);
      evidence.append(numeric);
      if (item.action === "watch") evidence.append(element("p", "instruction-numeric-note", "本项为观察指令；未提供下单价格或股数的字段保持空白。0% 是目标配置，不代替实际持仓。"));
      else evidence.append(element("p", "instruction-numeric-note", "以上为已核验计划值，成交以团队执行回报为准；跳空、滑点与费用可能使实际损失超过计划风险。"));
      if (item.sources.length) {
        const sources = element("ol", "instruction-sources");
        for (const source of item.sources) {
          const row = element("li");
          const link = element("a", "", source.title);
          link.href = sourceUrl(source.url); link.target = "_blank"; link.rel = "noopener noreferrer";
          row.append(link, element("span", "", validDate(source.published_at) ? ` · ${dateOnly(source.published_at)}` : " · 发布日期未提供"));
          sources.append(row);
        }
        evidence.append(sources);
      }
      const meta = element("div", "instruction-identifiers");
      meta.append(element("span", "", `行情快照 ${market.id}`), element("span", "", `完整行情序列 SHA256 ${market.series_sha256}`), element("span", "", `运行 ${research.runId}`), element("span", "", `原始有效范围 ${item.valid_for}`), element("span", "", `原始回执 SHA256 ${research.receiptHash}`));
      evidence.append(meta);
      article.append(evidence);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
  }

  function thesisBadges(thesis) {
    const badges = element("div", "thesis-badges");
    badges.append(element("span", "strategy-badge", STRATEGY_NAMES[thesis.strategy]),
      element("span", `research-status status-${thesis.status}`, RESEARCH_STATUS[thesis.status]));
    return badges;
  }

  function textList(container, title, values, className = "") {
    const section = element("div", `thesis-list ${className}`);
    section.append(element("h5", "", title));
    const list = element("ul");
    for (const value of values) list.append(element("li", "", value));
    section.append(list); container.append(section);
  }

  function renderThesis(thesis, research, title) {
    const details = element("details", "five-part-thesis");
    details.append(element("summary", "", title));
    const parts = element("div", "thesis-parts");
    for (const [key, label] of THESIS_PARTS) {
      const part = element("section", "thesis-part");
      part.append(element("h5", "", label), element("p", "", thesis.five_part[key].text));
      const refs = element("span", "thesis-reference", `依据 ${thesis.five_part[key].evidence_ids.join(" / ")}`);
      part.append(refs); parts.append(part);
    }
    details.append(parts);
    const event = element("div", `event-risk ${thesis.event_risk.status === "unknown" ? "is-unknown" : ""}`);
    event.append(element("h5", "", thesis.event_risk.status === "unknown" ? "事件风险仍有未知" : "本轮事件检查"), element("p", "", thesis.event_risk.text));
    details.append(event);
    textList(details, "执行前要看到什么", thesis.entry_triggers);
    textList(details, "出现什么就认错", thesis.falsification, "thesis-falsification");
    textList(details, "仍需承担的风险", thesis.risk_notes);
    if (thesis.gaps.length) textList(details, "尚未解决的缺口", thesis.gaps, "thesis-gaps");
    if (thesis.claims.length) {
      const claims = element("div", "thesis-claims");
      claims.append(element("h5", "", "事实判断与资料归属"));
      const kinds = { primary: "原始材料", media: "媒体报道", commentary: "评论或观点", unknown: "性质未知" };
      const times = { current: "本轮时效内", background: "背景资料", unknown: "时效未知" };
      for (const claim of thesis.claims) {
        const row = element("div", "thesis-claim");
        const source = research.source_index.find((item) => item.id === claim.source_id);
        row.append(element("p", "", claim.statement), element("span", "claim-attribution", `${kinds[claim.evidence_kind]} · ${times[claim.time_role]} · ${claim.origin_id === null ? "原始出处未核实" : `原始出处 ${claim.origin_id}`}`));
        if (source) {
          const link = element("a", "claim-source", source.title);
          link.href = sourceUrl(source.url); link.target = "_blank"; link.rel = "noopener noreferrer";
          row.append(link, element("span", "claim-time", source.published_at === null ? "发布日期未知" : `发布 ${dateTime(source.published_at)}`));
          if (claim.date_source_id) row.append(element("span", "claim-time", `发布时间取自同网址的搜索记录 ${claim.date_source_id}，原页读取本身未提供日期。`));
        }
        claims.append(row);
      }
      details.append(claims);
    }
    return details;
  }

  function panelHeading(index, title, description) {
    const heading = element("div", "research-panel-heading");
    heading.append(element("p", "section-index", index), element("h3", "", title), element("p", "", description));
    return heading;
  }

  function valueGrid(values, className = "") {
    const grid = element("dl", `research-value-grid ${className}`);
    for (const [label, value] of values) {
      const cell = element("div"); cell.append(element("dt", "", label), element("dd", "", value)); grid.append(cell);
    }
    return grid;
  }

  function renderUpgradePanels(record) {
    const container = byId("research-v2-panels");
    container.replaceChildren(); container.hidden = record.schemaVersion !== 3;
    if (container.hidden) return;
    const research = record.candidate_research;
    const coverage = research.coverage;
    const overview = element("section", "candidate-research-panel");
    overview.append(panelHeading("RESEARCH / 候选与持仓", "选中之前，也把没有选中的理由留下。", "候选与已有模拟持仓逐项研究；研究充分与数值获准分别核验。技术与资金型机会不统一要求公司利好新闻，新闻事件型须有及时且可归属的原始材料。"));
    overview.append(valueGrid([
      ["纳入研究请求", `${number(coverage.requested_symbols.length)} 只`],
      ["本轮实际研究", `${number(coverage.researched_symbols.length)} 只`],
      ["证据不足 / 冲突", `${number(coverage.insufficient_symbols.length + coverage.contradictory_symbols.length)} 只`],
      ["留待后续研究", `${number(coverage.deferred.length)} 只`],
    ]));
    overview.append(element("p", "research-coverage-copy", research.coverage_summary));
    overview.append(element("p", "research-input-scope", `本阶段实际搜索 ${number(research.counters.search)} 次、原页读取 ${number(research.counters.fetch)} 次、模型分析 ${number(research.counters.chat)} 次。模型使用当前资金和风险边界、当前持仓事实与所列资料；历史平仓和账本操作保留哈希引用，未作为完整历史交给模型阅读。`));
    const candidates = element("div", "candidate-thesis-grid");
    for (const thesis of research.theses) {
      const target = research.plan.targets.find((item) => item.symbol === thesis.symbol);
      const card = element("article", "candidate-thesis-card");
      const head = element("div", "candidate-thesis-head");
      head.append(element("h4", "", thesis.symbol), element("span", "", target.roles.includes("holding") ? "含已有模拟持仓研究" : "候选研究"));
      card.append(head, thesisBadges(thesis));
      if (thesis.issuer_name) card.append(element("p", "candidate-issuer", thesis.issuer_name));
      const stockText = Array.from(thesis.five_part.stock.text);
      if (stockText.length > 220) card.append(element("p", "candidate-preview-note", "个股段落节选 · 完整五段内容见展开"));
      card.append(element("p", "candidate-stock-thesis", stockText.length > 220 ? `${stockText.slice(0, 220).join("")}…` : thesis.five_part.stock.text));
      if (thesis.gaps.length) card.append(element("p", "candidate-visible-gap", `尚有缺口：${thesis.gaps.join("；")}`));
      card.append(element("p", "candidate-event-note", thesis.event_risk.status === "unknown" ? "事件风险有未核实项，不能解读为没有事件。" : "已完成本轮事件检查，具体范围见展开内容。"));
      card.append(renderThesis(thesis, research, "展开完整五段研究与反证"));
      candidates.append(card);
    }
    if (!research.theses.length) candidates.append(element("p", "research-panel-empty", "本轮没有待研究的候选或模拟持仓。模型的总体判断仍保留在上方。"));
    overview.append(candidates);
    if (coverage.deferred.length) {
      const deferred = element("details", "research-deferred");
      deferred.append(element("summary", "", `查看 ${coverage.deferred.length} 只未纳入本轮的标的`));
      for (const row of coverage.deferred) deferred.append(element("p", "", `${row.symbol} · ${row.reason === "candidate_limit" ? "达到本轮候选研究上限，未完成研究，不能据此买入" : row.reason}`));
      overview.append(deferred);
    }
    overview.append(renderSourceCoverage(research));
    container.append(overview, renderOpportunityPanel(record), renderBusinessPanel(record));
  }

  function renderSourceCoverage(research) {
    const details = element("details", "research-source-coverage");
    details.append(element("summary", "", "查看资料时间、原页与实际阅读范围"),
      element("p", "research-source-note", "来源数量不等于质量或独立确认。搜索正文、原页读取与发布日期分别记录；部分展示或未展示正文会明确列出。引用的模型判断保留原始出处与时间限定。"));
    for (const source of research.source_index) {
      const row = element("article", "source-coverage-entry");
      const link = element("a", "", source.title); link.href = sourceUrl(source.url); link.target = "_blank"; link.rel = "noopener noreferrer";
      row.append(link, element("p", "", `${source.source === "deepseek_fetch" ? "原页读取" : "原生搜索资料"} · ${source.published_at === null ? "发布日期未知" : `发布 ${dateTime(source.published_at)}`} · 获取 ${dateTime(new Date(source.fetched_at * 1000).toISOString())}`));
      row.append(element("p", "source-coverage-status", source.presentations.length
        ? `本任务各研究轮次实际展示的最长正文为 ${number(source.presented_content_characters)} / ${number(source.full_content_characters)} 字符${source.content_complete ? "，覆盖本条已获取正文" : "，属于局部内容"}。各标的累计已读范围如下，不代表最终模型重新通读全文。`
        : "已获取，但本任务各研究轮次均未展示正文；不能作为已读原文。"));
      for (const presentation of source.presentations) row.append(element("p", "source-presentation", `${presentation.symbol}：${number(presentation.presented_content_characters)} 字符 · ${presentation.content_complete ? "已获取正文完整展示" : "部分展示"}`));
      row.append(element("span", "source-identity", `来源 ${source.id}`));
      details.append(row);
    }
    if (!research.source_index.length) details.append(element("p", "research-panel-empty", "本轮定向检索没有返回可用公司正文；这不是“没有公司事件”的结论。"));
    return details;
  }

  function renderOpportunityPanel(record) {
    const lifecycle = record.opportunity_lifecycle;
    const panel = element("section", "opportunity-panel");
    panel.append(panelHeading("FOLLOW-THROUGH / 机会后续", "研究之后，继续跟到失效或结束。", "以下是独立模拟账户的机会状态。触发观察条件会请求新一轮风险复核；待执行意向仍须校验新报价和资金，不能当作已成交。真实账户持仓与成交仍以上方账簿和下方团队回报为准。"));
    const stateCounts = Object.entries(lifecycle.opportunities.reduce((counts, row) => { counts[row.state] = (counts[row.state] || 0) + 1; return counts; }, {}));
    panel.append(valueGrid(stateCounts.map(([key, value]) => [LIFECYCLE_NAMES[key], `${number(value)} 只`]), "opportunity-state-counts"));
    const rows = element("div", "opportunity-cards");
    for (const item of lifecycle.opportunities) {
      const card = element("article", `opportunity-entry state-${item.state.toLowerCase()}`);
      const head = element("div", "opportunity-head");
      head.append(element("h4", "", item.ticker), element("span", "", LIFECYCLE_NAMES[item.state]));
      card.append(head, valueGrid([["原结构参考", money(item.levels.entry)], ["当前认错线", money(item.levels.stop)], ["保护触发参考", money(item.levels.protect)]]));
      card.append(element("p", "opportunity-account-fact", `模拟已持有数量 ${number(item.filled_shares)} 股 · 研究日 ${calendarDate(item.signal_date) ? item.signal_date : "未提供"}`));
      if (item.rejection_reasons.length) card.append(element("p", "opportunity-refusals", `尚未放行的原因：${item.rejection_reasons.map(rejectionName).join("；")}`));
      if (item.state === "INTENT_PENDING") card.append(element("p", "opportunity-recheck", "执行前仍需新报价、当前资金与完整数值条件复核。此状态不代表真实买单或成交。"));
      if (item.state === "EXIT_PENDING") card.append(element("p", "opportunity-recheck", "模拟退出条件已触发；真实账户是否执行，等待团队真实回报。"));
      if (item.pending_protection_stop !== null && finite(item.pending_protection_stop)) card.append(element("p", "opportunity-recheck", `待确认的保护线 ${money(item.pending_protection_stop)}，不据此改写已记录止损。`));
      const meta = element("details", "opportunity-meta"); meta.append(element("summary", "", "查看来源与有效范围"));
      meta.append(element("p", "", `观察更新 ${dateTime(item.observed_at)} · 原研究有效范围 ${dateTime(item.expires_at)}`), element("p", "", `原运行 ${item.source_run_id || "由模拟账户事实建立"}`), element("p", "", `机会 ${item.opportunity_id}`));
      card.append(meta); rows.append(card);
    }
    if (!lifecycle.opportunities.length) rows.append(element("p", "research-panel-empty", "当前没有已登记的机会。零机会不会触发强制交易。"));
    panel.append(rows);
    const intents = lifecycle.intents.filter((item) => item.status === "pending_execution_recheck");
    if (intents.length) panel.append(element("p", "pending-intents-note", `${number(intents.length)} 条意向等待执行前复核。没有真实执行回报前，不进入公开真实账户持仓。`));
    const scope = lifecycle.scope;
    panel.append(element("p", "research-scope-note", recordObject(scope)
      ? `模拟机会账版本 ${number(lifecycle.revision)} · 展示当前运行与所有仍在跟踪的机会，共 ${number(scope.included_opportunities)} / ${number(scope.total_opportunities)} 项。另有 ${number(scope.historical_opportunities_omitted)} 项历史终态保留在私密核验账，不在此重复展示。`
      : `模拟机会账版本 ${number(lifecycle.revision)} · ${plainText(scope) ? scope : "展示当前运行与仍在跟踪的机会"}。`));
    return panel;
  }

  function renderBusinessPanel(record) {
    const report = record.business_evaluation;
    const account = report.account;
    const opportunities = report.opportunities;
    const panel = element("section", "business-panel");
    panel.append(panelHeading("EVALUATION / 模拟执行与业务评估", "看流程有没有走通，也看结果是否成熟。", "这组统计来自独立模拟执行与已登记候选队列。连续处理几次任务不等于多天收益；逐个候选的反事实结果不能相加为组合盈利，也不能代表全市场机会召回。"));
    panel.append(valueGrid([
      ["模拟已结束持仓", `${number(account.n_closed)} 笔`],
      ["模拟已结算净盈亏", account.n_closed > 0 ? money(account.pnl_net) : "待形成结束样本"],
      ["模拟每笔风险收益", account.expectancy_r === null ? "待成熟样本" : `${number(account.expectancy_r)} R`],
      ["候选结果已成熟", `${number(opportunities.matured_labels)} 项`],
    ], "evaluation-metrics"));
    const snapshot = record.current_portfolio_snapshot || record.portfolio_snapshot;
    const capitalBases = [...new Set(record.executionDecisions.map((row) => row.reference_capital_usd).filter((value) => finite(value) && value > 0))];
    panel.append(valueGrid([["模拟估值净资产", money(snapshot.nav)], ["模拟账户现金", money(snapshot.cash)],
      ["模拟待用现金预留", money(snapshot.reserved_cash)], ["指令资金基准", capitalBases.length === 1 ? money(capitalBases[0]) : capitalBases.length ? "按各条计划列示" : "本轮未提供单票计划"]]));
    panel.append(element("p", "research-scope-note", `模拟估值日期 ${snapshot.known_at}。估值快照包含模拟账户的持仓与预留；仓位计划采用各条指令所列的冻结资金基准。这些金额不替代公开真实账户的净资产。`));
    const funnelNames = { universe: "可用股票池", data_covered: "取得行情", hard_filter_pass: "硬条件通过", scanned_selected: "进入排序分析", tss_candidates: "结构候选", numeric_approved: "数值获准", final_buy: "最终买入指令", final_watch: "最终观察指令", issuer_researched: "定向研究完成", held: "模拟持有", pending: "模拟待处理" };
    const funnel = element("details", "evaluation-funnel"); funnel.append(element("summary", "", "查看从股票池到执行的逐层数量"));
    funnel.append(valueGrid(Object.entries(funnelNames).map(([key, label]) => [label, report.funnel[key] === null || report.funnel[key] === undefined ? "未提供" : number(report.funnel[key])])));
    const universe = record.universe_coverage;
    if (recordObject(universe)) {
      const description = typeof universe.disclosure === "string" ? universe.disclosure : typeof universe.note === "string" ? universe.note : "股票池与行情覆盖以本轮实际统计为准，未取得数据的部分不计作完整分析。";
      funnel.append(element("p", "research-scope-note", description));
    }
    panel.append(funnel);
    panel.append(valueGrid([
      ["所登记可交易机会捕获率", opportunities.capture_rate === null ? "结果尚未成熟" : percent(opportunities.capture_rate * 100)],
      ["待成熟 / 未形成标签", `${number(opportunities.pending_labels)} / ${number(opportunities.unlabeled)} 项`],
      ["无法核验 / 被拒绝", `${number(opportunities.unverifiable_or_refused)} 项`],
      ["模拟资金使用率", account.capital_utilization === null ? "估值尚未提供" : percent(account.capital_utilization * 100)],
    ]));
    const reasons = report.rejections?.reason_counts || {};
    if (Object.keys(reasons).length) {
      const rejections = element("details", "evaluation-rejections");
      rejections.append(element("summary", "", "查看未入选原因（同一标的可有多个原因）"));
      for (const [reason, count] of Object.entries(reasons)) rejections.append(element("p", "", `${rejectionName(reason)} · ${number(count)} 次${REJECTION_NAMES[reason] ? `（${reason}）` : ""}`));
      panel.append(rejections);
    }
    const latency = report.latency;
    if (recordObject(latency)) panel.append(element("p", "evaluation-latency", `本轮已记录报价观察 ${number(latency.quote_attempts)} 次、可用 ${number(latency.usable_quotes)} 次。触发至复核 ${number(latency.trigger_to_requalification_samples)} 组${latency.trigger_to_requalification_mean_s === null ? "，尚不能计算平均响应时间" : `，平均 ${number(latency.trigger_to_requalification_mean_s)} 秒`}。`));
    panel.append(element("p", "evaluation-caveat", "盈利能力尚未被建立。没有成熟结果时保留“待观察”，不把空样本的胜率或捕获率写成 0%。模拟结果不替代真人执行回报，也不承诺未来收益。"));
    panel.append(element("p", "research-scope-note", `评估截至 ${dateTime(report.as_of)} · 模拟估值 ${calendarDate(account.valuation_date) ? account.valuation_date : "尚未形成"} · 处理时段 ${({ daily: "日终", premarket: "盘前", intraday: "盘中" })[record.workflow_phase]}`));
    return panel;
  }

  function renderHistoricalResearch(data, qualifiedCurrent = null, connectionFailed = false) {
    const history = [...(data.research?.history || [])];
    if (data.research?.latest && !history.some((item) => item.runId === data.research.latest.runId)) history.unshift(data.research.latest);
    const rows = history.filter((item) => item && item.runId !== qualifiedCurrent?.runId);
    const container = byId("legacy-research");
    container.hidden = rows.length === 0;
    byId("legacy-research-title").textContent = `历史指令与运行记录（${rows.length}）`;
    const body = byId("legacy-research-body");
    body.replaceChildren();
    for (const record of rows) {
      const entry = element("details", "historical-entry");
      const qualification = modelQualified(record) && structuredDecisionsValid(record);
      const label = connectionFailed && record.runId === data.research?.latest?.runId ? "上次读取记录 · 当前连接未核验" : qualification ? "历史模型指令 · 以当时有效范围为准" : "历史运行 · 未满足模型必需条件";
      entry.append(element("summary", "", `${dateTime(record.asOf)} · ${label}`));
      entry.append(element("p", "historical-summary", typeof record.summary === "string" ? record.summary : "原记录未提供总体说明。"));
      if (!qualification) entry.append(element("p", "historical-qualification", record.modelMode === "disabled" ? "这条历史运行没有启用模型，不作为本轮交易指令。" : "这条历史运行没有完整的必需模型执行凭据，不作为本轮交易指令。"));
      for (const decision of record.decisions || []) {
        const note = element("div", "historical-decision");
        note.append(element("strong", "", `${decision.symbol} · 当时目标 ${percent(decision.targetWeight * 100)}`), element("p", "", decision.thesis));
        entry.append(note);
      }
      const metadata = element("div", "instruction-identifiers");
      metadata.append(element("span", "", `运行 ${record.runId || "未提供"}`), element("span", "", `行情截至 ${dateOnly(record.dataAsOf)}`));
      entry.append(metadata);
      body.append(entry);
    }
  }

  function renderTrades(trades) {
    const list = byId("trades-list");
    if (!trades.length) {
      list.replaceChildren(element("p", "journal-empty", "截至账户快照，暂无已入账的真实成交。新的团队执行回报待更新。"));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...trades].sort((a, b) => Date.parse(b.executedAt) - Date.parse(a.executedAt))) {
      const article = element("article", "trade-entry");
      const side = element("span", `trade-side${item.side === "sell" ? " sell" : ""}`, item.side === "buy" ? "买" : "卖");
      const main = element("div", "trade-main");
      const time = element("time", "", dateTime(item.executedAt));
      time.dateTime = item.executedAt;
      const detail = element("small");
      detail.append(time);
      main.append(element("strong", "", item.symbol), detail);
      const value = element("div", "trade-value", `${number(item.quantity)} 股 × ${money(item.price)}`);
      value.append(element("small", "", `费用 ${money(item.fees)}`));
      article.append(side, main, value);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
  }

  function renderChart() {
    let history = [...dashboard.history].sort((a, b) => Date.parse(a.asOf) - Date.parse(b.asOf));
    if (range !== "all" && history.length) {
      const threshold = Date.parse(history[history.length - 1].asOf) - (Number(range) - 1) * 86400000;
      history = history.filter((row) => Date.parse(row.asOf) >= threshold);
    }
    document.querySelectorAll("[data-range]").forEach((button) => {
      const selected = button.dataset.range === range;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    if (!history.length) {
      renderChartState("该时段暂无账户记录", "调整时间范围查看已公开的账页。" );
      return;
    }
    const container = byId("performance-chart");
    const mobile = window.matchMedia("(max-width: 740px)").matches;
    const width = mobile ? Math.max(270, container.clientWidth || 300) : 1100;
    const height = mobile ? 235 : 270;
    const padding = { top: 30, right: mobile ? 12 : 24, bottom: 34, left: mobile ? 40 : 57 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const svg = svgElement("svg", { class: "chart-svg", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    let min = history.reduce((value, point) => Math.min(value, point.returnPct), 0);
    let max = history.reduce((value, point) => Math.max(value, point.returnPct), 0);
    const spread = max - min;
    const margin = spread < .02 ? 1 : spread * .22;
    min -= margin;
    max += margin;
    const y = (value) => padding.top + (max - value) / (max - min) * plotHeight;
    const startTime = Date.parse(history[0].asOf);
    const endTime = Date.parse(history[history.length - 1].asOf);
    const x = (value) => history.length === 1 || endTime === startTime ? padding.left + plotWidth / 2 : padding.left + (Date.parse(value) - startTime) / (endTime - startTime) * plotWidth;
    for (let i = 0; i < 4; i += 1) {
      const value = max - (max - min) * i / 3;
      const position = y(value);
      svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: position, y2: position, stroke: "#e9ece1", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
      svg.append(svgElement("text", { x: padding.left - 12, y: position + 3, class: "chart-label", "text-anchor": "end" }, `${value.toFixed(1)}%`));
    }
    svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: y(0), y2: y(0), stroke: "#c6cebb", "stroke-dasharray": "3 5", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
    const tooltip = element("div", "chart-tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    if (history.length > 1) {
      const path = history.map((point, index) => `${index === 0 ? "M" : "L"}${x(point.asOf).toFixed(2)},${y(point.returnPct).toFixed(2)}`).join(" ");
      svg.append(svgElement("path", { d: path, fill: "none", stroke: "#fa6a32", "stroke-width": 2.3, "stroke-linecap": "round", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
    }
    for (const point of history) {
      const dot = svgElement("g");
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: history.length === 1 ? 6 : 3.5, fill: "#fa6a32", stroke: "#fffefa", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: 14, fill: "transparent" }));
      const label = `${dateOnly(point.asOf)} · 净资产 ${money(point.nav)} · 累计收益 ${percent(point.returnPct, true)}`;
      dot.append(svgElement("title", {}, label));
      dot.addEventListener("pointerenter", () => { tooltip.textContent = label; });
      dot.addEventListener("pointerleave", () => { tooltip.textContent = ""; });
      svg.append(dot);
    }
    if (history.length === 1) {
      const point = history[0];
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) - 24, class: "chart-point-label", "text-anchor": "middle" }, dashboard.history.length === 1 ? "第 1 天 · 基准建立" : "该时段 1 笔记录"));
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) + 29, class: "chart-point-note", "text-anchor": "middle" }, "长期结果，从这一点开始。"));
    }
    const labelIndexes = history.length === 1 ? [0] : [0, ...(history.length > 3 ? [Math.floor((history.length - 1) / 2)] : []), history.length - 1];
    for (const index of labelIndexes) {
      const point = history[index];
      svg.append(svgElement("text", { x: x(point.asOf), y: height - 8, class: "chart-label", "text-anchor": history.length === 1 ? "middle" : index === 0 ? "start" : index === history.length - 1 ? "end" : "middle" }, dateOnly(point.asOf)));
    }
    container.replaceChildren(svg, tooltip);
    container.classList.add("has-chart");
    container.setAttribute("aria-label", history.length === 1 ? `${dateOnly(history[0].asOf)}，${dashboard.history.length === 1 ? "首笔真实记录建立基准" : "该时段唯一记录"}，账户净资产 ${money(history[0].nav)}，累计收益 ${percent(history[0].returnPct)}。详细数据见下方每日账页。` : `${dateOnly(history[0].asOf)} 至 ${dateOnly(history[history.length - 1].asOf)}，共 ${history.length} 笔真实记录，最新累计收益 ${percent(history[history.length - 1].returnPct, true)}。详细数据见下方每日账页。`);
    byId("chart-subtitle").textContent = dashboard.history.length === 1 ? "首日只有一个真实数据点，持续记账后形成收益曲线。" : `按已公开账户快照连接，共 ${history.length} 笔记录；并非实时行情。`;
  }

  function showConnectionNotice(message) {
    const notice = byId("connection-notice");
    const button = element("button", "retry-button", "重新读取");
    button.type = "button";
    button.addEventListener("click", () => { void loadDashboard(); });
    notice.replaceChildren(element("span", "", message), button);
    notice.hidden = false;
  }

  async function loadDashboard() {
    if (requestPending) return;
    requestPending = true;
    lastAttemptAt = Date.now();
    byId("performance").setAttribute("aria-busy", "true");
    const retry = document.querySelector(".retry-button");
    if (retry) { retry.disabled = true; retry.textContent = "读取中…"; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/public/dashboard", { method: "GET", headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP_${response.status}`);
      const data = validateDashboard(await response.json());
      dashboard = data;
      byId("connection-notice").hidden = true;
      if (data.status === "active") renderActive(data); else renderAwaiting();
      renderResearch(data);
    } catch (error) {
      if (!dashboard) renderAwaiting(true);
      renderInstructionState("error", "当前无法核验最新模型指令", "本轮未取得完整的最新记录，当前不展示可执行的新指令。请重新读取；历史记录的原生成时间和有效范围保持不变。" );
      if (dashboard) renderHistoricalResearch(dashboard, null, true);
      showConnectionNotice(dashboard?.status === "active" ? "最新账簿暂时无法读取，当前保留上次成功读取的记录，请以标注的截至时间为准。" : "账户数据暂时无法读取。页面不展示未经核实的业绩，请重新读取。" );
    } finally {
      window.clearTimeout(timeout);
      requestPending = false;
      byId("performance").setAttribute("aria-busy", "false");
    }
  }

  byId("copyright-year").textContent = String(new Date().getFullYear());
  byId("history-toggle").addEventListener("click", () => { historyExpanded = !historyExpanded; renderHistory(); });
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => { range = button.dataset.range; renderChart(); });
  });
  window.addEventListener("online", () => { void loadDashboard(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastAttemptAt >= 300000) void loadDashboard(); });
  window.setInterval(() => { if (!document.hidden) void loadDashboard(); }, 300000);
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => { if (dashboard?.status === "active") renderChart(); }, 150);
  });
  void loadDashboard();
})();
