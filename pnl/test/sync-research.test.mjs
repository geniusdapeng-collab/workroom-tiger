import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildResearch, parseArgs, syncResearch, SyncError } from '../sync-research.mjs';
import { canonical } from '../lib/schema.mjs';

const DATE = '2026-01-03'; const PROFILE = 'us-public-paper-v1'; const RUN = `run-${'a'.repeat(32)}`;
const TEST_TOKEN = 'SYNC-UNIT-TEST-ONLY-NEVER-A-REAL-SECRET';
const hash = value => createHash('sha256').update(value).digest('hex');
function envelope(operation, changes = {}) { return { schema_version: 'tiger.agent-result/v1', operation, operation_ok: true, run_id: RUN, status: 'completed_degraded', execution_mode: 'paper', data: {}, summary: null, verification: null, limitations: [], evidence_refs: [], error: null, ...changes }; }
async function setup(t) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-pnl-sync-test-')); t.after(() => rm(path, { recursive: true, force: true }));
  const stateRoot = join(path, 'agent'); const runDir = join(stateRoot, 'runs', RUN); await mkdir(runDir, { recursive: true });
  const originalRequest = { schema_version: 'tiger.agent-request/v1', profile: PROFILE, paper_account: 'public-usd', idempotency_key: `tigerpnl-research-${PROFILE}-${DATE}` };
  const raw = { schema: 'tiger.receipt.v1', run_id: RUN, profile: PROFILE, paper_account: 'public-usd', status: 'completed_degraded', sealed_at_utc: '2026-01-03T20:01:30.123456+00:00' };
  const refs = [];
  for (const [name, object] of [['receipt.json', raw], ['request.json', originalRequest]]) { const bytes = Buffer.from(JSON.stringify(object)); await writeFile(join(runDir, name), bytes); refs.push({ path: name, sha256: hash(bytes), bytes: bytes.length, schema: name === 'receipt.json' ? 'tiger.receipt.v1' : 'tiger.agent-request/v1' }); }
  const verified = { ok: true, receipt_sha256: refs[0].sha256, evidence_count: 42, checks: ['fixture-only-check'] };
  const summary = { picks: [{ ticker: 'AAPL', position_pct: 0.1, card: 'TEST RESEARCH ONLY\nPrice < 120; watch risk.' }], watchlist: [{ ticker: 'MSFT', evidence: ['TEST observation only'], stop_plan: 'TEST research risk' }], positions: [{ ticker: 'FAKE', shares: 999999 }], pending: [{ ticker: 'FAKE' }], cash_usd: 987654321, equity_usd: 987654321, market_value_usd: 987654321, action: 'TRADE', market_view: 'UNIT TEST RESEARCH FIXTURE ONLY', as_of: { market_date: DATE, market_timezone: 'America/New_York', admitted_at_utc: '2026-01-03T20:00:00Z', benchmark_last_bar: '2026-01-02', freshness: {} }, disclosures: { synthetic: false, execution_mode: 'paper', model_mode: 'disabled', data_coverage: '9/10', scan_stats: { universe_size: 10 } } };
  const receipt = envelope('receipt', { summary, verification: verified, evidence_refs: refs });
  const verification = envelope('verify', { verification: verified, evidence_refs: refs, data: { source: { tree_sha256: 'b'.repeat(64) } } });
  const env = { TIGER_AGENT_PYTHON: '/test/trusted/python', TIGER_AGENT_STATE_ROOT: stateRoot, TIGER_AGENT_PERMISSION: 'paper', TIGER_PNL_SYNC_STATE_DIR: join(path, 'sync'), TIGER_PNL_WRITE_TOKEN: TEST_TOKEN, TIGER_PNL_URL: 'https://example.com' };
  const calls = []; const published = [];
  const invoke = async (operation, input) => { calls.push({ operation, input }); if (operation === 'doctor') return envelope(operation, { status: 'ready', data: { access_mode: 'paper' } }); if (operation === 'describe') return envelope(operation, { status: 'ready', data: { profile: { profile: input.profile }, access_mode: 'paper' } }); if (operation === 'run') return envelope(operation, { status: 'accepted' }); if (operation === 'status') return envelope(operation); if (operation === 'receipt') return receipt; if (operation === 'verify') return verification; throw new Error('Unexpected operation'); };
  const publish = async options => { published.push(options); return { idempotencyKey: options.record.idempotencyKey, payloadHash: hash(canonical(options.record)), hash: 'c'.repeat(64), sequence: 1 }; };
  const stateFile = join(env.TIGER_PNL_SYNC_STATE_DIR, `${PROFILE}-${DATE}.json`);
  return { path, env, originalRequest, raw, runDir, receipt, verification, calls, published, invoke, publish, stateFile };
}

