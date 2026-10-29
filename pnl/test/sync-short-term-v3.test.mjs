import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { canonical, digest } from '../lib/schema.mjs';
import { buildResearch, syncResearch, parseArgs } from '../sync-research.mjs';
import { researchV3Fixture, UPGRADE_PROFILE } from './short-term-fixture.mjs';

const RUN = `run-${'b'.repeat(32)}`, DATE = '2026-01-03';
function envelope(operation, changes = {}) { return { schema_version: 'tiger.agent-result/v1', operation, operation_ok: true, run_id: RUN, status: 'completed', execution_mode: 'paper', data: {}, summary: null, verification: null, limitations: [], evidence_refs: [], error: null, ...changes }; }
async function setup(t, { profile = UPGRADE_PROFILE, slot = null } = {}) {
  const path = await mkdtemp(join(tmpdir(), 'tiger-v3-sync-')); t.after(() => rm(path, { recursive: true, force: true }));
  const stateRoot = join(path, 'agent'), runDir = join(stateRoot, 'runs', RUN); await mkdir(runDir, { recursive: true });
  const fixture = researchV3Fixture(); fixture.business_evaluation.run_id = RUN;
  fixture.workflow_phase = profile.includes('premarket') ? 'premarket' : profile.includes('intraday') ? 'intraday' : 'daily';
  const request = { schema_version: 'tiger.agent-request/v1', profile, paper_account: 'public-usd', idempotency_key: `tigerpnl-research-${profile}-${DATE}${slot === null ? '' : `-${slot}`}` };
  const summary = { picks: [], watchlist: [], positions: [], pending: [], cash_usd: 100000, equity_usd: 100000, market_value_usd: 0,
    as_of: { market_date: DATE, market_timezone: 'America/New_York', admitted_at_utc: fixture.asOf, benchmark_last_bar: '2026-01-02', freshness: {} },
    disclosures: { synthetic: false, execution_mode: 'paper', model_mode: 'enabled', model_required: true, scan_stats: { universe_size: 10 } },
    model: fixture.model, action_summary: fixture.summary, execution_decisions: fixture.executionDecisions,
    ...Object.fromEntries(['candidate_research', 'opportunity_lifecycle', 'business_evaluation', 'portfolio_snapshot', 'execution_policy', 'workflow_phase', 'universe_coverage'].map(key => [key, fixture[key]])) };
  const source = { schema: 'tiger.source-manifest.v1', tree_sha256: 'c'.repeat(64), files: [] };
  const refs = [];
  for (const [name, value, schema] of [['request.json', request, 'tiger.agent-request/v1'], ['source-manifest.json', source, source.schema]]) { const bytes = Buffer.from(JSON.stringify(value)); await writeFile(join(runDir, name), bytes); refs.push({ path: name, sha256: digest(bytes.toString()), bytes: bytes.length, schema }); }
  const raw = { schema: 'tiger.receipt.v1', run_id: RUN, profile, paper_account: 'public-usd', status: 'completed', sealed_at_utc: '2026-01-03T21:01:00.123456+00:00', summary, source: refs[1] };
  const bytes = Buffer.from(JSON.stringify(raw)); await writeFile(join(runDir, 'receipt.json'), bytes); refs.push({ path: 'receipt.json', sha256: digest(bytes.toString()), bytes: bytes.length, schema: raw.schema });
  const verification = { ok: true, receipt_sha256: refs[2].sha256, evidence_count: 3, checks: ['SYNTHETIC-CONTRACT-ONLY'] };
  const receipt = envelope('receipt', { summary, verification, evidence_refs: refs });
  const verified = envelope('verify', { verification, evidence_refs: refs, data: { source: { tree_sha256: source.tree_sha256 } } });
  const env = { TIGER_AGENT_PYTHON: '/SYNTHETIC/python', TIGER_AGENT_STATE_ROOT: stateRoot, TIGER_AGENT_PERMISSION: 'paper', TIGER_PNL_SYNC_STATE_DIR: join(path, 'sync'), TIGER_PNL_WRITE_TOKEN: 'SYNTHETIC-PUBLICATION-KEY-NEVER-REAL-ONLY', LLM_BACKEND: 'api', LLM_BASE_URL: 'https://api.deepseek.com', LLM_MODEL: 'deepseek-flash', LLM_API_KEY: 'SYNTHETIC-MODEL-KEY-NEVER-REAL-ONLY' };
  const calls = [], payloads = [];
  const invoke = async (operation, input) => { calls.push({ operation, input }); if (operation === 'doctor') return envelope(operation, { status: 'ready', data: { access_mode: 'paper' } }); if (operation === 'describe') return envelope(operation, { status: 'ready', data: { access_mode: 'paper', profile: { profile: input.profile, execution: { model_mode: 'enabled' }, model_contract: { required: true, provider: 'deepseek', model: 'deepseek-flash', release: 'DeepSeek-V4.1-Flash-1017', search_mode: 'provider_native_search' } } } }); if (operation === 'run') return envelope(operation, { status: 'accepted' }); if (operation === 'receipt') return receipt; if (operation === 'verify') return verified; return envelope(operation); };
  const publish = async ({ record }) => { payloads.push(record); return { idempotencyKey: record.idempotencyKey, payloadHash: digest(record), hash: 'd'.repeat(64), sequence: 1 }; };
  return { path, stateRoot, runDir, raw, receipt, verified, env, calls, payloads, invoke, publish, request };
}
test('new default sync constructs schema3 from the exact source-bound receipt and independent verification', async t => {
  const f = await setup(t);
  const output = await syncResearch({ env: f.env, date: DATE }, { invoke: f.invoke, publish: f.publish });
  assert.equal(output.status, 'published_and_verified'); assert.equal(f.calls.find(row => row.operation === 'run').input.profile, UPGRADE_PROFILE);
  const payload = f.payloads[0]; assert.equal(payload.schemaVersion, 3); assert.deepEqual(payload.executionDecisions, f.receipt.summary.execution_decisions);
  assert.equal(payload.execution_policy, 'whitepaper-execution-v2'); assert.deepEqual(payload.business_evaluation, f.receipt.summary.business_evaluation);
  const saved = await readFile(join(f.env.TIGER_PNL_SYNC_STATE_DIR, `${UPGRADE_PROFILE}-${DATE}.json`), 'utf8');
  assert.equal(saved.includes(f.env.LLM_API_KEY), false); assert.equal(saved.includes(f.env.TIGER_PNL_WRITE_TOKEN), false);
  assert.equal((await syncResearch({ env: f.env, date: DATE }, { invoke: f.invoke, publish: f.publish })).status, 'already_published');
  assert.equal(f.calls.filter(row => row.operation === 'run').length, 1);
});
test('source tree mismatch, missing exact source ref and altered displayed business fields cannot publish', async t => {
  const f = await setup(t); const args = { receipt: f.receipt, verification: f.verified, stateRoot: f.stateRoot, profile: UPGRADE_PROFILE, idempotencyKey: 'SYNTHETIC-source-check' };
  f.verified.data.source.tree_sha256 = 'f'.repeat(64); await assert.rejects(buildResearch(args), { code: 'EVIDENCE_INVALID' });
  f.verified.data.source.tree_sha256 = 'c'.repeat(64); f.receipt.summary.business_evaluation.account.pnl_net = 999;
  await assert.rejects(buildResearch(args), { code: 'EVIDENCE_INVALID' });
  assert.equal(f.payloads.length, 0);
});
test('legacy model profile cannot start new research after v3 becomes the publication contract', async t => {
  const f = await setup(t);
  await assert.rejects(syncResearch({ env: f.env, date: DATE, profile: 'us-public-model-required-paper-v1' }, { invoke: f.invoke, publish: f.publish }), { code: 'MODEL_REQUIRED' });
  assert.equal(f.calls.length, 0); assert.equal(f.payloads.length, 0);
});


