/**
 * MC-019: approval feedback memory isolation on real PostgreSQL with RLS enabled.
 * Opt in with RUN_MEMORY_SCOPE_PG_TESTS=1 and three loopback workloom_tiger_* URLs.
 * Fixtures are unique per test/run and retained for audit; no production seeds or models.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { EVOLUTION_MIN_SIGNAL_SAMPLES, MINER_PATTERN_THRESHOLD, type BusinessEvent } from "@workloom/shared";
import { decide, type GestureInput } from "./approvals.js";
import { gatewayAppend } from "../workdata/gateway.js";
import { getMemorySources, MockEmbedder, recordMemoryUsage, searchMemories, upsertMemory, type Embedder } from "../workdata/memory.js";
import { runMemoryMinerBeat } from "../evolve/memory-miner.js";
import { loadActivePreferences } from "../evolve/preference-inject.js";
import { registerFeedbackEnums, unregisterFeedbackEnums } from "../evolve/feedback-enums.js";

type Scope = { tenantId: string; workspaceId: string };
type Fixture = Scope & { approvalId: string; threadId: string; reviewEventId: string };
type MemoryRow = { memory_id: string; tenant_id: string; workspace_id: string; kind: string; source_events: string[]; content: string };
const suite = process.env.RUN_MEMORY_SCOPE_PG_TESTS === "1" ? describe : describe.skip;
const boss = { memberNo: "MEM-MEMORY-FIXTURE", role: "owner" as const };
const id = () => randomUUID().replaceAll("-", "");
const embedder = new MockEmbedder();
let owner: pg.Pool;
let app: pg.Pool;
let gateway: pg.Pool;
const noNetwork = vi.fn(async () => { throw new Error("MC019 tests forbid network/model requests"); });

function config(): Record<"owner" | "app" | "gateway", URL> {
  const values = { owner: process.env.DATABASE_URL, app: process.env.DATABASE_APP_URL, gateway: process.env.DATABASE_GATEWAY_URL };
  const result = {} as Record<keyof typeof values, URL>;
  for (const key of ["owner", "app", "gateway"] as const) {
    let url: URL;
    try { url = new URL(values[key] ?? ""); }
    catch { throw new Error(`MC019 requires a valid ${key} database URL`); }
    if (!["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error("MC019 requires a loopback workloom_tiger_ test database");
    }
    result[key] = url;
  }
  for (const key of ["app", "gateway"] as const) {
    if (result[key].hostname !== result.owner.hostname || result[key].port !== result.owner.port
      || result[key].pathname !== result.owner.pathname) throw new Error("MC019 pools must share one local test database");
  }
  return result;
}

async function scoped<T>(db: pg.Pool, scope: Scope, fn: (client: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.tenant_id',$1,true), set_config('app.workspace_id',$2,true)", [scope.tenantId, scope.workspaceId]);
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

async function newScope(overrides: Partial<Scope> = {}): Promise<Scope> {
  const scope = { tenantId: `tenant-mc019-${id()}`, workspaceId: `ws-mc019-${id()}`, ...overrides };
  await owner.query("INSERT INTO tenants (id,name) VALUES ($1,'Memory fixture') ON CONFLICT (id) DO NOTHING", [scope.tenantId]);
  await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'Memory fixture',$1,'test')", [scope.workspaceId, scope.tenantId]);
  return scope;
}

async function pending(scope: Scope, action = "record.update", expiresAt?: string): Promise<Fixture> {
  const threadId = `thread-mc019-${id()}`;
  const approvalId = `apr-mc019-${id()}`;
  await owner.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by,status) VALUES ($1,$2,$3,'Memory fixture','fixture','pending_review')", [threadId, scope.tenantId, scope.workspaceId]);
  const reviewed = await gatewayAppend(gateway, { ...scope, actor: { id: "system", type: "system" }, sessionId: threadId }, {
    who: { id: "system", type: "system" },
    context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
    object: { type: "record", id: threadId },
    decision: { action, kind: "review", step_id: "s1", params: { value: "fixture" } },
    rule_impact: [], receipt: { mode: "simulated", synced: true },
  });
  await owner.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,snapshot) VALUES ($1,$2,$3,$4,'pending',$5)", [approvalId, scope.tenantId, scope.workspaceId, reviewed.eventId, JSON.stringify({ expires_at: expiresAt ?? new Date(Date.now() + 3600000).toISOString() })]);
  return { ...scope, threadId, approvalId, reviewEventId: reviewed.eventId };
}

const reject = (fixture: Fixture, reasonEnum: string, customEmbedder: Embedder = embedder) =>
  decide(app, gateway, fixture, boss, fixture.approvalId, { type: "reject", reasonEnum }, customEmbedder);

async function memories(scope: Scope): Promise<MemoryRow[]> {
  return (await owner.query<MemoryRow>("SELECT * FROM org_memory WHERE tenant_id=$1 AND workspace_id=$2 ORDER BY memory_id", [scope.tenantId, scope.workspaceId])).rows;
}

async function state(scope: Scope) {
  const rows = async (table: "approvals" | "threads" | "org_memory" | "memory_usage" | "biz_events") =>
    (await owner.query<{ row: unknown }>(`SELECT to_jsonb(t) AS row FROM ${table} t WHERE workspace_id=$1 ORDER BY to_jsonb(t)::text`, [scope.workspaceId])).rows.map((r) => r.row);
  return { approvals: await rows("approvals"), threads: await rows("threads"), memories: await rows("org_memory"), usage: await rows("memory_usage"), events: await rows("biz_events") };
}

async function linkedMemory(scope: Scope, eventId: string): Promise<MemoryRow> {
  const rows = (await memories(scope)).filter((r) => r.source_events.includes(eventId));
  expect(rows).toHaveLength(1);
  const memory = rows[0]!;
  expect(memory).toMatchObject({ tenant_id: scope.tenantId, workspace_id: scope.workspaceId, kind: "preference" });
  const sources = await getMemorySources(app, scope, memory.memory_id);
  expect(sources.sourceEvents.map((e) => e.event_id)).toContain(eventId);
  expect(sources.usedBy).toContain(eventId);
  expect(sources.sourceEvents.every((e) => e.context.workspace_id === scope.workspaceId && e.context.tenant_id === scope.tenantId)).toBe(true);
  expect((await owner.query("SELECT memory_id,event_id,workspace_id FROM memory_usage WHERE memory_id=$1 AND event_id=$2", [memory.memory_id, eventId])).rows).toEqual([{ memory_id: memory.memory_id, event_id: eventId, workspace_id: scope.workspaceId }]);
  return memory;
}

async function assertPair(a: Scope, b: Scope, reason: string) {
  const aResult = await reject(await pending(a), reason);
  const bResult = await reject(await pending(b), reason);
  expect(aResult.status).toBe("rejected");
  expect(bResult.status).toBe("rejected");
  const aMemory = await linkedMemory(a, aResult.gestureEventId!);
  const bMemory = await linkedMemory(b, bResult.gestureEventId!);
  expect(aMemory.memory_id).not.toBe(bMemory.memory_id);
  expect(await getMemorySources(app, a, bMemory.memory_id)).toEqual({ memory: null, sourceEvents: [], usedBy: [] });
  expect(await getMemorySources(app, b, aMemory.memory_id)).toEqual({ memory: null, sourceEvents: [], usedBy: [] });
  return { aResult, bResult, aMemory, bMemory };
}

async function withInsertFailure(table: "biz_events" | "memory_usage", scope: Scope, fn: () => Promise<void>) {
  const name = `mc019_${id()}`; // Generated identifier; no caller-supplied SQL identifiers.
  const literal = scope.workspaceId.replaceAll("'", "''");
  await owner.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC019 injected write failure'; END $$`);
  try {
    await owner.query(`CREATE TRIGGER ${name} BEFORE INSERT ON ${table} FOR EACH ROW WHEN (NEW.workspace_id='${literal}') EXECUTE FUNCTION ${name}()`);
    await fn();
  } finally {
    await owner.query(`DROP TRIGGER IF EXISTS ${name} ON ${table}`);
    await owner.query(`DROP FUNCTION ${name}()`);
  }
}

suite("MC019 scoped feedback memory with real PostgreSQL", () => {
  beforeAll(async () => {
    const urls = config();
    const pool = (url: URL) => new pg.Pool({ connectionString: url.toString(), max: 8, connectionTimeoutMillis: 5000, statement_timeout: 15000 });
    owner = pool(urls.owner); app = pool(urls.app); gateway = pool(urls.gateway);
    for (const [db, role] of [[app, "workloom_app"], [gateway, "workloom_gateway"]] as const) {
      const result = await db.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user");
      expect(result.rows[0]).toMatchObject({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    const tables = await owner.query("SELECT relname,relrowsecurity FROM pg_class WHERE oid=ANY($1::regclass[]) ORDER BY relname", [["org_memory", "memory_usage", "approvals", "biz_events"]]);
    expect(tables.rows).toHaveLength(4);
    expect(tables.rows.every((r) => r.relrowsecurity === true)).toBe(true);
    vi.stubGlobal("fetch", noNetwork);
  });
  afterAll(async () => {
    vi.unstubAllGlobals();
    await Promise.all([owner?.end(), app?.end(), gateway?.end()]);
    expect(noNetwork).not.toHaveBeenCalled();
  });

  it("different tenants reject the same reason without collisions or visible foreign sources", async () => {
    await assertPair(await newScope(), await newScope(), `reason-${id()}`);
  });

  it("two workspaces of one tenant retain separate preference and usage rows", async () => {
    const a = await newScope();
    await assertPair(a, await newScope({ tenantId: a.tenantId }), `reason-${id()}`);
  });

  it.each([":", "-", "/", "|"])("scope components containing %s cannot collide through delimiter concatenation", async (separator) => {
    const run = id();
    const a = await newScope({ tenantId: `${run}${separator}a`, workspaceId: `b${separator}${run}${separator}c` });
    const b = await newScope({ tenantId: `${run}${separator}a${separator}b`, workspaceId: `${run}${separator}c` });
    expect(`${a.tenantId}${separator}${a.workspaceId}`).toBe(`${b.tenantId}${separator}${b.workspaceId}`);
    await assertPair(a, b, `reason-${id()}`);
  });

  it("quotes, slashes, Unicode, whitespace and long reasons retain exact feedback with bounded IDs", async () => {
    const scope = await newScope({ tenantId: `tenant:'"\\/${id()}`, workspaceId: `ws:\n|工作区/"'${id()}` });
    for (const reason of [`reason:'"\\/\n|中文-${id()}`, `long-${id()}-${"unicode_内容:".repeat(1000)}`]) {
      const result = await reject(await pending(scope), reason);
      const memory = await linkedMemory(scope, result.gestureEventId!);
      expect(memory.memory_id).toMatch(/^mem-scoped-v1-[a-f0-9]{64}$/);
      const gesture = await owner.query<{ payload: BusinessEvent }>("SELECT payload FROM biz_events WHERE event_id=$1", [result.gestureEventId]);
      expect((gesture.rows[0]!.payload.decision.after as { reason_enum: string }).reason_enum).toBe(reason);
      expect(memory.content).toContain(reason);
    }
    expect(await memories(scope)).toHaveLength(2);
  });

  it("separate approvals with the same scoped reason upsert once and retain all usage links", async () => {
    const scope = await newScope();
    const reason = `repeat-${id()}`;
    const first = await reject(await pending(scope), reason);
    const original = await linkedMemory(scope, first.gestureEventId!);
    const second = await reject(await pending(scope), reason);
    const updated = await linkedMemory(scope, second.gestureEventId!);
    expect(updated.memory_id).toBe(original.memory_id);
    expect(updated.source_events).toEqual([second.gestureEventId]);
    expect(await memories(scope)).toHaveLength(1);
    expect((await getMemorySources(app, scope, updated.memory_id)).usedBy.sort()).toEqual([first.gestureEventId, second.gestureEventId].sort());
  });

  it("a retry after rejection is deduped without another event, usage, or state change", async () => {
    const fixture = await pending(await newScope());
    const reason = `retry-${id()}`;
    const first = await reject(fixture, reason);
    const before = await state(fixture);
    expect(await reject(fixture, reason)).toEqual({ approvalId: fixture.approvalId, status: "rejected", deduped: true });
    expect(await decide(app, gateway, fixture, boss, fixture.approvalId, { type: "approve" })).toEqual({ approvalId: fixture.approvalId, status: "rejected", deduped: true });
    expect(await state(fixture)).toEqual(before);
    expect(first.deduped).toBe(false);
  });

  it("concurrent callbacks for one approval commit exactly one rejection and one usage", async () => {
    const fixture = await pending(await newScope());
    const reason = `callback-${id()}`;
    const results = await Promise.all(Array.from({ length: 8 }, () => reject(fixture, reason)));
    expect(results.filter((r) => !r.deduped)).toHaveLength(1);
    expect(results.every((r) => r.status === "rejected")).toBe(true);
    const snapshot = await state(fixture);
    expect(snapshot.memories).toHaveLength(1);
    expect(snapshot.usage).toHaveLength(1);
    expect(snapshot.events).toHaveLength(2);
  });

  it("concurrent approvals in one workspace share one memory without losing usage attribution", async () => {
    const scope = await newScope();
    const fixtures = await Promise.all(Array.from({ length: 8 }, () => pending(scope)));
    const reason = `concurrent-${id()}`;
    const results = await Promise.all(fixtures.map((fixture) => reject(fixture, reason)));
    const rows = await memories(scope);
    expect(rows).toHaveLength(1);
    const sources = await getMemorySources(app, scope, rows[0]!.memory_id);
    expect(sources.usedBy.sort()).toEqual(results.map((r) => r.gestureEventId).sort());
    expect(rows[0]!.source_events).toHaveLength(1);
    expect(sources.usedBy).toContain(rows[0]!.source_events[0]);
  });

  it("concurrent workspaces using the same reason each commit a private memory", async () => {
    const scopes = await Promise.all(Array.from({ length: 6 }, () => newScope()));
    const fixtures = await Promise.all(scopes.map((scope) => pending(scope)));
    const reason = `concurrent-cross-${id()}`;
    const settled = await Promise.allSettled(fixtures.map((fixture) => reject(fixture, reason)));
    expect(settled.map((r) => r.status)).toEqual(scopes.map(() => "fulfilled"));
    const ids = [];
    for (const scope of scopes) {
      const rows = await memories(scope);
      expect(rows).toHaveLength(1);
      ids.push(rows[0]!.memory_id);
    }
    expect(new Set(ids).size).toBe(scopes.length);
  });

  it("RLS and memory APIs hide foreign memories and reject a forged usage link", async () => {
    const a = await newScope();
    const b = await newScope({ tenantId: a.tenantId });
    const { aMemory, bMemory, bResult } = await assertPair(a, b, `rls-${id()}`);
    const visible = await scoped(app, a, (c) => c.query("SELECT memory_id FROM org_memory WHERE memory_id=ANY($1)", [[aMemory.memory_id, bMemory.memory_id]]));
    expect(visible.rows).toEqual([{ memory_id: aMemory.memory_id }]);
    expect((await searchMemories(app, a, { limit: 50 })).map((r) => r.memory_id)).toEqual([aMemory.memory_id]);
    expect((await loadActivePreferences(app, a)).map((r) => r.memoryId)).toEqual([aMemory.memory_id]);
    await expect(recordMemoryUsage(app, a, bMemory.memory_id, bResult.gestureEventId!)).rejects.toThrow(/归属/);
    const before = await state(b);
    const changed = await scoped(app, a, (c) => c.query("UPDATE org_memory SET content='forged' WHERE memory_id=$1", [bMemory.memory_id]));
    expect(changed.rowCount).toBe(0);
    expect(await state(b)).toEqual(before);
  });

  it("a foreign approval cannot be decided from another workspace", async () => {
    const fixture = await pending(await newScope());
    const other = await newScope();
    const before = await state(fixture);
    await expect(decide(app, gateway, other, boss, fixture.approvalId, { type: "reject", reasonEnum: "not_owned" })).rejects.toMatchObject({ code: "NOT_FOUND" });
    expect(await state(fixture)).toEqual(before);
    expect(await memories(other)).toEqual([]);
  });

  it("legacy rejection memory, usage, and historical event payloads remain byte-for-byte unchanged", async () => {
    const a = await newScope();
    const b = await newScope();
    const reason = `legacy-${id()}`;
    const legacyId = `mem-reject-${reason}`;
    const source = await pending(a);
    await upsertMemory(app, a, { memoryId: legacyId, scope: "workspace", kind: "preference", content: "Historical preference", sourceEvents: [source.reviewEventId], confidence: 0.8 }, embedder);
    await recordMemoryUsage(app, a, legacyId, source.reviewEventId);
    const snapshot = async () => (await owner.query("SELECT to_jsonb(m) AS memory, (SELECT jsonb_agg(to_jsonb(u) ORDER BY event_id) FROM memory_usage u WHERE u.memory_id=m.memory_id) AS usage, (SELECT payload FROM biz_events WHERE event_id=$2) AS event FROM org_memory m WHERE memory_id=$1", [legacyId, source.reviewEventId])).rows;
    const before = await snapshot();
    const { aMemory, bMemory } = await assertPair(a, b, reason);
    expect(aMemory.memory_id).not.toBe(legacyId);
    expect(bMemory.memory_id).not.toBe(legacyId);
    expect(await snapshot()).toEqual(before);
    expect((await getMemorySources(app, a, legacyId)).sourceEvents.map((e) => e.event_id)).toEqual([source.reviewEventId]);
    expect((await getMemorySources(app, b, legacyId)).memory).toBeNull();
    expect((await loadActivePreferences(app, a, { limit: 20 })).map((p) => p.memoryId).sort()).toEqual([legacyId, aMemory.memory_id].sort());
  });

  it("event write failure rolls back the approval and thread, then permits a clean retry", async () => {
    const fixture = await pending(await newScope());
    const reason = `event-failure-${id()}`;
    const before = await state(fixture);
    await withInsertFailure("biz_events", fixture, async () => {
      await expect(reject(fixture, reason)).rejects.toThrow("MC019 injected write failure");
    });
    expect(await state(fixture)).toEqual(before);
    const result = await reject(fixture, reason);
    await linkedMemory(fixture, result.gestureEventId!);
    expect((await owner.query("SELECT status FROM threads WHERE id=$1", [fixture.threadId])).rows[0]!.status).toBe("cancelled");
  });

  it("embedding failure after event append rolls back all writes and allows retry", async () => {
    const fixture = await pending(await newScope());
    const reason = `embedding-failure-${id()}`;
    const before = await state(fixture);
    const failing: Embedder = { embed: async () => { throw new Error("MC019 injected embedding failure"); } };
    await expect(reject(fixture, reason, failing)).rejects.toThrow("MC019 injected embedding failure");
    expect(await state(fixture)).toEqual(before);
    const retried = await reject(fixture, reason);
    await linkedMemory(fixture, retried.gestureEventId!);
  });

  it("usage write failure rolls back an existing memory upsert and all decision writes", async () => {
    const scope = await newScope();
    const reason = `usage-failure-${id()}`;
    await reject(await pending(scope), reason);
    const fixture = await pending(scope);
    const before = await state(scope);
    await withInsertFailure("memory_usage", scope, async () => {
      await expect(reject(fixture, reason)).rejects.toThrow("MC019 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    const retried = await reject(fixture, reason);
    await linkedMemory(scope, retried.gestureEventId!);
    expect((await state(scope)).usage).toHaveLength(2);
  });

  it.each([
    ["missing reason", { type: "reject" }, "EMPTY_REASON"],
    ["blank reason", { type: "reject", reasonEnum: " \n " }, "EMPTY_REASON"],
    ["long explanation", { type: "reject", reasonEnum: "reason", reasonText: "x".repeat(201) }, "REASON_TOO_LONG"],
    ["invalid gesture", { type: "bogus", reasonEnum: "reason" }, "INVALID_GESTURE"],
  ] as const)("%s is refused before any feedback writes", async (_name, gesture, code) => {
    const fixture = await pending(await newScope());
    const before = await state(fixture);
    await expect(decide(app, gateway, fixture, boss, fixture.approvalId, gesture as GestureInput)).rejects.toMatchObject({ code });
    expect(await state(fixture)).toEqual(before);
  });

  it("readonly and unknown registered reasons preserve the pending approval and zero memories", async () => {
    const fixture = await pending(await newScope());
    const before = await state(fixture);
    await expect(decide(app, gateway, fixture, { ...boss, role: "readonly" }, fixture.approvalId, { type: "reject", reasonEnum: "known.reason" })).rejects.toMatchObject({ code: "FORBIDDEN_ROLE" });
    registerFeedbackEnums(fixture.workspaceId, [{ code: "known.reason", label: "Known reason" }]);
    try {
      await expect(reject(fixture, "unknown.reason")).rejects.toMatchObject({ code: "UNKNOWN_REASON_ENUM" });
    } finally { unregisterFeedbackEnums(fixture.workspaceId); }
    expect(await state(fixture)).toEqual(before);
  });

  it("expired rejection records expiry without a feedback memory or gesture event", async () => {
    const fixture = await pending(await newScope(), "record.update", new Date(Date.now() - 1000).toISOString());
    await expect(reject(fixture, "expired.reason")).rejects.toMatchObject({ code: "EXPIRED" });
    expect((await owner.query("SELECT status FROM approvals WHERE approval_id=$1", [fixture.approvalId])).rows[0]!.status).toBe("expired");
    const snapshot = await state(fixture);
    expect(snapshot.memories).toEqual([]);
    expect(snapshot.usage).toEqual([]);
    expect(snapshot.events).toHaveLength(1);
  });

  it("miner reinforces the same scoped rejection memories and separates edit patterns across workspaces", async () => {
    const a = await newScope();
    const b = await newScope({ tenantId: a.tenantId });
    const key = `record.same-${id()}`;
    const legacyId = `mem-pat-edit-${key}`;
    await upsertMemory(app, a, { memoryId: legacyId, scope: "workspace", kind: "pattern", content: "Historical edit pattern", sourceEvents: [], confidence: 0.4 }, embedder);
    const legacyBefore = (await owner.query("SELECT to_jsonb(m) AS row FROM org_memory m WHERE memory_id=$1", [legacyId])).rows;
    const newIds: string[] = [];
    for (const scope of [a, b]) {
      const rejectEvents: string[] = [];
      for (let i = 0; i < EVOLUTION_MIN_SIGNAL_SAMPLES; i++) {
        rejectEvents.push((await reject(await pending(scope), key)).gestureEventId!);
      }
      const prior = await linkedMemory(scope, rejectEvents.at(-1)!);
      for (let i = 0; i < MINER_PATTERN_THRESHOLD; i++) {
        const fixture = await pending(scope, key);
        await decide(app, gateway, scope, boss, fixture.approvalId, { type: "edit", editedAfter: { value: "updated" }, editKind: "preference" });
      }
      const mined = await runMemoryMinerBeat(app, gateway, scope, { embedder });
      expect(mined).toMatchObject({ reinforced: 1, editPatterns: 1, samples: EVOLUTION_MIN_SIGNAL_SAMPLES + MINER_PATTERN_THRESHOLD });
      expect(mined.calibrateEventIds).toHaveLength(2);
      const rows = (await memories(scope)).filter((m) => m.memory_id !== legacyId);
      expect(rows).toHaveLength(2);
      const pref = rows.find((m) => m.kind === "preference")!;
      const pattern = rows.find((m) => m.kind === "pattern")!;
      expect(pref.memory_id).toBe(prior.memory_id);
      expect(new Set(pref.source_events)).toEqual(new Set(rejectEvents));
      expect(pref.memory_id).not.toBe(pattern.memory_id);
      expect(pattern.content).toContain(`口味 ${MINER_PATTERN_THRESHOLD}`);
      for (const calibrationId of mined.calibrateEventIds) {
        const ev = (await owner.query<{ payload: BusinessEvent }>("SELECT payload FROM biz_events WHERE event_id=$1", [calibrationId])).rows[0]!.payload;
        expect(ev.context).toMatchObject({ tenant_id: scope.tenantId, workspace_id: scope.workspaceId });
        expect(rows.map((m) => m.memory_id)).toContain(ev.object.id);
      }
      expect((await getMemorySources(app, scope, pref.memory_id)).usedBy).toHaveLength(EVOLUTION_MIN_SIGNAL_SAMPLES);
      newIds.push(pref.memory_id, pattern.memory_id);
    }
    expect(new Set(newIds).size).toBe(4);
    expect((await owner.query("SELECT to_jsonb(m) AS row FROM org_memory m WHERE memory_id=$1", [legacyId])).rows).toEqual(legacyBefore);
  });

  it("miner calibration event failure rolls back reinforced memory and can be retried", async () => {
    const scope = await newScope();
    const reason = `miner-failure-${id()}`;
    for (let i = 0; i < EVOLUTION_MIN_SIGNAL_SAMPLES; i++) await reject(await pending(scope), reason);
    const before = await state(scope);
    await withInsertFailure("biz_events", scope, async () => {
      await expect(runMemoryMinerBeat(app, gateway, scope, { embedder })).rejects.toThrow("MC019 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    expect(await runMemoryMinerBeat(app, gateway, scope, { embedder })).toMatchObject({ reinforced: 1, editPatterns: 0 });
    expect(await memories(scope)).toHaveLength(1);
  });
});
