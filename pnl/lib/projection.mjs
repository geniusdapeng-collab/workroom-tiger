export const round = value => Math.round((value + Number.EPSILON) * 1e8) / 1e8;
export function publicRecord(entry) {
  const r = entry.record;
  return {
    sequence: entry.sequence, hash: entry.hash, asOf: r.asOf,
    account: { currency: r.account.currency, nav: r.account.nav, cash: r.account.cash },
    positions: r.positions.map(p => ({ symbol: p.symbol, name: p.name, quantity: p.quantity, avgCost: p.avgCost, price: p.price, marketValue: p.marketValue, weight: round(p.marketValue / r.account.nav), unrealizedPnl: round(p.marketValue - p.quantity * p.avgCost) })),
    trades: r.trades.map(t => ({ id: t.id, executedAt: t.executedAt, symbol: t.symbol, side: t.side, quantity: t.quantity, price: t.price, fees: t.fees })),
    decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })),
  };
}
export function publicResearch(entry) {
  const r = entry.record;
  const output = { schemaVersion: r.schemaVersion, sequence: entry.sequence, hash: entry.hash, asOf: r.asOf, runId: r.runId, receiptHash: r.receiptHash, dataAsOf: r.dataAsOf, executionMode: r.executionMode, modelMode: r.modelMode, status: r.status, universeSize: r.universeSize, summary: r.summary, decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })) };
  if (r.schemaVersion >= 2) {
    const m = r.model;
    output.model = { provider: m.provider, model: m.model, release: m.release, required: m.required, response_ids: [...m.response_ids], call_count: m.call_count, required_stages: [...m.required_stages], stage_counts: { ...m.stage_counts }, search_count: m.search_count, search_mode: m.search_mode, all_required_completed: m.all_required_completed };
    output.executionDecisions = r.executionDecisions.map(d => ({ symbol: d.symbol, action: d.action, target_weight: d.target_weight, action_summary: d.action_summary, why_this: d.why_this, why_now: d.why_now, entry_condition: d.entry_condition, invalidation: d.invalidation, risk_note: d.risk_note, sources: d.sources.map(s => ({ id: s.id, title: s.title, url: s.url, published_at: s.published_at })), market_evidence: d.market_evidence.map(m => ({ id: m.id, symbol: m.symbol, as_of: m.as_of, close: m.close, series_sha256: m.series_sha256 })), entry_price: d.entry_price, stop_price: d.stop_price, shares: d.shares, risk_usd: d.risk_usd, time_stop_days: d.time_stop_days, valid_for: d.valid_for, reference_capital_usd: d.reference_capital_usd }));
  }
  if (r.schemaVersion === 3) {
    output.model = structuredClone(r.model);
    output.executionDecisions = output.executionDecisions.map((row, index) => ({ ...row,
      sources: r.executionDecisions[index].sources.map(source => pick(source, ['id', 'title', 'url', 'published_at', 'fetched_at', 'method', 'content_sha256'])),
      thesis: publicThesis(r.executionDecisions[index].thesis) }));
    Object.assign(output, publicResearchExtras(r, r.runId));
  }
  return output;
}
export function dashboard(entries, now = new Date().toISOString(), researchEntries = []) {
  const result = { schemaVersion: 1, status: entries.length ? 'active' : 'awaiting_first_record', generatedAt: now, baseline: null, latest: null, metrics: null, history: [], records: [], research: { latest: researchEntries.length ? publicResearch(researchEntries.at(-1)) : null, history: researchEntries.slice().reverse().map(publicResearch) } };
  if (!entries.length) return result;
  let unitValue = 1; let peak = 1;
  for (let i = 0; i < entries.length; i++) {
    const r = entries[i].record;
    if (i) unitValue *= (r.account.nav - r.netExternalFlow) / entries[i - 1].record.account.nav;
    peak = Math.max(peak, unitValue);
    result.history.push({ asOf: r.asOf, nav: r.account.nav, returnPct: round((unitValue - 1) * 100), drawdownPct: round((unitValue / peak - 1) * 100) });
  }
  const latest = publicRecord(entries.at(-1)); const last = result.history.at(-1);
  result.baseline = { date: entries[0].record.asOf, nav: entries[0].record.account.nav };
  result.latest = latest;
  result.metrics = { returnPct: last.returnPct, drawdownPct: last.drawdownPct, unrealizedPnl: round(latest.positions.reduce((sum, p) => sum + p.unrealizedPnl, 0)), positionCount: latest.positions.length, recordCount: entries.length, netExternalFlow: round(entries.slice(1).reduce((sum, entry) => sum + entry.record.netExternalFlow, 0)) };
  result.records = entries.slice(-20).reverse().map(publicRecord);
  return result;
}

