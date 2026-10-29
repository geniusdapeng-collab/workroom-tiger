// Synthetic boundary fixtures only. No provider, runtime account or paid call.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { validateModelV2 } from './required-v2-contract.mjs';
import { validateEnvelope } from './workflow-client.mjs';
import { result, RUN_ID } from '../../../scripts/tiger-agent-test-support.mjs';
import { researchV3Fixture, modelV2Fixture } from '../../../pnl/test/short-term-fixture.mjs';
import { validateResearch } from '../../../pnl/lib/schema.mjs';
import { publicResearch } from '../../../pnl/lib/projection.mjs';
import { modelResearchFixture } from '../../../pnl/test/model-fixture.mjs';
const POLICY = 'tiger.clean-output.compact-lists-regeneration.v1';
const CODEC = 'tiger.clean-compact-lists-wire.v1';
const REGEN = 'tiger.model-regeneration.clean-schema.v1';
const OLD_POLICY = 'tiger.clean-output.responses-slots.v1';
const OLD_CODEC = 'tiger.clean-slots-wire.v1';
function model({ rejected = 1, cleanAccepted = Math.max(1, rejected) } = {}) {
  const value = modelV2Fixture(), template = structuredClone(value.budget.records[0].reservation);
  const counts = { chat: 0, search: 0, fetch: 0 }, records = [];
  const accepted = cleanAccepted + 5, all = accepted + rejected;
  let actual = 0;
  for (const kind of [...Array(all).fill('chat'), ...Array(14).fill('search')]) {
    const ordinal = records.length + 1;
    const reservation = { ...template, schema: 'tiger.request-budget-reservation.v3', policy_id: 'tiger.budget-policy.v3',
      call_id: ordinal.toString(16).padStart(32, '0'), kind, sequence: ordinal,
      input_token_upper_bound: kind === 'chat' ? 1000 : 0, output_token_upper_bound: kind === 'chat' ? 100 : 0,
      token_upper_bound: kind === 'chat' ? 1100 : 0, estimated_cost_upper_usd: kind === 'chat' ? .00132 : .00075,
      counts_before: { ...counts }, actual_tokens_before: actual, reserved_tokens_before: 0 };
    records.push({ reservation, status: 'settled', usage: kind === 'chat' ? { prompt_tokens: 90, completion_tokens: 10, total_tokens: 100 } : null });
    counts[kind]++; if (kind === 'chat') actual += 100;
  }
  Object.assign(value, { clean_output_policy_id: POLICY, clean_output_codec_id: CODEC,
    regeneration_policy_id: REGEN, accepted_call_count: accepted, rejected_call_count: rejected,
    call_count: all, response_ids: Array.from({ length: all }, (_, n) => `SYNTHETIC-compact-attempt-${n}`) });
  value.stage_counts['clean.llm_semantic'] = cleanAccepted + rejected;
  Object.assign(value.budget, { schema: 'tiger.request-budget.v3', policy_id: 'tiger.budget-policy.v3',
    counts, records, actual_tokens: actual, estimated_charged_usd: actual * 1.2 / 1e6 + .0105 });
  value.budget.limits.max_tokens = 600000;
  return value;
}
function receipt(value = model()) {
  const r = result('receipt'), fixture = researchV3Fixture({ model: value });
  Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
    model: value, action_summary: fixture.summary, execution_decisions: fixture.executionDecisions,
    ...Object.fromEntries(['candidate_research', 'opportunity_lifecycle', 'business_evaluation',
      'portfolio_snapshot', 'execution_policy', 'workflow_phase', 'universe_coverage'].map(k => [k, fixture[k]])) });
  Object.assign(r.summary.disclosures, { model_mode: 'enabled', model_required: true });
  return r;
}
for (const rejected of [0, 1, 4]) test(`completed compact summary with ${rejected} rejected attempts survives every public boundary`, () => {
  const value = model({ rejected }), before = structuredClone(value);
  assert.equal(validateModelV2(value), value);
  assert.equal(validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }).operation_ok, true);
  const research = researchV3Fixture({ model: value });
  assert.equal(validateResearch(research), research);
  assert.deepEqual(publicResearch({ sequence: 1, hash: 'd'.repeat(64), record: research }).model, value);
  assert.deepEqual(value, before);
});
test('historical slots exact pair retains its original closed model contract', () => {
  const value = model({ rejected: 0 });
  delete value.regeneration_policy_id; delete value.accepted_call_count; delete value.rejected_call_count;
  value.clean_output_policy_id = OLD_POLICY; value.clean_output_codec_id = OLD_CODEC;
  assert.equal(validateModelV2(value), value);
  assert.equal(validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }).operation_ok, true);
});
for (const [name, change] of [
  ['missing clean policy', m => { delete m.clean_output_policy_id; }],
  ['missing codec', m => { delete m.clean_output_codec_id; }],
  ['old policy with compact codec', m => { m.clean_output_policy_id = OLD_POLICY; }],
  ['compact policy with old codec', m => { m.clean_output_codec_id = OLD_CODEC; }],
  ['unknown clean policy', m => { m.clean_output_policy_id += '.unknown'; }],
  ['unknown codec', m => { m.clean_output_codec_id += '.unknown'; }],
  ['missing regeneration policy', m => { delete m.regeneration_policy_id; }],
  ['unknown regeneration policy', m => { m.regeneration_policy_id += '.unknown'; }],
  ['missing accepted count', m => { delete m.accepted_call_count; }],
  ['missing rejected count', m => { delete m.rejected_call_count; }],
  ['null clean policy', m => { m.clean_output_policy_id = null; }],
  ['array codec', m => { m.clean_output_codec_id = [CODEC]; }],
  ['fractional rejected count', m => { m.rejected_call_count = .5; }],
  ['boolean rejected count', m => { m.rejected_call_count = true; }],
  ['negative rejected count', m => { m.rejected_call_count = -1; }],
  ['too few accepted calls', m => { m.accepted_call_count = 5; }],
  ['counter sum mismatch', m => { m.accepted_call_count++; }],
  ['no accepted clean call', m => { m.stage_counts['clean.llm_semantic'] = 1; m.call_count--; m.accepted_call_count--; }],
  ['legacy budget', m => { m.budget = modelV2Fixture().budget; }],
  ['unsettled usage', m => { m.budget.frozen = true; }],
  ['missing paid record', m => { m.budget.records.shift(); }],
  ['reused response identity', m => { m.response_ids[1] = m.response_ids[0]; }],
]) test(`new model and controlled receipt reject ${name}`, () => {
  const value = model(); change(value);
  assert.throws(() => validateModelV2(value));
  assert.throws(() => validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});
for (const [name, options] of [['five regeneration calls', { rejected: 5 }], ['two rejections per accepted batch', { rejected: 2, cleanAccepted: 1 }]]) {
  test(`completed model rejects ${name} even with an otherwise reconciled paid ledger`, () => {
    const value = model(options);
    assert.equal(value.accepted_call_count + value.rejected_call_count, value.call_count);
    assert.equal(value.budget.counts.chat, value.call_count);
    assert.throws(() => validateModelV2(value));
    assert.throws(() => validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
  });
}
for (const mode of ['enabled', 'disabled']) test(`legacy ${mode} model cannot smuggle compact selectors`, () => {
  const r = result('receipt'), legacy = modelResearchFixture();
  Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
    model: { ...legacy.model, clean_output_policy_id: POLICY, clean_output_codec_id: CODEC },
    action_summary: legacy.summary, execution_decisions: legacy.executionDecisions });
  Object.assign(r.summary.disclosures, { model_mode: mode, model_required: true });
  assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});
