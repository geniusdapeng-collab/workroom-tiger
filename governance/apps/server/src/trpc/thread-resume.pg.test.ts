/**
 * MC-028: exercise the actual tRPC routes, RLS, Quest replay and audit transactions.
 * Opt in only against a migrated loopback workloom_tiger_* test database. Fixtures
 * are isolated; no paid model, external connector or business approval is used.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { appRouter } from "./router.js";

const suite = process.env.RUN_THREAD_RESUME_PG_TESTS === "1" ? describe : describe.skip;
const uid = () => randomUUID().replaceAll("-", "");
const originalGoal = "生成 MC028 metrics 任务";
const continuation = "继续按原任务目标推进";
type Agent = { id: string; key: string };
type Fixture = { identity: Identity; agent: Agent };
type ThreadInput = {
  ref?: string | null;
  title?: string;
  mode?: "quest" | "agent" | "ask";
  tenantId?: string;
};

function caller(identity: Identity | null) {
  return appRouter.createCaller({ session: identity, identity, partnerIdentity: null, headers: new Headers() });
}

suite("MC-028 persisted thread assignment and safe continuation", () => {
  let owner: pg.Pool;
  let fixture: Fixture;
  const savedProvider = process.env.LLM_PROVIDER;
  const savedWebSearch = process.env.ASK_WEB_SEARCH;

  async function addAgent(identity: Identity, key = "mc028-original", access: "read" | "write" = "write"): Promise<Agent> {
    const agent = { id: `agent-mc028-${uid()}`, key };
    await owner.query(
      `INSERT INTO agents (id,workspace_id,preset_key,name,version,kind,fence_bindings,meta)
       VALUES ($1,$2,$3,'MC028 fixture','v1','test',$4,$5)`,
      [agent.id, identity.workspaceId, key, JSON.stringify(["R-mc028-review"]), JSON.stringify({ tools: [
        { name: "metrics.read", access: "read", desc: "Read fixture metrics" },
        ...(access === "write" ? [{ name: "record.write", access: "write", desc: "Review fixture record" }] : []),
      ] })],
    );
    return agent;
  }

  async function makeFixture(tenantId = `tenant-mc028-${uid()}`): Promise<Fixture> {
    const workspaceId = `ws-mc028-${uid()}`, memberId = `member-mc028-${uid()}`;
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'MC028 fixture','pro') ON CONFLICT (id) DO NOTHING", [tenantId]);
    await owner.query(
      "INSERT INTO workspaces (id,tenant_id,name,slug,industry,stage) VALUES ($1,$2,'MC028 fixture',$1,'test','stable')",
      [workspaceId, tenantId],
    );
    await owner.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-MC028','MC028 fixture','owner')", [memberId, workspaceId]);
    await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test',$3)",
      [workspaceId, tenantId, JSON.stringify({ dataMode: "simulated" })]);
    await owner.query(
      `INSERT INTO fence_rules (id,rule_id,version,workspace_id,name,level,match_spec,action,status,created_by)
       VALUES ($1,'R-mc028-review','v1',$2,'MC028 manual review','review',$3,$4,'active','fixture')`,
      [`rule-mc028-${uid()}`, workspaceId, JSON.stringify({ object_types: ["record"], actions: ["record.write"], when: "true" }),
        JSON.stringify({ result: "review" })],
    );
    const identity: Identity = { memberId, memberNo: "MEM-MC028", name: "MC028 fixture", role: "owner", tenantId, workspaceId, plan: "pro" };
    return { identity, agent: await addAgent(identity) };
  }

  async function newThread(options: ThreadInput = {}, source = fixture): Promise<string> {
    const threadId = `mc028-${uid()}`;
    const ref = options.ref === undefined ? source.agent.id : options.ref;
    await owner.query(
      `INSERT INTO threads (id,tenant_id,workspace_id,title,mode,status,created_by,agent_id)
       VALUES ($1,$2,$3,$4,$5,'queued',$6,$7)`,
      [threadId, options.tenantId ?? source.identity.tenantId, source.identity.workspaceId,
        options.title ?? originalGoal, options.mode ?? "quest", source.identity.memberNo, ref],
    );
    return threadId;
  }

  async function state(threadId: string) {
    return (await owner.query("SELECT id,title,mode,status,agent_id,plan,plan_version,progress_done,progress_total FROM threads WHERE id=$1", [threadId])).rows[0];
  }
  async function events(threadId: string) {
    return (await owner.query("SELECT event_id,payload FROM biz_events WHERE session_id=$1 ORDER BY seq", [threadId])).rows;
  }
  async function approvals(threadId: string) {
    return (await owner.query(
      `SELECT a.approval_id,a.status,a.snapshot,a.consumed_at FROM approvals a
       JOIN biz_events e ON a.event_id=e.event_id WHERE e.session_id=$1 ORDER BY a.approval_id`, [threadId],
    )).rows;
  }
  async function run(threadId: string, presetKey?: string, goal = continuation, who = fixture.identity) {
    return caller(who).threads.run({ threadId, goal, ...(presetKey === undefined ? {} : { presetKey }) });
  }
  async function rejectedWithoutMutation(threadId: string, code: string, presetKey?: string) {
    const before = await state(threadId);
    const beforeEvents = await events(threadId);
    await expect(run(threadId, presetKey)).rejects.toMatchObject({ code });
    expect(await state(threadId)).toEqual(before);
    expect(await events(threadId)).toEqual(beforeEvents);
  }

  beforeAll(async () => {
    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)
      || url.hostname !== urls[0]!.hostname || url.port !== urls[0]!.port || url.pathname !== urls[0]!.pathname)) {
      throw new Error("MC028 tests require three roles on the same loopback workloom_tiger_ test database");
    }
    process.env.LLM_PROVIDER = "mock";
    process.env.ASK_WEB_SEARCH = "0";
    owner = new pg.Pool({ connectionString: urls[0]!.toString(), connectionTimeoutMillis: 5000, statement_timeout: 10000 });
    expect((await getAppPool().query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
      .toEqual({ current_user: "workloom_app", rolsuper: false, rolbypassrls: false });
  });
  beforeEach(async () => { fixture = await makeFixture(); });
  afterAll(async () => {
    if (savedProvider === undefined) delete process.env.LLM_PROVIDER; else process.env.LLM_PROVIDER = savedProvider;
    if (savedWebSearch === undefined) delete process.env.ASK_WEB_SEARCH; else process.env.ASK_WEB_SEARCH = savedWebSearch;
    await Promise.all([owner?.end(), closeAllPools()]);
  });

  it.each(["omitted", "agent-id", "preset-key"] as const)("resumes using the original ready assignment when the client reference is %s", async (reference) => {
    const threadId = await newThread();
    const result = await run(threadId, reference === "omitted" ? undefined : reference === "agent-id" ? fixture.agent.id : fixture.agent.key);
    expect(result).toMatchObject({ status: "pending_review", stepsDone: 1, stepsTotal: 2 });
    expect(await state(threadId)).toMatchObject({ agent_id: fixture.agent.id, title: originalGoal, status: "pending_review", progress_done: 1 });
    expect((await events(threadId)).map((event) => event.payload.who.id)).toEqual([fixture.agent.key, fixture.agent.key]);
    expect(await approvals(threadId)).toEqual([expect.objectContaining({ status: "pending", consumed_at: null })]);
  });

  it("accepts an unambiguous legacy stored preset key and normalizes only to its own installed agent", async () => {
    const threadId = await newThread({ ref: fixture.agent.key });
    expect(await run(threadId, fixture.agent.id)).toMatchObject({ status: "pending_review" });
    expect(await state(threadId)).toMatchObject({ agent_id: fixture.agent.id });
  });

  it("rejects an explicit different ready assignment without running either agent", async () => {
    const other = await addAgent(fixture.identity, "mc028-other");
    const threadId = await newThread();
    await rejectedWithoutMutation(threadId, "CONFLICT", other.key);
    await rejectedWithoutMutation(threadId, "CONFLICT", other.id);
    await rejectedWithoutMutation(threadId, "CONFLICT", "missing-explicit-agent");
  });

  it.each([null, "", "missing-original-agent"])("fails closed on missing original assignment %s even if the client proposes a valid role", async (ref) => {
    const threadId = await newThread({ ref });
    await rejectedWithoutMutation(threadId, "PRECONDITION_FAILED");
    await rejectedWithoutMutation(threadId, "PRECONDITION_FAILED", fixture.agent.key);
  });

  it.each(["disabled", "invalid"])("rejects original agent status %s without falling back", async (status) => {
    await addAgent(fixture.identity, "pricing-agent");
    await owner.query("UPDATE agents SET status=$2 WHERE id=$1", [fixture.agent.id, status]);
    await rejectedWithoutMutation(await newThread(), "PRECONDITION_FAILED");
  });

  it.each(["another-tenant", "same-tenant"] as const)("does not resolve an agent ID from %s even when a local role has the same key", async (scope) => {
    const outside = await makeFixture(scope === "same-tenant" ? fixture.identity.tenantId : undefined);
    await rejectedWithoutMutation(await newThread({ ref: outside.agent.id }), "PRECONDITION_FAILED", fixture.agent.key);
  });

  it("rejects ambiguous id/key references and duplicate preset keys", async () => {
    await addAgent(fixture.identity, fixture.agent.id);
    await rejectedWithoutMutation(await newThread(), "PRECONDITION_FAILED");
    const clean = await makeFixture();
    fixture = clean;
    await addAgent(fixture.identity, fixture.agent.key);
    await rejectedWithoutMutation(await newThread(), "PRECONDITION_FAILED");
  });

  it("hides missing, foreign-workspace and mismatched-tenant threads before runtime access", async () => {
    await expect(run("missing-thread", fixture.agent.key)).rejects.toMatchObject({ code: "NOT_FOUND" });
    const outside = await makeFixture();
    await rejectedWithoutMutation(await newThread({}, outside), "NOT_FOUND", fixture.agent.key);
    await rejectedWithoutMutation(await newThread({ tenantId: outside.identity.tenantId }), "NOT_FOUND", fixture.agent.key);
  });

  it("retains authentication, current readonly/guest/deny checks and the plan capability guard", async () => {
    const threadId = await newThread();
    await expect(caller(null).threads.run({ threadId, goal: continuation })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [fixture.identity.memberId]);
    await rejectedWithoutMutation(threadId, "FORBIDDEN");
    await owner.query("UPDATE members SET role='owner' WHERE id=$1", [fixture.identity.memberId]);
    await expect(run(threadId, undefined, continuation, { ...fixture.identity, memberNo: "GUEST" })).rejects.toMatchObject({ code: "FORBIDDEN" });
    await owner.query("UPDATE members SET permissions=$2 WHERE id=$1", [fixture.identity.memberId, JSON.stringify({ deny: ["workspace.write"] })]);
    await rejectedWithoutMutation(threadId, "FORBIDDEN");
    await owner.query("UPDATE members SET permissions='{}' WHERE id=$1", [fixture.identity.memberId]);
    await owner.query("UPDATE tenants SET plan='community' WHERE id=$1", [fixture.identity.tenantId]);
    await rejectedWithoutMutation(threadId, "FORBIDDEN");
  });

  it("uses the persisted task objective rather than a short continuation for Quest relevance and Ask evidence", async () => {
    // Completed read-only tasks expose the runtime relevance result. Pending
    // review intentionally has a narrower response and is covered separately.
    fixture.agent = await addAgent(fixture.identity, "mc028-goal-read", "read");
    const quest = await newThread();
    expect(await run(quest, fixture.agent.key)).toMatchObject({ planRelevance: { relevant: true } });
    const question = "当前有多少待审批事项？";
    const ask = await newThread({ mode: "ask", title: question });
    expect(await run(ask, fixture.agent.key)).toMatchObject({ status: "completed" });
    const answer = (await events(ask)).find((event) => event.payload.decision.action === "ask.answer");
    expect(answer?.payload.decision.params.question).toBe(question);
    expect(answer?.payload.who.id).toBe(fixture.agent.key);
    expect(await state(ask)).toMatchObject({ title: question, mode: "ask", agent_id: fixture.agent.id });
  });

  it("does not invent an objective when the stored title is blank", async () => {
    await rejectedWithoutMutation(await newThread({ title: "  " }), "PRECONDITION_FAILED", fixture.agent.key);
  });

  it("rejects empty identifiers and safely resolves exact references containing special characters", async () => {
    const threadId = await newThread();
    await expect(run("")).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(run(threadId, "")).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(run(threadId, "   ")).rejects.toMatchObject({ code: "BAD_REQUEST" });
    fixture.agent = await addAgent(fixture.identity, "mc028-'quoted\";--雪", "read");
    const special = await newThread();
    expect(await run(special, fixture.agent.key)).toMatchObject({ status: "completed", stepsDone: 1 });
    expect(await state(special)).toMatchObject({ agent_id: fixture.agent.id });
    expect(await caller(fixture.identity).threads.run({ threadId: special })).toMatchObject({ status: "completed", planReused: true });
  });

  it("reuses the pending approval, original plan and verified read receipt during repeated and concurrent continuation", async () => {
    const threadId = await newThread();
    const first = await run(threadId, fixture.agent.key);
    const savedState = await state(threadId), savedEvents = await events(threadId), savedApprovals = await approvals(threadId);
    const repeated = await Promise.all(Array.from({ length: 5 }, () => run(threadId)));
    for (const result of repeated) expect(result).toMatchObject({
      status: "pending_review", stepsDone: 1, stepsTotal: 2, planReused: true,
      pendingApprovalId: (first as { pendingApprovalId?: string }).pendingApprovalId,
    });
    expect(await state(threadId)).toEqual(savedState);
    expect(await events(threadId)).toEqual(savedEvents);
    expect(await approvals(threadId)).toEqual(savedApprovals);
  });

  it("retains agent-mode review before even the read step", async () => {
    const threadId = await newThread({ mode: "agent" });
    expect(await run(threadId)).toMatchObject({ status: "pending_review", stepsDone: 0 });
    expect((await events(threadId)).every((event) => event.payload.decision.kind === "review")).toBe(true);
  });

  it("replays a completed task without executing the verified step again", async () => {
    fixture.agent = await addAgent(fixture.identity, "mc028-readonly", "read");
    const threadId = await newThread();
    expect(await run(threadId, fixture.agent.key)).toMatchObject({ status: "completed", stepsDone: 1 });
    const saved = await events(threadId);
    expect(await run(threadId)).toMatchObject({ status: "completed", stepsDone: 1, planReused: true });
    expect(await events(threadId)).toEqual(saved);
  });

  it("keeps an unverified failed task failed without repeating its uncertain operation", async () => {
    await owner.query("UPDATE profiles SET archive=$2 WHERE workspace_id=$1", [fixture.identity.workspaceId, JSON.stringify({ dataMode: "real" })]);
    const threadId = await newThread();
    expect(await run(threadId, fixture.agent.key)).toMatchObject({ status: "failed", unverified: ["s1"] });
    const saved = await events(threadId);
    expect(await run(threadId)).toMatchObject({ status: "failed", unverified: ["s1"], planReused: true });
    expect(await events(threadId)).toEqual(saved);
    expect(await approvals(threadId)).toEqual([]);
  });

  it.each(["explicit-key", "explicit-id", "automatic"] as const)("persists the installed assignment atomically when dispatch selection is %s", async (reference) => {
    const input = { title: originalGoal, runImmediately: false,
      ...(reference === "automatic" ? {} : { presetKey: reference === "explicit-id" ? fixture.agent.id : fixture.agent.key }) };
    const result = await caller(fixture.identity).threads.dispatch(input);
    expect(result.kind).toBe("routed");
    if (result.kind !== "routed") throw new Error("Unexpected clarification");
    expect(result).toMatchObject({ status: "queued", presetKey: fixture.agent.key });
    expect(await state(result.threadId)).toMatchObject({ agent_id: fixture.agent.id, title: originalGoal, plan: null });
    expect(await events(result.threadId)).toEqual([expect.objectContaining({ payload: expect.objectContaining({
      decision: expect.objectContaining({ action: "thread.dispatch", after: expect.objectContaining({
        agentId: fixture.agent.id, presetKey: fixture.agent.key,
      }) }),
    }) })]);
    expect(await run(result.threadId)).toMatchObject({ status: "pending_review", stepsDone: 1 });
  });

  it("rejects new dispatch with no ready installed role or an invalid explicit reference without creating threads", async () => {
    await expect(caller(fixture.identity).threads.dispatch({ title: originalGoal, presetKey: "not-installed" }))
      .rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    await owner.query("UPDATE agents SET status='disabled' WHERE workspace_id=$1", [fixture.identity.workspaceId]);
    await expect(caller(fixture.identity).threads.dispatch({ title: originalGoal })).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    expect((await owner.query("SELECT id FROM threads WHERE workspace_id=$1", [fixture.identity.workspaceId])).rows).toEqual([]);
    expect((await owner.query("SELECT event_id FROM biz_events WHERE workspace_id=$1", [fixture.identity.workspaceId])).rows).toEqual([]);
  });

  it("preserves the 500-character dispatch boundary, agent queue mode and immediate Quest execution", async () => {
    const title = "生成 metrics 任务 '\";--雪".padEnd(500, "测");
    const queued = await caller(fixture.identity).threads.dispatch({ title, presetKey: fixture.agent.key });
    expect(queued).toMatchObject({ kind: "routed", status: "queued" });
    if (queued.kind !== "routed") throw new Error("Unexpected clarification");
    expect(await state(queued.threadId)).toMatchObject({ title, agent_id: fixture.agent.id });
    await expect(caller(fixture.identity).threads.dispatch({ title: `${title}测`, presetKey: fixture.agent.key }))
      .rejects.toMatchObject({ code: "BAD_REQUEST" });
    const agent = await caller(fixture.identity).threads.dispatch({ title: "每一步都确认后再处理 metrics", presetKey: fixture.agent.key, runImmediately: true });
    expect(agent).toMatchObject({ mode: "agent", status: "queued", presetKey: fixture.agent.key });
    const immediate = await caller(fixture.identity).threads.dispatch({ title: originalGoal, presetKey: fixture.agent.key, runImmediately: true });
    expect(immediate).toMatchObject({ mode: "quest", status: "pending_review", stepsDone: 1, presetKey: fixture.agent.key });
  });

  it("binds Ask dispatch to the installed role and records the original question", async () => {
    const question = "当前有多少待审批事项？";
    const result = await caller(fixture.identity).threads.dispatch({ title: question, presetKey: fixture.agent.id });
    expect(result).toMatchObject({ kind: "routed", mode: "ask", status: "completed", presetKey: fixture.agent.key });
    if (result.kind !== "routed") throw new Error("Unexpected clarification");
    expect(await state(result.threadId)).toMatchObject({ title: question, mode: "ask", agent_id: fixture.agent.id });
    const answer = (await events(result.threadId)).find((event) => event.payload.decision.action === "ask.answer");
    expect(answer?.payload.who.id).toBe(fixture.agent.key);
    expect(answer?.payload.decision.params.question).toBe(question);
  });

  it("surfaces deferred COMMIT failure, rolls back the assignment and audit event, then recovers", async () => {
    const trigger = `mc028_commit_fail_${uid()}`;
    await owner.query(`CREATE FUNCTION ${trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC028 deferred commit failure'; END $$`);
    try {
      await owner.query(`CREATE CONSTRAINT TRIGGER ${trigger} AFTER INSERT ON threads DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.workspace_id='${fixture.identity.workspaceId}') EXECUTE FUNCTION ${trigger}()`);
      await expect(caller(fixture.identity).threads.dispatch({ title: originalGoal, presetKey: fixture.agent.key, runImmediately: true }))
        .rejects.toThrow("MC028 deferred commit failure");
      expect((await owner.query("SELECT id FROM threads WHERE workspace_id=$1", [fixture.identity.workspaceId])).rows).toEqual([]);
      expect((await owner.query("SELECT event_id FROM biz_events WHERE workspace_id=$1", [fixture.identity.workspaceId])).rows).toEqual([]);
    } finally {
      await owner.query(`DROP TRIGGER IF EXISTS ${trigger} ON threads`);
      await owner.query(`DROP FUNCTION ${trigger}()`);
    }
    const retry = await caller(fixture.identity).threads.dispatch({ title: originalGoal, presetKey: fixture.agent.key });
    expect(retry).toMatchObject({ kind: "routed", status: "queued", presetKey: fixture.agent.key });
  });

  it("propagates database lookup failure without runtime writes and releases the connection for retry", async () => {
    const threadId = await newThread();
    await owner.query("REVOKE SELECT ON threads FROM workloom_app");
    try { await rejectedWithoutMutation(threadId, "INTERNAL_SERVER_ERROR", fixture.agent.key); }
    finally { await owner.query("GRANT SELECT ON threads TO workloom_app"); }
    expect(await run(threadId)).toMatchObject({ status: "pending_review" });
  });
});
