import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { canonical, digest, validateResearch } from '../lib/schema.mjs';
import { ResearchLedger } from '../lib/ledger.mjs';
import { createPnlServer } from '../server.mjs';
import { modelResearchFixture, instructionFixture } from './model-fixture.mjs';

const TOKEN = 'MODEL-REQUIRED-UNIT-TEST-ONLY-NOT-A-REAL-SECRET';
function legacyFixture(overrides = {}) {
  return { schemaVersion: 1, idempotencyKey: 'legacy-disabled-test-0001', asOf: '2026-01-02T21:00:00Z', runId: 'old-unit-test-disabled-run', receiptHash: digest('OLD TEST RECEIPT'), dataAsOf: '2026-01-02', executionMode: 'paper', modelMode: 'disabled', status: 'completed_degraded', universeSize: 0, summary: 'Historical test fixture only, model not called.', decisions: [], ...overrides };
}
async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-model-required-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}
async function writeHistoricalEntry(path, record) {
  await mkdir(join(path, 'records'), { recursive: true });
  const body = { sequence: 1, acceptedAt: '2026-01-02T22:00:00Z', previousHash: '0'.repeat(64), payloadHash: digest(record), record };
  const entry = { ...body, hash: digest(body) };
  await writeFile(join(path, 'records', '000000000001.json'), canonical(entry));
  return entry;
}

test('model-required research accepts the strict model contract and rejects missing stages, downgraded models and forged counters', () => {
  const original = modelResearchFixture();
  assert.doesNotThrow(() => validateResearch(original));
  const cases = [
    { ...original, modelMode: 'disabled' },
    { ...original, modelMode: 'passthrough' },
    { ...original, status: 'completed_degraded' },
    { ...original, model: { ...original.model, model: 'deepseek-v4-pro' } },
    { ...original, model: { ...original.model, release: 'unverified-latest' } },
    { ...original, model: { ...original.model, provider: 'other' } },
    { ...original, model: { ...original.model, required: false } },
    { ...original, model: { ...original.model, all_required_completed: false } },
    { ...original, model: { ...original.model, search_count: 0 } },
    { ...original, model: { ...original.model, call_count: 4 } },
    { ...original, model: { ...original.model, response_ids: [] } },
    { ...original, model: { ...original.model, required_stages: original.model.required_stages.slice(1) } },
    { ...original, model: { ...original.model, stage_counts: { ...original.model.stage_counts, 'decision.debate': 0 } } },
    { ...original, model: { ...original.model, apiKey: 'must-not-enter-contract' } },
  ];
  for (const value of cases) assert.throws(() => validateResearch(value), { status: 422 });
});

test('execution cards bind numeric instructions to the compatibility decision and refuse unsafe source links', () => {
  const buy = modelResearchFixture({ executionDecisions: [instructionFixture({ action: 'buy' })] });
  assert.doesNotThrow(() => validateResearch(buy));
  for (const changes of [
    { target_weight: 0.3 }, { entry_price: null }, { stop_price: 101 }, { shares: 0 },
    { shares: 1.5 }, { risk_usd: -1 }, { why_this: '' }, { valid_for: '' }, { valid_for: '2026-02-31T16:00:00-05:00' },
    { sources: [{ id: 'bad', title: 'bad', url: 'https://example.com/?accessToken=must-not-leak', published_at: null }] },
    { sources: [{ id: 'bad', title: 'bad', url: 'javascript:alert(1)', published_at: null }] },
    { sources: [{ id: 'bad', title: 'bad', url: 'https://user:password@example.com/', published_at: null }] },
  ]) {
    const value = structuredClone(buy); value.executionDecisions[0] = { ...value.executionDecisions[0], ...changes };
    assert.throws(() => validateResearch(value), { status: 422 });
  }
  const watch = modelResearchFixture();
  assert.equal(watch.executionDecisions[0].entry_price, null);
  assert.doesNotThrow(() => validateResearch(watch));
});

test('historical disabled chains still load unchanged while every new disabled append is refused', async t => {
  const path = await directory(t); const old = legacyFixture(); const entry = await writeHistoricalEntry(path, old);
  const store = await ResearchLedger.open(path);
  try {
    assert.equal(store.entries[0].hash, entry.hash);
    assert.deepEqual(store.entries[0].record, old);
    await assert.rejects(store.append(legacyFixture({ idempotencyKey: 'new-disabled-must-fail', asOf: '2026-01-03T21:00:00Z', runId: 'new-disabled-run' })), { code: 'MODEL_REQUIRED' });
    assert.equal(store.entries.length, 1);
    const accepted = await store.append(modelResearchFixture());
    assert.equal(accepted.sequence, 2); assert.equal(accepted.previousHash, entry.hash);
  } finally { await store.close(); }
  const restored = await ResearchLedger.open(path);
  try { assert.equal(restored.entries.length, 2); assert.equal(restored.entries[0].hash, entry.hash); }
  finally { await restored.close(); }
});

test('HTTP publishes model execution instructions without inventing account fills and serves the whitepaper as PDF', async t => {
  const path = await directory(t); const publicDir = join(path, 'public');
  await mkdir(join(publicDir, 'whitepaper'), { recursive: true });
  await writeFile(join(publicDir, 'index.html'), '<!doctype html><title>TEST ONLY</title>');
  const pdf = Buffer.from('%PDF-1.4\nTEST MIME FIXTURE ONLY\n%%EOF');
  await writeFile(join(publicDir, 'whitepaper', 'tiger-trading-20260730.pdf'), pdf);
  const app = await createPnlServer({ dataDir: join(path, 'private'), publicDir, token: TOKEN });
  await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${app.server.address().port}`;
  const post = value => fetch(`${base}/api/v1/research`, { method: 'POST', headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  try {
    const denied = await post(legacyFixture()); assert.equal(denied.status, 422); assert.equal((await denied.json()).error.code, 'MODEL_REQUIRED');
    const input = modelResearchFixture(); const accepted = await post(input); assert.equal(accepted.status, 201);
    const output = await (await fetch(`${base}/api/public/dashboard`)).json();
    assert.equal(output.latest, null); assert.deepEqual(output.records, []);
    assert.deepEqual(output.research.latest.model, input.model);
    assert.deepEqual(output.research.latest.executionDecisions, input.executionDecisions);
    assert.equal(output.research.latest.schemaVersion, 2);
    assert.equal(JSON.stringify(output).includes(TOKEN), false);
    const document = await fetch(`${base}/whitepaper/tiger-trading-20260730.pdf`);
    assert.equal(document.status, 200); assert.match(document.headers.get('content-type'), /^application\/pdf/);
    assert.deepEqual(Buffer.from(await document.arrayBuffer()), pdf);
    const before = await readFile(join(path, 'private', 'research', 'records', '000000000001.json'));
    assert.equal((await post(input)).status, 200);
    assert.deepEqual(await readFile(join(path, 'private', 'research', 'records', '000000000001.json')), before);
  } finally { await app.close(); }
});
