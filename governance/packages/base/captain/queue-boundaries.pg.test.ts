/**
 * MC022: queue authority, input freshness and scope at the PostgreSQL commit
 * boundary. Opt in with RUN_CAPTAIN_QUEUE_BOUNDARY_PG_TESTS=1 and three roles on
 * one loopback workloom_tiger_* database. Run serially with other same-database
 * suites: fault injection creates temporary triggers. All fixtures are simulated
 * and retained for audit; every model answer is local and deterministic.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { BusinessEvent } from "@workloom/shared";
import { defaultCharter, tightenAutonomy, type Charter } from "./charter.js";
import { runQueueBeat, type Scope } from "./loop.js";
import { gatewayAppend } from "../workdata/gateway.js";
import { eventHash } from "../workdata/events.js";

const suite = process.env.RUN_CAPTAIN_QUEUE_BOUNDARY_PG_TESTS === "1" ? describe : describe.skip;
const id = () => randomUUID().replaceAll("-", "");
const applicationName = `mc022-${id()}`;
let owner: pg.Pool;
let app: pg.Pool;
let gateway: pg.Pool;
const noNetwork = vi.fn(async () => { throw new Error("MC022 forbids external network/model requests"); });

function config(): Record<"owner" | "app" | "gateway", URL> {
  const inputs = { owner: process.env.DATABASE_URL, app: process.env.DATABASE_APP_URL, gateway: process.env.DATABASE_GATEWAY_URL };
  const urls = {} as Record<keyof typeof inputs, URL>;
  for (const key of ["owner", "app", "gateway"] as const) {
    let url: URL;
    try { url = new URL(inputs[key] ?? ""); }
    catch { throw new Error(`MC022 requires a valid ${key} database URL`); }
    if (!["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error("MC022 requires a loopback workloom_tiger_ test database");
    }
    urls[key] = url;
  }
  for (const key of ["app", "gateway"] as const) {
    if (urls[key].hostname !== urls.owner.hostname || urls[key].port !== urls.owner.port
      || urls[key].pathname !== urls.owner.pathname) throw new Error("MC022 pools must share one local test database");
  }
  return urls;
}

function charter(mode: Charter["mode"] = "active"): Charter {
  const c = defaultCharter();
  c.mode = mode;
  c.autonomy = {
    ranges: { change: { label: "变更范围", lower: 0, anchor: 1, upper: 2 } },
    caps: { spend: { label: "支出上限", limit: 5000 } },
  };
  c.grant = {
    event_id: "E-MC022-GRANT", granted_by: "MEM-MC022", granted_at: new Date().toISOString(),
    disclosure_version: "fixture-v1", clauses: ["simulation only"], shadow_days: 3, trial_days: 7,
    trial_ends_at: new Date(Date.now() + 86400000).toISOString(), retain_until: null,
  };
  return c;
}

async function fixture(c = charter(), tenantId?: string): Promise<Scope> {
  const scope = { tenantId: tenantId ?? `tenant-mc022-${id()}`, workspaceId: `ws-mc022-${id()}` };
  await owner.query("INSERT INTO tenants (id,name) VALUES ($1,'Queue boundary fixture') ON CONFLICT (id) DO NOTHING", [scope.tenantId]);
  await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'Queue boundary fixture',$1,'test')", [scope.workspaceId, scope.tenantId]);
  await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test',$3)", [scope.workspaceId, scope.tenantId, JSON.stringify({ charter: c, preserved: { note: "unrelated archive data" } })]);
  return scope;
}

async function approval(scope: Scope, snapshot: Record<string, unknown> = {}, approvalId = `apr-mc022-${id()}`): Promise<string> {
  const event = await gatewayAppend(gateway, { ...scope, actor: { id: "system", type: "system" } }, {
    who: { id: "system", type: "system" },
    context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
    object: { type: "record", id: approvalId },
    decision: { action: "record.update", kind: "review", params: {} },
    rule_impact: [], receipt: { mode: "simulated", synced: true },
  });
  await owner.query(
    `INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,status,tier,snapshot)
     VALUES ($1,$2,$3,$4,'pending','l2_captain',$5)`,
    [approvalId, scope.tenantId, scope.workspaceId, event.eventId,
      JSON.stringify({ action: "record.update", title: "Queue boundary fixture", params: { note: "original input" }, ...snapshot })],
  );
  return approvalId;
}

async function state(scope: Scope) {
  return {
    approvals: (await owner.query("SELECT * FROM approvals WHERE workspace_id=$1 ORDER BY approval_id", [scope.workspaceId])).rows,
    events: (await owner.query("SELECT tenant_id,workspace_id,payload FROM biz_events WHERE workspace_id=$1 ORDER BY seq", [scope.workspaceId])).rows,
    profile: (await owner.query("SELECT archive,tenant_id,updated_at FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0],
    workspace: (await owner.query("SELECT * FROM workspaces WHERE id=$1", [scope.workspaceId])).rows[0],
  };
}

async function events(scope: Scope, action = "ceo.decision"): Promise<BusinessEvent[]> {
  return (await owner.query<{ payload: BusinessEvent }>(
    "SELECT payload FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'=$2 ORDER BY seq",
    [scope.workspaceId, action],
  )).rows.map((row) => row.payload);
}

function modelAnswer(prompt: string, verdict: "approve" | "escalate" = "approve") {
  if (prompt.includes("决策分析师")) return JSON.stringify([{ label: "Fixture option", params: {}, stance: "conservative" }]);
  if (prompt.includes("红队评审")) return "Fixture risk reviewed";
  return JSON.stringify({ verdict, rationale: "Fixture deterministic rationale" });
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function bounded<T>(promise: Promise<T>, reason: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(reason)), 5000);
    })]);
  } finally { if (timer) clearTimeout(timer); }
}

function modelGate(verdict: "approve" | "escalate" = "approve", arrivals = 1) {
  const entered = deferred(), release = deferred();
  let count = 0;
  const llmCall = vi.fn(async (prompt: string) => {
    if (++count === arrivals) entered.resolve();
    await release.promise;
    return modelAnswer(prompt, verdict);
  });
  return { llmCall, entered: entered.promise, release: release.resolve };
}

async function duringModel(scope: Scope, change: () => Promise<unknown>, verdict: "approve" | "escalate" = "approve") {
  const gate = modelGate(verdict);
  const beat = runQueueBeat(app, scope, { llmCall: gate.llmCall });
  try {
    await bounded(gate.entered, "The local model was not reached");
    // These writes must complete while reasoning is paused: a model must never
    // keep database locks that prevent the owner from revoking authorization.
    await bounded(change(), "Queue reasoning held a database lock against a concurrent edit");
    const changed = await state(scope);
    gate.release();
    return { result: await beat, changed };
  } finally {
    gate.release();
    await Promise.allSettled([beat]);
  }
}

async function saveCharter(scope: Scope, next: Charter) {
  await owner.query("UPDATE profiles SET archive=jsonb_set(archive,'{charter}',$2::jsonb),updated_at=now() WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(next)]);
}

async function waitForBlocked(pid: number) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const row = await owner.query("SELECT 1 FROM pg_stat_activity WHERE application_name=$1 AND $2=ANY(pg_blocking_pids(pid))", [applicationName, pid]);
    if (row.rows.length) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("MC022 expected a queue transaction to wait for the concurrent database lock");
}

async function waitUntilExpired(deadline: string) {
  await owner.query("SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM $1::timestamptz-clock_timestamp())) + 0.02)", [deadline]);
}

async function withFailure(table: "approvals" | "biz_events" | "profiles", scope: Scope, fn: () => Promise<void>) {
  const name = `mc022_${id()}`;
  const literal = scope.workspaceId.replaceAll("'", "''");
  await owner.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC022 injected write failure'; END $$`);
  try {
    await owner.query(`CREATE TRIGGER ${name} BEFORE ${table === "biz_events" ? "INSERT" : "UPDATE"} ON ${table} FOR EACH ROW WHEN (NEW.workspace_id='${literal}') EXECUTE FUNCTION ${name}()`);
    await fn();
  } finally {
    await owner.query(`DROP TRIGGER IF EXISTS ${name} ON ${table}`);
    await owner.query(`DROP FUNCTION ${name}()`);
  }
}

suite("MC022 queue commit boundaries on real PostgreSQL", () => {
  beforeAll(async () => {
    const urls = config();
    const pool = (url: URL) => new pg.Pool({ connectionString: url.toString(), application_name: applicationName, max: 20, connectionTimeoutMillis: 5000, statement_timeout: 12000 });
    owner = pool(urls.owner); app = pool(urls.app); gateway = pool(urls.gateway);
    for (const [db, role] of [[app, "workloom_app"], [gateway, "workloom_gateway"]] as const) {
      expect((await db.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
        .toMatchObject({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    const tables = await owner.query("SELECT relname,relrowsecurity FROM pg_class WHERE oid=ANY($1::regclass[])", [["workspaces", "profiles", "approvals", "biz_events"]]);
    expect(tables.rows).toHaveLength(4);
    expect(tables.rows.every((row) => row.relrowsecurity)).toBe(true);
    vi.stubGlobal("fetch", noNetwork);
  });
  afterAll(async () => {
    vi.unstubAllGlobals();
    await Promise.all([owner?.end(), app?.end(), gateway?.end()]);
    expect(noNetwork).not.toHaveBeenCalled();
  });

  it.each(["active", "trial", "shadow"] as const)("%s keeps normal approval and escalation behavior", async (mode) => {
    for (const verdict of ["approve", "escalate"] as const) {
      const scope = await fixture(charter(mode));
      const approvalId = await approval(scope, { params: { note: "引号 ' \" <req> / 特殊字符\\n" } });
      const before = await state(scope);
      const result = await runQueueBeat(app, scope, { llmCall: async (prompt) => modelAnswer(prompt, verdict) });
      expect(result).toMatchObject({ decided: verdict === "approve" ? 1 : 0, escalated: verdict === "escalate" ? 1 : 0 });
      const after = await state(scope);
      if (mode === "shadow") expect(after.approvals).toEqual(before.approvals);
      else expect(after.approvals[0]).toMatchObject({ status: verdict === "approve" ? "approved" : "pending", tier: verdict === "approve" ? "l2_captain" : "l4_chairman" });
      const decisions = await events(scope);
      expect(decisions).toHaveLength(1);
      expect(decisions[0]!.context).toMatchObject({ tenant_id: scope.tenantId, workspace_id: scope.workspaceId });
      expect(decisions[0]!.decision.params).toMatchObject({ approval_id: approvalId, verdict, mode });
      expect((decisions[0]!.decision.params as Record<string, unknown>).dry_run).toBe(mode === "shadow" ? true : undefined);
      expect(after.profile).toEqual(before.profile);
    }
  });

  it.each(["approve", "escalate"] as const)("eight concurrent active %s beats commit exactly one real decision", async (verdict) => {
    const scope = await fixture();
    await approval(scope);
    const gate = modelGate(verdict, 8);
    const beats = Array.from({ length: 8 }, () => runQueueBeat(app, scope, { llmCall: gate.llmCall }));
    try {
      await bounded(gate.entered, "Concurrent beats did not all reach the model outside locks");
      gate.release();
      const results = await Promise.all(beats);
      expect(results.reduce((n, row) => n + row.decided + row.escalated, 0)).toBe(1);
      expect(await events(scope)).toHaveLength(1);
      const settled = await state(scope);
      expect(await runQueueBeat(app, scope)).toMatchObject({ decided: 0, escalated: 0 });
      expect(await state(scope)).toEqual(settled);
    } finally { gate.release(); await Promise.allSettled(beats); }
  });

  it("concurrent approve and escalate outcomes cannot both commit", async () => {
    const scope = await fixture();
    await approval(scope);
    const approve = modelGate("approve"), escalate = modelGate("escalate");
    const beats = [runQueueBeat(app, scope, { llmCall: approve.llmCall }), runQueueBeat(app, scope, { llmCall: escalate.llmCall })];
    try {
      await bounded(Promise.all([approve.entered, escalate.entered]), "Both verdicts must reach reasoning");
      approve.release(); escalate.release();
      const results = await Promise.all(beats);
      expect(results.reduce((n, row) => n + row.decided + row.escalated, 0)).toBe(1);
      expect(await events(scope)).toHaveLength(1);
    } finally { approve.release(); escalate.release(); await Promise.allSettled(beats); }
  });

  it("concurrent shadow reasoning preserves repeatable dry-run diagnostics", async () => {
    const scope = await fixture(charter("shadow"));
    await approval(scope);
    const before = await state(scope);
    const gate = modelGate("escalate", 4);
    const beats = Array.from({ length: 4 }, () => runQueueBeat(app, scope, { llmCall: gate.llmCall }));
    try {
      await bounded(gate.entered, "Shadow model calls did not arrive"); gate.release();
      expect((await Promise.all(beats)).every((row) => row.escalated === 1 && row.decided === 0)).toBe(true);
    } finally { gate.release(); await Promise.allSettled(beats); }
    expect((await state(scope)).approvals).toEqual(before.approvals);
    expect((await state(scope)).profile).toEqual(before.profile);
    expect((await events(scope)).every((row) => (row.decision.params as Record<string, unknown>).dry_run === true)).toBe(true);
    expect(await events(scope)).toHaveLength(4);
  });

  it.each([
    ["active", "suspended"], ["active", "disabled"], ["active", "shadow"], ["active", "trial"],
    ["shadow", "active"], ["trial", "active"], ["shadow", "suspended"],
  ] as const)("%s to %s during reasoning invalidates the previous verdict", async (from, to) => {
    const original = charter(from), scope = await fixture(original);
    await approval(scope);
    const { result, changed } = await duringModel(scope, () => saveCharter(scope, { ...original, mode: to }));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
    expect(await events(scope)).toHaveLength(0);
  });

  it.each(["tightened", "new grant", "charter ABA", "profile ABA"] as const)("%s during reasoning invalidates the captured authority version", async (variant) => {
    const original = charter(), scope = await fixture(original);
    await approval(scope, { autonomy_amount: 2000, autonomy_cap_key: "spend" });
    const { result, changed } = await duringModel(scope, async () => {
      if (variant === "tightened") await saveCharter(scope, tightenAutonomy(original));
      if (variant === "new grant") await saveCharter(scope, { ...original, grant: { ...original.grant!, event_id: "E-MC022-REGRANT" } });
      if (variant === "charter ABA") { await saveCharter(scope, { ...original, mode: "suspended" }); await saveCharter(scope, original); }
      if (variant === "profile ABA") {
        await owner.query("UPDATE profiles SET archive=archive || '{\"temporary\":true}'::jsonb WHERE workspace_id=$1", [scope.workspaceId]);
        await owner.query("UPDATE profiles SET archive=archive - 'temporary' WHERE workspace_id=$1", [scope.workspaceId]);
      }
    });
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each(["l3_fleet", "l4_chairman"])("tier changed to %s during reasoning is never decided as L2", async (tier) => {
    const scope = await fixture(), approvalId = await approval(scope);
    const { result, changed } = await duringModel(scope, () => owner.query("UPDATE approvals SET tier=$2 WHERE approval_id=$1", [approvalId, tier]));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each(["approved", "edited", "rejected", "expired", "superseded"])("a concurrent %s decision keeps its identity and produces no CEO duplicate", async (status) => {
    const scope = await fixture(), approvalId = await approval(scope);
    const { result, changed } = await duringModel(scope, () => owner.query("UPDATE approvals SET status=$2,decided_by='fixture-human',decided_at=now() WHERE approval_id=$1", [approvalId, status]));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each([
    { label: "parameters", patch: { params: { amount: 9000, note: "changed" } } },
    { label: "action", patch: { action: "record.delete" } },
    { label: "range", patch: { autonomy_range_key: "change", autonomy_range_value: 9 } },
    { label: "amount", patch: { autonomy_cap_key: "spend", autonomy_amount: 10000 } },
    { label: "irreversibility", patch: { irreversible: true } },
    { label: "domains", patch: { affected_domains: ["one", "two"] } },
    { label: "proposal version", patch: { proposal_hash: "changed", config_version: "v2" } },
    { label: "new future expiry", patch: { expires_at: "2099-01-01T00:00:00Z" } },
    { label: "new past expiry", patch: { expires_at: "2000-01-01T00:00:00Z" } },
    { label: "malformed expiry", patch: { expires_at: "not-a-timestamp" } },
  ])("changed $label invalidates the previous approval input", async ({ patch }) => {
    const scope = await fixture(), approvalId = await approval(scope);
    const { result, changed } = await duringModel(scope, () => owner.query("UPDATE approvals SET snapshot=snapshot || $2::jsonb WHERE approval_id=$1", [approvalId, JSON.stringify(patch)]));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each(["consumed", "event rebound", "approval ABA", "recreated"] as const)("%s during reasoning invalidates the captured approval row", async (variant) => {
    const scope = await fixture(), approvalId = await approval(scope);
    const { result, changed } = await duringModel(scope, async () => {
      if (variant === "consumed") await owner.query("UPDATE approvals SET consumed_at=now() WHERE approval_id=$1", [approvalId]);
      if (variant === "event rebound") await owner.query("UPDATE approvals SET event_id=$2 WHERE approval_id=$1", [approvalId, `E-MC022-REBIND-${id()}`]);
      if (variant === "approval ABA") {
        await owner.query("UPDATE approvals SET tier='l4_chairman' WHERE approval_id=$1", [approvalId]);
        await owner.query("UPDATE approvals SET tier='l2_captain' WHERE approval_id=$1", [approvalId]);
      }
      if (variant === "recreated") {
        const row = (await owner.query("SELECT row_to_json(a) AS row FROM approvals a WHERE approval_id=$1", [approvalId])).rows[0]!.row;
        await owner.query("DELETE FROM approvals WHERE approval_id=$1", [approvalId]);
        await owner.query("INSERT INTO approvals SELECT * FROM json_populate_record(NULL::approvals,$1::json)", [JSON.stringify(row)]);
      }
    });
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each(["approval", "active grant", "trial grant"] as const)("unchanged %s that expires during reasoning cannot authorize a decision", async (variant) => {
    const deadline = new Date(Date.now() + 900).toISOString();
    const c = charter(variant === "trial grant" ? "trial" : "active");
    if (variant === "active grant") c.grant!.retain_until = deadline;
    if (variant === "trial grant") c.grant!.trial_ends_at = deadline;
    const scope = await fixture(c);
    await approval(scope, variant === "approval" ? { expires_at: deadline } : {});
    const { result } = await duringModel(scope, () => waitUntilExpired(deadline));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect((await state(scope)).approvals[0]!.status).toBe("pending");
    expect(await events(scope)).toHaveLength(0);
    if (variant !== "approval") {
      expect((await state(scope)).profile.archive.charter.mode).toBe("suspended");
      expect(await events(scope, "captain.mode_change")).toHaveLength(1);
    }
  });

  it.each(["approval lock", "event-chain lock"] as const)("expiry is rechecked after waiting for the %s at commit", async (variant) => {
    const deadline = new Date(Date.now() + 1200).toISOString();
    const scope = await fixture(), approvalId = await approval(scope, { expires_at: deadline });
    const gate = modelGate(), beat = runQueueBeat(app, scope, { llmCall: gate.llmCall });
    const writer = await owner.connect();
    try {
      await bounded(gate.entered, "The expiry fixture did not reach the model");
      await writer.query("BEGIN");
      const pid = Number((await writer.query("SELECT pg_backend_pid() AS pid")).rows[0]!.pid);
      if (variant === "approval lock") await writer.query("SELECT 1 FROM approvals WHERE approval_id=$1 FOR UPDATE", [approvalId]);
      else await writer.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`event-chain:${scope.tenantId}:${scope.workspaceId}`]);
      gate.release();
      await waitForBlocked(pid);
      await waitUntilExpired(deadline);
      await writer.query("COMMIT");
      expect(await beat).toMatchObject({ decided: 0, escalated: 0 });
      expect((await state(scope)).approvals[0]!.status).toBe("pending");
      expect(await events(scope)).toHaveLength(0);
    } finally {
      gate.release(); await writer.query("ROLLBACK"); writer.release(); await Promise.allSettled([beat]);
    }
  }, 15000);

  it.each(["bad date", "", "infinity", "-infinity", 123, false, {}, []]
    .map((expires_at) => ({ expires_at, label: JSON.stringify(expires_at) })))("malformed expiry $label is skipped without blocking a valid next approval", async ({ expires_at }) => {
    const scope = await fixture();
    const bad = await approval(scope, { expires_at }, `apr-mc022-${scope.workspaceId}-a`);
    const good = await approval(scope, {}, `apr-mc022-${scope.workspaceId}-b`);
    const model = vi.fn(async (prompt: string) => modelAnswer(prompt));
    expect(await runQueueBeat(app, scope, { llmCall: model })).toMatchObject({ decided: 1, escalated: 0 });
    expect(model).toHaveBeenCalledTimes(1);
    const rows = (await state(scope)).approvals;
    expect(rows.find((row) => row.approval_id === bad).status).toBe("pending");
    expect(rows.find((row) => row.approval_id === good).status).toBe("approved");
  });

  it("invalid and expired rows cannot starve the 20-item eligible batch", async () => {
    const scope = await fixture();
    for (let n = 0; n < 20; n++) await approval(scope, { expires_at: n % 2 ? "bad date" : "2000-01-01T00:00:00Z" }, `apr-mc022-${scope.workspaceId}-a${String(n).padStart(2, "0")}`);
    await approval(scope, { expires_at: null }, `apr-mc022-${scope.workspaceId}-z`);
    expect(await runQueueBeat(app, scope, { llmCall: async (prompt) => modelAnswer(prompt) })).toMatchObject({ decided: 1, escalated: 0 });
    expect((await state(scope)).approvals.filter((row) => row.status === "pending")).toHaveLength(20);
  });

  it.each(["active", "trial"] as const)("malformed %s grant deadline fails closed before model reasoning", async (mode) => {
    const c = charter(mode);
    if (mode === "active") c.grant!.retain_until = "invalid deadline";
    else c.grant!.trial_ends_at = "invalid deadline";
    const scope = await fixture(c); await approval(scope);
    const before = await state(scope), model = vi.fn(async (prompt: string) => modelAnswer(prompt));
    expect(await runQueueBeat(app, scope, { llmCall: model })).toMatchObject({ decided: 0, escalated: 0, skipped: expect.any(String) });
    expect(model).not.toHaveBeenCalled();
    expect(await state(scope)).toEqual(before);
  });

  it.each([false, true])("mismatched tenant/workspace is rejected before any %s major model read", async (major) => {
    const actual = await fixture(), foreign = await fixture();
    await approval(actual, major ? { irreversible: true } : {});
    const before = await state(actual), model = vi.fn(async (prompt: string) => modelAnswer(prompt));
    const result = await runQueueBeat(app, { tenantId: foreign.tenantId, workspaceId: actual.workspaceId }, { llmCall: model });
    expect(result).toMatchObject({ decided: 0, escalated: 0, skipped: expect.any(String) });
    expect(model).not.toHaveBeenCalled();
    expect(await state(actual)).toEqual(before);
    expect((await owner.query("SELECT 1 FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2", [foreign.tenantId, actual.workspaceId])).rows).toHaveLength(0);
  });

  it.each(["profile", "workspace", "approval"] as const)("a stored %s with the wrong tenant is never used for queue reasoning", async (table) => {
    const actual = await fixture(), foreign = await fixture(), approvalId = await approval(actual);
    if (table === "profile") await owner.query("UPDATE profiles SET tenant_id=$2 WHERE workspace_id=$1", [actual.workspaceId, foreign.tenantId]);
    if (table === "workspace") await owner.query("UPDATE workspaces SET tenant_id=$2 WHERE id=$1", [actual.workspaceId, foreign.tenantId]);
    if (table === "approval") await owner.query("UPDATE approvals SET tenant_id=$2 WHERE approval_id=$1", [approvalId, foreign.tenantId]);
    const before = await state(actual), model = vi.fn(async (prompt: string) => modelAnswer(prompt));
    expect(await runQueueBeat(app, actual, { llmCall: model })).toMatchObject({ decided: 0, escalated: 0 });
    expect(model).not.toHaveBeenCalled();
    expect(await state(actual)).toEqual(before);
  });

  it.each(["profile", "workspace", "approval", "workspace ABA"] as const)("%s ownership changes during reasoning invalidate the old scope", async (table) => {
    const scope = await fixture(), foreign = await fixture(), approvalId = await approval(scope);
    const { result, changed } = await duringModel(scope, async () => {
      if (table === "profile") await owner.query("UPDATE profiles SET tenant_id=$2 WHERE workspace_id=$1", [scope.workspaceId, foreign.tenantId]);
      if (table === "workspace" || table === "workspace ABA") await owner.query("UPDATE workspaces SET tenant_id=$2 WHERE id=$1", [scope.workspaceId, foreign.tenantId]);
      if (table === "approval") await owner.query("UPDATE approvals SET tenant_id=$2 WHERE approval_id=$1", [approvalId, foreign.tenantId]);
      if (table === "workspace ABA") await owner.query("UPDATE workspaces SET tenant_id=$2 WHERE id=$1", [scope.workspaceId, scope.tenantId]);
    });
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it("a deleted profile and an empty queue are safe no-ops", async () => {
    const empty = await fixture();
    expect(await runQueueBeat(app, empty)).toMatchObject({ decided: 0, escalated: 0 });
    const scope = await fixture(); await approval(scope);
    const { result, changed } = await duringModel(scope, () => owner.query("DELETE FROM profiles WHERE workspace_id=$1", [scope.workspaceId]));
    expect(result).toMatchObject({ decided: 0, escalated: 0 });
    expect(await state(scope)).toEqual(changed);
  });

  it.each(["approve", "escalate"] as const)("active %s rolls back state and evidence after either write fails", async (verdict) => {
    for (const table of ["approvals", "biz_events"] as const) {
      const scope = await fixture(); await approval(scope);
      const before = await state(scope);
      await withFailure(table, scope, async () => {
        await expect(runQueueBeat(app, scope, { llmCall: async (prompt) => modelAnswer(prompt, verdict) })).rejects.toThrow("MC022 injected write failure");
      });
      expect(await state(scope)).toEqual(before);
      expect(await runQueueBeat(app, scope, { llmCall: async (prompt) => modelAnswer(prompt, verdict) })).toMatchObject({ decided: verdict === "approve" ? 1 : 0, escalated: verdict === "escalate" ? 1 : 0 });
      expect(await events(scope)).toHaveLength(1);
    }
  });

  it("expiry state and event failures roll back and allow exactly one suspension retry", async () => {
    for (const table of ["profiles", "biz_events"] as const) {
      const c = charter(); c.grant!.retain_until = "2000-01-01T00:00:00Z";
      const scope = await fixture(c); await approval(scope); const before = await state(scope);
      await withFailure(table, scope, async () => { await expect(runQueueBeat(app, scope)).rejects.toThrow("MC022 injected write failure"); });
      expect(await state(scope)).toEqual(before);
      expect(await runQueueBeat(app, scope)).toMatchObject({ decided: 0, escalated: 0, skipped: expect.stringContaining("suspended") });
      expect(await events(scope, "captain.mode_change")).toHaveLength(1);
      expect(await events(scope)).toHaveLength(0);
    }
  });

  it("foreign workspaces remain independent and every local event chain verifies", async () => {
    const a = await fixture(), b = await fixture(charter(), a.tenantId), c = await fixture();
    for (const scope of [a, b, c]) await approval(scope);
    const foreign = [await state(b), await state(c)];
    await runQueueBeat(app, a, { llmCall: async (prompt) => modelAnswer(prompt) });
    expect([await state(b), await state(c)]).toEqual(foreign);
    const rows = await owner.query("SELECT tenant_id,workspace_id,payload,prev_hash,hash FROM biz_events WHERE workspace_id LIKE 'ws-mc022-%' ORDER BY tenant_id,workspace_id,seq");
    const tails = new Map<string, string>();
    for (const row of rows.rows) {
      const key = `${row.tenant_id}:${row.workspace_id}`, previous = tails.get(key) ?? "GENESIS";
      expect(row.payload.context).toMatchObject({ tenant_id: row.tenant_id, workspace_id: row.workspace_id });
      expect(row.prev_hash).toBe(previous); expect(row.hash).toBe(eventHash(previous, row.payload));
      tails.set(key, row.hash);
    }
  });
});
