/**
 * MC-013: real JWT -> member facts -> verified active Bundle -> devtools guard.
 * RUN_DEVTOOLS_PERMISSION_PG_TESTS=1 enables the migrated local PostgreSQL test.
 * Business-handler sentinels prevent tool processes, filesystem writes and releases
 * in the exhaustive routing matrix; authorization and PostgreSQL are never mocked.
 * Separate create/read tests use the real service and its atomic event transaction.
 */
import { createHash, randomUUID } from "node:crypto";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { closeAllPools, getAppPool, getOwnerPool } from "@workloom/db";
import { signDemoToken, type Identity } from "@workloom/base/tenancy";
// Use the repository's built contract fixture utilities without adding a server dependency.
import { canonicalBundleArtifactPayload, parseBundleManifest } from "../../../../packages/industry-contract/dist/index.js";
import { createContext } from "../trpc/context.js";
import { resolveWorkspaceActiveBundle } from "./active-bundle.js";
import { resolveAuthoritativeClientAccess } from "./access-authority.js";
import * as devtools from "./devtools.js";
import { serviceRouter } from "./router.js";

type Caller = ReturnType<typeof serviceRouter.createCaller>;
interface Scope {
  tenantId: string;
  workspaceId: string;
  repoId: string;
  identity: Identity;
}
interface RouteCase {
  name: string;
  handler: keyof typeof devtools;
  write: boolean;
  call: (caller: Caller, scope: Scope) => Promise<unknown>;
}
const permission = "ai-pm.development.read";
const taskInput = (scope: Scope) => ({
  repoId: scope.repoId, title: "权限回归 '报价' <test>", prdSummary: "仅本地权限回归，不派发工具。",
  acceptance: ["任务及其事件一并落库"], changeKind: "fix" as const,
});
const routes: RouteCase[] = [
  { name: "tools", handler: "listTools", write: false, call: (c) => c.devtools.tools() },
  { name: "refreshTools", handler: "refreshTools", write: true, call: (c) => c.devtools.refreshTools() },
  { name: "addCustomTool", handler: "saveCustomTool", write: true, call: (c) => c.devtools.addCustomTool({
    tool_key: "mc013-test", display_name: "权限测试", bin: "nonexistent-mc013-tool", args: ["{prompt}"], output: { protocol: "text" },
  }) },
  { name: "repos", handler: "listRepos", write: false, call: (c) => c.devtools.repos() },
  { name: "registerRepo", handler: "registerRepo", write: true, call: (c) => c.devtools.registerRepo({ name: "权限测试", path: "/nonexistent/mc013" }) },
  { name: "setRepoStatus", handler: "setRepoStatus", write: true, call: (c, s) => c.devtools.setRepoStatus({ repoId: s.repoId, status: "disabled" }) },
  { name: "tasks", handler: "listTasks", write: false, call: (c) => c.devtools.tasks() },
  { name: "createTask", handler: "createTask", write: true, call: (c, s) => c.devtools.createTask(taskInput(s)) },
  { name: "confirmTask", handler: "confirmTask", write: true, call: (c) => c.devtools.confirmTask({ taskId: "mc013-task-sentinel" }) },
  { name: "dispatchTask", handler: "dispatchTask", write: true, call: (c) => c.devtools.dispatchTask({ taskId: "mc013-task-sentinel" }) },
  { name: "rejectTask", handler: "rejectTask", write: true, call: (c) => c.devtools.rejectTask({ taskId: "mc013-task-sentinel", note: "权限测试" }) },
  { name: "cancelTask", handler: "cancelTask", write: true, call: (c) => c.devtools.cancelTask({ taskId: "mc013-task-sentinel" }) },
  { name: "approveRelease", handler: "approveRelease", write: true, call: (c) => c.devtools.approveRelease({ taskId: "mc013-task-sentinel" }) },
  { name: "taskDetail", handler: "taskDetail", write: false, call: (c) => c.devtools.taskDetail({ taskId: "mc013-task-sentinel" }) },
  { name: "sessionEvents", handler: "sessionEvents", write: false, call: (c) => c.devtools.sessionEvents({ sessionId: "mc013-session-sentinel" }) },
  { name: "releases", handler: "listReleases", write: false, call: (c) => c.devtools.releases() },
];
const writeRoutes = routes.filter((route) => route.write);
const readRoutes = routes.filter((route) => !route.write);

