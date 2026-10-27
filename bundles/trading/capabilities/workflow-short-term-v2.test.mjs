import test from 'node:test';
import assert from 'node:assert/strict';
import { UPGRADE_PROFILE, PREMARKET_PROFILE, INTRADAY_PROFILE, validateRequest, validateEnvelope, invokeWorkflow } from './workflow-client.mjs';
import { REQUEST, RUN_ID, result, runnerFixture } from '../../../scripts/tiger-agent-test-support.mjs';
import { researchV3Fixture, modelV2Fixture } from '../../../pnl/test/short-term-fixture.mjs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { validateModelV2 } from './required-v2-contract.mjs';
import { validateResearch } from '../../../pnl/lib/schema.mjs';
import { publicResearch } from '../../../pnl/lib/projection.mjs';
import { modelResearchFixture } from '../../../pnl/test/model-fixture.mjs';

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

// Current Python-to-Node slots linkage; all receipts below are synthetic.
const POLICY = 'tiger.clean-output.responses-slots.v1';
const CODEC = 'tiger.clean-slots-wire.v1';
const ROOT = resolve(import.meta.dirname, '../../..');
const python = process.env.TIGER_TEST_PYTHON;
assert.ok(python, 'TIGER_TEST_PYTHON must select the tested project interpreter');
// Call the production summary builder. Fixture receipts and every HTTP value
// are synthetic. Network access and credential inheritance are disabled.
const summary = JSON.parse(execFileSync(python, ['-B', '-c', `
import json, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('External I/O forbidden in the synthetic summary fixture')
socket.socket.connect = forbidden
socket.create_connection = forbidden
sys.path.insert(0, 'tests')
from test_clean_responses_output_v1 import responses_summary_client
from trading_system.llm.required import model_summary_v2
client = responses_summary_client()
client.execution.policy.clean_output_policy_id = '${POLICY}'
client.budget.clean_output_policy_id = '${POLICY}'
for receipt in client.receipts:
    if receipt['stage'] == 'clean.llm_semantic':
        receipt['output_policy_id'] = '${POLICY}'
print(json.dumps(model_summary_v2(client), allow_nan=False))
`], { cwd: ROOT, encoding: 'utf8', timeout: 15000,
  env: { ...Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR', 'SYSTEMROOT'].filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]])), PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1' } }));

function slotsEnvelope(model) {
  const r = result('receipt');
  const fixture = researchV3Fixture({ model });
  Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
    model, action_summary: fixture.summary, execution_decisions: fixture.executionDecisions,
    ...Object.fromEntries(['candidate_research', 'opportunity_lifecycle', 'business_evaluation',
      'portfolio_snapshot', 'execution_policy', 'workflow_phase', 'universe_coverage'].map(key => [key, fixture[key]])) });
  Object.assign(r.summary.disclosures, { model_mode: 'enabled', model_required: true });
  return r;
}

test('actual Python slots summary passes the shared Node model contract unchanged', () => {
  const model = structuredClone(summary);
  assert.equal(model.clean_output_policy_id, POLICY);
  assert.equal(model.clean_output_codec_id, CODEC);
  assert.equal(validateModelV2(model), model);
  assert.deepEqual(model, summary);
});

test('actual Python slots summary passes the full controlled receipt envelope', () => {
  const r = slotsEnvelope(structuredClone(summary));
  assert.equal(validateEnvelope(r, 'receipt', { run_id: RUN_ID }), r);
});

test('actual Python slots summary survives server research validation and public projection', () => {
  const record = researchV3Fixture({ model: structuredClone(summary) });
  assert.equal(validateResearch(record), record);
  const projected = publicResearch({ sequence: 1, hash: 'd'.repeat(64), record });
  assert.deepEqual(projected.model, summary);
  assert.equal(validateModelV2(projected.model), projected.model);
});

for (const [name, mutate] of [
  ['missing policy', m => { delete m.clean_output_policy_id; }],
  ['missing codec', m => { delete m.clean_output_codec_id; }],
  ['unknown policy', m => { m.clean_output_policy_id += '.unregistered'; }],
  ['historical policy carrying a new codec', m => { m.clean_output_policy_id = 'tiger.clean-output.responses-schema.v1'; }],
  ['unknown codec', m => { m.clean_output_codec_id += '.unregistered'; }],
  ['null policy', m => { m.clean_output_policy_id = null; }],
  ['array codec', m => { m.clean_output_codec_id = [CODEC]; }],
  ['extra summary property', m => { m.unregistered = true; }],
  ['old budget', m => { m.budget = modelV2Fixture().budget; }],
]) test(`model and receipt contracts reject ${name}`, () => {
  const model = structuredClone(summary); mutate(model);
  assert.throws(() => validateModelV2(model));
  assert.throws(() => validateEnvelope(slotsEnvelope(model), 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});

test('historical models with neither selector retain the exact old contract', () => {
  const old = modelV2Fixture(); const before = structuredClone(old);
  assert.equal(validateModelV2(old), old);
  assert.deepEqual(old, before);
  const previousV3 = structuredClone(summary);
  delete previousV3.clean_output_policy_id; delete previousV3.clean_output_codec_id;
  assert.equal(validateModelV2(previousV3), previousV3);
  assert.equal(validateEnvelope(slotsEnvelope(previousV3), 'receipt', { run_id: RUN_ID }).operation_ok, true);
});

for (const mode of ['enabled', 'disabled']) {
  function historicalReceipt() {
    const r = result('receipt'); const fixture = modelResearchFixture();
    Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
      model: fixture.model, action_summary: fixture.summary, execution_decisions: fixture.executionDecisions });
    Object.assign(r.summary.disclosures, { model_mode: mode, model_required: true });
    return r;
  }
  test(`historical ${mode} receipt without slots selectors stays valid`, () => {
    const r = historicalReceipt();
    assert.equal(validateEnvelope(r, 'receipt', { run_id: RUN_ID }), r);
  });
  for (const form of ['both', 'only-policy', 'only-codec']) test(`historical ${mode} receipt rejects slots ${form}`, () => {
    const r = historicalReceipt();
    if (form !== 'only-codec') r.summary.model.clean_output_policy_id = POLICY;
    if (form !== 'only-policy') r.summary.model.clean_output_codec_id = CODEC;
    assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
  });
}

for (const [name, mutate] of [
  ['disabled model', r => { r.summary.disclosures.model_mode = 'disabled'; }],
  ['optional model', r => { r.summary.disclosures.model_required = false; }],
  ['missing execution policy', r => { delete r.summary.execution_policy; }],
]) test(`slots receipt rejects ${name} before legacy or disabled branches`, () => {
  const r = slotsEnvelope(structuredClone(summary)); mutate(r);
  assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});
