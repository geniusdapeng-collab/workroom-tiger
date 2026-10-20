/** MC-018: real loopback HTTP, signed JWTs, migrated PostgreSQL and RLS.
 * The sole synthetic seam is a build-time adapter for a test-only Bundle. It
 * still reads real scoped tickets and passes through the real projection guard.
 */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { serve } from "@hono/node-server";
import { SignJWT } from "jose";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { closeAllPools, getAppPool, getOwnerPool } from "@workloom/db";
import { canonicalBundleArtifactPayload, parseBundleManifest } from "../../../../packages/industry-contract/dist/index.js";
import { fixtureDatabaseUrls } from "../test-fixtures/governance.js";
import { issueCToken, issueH5EntryToken, verifyCToken, verifyH5EntryToken } from "./channels.js";
import { ensureServiceSchema, indexChunks } from "./store.js";
import { classify, ticketKindOf } from "./dialog.js";
import { resolveWorkspaceBusinessAdapter } from "./adapters/business-registry.js";
import { serviceGateway } from "./gateway.js";

const adapterMode = vi.hoisted(() => ({ value: "normal" }));
vi.mock("../industry/business-adapter-catalog.js", () => ({ BUNDLED_BUSINESS_ADAPTERS: [{
  bundleIds: ["h5-fixture"],
  adapter: {
    id: "mc018-fixture",
    classify: (text: string) => text.includes("测试记录") ? { tool: "query_order", answer: "为您查询测试记录：" }
      : text.includes("测试目录") ? { tool: "query_catalog", answer: "为您查询测试目录：" } : null,
    ticketKind: (text: string) => text.includes("测试维护") ? "repair" : null,
    async queryOrder(ctx: { workspaceId: string; cUserId: string; memberId?: string | null }) {
      if (adapterMode.value === "throw") throw new Error("private-adapter-failure-must-not-leak");
      const { svcQuery } = await import("./events.js");
      const rows = await svcQuery<{ id: string; title: string }>(ctx.workspaceId,
        "SELECT id,title FROM c_tickets WHERE workspace_id=$1 AND c_user_id=$2 ORDER BY id", [ctx.workspaceId, ctx.cUserId]);
      return { demo: false, privateWorkspace: ctx.workspaceId, orders: rows.map((row) => ({
        id: row.id, cardTitle: "测试记录", title: adapterMode.value === "invalid" ? "workspace_id" : row.title,
        statusText: "已受理", details: [{ label: "核验", value: ctx.memberId ? "已核验" : "未核验" }], privateSecret: "never-client",
      })) };
    },
    async queryMember() { return { demo: false, member: { title: "测试客户", metric: { label: "次数", value: "3" }, benefits: ["测试权益"], privateField: "never-client" } }; },
    async queryCatalog() { return { demo: false, cardTitle: "测试目录", items: [{ id: "fixture", title: "测试服务", details: [], priceText: "免费" }] }; },
  },
}] }));

interface Scope { workspaceId: string; tenantId: string; userId: string; token: string; bundleId: string }
type Json = Record<string, unknown>;
interface TicketBody extends Json { ticket?: { id: string; title: string; cUserId: string; payload: Json }; receipt?: { requestId: string; state: string; resourceId: string; eventId?: string; idempotentReplay?: boolean; delivery?: { state: string; notificationId?: string } }; }
interface SessionBody extends Json { token?: string; user?: { id: string; workspaceId: string; authMode?: string; openid: string } }
const envKeys = ["NODE_ENV", "SERVICE_C_DEMO_AUTH", "SERVICE_C_SECRET", "SERVICE_C_H5_ENTRY_SECRET", "SERVICE_C_WORKSPACE_ID", "SERVICE_C_WORKSPACE_MAP", "SERVICE_C_PUBLIC_WORKSPACE_KEY"] as const;

function writeBundle(root: string, id: string, enabled: boolean, adapterId?: string) {
  const raw = {
    schemaVersion: "workloom.bundle/v1", name: `@workloom/${id}`, version: "1.0.0",
    workloom: {
      industry: id, displayName: "入口隔离测试包", description: "仅用于本地接口测试", status: "candidate", owner: "WorkLoom",
      compatibility: { base: ">=0.1.0 <1.0.0", ui: ">=0.1.0 <1.0.0", contract: "2.0.0" },
      provides: { presets: [], fences: [], skills: [], schemas: [], ui: [], serviceFront: [], seeds: [] },
      ui: { schemaVersion: "workloom.bundle.ui/v1", terminology: {}, navigation: { slots: [] }, home: { widgets: [] }, objects: [], workflows: [],
        serviceFront: { enabled, identityPolicy: "disabled", tabs: [], services: [], ...(adapterId ? { adapterId } : {}) },
        theme: { brand: {} }, permissions: [], experiments: [] },
    },
  };
  const digest = createHash("sha256").update(canonicalBundleArtifactPayload(parseBundleManifest(raw), {})).digest("hex");
  mkdirSync(join(root, id));
  writeFileSync(join(root, id, "bundle.json"), JSON.stringify({ ...raw, integrity: { algorithm: "sha256", digest, assets: {} } }));
}