function interceptBusinessHandlers() {
  return new Map(routes.map((route) => [route.handler,
    vi.spyOn(devtools, route.handler).mockImplementation(() => { throw new Error(`business-handler:${route.handler}`); }),
  ]));
}

async function callerFor(identity: Identity | null, token?: string): Promise<Caller> {
  const signed = token ?? (identity ? await signDemoToken(identity) : null);
  return serviceRouter.createCaller(await createContext(new Request("http://127.0.0.1/mc013", {
    headers: signed ? { authorization: `Bearer ${signed}` } : undefined,
  })));
}

function writeAiPmBundle(root: string) {
  const raw = {
    schemaVersion: "workloom.bundle/v1", name: "@workloom/ai-pm", version: "1.0.0",
    workloom: {
      industry: "ai-pm", displayName: "权限测试候选包", description: "仅验证已验行业权限，无业务实现",
      status: "candidate", owner: "WorkLoom",
      compatibility: { base: ">=0.1.0 <1.0.0", ui: ">=0.1.0 <1.0.0", contract: "2.0.0" },
      provides: { presets: [], fences: [], skills: [], schemas: [], ui: [], serviceFront: [], seeds: [] },
      ui: {
        schemaVersion: "workloom.bundle.ui/v1", terminology: {},
        navigation: { slots: [{ capabilityId: "ai-pm.development", title: "开发", route: "/development",
          group: "operations", icon: "report", clients: ["pc"], permissions: [permission] }] },
        home: { widgets: [] }, objects: [], workflows: [],
        serviceFront: { enabled: false, identityPolicy: "disabled", tabs: [], services: [] },
        theme: { brand: {} }, permissions: [permission], experiments: [],
      },
    },
  };
  const normalized = parseBundleManifest(raw);
  const digest = createHash("sha256").update(canonicalBundleArtifactPayload(normalized, {})).digest("hex");
  mkdirSync(join(root, "ai-pm"));
  writeFileSync(join(root, "ai-pm/bundle.json"), JSON.stringify({ ...raw, integrity: { algorithm: "sha256", digest, assets: {} } }));
}

