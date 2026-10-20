/**
 * MC-014: real PostgreSQL migration/runtime contract regressions.
 * Opt in with RUN_RUNTIME_SCHEMA_PG_TESTS=1 and three local workloom_tiger_* DB URLs.
 * The configured database must already be migrated. Migration cases create/drop only
 * uniquely named databases created by this suite; runtime cases use isolated fixtures.
 * No model requests or external tools are used. All execution receipts are simulated.
 */
import { createHash, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { approvals, threads } from "@workloom/db/schema";
import { APPROVAL_STATUSES, THREAD_STATUSES, type BusinessEvent } from "@workloom/shared";
import { decide } from "@workloom/base/review-console";
import { gatewayAppend, gatewayAppendOnClient, eventHash, type EventDraft } from "@workloom/base/workdata";
import { runQuest, type QuestStep } from "./loop.js";
import { insertWithReadableId, THREAD_ID_SOURCE } from "@workloom/base/workdata";
import { dispatchFromAnomaly } from "@workloom/base/inspection";

const enabled = process.env.RUN_RUNTIME_SCHEMA_PG_TESTS === "1";
const suite = enabled ? describe : describe.skip;
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const migrationsDir = join(root, "packages/db/migrations");
const migrationName = "0036_runtime_persistence.sql";
type Scope = { tenantId: string; workspaceId: string };
let owner: pg.Pool;
let app: pg.Pool;
let gateway: pg.Pool;
let urls: Record<"owner" | "app" | "gateway", URL>;
const id = () => randomUUID().replaceAll("-", "");
const scope: Scope = { tenantId: `tenant-runtime-${id()}`, workspaceId: `ws-runtime-${id()}` };
const other: Scope = { tenantId: `tenant-runtime-${id()}`, workspaceId: `ws-runtime-${id()}` };

function readConfig(): typeof urls {
  const values = {
    owner: process.env.DATABASE_URL,
    app: process.env.DATABASE_APP_URL,
    gateway: process.env.DATABASE_GATEWAY_URL,
  };
  const result = {} as typeof urls;
  for (const key of ["owner", "app", "gateway"] as const) {
    if (!values[key]) throw new Error(`Runtime schema PG tests require the ${key} database URL`);
    let url: URL;
    try { url = new URL(values[key]!); } catch { throw new Error(`Invalid ${key} database URL`); }
    if (!["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error("Runtime schema PG tests require a loopback workloom_tiger_ test database");
    }
    result[key] = url;
  }
  for (const key of ["app", "gateway"] as const) {
    if (result[key].hostname !== result.owner.hostname || result[key].port !== result.owner.port
      || result[key].pathname !== result.owner.pathname) {
      throw new Error("Runtime schema PG test pools must use the same local database");
    }
  }
  return result;
}

function pool(url: URL): pg.Pool {
  return new pg.Pool({ connectionString: url.toString(), max: 8, connectionTimeoutMillis: 5000, statement_timeout: 10000 });
}

async function scoped<T>(db: pg.Pool, ctx: Scope, fn: (client: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await db.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.tenant_id', $1, true), set_config('app.workspace_id', $2, true)", [ctx.tenantId, ctx.workspaceId]);
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

async function seedWorkspace(db: pg.Pool, ctx: Scope): Promise<void> {
  await db.query("INSERT INTO tenants (id, name) VALUES ($1, 'Runtime schema fixture')", [ctx.tenantId]);
  await db.query("INSERT INTO workspaces (id, tenant_id, name, slug, industry, stage) VALUES ($1,$2,'Runtime schema fixture',$1,'test','stable')", [ctx.workspaceId, ctx.tenantId]);
  await db.query("INSERT INTO profiles (workspace_id, tenant_id, industry, archive) VALUES ($1,$2,'test',$3)", [ctx.workspaceId, ctx.tenantId, JSON.stringify({ dataMode: "simulated" })]);
  for (const highRisk of [false, true]) {
    await db.query(
      `INSERT INTO agents (id, workspace_id, preset_key, name, version, kind, fence_bindings, meta)
       VALUES ($1,$2,$3,'Runtime fixture','v1','test',$4,$5)`,
      [`agent-${id()}`, ctx.workspaceId, highRisk ? "runtime-high-risk" : "runtime-test", JSON.stringify(["R-runtime-auto", "R-runtime-review"]), JSON.stringify({ high_risk: highRisk, tools: [
        { name: "metrics.read", access: "read", desc: "Read fixture metrics" },
        { name: "record.write", access: "write", desc: "Write fixture record" },
        { name: "manual.write", access: "write", desc: "Review fixture record" },
      ] })],
    );
  }
  for (const [action, level] of [["record.write", "auto"], ["manual.write", "review"]]) {
    await db.query(
      `INSERT INTO fence_rules (id, rule_id, version, workspace_id, name, level, match_spec, action, status, created_by)
       VALUES ($1,$2,'v1',$3,'Runtime fixture',$4,$5,$6,'active','fixture')`,
      [`rule-${id()}`, `R-runtime-${level}`, ctx.workspaceId, level,
        JSON.stringify({ object_types: ["record"], actions: [action], when: "true" }), JSON.stringify({ result: level })],
    );
  }
}

async function newThread(ctx = scope): Promise<string> {
  const threadId = `runtime-${id()}`;
  await owner.query("INSERT INTO threads (id, tenant_id, workspace_id, title, created_by) VALUES ($1,$2,$3,'Runtime persistence fixture','fixture')", [threadId, ctx.tenantId, ctx.workspaceId]);
  return threadId;
}

function step(tool = "record.write", value: unknown = "initial"): QuestStep {
  return { stepId: "s1", action: tool, objectType: "record", objectId: "fixture-record", tool, params: { value }, label: "Runtime record" };
}

function input(threadId: string, steps = [step()]) {
  return { threadId, goal: "Runtime record", presetKey: "runtime-test", fallbackPlanner: () => structuredClone(steps) };
}

async function state(threadId: string) {
  return (await owner.query("SELECT * FROM threads WHERE id=$1", [threadId])).rows[0]!;
}

async function events(threadId: string): Promise<BusinessEvent[]> {
  return (await owner.query<{ payload: BusinessEvent }>("SELECT payload FROM biz_events WHERE session_id=$1 ORDER BY seq", [threadId])).rows.map((row) => row.payload);
}

async function approval(snapshot: Record<string, unknown>, status = "approved", ctx = scope): Promise<string> {
  const approvalId = `apr-runtime-${id()}`;
  await owner.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,snapshot) VALUES ($1,$2,$3,$4,$5,$6)", [approvalId, ctx.tenantId, ctx.workspaceId, `E-FIXTURE-${id()}`, status, JSON.stringify(snapshot)]);
  return approvalId;
}

const bound = () => ({ action: "record.write", object_type: "record", object_id: "fixture-record", expires_at: new Date(Date.now() + 3600_000).toISOString() });
const highRiskActor = { id: "runtime-high-risk", type: "agent" as const, highRisk: true, fenceBindings: ["R-runtime-auto"] };
function draft(): EventDraft {
  return {
    who: { id: highRiskActor.id, type: "agent", version: "v1" },
    context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
    object: { type: "record", id: "fixture-record" },
    decision: { action: "record.write", kind: "execute", params: { value: "fixture" } },
    rule_impact: [], receipt: { synced: true, mode: "simulated" },
  };
}

async function temporaryDatabase<T>(label: string, fn: (db: pg.Pool, url: URL) => Promise<T>): Promise<T> {
  // The fixed prefix and random suffix are generated here, never accepted from a caller.
  const name = `workloom_tiger_schema_${label}_${id().slice(0, 12)}`;
  await owner.query(`CREATE DATABASE "${name}"`);
  const url = new URL(urls.owner);
  url.pathname = `/${name}`;
  const db = pool(url);
  try { return await fn(db, url); }
  finally {
    await db.end();
    await owner.query(`DROP DATABASE "${name}"`);
  }
}

function migrate(url: URL): { status: number | null; log: string } {
  const env = {
    ...process.env, DATABASE_URL: url.toString(),
    APP_DB_PASSWORD: decodeURIComponent(urls.app.password),
    GATEWAY_DB_PASSWORD: decodeURIComponent(urls.gateway.password),
  };
  const result = spawnSync(process.execPath, ["--import", "tsx", "scripts/migrate.ts"], { cwd: root, env, encoding: "utf8", timeout: 30000 });
  if (result.error) throw new Error(`Migration subprocess failed: ${result.error.name}`);
  // Migration logs are deterministic file names/errors. Still redact all connection values.
  let log = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  for (const value of [url.toString(), env.APP_DB_PASSWORD, env.GATEWAY_DB_PASSWORD].filter(Boolean)) log = log.split(value).join("[REDACTED]");
  return { status: result.status, log };
}

async function oldSchema(db: pg.Pool, before = migrationName): Promise<string[]> {
  const files = readdirSync(migrationsDir).filter((name) => name.endsWith(".sql") && name < before).sort();
  await db.query("CREATE TABLE _migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), checksum TEXT)");
  for (const file of files) {
    const sql = readFileSync(join(migrationsDir, file), "utf8");
    const client = await db.connect();
    try {
      await client.query("BEGIN");
      await client.query(sql);
      await client.query("INSERT INTO _migrations (name, checksum) VALUES ($1,$2)", [file, createHash("sha256").update(sql).digest("hex")]);
      await client.query("COMMIT");
    } catch (error) { await client.query("ROLLBACK"); throw error; }
    finally { client.release(); }
  }
  return files;
}

suite("MC-014 real PostgreSQL runtime schema", () => {
  beforeAll(async () => {
    urls = readConfig();
    owner = pool(urls.owner); app = pool(urls.app); gateway = pool(urls.gateway);
    const roles = await app.query<{ current_user: string; rolsuper: boolean; rolbypassrls: boolean }>("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user");
    expect(roles.rows[0]).toMatchObject({ current_user: "workloom_app", rolsuper: false, rolbypassrls: false });
    await seedWorkspace(owner, scope);
    await seedWorkspace(owner, other);
  });
  afterAll(async () => { await Promise.all([owner?.end(), app?.end(), gateway?.end()]); });

  it("declares nullable unplanned state, zero initial version, nullable unconsumed approval, and matching type fields", async () => {
    const threadId = await newThread();
    expect((await owner.query("SELECT plan, plan_version FROM threads WHERE id=$1", [threadId])).rows[0]).toEqual({ plan: null, plan_version: 0 });
    const approvalId = await approval(bound());
    expect((await owner.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [approvalId])).rows[0]).toEqual({ consumed_at: null });
    expect(threads.plan.name).toBe("plan");
    expect(threads.plan.notNull).toBe(false);
    expect(threads.planVersion.name).toBe("plan_version");
    expect(threads.planVersion.notNull).toBe(true);
    expect(approvals.consumedAt.name).toBe("consumed_at");
    expect(approvals.consumedAt.notNull).toBe(false);
  });

  it("accepts exactly the shared thread/approval states while rejecting unknown states", async () => {
    const threadId = await newThread();
    const approvalId = await approval(bound());
    for (const value of THREAD_STATUSES) await owner.query("UPDATE threads SET status=$2 WHERE id=$1", [threadId, value]);
    for (const value of APPROVAL_STATUSES) await owner.query("UPDATE approvals SET status=$2 WHERE approval_id=$1", [approvalId, value]);
    await expect(owner.query("UPDATE threads SET status='invented' WHERE id=$1", [threadId])).rejects.toMatchObject({ code: "23514" });
    await expect(owner.query("UPDATE approvals SET status='invented' WHERE approval_id=$1", [approvalId])).rejects.toMatchObject({ code: "23514" });
  });

  it("allows no plan or an array, rejects non-array plans and negative/null versions without changing the saved plan", async () => {
    const threadId = await newThread();
    await owner.query("UPDATE threads SET plan='[]', plan_version=0 WHERE id=$1", [threadId]);
    expect((await state(threadId)).plan).toEqual([]);
    for (const invalid of [{ invalid: "object" }, "scalar", 1, true]) {
      await expect(owner.query("UPDATE threads SET plan=$2::jsonb WHERE id=$1", [threadId, JSON.stringify(invalid)])).rejects.toMatchObject({ code: "23514" });
    }
    await expect(owner.query("UPDATE threads SET plan_version=-1 WHERE id=$1", [threadId])).rejects.toMatchObject({ code: "23514" });
    await expect(owner.query("UPDATE threads SET plan_version=NULL WHERE id=$1", [threadId])).rejects.toMatchObject({ code: "23502" });
    expect(await state(threadId)).toMatchObject({ plan: [], plan_version: 0 });
    await owner.query("UPDATE threads SET plan=NULL WHERE id=$1", [threadId]);
    expect((await state(threadId)).plan).toBeNull();
  });

  it("persists a plan and reuses it after pool restart without replanning or repeating completed steps", async () => {
    const threadId = await newThread();
    const steps = [step("metrics.read", "特殊字符 \" ' \\ 中文"), { ...step(), stepId: "s2" }];
    const planner = vi.fn(() => structuredClone(steps));
    const first = await runQuest(app, gateway, scope, { ...input(threadId), fallbackPlanner: planner });
    expect(first).toMatchObject({ status: "completed", stepsDone: 2, planVersion: 1, planReused: false });
    expect((await state(threadId)).plan).toEqual(steps);
    const originalEvents = await events(threadId);
    const restartedApp = pool(urls.app); const restartedGateway = pool(urls.gateway);
    try {
      const replay = await runQuest(restartedApp, restartedGateway, scope, { ...input(threadId), fallbackPlanner: planner });
      expect(replay).toMatchObject({ status: "completed", stepsDone: 2, planVersion: 1, planReused: true });
      expect(planner).toHaveBeenCalledTimes(1);
      expect(await events(threadId)).toEqual(originalEvents);
    } finally { await Promise.all([restartedApp.end(), restartedGateway.end()]); }
  });

  it("persists the maximum six-step plan and refuses empty/oversized new plans before any persistence or execution", async () => {
    const threadId = await newThread();
    const steps = Array.from({ length: 6 }, (_, index) => ({ ...step("metrics.read", { index }), stepId: `s${index + 1}` }));
    const result = await runQuest(app, gateway, scope, input(threadId, steps));
    expect(result).toMatchObject({ status: "completed", stepsDone: 6, stepsTotal: 6, planVersion: 1 });
    expect((await state(threadId)).plan).toEqual(steps);
    for (const invalid of [[], [...steps, { ...step("metrics.read"), stepId: "s7" }]]) {
      const invalidThread = await newThread();
      await expect(runQuest(app, gateway, scope, input(invalidThread, invalid))).rejects.toThrow("任务步骤数量不合法");
      expect(await state(invalidThread)).toMatchObject({ status: "queued", plan: null, plan_version: 0 });
      expect(await events(invalidThread)).toHaveLength(0);
    }
  });

  it("replan increments the version, supersedes only this thread's pending approvals, and preserves their evidence", async () => {
    const threadId = await newThread();
    const first = await runQuest(app, gateway, scope, input(threadId, [step("manual.write")]));
    expect(first.status).toBe("pending_review");
    const oldApproval = (await owner.query("SELECT * FROM approvals WHERE approval_id=$1", [first.pendingApprovalId])).rows[0];
    const replay = await runQuest(app, gateway, scope, input(threadId, [step("manual.write", "must-not-replan")]));
    expect(replay).toMatchObject({ pendingApprovalId: first.pendingApprovalId, planReused: true, planVersion: 1 });
    const unrelated = await approval(bound(), "pending");
    const next = await runQuest(app, gateway, scope, { ...input(threadId, [step("manual.write", "replanned")]), replan: true });
    expect(next).toMatchObject({ status: "pending_review", planVersion: 2, planReused: false });
    expect(next.pendingApprovalId).not.toBe(first.pendingApprovalId);
    const superseded = (await owner.query("SELECT * FROM approvals WHERE approval_id=$1", [first.pendingApprovalId])).rows[0];
    expect(superseded).toEqual({ ...oldApproval, status: "superseded" });
    expect((await owner.query("SELECT status FROM approvals WHERE approval_id=$1", [unrelated])).rows[0]?.status).toBe("pending");
    expect((await state(threadId)).plan).toEqual([step("manual.write", "replanned")]);
    const audits = (await owner.query<{ payload: BusinessEvent }>("SELECT payload FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='approval.superseded'", [scope.workspaceId])).rows;
    expect(audits.some((row) => row.payload.object.id === first.pendingApprovalId)).toBe(true);
    expect((await events(threadId)).some((event) => event.decision.action === "quest.replan")).toBe(true);
    expect(await decide(app, gateway, scope, { memberNo: "MEM-TEST", role: "owner" }, first.pendingApprovalId!, { type: "approve" })).toMatchObject({ status: "superseded", deduped: true });
  });

  it("rolls back both replan and supersession when the required audit insert fails", async () => {
    const threadId = await newThread();
    const first = await runQuest(app, gateway, scope, input(threadId, [step("manual.write")]));
    const before = await state(threadId);
    const trigger = `reject_supersede_${id()}`;
    // A local database trigger injects failure in the real append function transaction.
    await owner.query(`CREATE FUNCTION ${trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF NEW.workspace_id = '${scope.workspaceId}' AND NEW.payload->'decision'->>'action' = 'approval.superseded'
      THEN RAISE EXCEPTION 'MC014 injected audit failure'; END IF; RETURN NEW; END $$`);
    await owner.query(`CREATE TRIGGER ${trigger} BEFORE INSERT ON biz_events FOR EACH ROW EXECUTE FUNCTION ${trigger}()`);
    try {
      await expect(runQuest(app, gateway, scope, { ...input(threadId, [step("manual.write", "rollback")]), replan: true })).rejects.toThrow("MC014 injected audit failure");
      expect(await state(threadId)).toEqual(before);
      expect((await owner.query("SELECT status FROM approvals WHERE approval_id=$1", [first.pendingApprovalId])).rows[0]?.status).toBe("pending");
    } finally {
      await owner.query(`DROP TRIGGER ${trigger} ON biz_events`);
      await owner.query(`DROP FUNCTION ${trigger}()`);
    }
  });

  it("an actual rejection commits its gesture, memory attribution, and cancelled thread together", async () => {
    const threadId = await newThread();
    const pending = await runQuest(app, gateway, scope, input(threadId, [step("manual.write")]));
    const result = await decide(app, gateway, scope, { memberNo: "MEM-TEST", role: "owner" }, pending.pendingApprovalId!, { type: "reject", reasonEnum: `runtime_test_${id()}`, reasonText: "Local fixture rejected" });
    expect(result.status).toBe("rejected");
    expect((await state(threadId)).status).toBe("cancelled");
    expect((await events(threadId)).some((event) => event.event_id === result.gestureEventId && event.decision.action === "approval.gesture")).toBe(true);
    expect((await owner.query("SELECT 1 FROM memory_usage WHERE workspace_id=$1 AND event_id=$2", [scope.workspaceId, result.gestureEventId])).rowCount).toBe(1);
  });

  it("high-risk runtime approval resumes once, records consumption, and completed replay never reuses it", async () => {
    const threadId = await newThread();
    const quest = { ...input(threadId), presetKey: "runtime-high-risk" };
    const pending = await runQuest(app, gateway, scope, quest);
    expect(pending.status).toBe("pending_review");
    await decide(app, gateway, scope, { memberNo: "MEM-TEST", role: "owner" }, pending.pendingApprovalId!, { type: "approve" });
    const resumed = await runQuest(app, gateway, scope, quest);
    expect(resumed).toMatchObject({ status: "completed", planReused: true, planVersion: 1 });
    const consumed = (await owner.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [pending.pendingApprovalId])).rows[0]?.consumed_at;
    expect(consumed).toBeInstanceOf(Date);
    const count = (await events(threadId)).length;
    expect((await runQuest(app, gateway, scope, quest)).status).toBe("completed");
    expect((await events(threadId)).length).toBe(count);
  });

  it("two simultaneous gateway consumers receive exactly one authorization and one committed event", async () => {
    const approvalId = await approval(bound());
    const threadId = `runtime-cas-${id()}`;
    const ctx = { ...scope, actor: highRiskActor, approvalRef: approvalId, sessionId: threadId };
    const results = await Promise.allSettled([gatewayAppend(gateway, ctx, draft()), gatewayAppend(gateway, ctx, draft())]);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    const failed = results.find((result) => result.status === "rejected") as PromiseRejectedResult;
    expect(failed.reason.message).toContain("已被消费");
    expect(await events(threadId)).toHaveLength(1);
  });

  it("rollback restores the authorization marker and event chain so a later transaction may consume once", async () => {
    const approvalId = await approval(bound());
    const threadId = `runtime-rollback-${id()}`;
    const ctx = { ...scope, actor: highRiskActor, approvalRef: approvalId, sessionId: threadId };
    await expect(scoped(app, scope, async (client) => {
      await gatewayAppendOnClient(client, ctx, draft());
      throw new Error("MC014 caller rollback");
    })).rejects.toThrow("MC014 caller rollback");
    expect((await owner.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [approvalId])).rows[0]?.consumed_at).toBeNull();
    expect(await events(threadId)).toHaveLength(0);
    await gatewayAppend(gateway, ctx, draft());
    expect(await events(threadId)).toHaveLength(1);
  });

  it("unapproved, superseded, expired, unbound and mismatched approvals remain unusable and unconsumed", async () => {
    const cases = [
      { status: "pending", snapshot: bound() },
      { status: "edited", snapshot: bound() },
      { status: "rejected", snapshot: bound() },
      { status: "expired", snapshot: bound() },
      { status: "superseded", snapshot: bound() },
      { status: "approved", snapshot: { ...bound(), expires_at: "2000-01-01T00:00:00.000Z" } },
      { status: "approved", snapshot: { expires_at: bound().expires_at } },
      { status: "approved", snapshot: { ...bound(), action: "other.write" } },
      { status: "approved", snapshot: { ...bound(), object_id: "other-object" } },
    ];
    for (const value of cases) {
      const approvalId = await approval(value.snapshot, value.status);
      await expect(gatewayAppend(gateway, { ...scope, actor: highRiskActor, approvalRef: approvalId }, draft())).rejects.toMatchObject({ stage: "authorization" });
      expect((await owner.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [approvalId])).rows[0]?.consumed_at).toBeNull();
    }
  });

  it("RLS blocks foreign workspace plans and approvals, including updates and missing context", async () => {
    const foreignThread = await newThread(other);
    const foreignApproval = await approval(bound(), "approved", other);
    await scoped(app, scope, async (client) => {
      expect((await client.query("SELECT plan, plan_version FROM threads WHERE id=$1", [foreignThread])).rows).toEqual([]);
      expect((await client.query("UPDATE threads SET plan='[]', plan_version=1 WHERE id=$1", [foreignThread])).rowCount).toBe(0);
      expect((await client.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [foreignApproval])).rows).toEqual([]);
      expect((await client.query("UPDATE approvals SET consumed_at=now() WHERE approval_id=$1", [foreignApproval])).rowCount).toBe(0);
    });
    await expect(gatewayAppend(gateway, { ...scope, actor: highRiskActor, approvalRef: foreignApproval }, draft())).rejects.toMatchObject({ stage: "authorization" });
    const noContext = pool(urls.app);
    try {
      expect((await noContext.query("SELECT plan FROM threads WHERE id=$1", [foreignThread])).rows).toEqual([]);
      expect((await noContext.query("SELECT consumed_at FROM approvals WHERE approval_id=$1", [foreignApproval])).rows).toEqual([]);
    } finally { await noContext.end(); }
    expect((await state(foreignThread)).plan).toBeNull();
  });


  it("readable thread IDs allocate new numbers for consecutive inserts in one transaction", async () => {
    const created = await scoped(app, scope, async (client) => {
      const ids: string[] = [];
      for (let index = 0; index < 3; index++) {
        const inserted = await insertWithReadableId(client, THREAD_ID_SOURCE, async (threadId) => {
          await client.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Readable ID fixture','fixture')", [threadId, scope.tenantId, scope.workspaceId]);
          return threadId;
        });
        ids.push(inserted.id);
      }
      return ids;
    });
    expect(new Set(created).size).toBe(3);
    expect(created.every((threadId) => /^T-[0-9]+$/.test(threadId))).toBe(true);
  });

  it("twelve concurrent inserts across workspaces commit twelve different globally unique thread IDs", async () => {
    const results = await Promise.allSettled(Array.from({ length: 12 }, async (_, index) => {
      const ctx = index % 2 === 0 ? scope : other;
      return scoped(app, ctx, async (client) => insertWithReadableId(client, THREAD_ID_SOURCE, async (threadId) => {
        await client.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Concurrent ID fixture','fixture')", [threadId, ctx.tenantId, ctx.workspaceId]);
        return threadId;
      }));
    }));
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(12);
    const created = results.map((result) => (result as PromiseFulfilledResult<{ id: string }>).value.id);
    expect(new Set(created).size).toBe(12);
  });

  it("sequence allocation survives connection-pool restart and rollback never reuses a reserved number", async () => {
    const first = BigInt((await app.query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
    const restarted = pool(urls.app);
    try {
      const next = BigInt((await restarted.query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
      expect(next).toBeGreaterThan(first);
      let reserved = 0n;
      await expect(scoped(restarted, scope, async (client) => {
        reserved = BigInt((await client.query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
        throw new Error("MC014 allocator rollback");
      })).rejects.toThrow("MC014 allocator rollback");
      expect(BigInt((await restarted.query("SELECT public.threads_max_t_no() AS n")).rows[0].n)).toBeGreaterThan(reserved);
    } finally { await restarted.end(); }
  });

  it("existing SAVEPOINT retry advances past a manually occupied next ID without losing prior transaction work", async () => {
    const seq = (await owner.query("SELECT last_value, is_called FROM public.thread_no_seq")).rows[0];
    const occupied = `T-${BigInt(seq.last_value) + (seq.is_called ? 1n : 0n)}`;
    await owner.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Manual historical ID','fixture')", [occupied, scope.tenantId, scope.workspaceId]);
    const marker = await newThread();
    const created = await scoped(app, scope, async (client) => {
      await client.query("UPDATE threads SET title='Survives collision retry' WHERE id=$1", [marker]);
      return insertWithReadableId(client, THREAD_ID_SOURCE, async (threadId) => {
        await client.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Retried ID','fixture')", [threadId, scope.tenantId, scope.workspaceId]);
        return threadId;
      });
    });
    expect(created.id).not.toBe(occupied);
    expect((await state(marker)).title).toBe("Survives collision retry");
  });

  it("allocation wrapper is explicitly granted to app/gateway, exposes no row data, and denies direct sequence access", async () => {
    const definition = (await owner.query("SELECT prosecdef,proconfig,proacl,proowner FROM pg_proc WHERE oid='public.threads_max_t_no()'::regprocedure")).rows[0];
    expect(definition.prosecdef).toBe(true);
    expect(definition.proconfig).toContain("search_path=pg_catalog, pg_temp");
    expect((await owner.query("SELECT 1 FROM pg_proc p, LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid='public.threads_max_t_no()'::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE'")).rowCount).toBe(0);
    for (const db of [app, gateway]) {
      expect(BigInt((await db.query("SELECT public.threads_max_t_no() AS n")).rows[0].n)).toBeGreaterThan(100n);
      expect((await db.query("SELECT has_sequence_privilege(current_user,'public.thread_no_seq','USAGE,SELECT,UPDATE') AS granted")).rows[0].granted).toBe(false);
      await expect(db.query("SELECT nextval('public.thread_no_seq')")).rejects.toMatchObject({ code: "42501" });
      await expect(db.query("SELECT setval('public.thread_no_seq',101)")).rejects.toMatchObject({ code: "42501" });
    }
    const foreignThread = await newThread(other);
    await scoped(app, scope, async (client) => {
      expect((await client.query("SELECT id FROM threads WHERE id=$1", [foreignThread])).rows).toEqual([]);
    });
  });

  it("unprivileged roles cannot execute the definer and temporary objects cannot redirect its sequence", async () => {
    const roleName = `mc014_unprivileged_${id()}`;
    await owner.query(`CREATE ROLE ${roleName} NOLOGIN`);
    const client = await owner.connect();
    try {
      await client.query(`SET ROLE ${roleName}`);
      await expect(client.query("SELECT public.threads_max_t_no()" )).rejects.toMatchObject({ code: "42501" });
    } finally {
      await client.query("RESET ROLE");
      client.release();
      await owner.query(`DROP ROLE ${roleName}`);
    }
    await scoped(app, scope, async (connection) => {
      await connection.query("CREATE TEMP TABLE threads (id text) ON COMMIT DROP");
      await connection.query("INSERT INTO threads VALUES ('T-900000000000')");
      await connection.query("CREATE TEMP SEQUENCE thread_no_seq START WITH 900000000000");
      try {
        const value = BigInt((await connection.query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
        expect(value).toBeLessThan(900000000000n);
      } finally { await connection.query("DROP SEQUENCE pg_temp.thread_no_seq"); }
    });
  });

  it("two actual inspection dispatches create separate threads with their linked audit events", async () => {
    const results: Array<{ threadId: string; eventId: string }> = [];
    for (let index = 0; index < 2; index++) {
      const anomaly = await gatewayAppend(gateway, { ...scope, actor: { id: "fixture", type: "human" } }, {
        who: { id: "fixture", type: "human" },
        context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
        object: { type: "record", id: `anomaly-${id()}` },
        decision: { action: "inspect.anomaly", after: { severity: "low", summary: "Runtime schema fixture" } }, rule_impact: [],
      });
      const dispatched = await dispatchFromAnomaly(app, gateway, scope, { anomalyEventId: anomaly.eventId, presetKey: "runtime-test", by: "fixture" });
      results.push(dispatched);
      expect((await state(dispatched.threadId)).workspace_id).toBe(scope.workspaceId);
      expect((await events(dispatched.threadId)).find((event) => event.event_id === dispatched.eventId)).toMatchObject({ decision: { action: "inspect.dispatch", after: { threadId: dispatched.threadId } }, links: [anomaly.eventId] });
    }
    expect(results[0]!.threadId).not.toBe(results[1]!.threadId);
  });

  it.each(["missing", "ahead", "uncalled", "cached"])("thread sequence upgrade preserves historical rows and never moves %s source backwards", async (kind) => {
    await temporaryDatabase("threadupgrade", async (db, url) => {
      await oldSchema(db, "0037_thread_number_sequence.sql");
      const legacy: Scope = { tenantId: `tenant-${id()}`, workspaceId: `ws-${id()}` };
      await seedWorkspace(db, legacy);
      for (const threadId of ["T-00009000", "non-thread-99999999999", "T-9001-suffix"]) {
        await db.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Historical ID preserved','fixture')", [threadId, legacy.tenantId, legacy.workspaceId]);
      }
      const before = (await db.query("SELECT to_jsonb(t) AS row FROM threads t ORDER BY id")).rows;
      if (kind !== "missing") {
        await db.query(`CREATE SEQUENCE public.thread_no_seq START WITH 20000 CACHE ${kind === "cached" ? 32 : 1}`);
        if (kind !== "uncalled") await db.query("SELECT nextval('public.thread_no_seq')");
      }
      const oldState = kind === "missing" ? null : (await db.query("SELECT last_value,is_called FROM public.thread_no_seq")).rows[0];
      const expected = oldState ? BigInt(oldState.last_value) + (oldState.is_called ? 1n : 0n) : 9001n;
      const upgraded = migrate(url);
      expect(upgraded.status, upgraded.log).toBe(0);
      expect((await db.query("SELECT to_jsonb(t) AS row FROM threads t ORDER BY id")).rows).toEqual(before);
      const value = BigInt((await db.query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
      expect(value).toBe(expected);
      const repeated = migrate(url);
      expect(repeated.status, repeated.log).toBe(0);
      expect(BigInt((await db.query("SELECT public.threads_max_t_no() AS n")).rows[0].n)).toBeGreaterThan(value);
    });
  }, 60000);

  it("out-of-range historical thread numbers fail the migration without losing old rows or creating a partial source", async () => {
    await temporaryDatabase("threadoverflow", async (db, url) => {
      await oldSchema(db, "0037_thread_number_sequence.sql");
      await seedWorkspace(db, { tenantId: "tenant-historical", workspaceId: "ws-historical" });
      await db.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ('T-9007199254740992','tenant-historical','ws-historical','Oversized ID','fixture')");
      const result = migrate(url);
      expect(result.status, result.log).not.toBe(0);
      expect(result.log).toContain("thread number");
      expect((await db.query("SELECT id FROM threads")).rows).toEqual([{ id: "T-9007199254740992" }]);
      expect((await db.query("SELECT to_regclass('public.thread_no_seq') AS seq")).rows[0].seq).toBeNull();
      expect((await db.query("SELECT 1 FROM _migrations WHERE name='0037_thread_number_sequence.sql'")).rowCount).toBe(0);
    });
  }, 60000);

  it("fresh install applies the full migration chain and repeat migration is checksum-idempotent", async () => {
    await temporaryDatabase("fresh", async (db, url) => {
      const first = migrate(url);
      expect(first.status, first.log).toBe(0);
      const names = readdirSync(migrationsDir).filter((name) => name.endsWith(".sql")).sort();
      expect((await db.query<{ name: string }>("SELECT name FROM _migrations ORDER BY name")).rows.map((row) => row.name)).toEqual(names);
      expect(names).toContain(migrationName);
      expect((await db.query("SELECT plan, plan_version FROM threads LIMIT 0")).fields.map((field) => field.name)).toEqual(["plan", "plan_version"]);
      expect((await db.query("SELECT consumed_at FROM approvals LIMIT 0")).fields[0]?.name).toBe("consumed_at");
      const second = migrate(url);
      expect(second.status, second.log).toBe(0);
      expect(second.log).toContain("数据库已是最新");
    });
  }, 60000);

  it("populated legacy schema reproduces all missing contracts, upgrades without data loss, and preserves consumed history", async () => {
    await temporaryDatabase("upgrade", async (db, url) => {
      const legacyFiles = await oldSchema(db);
      const legacy: Scope = { tenantId: `tenant-${id()}`, workspaceId: `ws-${id()}` };
      await seedWorkspace(db, legacy);
      const threadId = `thread-${id()}`;
      await db.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Historical thread','fixture')", [threadId, legacy.tenantId, legacy.workspaceId]);
      const oldStatuses = ["pending", "approved", "edited", "rejected", "expired"];
      for (const status of oldStatuses) {
        await db.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,snapshot) VALUES ($1,$2,$3,$4,$5,$6)", [`old-${status}`, legacy.tenantId, legacy.workspaceId, `event-${status}`, status, JSON.stringify({ ...bound(), historical: "preserve me" })]);
      }
      await db.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,snapshot) VALUES ('old-consumed',$1,$2,'event-consumed','approved',$3)", [legacy.tenantId, legacy.workspaceId, JSON.stringify({ ...bound(), used_at: "2026-09-29T03:04:05.123Z" })]);
      const beforeThreads = (await db.query("SELECT to_jsonb(t) AS row FROM threads t ORDER BY id")).rows;
      const beforeApprovals = (await db.query("SELECT to_jsonb(a) AS row FROM approvals a ORDER BY approval_id")).rows;
      const beforeChecksums = (await db.query("SELECT name,checksum FROM _migrations ORDER BY name")).rows;
      await expect(db.query("SELECT plan, plan_version FROM threads")).rejects.toMatchObject({ code: "42703" });
      await expect(db.query("SELECT consumed_at FROM approvals")).rejects.toMatchObject({ code: "42703" });
      await expect(db.query("UPDATE approvals SET status='superseded' WHERE approval_id='old-pending'")).rejects.toMatchObject({ code: "23514" });
      await expect(db.query("UPDATE threads SET status='cancelled' WHERE id=$1", [threadId])).rejects.toMatchObject({ code: "23514" });
      const upgraded = migrate(url);
      expect(upgraded.status, upgraded.log).toBe(0);
      expect(upgraded.log).toContain(migrationName);
      expect((await db.query("SELECT to_jsonb(t)-'plan'-'plan_version' AS row FROM threads t ORDER BY id")).rows).toEqual(beforeThreads);
      expect((await db.query("SELECT to_jsonb(a)-'consumed_at' AS row FROM approvals a ORDER BY approval_id")).rows).toEqual(beforeApprovals);
      expect((await db.query("SELECT name,checksum FROM _migrations WHERE name = ANY($1) ORDER BY name", [legacyFiles])).rows).toEqual(beforeChecksums);
      const consumed = (await db.query("SELECT approval_id,consumed_at FROM approvals ORDER BY approval_id")).rows;
      expect(consumed.find((row) => row.approval_id === "old-consumed")?.consumed_at.toISOString()).toBe("2026-09-29T03:04:05.123Z");
      expect(consumed.filter((row) => row.approval_id !== "old-consumed").every((row) => row.consumed_at === null)).toBe(true);
      const upgradedGatewayUrl = new URL(urls.gateway); upgradedGatewayUrl.pathname = url.pathname;
      const upgradedGateway = pool(upgradedGatewayUrl);
      try {
        await expect(gatewayAppend(upgradedGateway, { ...legacy, actor: highRiskActor, approvalRef: "old-consumed" }, draft())).rejects.toThrow("已被消费");
      } finally { await upgradedGateway.end(); }
    });
  }, 60000);

  it.each(["not-a-timestamp", "2026-99-40T25:61:00Z", "", 42])("invalid historical used_at=%j rejects atomically and preserves its evidence", async (usedAt) => {
    await temporaryDatabase("invalid", async (db, url) => {
      await oldSchema(db);
      await db.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,snapshot) VALUES ('old-invalid','tenant-invalid','ws-invalid','event-invalid','approved',$1)", [JSON.stringify({ ...bound(), used_at: usedAt })]);
      const before = (await db.query("SELECT to_jsonb(a) AS row FROM approvals a")).rows;
      const result = migrate(url);
      expect(result.status, result.log).not.toBe(0);
      expect(result.log).toContain("snapshot.used_at");
      expect((await db.query("SELECT to_jsonb(a) AS row FROM approvals a")).rows).toEqual(before);
      expect((await db.query("SELECT 1 FROM _migrations WHERE name=$1", [migrationName])).rowCount).toBe(0);
      expect((await db.query("SELECT 1 FROM information_schema.columns WHERE table_name='approvals' AND column_name='consumed_at'")).rowCount).toBe(0);
    });
  }, 60000);

  it("all emitted fixture events retain append-only integrity and the hash chain", async () => {
    const rows = (await owner.query<{ payload: BusinessEvent; prev_hash: string; hash: string }>("SELECT payload,prev_hash,hash FROM biz_events WHERE workspace_id=$1 ORDER BY seq", [scope.workspaceId])).rows;
    let previous = "GENESIS";
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.prev_hash).toBe(previous);
      expect(row.hash).toBe(eventHash(previous, row.payload));
      previous = row.hash;
    }
    await expect(owner.query("UPDATE biz_events SET hash='tamper' WHERE workspace_id=$1", [scope.workspaceId])).rejects.toThrow("append-only");
  });
});