describe.skipIf(process.env.RUN_SERVICE_GATEWAY_PG_TESTS !== "1")("C gateway boundaries (real HTTP/PostgreSQL)", () => {
  const tag = `mc018-${randomUUID()}`;
  const scopes: Scope[] = [];
  const evidence: Json[] = [];
  const originalEnv = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
  const originalRoot = process.env.BUNDLES_ROOT;
  let baseEnv: Record<string, string | undefined>;
  let bundleRoot = "";
  let server: ReturnType<typeof serve>;
  let baseUrl = "";
  let generic: Scope;
  let other: Scope;
  let adapter: Scope;
  let edge: Scope;
  const blocked = new Map<string, Scope>();

  function restoreEnv(values: Record<string, string | undefined>) {
    for (const key of envKeys) if (values[key] === undefined) delete process.env[key]; else process.env[key] = values[key];
  }
  function scrub(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(scrub);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, /token|secret|authorization/i.test(key) ? "[REDACTED]" : scrub(item)]));
    return value;
  }
  async function request<T extends Json = Json>(path: string, body?: unknown, token?: string) {
    const response = await fetch(`${baseUrl}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": randomUUID(), ...(token ? { authorization: `Bearer ${token}` } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const data = await response.json() as T;
    evidence.push({ path, method: body === undefined ? "GET" : "POST", authenticated: !!token, input: scrub(body), status: response.status, body: scrub(data) });
    return { status: response.status, body: data };
  }
  async function entry(workspaceKey = "brand-a", subject = "signed-person", appId = "gateway-a") {
    return issueH5EntryToken({ workspaceKey, subject, appId, secret: process.env.SERVICE_C_H5_ENTRY_SECRET! });
  }
  async function login(workspaceKey = "brand-a", subject?: string, appId?: string) {
    return request<SessionBody>("/session", { channel: "h5", workspaceKey, entryToken: await entry(workspaceKey, subject, appId) });
  }
  async function seed(label: string, bundleId = "trading", install: string | null = bundleId): Promise<Scope> {
    const workspaceId = `${tag}-${label}-ws`;
    const tenantId = `${tag}-${label}-tenant`;
    const userId = `${tag}-${label}-user`;
    await getOwnerPool().query("INSERT INTO tenants(id,name,plan) VALUES ($1,'入口测试','pro')", [tenantId]);
    await getOwnerPool().query("INSERT INTO workspaces(id,tenant_id,slug,name,industry,bundle_id) VALUES ($1,$2,$1,'入口测试',$3,$3)", [workspaceId, tenantId, bundleId]);
    if (install) await getOwnerPool().query("INSERT INTO bundle_installs(id,workspace_id,bundle_id,status) VALUES ($1,$2,$3,'active')", [`${workspaceId}-bi`, workspaceId, install]);
    await getOwnerPool().query("INSERT INTO c_users(id,workspace_id,channel,openid) VALUES ($1,$2,'h5',$1)", [userId, workspaceId]);
    const token = await issueCToken({ workspaceId, cUserId: userId, channel: "h5", secret: process.env.SERVICE_C_SECRET! });
    const scope = { workspaceId, tenantId, userId, token, bundleId };
    scopes.push(scope);
    return scope;
  }
  async function dataSnapshot() {
    return Object.fromEntries(await Promise.all(["kb_collections", "kb_documents", "kb_chunks", "demo_orders", "demo_members"].map(async (table) => {
      const result = await getOwnerPool().query(`SELECT count(*)::int AS count,md5(COALESCE(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)::text),'')) AS digest FROM ${table} t WHERE workspace_id=ANY($1::text[])`, [scopes.map((s) => s.workspaceId)]);
      return [table, result.rows[0]];
    })));
  }
  async function ticket(scope: Scope, key = randomUUID(), extra: Json = {}) {
    return request<TicketBody>("/tickets", { kind: "other", title: "用户工单 '特殊字符' <回归>", payload: { detail: "正文" }, idempotencyKey: key, ...extra }, scope.token);
  }
  async function eventsFor(scope: Scope, id: string, action: string) {
    return (await getOwnerPool().query("SELECT event_id AS id,payload FROM biz_events WHERE workspace_id=$1 AND payload->'object'->>'id'=$2 AND payload->'decision'->>'action'=$3", [scope.workspaceId, id, action])).rows;
  }
  async function completedTicket(scope: Scope) {
    const result = await ticket(scope);
    expect(result.status).toBe(200);
    const id = result.body.ticket!.id;
    await getOwnerPool().query("UPDATE c_tickets SET status='done' WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, id]);
    return id;
  }
  beforeAll(async () => {
    fixtureDatabaseUrls();
    process.env.SERVICE_C_SECRET ??= randomBytes(40).toString("base64url");
    process.env.SERVICE_C_H5_ENTRY_SECRET ??= randomBytes(40).toString("base64url");
    expect(new URL(process.env.DATABASE_URL!).pathname).toMatch(/^\/workloom_tiger_/);
    expect((await getAppPool().query("SELECT rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0]).toMatchObject({ rolname: "workloom_app", rolsuper: false, rolbypassrls: false });
    bundleRoot = mkdtempSync(join(tmpdir(), "workloom-mc018-"));
    cpSync(fileURLToPath(new URL("../../../../bundles/trading", import.meta.url)), join(bundleRoot, "trading"), { recursive: true });
    writeBundle(bundleRoot, "h5-fixture", true, "mc018-fixture");
    writeBundle(bundleRoot, "h5-disabled", false);
    writeBundle(bundleRoot, "h5-unknown", true, "unknown-adapter");
    writeBundle(bundleRoot, "h5-untrusted", true, "mc018-fixture");
    process.env.BUNDLES_ROOT = bundleRoot;
    generic = await seed("generic"); other = await seed("other"); adapter = await seed("adapter", "h5-fixture"); edge = await seed("edge");
    for (const [label, bundleId, install] of [["disabled", "h5-disabled", "h5-disabled"], ["unknown", "h5-unknown", "h5-unknown"], ["untrusted", "h5-untrusted", "h5-untrusted"], ["missing", "trading", null], ["mismatch", "trading", "h5-fixture"]]) {
      blocked.set(label!, await seed(label!, bundleId!, install));
    }
    process.env.NODE_ENV = "test"; process.env.SERVICE_C_DEMO_AUTH = "false";
    delete process.env.SERVICE_C_WORKSPACE_ID; delete process.env.SERVICE_C_PUBLIC_WORKSPACE_KEY;
    process.env.SERVICE_C_WORKSPACE_MAP = JSON.stringify({ "brand-a": generic.workspaceId, "brand-b": other.workspaceId, ...Object.fromEntries([...blocked].map(([key, scope]) => [key, scope.workspaceId])) });
    baseEnv = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
    // Explicit, neutral knowledge fixture and a historical record that startup must preserve.
    const docId = `${tag}-document`;
    await getOwnerPool().query("INSERT INTO kb_collections(id,workspace_id,name) VALUES ($1,$2,'隔离知识')", [`${tag}-collection`, generic.workspaceId]);
    const md = "# 隔离知识\n\n## 回归资料使用说明\n回归资料仅供本地模拟验证，正式资料由工作区管理员上传。";
    await getOwnerPool().query("INSERT INTO kb_documents(id,workspace_id,collection_id,title,source_kind,content_md,status) VALUES ($1,$2,$3,'隔离知识','manual',$4,'active')", [docId, generic.workspaceId, `${tag}-collection`, md]);
    await indexChunks(getOwnerPool(), generic.workspaceId, docId, md);
    await getOwnerPool().query("INSERT INTO demo_members(member_id,workspace_id,name,phone,tier,points) VALUES ('historical',$1,'历史记录','10000','测试',1)", [generic.workspaceId]);
    evidence.push({ phase: "before-bootstrap", snapshot: await dataSnapshot() });
    const port = Number(process.env.MC018_HTTP_PORT ?? 8798);
    server = serve({ fetch: serviceGateway.fetch, hostname: "127.0.0.1", port });
    await new Promise<void>((resolve, reject) => { server.once("listening", resolve); server.once("error", reject); });
    baseUrl = `http://127.0.0.1:${port}`;
  }, 30_000);
  afterEach(() => { restoreEnv(baseEnv); adapterMode.value = "normal"; });
  afterAll(async () => {
    if (server) await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    if (scopes.length) evidence.push({ phase: "after-tests", snapshot: await dataSnapshot(), scopes: scopes.map(({ token: _token, ...scope }) => scope) });
    if (process.env.MC018_HTTP_EVIDENCE) writeFileSync(process.env.MC018_HTTP_EVIDENCE, `${JSON.stringify(evidence, null, 2)}\n`);
    restoreEnv(originalEnv);
    if (originalRoot === undefined) delete process.env.BUNDLES_ROOT; else process.env.BUNDLES_ROOT = originalRoot;
    if (bundleRoot) rmSync(bundleRoot, { recursive: true, force: true });
    await closeAllPools();
  });

  it("fails on a missing migrated table, retries after restoration, and never seeds or mutates workspace data", async () => {
    const before = await dataSnapshot();
    await getOwnerPool().query("ALTER TABLE kb_sources RENAME TO mc018_hidden_kb_sources");
    try { await expect(ensureServiceSchema()).rejects.toThrow(); }
    finally { await getOwnerPool().query("ALTER TABLE mc018_hidden_kb_sources RENAME TO kb_sources"); }
    await ensureServiceSchema();
    expect(await dataSnapshot()).toEqual(before);
  });
  it("resolves the real trading generic front and the declared synthetic adapter", async () => {
    expect((await resolveWorkspaceBusinessAdapter(generic.workspaceId)).state).toBe("adapter-not-declared");
    expect((await resolveWorkspaceBusinessAdapter(adapter.workspaceId)).state).toBe("ready");
  });
  it("requires a signed H5 entry in production even when legacy demo=true", async () => {
    process.env.NODE_ENV = "production"; process.env.SERVICE_C_DEMO_AUTH = "true";
    const result = await request("/session", { channel: "h5", openid: "forged", workspaceKey: "brand-a" });
    expect(result.status).toBe(401); expect(result.body.code).toBe("H5_ENTRY_REQUIRED");
  });
  it.each(["malformed", "expired", "tampered", "wrong-audience", "no-exp", "wrong-alg"])("rejects %s entry tokens without demo fallback", async (kind) => {
    process.env.SERVICE_C_DEMO_AUTH = "true";
    let token = "mock.invalid.jwt";
    if (kind === "expired") token = await issueH5EntryToken({ workspaceKey: "brand-a", subject: "person", appId: "app", secret: process.env.SERVICE_C_H5_ENTRY_SECRET!, expiresIn: "-1s" });
    if (kind === "tampered") { const valid = await entry(); token = `${valid.slice(0, valid.lastIndexOf(".") + 1)}AAAA`; }
    if (["wrong-audience", "no-exp", "wrong-alg"].includes(kind)) {
      let jwt = new SignJWT({ workspaceKey: "brand-a", subject: "person", appId: "app", scope: "c-entry" }).setProtectedHeader({ alg: kind === "wrong-alg" ? "HS512" : "HS256" }).setIssuer("workloom-c-entry").setAudience(kind === "wrong-audience" ? "other" : "workloom-c-h5").setIssuedAt();
      if (kind !== "no-exp") jwt = jwt.setExpirationTime("15m");
      token = await jwt.sign(new TextEncoder().encode(process.env.SERVICE_C_H5_ENTRY_SECRET!));
    }
    const result = await request("/session", { channel: "h5", entryToken: token, workspaceKey: "brand-a", openid: "forged" });
    expect(result.status).toBe(401); expect(result.body.code).toBe("H5_ENTRY_INVALID");
  });
  it("rejects body/signature scope mismatch before any routing lookup", async () => {
    delete process.env.SERVICE_C_WORKSPACE_MAP;
    const result = await request("/session", { channel: "h5", workspaceKey: "brand-b", entryToken: await entry() });
    expect(result.status).toBe(403); expect(result.body.code).toBe("H5_ENTRY_SCOPE_MISMATCH");
  });
  it.each(["missing", "short"])("fails closed for a %s production entry secret", async (kind) => {
    const token = await entry(); process.env.NODE_ENV = "production";
    if (kind === "missing") delete process.env.SERVICE_C_H5_ENTRY_SECRET; else process.env.SERVICE_C_H5_ENTRY_SECRET = "short";
    expect((await request("/session", { channel: "h5", workspaceKey: "brand-a", entryToken: token })).status).toBe(503);
  });
  it.each(["unconfigured", "unknown", "missing-workspace", "malformed-map", "array-map"])("rejects %s routing and ignores raw client workspace IDs", async (kind) => {
    if (kind === "unconfigured") delete process.env.SERVICE_C_WORKSPACE_MAP;
    if (kind === "unknown") process.env.SERVICE_C_WORKSPACE_MAP = "{}";
    if (kind === "missing-workspace") process.env.SERVICE_C_WORKSPACE_MAP = JSON.stringify({ "brand-a": "nonexistent-workspace" });
    if (kind === "malformed-map") process.env.SERVICE_C_WORKSPACE_MAP = "{";
    if (kind === "array-map") process.env.SERVICE_C_WORKSPACE_MAP = "[]";
    const result = await request("/session", { channel: "h5", workspaceKey: "brand-a", workspaceId: generic.workspaceId, entryToken: await entry() });
    expect(result.status).toBe(503); expect(JSON.stringify(result.body)).not.toContain("nonexistent-workspace");
  });
  it.each(["disabled", "unknown", "untrusted", "missing", "mismatch"])("blocks login and existing sessions for %s Bundle state", async (key) => {
    expect((await login(key)).status).toBe(503);
    expect((await request("/tickets", undefined, blocked.get(key)!.token)).status).toBe(503);
  });
  it("derives signed identity from app and subject, ignores openid, and preserves stable concurrent login", async () => {
    const results = await Promise.all(Array.from({ length: 12 }, () => login("brand-a", "并发身份")));
    expect(results.map((r) => r.status)).toEqual(Array(12).fill(200));
    const ids = new Set(results.map((r) => r.body.user!.id)); expect(ids.size).toBe(1);
    expect(results[0]!.body.user).toMatchObject({ workspaceId: generic.workspaceId, authMode: "channel" });
    const forged = await request<SessionBody>("/session", { channel: "h5", entryToken: await entry("brand-a", "并发身份"), openid: "different-client-identity" });
    expect(forged.body.user?.id).toBe(results[0]!.body.user!.id);
    expect((await login("brand-a", "并发身份", "another-app")).body.user?.id).not.toBe(results[0]!.body.user!.id);
    expect((await login("brand-b", "并发身份")).body.user?.id).not.toBe(results[0]!.body.user!.id);
  });
  it("supports explicit development demo sessions without impersonating a signed identity", async () => {
    const signed = await login(); expect(signed.status).toBe(200);
    process.env.SERVICE_C_DEMO_AUTH = "true";
    const demo = await request<SessionBody>("/session", { channel: "h5", workspaceKey: "brand-a", openid: signed.body.user!.openid });
    expect(demo.status).toBe(200); expect(demo.body.user?.authMode).toBe("demo"); expect(demo.body.user?.id).not.toBe(signed.body.user!.id);
  });
  it("accepts the real web client null entry-token shape only in development demo mode", async () => {
    process.env.SERVICE_C_DEMO_AUTH = "true";
    const body = { channel: "h5", workspaceKey: "brand-a", openid: "browser-local-identity", entryToken: null, nickname: "测试用户" };
    const demo = await request<SessionBody>("/session", body);
    expect(demo.status).toBe(200); expect(demo.body.user?.authMode).toBe("demo");
    process.env.NODE_ENV = "production";
    const formal = await request("/session", body);
    expect(formal.status).toBe(401); expect(formal.body.code).toBe("H5_ENTRY_REQUIRED");
  });
  it("honors configured fixed workspace while checking the configured public site key", async () => {
    process.env.SERVICE_C_WORKSPACE_ID = generic.workspaceId; process.env.SERVICE_C_PUBLIC_WORKSPACE_KEY = "brand-a";
    expect((await login()).status).toBe(200);
    expect((await login("brand-b")).status).toBe(403);
  });
  it.each(["missing", "tampered", "expired", "wrong-workspace", "missing-workspace", "wrong-user", "wrong-channel", "no-exp", "invalid-claims"])("rejects %s C session authentication", async (kind) => {
    let token: string | undefined = generic.token;
    if (kind === "missing") token = undefined;
    if (kind === "tampered") token = "mock.invalid.jwt";
    if (["expired", "no-exp", "invalid-claims"].includes(kind)) {
      let jwt = new SignJWT({ workspaceId: generic.workspaceId, cUserId: kind === "invalid-claims" ? undefined : generic.userId, channel: "h5", scope: "c-user" }).setProtectedHeader({ alg: "HS256" }).setIssuer("workloom-c").setIssuedAt();
      if (kind !== "no-exp") jwt = jwt.setExpirationTime(kind === "expired" ? "-1s" : "12h");
      token = await jwt.sign(new TextEncoder().encode(process.env.SERVICE_C_SECRET!));
    }
    if (["wrong-workspace", "wrong-user", "wrong-channel"].includes(kind)) token = await issueCToken({ workspaceId: kind === "wrong-workspace" ? other.workspaceId : generic.workspaceId, cUserId: kind === "wrong-user" ? "missing-user" : generic.userId, channel: kind === "wrong-channel" ? "alipay" : "h5", secret: process.env.SERVICE_C_SECRET! });
    if (kind === "missing-workspace") token = await issueCToken({ workspaceId: "missing-workspace", cUserId: generic.userId, channel: "h5", secret: process.env.SERVICE_C_SECRET! });
    expect((await request("/tickets", undefined, token)).status).toBe(401);
  });
  it("requires expiration in both exported JWT verifiers", async () => {
    const c = await new SignJWT({ workspaceId: generic.workspaceId, cUserId: generic.userId, channel: "h5", scope: "c-user" }).setProtectedHeader({ alg: "HS256" }).setIssuer("workloom-c").setIssuedAt().sign(new TextEncoder().encode(process.env.SERVICE_C_SECRET!));
    const h = await new SignJWT({ workspaceKey: "brand-a", subject: "person", appId: "app", scope: "c-entry" }).setProtectedHeader({ alg: "HS256" }).setIssuer("workloom-c-entry").setAudience("workloom-c-h5").setIssuedAt().sign(new TextEncoder().encode(process.env.SERVICE_C_H5_ENTRY_SECRET!));
    expect((await verifyCToken(c, process.env.SERVICE_C_SECRET!)) === null).toBe(true);
    expect((await verifyH5EntryToken(h, process.env.SERVICE_C_H5_ENTRY_SECRET!)) === null).toBe(true);
  });
  it.each((["c", "h5"] as const).flatMap((mode) => ["no-iat", "long-window", "wrong-alg", "future-iat", "expired"].map((defect) => ({ mode, defect }))))("rejects $mode JWT $defect at the verifier boundary", async ({ mode, defect }) => {
    const now = Math.floor(Date.now() / 1000);
    const lifetime = mode === "c" ? 12 * 3600 : 15 * 60;
    const secret = mode === "c" ? process.env.SERVICE_C_SECRET! : process.env.SERVICE_C_H5_ENTRY_SECRET!;
    const claims = mode === "c" ? { workspaceId: generic.workspaceId, cUserId: generic.userId, channel: "h5", scope: "c-user" }
      : { workspaceKey: "brand-a", subject: "person", appId: "app", scope: "c-entry" };
    let jwt = new SignJWT(claims).setProtectedHeader({ alg: defect === "wrong-alg" ? "HS512" : "HS256" }).setIssuer(mode === "c" ? "workloom-c" : "workloom-c-entry");
    if (mode === "h5") jwt = jwt.setAudience("workloom-c-h5");
    if (defect !== "no-iat") jwt = jwt.setIssuedAt(defect === "future-iat" ? now + 600 : now);
    jwt = jwt.setExpirationTime(defect === "expired" ? now - 1 : now + (defect === "long-window" ? lifetime * 2 : lifetime));
    const token = await jwt.sign(new TextEncoder().encode(secret));
    const valid = mode === "c" ? await verifyCToken(token, secret) : await verifyH5EntryToken(token, secret);
    expect(valid === null).toBe(true);
  });
  it("revokes an existing session when active Bundle facts change", async () => {
    expect((await request("/tickets", undefined, other.token)).status).toBe(200);
    await getOwnerPool().query("UPDATE bundle_installs SET status='uninstalled' WHERE workspace_id=$1", [other.workspaceId]);
    try { expect((await request("/tickets", undefined, other.token)).status).toBe(503); }
    finally { await getOwnerPool().query("UPDATE bundle_installs SET status='active' WHERE workspace_id=$1", [other.workspaceId]); }
  });
  it("rejects tampered Bundle source for existing sessions", async () => {
    const path = join(bundleRoot, "h5-fixture/bundle.json"); const original = readFileSync(path, "utf8");
    const changed = JSON.parse(original); changed.workloom.ui.serviceFront.enabled = false;
    writeFileSync(path, JSON.stringify(changed));
    try { expect((await request("/orders", undefined, adapter.token)).status).toBe(503); }
    finally { writeFileSync(path, original); }
  });
  it.each(["/orders", "/member"])("returns unavailable for undeclared business capability %s", async (path) => {
    await getOwnerPool().query("UPDATE c_users SET member_id='M-1001' WHERE id=$1", [generic.userId]);
    const result = await request(path, undefined, generic.token);
    expect(result.status).toBe(503); expect(result.body.code).toBe("BUSINESS_ADAPTER_UNAVAILABLE"); expect(result.body.requestId).toEqual(expect.any(String));
  });
  it("retains neutral knowledge and does not infer hotel or trading intents without an adapter", async () => {
    expect(classify("豪华大床房多少钱").intent).toBe("kb_qa"); expect(ticketKindOf("空调坏了")).toBe("other"); expect(ticketKindOf("订阅研报")).toBe("other");
    const kb = await request("/chat", { text: "回归资料使用说明" }, generic.token); expect(kb.status).toBe(200);
    expect(JSON.stringify(kb.body.citations)).toContain("正式资料由工作区管理员上传");
    const hotel = await request("/chat", { text: "豪华大床房多少钱" }, generic.token); expect(hotel.status).toBe(200);
    expect(hotel.body.cards).toEqual([]); expect(JSON.stringify(hotel.body)).not.toMatch(/云栖酒店|入住.*豪华|588/);
  });
  it("uses the selected adapter, scopes records to the caller, and strips private output", async () => {
    const made = await ticket(adapter); expect(made.status).toBe(200);
    const result = await request("/orders", undefined, adapter.token); expect(result.status).toBe(200);
    const orders = result.body.orders as Json[]; expect(orders.map((o) => o.id)).toContain(made.body.ticket!.id);
    expect(JSON.stringify(result.body)).not.toMatch(/privateWorkspace|privateSecret|never-client/);
    expect(orders[0]?.details).toEqual([{ label: "核验", value: "未核验" }]);
    await getOwnerPool().query("UPDATE c_users SET member_id='unverified-subject' WHERE id=$1", [adapter.userId]);
    const unverified = await request("/orders", undefined, adapter.token);
    expect((unverified.body.orders as Json[])[0]?.details).toEqual([{ label: "核验", value: "未核验" }]);
    const member = await request("/member", undefined, adapter.token); expect(member.body).toMatchObject({ title: "测试客户", benefits: ["测试权益"] }); expect(member.body.level).toBeUndefined();
    const chat = await request("/chat", { text: "查询测试记录" }, adapter.token); expect(chat.status).toBe(200); expect(chat.body.answer).toBe("为您查询测试记录："); expect((chat.body.cards as Json[])[0]?.kind).toBe("order");
    const catalog = await request("/chat", { text: "查询测试目录" }, adapter.token); expect(catalog.status).toBe(200); expect((catalog.body.cards as Json[])[0]?.kind).toBe("catalog");
  });
  it.each(["invalid", "throw"])("rejects %s adapter output without leaking implementation details", async (mode) => {
    await ticket(adapter); adapterMode.value = mode;
    const result = await request("/orders", undefined, adapter.token); expect(result.status).toBe(mode === "invalid" ? 502 : 500);
    expect(JSON.stringify(result.body)).not.toMatch(/private-adapter|workspace_id|never-client/);
  });
  it("returns real creation receipts and one ticket/event/notification under concurrent replay", async () => {
    const key = randomUUID(); const results = await Promise.all(Array.from({ length: 8 }, () => ticket(generic, key)));
    expect(results.map((r) => r.status)).toEqual(Array(8).fill(200));
    expect(new Set(results.map((r) => r.body.ticket!.id)).size).toBe(1);
    const id = results[0]!.body.ticket!.id;
    const events = await eventsFor(generic, id, "service.ticket.create"); expect(events).toHaveLength(1);
    for (const result of results) expect(result.body.receipt).toMatchObject({ state: "accepted", resourceId: id, eventId: events[0]!.id, requestId: expect.any(String) });
    const notes = await getOwnerPool().query("SELECT id,status FROM c_notifications WHERE workspace_id=$1 AND payload->>'ticketId'=$2", [generic.workspaceId, id]); expect(notes.rows).toHaveLength(1); expect(notes.rows[0].status).toBe("pending");
    const original = results.find((r) => !r.body.receipt?.idempotentReplay)!; expect(original.body.receipt?.delivery).toMatchObject({ state: "demo", notificationId: String(notes.rows[0].id) });
  });
  it("does not leak another user ticket on idempotency-key collision", async () => {
    const key = randomUUID(); const made = await ticket(generic, key); expect(made.status).toBe(200);
    const loginResult = await login("brand-a", "second-person"); expect(loginResult.status).toBe(200);
    const replay = await request("/tickets", { kind: "other", title: "攻击重放", idempotencyKey: key }, loginResult.body.token);
    expect(replay.status).toBe(409); expect(JSON.stringify(replay.body)).not.toContain(made.body.ticket!.id);
  });
  it("validates conversation ownership before ticket writes and keeps chat in the caller scope", async () => {
    const foreign = `${tag}-foreign-conversation`;
    await getOwnerPool().query("INSERT INTO c_conversations(id,workspace_id,c_user_id,channel) VALUES ($1,$2,$3,'h5')", [foreign, other.workspaceId, other.userId]);
    const created = await ticket(generic, randomUUID(), { conversationId: foreign }); expect(created.status).toBe(404);
    const chat = await request("/chat", { text: "需要人工协助", conversationId: foreign }, generic.token); expect(chat.status).toBe(200); expect(chat.body.conversationId).not.toBe(foreign);
    expect((await getOwnerPool().query("SELECT id FROM c_messages WHERE conversation_id=$1", [foreign])).rowCount).toBe(0);
  });
  it("returns a real receipt when confirming a chat draft and reads only the current user's ticket status", async () => {
    const result = await request<TicketBody>("/chat", { text: "请处理这条请求", confirmTicket: true, ticketDraft: { kind: "other", title: "聊天确认测试", payload: {} } }, generic.token);
    expect(result.status).toBe(200); expect(result.body.receipt?.resourceId).toBe(result.body.ticket!.id);
    expect((await eventsFor(generic, result.body.ticket!.id, "service.ticket.create"))[0]?.id).toBe(result.body.receipt?.eventId);
    const status = await request("/chat", { text: "工单进度怎么样" }, generic.token); expect(status.status).toBe(200); expect(String(status.body.answer)).toContain("聊天确认测试");
  });
  it.each([null, [], { text: 7 }, { text: "x".repeat(2001) }, { text: "确认", confirmTicket: true, ticketDraft: { kind: "arbitrary", title: "测试", payload: {} } }, { text: "确认", confirmTicket: true, ticketDraft: { kind: "other", title: "x".repeat(121), payload: {} } }, { text: "确认", confirmTicket: true, ticketDraft: { kind: "other", title: "测试", payload: { large: "x".repeat(10 * 1024) } } }])("rejects malformed or oversized chat input %# before a ticket write", async (body) => {
    const before = (await getOwnerPool().query("SELECT count(*)::int AS n FROM c_tickets WHERE workspace_id=$1", [generic.workspaceId])).rows[0].n;
    expect((await request("/chat", body, generic.token)).status).toBe(400);
    expect((await getOwnerPool().query("SELECT count(*)::int AS n FROM c_tickets WHERE workspace_id=$1", [generic.workspaceId])).rows[0].n).toBe(before);
  });
  it.each([null, [], { kind: "other", title: 9 }, { kind: "other", title: "测试", payload: [] }, { kind: "other", title: "测试", idempotencyKey: {} }])("rejects malformed ticket input %# as client error", async (body) => {
    expect((await request("/tickets", body, generic.token)).status).toBe(400);
  });
  it("accepts exact text/title/payload limits and rejects excess UTF-8 payload bytes", async () => {
    expect((await request("/chat", { text: "问".repeat(2000) }, edge.token)).status).toBe(200);
    const payload = { value: "x".repeat(10 * 1024 - Buffer.byteLength(JSON.stringify({ value: "" }))) };
    expect(Buffer.byteLength(JSON.stringify(payload))).toBe(10 * 1024);
    expect((await ticket(edge, randomUUID(), { title: "测".repeat(120), payload })).status).toBe(200);
    expect((await ticket(edge, randomUUID(), { payload: { value: "测".repeat(3500) } })).status).toBe(400);
  });
  it("protects ticket detail, rating, lists and notifications from other customers", async () => {
    const id = await completedTicket(edge);
    expect((await request(`/tickets/${id}`, undefined, other.token)).status).toBe(404);
    expect((await request(`/tickets/${id}/rate`, { score: 5 }, other.token)).status).toBe(404);
    expect(JSON.stringify((await request("/tickets", undefined, other.token)).body)).not.toContain(id);
    expect(JSON.stringify((await request("/notifications", undefined, other.token)).body)).not.toContain(id);
  });
  it.each(["recoverable", "unavailable"])("keeps committed acceptance truthful when notifications are %s", async (mode) => {
    await getOwnerPool().query(`CREATE OR REPLACE FUNCTION mc018_fail_notification() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF ${mode === "recoverable" ? "NEW.status <> 'failed'" : "true"} THEN RAISE EXCEPTION 'mc018 injected notification failure'; END IF;
      RETURN NEW; END $$`);
    await getOwnerPool().query("CREATE TRIGGER mc018_fail_notification BEFORE INSERT ON c_notifications FOR EACH ROW EXECUTE FUNCTION mc018_fail_notification()");
    try {
      const result = await ticket(edge);
      expect(result.status).toBe(200); expect(result.body.receipt?.delivery?.state).toBe("failed");
      expect(await eventsFor(edge, result.body.ticket!.id, "service.ticket.create")).toHaveLength(1);
      const notes = (await getOwnerPool().query("SELECT id,status FROM c_notifications WHERE workspace_id=$1 AND payload->>'ticketId'=$2", [edge.workspaceId, result.body.ticket!.id])).rows;
      if (mode === "recoverable") {
        expect(notes).toHaveLength(1); expect(notes[0].status).toBe("failed");
        expect(result.body.receipt?.delivery?.notificationId).toBe(String(notes[0].id));
      } else { expect(notes).toHaveLength(0); expect(result.body.receipt?.delivery?.notificationId).toBeUndefined(); }
    } finally { await getOwnerPool().query("DROP TRIGGER mc018_fail_notification ON c_notifications"); await getOwnerPool().query("DROP FUNCTION mc018_fail_notification()"); }
  });
  it("does not commit a new ticket or timeline when its immutable creation event fails", async () => {
    const key = randomUUID();
    const before = (await getOwnerPool().query("SELECT count(*)::int AS n FROM c_ticket_events WHERE workspace_id=$1", [edge.workspaceId])).rows[0].n;
    await getOwnerPool().query("CREATE OR REPLACE FUNCTION mc018_fail_create() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.payload->'decision'->>'action' = 'service.ticket.create' THEN RAISE EXCEPTION 'mc018 injected creation failure'; END IF; RETURN NEW; END $$");
    await getOwnerPool().query("CREATE TRIGGER mc018_fail_create BEFORE INSERT ON biz_events FOR EACH ROW EXECUTE FUNCTION mc018_fail_create()");
    try {
      expect((await ticket(edge, key)).status).toBe(500);
      expect((await getOwnerPool().query("SELECT id FROM c_tickets WHERE workspace_id=$1 AND idempotency_key=$2", [edge.workspaceId, key])).rowCount).toBe(0);
      expect((await getOwnerPool().query("SELECT count(*)::int AS n FROM c_ticket_events WHERE workspace_id=$1", [edge.workspaceId])).rows[0].n).toBe(before);
    } finally { await getOwnerPool().query("DROP TRIGGER mc018_fail_create ON biz_events"); await getOwnerPool().query("DROP FUNCTION mc018_fail_create()"); }
  });
  it("records rating and audit event atomically and returns their actual receipt", async () => {
    const id = await completedTicket(other);
    const result = await request<TicketBody>(`/tickets/${id}/rate`, { score: 5, comment: "完成，含 '引号'" }, other.token); expect(result.status).toBe(200);
    const events = await eventsFor(other, id, "service.ticket.rate"); expect(events).toHaveLength(1);
    expect(result.body.receipt).toMatchObject({ state: "recorded", resourceId: id, eventId: events[0]!.id });
    expect((await request(`/tickets/${id}/rate`, { score: 4 }, other.token)).status).toBe(409);
  });
  it.each([0, 6, 1.5, "5", null])("rejects invalid rating %s", async (score) => {
    const id = await completedTicket(other);
    expect((await request(`/tickets/${id}/rate`, { score }, other.token)).status).toBe(400);
  });
  it("rolls back rating and local timeline if the immutable event append fails", async () => {
    const id = await completedTicket(other);
    await getOwnerPool().query("CREATE OR REPLACE FUNCTION mc018_fail_rating() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.payload->'decision'->>'action' = 'service.ticket.rate' THEN RAISE EXCEPTION 'mc018 injected event failure'; END IF; RETURN NEW; END $$");
    await getOwnerPool().query("CREATE TRIGGER mc018_fail_rating BEFORE INSERT ON biz_events FOR EACH ROW EXECUTE FUNCTION mc018_fail_rating()");
    try {
      expect((await request(`/tickets/${id}/rate`, { score: 5 }, other.token)).status).toBe(500);
      expect((await getOwnerPool().query("SELECT payload->'rating' AS rating FROM c_tickets WHERE id=$1", [id])).rows[0].rating).toBeNull();
      expect((await getOwnerPool().query("SELECT id FROM c_ticket_events WHERE ticket_id=$1 AND action='rate'", [id])).rowCount).toBe(0);
    } finally { await getOwnerPool().query("DROP TRIGGER mc018_fail_rating ON biz_events"); await getOwnerPool().query("DROP FUNCTION mc018_fail_rating()"); }
  });
});