const pick = (value, fields) => Object.fromEntries(fields.filter(key => Object.hasOwn(value ?? {}, key)).map(key => [key, structuredClone(value[key])]));
const PRIVATE_RESEARCH_KEYS = new Set(['content', 'raw', 'raw_response', 'model_rounds', 'model_output', 'reasoning_content', 'chain_of_thought', 'messages', 'api_key', 'apikey', 'authorization', 'password', 'credential', 'token', 'secret', 'source_account_state', 'cohort_inputs']);
function publicTree(value) {
  if (Array.isArray(value)) return value.map(publicTree);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([key]) => !PRIVATE_RESEARCH_KEYS.has(key.toLowerCase())).map(([key, child]) => [key, publicTree(child)]));
  return value;
}
export function publicThesis(thesis) {
  const output = pick(thesis, ['schema_version', 'symbol', 'strategy', 'status', 'issuer_name', 'issuer_identity_evidence_ids', 'entry_triggers', 'falsification', 'risk_notes', 'gaps']);
  output.five_part = Object.fromEntries(['market', 'sector', 'chain', 'stock', 'plan'].map(scope => [scope, pick(thesis?.five_part?.[scope], ['text', 'evidence_ids'])]));
  output.claims = (thesis?.claims ?? []).map(row => pick(row, ['claim_id', 'statement', 'source_id', 'quote', 'origin_id', 'evidence_kind', 'time_role', 'date_source_id']));
  output.event_risk = pick(thesis?.event_risk, ['status', 'text', 'source_ids']);
  output.followup = pick(thesis?.followup, ['needed', 'question', 'fetch_source_id']);
  return output;
}
export function publicResearchExtras(summary, runId) {
  const research = summary.candidate_research;
  const candidate = pick(research, ['schema', 'assessment', 'coverage', 'counters', 'coverage_summary', 'fact_evidence', 'market_evidence']);
  candidate.plan = pick(research?.plan, ['targets', 'requested_symbols', 'deferred', 'limits', 'plan_sha256']);
  candidate.theses = (research?.theses ?? []).map(publicThesis);
  candidate.source_index = (research?.source_index ?? []).map(row => ({ ...pick(row, ['id', 'title', 'url', 'published_at', 'fetched_at', 'source', 'full_content_characters', 'presented_content_characters', 'content_complete', 'original_content_sha256', 'presented_content_sha256', 'search_source_id', 'target_symbols']),
    presentations: (row.presentations ?? []).map(shown => pick(shown, ['symbol', 'presented_content_characters', 'content_complete', 'presented_content_sha256'])) }));
  const original = summary.opportunity_lifecycle;
  const active = new Set(['WATCH', 'QUALIFIED', 'INTENT_PENDING', 'HELD', 'EXIT_PENDING']);
  const opportunities = (original?.opportunities ?? []).filter(row => row.source_run_id === runId || active.has(row.state));
  const ids = new Set(opportunities.map(row => row.opportunity_id));
  const lifecycle = { ...pick(original, ['schema', 'account_kind', 'revision']), scope: structuredClone(original?.scope ?? 'current_run_and_active'),
    opportunities: opportunities.map(row => pick(row, ['opportunity_id', 'ticker', 'source_run_id', 'signal_date', 'observed_at', 'expires_at', 'state', 'qualified_at_signal', 'decision', 'levels', 'initial_stop', 'rejection_reasons', 'source_ids', 'entry_template', 'plan', 'filled_shares', 'pending_protection_stop', 'account_plan_id', 'entry_date', 'exit_date', 'closed_fact'])),
    intents: (original?.intents ?? []).filter(row => ids.has(row.opportunity_id)).map(publicTree),
    events: (original?.events ?? []).filter(row => ids.has(row.opportunity_id)).map(publicTree),
    cohort_outcomes: (original?.cohort_outcomes ?? []).filter(row => ids.has(row.opportunity_id)).map(publicTree) };
  return publicTree({ candidate_research: candidate, opportunity_lifecycle: lifecycle,
    business_evaluation: summary.business_evaluation, portfolio_snapshot: summary.current_portfolio_snapshot ?? summary.portfolio_snapshot,
    execution_policy: summary.execution_policy, workflow_phase: summary.workflow_phase,
    universe_coverage: summary.universe_coverage });
}
