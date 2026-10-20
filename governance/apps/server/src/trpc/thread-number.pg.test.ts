/** MC-014: real tRPC dispatch, RLS, audit atomicity and durable thread numbering. */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool, getGatewayPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { gatewayAppend } from "@workloom/base/workdata";
import { dispatchFromAnomaly } from "@workloom/base/inspection";
import { appRouter } from "./router.js";

const enabled = process.env.RUN_RUNTIME_SCHEMA_PG_TESTS === "1";
const suite = enabled ? describe : describe.skip;
const id = () => randomUUID().replaceAll("-", "");
const title = "生成本机线程编号回归报告";
function caller(identity: Identity | null) {
  return appRouter.createCaller({ session: identity, identity, partnerIdentity: null, headers: new Headers() });
}

suite("MC-014 actual thread dispatch numbering", () => {
  let owner: pg.Pool;
  let identity: Identity;
  const originalProvider = process.env.LLM_PROVIDER;

  async function makeIdentity(): Promise<Identity> {
    const tenantId = `tenant-thread-${id()}`, workspaceId = `ws-thread-${id()}`, memberId = `member-thread-${id()}`;
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'Thread numbering fixture','pro')", [tenantId]);
    await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'Thread numbering fixture',$1,'test')", [workspaceId, tenantId]);
    await owner.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-THREAD','Thread fixture','owner')", [memberId, workspaceId]);
    await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test',$3)", [workspaceId, tenantId, JSON.stringify({ dataMode: "simulated" })]);
    return { memberId, memberNo: "MEM-THREAD", name: "Thread fixture", role: "owner", tenantId, workspaceId, plan: "pro" };
  }
  async function dispatch(who = identity) {
    const result = await caller(who).threads.dispatch({ title, runImmediately: false });
    expect(result.kind).toBe("routed");
    if (result.kind !== "routed") throw new Error("Thread fixture unexpectedly requested clarification");
    return result;
  }
  async function rows(who = identity) {
    return (await owner.query("SELECT id,title,status FROM threads WHERE workspace_id=$1 ORDER BY id", [who.workspaceId])).rows;
  }
  async function events(who = identity) {
    return (await owner.query("SELECT event_id,session_id,payload FROM biz_events WHERE workspace_id=$1 ORDER BY seq", [who.workspaceId])).rows;
  }
  beforeAll(async () => {
    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)
      || url.hostname !== urls[0]!.hostname || url.port !== urls[0]!.port || url.pathname !== urls[0]!.pathname)) {
      throw new Error("Thread numbering tests require three roles on the same loopback workloom_tiger_ test database");
    }
    process.env.LLM_PROVIDER = "mock";
    owner = new pg.Pool({ connectionString: urls[0]!.toString(), connectionTimeoutMillis: 5000, statement_timeout: 10000 });
    expect((await getAppPool().query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
      .toEqual({ current_user: "workloom_app", rolsuper: false, rolbypassrls: false });
  });
  beforeEach(async () => { identity = await makeIdentity(); });
  afterAll(async () => {
    if (originalProvider === undefined) delete process.env.LLM_PROVIDER; else process.env.LLM_PROVIDER = originalProvider;
    await Promise.all([owner?.end(), closeAllPools()]);
  });

  it("uses the allocated number exactly and retains thread.dispatch attribution after pool restart", async () => {
    const first = BigInt((await getAppPool().query("SELECT public.threads_max_t_no() AS n")).rows[0].n);
    const dispatched = await dispatch();
    expect(dispatched.threadId).toBe(`T-${first + 1n}`);
    expect((await rows()).map((row) => row.id)).toEqual([dispatched.threadId]);
    expect(await events()).toEqual([expect.objectContaining({ session_id: dispatched.threadId, payload: expect.objectContaining({
      who: expect.objectContaining({ id: identity.memberNo }),
      context: expect.objectContaining({ tenant_id: identity.tenantId, workspace_id: identity.workspaceId }),
      object: { type: "thread", id: dispatched.threadId },
      decision: expect.objectContaining({ action: "thread.dispatch", after: expect.objectContaining({ threadId: dispatched.threadId, title }) }),
    }) })]);
    await closeAllPools();
    const second = await dispatch();
    expect(BigInt(second.threadId.slice(2))).toBeGreaterThan(BigInt(dispatched.threadId.slice(2)));
  });

  it("twelve real concurrent tRPC dispatches across workspaces commit distinct IDs and matching events", async () => {
    const identities = await Promise.all(Array.from({ length: 12 }, () => makeIdentity()));
    const results = await Promise.allSettled(identities.map((who) => dispatch(who)));
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(12);
    const created = results.map((result) => (result as PromiseFulfilledResult<{ threadId: string }>).value.threadId);
    expect(new Set(created).size).toBe(12);
    for (let index = 0; index < identities.length; index++) {
      expect((await rows(identities[index]!)).map((row) => row.id)).toEqual([created[index]]);
      expect((await events(identities[index]!)).map((event) => event.session_id)).toEqual([created[index]]);
    }
  });

  it("inspection and tRPC dispatch share one source under simultaneous allocation", async () => {
    const sources = await Promise.all(Array.from({ length: 6 }, () => makeIdentity()));
    const anomalies = await Promise.all(sources.map((who) => gatewayAppend(getGatewayPool(), { ...who, actor: { id: who.memberNo, type: "human" } }, {
      who: { id: who.memberNo, type: "human" },
      context: { tenant_id: who.tenantId, workspace_id: who.workspaceId, time: new Date().toISOString() },
      object: { type: "record", id: `anomaly-${id()}` },
      decision: { action: "inspect.anomaly", after: { summary: "Mixed allocator fixture", severity: "low" } }, rule_impact: [],
    })));
    const tasks = sources.flatMap((who, index) => [
      dispatch(who),
      dispatchFromAnomaly(getAppPool(), getGatewayPool(), who, { anomalyEventId: anomalies[index]!.eventId, presetKey: "fixture", by: who.memberNo }),
    ]);
    const results = await Promise.allSettled(tasks);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(12);
    const ids = results.map((result) => (result as PromiseFulfilledResult<{ threadId: string }>).value.threadId);
    expect(new Set(ids).size).toBe(12);
    for (const who of sources) expect(await rows(who)).toHaveLength(2);
  });

  it("audit insert failure rolls back the created thread and later retry succeeds with a fresh number", async () => {
    const trigger = `mc014_dispatch_fail_${id()}`;
    await owner.query(`CREATE FUNCTION ${trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC014 dispatch audit failure'; END $$`);
    try {
      await owner.query(`CREATE TRIGGER ${trigger} BEFORE INSERT ON biz_events FOR EACH ROW WHEN (NEW.workspace_id='${identity.workspaceId}' AND NEW.payload->'decision'->>'action'='thread.dispatch') EXECUTE FUNCTION ${trigger}()`);
      await expect(dispatch()).rejects.toThrow("MC014 dispatch audit failure");
      expect(await rows()).toEqual([]);
      expect(await events()).toEqual([]);
    } finally {
      await owner.query(`DROP TRIGGER IF EXISTS ${trigger} ON biz_events`);
      await owner.query(`DROP FUNCTION ${trigger}()`);
    }
    expect((await dispatch()).threadId).toMatch(/^T-[0-9]+$/);
    expect(await rows()).toHaveLength(1);
    expect(await events()).toHaveLength(1);
  });

  it("unauthenticated, readonly, guest, denied and over-quota requests retain existing server guards", async () => {
    await expect(caller(null).threads.dispatch({ title })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [identity.memberId]);
    await expect(dispatch()).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE members SET role='owner' WHERE id=$1", [identity.memberId]);
    await expect(dispatch({ ...identity, memberNo: "GUEST" })).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE members SET permissions=$2 WHERE id=$1", [identity.memberId, JSON.stringify({ deny: ["workspace.write"] })]);
    await expect(dispatch()).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE members SET permissions='{}' WHERE id=$1", [identity.memberId]);
    await owner.query("UPDATE tenants SET plan='community' WHERE id=$1", [identity.tenantId]);
    await expect(dispatch()).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE tenants SET plan='pro' WHERE id=$1", [identity.tenantId]);
    expect(await rows()).toEqual([]);
    for (let index = 0; index < 10; index++) {
      await owner.query("INSERT INTO threads (id,tenant_id,workspace_id,title,created_by) VALUES ($1,$2,$3,'Quota fixture','fixture')", [`quota-${id()}`, identity.tenantId, identity.workspaceId]);
    }
    await expect(dispatch()).rejects.toMatchObject({ code: "TOO_MANY_REQUESTS" });
    expect(await events()).toEqual([]);
  });

  it("invalid titles and cross-workspace thread reads remain rejected or hidden", async () => {
    await expect(caller(identity).threads.dispatch({ title: "" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(caller(identity).threads.dispatch({ title: "x".repeat(501) })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    const outside = await makeIdentity();
    const created = await dispatch(outside);
    expect(await caller(identity).threads.get({ threadId: created.threadId })).toBeNull();
    expect(await caller(identity).threads.events({ threadId: created.threadId })).toEqual([]);
    expect(await rows()).toEqual([]);
  });
});
