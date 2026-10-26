import test from 'node:test';
import assert from 'node:assert/strict';
import { UPGRADE_PROFILE, PREMARKET_PROFILE, INTRADAY_PROFILE, validateRequest, validateEnvelope, invokeWorkflow } from './workflow-client.mjs';
import { REQUEST, RUN_ID, result, runnerFixture } from '../../../scripts/tiger-agent-test-support.mjs';
import { researchV3Fixture } from '../../../pnl/test/short-term-fixture.mjs';

function receipt() {
  const r = result('receipt'); const fixture = researchV3Fixture();
  Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
    model: fixture.model, action_summary: fixture.summary, execution_decisions: fixture.executionDecisions,
    candidate_research: fixture.candidate_research, opportunity_lifecycle: fixture.opportunity_lifecycle,
    business_evaluation: fixture.business_evaluation, portfolio_snapshot: fixture.portfolio_snapshot,
    execution_policy: fixture.execution_policy, workflow_phase: fixture.workflow_phase,
    universe_coverage: fixture.universe_coverage });
  Object.assign(r.summary.disclosures, { model_mode: 'enabled', model_required: true });
  return r;
}
for (const profile of [UPGRADE_PROFILE, PREMARKET_PROFILE, INTRADAY_PROFILE]) test(`fixed v2 profile ${profile} accepts no free execution options`, () => {
  assert.equal(validateRequest('run', { ...REQUEST, profile }).profile, profile);
  assert.equal(validateRequest('describe', { profile }).profile, profile);
  assert.throws(() => validateRequest('run', { ...REQUEST, profile, mode: 'live' }), { code: 'INVALID_REQUEST' });
});
test('v2 receipt accepts technical BUY with six fully settled stages and own stock evidence', () => {
  const r = receipt(); assert.equal(validateEnvelope(r, 'receipt', { run_id: RUN_ID }), r);
});
for (const [name, mutate] of [
  ['lost candidate stage', r => { delete r.summary.model.stage_counts['research.candidates']; }],
  ['unknown paid usage', r => { r.summary.model.budget.frozen = true; }],
  ['insufficient buy thesis', r => { r.summary.execution_decisions[0].thesis.status = 'insufficient'; }],
  ['missing own snapshot thesis', r => { r.summary.execution_decisions[0].thesis.five_part.stock.evidence_ids = ['fx_TEST_stock']; }],
  ['unconfirmed event source', r => { r.summary.execution_decisions[0].thesis.strategy = 'news_event_v2'; }],
  ['legacy marker with new model', r => { delete r.summary.execution_policy; }],
]) test(`v2 receipt rejects ${name}`, () => {
  const r = receipt(); mutate(r); assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});
test('v2 run transmits only protected fixed model configuration to its controlled interpreter', async t => {
  const f = await runnerFixture(t);
  const names = ['LLM_BACKEND', 'LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY']; const before = Object.fromEntries(names.map(name => [name, process.env[name]]));
  Object.assign(process.env, { LLM_BACKEND: 'api', LLM_BASE_URL: 'https://api.deepseek.com', LLM_MODEL: 'deepseek-flash', LLM_API_KEY: 'SYNTHETIC-V2-KEY-ONLY' });
  t.after(() => { for (const name of names) { if (before[name] === undefined) delete process.env[name]; else process.env[name] = before[name]; } });
  await invokeWorkflow('run', { ...REQUEST, profile: UPGRADE_PROFILE }, f.options);
  assert.equal((await f.calls())[0].envNames.includes('LLM_API_KEY'), true);
  assert.equal((await f.calls())[0].envNames.includes('TIGER_PNL_WRITE_TOKEN'), false);
});

test('controlled interpreter transports an over-1MiB v2 receipt while an equally sized historical receipt is refused', async t => {
  const v2 = receipt(); v2.data = { synthetic_contract_only: 'x'.repeat(1_200_000) };
  const fresh = await runnerFixture(t, `send(${JSON.stringify(v2)});`);
  const accepted = await invokeWorkflow('receipt', { run_id: RUN_ID }, fresh.options);
  assert.equal(accepted.operation_ok, true); assert.equal(accepted.data.synthetic_contract_only.length, 1_200_000);
  assert.deepEqual(accepted.summary.model.required_stages, v2.summary.model.required_stages);
  const old = result('receipt'); old.data = { synthetic_contract_only: 'x'.repeat(1_200_000) };
  const historical = await runnerFixture(t, `send(${JSON.stringify(old)});`);
  const rejected = await invokeWorkflow('receipt', { run_id: RUN_ID }, historical.options);
  assert.equal(rejected.operation_ok, false); assert.equal(rejected.error.code, 'RESULT_INVALID');
});
