/**
 * MC-041: real protected tRPC + PostgreSQL/RLS, synthetic read-only fixtures.
 * Never execute a task, call a model or write an event. Opt in on a migrated
 * loopback workloom_tiger_* database; the app role must not bypass RLS.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { appRouter } from "./router.js";
import { projectThreadPlan } from "./thread-plan-display.js";

const suite = process.env.RUN_TASK_PLAN_PG_TESTS === "1" ? describe : describe.skip;
const uid = () => randomUUID().replaceAll("-", "");
const label = "核对公开工作记录，并整理待确认事项";
const warning = "【确定性兜底：参数待补全】核对资料 '雪' <script>alert(1)</script>";
const plan = [
  { stepId: "s2", label, tool: "private.tool", action: "private.action", params: { token: "MC041-HIDDEN-MOCK-SECRET" }, context: { private: true } },
  { stepId: "s1", label: warning, internal: "MC041-HIDDEN-INTERNAL" },
];
const displayPlan = plan.map(({ stepId, label: text }) => ({ stepId, label: text }));
function caller(identity: Identity | null) {
  return appRouter.createCaller({ session: identity, identity, partnerIdentity: null, headers: new Headers() });
}

describe("MC-041 plan projection boundary before database constraints", () => {
  it.each([undefined, null])("keeps absent %s separate from an empty plan", (value) => {
    expect(projectThreadPlan(value)).toEqual({ plan_state: "pending", plan: null });
  });
  it.each([false, 1, "steps", {}, { steps: plan }])("rejects a non-array value %j", (value) => {
    expect(projectThreadPlan(value)).toEqual({ plan_state: "invalid", plan: null });
  });
  it("does not mutate the saved input and never copies arbitrary fields", () => {
    const saved = structuredClone(plan);
    expect(projectThreadPlan(saved)).toEqual({ plan_state: "ready", plan: displayPlan });
    expect(saved).toEqual(plan);
    expect(projectThreadPlan([])).toEqual({ plan_state: "empty", plan: [] });
  });
  it("retains legitimate nonempty single-character labels", () => {
    const steps = [{ stepId: "s1", label: "读" }, { stepId: "s2", label: "写" }];
    expect(projectThreadPlan(steps)).toEqual({ plan_state: "ready", plan: steps });
  });
});

suite("MC-041 display-only task plan contract", () => {
  let owner: pg.Pool;
  const identities: Identity[] = [];
  let primary: Identity;
  async function fixture(tenantId = `tenant-mc041-${uid()}`): Promise<Identity> {
    const workspaceId = `ws-mc041-${uid()}`, memberId = `member-mc041-${uid()}`;
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'MC041 fixture','pro') ON CONFLICT (id) DO NOTHING", [tenantId]);
    await owner.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry,stage) VALUES ($1,$2,'MC041 fixture',$1,'test','stable')", [workspaceId, tenantId]);
    await owner.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-MC041','MC041 fixture','owner')", [memberId, workspaceId]);
    const identity: Identity = { memberId, memberNo: "MEM-MC041", name: "MC041 fixture", role: "owner", tenantId, workspaceId, plan: "pro" };
    identities.push(identity);
    return identity;
  }
  async function thread(value: unknown, who = primary, tenantId = who.tenantId): Promise<string> {
    const id = `mc041-${uid()}`;
    await owner.query(
      `INSERT INTO threads (id,tenant_id,workspace_id,title,mode,status,created_by,plan,plan_version)
       VALUES ($1,$2,$3,'MC041 只读计划展示','quest','running',$4,$5,3)`,
      [id, tenantId, who.workspaceId, who.memberNo, value === null ? null : JSON.stringify(value)],
    );
    return id;
  }
  async function read(id: string, who: Identity | null = primary) {
    return caller(who).threads.get({ threadId: id });
  }
  beforeAll(async () => {
    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)
      || url.hostname !== urls[0]!.hostname || url.port !== urls[0]!.port || url.pathname !== urls[0]!.pathname)) {
      throw new Error("MC041 requires three roles on the same loopback workloom_tiger_ test database");
    }
    owner = new pg.Pool({ connectionString: urls[0]!.toString(), connectionTimeoutMillis: 5000, statement_timeout: 10000 });
    expect((await getAppPool().query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
      .toEqual({ current_user: "workloom_app", rolsuper: false, rolbypassrls: false });
    primary = await fixture();
  });
  afterAll(async () => {
    try {
      for (const who of identities) {
        await owner.query("DELETE FROM threads WHERE workspace_id=$1", [who.workspaceId]);
        await owner.query("DELETE FROM members WHERE workspace_id=$1", [who.workspaceId]);
        await owner.query("DELETE FROM workspaces WHERE id=$1", [who.workspaceId]);
      }
      for (const tenantId of new Set(identities.map((who) => who.tenantId))) await owner.query("DELETE FROM tenants WHERE id=$1", [tenantId]);
    } finally { await Promise.all([owner?.end(), closeAllPools()]); }
  });

  it("returns only ordered step IDs and complete labels, preserving warnings", async () => {
    const result = await read(await thread(plan));
    expect(result).toMatchObject({ plan: displayPlan, plan_state: "ready", plan_version: 3 });
    expect(JSON.stringify(result)).not.toMatch(/MC041-HIDDEN|private\.tool|private\.action|"params"|"context"|"internal"/);
    expect(Object.keys(result!.plan[0]).sort()).toEqual(["label", "stepId"]);
  });
  it.each([
    ["missing", null, "pending", null],
    ["empty", [], "empty", []],
    ["null item", [null], "invalid", null],
    ["missing label", [{ stepId: "s1" }], "invalid", null],
    ["blank label", [{ stepId: "s1", label: " \n " }], "invalid", null],
    ["missing ID", [{ label }], "invalid", null],
    ["numeric ID", [{ stepId: 1, label }], "invalid", null],
    ["blank ID", [{ stepId: " ", label }], "invalid", null],
    ["duplicate IDs", [{ stepId: "s1", label }, { stepId: "s1", label: warning }], "invalid", null],
    ["partly malformed", [plan[0], { stepId: "s1" }], "invalid", null],
  ])("distinguishes %s without inventing or silently dropping steps", async (_name, value, state, expected) => {
    expect(await read(await thread(value))).toMatchObject({ plan: expected, plan_state: state, plan_version: 3 });
  });
  it("preserves long/special text and all steps rather than truncating the contract", async () => {
    const steps = Array.from({ length: 100 }, (_, index) => ({ stepId: `s'${index}-雪`, label: `${index} ${warning}\n${"资料".repeat(1000)}` }));
    expect(await read(await thread(steps))).toMatchObject({ plan: steps, plan_state: "ready" });
  });
  it("permits a current readonly member and refreshes stale role claims", async () => {
    const id = await thread(plan);
    await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [primary.memberId]);
    try { expect(await read(id)).toMatchObject({ plan: displayPlan }); }
    finally { await owner.query("UPDATE members SET role='owner' WHERE id=$1", [primary.memberId]); }
  });
  it("rejects unauthenticated, removed and mismatched identities", async () => {
    const id = await thread(plan);
    await expect(read(id, null)).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await expect(read(id, { ...primary, tenantId: "nonexistent" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await owner.query("UPDATE members SET status='removed' WHERE id=$1", [primary.memberId]);
    try { await expect(read(id)).rejects.toMatchObject({ code: "UNAUTHORIZED" }); }
    finally { await owner.query("UPDATE members SET status='active' WHERE id=$1", [primary.memberId]); }
  });
  it("hides missing, foreign workspace, foreign tenant and inconsistent tenant rows", async () => {
    const sibling = await fixture(primary.tenantId), foreign = await fixture();
    expect(await read("mc041-missing")).toBeNull();
    expect(await read(await thread(plan, sibling))).toBeNull();
    expect(await read(await thread(plan, foreign))).toBeNull();
    expect(await read(await thread(plan, primary, foreign.tenantId))).toBeNull();
  });
  it("remains read-only under concurrent reads", async () => {
    const id = await thread(plan);
    const snapshot = async () => ({
      thread: (await owner.query("SELECT * FROM threads WHERE id=$1", [id])).rows,
      events: (await owner.query("SELECT event_id,payload FROM biz_events WHERE session_id=$1", [id])).rows,
    });
    const before = await snapshot();
    const results = await Promise.all(Array.from({ length: 12 }, () => read(id)));
    expect(results.every((result) => JSON.stringify(result?.plan) === JSON.stringify(displayPlan))).toBe(true);
    expect(await snapshot()).toEqual(before);
  });
  it("propagates database read failure, then releases the connection for recovery", async () => {
    const id = await thread(plan);
    await owner.query("REVOKE SELECT ON threads FROM workloom_app");
    try { await expect(read(id)).rejects.toMatchObject({ code: "INTERNAL_SERVER_ERROR" }); }
    finally { await owner.query("GRANT SELECT ON threads TO workloom_app"); }
    expect(await read(id)).toMatchObject({ plan: displayPlan });
  });
});
