import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { canonical, digest, validateResearch } from '../lib/schema.mjs';
import { ResearchLedger } from '../lib/ledger.mjs';
import { publicResearch } from '../lib/projection.mjs';
import { modelResearchFixture, modelFixture } from './model-fixture.mjs';
import { researchV3Fixture, modelV2Fixture } from './short-term-fixture.mjs';

// Finite synthetic contract cases. Never written to a public/runtime ledger.
test('schema3 admits complete six-stage technical research without inventing issuer news', () => {
  assert.doesNotThrow(() => validateResearch(researchV3Fixture()));
});
for (const [name, change] of [
  ['historical five-stage model in new schema3', r => { r.model = modelFixture(); }],
  ['missing research stage', r => { delete r.model.stage_counts['research.candidates']; }],
  ['unsettled usage', r => { r.model.budget.records[0].status = 'unknown'; }],
  ['inconsistent token use', r => { r.model.budget.actual_tokens++; }],
  ['missing thesis', r => { delete r.executionDecisions[0].thesis; }],
  ['insufficient buy', r => { r.executionDecisions[0].thesis.status = 'insufficient'; }],
  ['unbound own market', r => { r.executionDecisions[0].thesis.five_part.stock.evidence_ids = ['fx_TEST_stock']; }],
  ['event buy without primary source', r => { r.executionDecisions[0].thesis.strategy = 'news_event_v2'; }],
  ['raw document', r => { r.candidate_research.content = 'PRIVATE ARTICLE MUST NEVER PUBLISH'; }],
  ['raw model', r => { r.candidate_research.model_rounds = ['PRIVATE MODEL']; }],
  ['future portfolio mark', r => { r.portfolio_snapshot.known_at = '2026-01-05'; }],
  ['invented real profit', r => { r.business_evaluation.account.profitability_established = true; }],
]) test(`schema3 rejects ${name}`, () => {
  const r = researchV3Fixture(); change(r); assert.throws(() => validateResearch(r), { status: 422 });
});
test('new schema3 accepts microsecond observation times and dynamic Fetch counts', () => {
  const r = researchV3Fixture({ asOf: '2026-01-03T21:01:00Z', model: modelV2Fixture({ searchCount: 17, fetchCount: 1 }) }); r.business_evaluation.as_of = '2026-01-03T21:00:00.123456+00:00';
  assert.doesNotThrow(() => validateResearch(r));
});
test('public schema3 projection preserves reviewed business fields and cannot leak unknown raw source fields', () => {
  const r = researchV3Fixture(); r.candidate_research.document_content = 'PRIVATE';
  const out = publicResearch({ record: r, sequence: 1, hash: digest('SYNTHETIC ENTRY') });
  assert.equal(out.schemaVersion, 3); assert.deepEqual(out.executionDecisions, r.executionDecisions);
  assert.equal(JSON.stringify(out).includes('PRIVATE'), false);
  assert.deepEqual(out.business_evaluation, r.business_evaluation);
});
test('v1 and v2 immutable records retain hashes and retries while only schema3 can append', async t => {
  const path = await mkdtemp(join(tmpdir(), 'tiger-v3-contract-')); t.after(() => rm(path, { recursive: true, force: true }));
  const old = modelResearchFixture(); await mkdir(join(path, 'records'));
  const body = { sequence: 1, acceptedAt: '2026-01-03T22:00:00Z', previousHash: '0'.repeat(64), payloadHash: digest(old), record: old };
  const entry = { ...body, hash: digest(body) }; const file = join(path, 'records', '000000000001.json');
  await writeFile(file, canonical(entry)); const bytes = await readFile(file);
  const ledger = await ResearchLedger.open(path);
  try {
    assert.equal((await ledger.append(old)).replayed, true);
    await assert.rejects(ledger.append({ ...old, idempotencyKey: 'new-legacy-forbidden', runId: 'other-old-run', asOf: '2026-01-04T21:00:00Z' }), { code: 'MODEL_REQUIRED' });
    const r = researchV3Fixture({ idempotencyKey: 'schema3-new-research', runId: 'new-v3-run', asOf: '2026-01-04T21:00:00Z' });
    r.decisions.forEach(d => { d.runId = r.runId; d.decidedAt = r.asOf; }); r.business_evaluation.run_id = r.runId;
    assert.equal((await ledger.append(r)).previousHash, entry.hash);
    assert.deepEqual(await readFile(file), bytes);
  } finally { await ledger.close(); }
});

function primaryEventResearch() {
  const r = researchV3Fixture(), id = 'SYNTHETIC-primary-AAPL';
  const contentHash = digest('SYNTHETIC PRIMARY SOURCE BODY ONLY');
  r.candidate_research.source_index = [{ id, title: 'SYNTHETIC issuer announcement', url: 'https://example.com/synthetic-primary', published_at: '2026-01-02T16:00:00Z', fetched_at: 1767384000, source: 'deepseek_fetch', full_content_characters: 60, presented_content_characters: 60, content_complete: true, original_content_sha256: contentHash, presented_content_sha256: contentHash, search_source_id: null, target_symbols: ['AAPL'], presentations: [{ symbol: 'AAPL', presented_content_characters: 60, content_complete: true, presented_content_sha256: contentHash }] }];
  const thesis = r.executionDecisions[0].thesis;
  Object.assign(thesis, { strategy: 'news_event_v2', issuer_name: 'SYNTHETIC ISSUER', issuer_identity_evidence_ids: [id], claims: [{ claim_id: 'SYNTHETIC-current-primary', statement: 'SYNTHETIC current issuer fact', source_id: id, quote: 'SYNTHETIC issuer announcement evidence', origin_id: id, evidence_kind: 'primary', time_role: 'current' }] });
  r.candidate_research.theses = [structuredClone(thesis)];
  return r;
}
test('event strategy requires the assessed issuer and a current complete primary presentation', () => {
  assert.doesNotThrow(() => validateResearch(primaryEventResearch()));
  for (const mutate of [
    r => { r.candidate_research.source_index[0].target_symbols = ['MSFT']; },
    r => { r.candidate_research.source_index[0].presentations[0].symbol = 'MSFT'; },
    r => { r.candidate_research.source_index[0].published_at = '2025-12-20T00:00:00Z'; },
    r => { r.candidate_research.source_index[0].published_at = '2026-01-04T00:00:00Z'; },
    r => { r.candidate_research.source_index[0].published_at = null; },
    r => { const s = r.candidate_research.source_index[0]; s.presented_content_characters = 30; s.content_complete = false; s.presentations[0].presented_content_characters = 30; s.presentations[0].content_complete = false; },
  ]) { const r = primaryEventResearch(); mutate(r); assert.throws(() => validateResearch(r), { status: 422 }); }
});
