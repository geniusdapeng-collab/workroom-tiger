/**
 * MC021: shadow queue decisions are advisory on real PostgreSQL. Opt in with
 * RUN_CAPTAIN_QUEUE_PG_TESTS=1 and three roles on one loopback workloom_tiger_*
 * database. Fault-injection DDL requires this file to run serially within that
 * database. Unique, simulated fixtures remain available for audit.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { BusinessEvent } from "@workloom/shared";
import { defaultCharter, type Charter } from "./charter.js";
import { loadCharter, runQueueBeat, type Scope } from "./loop.js";
import { gatewayAppend } from "../workdata/gateway.js";
import { eventHash } from "../workdata/events.js";

const suite = process.env.RUN_CAPTAIN_QUEUE_PG_TESTS === "1" ? describe : describe.skip;
const id = () => randomUUID().replaceAll("-", "");
let owner: pg.Pool;
let app: pg.Pool;
let gateway: pg.Pool;
const noNetwork = vi.fn(async () => { throw new Error("MC021 forbids network/model requests"); });

function config(): Record<"owner" | "app" | "gateway", URL> {
  const inputs = { owner: process.env.DATABASE_URL, app: process.env.DATABASE_APP_URL, gateway: process.env.DATABASE_GATEWAY_URL };
  const urls = {} as Record<keyof typeof inputs, URL>;
  for (const key of ["owner", "app", "gateway"] as const) {
    let url: URL;
    try { url = new URL(inputs[key] ?? ""); }
    catch { throw new Error(`MC021 requires a valid ${key} database URL`); }
    if (!["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error("MC021 requires a loopback workloom_tiger_ test database");
    }
    urls[key] = url;
  }
  for (const key of ["app", "gateway"] as const) {
    if (urls[key].hostname !== urls.owner.hostname || urls[key].port !== urls.owner.port
      || urls[key].pathname !== urls.owner.pathname) throw new Error("MC021 pools must share one local test database");
  }
  return urls;
}

function charter(mode: Charter["mode"] = "shadow"): Charter {
  const c = defaultCharter();
  c.mode = mode;
  c.autonomy = {
    ranges: { change: { label: "变更范围", lower: 0, anchor: 1, upper: 2 } },
    caps: { spend: { label: "支出上限", limit: 5000 } },
  };
  c.grant = {
    event_id: "E-MC021-GRANT", granted_by: "MEM-MC021", granted_at: new Date().toISOString(),
    disclosure_version: "fixture-v1", clauses: ["simulation only"], shadow_days: 3, trial_days: 7,
    trial_ends_at: new Date(Date.now() + 86400000).toISOString(), retain_until: null,
  };
  return c;
}

async function fixture(c = charter(), overrides: Partial<Scope> = {}): Promise<Scope> {
  const scope = { tenantId: `tenant-mc021-${id()}`, workspaceId: `ws-mc021-${id()}`, ...overrides };
  await owner.query("INSERT INTO tenants (id,name) VALUES ($1,'Queue fixture') ON CONFLICT (id) DO NOTHING", [scope.tenantId]);
  await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'Queue fixture',$1,'test')", [scope.workspaceId, scope.tenantId]);
  await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test',$3)", [scope.workspaceId, scope.tenantId, JSON.stringify({ charter: c, preserved: { note: "unrelated archive data" } })]);
  return scope;
}

async function approval(scope: Scope, snapshot: Record<string, unknown> = {}, options: { status?: string; tier?: string; approvalId?: string } = {}): Promise<string> {
  const approvalId = options.approvalId ?? `apr-mc021-${id()}`;
  const event = await gatewayAppend(gateway, { ...scope, actor: { id: "system", type: "system" } }, {
    who: { id: "system", type: "system" },
    context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
    object: { type: "record", id: approvalId },
    decision: { action: "record.update", kind: "review", params: {} },
    rule_impact: [], receipt: { mode: "simulated", synced: true },
  });
  await owner.query(
    `INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,tier,snapshot)
     VALUES ($1,$2,$3,$4,$5,$6,$7)`,
    [approvalId, scope.tenantId, scope.workspaceId, event.eventId, options.status ?? "pending", options.tier ?? "l2_captain",
      JSON.stringify({ action: "record.update", title: "Queue fixture", params: { note: "unchanged input" }, ...snapshot })],
  );
  return approvalId;
}

async function approvals(scope: Scope) {
  return (await owner.query("SELECT * FROM approvals WHERE tenant_id=$1 AND workspace_id=$2 ORDER BY approval_id", [scope.tenantId, scope.workspaceId])).rows;
}

async function events(scope: Scope, action?: string): Promise<BusinessEvent[]> {
  return (await owner.query<{ payload: BusinessEvent }>(
    "SELECT payload FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2 AND ($3::text IS NULL OR payload->'decision'->>'action'=$3) ORDER BY seq",
    [scope.tenantId, scope.workspaceId, action ?? null],
  )).rows.map((r) => r.payload);
}

async function state(scope: Scope) {
  return {
    approvals: await approvals(scope), events: await events(scope),
    profile: (await owner.query("SELECT archive,updated_at FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0],
  };
}

function model(verdict: "approve" | "escalate", viable = true) {
  return vi.fn(async (prompt: string) => {
    if (prompt.includes("决策分析师")) return JSON.stringify([{ label: "Fixture option", params: {}, stance: "conservative" }]);
    if (prompt.includes("红队评审")) return viable ? "Fixture risk reviewed" : "不可行：fixture fails the review";
    return JSON.stringify({ verdict, rationale: "Fixture deterministic rationale" });
  });
}

async function assertDecision(scope: Scope, approvalId: string, verdict: string, tier: string, dryRun: boolean) {
  const rows = await events(scope, "ceo.decision");
  expect(rows).toHaveLength(1);
  expect(rows[0]!.context).toMatchObject({ tenant_id: scope.tenantId, workspace_id: scope.workspaceId });
  expect(rows[0]!.who).toMatchObject({ type: "agent", id: "company-ceo" });
  expect(rows[0]!.decision.params).toMatchObject({ approval_id: approvalId, verdict, tier });
  expect((rows[0]!.decision.params as Record<string, unknown>).dry_run).toBe(dryRun ? true : undefined);
  expect(rows[0]!.decision.basis!.length).toBeGreaterThan(0);
  expect(rows[0]!.decision.after).toMatchObject({ memo: { recommendation: expect.any(String) } });
  return rows[0]!;
}

async function withFailure(table: "approvals" | "biz_events", scope: Scope, fn: () => Promise<void>) {
  const name = `mc021_${id()}`;
  const literal = scope.workspaceId.replaceAll("'", "''");
  await owner.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC021 injected write failure'; END $$`);
  try {
    await owner.query(`CREATE TRIGGER ${name} BEFORE ${table === "approvals" ? "UPDATE" : "INSERT"} ON ${table} FOR EACH ROW WHEN (NEW.workspace_id='${literal}') EXECUTE FUNCTION ${name}()`);
    await fn();
  } finally {
    await owner.query(`DROP TRIGGER IF EXISTS ${name} ON ${table}`);
    await owner.query(`DROP FUNCTION ${name}()`);
  }
}

suite("MC021 queue advisory contract on real PostgreSQL", () => {
  beforeAll(async () => {
    const urls = config();
    const pool = (url: URL) => new pg.Pool({ connectionString: url.toString(), max: 16, connectionTimeoutMillis: 5000, statement_timeout: 15000 });
    owner = pool(urls.owner); app = pool(urls.app); gateway = pool(urls.gateway);
    for (const [db, role] of [[app, "workloom_app"], [gateway, "workloom_gateway"]] as const) {
      expect((await db.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
        .toMatchObject({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    const tables = await owner.query("SELECT relname,relrowsecurity FROM pg_class WHERE oid=ANY($1::regclass[])", [["profiles", "approvals", "biz_events"]]);
    expect(tables.rows).toHaveLength(3);
    expect(tables.rows.every((r) => r.relrowsecurity)).toBe(true);
    vi.stubGlobal("fetch", noNetwork);
  });
  afterAll(async () => {
    vi.unstubAllGlobals();
    await Promise.all([owner?.end(), app?.end(), gateway?.end()]);
    expect(noNetwork).not.toHaveBeenCalled();
  });

  it.each([
    { label: "standard escalation", snapshot: {}, verdict: "escalate" as const, tier: "standard", viable: true, calls: 1 },
    { label: "standard approval", snapshot: {}, verdict: "approve" as const, tier: "standard", viable: true, calls: 1 },
    { label: "micro approval", snapshot: { autonomy_range_key: "change", autonomy_range_value: 1 }, verdict: "approve" as const, tier: "micro", viable: true, calls: 0 },
    { label: "micro edge escalation", snapshot: { autonomy_range_key: "change", autonomy_range_value: 0.05 }, verdict: "escalate" as const, tier: "micro", viable: true, calls: 0 },
    { label: "major approval", snapshot: { irreversible: true }, verdict: "approve" as const, tier: "major", viable: true, calls: 2 },
    { label: "major escalation", snapshot: { irreversible: true }, verdict: "escalate" as const, tier: "major", viable: false, calls: 2 },
  ])("shadow $label preserves every approval field and emits only dry-run evidence", async ({ snapshot, verdict, tier, viable, calls }) => {
    const scope = await fixture();
    const approvalId = await approval(scope, snapshot);
    const before = await state(scope);
    const llmCall = model(verdict, viable);
    const result = await runQueueBeat(app, scope, { llmCall });
    expect(await approvals(scope)).toEqual(before.approvals);
    expect((await state(scope)).profile).toEqual(before.profile);
    expect(result).toMatchObject({ decided: verdict === "approve" ? 1 : 0, escalated: verdict === "escalate" ? 1 : 0 });
    expect(result.tiers?.[tier]).toBe(1);
    expect(llmCall).toHaveBeenCalledTimes(calls);
    await assertDecision(scope, approvalId, verdict, tier, true);
  });

  it.each(["absent", "throws", "malformed", "unknown"] as const)("shadow standard model %s retains conservative escalation without mutation", async (variant) => {
    const scope = await fixture();
    const approvalId = await approval(scope);
    const before = await approvals(scope);
    const llmCall = variant === "absent" ? undefined : async () => {
      if (variant === "throws") throw new Error("Fixture model unavailable");
      return variant === "malformed" ? "not-json" : JSON.stringify({ verdict: "unrecognized" });
    };
    expect(await runQueueBeat(app, scope, { llmCall })).toMatchObject({ decided: 0, escalated: 1 });
    expect(await approvals(scope)).toEqual(before);
    await assertDecision(scope, approvalId, "escalate", "standard", true);
  });

  it.each(["approve", "escalate"] as const)("shadow %s does not issue an approvals UPDATE", async (verdict) => {
    const scope = await fixture();
    const approvalId = await approval(scope);
    const before = await approvals(scope);
    await withFailure("approvals", scope, async () => {
      await runQueueBeat(app, scope, { llmCall: model(verdict) });
    });
    expect(await approvals(scope)).toEqual(before);
    await assertDecision(scope, approvalId, verdict, "standard", true);
  });

  it.each(["active", "trial"] as const)("%s standard approval commits one approval and matching event; replay is empty", async (mode) => {
    const scope = await fixture(charter(mode));
    const approvalId = await approval(scope);
    const before = (await approvals(scope))[0]!;
    expect(await runQueueBeat(app, scope, { llmCall: model("approve") })).toMatchObject({ decided: 1, escalated: 0 });
    const after = (await approvals(scope))[0]!;
    expect(after).toMatchObject({ status: "approved", tier: "l2_captain", decided_by: "company-ceo", snapshot: before.snapshot, consumed_at: null });
    expect(after.decided_at).toBeInstanceOf(Date);
    expect(after.gesture).toMatchObject({ type: "approve", weight: 1 });
    await assertDecision(scope, approvalId, "approve", "standard", false);
    const settled = await state(scope);
    expect(await runQueueBeat(app, scope, { llmCall: model("approve") })).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(settled);
  });

  it.each(["active", "trial"] as const)("%s standard escalation keeps pending status and commits one L4 handoff; replay is empty", async (mode) => {
    const scope = await fixture(charter(mode));
    const approvalId = await approval(scope);
    const before = (await approvals(scope))[0]!;
    expect(await runQueueBeat(app, scope, { llmCall: model("escalate") })).toMatchObject({ decided: 0, escalated: 1 });
    const after = (await approvals(scope))[0]!;
    expect(after).toMatchObject({ status: "pending", tier: "l4_chairman", gesture: null, decided_by: null, decided_at: null, consumed_at: null });
    expect(after.snapshot).toMatchObject({ ...before.snapshot, ceo_escalated: true, ceo_rationale: expect.any(String) });
    await assertDecision(scope, approvalId, "escalate", "standard", false);
    const settled = await state(scope);
    expect(await runQueueBeat(app, scope, { llmCall: model("escalate") })).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(settled);
  });

  it.each(["active", "trial"] as const)("%s micro decisions retain the effective autonomy boundaries", async (mode) => {
    const scope = await fixture(charter(mode));
    await approval(scope, { autonomy_range_key: "change", autonomy_range_value: 1 });
    await approval(scope, { autonomy_range_key: "change", autonomy_range_value: mode === "trial" ? 0.51 : 0.05 });
    const llmCall = model("approve");
    expect(await runQueueBeat(app, scope, { llmCall })).toEqual({ decided: 1, escalated: 1, tiers: { micro: 2, standard: 0, major: 0 } });
    expect(llmCall).not.toHaveBeenCalled();
    const rows = await approvals(scope);
    expect(rows.filter((r) => r.status === "approved")).toHaveLength(1);
    expect(rows.filter((r) => r.tier === "l4_chairman" && r.status === "pending")).toHaveLength(1);
  });

  it.each([
    { mode: "active" as const, viable: true, verdict: "approve" },
    { mode: "active" as const, viable: false, verdict: "escalate" },
    { mode: "trial" as const, viable: true, verdict: "escalate" },
    { mode: "trial" as const, viable: false, verdict: "escalate" },
  ])("$mode major viable=$viable preserves its existing $verdict outcome", async ({ mode, viable, verdict }) => {
    const scope = await fixture(charter(mode));
    const approvalId = await approval(scope, { irreversible: true });
    const llmCall = model("approve", viable);
    expect(await runQueueBeat(app, scope, { llmCall })).toMatchObject({ decided: verdict === "approve" ? 1 : 0, escalated: verdict === "escalate" ? 1 : 0 });
    expect(llmCall).toHaveBeenCalledTimes(2);
    const event = await assertDecision(scope, approvalId, verdict, "major", false);
    expect(event.decision.after).toMatchObject({ analysis: { via: "llm", options: [{ critic: expect.any(String) }] } });
  });

  it.each(["disabled", "suspended"] as const)("%s remains silent with queued approvals", async (mode) => {
    const scope = await fixture(charter(mode));
    await approval(scope);
    const before = await state(scope);
    const llmCall = model("escalate");
    expect(await runQueueBeat(app, scope, { llmCall })).toMatchObject({ decided: 0, escalated: 0, skipped: expect.stringContaining(mode) });
    expect(await state(scope)).toEqual(before);
    expect(llmCall).not.toHaveBeenCalled();
  });

  it.each(["active", "trial"] as const)("expired %s suspends before queue reasoning", async (mode) => {
    const c = charter(mode);
    if (mode === "trial") c.grant!.trial_ends_at = new Date(Date.now() - 1000).toISOString();
    else c.grant!.retain_until = new Date(Date.now() - 1000).toISOString();
    const scope = await fixture(c);
    await approval(scope);
    const before = await approvals(scope);
    const llmCall = model("approve");
    expect(await runQueueBeat(app, scope, { llmCall })).toMatchObject({ decided: 0, escalated: 0, skipped: expect.stringContaining("suspended") });
    expect(await approvals(scope)).toEqual(before);
    expect((await loadCharter(app, scope)).mode).toBe("suspended");
    expect(await events(scope, "captain.mode_change")).toHaveLength(1);
    expect(await events(scope, "ceo.decision")).toHaveLength(0);
    expect(llmCall).not.toHaveBeenCalled();
  });

  it.each(["active", "shadow"] as const)("%s excludes expired, already-decided and non-L2 approvals", async (mode) => {
    const scope = await fixture(charter(mode));
    await approval(scope, { expires_at: new Date(Date.now() - 1000).toISOString() });
    for (const status of ["approved", "edited", "rejected", "expired", "superseded"]) await approval(scope, {}, { status });
    for (const tier of ["l3_fleet", "l4_chairman"]) await approval(scope, {}, { tier });
    const before = await state(scope);
    const llmCall = model("escalate");
    expect(await runQueueBeat(app, scope, { llmCall })).toEqual({ decided: 0, escalated: 0, tiers: { micro: 0, standard: 0, major: 0 } });
    expect(await state(scope)).toEqual(before);
    expect(llmCall).not.toHaveBeenCalled();
  });

  it("an empty queue emits no evidence", async () => {
    const scope = await fixture();
    const before = await state(scope);
    expect(await runQueueBeat(app, scope)).toEqual({ decided: 0, escalated: 0, tiers: { micro: 0, standard: 0, major: 0 } });
    expect(await state(scope)).toEqual(before);
  });

  it("the 20-item batch limit leaves the last approval for the next active beat", async () => {
    const scope = await fixture(charter("active"));
    for (let i = 0; i < 21; i++) await approval(scope, {}, { approvalId: `apr-mc021-${scope.workspaceId}-${String(i).padStart(2, "0")}` });
    expect(await runQueueBeat(app, scope, { llmCall: model("approve") })).toMatchObject({ decided: 20, escalated: 0 });
    expect((await approvals(scope)).filter((r) => r.status === "pending")).toHaveLength(1);
    expect(await runQueueBeat(app, scope, { llmCall: model("approve") })).toMatchObject({ decided: 1, escalated: 0 });
    expect(await events(scope, "ceo.decision")).toHaveLength(21);
  });

  it.each(["approve", "escalate"] as const)("shadow %s replay/concurrent observations never consume or change the approval", async (verdict) => {
    const scope = await fixture();
    await approval(scope, { title: "引号 ' \" / 换行\n保持", params: { note: "非指令 <req> ' \" \\n" } }, { approvalId: `apr-mc021-引号'-${id()}` });
    const before = await approvals(scope);
    const results = await Promise.all(Array.from({ length: 6 }, () => runQueueBeat(app, scope, { llmCall: model(verdict) })));
    expect(results.every((r) => r.decided + r.escalated === 1)).toBe(true);
    expect(await approvals(scope)).toEqual(before);
    expect(await events(scope, "ceo.decision")).toHaveLength(6);
    await runQueueBeat(app, scope, { llmCall: model(verdict) });
    expect(await approvals(scope)).toEqual(before);
    const decisions = await events(scope, "ceo.decision");
    expect(decisions).toHaveLength(7);
    expect(decisions.every((e) => (e.decision.params as Record<string, unknown>).dry_run === true)).toBe(true);
    const chain = await owner.query("SELECT payload,prev_hash,hash FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2 ORDER BY seq", [scope.tenantId, scope.workspaceId]);
    let previous = "GENESIS";
    for (const row of chain.rows) {
      expect(row.prev_hash).toBe(previous);
      expect(row.hash).toBe(eventHash(previous, row.payload));
      previous = row.hash;
    }
  });

  it.each(["active", "shadow"] as const)("%s affects only its workspace and tenant fixtures", async (mode) => {
    const target = await fixture(charter(mode));
    const sameTenant = await fixture(charter(mode), { tenantId: target.tenantId });
    const otherTenant = await fixture(charter(mode));
    for (const scope of [target, sameTenant, otherTenant]) await approval(scope);
    const foreign = [await state(sameTenant), await state(otherTenant)];
    await runQueueBeat(app, target, { llmCall: model("escalate") });
    expect([await state(sameTenant), await state(otherTenant)]).toEqual(foreign);
    expect(await events(target, "ceo.decision")).toHaveLength(1);
    const client = await app.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id',$1,true),set_config('app.tenant_id',$2,true)", [target.workspaceId, target.tenantId]);
      expect((await client.query("SELECT approval_id FROM approvals WHERE workspace_id=ANY($1)", [[sameTenant.workspaceId, otherTenant.workspaceId]])).rows).toHaveLength(0);
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally { client.release(); }
  });

  it.each(["approve", "escalate"] as const)("active %s approval-write failure rolls back without an event and retries once", async (verdict) => {
    const scope = await fixture(charter("active"));
    const approvalId = await approval(scope);
    const before = await state(scope);
    await withFailure("approvals", scope, async () => {
      await expect(runQueueBeat(app, scope, { llmCall: model(verdict) })).rejects.toThrow("MC021 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    await runQueueBeat(app, scope, { llmCall: model(verdict) });
    await assertDecision(scope, approvalId, verdict, "standard", false);
  });

  it.each([
    { mode: "active" as const, verdict: "approve" as const }, { mode: "active" as const, verdict: "escalate" as const },
    { mode: "shadow" as const, verdict: "approve" as const }, { mode: "shadow" as const, verdict: "escalate" as const },
  ])("$mode $verdict event failure rolls back every approval field and retries once", async ({ mode, verdict }) => {
    const scope = await fixture(charter(mode));
    const approvalId = await approval(scope);
    const before = await state(scope);
    await withFailure("biz_events", scope, async () => {
      await expect(runQueueBeat(app, scope, { llmCall: model(verdict) })).rejects.toThrow("MC021 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    await runQueueBeat(app, scope, { llmCall: model(verdict) });
    await assertDecision(scope, approvalId, verdict, "standard", mode === "shadow");
    if (mode === "shadow") expect(await approvals(scope)).toEqual(before.approvals);
  });
});
