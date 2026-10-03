import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { approvalSnapshot, reviewContext, sha256, validateApproval, verifyExecution } from "./proposal-bridge-contract.mjs";
import { pull, push } from "./proposal-bridge.ts";

function fixture(status = "approved") {
  const root = mkdtempSync(join(tmpdir(), "tiger-approval-contract-"));
  const kernel = join(root, "kernel");
  const directory = join(root, "reports", "review_proposals");
  mkdirSync(join(kernel, "trading_system"), { recursive: true });
  mkdirSync(directory, { recursive: true });
  writeFileSync(join(kernel, "main.py"), "print('fixture')\n");
  writeFileSync(join(kernel, "trading_system", "config.py"), "DEMO=True\n");
  const context = reviewContext({ kernelRoot: kernel, pythonExe: process.execPath, proposalsDir: directory });
  const file = join(directory, "PROP-AUDIT.json");
  const proposal = { proposal_id: "PROP-AUDIT", status: "pending_review", verdict: "pending_review", created_at: new Date().toISOString(), dsr: 0.99, oos_expectancy: 1.2, grid_result: { recommended_params: { mrs_go: 6, cap: 0.2 } } };
  writeFileSync(file, JSON.stringify(proposal));
  const snapshot = approvalSnapshot(context, proposal.proposal_id, Date.now() - 1000);
  const row = { approval_id: proposal.proposal_id, tenant_id: "tiger", workspace_id: "trading", status,
    snapshot, decided_by: "AUDIT-HUMAN", decided_at: new Date().toISOString(), gesture: { type: status === "approved" ? "approve" : "reject", ...(status === "rejected" ? { reason_enum: "insufficient_evidence", reason_text: "需重跑" } : {}) } };
  return { root, context, file, proposal, row, cleanup: () => rmSync(root, { recursive: true, force: true }) };
}

function completed(f, row, expected) {
  const p = { ...f.proposal, status: row.status, execution_id: expected.executionId, preimage_sha256: row.snapshot.proposal_sha256, reason: expected.reason, effective_from: "2026-10-03" };
  writeFileSync(f.file, JSON.stringify(p));
  mkdirSync(join(f.context.outDir, "review_executions"), { recursive: true });
  let effect = null;
  if (row.status === "approved") {
    effect = JSON.stringify({ proposal_id: row.approval_id, execution_id: expected.executionId, params: p.grid_result.recommended_params, effective_from: p.effective_from });
    writeFileSync(join(f.context.outDir, "tuned_params.json"), effect);
    writeFileSync(join(f.context.outDir, "review_executions", `${expected.executionId}.effect.json`), effect);
  }
  writeFileSync(join(f.context.outDir, "review_executions", `${expected.executionId}.json`), JSON.stringify({ proposal_id: row.approval_id, execution_id: expected.executionId, preimage_sha256: row.snapshot.proposal_sha256, status: row.status, effective_from: p.effective_from,
    tuned_path: effect ? join(f.context.outDir, "review_executions", `${expected.executionId}.effect.json`) : null, tuned_sha256: effect ? sha256(effect) : null, reason: expected.reason }));
}

function poolFor(row) {
  const state = { row, locked: false, updates: 0, events: 0, failCommit: false, inserted: false };
  const pool = { async connect() {
    let ownsLock = false;
    let previous = null;
    const release = () => { if (ownsLock) state.locked = false; ownsLock = false; };
    return { release, async query(sql, params = []) {
      if (sql.startsWith("SELECT approval_id")) return { rows: state.row ? [{ approval_id: state.row.approval_id }] : [] };
      if (sql.includes("SELECT * FROM approvals")) {
        if (state.row?.gesture?.executed || state.locked) return { rows: [] };
        state.locked = true; ownsLock = true; previous = structuredClone(state.row);
        return { rows: state.row ? [structuredClone(state.row)] : [] };
      }
      if (sql.startsWith("UPDATE approvals")) {
        assert.equal(ownsLock, true); assert.equal(params[2], "tiger"); assert.equal(params[3], "trading");
        state.row.gesture = { ...state.row.gesture, ...JSON.parse(params[0]) }; state.updates++;
        return { rows: [{ approval_id: row.approval_id }], rowCount: 1 };
      }
      if (sql.startsWith("INSERT INTO approvals")) { state.inserted = true; state.row = { approval_id: params[0], snapshot: JSON.parse(params[4]), gesture: {} }; return { rows: [], rowCount: 1 }; }
      if (sql === "COMMIT") { if (state.failCommit && ownsLock) { state.failCommit = false; throw new Error("DB commit fault"); } release(); }
      if (sql === "ROLLBACK") { if (ownsLock) state.row = previous; release(); }
      return { rows: [], rowCount: 0 };
    } };
  } };
  const append = async () => { state.events++; return { eventId: `E-${state.events}`, hash: "audit-event-hash" }; };
  return { pool, state, append };
}