test('intraday scheduled slot persists one exact request and is idempotent across observer retries', async t => {
  const profile = 'us-public-model-required-intraday-paper-v2', slot = '1030';
  const f = await setup(t, { profile, slot });
  const options = { env: f.env, date: DATE, profile, slot };
  assert.equal((await syncResearch(options, { invoke: f.invoke, publish: f.publish })).status, 'published_and_verified');
  assert.equal((await syncResearch(options, { invoke: f.invoke, publish: f.publish })).status, 'already_published');
  const run = f.calls.find(row => row.operation === 'run');
  assert.equal(run.input.idempotency_key, `tigerpnl-research-${profile}-${DATE}-${slot}`);
  assert.equal(f.calls.filter(row => row.operation === 'run').length, 1);
  assert.equal(f.payloads[0].idempotencyKey, `pnl-${profile}-${DATE}-${slot}`);
  const state = JSON.parse(await readFile(join(f.env.TIGER_PNL_SYNC_STATE_DIR, `${profile}-${DATE}-${slot}.json`), 'utf8'));
  assert.equal(state.slot, slot); assert.deepEqual(state.request, run.input);
  assert.deepEqual(parseArgs(['--profile', profile, '--date', DATE, '--slot', slot]), { profile, date: DATE, slot });
});

test('malformed, legacy and outside-session intraday slots never submit a new logical task', async t => {
  const f = await setup(t);
  for (const [profile, slot] of [['us-public-model-required-paper-v1', '1030'], ['us-public-model-required-intraday-paper-v2', '0929'], ['us-public-model-required-intraday-paper-v2', '1600'], [UPGRADE_PROFILE, '2400'], [UPGRADE_PROFILE, '1060'], [UPGRADE_PROFILE, '10:30'], [UPGRADE_PROFILE, '../1030']]) {
    await assert.rejects(syncResearch({ env: f.env, date: DATE, profile, slot }, { invoke: f.invoke, publish: f.publish }), { code: 'INVALID_SYNC_REQUEST' });
  }
  assert.equal(f.calls.length, 0); assert.equal(f.payloads.length, 0);
});