test('verified sync calls the controlled six operations, publishes research only, and persists no token or paper holdings', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish });
  assert.equal(result.status, 'published_and_verified'); assert.deepEqual(f.calls.map(c => c.operation), ['doctor', 'describe', 'run', 'status', 'receipt', 'verify']);
  const payload = f.published[0].record; assert.equal(payload.decisions[0].action, 'buy'); assert.equal(payload.decisions[0].targetWeight, 0.1); assert.equal(payload.decisions[1].action, 'watch');
  assert.equal(payload.dataAsOf, '2026-01-02'); assert.equal(payload.asOf, '2026-01-03T20:01:30.123Z'); assert.equal(payload.modelMode, 'disabled');
  assert.equal(Object.hasOwn(payload, 'positions'), false); assert.equal(Object.hasOwn(payload, 'account'), false); assert.equal(Object.hasOwn(payload, 'trades'), false);
  const saved = await readFile(f.stateFile, 'utf8'); for (const forbidden of [TEST_TOKEN, '987654321', 'FAKE', 'cash_usd', 'positions', 'pending']) assert.equal(saved.includes(forbidden), false, forbidden);
  const again = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }); assert.equal(again.status, 'already_published'); assert.equal(f.published.length, 1);
  assert.equal((await readdir(f.env.TIGER_PNL_SYNC_STATE_DIR)).some(name => name.endsWith('.lock')), false);
});

test('lost acceptance response preserves exactly the same original request and daily key on recovery', async t => {
  const f = await setup(t); let dropped = false;
  const invoke = async (operation, input, ...rest) => { if (operation === 'run' && !dropped) { dropped = true; f.calls.push({ operation, input }); throw new SyncError('OBSERVATION_TIMEOUT', 'lost response'); } return f.invoke(operation, input, ...rest); };
  const first = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(first.status, 'pending'); assert.equal(first.runId, null); assert.equal(first.workerCancellationRequested, false);
  const second = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(second.status, 'published_and_verified');
  const requests = f.calls.filter(c => c.operation === 'run'); assert.equal(requests.length, 2); assert.deepEqual(requests[0].input, requests[1].input);
});

test('accepted running research returns pending, resumes the existing ID, and never replaces it', async t => {
  const f = await setup(t); let running = true;
  const invoke = async (operation, input, ...rest) => operation === 'status' && running ? envelope('status', { status: 'running' }) : f.invoke(operation, input, ...rest);
  const first = await syncResearch({ env: f.env, date: DATE, profile: PROFILE, observeMs: 1100 }, { invoke, publish: f.publish }); assert.equal(first.status, 'pending'); assert.equal(first.runId, RUN); assert.equal(f.published.length, 0);
  running = false; const second = await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish }); assert.equal(second.status, 'published_and_verified'); assert.equal(f.calls.filter(c => c.operation === 'run').length, 1);
});

test('lost website response retries the persisted identical payload without rerunning research', async t => {
  const f = await setup(t); let dropped = false;
  const publish = async options => { if (!dropped) { dropped = true; f.published.push(options); throw new TypeError('network response lost'); } return f.publish(options); };
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'pending');
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'published_and_verified');
  assert.deepEqual(f.published[0].record, f.published[1].record); assert.equal(f.calls.filter(c => c.operation === 'run').length, 1); assert.equal(f.calls.filter(c => c.operation === 'verify').length, 1);
});

