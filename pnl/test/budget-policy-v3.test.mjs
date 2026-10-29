import test from 'node:test';
import assert from 'node:assert/strict';
import { validateModelV2 } from '../../bundles/trading/capabilities/required-v2-contract.mjs';
import { modelV2Fixture } from './short-term-fixture.mjs';

const POLICY = 'tiger.budget-policy.v3';
function version3() {
  const model = modelV2Fixture();
  Object.assign(model.budget, { schema: 'tiger.request-budget.v3', policy_id: POLICY });
  model.budget.limits.max_tokens = 600000;
  for (const record of model.budget.records) Object.assign(record.reservation,
    { schema: 'tiger.request-budget-reservation.v3', policy_id: POLICY });
  return model;
}
function largeUsage(model, firstActual = 500000, secondBound = 1100) {
  let actual = 0;
  for (const [index, record] of model.budget.records.entries()) {
    const r = record.reservation;
    r.actual_tokens_before = actual;
    if (index === 0) {
      r.input_token_upper_bound = firstActual - 100;
      r.output_token_upper_bound = 100;
      record.usage = { prompt_tokens: firstActual - 100, completion_tokens: 100, total_tokens: firstActual };
    } else if (index === 1) r.input_token_upper_bound = secondBound - r.output_token_upper_bound;
    r.token_upper_bound = r.input_token_upper_bound + r.output_token_upper_bound;
    r.estimated_cost_upper_usd = r.kind === 'chat' ? r.token_upper_bound * 1.2 / 1e6 : .00075;
    actual += record.usage?.total_tokens ?? 0;
  }
  model.budget.actual_tokens = actual;
  model.budget.estimated_charged_usd = actual * 1.2 / 1e6 + (model.search_count + model.fetch_count) * .00075;
  return model;
}

test('historical schema2/300k and explicit schema3/600k are both accepted', () => {
  assert.equal(validateModelV2(modelV2Fixture()).budget.schema, 'tiger.request-budget.v2');
  const model = largeUsage(version3());
  assert.equal(validateModelV2(model), model);
  const old = largeUsage(modelV2Fixture());
  assert.throws(() => validateModelV2(old));
});

for (const [name, mutate] of [
  ['arbitrary token ceiling', m => { m.budget.limits.max_tokens = 900000; }],
  ['300k advertised as v3', m => { m.budget.limits.max_tokens = 300000; }],
  ['policy missing', m => { delete m.budget.policy_id; }],
  ['policy unknown', m => { m.budget.policy_id = 'tiger.budget-policy.v4'; }],
  ['schema downgrade', m => { m.budget.schema = 'tiger.request-budget.v2'; }],
  ['reservation missing policy', m => { delete m.budget.records[0].reservation.policy_id; }],
  ['reservation wrong policy', m => { m.budget.records[0].reservation.policy_id = 'tiger.budget-policy.v2'; }],
  ['reservation schema downgrade', m => { m.budget.records[0].reservation.schema = 'tiger.request-budget-reservation.v2'; }],
  ['changed dollar ceiling', m => { m.budget.limits.max_cost_usd = 2; }],
  ['changed chat ceiling', m => { m.budget.limits.max_chat_calls = 41; }],
  ['changed pricing', m => { m.budget.pricing.token_upper_usd_per_million = 1; }],
  ['changed pricing basis', m => { m.budget.pricing.basis = 'Input tokens ignored'; }],
  ['unknown usage', m => { m.budget.records[0].status = 'unknown'; }],
  ['nonfinite cap', m => { m.budget.limits.max_tokens = Infinity; }],
]) test(`closed budget v3 rejects ${name}`, () => {
  const model = version3(); mutate(model); assert.throws(() => validateModelV2(model));
});

test('a settled total below 600k cannot hide an over-budget prior reservation', () => {
  const model = largeUsage(version3(), 590000, 20000);
  assert.ok(model.budget.actual_tokens < 600000);
  assert.throws(() => validateModelV2(model), /prepaid policy/);
});

test('schema2 cannot silently attach a new policy selector', () => {
  const model = modelV2Fixture(); model.budget.policy_id = POLICY;
  assert.throws(() => validateModelV2(model));
});
