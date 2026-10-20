/**
 * MC020: generic KPI circuit breakers on real PostgreSQL, including concurrent
 * charter edits and rollback. RUN_CAPTAIN_BREAKER_PG_TESTS=1 opts in; all pools
 * must target one loopback workloom_tiger_* database. Unique fixtures stay for audit.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { defaultCharter, tightenAutonomy, type Charter } from "./charter.js";
import { loadCharter, runBreakerBeat, type Scope } from "./loop.js";
import { gatewayAppend } from "../workdata/gateway.js";
import type { BusinessEvent } from "@workloom/shared";

const suite = process.env.RUN_CAPTAIN_BREAKER_PG_TESTS === "1" ? describe : describe.skip;
const id = () => randomUUID().replaceAll("-", "");
const applicationName = `mc020-${id()}`;
let owner: pg.Pool;
let app: pg.Pool;
let gateway: pg.Pool;
const noNetwork = vi.fn(async () => { throw new Error("MC020 tests forbid network/model requests"); });

function config(): Record<"owner" | "app" | "gateway", URL> {
  const inputs = { owner: process.env.DATABASE_URL, app: process.env.DATABASE_APP_URL, gateway: process.env.DATABASE_GATEWAY_URL };
  const urls = {} as Record<keyof typeof inputs, URL>;
  for (const key of ["owner", "app", "gateway"] as const) {
    let url: URL;
    try { url = new URL(inputs[key] ?? ""); }
    catch { throw new Error(`MC020 requires a valid ${key} database URL`); }
    if (!["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error("MC020 requires a loopback workloom_tiger_ test database");
    }
    urls[key] = url;
  }
  for (const key of ["app", "gateway"] as const) {
    if (urls[key].hostname !== urls.owner.hostname || urls[key].port !== urls.owner.port
      || urls[key].pathname !== urls.owner.pathname) throw new Error("MC020 pools must share one local test database");
  }
  return urls;
}

function charter(mode: Charter["mode"] = "active", floors: Record<string, number> = { quality: 0.7 }): Charter {
  const c = defaultCharter();
  c.mode = mode;
  c.autonomy = {
    ranges: {
      change: { label: "变更范围", lower: -3, anchor: 1, upper: 7 },
      fixed: { label: "固定边界", lower: 0, anchor: 0, upper: 0 },
    },
    caps: { spend: { label: "支出上限", limit: 5001 }, zero: { label: "零额度", limit: 0 } },
  };
  c.circuit_breaker.kpi_floor = floors;
  c.grant = {
    event_id: "E-MC020-GRANT", granted_by: "MEM-MC020", granted_at: new Date().toISOString(),
    disclosure_version: "fixture-v1", clauses: ["simulation only"], shadow_days: 3, trial_days: 7,
    trial_ends_at: new Date(Date.now() + 86400000).toISOString(), retain_until: null,
  };
  return c;
}

async function fixture(c = charter(), overrides: Partial<Scope> = {}): Promise<Scope> {
  const scope = { tenantId: `tenant-mc020-${id()}`, workspaceId: `ws-mc020-${id()}`, ...overrides };
  await owner.query("INSERT INTO tenants (id,name) VALUES ($1,'Breaker fixture') ON CONFLICT (id) DO NOTHING", [scope.tenantId]);
  await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'Breaker fixture',$1,'test')", [scope.workspaceId, scope.tenantId]);
  await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test',$3)", [scope.workspaceId, scope.tenantId, JSON.stringify({ charter: c, preserved: { note: "unrelated archive data" } })]);
  return scope;
}

async function summary(scope: Scope, after: Record<string, unknown>, action = "store.daily.summary"): Promise<string> {
  const result = await gatewayAppend(gateway, { ...scope, actor: { id: "system", type: "system" } }, {
    who: { id: "system", type: "system" },
    context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
    object: { type: "metric_snapshot", id: scope.workspaceId },
    decision: { action, after }, rule_impact: [], receipt: { mode: "simulated", synced: true },
  });
  return result.eventId;
}

async function events(scope: Scope, action?: string): Promise<BusinessEvent[]> {
  return (await owner.query<{ payload: BusinessEvent }>(
    "SELECT payload FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2 AND ($3::text IS NULL OR payload->'decision'->>'action'=$3) ORDER BY seq",
    [scope.tenantId, scope.workspaceId, action ?? null],
  )).rows.map((r) => r.payload);
}

async function state(scope: Scope) {
  const profile = await owner.query("SELECT archive,updated_at FROM profiles WHERE workspace_id=$1", [scope.workspaceId]);
  return { profile: profile.rows[0], events: await events(scope) };
}

async function assertTightened(scope: Scope, original: Charter, metric: string, actual: number, floor: number) {
  const c = await loadCharter(app, scope);
  const expected = tightenAutonomy(original);
  expect(c.autonomy).toEqual(expected.autonomy);
  expect(c.circuit_breaker).toEqual({ ...original.circuit_breaker, tightened: true });
  expect(c.grant).toEqual(original.grant);
  expect(c.mode).toBe(original.mode);
  expect(c.escalate).toEqual(original.escalate);
  const rows = await events(scope, "ceo.circuit_breaker");
  expect(rows).toHaveLength(1);
  expect(rows[0]!.context).toMatchObject({ tenant_id: scope.tenantId, workspace_id: scope.workspaceId });
  expect(rows[0]!.decision.params).toEqual({ metric, actual, floor });
  expect(rows[0]!.decision.after).toEqual({ tightened_to: expected.autonomy });
  expect(rows[0]!.decision.basis).toHaveLength(2);
  expect((await state(scope)).profile.archive.preserved).toEqual({ note: "unrelated archive data" });
}

async function withFailure(table: "profiles" | "biz_events", scope: Scope, fn: () => Promise<void>) {
  const name = `mc020_${id()}`;
  const literal = scope.workspaceId.replaceAll("'", "''");
  await owner.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC020 injected write failure'; END $$`);
  try {
    await owner.query(`CREATE TRIGGER ${name} BEFORE ${table === "profiles" ? "UPDATE" : "INSERT"} ON ${table} FOR EACH ROW WHEN (NEW.workspace_id='${literal}') EXECUTE FUNCTION ${name}()`);
    await fn();
  } finally {
    await owner.query(`DROP TRIGGER IF EXISTS ${name} ON ${table}`);
    await owner.query(`DROP FUNCTION ${name}()`);
  }
}

async function waitForBlocked(pid: number): Promise<void> {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const r = await owner.query("SELECT 1 FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid))", [pid]);
    if (r.rows.length) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("MC020 expected the breaker to wait for the concurrent charter edit");
}

async function waitForBlockedBeats(count: number): Promise<void> {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const r = await owner.query<{ n: number }>(
      "SELECT count(*)::int AS n FROM pg_stat_activity WHERE application_name=$1 AND usename='workloom_app' AND wait_event_type='Lock'",
      [applicationName],
    );
    if (r.rows[0]!.n >= count) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`MC020 expected ${count} blocked breaker transactions before releasing the profile`);
}

async function beatDuringEdit(scope: Scope, next: Charter) {
  const writer = await owner.connect();
  let beat: ReturnType<typeof runBreakerBeat> | undefined;
  try {
    await writer.query("BEGIN");
    const pid = Number((await writer.query("SELECT pg_backend_pid() AS pid")).rows[0].pid);
    await writer.query("UPDATE profiles SET archive=jsonb_set(archive,'{charter}',$2::jsonb) WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(next)]);
    beat = runBreakerBeat(app, scope);
    await waitForBlocked(pid);
    await writer.query("COMMIT");
    return await beat;
  } finally {
    await writer.query("ROLLBACK");
    writer.release();
    if (beat) await Promise.allSettled([beat]);
  }
}

suite("MC020 generic KPI circuit breaker on real PostgreSQL", () => {
  beforeAll(async () => {
    const urls = config();
    const pool = (url: URL) => new pg.Pool({ connectionString: url.toString(), application_name: applicationName, max: 16, connectionTimeoutMillis: 5000, statement_timeout: 15000 });
    owner = pool(urls.owner); app = pool(urls.app); gateway = pool(urls.gateway);
    for (const [db, role] of [[app, "workloom_app"], [gateway, "workloom_gateway"]] as const) {
      expect((await db.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
        .toMatchObject({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    const tables = await owner.query("SELECT relname,relrowsecurity FROM pg_class WHERE oid=ANY($1::regclass[])", [["profiles", "biz_events"]]);
    expect(tables.rows).toHaveLength(2);
    expect(tables.rows.every((r) => r.relrowsecurity)).toBe(true);
    vi.stubGlobal("fetch", noNetwork);
  });
  afterAll(async () => {
    vi.unstubAllGlobals();
    await Promise.all([owner?.end(), app?.end(), gateway?.end()]);
    expect(noNetwork).not.toHaveBeenCalled();
  });

  it.each([0.62, 0.61, 0])("quality=%s below the declared floor tightens once with matching evidence", async (value) => {
    const original = charter();
    const scope = await fixture(original);
    await summary(scope, { quality: value, label: "snapshot" });
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    await assertTightened(scope, original, "quality", value, 0.7);
  });

  it.each([0.7, 0.9])("quality=%s at or above the floor does not mutate state", async (value) => {
    const scope = await fixture();
    await summary(scope, { quality: value });
    const before = await state(scope);
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: false });
    expect(await state(scope)).toEqual(before);
  });

  it.each([
    { label: "null", value: null }, { label: "numeric string", value: "0.2" },
    { label: "NaN string", value: "NaN" }, { label: "empty string", value: "" },
    { label: "false", value: false }, { label: "true", value: true },
    { label: "empty array", value: [] }, { label: "numeric array", value: [0] },
    { label: "empty object", value: {} }, { label: "numeric object", value: { value: 0 } },
    { label: "NaN", value: NaN }, { label: "Infinity", value: Infinity }, { label: "-Infinity", value: -Infinity },
  ])("invalid KPI $label is never coerced to a measurement", async ({ value }) => {
      const scope = await fixture();
      await summary(scope, { quality: value });
      const before = await state(scope);
      expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: false });
      expect(await state(scope)).toEqual(before);
    });

  it.each([
    { label: "numeric string", value: "0.2" }, { label: "empty string", value: "" },
    { label: "false", value: false }, { label: "empty array", value: [] }, { label: "numeric array", value: [0] },
  ])("legacy-supported non-number $label is not coerced into a breach", async ({ value }) => {
    const scope = await fixture(charter("active", { occ: 0.7 }));
    await summary(scope, { occ: value });
    const before = await state(scope);
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: false });
    expect(await state(scope)).toEqual(before);
  });

  it("a missing latest metric is not backfilled from an older summary or a different action", async () => {
    const scope = await fixture();
    await summary(scope, { quality: 0.1 });
    await summary(scope, { unrelated: 0 });
    await summary(scope, { quality: 0.1 }, "unrelated.summary");
    const before = await state(scope);
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: false });
    expect(await state(scope)).toEqual(before);
  });

  it("no summary and no configured floors are non-tripping states", async () => {
    const scopes = [await fixture(), await fixture(charter("active", {}))];
    await summary(scopes[1]!, { quality: 0 });
    for (const scope of scopes) {
      const before = await state(scope);
      expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: false });
      expect(await state(scope)).toEqual(before);
    }
  });

  it("arbitrary metric names and negative/zero thresholds preserve exact values", async () => {
    for (const [metric, floor, actual] of [["质量.rate/\"'", -1, -2], ["zero-floor", 0, -1]] as const) {
      const original = charter("active", { [metric]: floor });
      const scope = await fixture(original);
      await summary(scope, { [metric]: actual });
      expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
      await assertTightened(scope, original, metric, actual, floor);
    }
  });

  it("an unrelated or invalid metric cannot hide another configured breach", async () => {
    const original = charter("active", { invalid: 0.7, healthy: 0.7, quality: 0.7 });
    const scope = await fixture(original);
    await summary(scope, { invalid: "0", healthy: 0.9, quality: 0.4, unrelated: -100 });
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    await assertTightened(scope, original, "quality", 0.4, 0.7);
  });

  it("latest same-workspace summary wins over a newer foreign workspace summary", async () => {
    const original = charter();
    const a = await fixture(original);
    const b = await fixture(charter(), { tenantId: a.tenantId });
    const c = await fixture();
    await summary(a, { quality: 0.95 });
    await summary(a, { quality: 0.4 });
    await summary(b, { quality: 0.99 });
    await summary(c, { quality: 0.01 });
    const foreign = [await state(b), await state(c)];
    expect(await runBreakerBeat(app, a)).toEqual({ tripped: true, tightened: true });
    await assertTightened(a, original, "quality", 0.4, 0.7);
    expect([await state(b), await state(c)]).toEqual(foreign);
  });

  it.each(["disabled", "shadow", "suspended"] as const)("%s mode never tightens or emits a breaker event", async (mode) => {
    const scope = await fixture(charter(mode));
    await summary(scope, { quality: 0 });
    const before = await state(scope);
    expect(await runBreakerBeat(app, scope)).toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining(mode) });
    expect(await state(scope)).toEqual(before);
  });

  it("trial mode applies the stored tighten formula once and keeps the grant/mode", async () => {
    const original = charter("trial");
    const scope = await fixture(original);
    await summary(scope, { quality: 0.4 });
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    await assertTightened(scope, original, "quality", 0.4, 0.7);
  });

  it.each(["trial", "active"] as const)("expired %s authorization suspends before KPI execution", async (mode) => {
    const original = charter(mode);
    if (mode === "trial") original.grant!.trial_ends_at = new Date(Date.now() - 1000).toISOString();
    else original.grant!.retain_until = new Date(Date.now() - 1000).toISOString();
    const scope = await fixture(original);
    await summary(scope, { quality: 0 });
    expect(await runBreakerBeat(app, scope)).toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining("suspended") });
    expect((await loadCharter(app, scope)).mode).toBe("suspended");
    expect((await loadCharter(app, scope)).autonomy).toEqual(original.autonomy);
    const modeChanges = await events(scope, "captain.mode_change");
    expect(modeChanges).toHaveLength(1);
    expect(modeChanges[0]!.decision.basis).toContain(
      `${mode === "trial" ? "试用" : "保留"}截止 ${mode === "trial" ? original.grant!.trial_ends_at : original.grant!.retain_until}`,
    );
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  });

  it("repeated and recovered observations never halve the already-tightened charter again", async () => {
    const original = charter();
    const scope = await fixture(original);
    await summary(scope, { quality: 0.2 });
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    const tightened = await state(scope);
    for (let i = 0; i < 4; i++) expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    expect(await state(scope)).toEqual(tightened);
    await summary(scope, { quality: 0.9 });
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: false, tightened: true });
    await assertTightened(scope, original, "quality", 0.2, 0.7);
  });

  it("twelve concurrent beats for a legacy-supported metric commit one tightening event", async () => {
    // This metric was already projected before MC020, isolating the pre-existing race.
    const original = charter("active", { occ: 0.7 });
    const scope = await fixture(original);
    await summary(scope, { occ: 0.4 });
    const writer = await owner.connect();
    const beats: ReturnType<typeof runBreakerBeat>[] = [];
    try {
      await writer.query("BEGIN");
      await writer.query("SELECT 1 FROM profiles WHERE workspace_id=$1 FOR UPDATE", [scope.workspaceId]);
      for (let i = 0; i < 12; i++) beats.push(runBreakerBeat(app, scope));
      await waitForBlockedBeats(12);
      await writer.query("COMMIT");
      const results = await Promise.all(beats);
      expect(results.every((r) => r.tripped && r.tightened)).toBe(true);
    } finally {
      await writer.query("ROLLBACK");
      writer.release();
      await Promise.allSettled(beats);
    }
    await assertTightened(scope, original, "occ", 0.4, 0.7);
  }, 15000);

  it("a concurrent user tightening is preserved without a stale overwrite or duplicate event", async () => {
    const original = charter("active", { occ: 0.7 });
    const scope = await fixture(original);
    await summary(scope, { occ: 0.4 });
    const userEdit = tightenAutonomy(original);
    userEdit.autonomy.caps.spend!.limit = 1000;
    userEdit.identity.name = "Updated charter";
    expect(await beatDuringEdit(scope, userEdit)).toEqual({ tripped: true, tightened: true });
    expect(await loadCharter(app, scope)).toEqual(userEdit);
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  }, 15000);

  it("a concurrent authorization revoke takes effect before the breaker writes", async () => {
    const original = charter("active", { occ: 0.7 });
    const scope = await fixture(original);
    await summary(scope, { occ: 0.4 });
    const revoked = { ...original, mode: "suspended" as const };
    expect(await beatDuringEdit(scope, revoked)).toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining("suspended") });
    expect(await loadCharter(app, scope)).toEqual(revoked);
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  }, 15000);

  it("a concurrent authorization expiry is evaluated from the committed charter", async () => {
    const original = charter("active", { occ: 0.7 });
    const scope = await fixture(original);
    await summary(scope, { occ: 0.4 });
    const expired = structuredClone(original);
    expired.grant!.retain_until = new Date(Date.now() - 1000).toISOString();
    expect(await beatDuringEdit(scope, expired))
      .toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining("suspended") });
    const current = await loadCharter(app, scope);
    expect(current.mode).toBe("suspended");
    expect(current.autonomy).toEqual(original.autonomy);
    expect(current.grant).toEqual(expired.grant);
    expect(await events(scope, "captain.mode_change")).toHaveLength(1);
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  }, 15000);

  it("concurrent expiry beats append one suspension event", async () => {
    const original = charter("trial", { occ: 0.7 });
    original.grant!.trial_ends_at = new Date(Date.now() - 1000).toISOString();
    const scope = await fixture(original);
    await summary(scope, { occ: 0.4 });
    const writer = await owner.connect();
    const beats: ReturnType<typeof runBreakerBeat>[] = [];
    try {
      await writer.query("BEGIN");
      await writer.query("SELECT 1 FROM profiles WHERE workspace_id=$1 FOR UPDATE", [scope.workspaceId]);
      for (let i = 0; i < 8; i++) beats.push(runBreakerBeat(app, scope));
      await waitForBlockedBeats(8);
      await writer.query("COMMIT");
      const results = await Promise.all(beats);
      expect(results.every((r) => !r.tripped && !r.tightened && r.skipped?.includes("suspended"))).toBe(true);
    } finally {
      await writer.query("ROLLBACK");
      writer.release();
      await Promise.allSettled(beats);
    }
    expect((await loadCharter(app, scope)).mode).toBe("suspended");
    expect((await loadCharter(app, scope)).autonomy).toEqual(original.autonomy);
    expect(await events(scope, "captain.mode_change")).toHaveLength(1);
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  }, 15000);

  it("concurrent workspaces independently tighten their own charter and event chain", async () => {
    const original = charter();
    const scopes = await Promise.all(Array.from({ length: 4 }, () => fixture(original)));
    for (const scope of scopes) await summary(scope, { quality: 0.3 });
    expect(await Promise.all(scopes.map((scope) => runBreakerBeat(app, scope))))
      .toEqual(scopes.map(() => ({ tripped: true, tightened: true })));
    for (const scope of scopes) await assertTightened(scope, original, "quality", 0.3, 0.7);
  });

  it("a locked profile in one workspace does not block another workspace's breaker", async () => {
    const original = charter("active", { occ: 0.7 });
    const a = await fixture(original);
    const b = await fixture(original, { tenantId: a.tenantId });
    await summary(a, { occ: 0.4 });
    await summary(b, { occ: 0.4 });
    const writer = await owner.connect();
    let waiting: ReturnType<typeof runBreakerBeat> | undefined;
    let other: ReturnType<typeof runBreakerBeat> | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await writer.query("BEGIN");
      const pid = Number((await writer.query("SELECT pg_backend_pid() AS pid")).rows[0].pid);
      await writer.query("SELECT 1 FROM profiles WHERE workspace_id=$1 FOR UPDATE", [a.workspaceId]);
      waiting = runBreakerBeat(app, a);
      await waitForBlocked(pid);
      other = runBreakerBeat(app, b);
      const result = await Promise.race([
        other,
        new Promise<never>((_, reject) => {
          timeout = setTimeout(() => reject(new Error("A foreign workspace blocked the breaker")), 3000);
        }),
      ]);
      expect(result).toEqual({ tripped: true, tightened: true });
      await assertTightened(b, original, "occ", 0.4, 0.7);
      await writer.query("COMMIT");
      expect(await waiting).toEqual({ tripped: true, tightened: true });
    } finally {
      if (timeout) clearTimeout(timeout);
      await writer.query("ROLLBACK");
      writer.release();
      await Promise.allSettled([waiting, other].filter((p) => p !== undefined));
    }
    await assertTightened(a, original, "occ", 0.4, 0.7);
  }, 15000);

  it("a mismatched tenant/workspace scope cannot read or overwrite the stored charter", async () => {
    const actual = await fixture(charter("active", { occ: 0.7 }));
    const foreign = await fixture();
    await summary(actual, { occ: 0.4 });
    const before = await state(actual);
    expect(await runBreakerBeat(app, { tenantId: foreign.tenantId, workspaceId: actual.workspaceId }))
      .toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining("disabled") });
    expect(await state(actual)).toEqual(before);
    expect((await owner.query("SELECT 1 FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2", [foreign.tenantId, actual.workspaceId])).rows).toHaveLength(0);
  });

  it.each(["profiles", "biz_events"] as const)("%s write failure rolls back the full beat and permits one retry", async (table) => {
    const original = charter();
    const scope = await fixture(original);
    await summary(scope, { quality: 0.3 });
    const before = await state(scope);
    await withFailure(table, scope, async () => {
      await expect(runBreakerBeat(app, scope)).rejects.toThrow("MC020 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    expect(await runBreakerBeat(app, scope)).toEqual({ tripped: true, tightened: true });
    await assertTightened(scope, original, "quality", 0.3, 0.7);
  });

  it("an expiry event failure rolls back the suspension and succeeds on retry", async () => {
    const original = charter("trial");
    original.grant!.trial_ends_at = new Date(Date.now() - 1000).toISOString();
    const scope = await fixture(original);
    await summary(scope, { quality: 0.3 });
    const before = await state(scope);
    await withFailure("biz_events", scope, async () => {
      await expect(runBreakerBeat(app, scope)).rejects.toThrow("MC020 injected write failure");
    });
    expect(await state(scope)).toEqual(before);
    expect(await runBreakerBeat(app, scope))
      .toMatchObject({ tripped: false, tightened: false, skipped: expect.stringContaining("suspended") });
    expect((await loadCharter(app, scope)).mode).toBe("suspended");
    expect(await events(scope, "captain.mode_change")).toHaveLength(1);
    expect(await events(scope, "ceo.circuit_breaker")).toHaveLength(0);
  });
});