describe.skipIf(process.env.RUN_DEVTOOLS_PERMISSION_PG_TESTS !== "1")("devtools authority (real PostgreSQL)", () => {
  const tag = `mc013-${randomUUID()}`;
  const scopes: Scope[] = [];
  let bundleRoot: string | undefined;
  const originalBundleRoot = process.env.BUNDLES_ROOT;
  let tiger: Scope;
  let ai: Scope;
  let aiOther: Scope;
  let missing: Scope;
  let mismatch: Scope;
  const identities = new Map<string, Identity>();
  const callers = new Map<string, Caller>();

  async function seedScope(label: string, bundleId: string, installBundleId: string | null = bundleId): Promise<Scope> {
    const tenantId = `${tag}-${label}-tenant`;
    const workspaceId = `${tag}-${label}-workspace`;
    const memberId = `${tag}-${label}-owner`;
    const repoId = `${tag}-${label}-repo`;
    const pool = getOwnerPool();
    await pool.query("INSERT INTO tenants (id,name,plan) VALUES ($1,$2,'pro')", [tenantId, label]);
    await pool.query("INSERT INTO workspaces (id,tenant_id,name,slug,industry,bundle_id) VALUES ($1,$2,$3,$1,$4,$4)", [workspaceId, tenantId, label, bundleId]);
    await pool.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-MC013','本地回归','owner')", [memberId, workspaceId]);
    if (installBundleId) await pool.query("INSERT INTO bundle_installs (id,workspace_id,bundle_id) VALUES ($1,$2,$3)", [`${tag}-${label}-install`, workspaceId, installBundleId]);
    await pool.query("INSERT INTO dev_repos (id,workspace_id,name,path,registered_by) VALUES ($1,$2,$3,$4,'MEM-MC013')", [repoId, workspaceId, label, `/nonexistent/${tag}/${label}`]);
    const scope: Scope = { tenantId, workspaceId, repoId, identity: {
      kind: "member", memberId, memberNo: "MEM-MC013", name: "本地回归", role: "owner", tenantId, workspaceId, plan: "pro",
    } };
    scopes.push(scope);
    return scope;
  }

  async function seedMember(scope: Scope, label: string, role: Identity["role"], permissions: Record<string, unknown>): Promise<Identity> {
    const identity: Identity = { ...scope.identity, memberId: `${tag}-${label}`, memberNo: `MEM-${label}`, role };
    await getOwnerPool().query("INSERT INTO members (id,workspace_id,member_no,name,role,permissions) VALUES ($1,$2,$3,'权限回归',$4,$5)", [
      identity.memberId, identity.workspaceId, identity.memberNo, role, JSON.stringify(permissions),
    ]);
    return identity;
  }

  async function snapshot() {
    const tables = ["dev_tool_installs", "dev_repos", "dev_tasks", "dev_sessions", "dev_events", "dev_changesets", "releases", "dev_fences_audit", "biz_events"] as const;
    const records = await Promise.all(tables.map(async (table) => {
      const result = await getOwnerPool().query<{ count: number; digest: string }>(
        `SELECT count(*)::int AS count, md5(COALESCE(string_agg(to_jsonb(t)::text, '' ORDER BY to_jsonb(t)::text), '')) AS digest
         FROM ${table} t WHERE workspace_id=ANY($1::text[])`, [scopes.map((scope) => scope.workspaceId)],
      );
      return [table, result.rows[0]];
    }));
    return Object.fromEntries(records);
  }

  beforeAll(async () => {
    for (const key of ["DATABASE_URL", "DATABASE_APP_URL"]) {
      const url = new URL(process.env[key] ?? "");
      if (!["127.0.0.1", "localhost", "::1"].includes(url.hostname)) throw new Error("MC-013 requires a local PostgreSQL test database");
    }
    const role = await getAppPool().query<{ rolname: string; rolsuper: boolean; rolbypassrls: boolean }>(
      "SELECT rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user",
    );
    expect(role.rows[0]).toMatchObject({ rolname: "workloom_app", rolsuper: false, rolbypassrls: false });
    bundleRoot = mkdtempSync(join(tmpdir(), "workloom-mc013-"));
    cpSync(fileURLToPath(new URL("../../../../bundles/trading", import.meta.url)), join(bundleRoot, "trading"), { recursive: true });
    writeAiPmBundle(bundleRoot);
    process.env.BUNDLES_ROOT = bundleRoot;
    tiger = await seedScope("tiger", "trading");
    ai = await seedScope("ai", "ai-pm");
    aiOther = await seedScope("other", "ai-pm");
    missing = await seedScope("missing", "ai-pm", null);
    mismatch = await seedScope("mismatch", "trading", "ai-pm");
    identities.set("Tiger owner", tiger.identity);
    identities.set("Tiger spoofed allow", await seedMember(tiger, "tiger-allow", "owner", { allow: [permission] }));
    identities.set("AI-PM owner", ai.identity);
    identities.set("AI-PM manager", await seedMember(ai, "manager", "manager", {}));
    identities.set("AI-PM staff", await seedMember(ai, "staff", "staff", {}));
    identities.set("AI-PM delegated staff", await seedMember(ai, "staff-allow", "staff", { allow: [permission] }));
    identities.set("AI-PM readonly", await seedMember(ai, "readonly", "readonly", { allow: [permission, "workspace.write"] }));
    identities.set("AI-PM explicit deny", await seedMember(ai, "deny", "owner", { allow: [permission], deny: [permission] }));
    identities.set("AI-PM write deny", await seedMember(ai, "write-deny", "owner", { deny: ["workspace.write"] }));
    identities.set("guest", { ...ai.identity, memberNo: "GUEST" });
    identities.set("missing bundle", missing.identity);
    identities.set("mismatched bundle", mismatch.identity);
    identities.set("cross-tenant member", { ...ai.identity, tenantId: aiOther.tenantId, workspaceId: aiOther.workspaceId });
    for (const [label, identity] of identities) callers.set(label, await callerFor(identity));
    callers.set("anonymous", await callerFor(null));
    callers.set("invalid JWT", await callerFor(null, "invalid-mc013-token"));
  }, 30_000);

  afterEach(() => vi.restoreAllMocks());
  afterAll(async () => {
    if (originalBundleRoot === undefined) delete process.env.BUNDLES_ROOT;
    else process.env.BUNDLES_ROOT = originalBundleRoot;
    if (bundleRoot) rmSync(bundleRoot, { recursive: true, force: true });
    // Preserve append-only event evidence; each run owns UUID-isolated test rows.
    await closeAllPools();
  });

  it("loads both real Tiger and minimal AI-PM bundles successfully before evaluating permissions", async () => {
    expect(await resolveWorkspaceActiveBundle(tiger.workspaceId)).toMatchObject({ state: "ready", bundleId: "trading" });
    expect(await resolveWorkspaceActiveBundle(ai.workspaceId)).toMatchObject({ state: "ready", bundleId: "ai-pm" });
    expect((await resolveAuthoritativeClientAccess(tiger.identity)).navigationPermissions).not.toContain(permission);
    expect((await resolveAuthoritativeClientAccess(ai.identity)).navigationPermissions).toContain(permission);
    expect(routes).toHaveLength(16);
    expect(readRoutes).toHaveLength(6);
    expect(writeRoutes).toHaveLength(10);
  });

  const denied = ["Tiger owner", "Tiger spoofed allow", "AI-PM staff", "AI-PM explicit deny", "guest", "missing bundle", "mismatched bundle", "anonymous", "invalid JWT", "cross-tenant member"];
  it.each(denied.flatMap((label) => routes.map((route) => ({ label, route, name: route.name }))))("rejects $label / $name before its business handler", async ({ label, route }) => {
    const spies = interceptBusinessHandlers();
    const code = ["anonymous", "invalid JWT", "cross-tenant member"].includes(label) ? "UNAUTHORIZED" : "FORBIDDEN";
    await expect(route.call(callers.get(label)!, label.startsWith("Tiger") ? tiger : ai)).rejects.toMatchObject({ code });
    for (const spy of spies.values()) expect(spy).not.toHaveBeenCalled();
  });

  it.each(["AI-PM readonly", "AI-PM write deny"].flatMap((label) => writeRoutes.map((route) => ({ label, route, name: route.name }))))("rejects $label / $name even with navigation permission", async ({ label, route }) => {
    const spies = interceptBusinessHandlers();
    await expect(route.call(callers.get(label)!, ai)).rejects.toMatchObject({ code: "FORBIDDEN" });
    for (const spy of spies.values()) expect(spy).not.toHaveBeenCalled();
  });

  const allowed = ["AI-PM owner", "AI-PM manager", "AI-PM delegated staff"].flatMap((label) => routes.map((route) => ({ label, route, name: route.name })))
    .concat(["AI-PM readonly", "AI-PM write deny"].flatMap((label) => readRoutes.map((route) => ({ label, route, name: route.name }))));
  it.each(allowed)("routes authorized $label / $name to the expected handler", async ({ label, route }) => {
    const spies = interceptBusinessHandlers();
    await expect(route.call(callers.get(label)!, ai)).rejects.toThrow(`business-handler:${route.handler}`);
    expect(spies.get(route.handler)).toHaveBeenCalledTimes(1);
    expect(spies.get(route.handler)?.mock.calls[0]?.[0]).toBe(ai.workspaceId);
    for (const [handler, spy] of spies) if (handler !== route.handler) expect(spy).not.toHaveBeenCalled();
  });

  it("denies concurrent real Tiger task writes with no dev-table or event changes", async () => {
    const before = await snapshot();
    const results = await Promise.allSettled(Array.from({ length: 8 }, () => callers.get("Tiger owner")!.devtools.createTask(taskInput(tiger))));
    const outcomes = results.map((result) => result.status === "fulfilled" ? "FULFILLED" : result.reason.code);
    expect({ outcomes, after: await snapshot() }).toEqual({ outcomes: Array(8).fill("FORBIDDEN"), after: before });
  });

  it("creates an authorized real task with one event and keeps another tenant's reads empty", async () => {
    const caller = callers.get("AI-PM owner")!;
    const created = await caller.devtools.createTask(taskInput(ai));
    expect(created.task).toMatchObject({ workspace_id: ai.workspaceId, repo_id: ai.repoId, title: taskInput(ai).title, status: "draft", created_by: ai.identity.memberNo });
    const event = await getOwnerPool().query("SELECT payload FROM biz_events WHERE workspace_id=$1 AND payload->'object'->>'id'=$2", [ai.workspaceId, created.task.id]);
    expect(event.rows).toHaveLength(1);
    expect(event.rows[0]?.payload).toMatchObject({ who: { id: ai.identity.memberNo, type: "human" }, context: { tenant_id: ai.tenantId, workspace_id: ai.workspaceId }, decision: { action: "dev.task.create" } });
    expect((await caller.devtools.tasks()).tasks).toEqual(expect.arrayContaining([expect.objectContaining({ id: created.task.id })]));
    expect((await caller.devtools.taskDetail({ taskId: created.task.id })).task).toMatchObject({ id: created.task.id });
    const other = await callerFor(aiOther.identity);
    expect((await other.devtools.tasks()).tasks).toEqual([]);
    expect(await other.devtools.taskDetail({ taskId: created.task.id })).toMatchObject({ task: null, sessions: [], changesets: [], fences: [] });
    expect((await callers.get("AI-PM readonly")!.devtools.taskDetail({ taskId: created.task.id })).task).toMatchObject({ id: created.task.id });
  });

  it("accepts the declared size boundary and rejects empty/oversize inputs without writes", async () => {
    const caller = callers.get("AI-PM owner")!;
    const maxInput = { ...taskInput(ai), title: "界".repeat(200), prdSummary: "文".repeat(8000), acceptance: Array(20).fill("验".repeat(500)), constraints: Array(20).fill("限".repeat(300)) };
    const created = await caller.devtools.createTask(maxInput);
    expect(created.task.title).toBe(maxInput.title);
    const before = await snapshot();
    for (const input of [
      { ...taskInput(ai), title: "" }, { ...taskInput(ai), title: "x".repeat(201) },
      { ...taskInput(ai), acceptance: [] }, { ...taskInput(ai), acceptance: Array(21).fill("x") },
      { ...taskInput(ai), prdSummary: "x".repeat(8001) },
    ]) await expect(caller.devtools.createTask(input)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(await snapshot()).toEqual(before);
  });

  it("rejects a real authorized mutation aimed at another workspace's repo without changes", async () => {
    const before = await snapshot();
    await expect(callers.get("AI-PM owner")!.devtools.createTask({ ...taskInput(ai), repoId: aiOther.repoId })).rejects.toThrow("仓库未登记");
    expect(await snapshot()).toEqual(before);
  });

  it("rechecks database permissions, role and status for an already issued JWT", async () => {
    const identity = await seedMember(ai, "revocation", "owner", {});
    const caller = await callerFor(identity, await signDemoToken(identity));
    await expect(caller.devtools.repos()).resolves.toMatchObject({ repos: expect.any(Array) });
    const before = await snapshot();
    await getOwnerPool().query("UPDATE members SET permissions=$2 WHERE id=$1", [identity.memberId, JSON.stringify({ deny: [permission] })]);
    await expect(caller.devtools.repos()).rejects.toMatchObject({ code: "FORBIDDEN" });
    await expect(caller.devtools.createTask(taskInput(ai))).rejects.toMatchObject({ code: "FORBIDDEN" });
    await getOwnerPool().query("UPDATE members SET role='readonly',permissions=$2 WHERE id=$1", [identity.memberId, JSON.stringify({ allow: [permission] })]);
    await expect(caller.devtools.repos()).resolves.toMatchObject({ repos: expect.any(Array) });
    await expect(caller.devtools.createTask(taskInput(ai))).rejects.toMatchObject({ code: "FORBIDDEN" });
    await getOwnerPool().query("UPDATE members SET status='removed' WHERE id=$1", [identity.memberId]);
    await expect(caller.devtools.repos()).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    expect(await snapshot()).toEqual(before);
  });

  it("fails closed on a tampered Bundle and restores access only after integrity is restored", async () => {
    const path = join(bundleRoot!, "ai-pm/bundle.json");
    const original = readFileSync(path, "utf8");
    const tampered = JSON.parse(original);
    tampered.workloom.displayName = "摘要失配";
    const before = await snapshot();
    writeFileSync(path, JSON.stringify(tampered));
    try {
      expect(await resolveWorkspaceActiveBundle(ai.workspaceId)).toMatchObject({ state: "projection-invalid" });
      const spies = interceptBusinessHandlers();
      for (const route of routes) await expect(route.call(callers.get("AI-PM owner")!, ai)).rejects.toMatchObject({ code: "FORBIDDEN" });
      for (const spy of spies.values()) expect(spy).not.toHaveBeenCalled();
      expect(await snapshot()).toEqual(before);
    } finally {
      writeFileSync(path, original);
    }
    expect(await resolveWorkspaceActiveBundle(ai.workspaceId)).toMatchObject({ state: "ready" });
  });
});