const invalidCases = [
  ["foreign kind", (r) => { r.snapshot.kind = "publish.unrelated"; }],
  ["changed raw hash", (r) => { r.snapshot.proposal_sha256 = "a".repeat(64); }],
  ["expired snapshot", (r) => { r.snapshot.expires_at = "2000-01-01T00:00:00Z"; }],
  ["consumed approval", (r) => { r.gesture.executed = true; }],
  ["wrong directory", (r) => { r.snapshot.proposals_dir = "/other/review_proposals"; }],
  ["wrong tenant", (r) => { r.tenant_id = "another-tenant"; }],
  ["wrong workspace", (r) => { r.workspace_id = "another-workspace"; }],
  ["live environment", (r) => { r.snapshot.environment = "live"; }],
  ["changed config", (r) => { r.snapshot.config_sha256 = "f".repeat(64); }],
  ["missing actor", (r) => { r.decided_by = ""; }],
  ["edited parameters", (r) => { r.status = "edited"; r.gesture.type = "edit"; r.gesture.edited_after = { mrs_go: 8 }; }],
];
for (const [name, mutate] of invalidCases) test(`approval bridge refuses ${name} without kernel call or executed marker`, async () => {
  const f = fixture();
  try {
    mutate(f.row);
    const mock = poolFor(f.row);
    let calls = 0;
    let rejection = null;
    try { await pull(mock.pool, f.context, async () => { calls++; }, mock.append); }
    catch (error) { rejection = error; }
    if (name !== "consumed approval") assert.ok(rejection instanceof Error);
    assert.equal(calls, 0);
    assert.equal(mock.state.updates, 0);
    assert.equal(mock.state.events, 0);
    assert.equal(JSON.parse(readFileSync(f.file)).status, "pending_review");
  } finally { f.cleanup(); }
});

test("changed proposal bytes/parameters and symlink replacements are rejected", () => {
  const f = fixture();
  try {
    writeFileSync(f.file, JSON.stringify(f.proposal, null, 2));
    assert.throws(() => validateApproval(f.row, f.context), /已变化/);
    writeFileSync(f.file, JSON.stringify({ ...f.proposal, grid_result: { recommended_params: { mrs_go: 7 } } }));
    assert.throws(() => validateApproval(f.row, f.context), /参数摘要/);
    rmSync(f.file); writeFileSync(join(f.root, "outside.json"), JSON.stringify(f.proposal)); symlinkSync(join(f.root, "outside.json"), f.file);
    assert.throws(() => validateApproval(f.row, f.context), /符号链接/);
  } finally { f.cleanup(); }
});

test("approval rejects a claimed success unless final proposal and tuned bytes match", async () => {
  const f = fixture();
  try {
    const mock = poolFor(f.row);
    await assert.rejects(pull(mock.pool, f.context, async () => {}, mock.append), /回执/);
    assert.equal(mock.state.updates, 0);
    assert.equal(mock.state.events, 0);
  } finally { f.cleanup(); }
});

test("concurrent pulls consume one approved snapshot and produce a verified receipt", async () => {
  const f = fixture();
  try {
    const mock = poolFor(f.row);
    let calls = 0;
    const execute = async (row, _context, expected) => { calls++; await new Promise((resolve) => setTimeout(resolve, 25)); completed(f, row, expected); };
    const counts = await Promise.all([pull(mock.pool, f.context, execute, mock.append), pull(mock.pool, f.context, execute, mock.append)]);
    assert.equal(counts.reduce((a, b) => a + b, 0), 1);
    assert.equal(calls, 1); assert.equal(mock.state.updates, 1); assert.equal(mock.state.events, 1);
    assert.equal(mock.state.row.gesture.execution_receipt.environment, "paper");
    assert.match(mock.state.row.gesture.execution_receipt.tuned.sha256, /^[a-f0-9]{64}$/);
  } finally { f.cleanup(); }
});

test("DB commit failure recovers matching kernel receipt without re-executing", async () => {
  const f = fixture();
  try {
    const mock = poolFor(f.row); mock.state.failCommit = true;
    let calls = 0;
    const execute = async (row, _context, expected) => { calls++; completed(f, row, expected); };
    await assert.rejects(pull(mock.pool, f.context, execute, mock.append), /commit fault/);
    assert.equal(mock.state.row.gesture.executed, undefined);
    assert.equal(await pull(mock.pool, f.context, execute, mock.append), 1);
    assert.equal(calls, 1);
    assert.equal(mock.state.row.gesture.execution_receipt.recovered, true);
  } finally { f.cleanup(); }
});

test("reject uses controlled reason and needs a verified rejected proposal", async () => {
  const f = fixture("rejected");
  try {
    const mock = poolFor(f.row);
    assert.equal(await pull(mock.pool, f.context, async (row, _ctx, expected) => completed(f, row, expected), mock.append), 1);
    assert.equal(JSON.parse(readFileSync(f.file)).reason, "insufficient_evidence：需重跑");
    assert.equal(mock.state.row.gesture.execution_receipt.tuned, null);
  } finally { f.cleanup(); }
});

test("missing reject reason, relative runtime and code-directory outputs fail closed", () => {
  const f = fixture("rejected");
  try {
    f.row.gesture.reason_enum = "";
    assert.throws(() => validateApproval(f.row, f.context), /原因枚举/);
    assert.throws(() => reviewContext({ kernelRoot: ".", pythonExe: process.execPath, proposalsDir: f.context.proposalsDir }), /绝对/);
    mkdirSync(join(f.context.kernelRoot, "review_proposals"));
    assert.throws(() => reviewContext({ kernelRoot: f.context.kernelRoot, pythonExe: process.execPath, proposalsDir: join(f.context.kernelRoot, "review_proposals") }), /代码目录/);
  } finally { f.cleanup(); }
});

test("push writes event and immutable snapshot in one transaction and is idempotent", async () => {
  const f = fixture();
  try {
    const mock = poolFor(null);
    assert.equal(await push(mock.pool, f.context, mock.append), 1);
    assert.equal(mock.state.events, 1); assert.equal(mock.state.inserted, true);
    assert.equal(await push(mock.pool, f.context, mock.append), 0);
    assert.equal(mock.state.events, 1);
    assert.equal(mock.state.row.snapshot.high_risk, true);
  } finally { f.cleanup(); }
});