test('missing receipt/failed run/mismatched verification cannot publish and retain their run identity', async t => {
  const f = await setup(t);
  const invoke = async (operation, input, ...rest) => operation === 'receipt' ? envelope('receipt', { operation_ok: false, status: 'invalid', summary: null, verification: null, error: { code: 'EVIDENCE_MISSING', message: 'fixture missing', run_id: RUN, stage: null, retryable: false, details_ref: null } }) : f.invoke(operation, input, ...rest);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke, publish: f.publish })); assert.equal(f.published.length, 0); assert.equal(JSON.parse(await readFile(f.stateFile, 'utf8')).runId, RUN);
  f.verification.verification = { ...f.verification.verification, receipt_sha256: 'f'.repeat(64) };
  await assert.rejects(buildResearch({ receipt: f.receipt, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'test-bad-verification' }), /both pass/);
  const failed = async (operation, input, ...rest) => operation === 'status' ? envelope('status', { status: 'failed' }) : f.invoke(operation, input, ...rest);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: failed, publish: f.publish }), { code: 'RUN_NOT_COMPLETED' });
  assert.equal(JSON.parse(await readFile(f.stateFile, 'utf8')).phase, 'failed'); assert.equal(f.published.length, 0);
});

test('verified artifact tampering and imported profile mismatch are rejected', async t => {
  const f = await setup(t); await writeFile(join(f.runDir, 'receipt.json'), JSON.stringify({ ...f.raw, profile: 'not-allowed' }));
  await assert.rejects(buildResearch({ receipt: f.receipt, verification: f.verification, stateRoot: f.env.TIGER_AGENT_STATE_ROOT, profile: PROFILE, idempotencyKey: 'test-artifact-tampering' }), /size differs|hash differs/);
});

test('explicit existing-run import does not submit run and trusted configuration cannot be overridden by payload', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE, profile: PROFILE, runId: RUN }, { invoke: f.invoke, publish: f.publish }); assert.equal(result.status, 'published_and_verified'); assert.equal(f.calls.some(c => c.operation === 'run'), false);
  await assert.rejects(syncResearch({ env: { ...f.env, TIGER_AGENT_PERMISSION: 'readonly' }, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_TRUSTED_CONFIGURATION' });
  await assert.rejects(syncResearch({ env: f.env, date: '2026-02-31', profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_SYNC_REQUEST' });
  assert.throws(() => parseArgs(['--mode', 'live']), /only/); assert.throws(() => parseArgs(['--profile', PROFILE, '--profile', PROFILE]), /only/);
});

test('sync rejects live observers, reclaims a provably dead observer, and omits unexpected receipt fields', async t => {
  const f = await setup(t); await mkdir(f.env.TIGER_PNL_SYNC_STATE_DIR);
  const lock = `${f.stateFile}.lock`; await writeFile(lock, JSON.stringify({ pid: process.pid }));
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'SYNC_BUSY' });
  // PID upper bound is outside ordinary local process allocation on supported hosts.
  let deadPid = 2147483647;
  try { process.kill(deadPid, 0); assert.fail('Test PID unexpectedly exists'); } catch (cause) { assert.equal(cause.code, 'ESRCH'); }
  await writeFile(lock, JSON.stringify({ pid: deadPid }));
  const publish = async options => ({ ...await f.publish(options), arbitraryRemoteField: TEST_TOKEN });
  assert.equal((await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish })).status, 'published_and_verified');
  assert.equal((await readFile(f.stateFile, 'utf8')).includes(TEST_TOKEN), false);
});

test('corrupt persisted publication metadata cannot be reported as already published', async t => {
  const f = await setup(t); await syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish });
  const state = JSON.parse(await readFile(f.stateFile, 'utf8')); state.websiteReceipt.payloadHash = 'f'.repeat(64); await writeFile(f.stateFile, JSON.stringify(state));
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: PROFILE }, { invoke: f.invoke, publish: f.publish }), { code: 'SYNC_STATE_CONFLICT' });
  assert.equal(f.published.length, 1);
});

test('unspecified sync profile remains the verified conservative profile', async t => {
  const f = await setup(t);
  const result = await syncResearch({ env: f.env, date: DATE }, { invoke: f.invoke, publish: f.publish });
  assert.equal(result.status, 'published_and_verified');
  assert.equal(f.calls.find(call => call.operation === 'describe').input.profile, 'us-public-paper-v1');
  assert.equal(f.calls.find(call => call.operation === 'run').input.profile, 'us-public-paper-v1');
  assert.equal(f.published[0].record.idempotencyKey, `pnl-us-public-paper-v1-${DATE}`);
});