for (const [name, change] of [
  ['disabled', r => { r.summary.disclosures.model_mode = 'disabled'; }],
  ['optional', r => { r.summary.disclosures.model_required = false; }],
  ['no execution policy', r => { delete r.summary.execution_policy; }],
]) test(`compact receipt rejects ${name} before legacy routing`, () => {
  const r = receipt(); change(r);
  assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
});
for (const field of ['regeneration_policy_id', 'accepted_call_count', 'rejected_call_count']) {
  test(`historical no-pair and slots summaries reject smuggled ${field}`, () => {
    for (const selected of [false, true]) {
      const value = model({ rejected: 0 });
      delete value.clean_output_policy_id; delete value.clean_output_codec_id;
      delete value.regeneration_policy_id; delete value.accepted_call_count; delete value.rejected_call_count;
      if (selected) { value.clean_output_policy_id = OLD_POLICY; value.clean_output_codec_id = OLD_CODEC; }
      assert.equal(validateModelV2(value), value);
      value[field] = field.endsWith('_id') ? REGEN : 0;
      assert.throws(() => validateModelV2(value));
    }
  });
}
test('published schema lists both reviewed selectors and the closed new scalar fields', () => {
  const d = JSON.parse(readFileSync(resolve(import.meta.dirname, '../agent-workflow.schema.json'), 'utf8'));
  const fields = d.properties.summary.properties.model.properties;
  assert.deepEqual(new Set(fields.clean_output_policy_id.enum), new Set([OLD_POLICY, POLICY]));
  assert.deepEqual(new Set(fields.clean_output_codec_id.enum), new Set([OLD_CODEC, CODEC]));
  assert.equal(fields.regeneration_policy_id.const, REGEN);
  assert.deepEqual(fields.accepted_call_count, { type: 'integer', minimum: 6, maximum: 160 });
  assert.deepEqual(fields.rejected_call_count, { type: 'integer', minimum: 0, maximum: 56 });
});

