// Synthetic bounds and public contract tests only: no provider or business run.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { resolve } from 'node:path';
import { validateModelV2 } from '../../bundles/trading/capabilities/required-v2-contract.mjs';
import { validateEnvelope, LIMITS as CLIENT_LIMITS } from '../../bundles/trading/capabilities/workflow-client.mjs';
import { modelV2Fixture, researchV3Fixture } from './short-term-fixture.mjs';
import { result, RUN_ID } from '../../scripts/tiger-agent-test-support.mjs';
import { validateResearch } from '../lib/schema.mjs';
import { publicResearch } from '../lib/projection.mjs';
const V4 = 'tiger.budget-policy.v4';
const LIMITS = { max_chat_calls: 160, max_tokens: 20000000, max_search_calls: 60, max_fetch_calls: 20,
  max_remote_requests: 240, max_cost_usd: 30, deadline_seconds: 7200 };
function model({ cleanAccepted = 56, rejected = 56, issuer = 14, firstUsage = 1000000 } = {}) {
  const value = modelV2Fixture(), sample = value.budget.records[0].reservation;
  const accepted = cleanAccepted + issuer + 4, total = accepted + rejected;
  const counts = { chat: 0, search: 0, fetch: 0 }, records = [];
  let actual = 0;
  for (const kind of [...Array(total).fill('chat'), ...Array(14).fill('search')]) {
    const index = records.length, usage = kind === 'chat' ? (index === 0 ? firstUsage : 100) : 0;
    const reservation = { ...sample, schema: 'tiger.request-budget-reservation.v4', policy_id: V4,
      call_id: (index + 1).toString(16).padStart(32, '0'), kind, sequence: index + 1,
      input_token_upper_bound: kind === 'chat' ? usage : 0, output_token_upper_bound: kind === 'chat' ? 100 : 0,
      token_upper_bound: kind === 'chat' ? usage + 100 : 0,
      estimated_cost_upper_usd: kind === 'chat' ? (usage + 100) * 1.2 / 1e6 : .00075,
      counts_before: { ...counts }, actual_tokens_before: actual, reserved_tokens_before: 0 };
    records.push({ reservation, status: 'settled', usage: kind === 'chat'
      ? { prompt_tokens: usage - 10, completion_tokens: 10, total_tokens: usage } : null });
    counts[kind]++; actual += usage;
  }
  Object.assign(value, { clean_output_policy_id: 'tiger.clean-output.compact-lists-regeneration.v1',
    clean_output_codec_id: 'tiger.clean-compact-lists-wire.v1', regeneration_policy_id: 'tiger.model-regeneration.clean-schema.v1',
    accepted_call_count: accepted, rejected_call_count: rejected, call_count: total,
    response_ids: Array.from({ length: total }, (_, index) => `SYNTHETIC-v4-${index}`) });
  value.stage_counts['clean.llm_semantic'] = cleanAccepted + rejected;
  value.stage_counts['research.candidates'] = issuer;
  Object.assign(value.budget, { schema: 'tiger.request-budget.v4', policy_id: V4, limits: { ...LIMITS },
    counts, records, actual_tokens: actual, estimated_charged_usd: actual * 1.2 / 1e6 + .0105 });
  return value;
}
function receipt(value) {
  const envelope = result('receipt'), research = researchV3Fixture({ model: value });
  Object.assign(envelope.summary, { as_of: { ...envelope.summary.as_of, market_date: '2026-01-03' },
    model: value, action_summary: research.summary, execution_decisions: research.executionDecisions,
    ...Object.fromEntries(['candidate_research', 'opportunity_lifecycle', 'business_evaluation',
      'portfolio_snapshot', 'execution_policy', 'workflow_phase', 'universe_coverage'].map(key => [key, research[key]])) });
  Object.assign(envelope.summary.disclosures, { model_mode: 'enabled', model_required: true });
  return envelope;
}
const browserSource = readFileSync(resolve(import.meta.dirname, '../public/app.js'), 'utf8');
const browserFunction = browserSource.slice(browserSource.indexOf('  function settledBudgetValid(model) {'), browserSource.indexOf('  function thesisValid('));
const browserValid = runInNewContext(`const recordObject=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const nonnegativeInteger=v=>Number.isSafeInteger(v)&&v>=0;
const positiveInteger=v=>Number.isSafeInteger(v)&&v>0;
const finite=v=>typeof v==='number'&&Number.isFinite(v);
const plainText=v=>typeof v==='string'&&v.trim().length>0;
${browserFunction}; settledBudgetValid`);
test('130 paid chat calls including 56 rejected generations survive all public boundaries', () => {
  const value = model(), before = structuredClone(value);
  assert.equal(value.call_count, 130);
  assert.ok(value.budget.actual_tokens > 600000);
  assert.ok(value.budget.records[0].reservation.estimated_cost_upper_usd > 1);
  assert.equal(validateModelV2(value), value);
  assert.equal(validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }).operation_ok, true);
  const research = researchV3Fixture({ model: value });
  assert.equal(validateResearch(research), research);
  assert.deepEqual(publicResearch({ sequence: 1, hash: 'd'.repeat(64), record: research }).model, value);
  assert.equal(browserValid(value), true);
  assert.deepEqual(value, before);
  assert.ok(CLIENT_LIMITS.waitMs > 7200000);
});
for (const [label, change] of [
  ['policy/schema mismatch', value => { value.budget.policy_id = 'tiger.budget-policy.v3'; }],
  ['numerical quota edit', value => { value.budget.limits.max_tokens++; }],
  ['noncompact policy', value => { value.clean_output_policy_id = 'tiger.clean-output.responses-slots.v1'; }],
  ['wrong reservation schema', value => { value.budget.records[0].reservation.schema = 'tiger.request-budget-reservation.v3'; }],
  ['wrong reservation policy', value => { value.budget.records[0].reservation.policy_id = 'tiger.budget-policy.v3'; }],
  ['unsettled paid response', value => { value.budget.records[0].status = 'unknown'; }],
  ['unknown cost', value => { value.budget.frozen = true; }],
  ['over30 estimate', value => { value.budget.estimated_charged_usd = 30.01; }],
  ['unbound new call', value => { value.response_ids[0] = value.response_ids[1]; }],
]) test(`v4 refuses ${label}`, () => {
  const value = model(); change(value);
  assert.throws(() => validateModelV2(value));
  assert.throws(() => validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }));
});
test('57 regenerations cannot be legalized by a fully reconciled larger ledger', () => {
  const value = model({ cleanAccepted: 57, rejected: 57 });
  assert.equal(value.budget.counts.chat, value.call_count);
  assert.throws(() => validateModelV2(value), /regeneration/);
  assert.equal(browserValid(value), false);
});
test('v4 schema cannot be attached to an unselected historical model', () => {
  const value = model({ cleanAccepted: 1, rejected: 0, issuer: 1, firstUsage: 100 });
  for (const key of ['clean_output_policy_id', 'clean_output_codec_id', 'regeneration_policy_id', 'accepted_call_count', 'rejected_call_count']) delete value[key];
  assert.throws(() => validateModelV2(value), /compact/);
  assert.equal(browserValid(value), false);
});
