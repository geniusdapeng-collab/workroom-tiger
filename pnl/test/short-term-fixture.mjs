import { digest } from '../lib/schema.mjs';
import { instructionFixture, modelResearchFixture } from './model-fixture.mjs';

// Synthetic contract fixtures only. No runtime account, source or paid call.
export const UPGRADE_PROFILE = 'us-public-model-required-paper-v2';
export const V2_STAGES = ['clean.llm_semantic', 'tech.sentiment', 'tech.risk', 'sector.narrative', 'research.candidates', 'decision.debate'];
export const LIMITS = { max_chat_calls: 40, max_tokens: 300000, max_search_calls: 60, max_fetch_calls: 20, max_remote_requests: 120, max_cost_usd: 1, deadline_seconds: 2400 };
export const PRICING = { observed_date: '2026-10-27', source: 'https://api-docs.deepseek.com/quick_start/pricing/', model: 'deepseek-flash', token_upper_usd_per_million: 1.2, search_upper_usd_per_request: .00075, fetch_upper_usd_per_request: .00075, basis: 'Peak output price applied to all tokens; every native request charged conservatively.', invoice: false };
export function modelV2Fixture({ searchCount = 14, fetchCount = 0 } = {}) {
  const records = [], counts = { chat: 0, search: 0, fetch: 0 };
  let actual = 0;
  for (const kind of [...V2_STAGES.map(() => 'chat'), ...Array(searchCount).fill('search'), ...Array(fetchCount).fill('fetch')]) {
    const reservation = { schema: 'tiger.request-budget-reservation.v2', call_id: (records.length + 1).toString(16).padStart(32, '0'), kind, sequence: records.length + 1,
      request_sha256: digest(`SYNTHETIC ${kind} ${records.length}`), input_token_upper_bound: kind === 'chat' ? 1000 : 0, output_token_upper_bound: kind === 'chat' ? 100 : 0,
      token_upper_bound: kind === 'chat' ? 1100 : 0, estimated_cost_upper_usd: kind === 'chat' ? .00132 : .00075,
      counts_before: { ...counts }, actual_tokens_before: actual, reserved_tokens_before: 0 };
    records.push({ reservation, status: 'settled', usage: kind === 'chat' ? { prompt_tokens: 90, completion_tokens: 10, total_tokens: 100 } : null });
    counts[kind]++; if (kind === 'chat') actual += 100;
  }
  return { provider: 'deepseek', model: 'deepseek-flash', release: 'DeepSeek-V4.1-Flash-1017', required: true,
    research_contract: 'tiger.short-term-research.v2', response_ids: V2_STAGES.map((_, i) => `SYNTHETIC-response-${i}`), call_count: V2_STAGES.length,
    required_stages: [...V2_STAGES], stage_counts: Object.fromEntries(V2_STAGES.map(stage => [stage, 1])), search_count: searchCount, fetch_count: fetchCount,
    search_mode: 'provider_native_search', all_required_completed: true,
    budget: { schema: 'tiger.request-budget.v2', limits: { ...LIMITS }, pricing: { ...PRICING }, counts,
      actual_tokens: actual, reserved_tokens: 0, estimated_charged_usd: actual * 1.2 / 1e6 + (searchCount + fetchCount) * .00075,
      estimated_reserved_usd: 0, frozen: false, records } };
}
export function thesisFixture(symbol = 'AAPL') {
  const fivePart = Object.fromEntries(['market', 'sector', 'chain', 'stock', 'plan'].map(scope => [scope, { text: `TEST ONLY ${scope} evidence`, evidence_ids: [`fx_TEST_${scope}`] }]));
  return { schema_version: 2, symbol, strategy: 'technical_flow_v2', status: 'sufficient', issuer_name: null,
    issuer_identity_evidence_ids: [], five_part: fivePart, claims: [], event_risk: { status: 'unknown', text: 'TEST ONLY event status unverified', source_ids: [] },
    entry_triggers: ['TEST ONLY confirmed structure'], falsification: ['TEST ONLY structural stop'], risk_notes: ['TEST ONLY gap risk'], gaps: [],
    followup: { needed: false, question: null, fetch_source_id: null } };
}
export function researchV3Fixture(overrides = {}) {
  const instruction = instructionFixture({ action: 'buy', sources: [], time_stop_days: 7 });
  instruction.thesis = thesisFixture(instruction.symbol);
  instruction.thesis.five_part.stock.evidence_ids.push(instruction.market_evidence[0].id);
  const base = modelResearchFixture({ executionDecisions: [instruction] });
  const facts = ['market', 'sector', 'chain', 'stock', 'plan'].map(scope => ({ id: `fx_TEST_${scope}`, scope, symbol: instruction.symbol, source_stage: `TEST.${scope}`, known_at: base.asOf, facts: { synthetic_test_only: true } }));
  return { ...base, schemaVersion: 3, model: modelV2Fixture(),
    candidate_research: { schema: 'tiger.candidate-research.v2', assessment: { run_date: '2026-01-03', market_timezone: 'America/New_York', admitted_at_utc: base.asOf },
      plan: { targets: [{ symbol: instruction.symbol, roles: ['numeric_approved'], numeric_approved: true }], requested_symbols: [instruction.symbol], deferred: [], limits: {}, plan_sha256: digest('SYNTHETIC PLAN ONLY') },
      theses: [structuredClone(instruction.thesis)], coverage: { requested_symbols: [instruction.symbol], researched_symbols: [instruction.symbol] }, counters: {}, coverage_summary: 'TEST ONLY coverage',
      fact_evidence: facts, market_evidence: instruction.market_evidence, source_index: [] },
    opportunity_lifecycle: { schema: 'tiger.opportunity-ledger.v2', account_kind: 'paper', revision: 1,
      scope: 'current_run_and_active', opportunities: [], intents: [], events: [], cohort_outcomes: [] },
    business_evaluation: { schema: 'tiger.business-evaluation.v2', run_id: base.runId, as_of: base.asOf,
      funnel: {}, funnel_unavailable: {}, funnel_evidence: {}, rejections: { multi_label: true, candidate_count: 0, reason_counts: {}, candidates: [] },
      research: {}, lifecycle: {}, latency: {}, account: { account_kind: 'paper', n_closed: 0, win_rate: null, expectancy_r: null, profitability_established: false },
      opportunities: { capture_rate: null, unavailable_reason: 'NO_MATURED_INDEPENDENT_LABELS' }, evaluation_limits: ['SYNTHETIC TEST ONLY'] },
    portfolio_snapshot: { schema: 'tiger.portfolio-snapshot.v2', known_at: '2026-01-02', nav: 100000, cash: 100000, gross: 0, reserved_cash: 0, reserved_risk: 0, held_risk: 0,
      group_risk: {}, committed_symbols: [], held: {}, reservations: {}, state_hash: digest('SYNTHETIC SNAPSHOT ONLY') },
    execution_policy: 'whitepaper-execution-v2', workflow_phase: 'daily', universe_coverage: { requested: 'full', actual: 'full', count: 10 }, ...overrides };
}