for (const mode of ['enabled', 'disabled']) {
  function legacyReceipt() {
    const r = result('receipt'), legacy = modelResearchFixture();
    Object.assign(r.summary, { as_of: { ...r.summary.as_of, market_date: '2026-01-03' },
      model: structuredClone(legacy.model), action_summary: legacy.summary, execution_decisions: legacy.executionDecisions });
    Object.assign(r.summary.disclosures, { model_mode: mode, model_required: true });
    return r;
  }
  test(`legacy ${mode} receipt without new claims remains valid`, () => {
    assert.equal(validateEnvelope(legacyReceipt(), 'receipt', { run_id: RUN_ID }).operation_ok, true);
  });
  for (const field of ['regeneration_policy_id', 'accepted_call_count', 'rejected_call_count']) {
    test(`legacy ${mode} receipt refuses standalone ${field} with both clean selectors absent`, () => {
      const r = legacyReceipt(); r.summary.model[field] = field.endsWith('_id') ? REGEN : 0;
      assert.throws(() => validateEnvelope(r, 'receipt', { run_id: RUN_ID }), { code: 'RESULT_INVALID' });
    });
  }
}

test('actual production Python summary with rejected and accepted attempts survives Node and publication unchanged', async () => {
  const { execFileSync } = await import('node:child_process');
  const python = process.env.TIGER_TEST_PYTHON;
  assert.ok(python, 'TIGER_TEST_PYTHON must select the tested project interpreter');
  const text = execFileSync(python, ['-B', '-c', `
import json, socket, sys
import requests
def forbidden(*args, **kwargs):
    raise AssertionError('External I/O forbidden in the synthetic production-summary fixture')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
requests.sessions.Session.request = forbidden
sys.path.insert(0, 'tests')
from test_clean_compact_regeneration_core_v1 import make_completed_summary_client
from trading_system.llm.required import model_summary_v2
print(json.dumps(model_summary_v2(make_completed_summary_client()), allow_nan=False))
`], { cwd: resolve(import.meta.dirname, '../../..'), encoding: 'utf8', timeout: 15000,
    env: { ...Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR', 'SYSTEMROOT'].filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]])), PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1' } });
  const value = JSON.parse(text), before = structuredClone(value);
  assert.equal(value.clean_output_policy_id, POLICY); assert.equal(value.clean_output_codec_id, CODEC);
  assert.equal(value.regeneration_policy_id, REGEN);
  assert.equal(value.call_count, 7); assert.equal(value.accepted_call_count, 6); assert.equal(value.rejected_call_count, 1);
  assert.equal(value.stage_counts['clean.llm_semantic'], 2);
  assert.equal(validateModelV2(value), value);
  assert.equal(validateEnvelope(receipt(value), 'receipt', { run_id: RUN_ID }).operation_ok, true);
  const research = researchV3Fixture({ model: value });
  assert.equal(validateResearch(research), research);
  assert.deepEqual(publicResearch({ sequence: 1, hash: 'd'.repeat(64), record: research }).model, value);
  assert.deepEqual(value, before);
});
