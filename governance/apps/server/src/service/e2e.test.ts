/**
 * server · AI 服务前台端到端套件（D 业务查询 / E 渠道与安全 / F C 端旅程 / G B 端视角）
 * 实测口径：RUN_DB_TESTS=1 + SERVICE_TEST_FIXTURES=owned 时创建独立 trading 工作区，
 * beforeAll 拉起自有真实服务（SERVER_E2E_PORT 缺省 8795），afterAll 精确清理本轮夹具；
 * 全部断言经 HTTP（fetch）命中活库；B 端走 tRPC serviceRouter（auth.loginAs 签 JWT）。
 * Tiger 未声明业务适配器：旧酒店正例透明迁移为 unavailable/隔离断言，不声称酒店能力通过。
 * 禁止读取 .env、默认数据库回退、继承真实模型或渠道凭据；原 30s/60s/90s 门限保持。
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { createServer } from "node:net";
import { SignJWT } from "jose";
import pg from "pg";

const RUN_DB = process.env.RUN_DB_TESTS === "1";
const describeDb = RUN_DB ? describe.sequential : describe.skip;
const DB_KEYS = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const;
if (RUN_DB) {
  if (process.env.SERVICE_TEST_FIXTURES !== "owned") {
    throw new Error("MC193 e2e: RUN_DB_TESTS=1 requires SERVICE_TEST_FIXTURES=owned");
  }
  const databases = DB_KEYS.map((key) => {
    const value = process.env[key];
    if (!value?.trim()) throw new Error(`MC193 e2e: RUN_DB_TESTS=1 requires ${key}`);
    try {
      const url = new URL(value);
      if (!["postgres:", "postgresql:"].includes(url.protocol)
        || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
        || url.pathname.length <= 1) throw new Error("invalid fixture database");
      return `${url.hostname}:${url.port || "5432"}${url.pathname}`;
    } catch {
      throw new Error(`MC193 e2e: ${key} must select an explicit loopback fixture database`);
    }
  });
  if (new Set(databases).size !== 1) throw new Error("MC193 e2e: all database roles must select the same fixture database");
}
const PORT = Number(process.env.SERVER_E2E_PORT ?? 8795);
if (!Number.isInteger(PORT) || PORT < 1024 || PORT > 65535) throw new Error("MC193 e2e: SERVER_E2E_PORT must be an integer in 1024..65535");
const BASE = `http://127.0.0.1:${PORT}`;
const ROOT = fileURLToPath(new URL("../../../../", import.meta.url));
const RUN = `mc193-e2e-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;
const TENANT_ID = `tenant-${RUN}`;
const WORKSPACE_ID = `ws-${RUN}`;
const WORKSPACE_SLUG = RUN;
const INSTALL_ID = `bi-${RUN}`;
const COLLECTION_ID = `kbc-${RUN}`;
const DOCUMENT_ID = `kbd-${RUN}`;
const COLLECTION_NAME = `${RUN}-模拟服务知识库`;
const OWNER_MEMBER_NO = `MEM-OWNER-${RUN}`;
const STAFF_MEMBER_NO = `MEM-STAFF-${RUN}`;
const READONLY_MEMBER_NO = `MEM-READONLY-${RUN}`;
const INSTANCE_ID = randomUUID();
const DEV_C_SECRET = "mc193-e2e-fixture-only-c-secret-00000000";
const DB_URL = process.env.DATABASE_URL;
const KB_ITEMS = [
  { heading: "决策日报几点发", content: "决策日报几点发？本轮夹具的示例日报在盘前生成；这是虚构验收材料，不代表真实运营时间。" },
  { heading: "系统什么情况下会开仓", content: "系统什么情况下会开仓？MRS_TEST_ONLY 是本轮虚构验收术语，所有操作仅模拟，本夹具不授权交易。" },
  { heading: "开仓的硬逻辑是什么", content: "开仓的硬逻辑是什么？MRS_TEST_ONLY 仅用于检索断言，本轮不设生产阈值，不发起真实开仓。" },
  { heading: "Wi-Fi 密码是多少", content: "Wi-Fi 密码是多少？本轮示例口令为 EXAMPLE_NETWORK_193，仅为虚构网络知识，不对应任何实际账号或网络。" },
] as const;

let server: ChildProcess;
let db: pg.Client;
let bToken = "";   // 本轮独立 owner
let roToken = "";  // 本轮独立 readonly
let fixtureCommitted = false;
let fixtureConnectionFailed = false;
let spawnFailed = false;
const childDiagnostics = { stdoutBytes: 0, stderrBytes: 0, categories: new Set<string>() };

/* ---------------- 基础工具 ---------------- */

async function cReq(path: string, init: RequestInit = {}, token?: string, ip = "203.0.113.1"): Promise<Response> {
  return fetch(`${BASE}/c${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "x-forwarded-for": ip,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
}

async function cSession(openid: string, channel = "h5", ip = "203.0.113.1"): Promise<{ token: string; user: { id: string; memberId: string | null } }> {
  const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel, openid, nickname: "e2e" }) }, undefined, ip);
  expect(res.status).toBe(200);
  return (await res.json()) as { token: string; user: { id: string; memberId: string | null } };
}

async function chat(token: string, text: string, extra: Record<string, unknown> = {}, ip?: string): Promise<Record<string, unknown>> {
  const res = await cReq("/chat", { method: "POST", body: JSON.stringify({ text, ...extra }) }, token, ip);
  expect(res.status).toBe(200);
  return (await res.json()) as Record<string, unknown>;
}

interface TrpcResult { status: number; data: unknown; error: { code: number; data?: { code?: string; httpStatus?: number } } | null }

async function trpc(proc: string, opts: { input?: unknown; token?: string; method?: "query" | "mutation" } = {}): Promise<TrpcResult> {
  const method = opts.method ?? "query";
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  let res: Response;
  if (method === "query") {
    const qs = opts.input !== undefined ? `?input=${encodeURIComponent(JSON.stringify(opts.input))}` : "";
    res = await fetch(`${BASE}/trpc/${proc}${qs}`, { headers });
  } else {
    res = await fetch(`${BASE}/trpc/${proc}`, { method: "POST", headers, body: JSON.stringify(opts.input ?? {}) });
  }
  const body = (await res.json()) as { result?: { data?: unknown }; error?: TrpcResult["error"] };
  return { status: res.status, data: body.result?.data ?? null, error: body.error ?? null };
}

async function loginAs(memberNo: string): Promise<string> {
  const r = await trpc("auth.loginAs", { input: { workspaceSlug: WORKSPACE_SLUG, memberNo }, method: "mutation" });
  expect(r.error).toBeNull();
  return (r.data as { token: string }).token;
}

async function bindMember(cUserId: string, memberId: string): Promise<void> {
  const result = await db.query(`UPDATE c_users SET member_id=$3 WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, cUserId, memberId]);
  if (result.rowCount !== 1) throw new Error("MC193 synthetic subject binding did not select the owned C user");
}

async function signCToken(over: Record<string, unknown>, secret = DEV_C_SECRET, exp = "1h"): Promise<string> {
  return new SignJWT({ workspaceId: WORKSPACE_ID, cUserId: `${RUN}-unknown-user`, channel: "h5", scope: "c-user", ...over })
    .setProtectedHeader({ alg: "HS256" }).setIssuedAt().setIssuer("workloom-c").setExpirationTime(exp)
    .sign(new TextEncoder().encode(secret));
}

/* ---------------- 服务拉起/回收 ---------------- */

async function prepareFixture(): Promise<void> {
  db = new pg.Client({ connectionString: DB_URL!, connectionTimeoutMillis: 5_000, statement_timeout: 30_000 });
  db.on("error", () => { fixtureConnectionFailed = true; });
  try {
    await db.connect();
    await db.query("BEGIN");
    await db.query(`INSERT INTO tenants (id,name,plan) VALUES ($1,$2,'pro')`, [TENANT_ID, RUN]);
    await db.query(
      `INSERT INTO workspaces (id,tenant_id,name,slug,industry,bundle_id,is_example) VALUES ($1,$2,$3,$3,'trading','trading',false)`,
      [WORKSPACE_ID, TENANT_ID, WORKSPACE_SLUG],
    );
    for (const [memberNo, role] of [[OWNER_MEMBER_NO, "owner"], [STAFF_MEMBER_NO, "staff"], [READONLY_MEMBER_NO, "readonly"]] as const) {
      await db.query(
        `INSERT INTO members (id,workspace_id,member_no,name,role,permissions,status) VALUES ($1,$2,$3,$4,$5,'{}','active')`,
        [`member-${memberNo}`, WORKSPACE_ID, memberNo, `${RUN}-${role}`, role],
      );
    }
    await db.query(
      `INSERT INTO bundle_installs (id,workspace_id,bundle_id,assets,status) VALUES ($1,$2,'trading',$3,'active')`,
      [INSTALL_ID, WORKSPACE_ID, JSON.stringify({ fixture: "MC193", run: RUN, nativeInstallation: false })],
    );
    await db.query(`INSERT INTO kb_collections (id,workspace_id,name) VALUES ($1,$2,$3)`, [COLLECTION_ID, WORKSPACE_ID, COLLECTION_NAME]);
    const content = KB_ITEMS.map((item) => `## ${item.heading}\n\n${item.content}`).join("\n\n");
    await db.query(
      `INSERT INTO kb_documents (id,workspace_id,collection_id,title,source_kind,status,content_md,hash) VALUES ($1,$2,$3,$4,'manual','active',$5,$6)`,
      [DOCUMENT_ID, WORKSPACE_ID, COLLECTION_ID, `${RUN}-模拟服务须知`, content, createHash("sha256").update(content).digest("hex")],
    );
    for (const [index, item] of KB_ITEMS.entries()) {
      await db.query(
        `INSERT INTO kb_chunks (workspace_id,document_id,chunk_index,heading,content) VALUES ($1,$2,$3,$4,$5)`,
        [WORKSPACE_ID, DOCUMENT_ID, index, item.heading, item.content],
      );
    }
    await db.query("COMMIT");
    fixtureCommitted = true;
  } catch {
    try { await db.query("ROLLBACK"); } catch { throw new Error("MC193 e2e fixture preparation and rollback failed"); }
    throw new Error("MC193 e2e fixture preparation failed");
  }
}

async function cleanFixture(): Promise<void> {
  if (!fixtureCommitted) return;
  if (fixtureConnectionFailed) throw new Error("MC193 e2e fixture connection failed before cleanup");
  try {
    await db.query("BEGIN");
    const owned = await db.query(`SELECT 1 FROM workspaces WHERE id=$1 AND tenant_id=$2 AND name=$3`, [WORKSPACE_ID, TENANT_ID, RUN]);
    if (owned.rowCount !== 1) throw new Error("fixture ownership mismatch");
    for (const table of ["c_notifications", "c_ticket_events", "c_tickets", "c_messages", "c_conversations", "c_users", "kb_chunks", "kb_documents", "kb_sources", "kb_collections", "members"] as const) {
      await db.query(`DELETE FROM ${table} WHERE workspace_id=$1`, [WORKSPACE_ID]);
    }
    await db.query(`DELETE FROM bundle_installs WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, INSTALL_ID]);
    // biz_events/approvals 永不删除；保留本轮 tenant/workspace 作为审计身份锚点。
    await db.query("COMMIT");
  } catch {
    try { await db.query("ROLLBACK"); } catch { throw new Error("MC193 e2e fixture cleanup and rollback failed"); }
    throw new Error("MC193 e2e fixture cleanup failed");
  }
}

async function requireFreePort(): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const probe = createServer();
    probe.once("error", () => reject(new Error("MC193 e2e server port is already occupied or unavailable")));
    probe.listen(PORT, "127.0.0.1", () => {
      probe.close((error) => error ? reject(new Error("MC193 e2e port probe close failed")) : resolve());
    });
  });
}

function observeChildOutput(stream: "stdout" | "stderr", chunk: Buffer): void {
  childDiagnostics[stream === "stdout" ? "stdoutBytes" : "stderrBytes"] += chunk.byteLength;
  const text = chunk.toString("utf8");
  if (/ERR_MODULE_NOT_FOUND|Cannot find module/.test(text)) childDiagnostics.categories.add("module_missing");
  if (/EADDRINUSE/.test(text)) childDiagnostics.categories.add("port_occupied");
  if (/ECONNREFUSED|database.*does not exist|password authentication failed/i.test(text)) childDiagnostics.categories.add("database_unavailable");
  // 原始字节只在本回调临时分类，永不保存/打印子进程输出、数据库 URL 或上游错误正文。
}

function startupFailure(category: string): Error {
  const observed = [...childDiagnostics.categories].sort().join(",") || "none";
  return new Error(`MC193 e2e startup failed category=${category} observed=${observed} stdoutBytes=${childDiagnostics.stdoutBytes} stderrBytes=${childDiagnostics.stderrBytes}`);
}

beforeAll(async () => {
  if (!RUN_DB) return;
  await requireFreePort();
  await prepareFixture();
  const childEnv: NodeJS.ProcessEnv = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    TMPDIR: process.env.TMPDIR,
    DATABASE_URL: process.env.DATABASE_URL,
    DATABASE_APP_URL: process.env.DATABASE_APP_URL,
    DATABASE_GATEWAY_URL: process.env.DATABASE_GATEWAY_URL,
    NODE_ENV: "test",
    SERVER_HOST: "127.0.0.1",
    SERVER_PORT: String(PORT),
    SERVER_INSTANCE_ID: INSTANCE_ID,
    SERVICE_C_DEMO_AUTH: "true",
    SERVICE_C_SECRET: DEV_C_SECRET,
    SERVICE_C_WORKSPACE_ID: WORKSPACE_ID,
    JWT_SECRET: "mc193-e2e-fixture-only-member-secret-00000",
    JWT_ISSUER: "mc193-tiger-service-tests",
    LLM_PROVIDER: "mock",
    WORKLOOM_SCHEDULER_MS: "0",
    BUNDLES_ROOT: fileURLToPath(new URL("../../../../bundles", import.meta.url)),
  };
  try {
    server = spawn(process.execPath, ["node_modules/tsx/dist/cli.mjs", "apps/server/src/index.ts"], {
      cwd: ROOT, env: childEnv, stdio: ["ignore", "pipe", "pipe"], detached: true,
    });
  } catch { throw startupFailure("spawn_failed"); }
  server.on("error", () => { spawnFailed = true; });
  server.stdout?.on("data", (chunk: Buffer) => observeChildOutput("stdout", chunk));
  server.stderr?.on("data", (chunk: Buffer) => observeChildOutput("stderr", chunk));
  const deadline = Date.now() + 60_000;
  for (;;) {
    if (spawnFailed || !server.pid) throw startupFailure("spawn_failed");
    if (server.exitCode !== null || server.signalCode !== null) throw startupFailure("child_exited");
    let response: Response | undefined;
    try {
      response = await fetch(`${BASE}/health`, { redirect: "error", signal: AbortSignal.timeout(1_000) });
    } catch {
      childDiagnostics.categories.add("health_not_ready");
    }
    if (response) {
      if (!response.ok) throw startupFailure("health_status_invalid");
      let body: { ok?: unknown; service?: unknown; instanceId?: unknown };
      try { body = await response.json() as typeof body; } catch { throw startupFailure("health_body_invalid"); }
      if (body.ok !== true || body.service !== "workloom-im-server" || body.instanceId !== INSTANCE_ID) throw startupFailure("health_identity_mismatch");
      if (server.exitCode !== null || server.signalCode !== null) throw startupFailure("child_exited_after_health");
      break;
    }
    if (Date.now() > deadline) throw startupFailure("readiness_timeout");
    await new Promise((r) => setTimeout(r, 500));
  }
  bToken = await loginAs(OWNER_MEMBER_NO);
  roToken = await loginAs(READONLY_MEMBER_NO);
}, 90_000);

afterAll(async () => {
  if (!RUN_DB) return;
  const failures: string[] = [];
  if (server?.pid) {
    try { process.kill(-server.pid, "SIGKILL"); } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") failures.push("owned_server_stop_failed");
    }
    if (server.exitCode === null && server.signalCode === null) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => { failures.push("owned_server_exit_timeout"); resolve(); }, 5_000);
        server.once("exit", () => { clearTimeout(timer); resolve(); });
      });
    }
  }
  try { await cleanFixture(); } catch { failures.push("fixture_cleanup_failed"); }
  try { await db?.end(); } catch { failures.push("fixture_connection_close_failed"); }
  if (failures.length) throw new Error(`MC193 e2e cleanup failed: ${failures.join(",")}`);
});

/* ================= E. 渠道与安全 ================= */

describeDb("E 渠道 · session 签发", () => {
  // MC193-map: MC193-e2e-001
  it("h5 演示直登 → {token, user}", async () => {
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "h5", openid: `${RUN}-e1` }) }, undefined, "203.0.113.11");
    expect(res.status).toBe(200);
    const j = (await res.json()) as { token: string; user: Record<string, unknown> };
    expect(typeof j.token).toBe("string");
    expect(j.user).toMatchObject({ channel: "h5", openid: `${RUN}-e1` });
  });

  // MC193-map: MC193-e2e-002
  it("wechat-mini / alipay 开发态 openid 直登放行", async () => {
    for (const ch of ["wechat-mini", "alipay"]) {
      const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: ch, openid: `${RUN}-${ch}` }) }, undefined, "203.0.113.12");
      expect(res.status, ch).toBe(200);
      const j = (await res.json()) as { user: { channel: string } };
      expect(j.user.channel).toBe(ch);
    }
  });

  // MC193-map: MC193-e2e-003
  it("非法渠道 → 400", async () => {
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "tiktok", openid: "x" }) }, undefined, "203.0.113.13");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-004
  it("h5 缺 openid → 400", async () => {
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "h5" }) }, undefined, "203.0.113.14");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-005
  it("wechat-mini 无 openid 且缺 code → 400；有 code 无凭据 → 503 渠道未配置", async () => {
    const ip = "203.0.113.15";
    const r1 = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "wechat-mini" }) }, undefined, ip);
    expect(r1.status).toBe(400);
    const r2 = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "wechat-mini", code: "abc" }) }, undefined, ip);
    expect(r2.status).toBe(503);
    expect(((await r2.json()) as { error: string }).error).toContain("渠道未配置");
  });

  // MC193-map: MC193-e2e-006
  it("alipay code 换登未装配 → 503", async () => {
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "alipay", code: "abc" }) }, undefined, "203.0.113.16");
    expect(res.status).toBe(503);
  });
});

describeDb("E 安全 · 限流", () => {
  // MC193-map: MC193-e2e-007
  it("session 同 IP+channel 第 61 次 → 429", async () => {
    const ip = "198.51.100.61";
    let last = 0;
    for (let i = 0; i < 62; i++) {
      const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "h5", openid: `${RUN}-rl` }) }, undefined, ip);
      last = res.status;
      if (last === 429) break;
    }
    expect(last).toBe(429);
  });

  // MC193-map: MC193-e2e-008
  it("限流按 IP+channel 维度隔离（同 IP 换 channel 仍放行）", async () => {
    const ip = "198.51.100.61"; // 同上一用例 IP：h5 已限流
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "alipay", openid: `${RUN}-rl2` }) }, undefined, ip);
    expect(res.status).toBe(200);
  });

  // MC193-map: MC193-e2e-009
  it("限流按 IP 隔离（换 IP 仍放行）", async () => {
    const res = await cReq("/session", { method: "POST", body: JSON.stringify({ channel: "h5", openid: `${RUN}-rl3` }) }, undefined, "198.51.100.62");
    expect(res.status).toBe(200);
  });

  // MC193-map: MC193-e2e-010
  it("chat 同用户第 61 次 → 429", async () => {
    const { token } = await cSession(`${RUN}-crl`, "h5", "198.51.100.63");
    let last = 0;
    for (let i = 0; i < 62; i++) {
      const res = await cReq("/chat", { method: "POST", body: JSON.stringify({ text: "日报几点发" }) }, token, "198.51.100.63");
      last = res.status;
      if (last === 429) break;
    }
    expect(last).toBe(429);
  });
});

describeDb("E 安全 · token 校验", () => {
  // MC193-map: MC193-e2e-011
  it("缺 Authorization 头 → 401", async () => {
    const res = await cReq("/tickets", {}, undefined, "203.0.113.21");
    expect(res.status).toBe(401);
  });

  // MC193-map: MC193-e2e-012
  it("伪造签名（错误密钥）→ 401", async () => {
    const forged = await signCToken({}, "wrong-secret-wrong-secret-wrong!!");
    const res = await cReq("/tickets", {}, forged, "203.0.113.22");
    expect(res.status).toBe(401);
  });

  // MC193-map: MC193-e2e-013
  it("过期 token → 401", async () => {
    const expired = await signCToken({}, DEV_C_SECRET, "-1s");
    const res = await cReq("/tickets", {}, expired, "203.0.113.23");
    expect(res.status).toBe(401);
  });

  // MC193-map: MC193-e2e-014
  it("scope 非 c-user（B 端令牌混用）→ 401", async () => {
    const bad = await signCToken({ scope: "b" });
    const res = await cReq("/tickets", {}, bad, "203.0.113.24");
    expect(res.status).toBe(401);
  });

  // MC193-map: MC193-e2e-015
  it("畸形 token 串 → 401", async () => {
    const res = await cReq("/tickets", {}, "not-a-jwt", "203.0.113.25");
    expect(res.status).toBe(401);
  });
});

describeDb("E 安全 · 越权与输入约束", () => {
  // MC193-map: MC193-e2e-016
  it("越权读他人工单详情 → 404", async () => {
    const a = await cSession(`${RUN}-oa`, "h5", "203.0.113.31");
    const b = await cSession(`${RUN}-ob`, "h5", "203.0.113.32");
    const created = (await (await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "repair", title: "越权测试单", payload: {}, idempotencyKey: `${RUN}-oa-1` }),
    }, a.token, "203.0.113.31")).json()) as { ticket: { id: string } };
    const res = await cReq(`/tickets/${created.ticket.id}`, {}, b.token, "203.0.113.32");
    expect(res.status).toBe(404);
  });

  // MC193-map: MC193-e2e-017
  it("越权读他人通知：通知箱仅含本人条目", async () => {
    const a = await cSession(`${RUN}-na`, "h5", "203.0.113.33");
    const b = await cSession(`${RUN}-nb`, "h5", "203.0.113.34");
    const created = (await (await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "other", title: "通知隔离单", payload: {}, idempotencyKey: `${RUN}-na-1` }),
    }, a.token, "203.0.113.33")).json()) as { ticket: { id: string } };
    const nA = (await (await cReq("/notifications", {}, a.token, "203.0.113.33")).json()) as { notifications: Array<{ payload: { ticketId?: string } }> };
    expect(nA.notifications.some((n) => n.payload.ticketId === created.ticket.id)).toBe(true);
    const nB = (await (await cReq("/notifications", {}, b.token, "203.0.113.34")).json()) as { notifications: Array<{ payload: { ticketId?: string } }> };
    expect(nB.notifications.some((n) => n.payload.ticketId === created.ticket.id)).toBe(false);
  });

  // MC193-map: MC193-e2e-018
  it("越权续聊他人会话 → 不复用（归属校验后新建会话）", async () => {
    const a = await cSession(`${RUN}-ca`, "h5", "203.0.113.35");
    const b = await cSession(`${RUN}-cb`, "h5", "203.0.113.36");
    const first = await chat(a.token, "日报几点发", {}, "203.0.113.35");
    const hijack = await chat(b.token, "净值哪里看", { conversationId: first.conversationId }, "203.0.113.36");
    expect(hijack.conversationId).not.toBe(first.conversationId);
  });

  // MC193-map: MC193-e2e-019
  it("text 超 2000 字符 → 400", async () => {
    const { token } = await cSession(`${RUN}-v1`, "h5", "203.0.113.41");
    const res = await cReq("/chat", { method: "POST", body: JSON.stringify({ text: "长".repeat(2001) }) }, token, "203.0.113.41");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-020
  it("空 text / 缺 text → 400", async () => {
    const { token } = await cSession(`${RUN}-v2`, "h5", "203.0.113.42");
    const r1 = await cReq("/chat", { method: "POST", body: JSON.stringify({ text: "   " }) }, token, "203.0.113.42");
    expect(r1.status).toBe(400);
    const r2 = await cReq("/chat", { method: "POST", body: JSON.stringify({}) }, token, "203.0.113.42");
    expect(r2.status).toBe(400);
  });

  // MC193-map: MC193-e2e-021
  it("非法 kind → 400", async () => {
    const { token } = await cSession(`${RUN}-v3`, "h5", "203.0.113.43");
    const res = await cReq("/tickets", { method: "POST", body: JSON.stringify({ kind: "hack", title: "x", payload: {} }) }, token, "203.0.113.43");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-022
  it("title 超 120 字符 → 400", async () => {
    const { token } = await cSession(`${RUN}-v4`, "h5", "203.0.113.44");
    const res = await cReq("/tickets", { method: "POST", body: JSON.stringify({ kind: "other", title: "长".repeat(121), payload: {} }) }, token, "203.0.113.44");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-023
  it("payload 超 10KB → 400", async () => {
    const { token } = await cSession(`${RUN}-v5`, "h5", "203.0.113.45");
    const res = await cReq("/tickets", { method: "POST", body: JSON.stringify({ kind: "other", title: "大 payload", payload: { blob: "x".repeat(11 * 1024) } }) }, token, "203.0.113.45");
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-024
  it("XSS 文本原样存储不执行（标题含 script 标签原样返回）", async () => {
    const { token } = await cSession(`${RUN}-v6`, "h5", "203.0.113.46");
    const xss = "<script>alert(1)</script>";
    const r = (await (await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "other", title: xss, payload: {}, idempotencyKey: `${RUN}-xss-1` }),
    }, token, "203.0.113.46")).json()) as { ticket: { id: string; title: string } };
    expect(r.ticket.title).toBe(xss);
    const detail = (await (await cReq(`/tickets/${r.ticket.id}`, {}, token, "203.0.113.46")).json()) as { ticket: { title: string } };
    expect(detail.ticket.title).toBe(xss);
  });

  // MC193-map: MC193-e2e-025
  it("评价 score 越界（0 / 6）→ 400", async () => {
    const { token } = await cSession(`${RUN}-v7`, "h5", "203.0.113.47");
    for (const score of [0, 6]) {
      const res = await cReq("/tickets/tck-x/rate", { method: "POST", body: JSON.stringify({ score }) }, token, "203.0.113.47");
      expect(res.status, String(score)).toBe(400);
    }
  });

  // MC193-map: MC193-e2e-026
  it("非法 JSON body → 按缺参 400（不 500）", async () => {
    const { token } = await cSession(`${RUN}-v8`, "h5", "203.0.113.48");
    const res = await fetch(`${BASE}/c/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, "x-forwarded-for": "203.0.113.48" },
      body: "{not json",
    });
    expect(res.status).toBe(400);
  });

  // MC193-map: MC193-e2e-027
  it("不存在工单详情 → 404 带 requestId", async () => {
    const { token } = await cSession(`${RUN}-v9`, "h5", "203.0.113.49");
    const res = await cReq("/tickets/tck-not-exist", {}, token, "203.0.113.49");
    expect(res.status).toBe(404);
    expect(typeof ((await res.json()) as { requestId?: string }).requestId).toBe("string");
  });
});

/* ================= D. 业务查询 ================= */

describeDb("D 业务查询 · Tiger 未声明适配器时的 unavailable 与业务隔离", () => {
  // MC193-new: e2e-identity-unavailable
  it("身份绑定端点返回 503 IDENTITY_ADAPTER_UNAVAILABLE，合法格式请求不写身份", async () => {
    const session = await cSession(`${RUN}-d-identity`, "h5", "203.0.113.150");
    for (const path of ["/identity/code", "/identity/bind"]) {
      const response = await cReq(path, {
        method: "POST", body: JSON.stringify({ phone: "19900000193", code: "123456" }),
      }, session.token, "203.0.113.150");
      expect(response.status).toBe(503);
      const body = await response.json() as { code?: string; requestId?: string };
      expect(body.code).toBe("IDENTITY_ADAPTER_UNAVAILABLE");
      expect(typeof body.requestId).toBe("string");
    }
    const user = await db.query<{ member_id: string | null; phone_hash: string | null }>(
      `SELECT member_id,phone_hash FROM c_users WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, session.user.id],
    );
    expect(user.rows).toEqual([{ member_id: null, phone_hash: null }]);
  });

  // MC193-new: e2e-identity-invalid-phone
  it("身份绑定仍先执行手机号输入边界：非法格式返回 400 INVALID_PHONE", async () => {
    const { token } = await cSession(`${RUN}-d-invalid-phone`, "h5", "203.0.113.151");
    for (const path of ["/identity/code", "/identity/bind"]) {
      const response = await cReq(path, { method: "POST", body: JSON.stringify({ phone: "invalid", code: "123456" }) }, token, "203.0.113.151");
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ code: "INVALID_PHONE" });
    }
  });

  // MC193-map: MC193-e2e-028
  it("未绑定查订单：未声明适配器返回空集 + unavailable，不编造绑定引导", async () => {
    const { token } = await cSession(`${RUN}-d1`, "h5", "203.0.113.51");
    const j = (await (await cReq("/orders", {}, token, "203.0.113.51")).json()) as Record<string, unknown>;
    expect(j).toMatchObject({ available: false, demo: false });
    expect(j.orders).toEqual([]);
    expect(j).not.toHaveProperty("hint");
    expect(j).not.toHaveProperty("bindRequired");
  });

  // MC193-map: MC193-e2e-029
  it("未绑定查权益：/member 明示不可用，不返回酒店游客等级或积分", async () => {
    const { token } = await cSession(`${RUN}-d2`, "h5", "203.0.113.52");
    const j = (await (await cReq("/member", {}, token, "203.0.113.52")).json()) as Record<string, unknown>;
    expect(j).toMatchObject({ title: "权益信息不可用", available: false, demo: false });
    expect(j.benefits).toEqual([]);
    for (const field of ["level", "points", "bindRequired"]) expect(j).not.toHaveProperty(field);
  });

  // MC193-map: MC193-e2e-030
  it("遗留 subject 绑定后 /orders 仍不可用，不投影酒店订单私有字段", async () => {
    const s = await cSession(`${RUN}-d3`, "h5", "203.0.113.53");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const j = (await (await cReq("/orders", {}, s.token, "203.0.113.53")).json()) as { orders: Array<Record<string, unknown>>; demo: boolean; available: boolean; bindRequired?: boolean };
    expect(j.bindRequired).toBeUndefined();
    expect(j.orders.length).toBe(0);
    expect(j.available).toBe(false);
    for (const k of ["id", "title", "status", "checkIn", "roomType", "amount"]) expect(j, k).not.toHaveProperty(k);
  });

  // MC193-map: MC193-e2e-031
  it("未启用业务适配器时不伪造金额（原酒店金额换算正例不适用于 Tiger）", async () => {
    const s = await cSession(`${RUN}-d4`, "h5", "203.0.113.54");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const j = (await (await cReq("/orders", {}, s.token, "203.0.113.54")).json()) as { orders: Array<{ id: string; amount: number }> };
    expect(JSON.stringify(j)).not.toMatch(/"amount"|"amountFen"|"priceYuan"/);
    expect(j.orders).toEqual([]);
  });

  // MC193-map: MC193-e2e-032
  it("遗留 subject 绑定后 /member 仍 unavailable，不伪造等级积分权益", async () => {
    const s = await cSession(`${RUN}-d5`, "h5", "203.0.113.55");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const j = (await (await cReq("/member", {}, s.token, "203.0.113.55")).json()) as Record<string, unknown>;
    expect(j).toMatchObject({ title: "权益信息不可用", demo: false, available: false });
    expect(j.benefits).toEqual([]);
    for (const field of ["level", "points"]) expect(j).not.toHaveProperty(field);
  });

  // MC193-map: MC193-e2e-033
  it("不同 subject 均不可用且为空，禁用态不会导出其他行业订单", async () => {
    const s = await cSession(`${RUN}-d6`, "h5", "203.0.113.56");
    await bindMember(s.user.id, `${RUN}-subject-b`);
    const a = await cSession(`${RUN}-d6-other`, "h5", "203.0.113.156");
    await bindMember(a.user.id, `${RUN}-subject-a`);
    const j = (await (await cReq("/orders", {}, s.token, "203.0.113.56")).json()) as { orders: unknown[]; available: boolean };
    expect(j.orders.length).toBe(0);
    const other = (await (await cReq("/orders", {}, a.token, "203.0.113.156")).json()) as { orders: unknown[]; available: boolean };
    expect(other.orders).toEqual([]);
    expect(j.available).toBe(false);
    expect(other.available).toBe(false);
  });

  // MC193-map: MC193-e2e-034
  it("chat 查订单（遗留绑定）→ 知识库兜底，不生成行业 order 卡片", async () => {
    const s = await cSession(`${RUN}-d7`, "h5", "203.0.113.57");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const r = await chat(s.token, "帮我查一下我的订单", {}, "203.0.113.57");
    expect(r.intent).toBe("kb_qa");
    const cards = r.cards as Array<{ kind: string; data: Record<string, unknown> }>;
    expect(cards.length).toBe(0);
    expect(cards.some((card) => card.kind === "order")).toBe(false);
    expect(JSON.stringify(r)).not.toContain('"checkIn"');
  });

  // MC193-map: MC193-e2e-035
  it("chat 查会员（遗留绑定）→ 不生成 member 卡片或酒店积分权益", async () => {
    const s = await cSession(`${RUN}-d8`, "h5", "203.0.113.58");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const r = await chat(s.token, "我的会员积分还有多少", {}, "203.0.113.58");
    const cards = r.cards as Array<{ kind: string; data: Record<string, unknown> }>;
    expect(cards).toEqual([]);
    expect(JSON.stringify(r)).not.toMatch(/"level"|"points"/);
    expect(JSON.stringify(r)).not.toContain('"benefits"');
  });

  // MC193-map: MC193-e2e-036
  it("酒店房价问句只走知识库兜底，不串入 catalog/sku/priceYuan", async () => {
    const { token } = await cSession(`${RUN}-d9`, "h5", "203.0.113.59");
    const r = await chat(token, "豪华大床房多少钱一晚", {}, "203.0.113.59");
    const cards = r.cards as Array<{ kind: string; data: { items: Array<Record<string, unknown>> } }>;
    expect(r.intent).toBe("kb_qa");
    expect(cards).toEqual([]);
    expect(JSON.stringify(r)).not.toContain('"sku"');
    expect(JSON.stringify(r)).not.toContain('"priceYuan"');
  });

  // MC193-map: MC193-e2e-037
  it("chat 未绑定查订单 → 诚实知识拒答且不出卡，不编造酒店绑定入口", async () => {
    const { token } = await cSession(`${RUN}-d10`, "h5", "203.0.113.60");
    const r = await chat(token, "我的订单呢", {}, "203.0.113.60");
    expect(r.intent).toBe("kb_qa");
    expect(String(r.answer)).toContain("无法准确回答");
    expect(r.cards).toEqual([]);
  });

  // MC193-map: MC193-e2e-038
  it("chat 酒店账单问句（遗留绑定）→ 知识库兜底，无订单卡片", async () => {
    const s = await cSession(`${RUN}-d11`, "h5", "203.0.113.64");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const r = await chat(s.token, "我的账单和房费", {}, "203.0.113.64");
    expect(r.intent).toBe("kb_qa");
    expect(r.cards).toEqual([]);
  });

  // MC193-map: MC193-e2e-039
  it("chat 问工单进度 → query_ticket 应答", async () => {
    const { token } = await cSession(`${RUN}-d12`, "h5", "203.0.113.65");
    const created = await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "other", title: `${RUN}-进度查询`, payload: {}, idempotencyKey: `${RUN}-d12-1` }),
    }, token, "203.0.113.65");
    expect(created.status).toBe(200);
    const r = await chat(token, "我的工单进度怎么样了", {}, "203.0.113.65");
    expect(r.intent).toBe("biz_query");
    expect(String(r.answer)).toContain(`${RUN}-进度查询`);
    expect(String(r.answer)).toContain("已受理");
  });

  // MC193-map: MC193-e2e-040
  it("/orders 与 /member 不可用时 demo:false + available:false", async () => {
    const s = await cSession(`${RUN}-d13`, "h5", "203.0.113.66");
    await bindMember(s.user.id, `${RUN}-subject-a`);
    const o = (await (await cReq("/orders", {}, s.token, "203.0.113.66")).json()) as { demo: boolean; available: boolean };
    const m = (await (await cReq("/member", {}, s.token, "203.0.113.66")).json()) as { demo: boolean; available: boolean };
    expect(o.demo).toBe(false);
    expect(m.demo).toBe(false);
    expect(o.available).toBe(false);
    expect(m.available).toBe(false);
  });

  // MC193-map: MC193-e2e-041
  it("/tickets 列表仅本人单（工单查询隔离）", async () => {
    const a = await cSession(`${RUN}-d14a`, "h5", "203.0.113.67");
    const b = await cSession(`${RUN}-d14b`, "h5", "203.0.113.68");
    await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "other", title: "隔离列表单", payload: {}, idempotencyKey: `${RUN}-d14-1` }),
    }, a.token, "203.0.113.67");
    const listA = (await (await cReq("/tickets", {}, a.token, "203.0.113.67")).json()) as { tickets: Array<{ title: string }> };
    expect(listA.tickets.some((t) => t.title === "隔离列表单")).toBe(true);
    const listB = (await (await cReq("/tickets", {}, b.token, "203.0.113.68")).json()) as { tickets: Array<{ title: string }> };
    expect(listB.tickets.some((t) => t.title === "隔离列表单")).toBe(false);
  });
});

/* ================= F. C 端端到端旅程 ================= */

describeDb("F C 端旅程 · 首问到五星评价全链路", () => {
  let token = "";
  let cUserId = "";
  let convId = "";
  let ticketId = "";
  let deliveryDraft: { kind: string; title: string; payload: Record<string, unknown> };
  const ip = "203.0.113.101";

  // MC193-map: MC193-e2e-042
  it("F1 新用户首问「决策日报几点发」→ 命中 KB 带引用", async () => {
    const s = await cSession(`${RUN}-f`, "h5", ip);
    token = s.token;
    cUserId = s.user.id;
    const r = await chat(token, "决策日报几点发？", {}, ip);
    convId = String(r.conversationId);
    expect(r.intent).toBe("kb_qa");
    expect((r.citations as unknown[]).length).toBeGreaterThan(0);
    expect(String(r.answer)).toContain("盘前");
  });

  // MC193-map: MC193-e2e-043
  it("F2 同会话追问「系统什么情况下会开仓」→ conversationId 续聊且命中本轮模拟知识引用", async () => {
    const r = await chat(token, "系统什么情况下会开仓？", { conversationId: convId }, ip);
    expect(r.conversationId).toBe(convId);
    expect((r.citations as unknown[]).length).toBeGreaterThan(0);
    expect(String(r.answer)).toContain("MRS");
  });

  // MC193-map: MC193-e2e-044
  it("F3 查订单未绑定 → 无业务适配器时诚实知识拒答，不出卡", async () => {
    const r = await chat(token, "查一下我的订单", { conversationId: convId }, ip);
    expect(String(r.answer)).toContain("无法准确回答");
    expect(r.cards).toEqual([]);
  });

  // MC193-map: MC193-e2e-045
  it("F4 明确人工协助 → 通用 other 草稿（未确认不建单）；不猜测行业 delivery", async () => {
    const r = await chat(token, "需要协助，帮我开通研报速递订阅", { conversationId: convId }, ip);
    expect(r.intent).toBe("service_request");
    expect(r.ticketDraft).toMatchObject({ kind: "other" });
    expect(r.ticket).toBeNull();
    // delivery 是本轮用户显式选择，未声明行业适配器不会从“订阅”一词自动推断。
    const original = r.ticketDraft as { title: string; payload: Record<string, unknown> };
    deliveryDraft = { kind: "delivery", title: original.title, payload: original.payload };
  });

  // MC193-map: MC193-e2e-046
  it("F5 confirmTicket:true → 建单 assigned + 复盘组 + statusText 已受理", async () => {
    const r = await chat(token, "帮我开通研报速递订阅", {
      conversationId: convId, confirmTicket: true, ticketDraft: deliveryDraft, idempotencyKey: `${RUN}-f5`,
    }, ip);
    const t = r.ticket as { id: string; status: string; dept: string; statusText: string };
    ticketId = t.id;
    expect(t).toMatchObject({ status: "assigned", dept: "复盘组", statusText: "已受理" });
  });

  // MC193-map: MC193-e2e-047
  it("F6 受理通知：通知箱含 ticket.accepted", async () => {
    const n = (await (await cReq("/notifications", {}, token, ip)).json()) as { notifications: Array<{ kind: string; payload: { ticketId?: string } }> };
    expect(n.notifications.some((x) => x.kind === "ticket.accepted" && x.payload.ticketId === ticketId)).toBe(true);
  });

  // MC193-map: MC193-e2e-048
  it("F7 同幂等键重放 → deduped:true 同单号", async () => {
    const r = await chat(token, "帮我开通研报速递订阅", {
      conversationId: convId, confirmTicket: true, ticketDraft: deliveryDraft, idempotencyKey: `${RUN}-f5`,
    }, ip);
    expect(r.deduped).toBe(true);
    expect((r.ticket as { id: string }).id).toBe(ticketId);
  });

  // MC193-map: MC193-e2e-049
  it("F8 进度查询：详情时间线含 create/assign", async () => {
    const d = (await (await cReq(`/tickets/${ticketId}`, {}, token, ip)).json()) as { timeline: Array<{ action: string }> };
    const actions = d.timeline.map((e) => e.action);
    expect(actions).toContain("create");
    expect(actions).toContain("assign");
  });

  // MC193-map: MC193-e2e-050
  it("F9 B 端受理推进（start → processing）", async () => {
    const r = await trpc("service.tickets.advance", { input: { ticketId, action: "start" }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    expect((r.data as { ticket: { status: string } }).ticket.status).toBe("processing");
  });

  // MC193-map: MC193-e2e-051
  it("F10 B 端办结（complete → done + 结果回填）", async () => {
    const r = await trpc("service.tickets.complete", { input: { ticketId, result: "模拟研报速递订阅已处理" }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    const t = (r.data as { ticket: { status: string; result: { text: string } } }).ticket;
    expect(t.status).toBe("done");
    expect(t.result.text).toContain("研报速递");
  });

  // MC193-map: MC193-e2e-052
  it("F11 完成通知：通知箱含 ticket.completed", async () => {
    const n = (await (await cReq("/notifications", {}, token, ip)).json()) as { notifications: Array<{ kind: string; payload: { ticketId?: string } }> };
    expect(n.notifications.some((x) => x.kind === "ticket.completed" && x.payload.ticketId === ticketId)).toBe(true);
  });

  // MC193-map: MC193-e2e-053
  it("F12 进度再查：statusText=已完成", async () => {
    const d = (await (await cReq(`/tickets/${ticketId}`, {}, token, ip)).json()) as { ticket: { status: string; statusText: string } };
    expect(d.ticket).toMatchObject({ status: "done", statusText: "已完成" });
  });

  // MC193-map: MC193-e2e-054
  it("F13 五星评价 → ratingScore 5", async () => {
    const res = await cReq(`/tickets/${ticketId}/rate`, { method: "POST", body: JSON.stringify({ score: 5, comment: "响应很快" }) }, token, ip);
    expect(res.status).toBe(200);
    const t = ((await res.json()) as { ticket: { ratingScore: number; ratingComment: string } }).ticket;
    expect(t.ratingScore).toBe(5);
    expect(t.ratingComment).toBe("响应很快");
  });

  // MC193-map: MC193-e2e-055
  it("F14 重复评价 → 409", async () => {
    const res = await cReq(`/tickets/${ticketId}/rate`, { method: "POST", body: JSON.stringify({ score: 4 }) }, token, ip);
    expect(res.status).toBe(409);
  });

  // MC193-map: MC193-e2e-056
  it("F15 全旅程时间线完整：create→assign→start→complete→rate", async () => {
    const d = (await (await cReq(`/tickets/${ticketId}`, {}, token, ip)).json()) as { timeline: Array<{ action: string }> };
    expect(d.timeline.map((e) => e.action)).toEqual(["create", "assign", "start", "complete", "rate"]);
  });

  // MC193-map: MC193-e2e-057
  it("F16 会话消息全量落库（c_messages ≥ 8 条）", async () => {
    const r = await db.query(`SELECT role, count(*)::int AS n FROM c_messages WHERE workspace_id=$1 AND conversation_id=$2 GROUP BY role`, [WORKSPACE_ID, convId]);
    const by = Object.fromEntries(r.rows.map((x) => [x.role, x.n]));
    expect(by.user).toBeGreaterThanOrEqual(4);
    expect(by.assistant).toBeGreaterThanOrEqual(4);
  });
});

describeDb("F C 端旅程 · 场景分支", () => {
  // MC193-new: e2e-no-inferred-industry-write
  it("无行业适配器时订阅/行情词不自动推断 delivery/repair，不确认不落单", async () => {
    const { token } = await cSession(`${RUN}-f-no-adapter`, "h5", "203.0.113.152");
    for (const text of ["帮我开通研报速递订阅", "行情数据推送中断，帮我申报一下"]) {
      const result = await chat(token, text, {}, "203.0.113.152");
      expect(result.intent).toBe("kb_qa");
      const kind = (result.ticketDraft as { kind: string } | null)?.kind;
      expect(kind).not.toBe("delivery");
      expect(kind).not.toBe("repair");
      expect(result.ticket).toBeNull();
      expect(result.cards).toEqual([]);
    }
    const response = await cReq("/tickets", {}, token, "203.0.113.152");
    expect((await response.json() as { tickets: unknown[] }).tickets).toEqual([]);
  });

  // MC193-new: e2e-wifi-normalized-query
  it("无连字符 wifi 问句与 Wi-Fi 对照组命中同一本轮虚构知识", async () => {
    const { token } = await cSession(`${RUN}-f-wifi-normalized`, "h5", "203.0.113.153");
    const result = await chat(token, "wifi 密码是多少", {}, "203.0.113.153");
    expect((result.citations as Array<{ documentTitle: string }>).some((citation) => citation.documentTitle === `${RUN}-模拟服务须知`)).toBe(true);
    expect(String(result.answer)).toContain("EXAMPLE_NETWORK_193");
  });

  // MC193-map: MC193-e2e-058
  it("投诉一句话直达 → intent complaint + complaint 草稿（confirm 后值班负责人）", async () => {
    const { token } = await cSession(`${RUN}-fc`, "h5", "203.0.113.111");
    const r = await chat(token, "我要投诉，模拟报告的状态反馈不准确", {}, "203.0.113.111");
    expect(r.intent).toBe("complaint");
    expect(r.ticketDraft).toMatchObject({ kind: "complaint" });
    const ok = await chat(token, "我要投诉，模拟报告的状态反馈不准确", { confirmTicket: true, idempotencyKey: `${RUN}-fc-1` }, "203.0.113.111");
    expect((ok.ticket as { dept: string; kind: string })).toMatchObject({ kind: "complaint", dept: "值班负责人" });
  });

  // MC193-map: MC193-e2e-059
  it("低置信拒答转单：answer 拒答 + ticketDraft，confirm 后建 other 单（合规组）", async () => {
    const { token } = await cSession(`${RUN}-fl`, "h5", "203.0.113.112");
    const r = await chat(token, "火星移民船票怎么买", {}, "203.0.113.112");
    expect(String(r.answer)).toContain("无法准确回答");
    expect(r.citations).toEqual([]);
    expect(r.ticketDraft).toMatchObject({ kind: "other" });
    const ok = await chat(token, "火星移民船票怎么买", { confirmTicket: true, idempotencyKey: `${RUN}-fl-1` }, "203.0.113.112");
    expect((ok.ticket as { kind: string; dept: string })).toMatchObject({ kind: "other", dept: "合规组" });
  });

  // MC193-map: MC193-e2e-060
  it("行情异常明确人工协助 → 通用 other 草稿；显式选择 repair 后分派数据质量组", async () => {
    const { token } = await cSession(`${RUN}-fr`, "h5", "203.0.113.113");
    const r = await chat(token, "需要协助，行情数据推送中断，帮我申报一下", {}, "203.0.113.113");
    expect(r.intent).toBe("service_request");
    expect(r.ticketDraft).toMatchObject({ kind: "other" });
    const draft = r.ticketDraft as { title: string; payload: Record<string, unknown> };
    const ok = await chat(token, "行情数据推送中断，帮我申报一下", {
      confirmTicket: true, ticketDraft: { ...draft, kind: "repair" }, idempotencyKey: `${RUN}-fr-1`,
    }, "203.0.113.113");
    expect((ok.ticket as { dept: string })).toMatchObject({ dept: "数据质量组" });
  });

  // MC193-map: MC193-e2e-061
  it("疑问句含服务词不建服务单：「送站巴士几点发」走 kb_qa（未覆盖仅给拒答草稿，不落单）", async () => {
    const { token } = await cSession(`${RUN}-fq`, "h5", "203.0.113.114");
    const r = await chat(token, "送站巴士几点发车", {}, "203.0.113.114");
    expect(r.intent).toBe("kb_qa");
    expect(r.ticket).toBeNull(); // 不建单
    // KB 未覆盖 → 低置信拒答草稿（other），而非 service_request 送物单
    expect((r.ticketDraft as { kind: string } | null)?.kind ?? "other").not.toBe("delivery");
    const list = (await (await cReq("/tickets", {}, token, "203.0.113.114")).json()) as { tickets: unknown[] };
    expect(list.tickets).toHaveLength(0);
  });

  // 原案例检验显式 body.ticketDraft 优先；Tiger 不从订阅词推断 delivery，类型由用户显式选择。
  // MC193-map: MC193-e2e-062
  it("confirmTicket 带 body.ticketDraft 兜底（上轮草稿本轮确认，显式草稿优先）【bug 已修复】", async () => {
    const { token } = await cSession(`${RUN}-fb`, "h5", "203.0.113.115");
    const first = await chat(token, "帮我订购一份个股深度报告", {}, "203.0.113.115");
    const draft = first.ticketDraft as { kind: string; title: string; payload: Record<string, unknown> };
    expect(draft).toBeDefined();
    expect(draft.kind).toBe("other");
    const second = await chat(token, "好的确认提交", {
      confirmTicket: true, ticketDraft: { ...draft, kind: "delivery" }, idempotencyKey: `${RUN}-fb-1`,
    }, "203.0.113.115");
    expect((second.ticket as { kind: string; status: string })).toMatchObject({ kind: "delivery", status: "assigned" });
  });

  // MC193-map: MC193-e2e-063
  it("mock 标注：LLM 未装配时响应 mock:true", async () => {
    const { token } = await cSession(`${RUN}-fm`, "h5", "203.0.113.116");
    const r = await chat(token, "模拟盘净值哪里看", {}, "203.0.113.116");
    expect(r.mock).toBe(true);
  });

  // MC193-map: MC193-e2e-064
  it("latencyMs 为非负数字", async () => {
    const { token } = await cSession(`${RUN}-ft`, "h5", "203.0.113.117");
    const r = await chat(token, "日报几点发", {}, "203.0.113.117");
    expect(typeof r.latencyMs).toBe("number");
    expect(r.latencyMs as number).toBeGreaterThanOrEqual(0);
  });

  // 原 MRS 案例曾误断言酒店 WiFi 房间口令；以原案例标识保留映射，改为独立模拟 MRS 知识。
  // MC193-map: MC193-e2e-065
  it("开仓纪律问句命中本轮模拟 KB（MRS_TEST_ONLY，无生产交易阈值）", async () => {
    const { token } = await cSession(`${RUN}-fw`, "h5", "203.0.113.118");
    const r = await chat(token, "开仓的硬逻辑是什么", {}, "203.0.113.118");
    expect((r.citations as unknown[]).length).toBeGreaterThan(0);
    expect(String(r.answer)).toContain("MRS_TEST_ONLY");
  });

  // MC193-map: MC193-e2e-066
  it("连字符原样问句「Wi-Fi 密码」可命中（对照组）", async () => {
    const { token } = await cSession(`${RUN}-fw2`, "h5", "203.0.113.120");
    const r = await chat(token, "Wi-Fi 密码是多少", {}, "203.0.113.120");
    expect((r.citations as unknown[]).length).toBeGreaterThan(0);
    expect(String(r.answer)).toContain("EXAMPLE_NETWORK_193");
  });

  // MC193-map: MC193-e2e-067
  it("未 confirmTicket 时 ticket 为 null（草稿不落库）", async () => {
    const { token } = await cSession(`${RUN}-fn`, "h5", "203.0.113.119");
    const r = await chat(token, "帮我打扫一下房间", {}, "203.0.113.119");
    expect(r.ticket).toBeNull();
    const list = (await (await cReq("/tickets", {}, token, "203.0.113.119")).json()) as { tickets: unknown[] };
    expect(list.tickets).toHaveLength(0);
  });
});

/* ================= G. B 端视角（tRPC serviceRouter） ================= */

describeDb("G B 端 · 登录与守卫", () => {
  // MC193-map: MC193-e2e-068
  it("loginAs 返回 token + identity（owner / plan pro）", async () => {
    const r = await trpc("auth.loginAs", { input: { workspaceSlug: WORKSPACE_SLUG, memberNo: OWNER_MEMBER_NO }, method: "mutation" });
    expect(r.error).toBeNull();
    const d = r.data as { token: string; identity: Record<string, unknown> };
    expect(typeof d.token).toBe("string");
    expect(d.identity).toMatchObject({ memberNo: OWNER_MEMBER_NO, role: "owner", workspaceId: WORKSPACE_ID, tenantId: TENANT_ID, plan: "pro" });
  });

  // MC193-map: MC193-e2e-069
  it("loginAs 不存在工作区 → NOT_FOUND", async () => {
    const r = await trpc("auth.loginAs", { input: { workspaceSlug: `${RUN}-missing-workspace`, memberNo: OWNER_MEMBER_NO }, method: "mutation" });
    expect(r.error).not.toBeNull();
    expect(r.error!.data?.code).toBe("NOT_FOUND");
  });

  // MC193-map: MC193-e2e-070
  it("loginAs 不存在成员 → NOT_FOUND", async () => {
    const r = await trpc("auth.loginAs", { input: { workspaceSlug: WORKSPACE_SLUG, memberNo: `${RUN}-missing-member` }, method: "mutation" });
    expect(r.error!.data?.code).toBe("NOT_FOUND");
  });

  // MC193-map: MC193-e2e-071
  it("无 JWT 调受保护过程 → UNAUTHORIZED(401)", async () => {
    const r = await trpc("service.stats.overview");
    expect(r.status).toBe(401);
    expect(r.error!.data?.code).toBe("UNAUTHORIZED");
  });

  // MC193-map: MC193-e2e-072
  it("readonly 成员写操作 → FORBIDDEN(403)；查询放行", async () => {
    const w = await trpc("service.kb.createCollection", { input: { name: `${RUN}-ro` }, token: roToken, method: "mutation" });
    expect(w.status).toBe(403);
    expect(w.error!.data?.code).toBe("FORBIDDEN");
    const q = await trpc("service.kb.listCollections", { token: roToken });
    expect(q.error).toBeNull();
  });
});

describeDb("G B 端 · KB 管理", () => {
  let colId = "";
  let docId = "";

  // MC193-map: MC193-e2e-073
  it("kb.listCollections 含本轮独立模拟服务知识库", async () => {
    const r = await trpc("service.kb.listCollections", { token: bToken });
    const cols = (r.data as { collections: Array<{ id: string; name: string }> }).collections;
    expect(cols.some((c) => c.id === COLLECTION_ID && c.name === COLLECTION_NAME)).toBe(true);
  });

  // MC193-map: MC193-e2e-074
  it("kb.createCollection 创建后列表可见", async () => {
    const r = await trpc("service.kb.createCollection", { input: { name: `${RUN}-集合`, description: "e2e" }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    colId = (r.data as { collection: { id: string } }).collection.id;
    const list = await trpc("service.kb.listCollections", { token: bToken });
    expect((list.data as { collections: Array<{ id: string }> }).collections.some((c) => c.id === colId)).toBe(true);
  });

  // MC193-map: MC193-e2e-075
  it("kb.upsertDocument 新建 → version 1 + 切块数 > 0（pending_review）", async () => {
    const r = await trpc("service.kb.upsertDocument", {
      input: {
        collectionId: colId, title: `${RUN}-研报速递政策`, sourceKind: "manual",
        contentMd: `## 研报速递\n\n研报速递为付费订阅服务，盘前五分钟推送，每月十八元，正文长度足够。（${RUN}）\n`,
      }, token: bToken, method: "mutation",
    });
    expect(r.error).toBeNull();
    const d = r.data as { documentId: string; version: number; chunks: number };
    docId = d.documentId;
    expect(d.version).toBe(1);
    expect(d.chunks).toBeGreaterThan(0);
  });

  // MC193-map: MC193-e2e-076
  it("kb.upsertDocument 同内容再传 → hash 幂等（chunks 0 同文档）", async () => {
    const r = await trpc("service.kb.upsertDocument", {
      input: {
        collectionId: colId, title: `${RUN}-研报速递政策`, sourceKind: "manual",
        contentMd: `## 研报速递\n\n研报速递为付费订阅服务，盘前五分钟推送，每月十八元，正文长度足够。（${RUN}）\n`,
      }, token: bToken, method: "mutation",
    });
    const d = r.data as { documentId: string; version: number; chunks: number };
    expect(d.documentId).toBe(docId);
    expect(d.chunks).toBe(0);
  });

  // MC193-map: MC193-e2e-077
  it("kb.upsertDocument 同标题新内容 → version 2", async () => {
    const r = await trpc("service.kb.upsertDocument", {
      input: {
        collectionId: colId, title: `${RUN}-研报速递政策`, sourceKind: "manual",
        contentMd: `## 研报速递\n\n研报速递服务调整为每月二十元，盘前十分钟推送，正文长度足够用来切块。（${RUN}）\n`,
      }, token: bToken, method: "mutation",
    });
    expect((r.data as { version: number }).version).toBe(2);
  });

  // MC193-map: MC193-e2e-078
  it("kb.listDocuments 按集合过滤", async () => {
    const r = await trpc("service.kb.listDocuments", { input: { collectionId: colId }, token: bToken });
    const docs = (r.data as { documents: Array<{ id: string; collectionId: string }> }).documents;
    expect(docs.length).toBeGreaterThan(0);
    expect(docs.every((d) => d.collectionId === colId)).toBe(true);
  });

  // MC193-map: MC193-e2e-079
  it("kb.pendingReviews 列出待审文档", async () => {
    const r = await trpc("service.kb.pendingReviews", { token: bToken });
    const docs = (r.data as { documents: Array<{ id: string; title: string }> }).documents;
    expect(docs.some((d) => d.id === docId)).toBe(true);
  });

  // MC193-map: MC193-e2e-080
  it("approveDocument 批准生效 → ok + eventId（pendingReviews 移除）", async () => {
    const r = await trpc("service.kb.approveDocument", { input: { documentId: docId }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    const d = r.data as { ok: boolean; eventId: string };
    expect(d.ok).toBe(true);
    expect(typeof d.eventId).toBe("string");
    const after = await trpc("service.kb.pendingReviews", { token: bToken });
    expect((after.data as { documents: Array<{ id: string }> }).documents.some((x) => x.id === docId)).toBe(false);
  });

  // MC193-map: MC193-e2e-081
  it("approveDocument 联动审批台（五元事件 + approvals 行落库，event_id 关联）", async () => {
    const ev = await db.query<{ event_id: string }>(
      `SELECT event_id FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='kb.publish'
       AND payload->'object'->>'id'=$2 ORDER BY seq DESC LIMIT 1`,
      [WORKSPACE_ID, docId],
    );
    expect(ev.rows.length).toBeGreaterThan(0);
    const ap = await db.query<{ approval_id: string; event_id: string; status: string }>(
      `SELECT approval_id, event_id, status FROM approvals WHERE workspace_id=$1 AND event_id=$2 AND channel='inapp'`,
      [WORKSPACE_ID, ev.rows[0]!.event_id],
    );
    expect(ap.rows.length).toBeGreaterThan(0);
    expect(ap.rows[0]!.event_id).toBe(ev.rows[0]!.event_id);
    expect(ap.rows[0]!.status).toBe("approved");
  });

  // MC193-map: MC193-e2e-082
  it("approveDocument 后检索可见（kb.search 命中）", async () => {
    // 查询词带 RUN 指纹：历次运行累积的同主题 active 文档不会稀释本轮命中
    const r = await trpc("service.kb.search", { input: { query: `研报速递怎么订阅 ${RUN}`, limit: 20 }, token: bToken });
    const hits = (r.data as { hits: Array<{ documentTitle: string }> }).hits;
    expect(hits.some((h) => h.documentTitle === `${RUN}-研报速递政策`)).toBe(true);
  });

  // MC193-map: MC193-e2e-083
  it("approveDocument 不存在文档 → NOT_FOUND", async () => {
    const r = await trpc("service.kb.approveDocument", { input: { documentId: "kbd-none" }, token: bToken, method: "mutation" });
    expect(r.error!.data?.code).toBe("NOT_FOUND");
  });

  // MC193-map: MC193-e2e-084
  it("kb.setStatus disabled → 检索不可见；恢复 active 可检索", async () => {
    await trpc("service.kb.setStatus", { input: { documentId: docId, status: "disabled" }, token: bToken, method: "mutation" });
    const off = await trpc("service.kb.search", { input: { query: `研报速递订阅 ${RUN}`, limit: 20 }, token: bToken });
    expect((off.data as { hits: Array<{ documentTitle: string }> }).hits.some((h) => h.documentTitle === `${RUN}-研报速递政策`)).toBe(false);
    await trpc("service.kb.setStatus", { input: { documentId: docId, status: "active" }, token: bToken, method: "mutation" });
    const on = await trpc("service.kb.search", { input: { query: `研报速递订阅 ${RUN}`, limit: 20 }, token: bToken });
    expect((on.data as { hits: Array<{ documentTitle: string }> }).hits.some((h) => h.documentTitle === `${RUN}-研报速递政策`)).toBe(true);
  });
});

describeDb("G B 端 · 工单消费", () => {
  let cTicketId = "";
  let cToken = "";
  let createdId = "";

  // MC193-new: e2e-assigned-state-boundary
  it("tickets.assign processing/done/closed 仍拒绝 409 CONFLICT，不改状态或追加事件", async () => {
    for (const status of ["processing", "done", "closed"] as const) {
      const id = `tck-${RUN}-terminal-${status}`;
      await db.query(
        `INSERT INTO c_tickets (id,workspace_id,kind,title,payload,status,dept) VALUES ($1,$2,'other',$3,'{}',$4,'合规组')`,
        [id, WORKSPACE_ID, `${RUN}-${status}-边界单`, status],
      );
      const response = await trpc("service.tickets.assign", { input: { ticketId: id, dept: "复盘组" }, token: bToken, method: "mutation" });
      expect(response.status).toBe(409);
      expect(response.error?.data?.code).toBe("CONFLICT");
      const row = await db.query<{ status: string; dept: string }>(`SELECT status,dept FROM c_tickets WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, id]);
      expect(row.rows).toEqual([{ status, dept: "合规组" }]);
      const events = await db.query<{ n: number }>(
        `SELECT (SELECT count(*) FROM c_ticket_events WHERE workspace_id=$1 AND ticket_id=$2)::int
              + (SELECT count(*) FROM biz_events WHERE workspace_id=$1 AND payload->'object'->>'id'=$2)::int AS n`,
        [WORKSPACE_ID, id],
      );
      expect(events.rows[0]!.n).toBe(0);
    }
  });

  // MC193-map: MC193-e2e-085
  it("tickets.list 按状态过滤（assigned）", async () => {
    const s = await cSession(`${RUN}-g`, "h5", "203.0.113.121");
    cToken = s.token;
    const made = (await (await cReq("/tickets", {
      method: "POST", body: JSON.stringify({ kind: "delivery", title: `${RUN}-B端消费单`, payload: {}, idempotencyKey: `${RUN}-g-1` }),
    }, cToken, "203.0.113.121")).json()) as { ticket: { id: string } };
    cTicketId = made.ticket.id;
    const r = await trpc("service.tickets.list", { input: { status: "assigned" }, token: bToken });
    const tickets = (r.data as { tickets: Array<{ id: string; status: string }> }).tickets;
    expect(tickets.some((t) => t.id === cTicketId)).toBe(true);
    expect(tickets.every((t) => t.status === "assigned")).toBe(true);
  });

  // MC193-map: MC193-e2e-086
  it("tickets.assign 指定 dept/assignee（created 单）", async () => {
    // 直插一张 created 单（C 端链路建单即 assigned，created 态由 DB fixture 构造）
    createdId = `tck-${RUN}-created`;
    await db.query(
      `INSERT INTO c_tickets (id, workspace_id, kind, title, payload, status) VALUES ($1,$2,'other',$3,'{}','created')`,
      [createdId, WORKSPACE_ID, `${RUN}-待分派单`],
    );
    const r = await trpc("service.tickets.assign", { input: { ticketId: createdId, dept: "复盘组", assignee: STAFF_MEMBER_NO }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    expect((r.data as { ticket: Record<string, unknown> }).ticket).toMatchObject({ dept: "复盘组", assignee: STAFF_MEMBER_NO, status: "assigned" });
  });

  // MC193-map: MC193-e2e-087
  it("tickets.assign assigned 同值重复为 no-op；真实改派仅追加一条留痕", async () => {
    const snapshot = async () => {
      const row = await db.query<{ status: string; dept: string; assignee: string; updated_at: Date }>(
        `SELECT status,dept,assignee,updated_at FROM c_tickets WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, createdId],
      );
      const events = await db.query<{ ticket_events: number; audit_events: number }>(
        `SELECT (SELECT count(*)::int FROM c_ticket_events WHERE workspace_id=$1 AND ticket_id=$2) AS ticket_events,
                (SELECT count(*)::int FROM biz_events WHERE workspace_id=$1 AND payload->'object'->>'id'=$2
                 AND payload->'decision'->>'action'='service.ticket.assign') AS audit_events`, [WORKSPACE_ID, createdId],
      );
      return { row: row.rows[0]!, events: events.rows[0]! };
    };
    const before = await snapshot();
    const r = await trpc("service.tickets.assign", {
      input: { ticketId: createdId, dept: "复盘组", assignee: STAFF_MEMBER_NO }, token: bToken, method: "mutation",
    });
    expect(r.error).toBeNull();
    expect((r.data as { ticket: Record<string, unknown> }).ticket).toMatchObject({ status: "assigned", dept: "复盘组", assignee: STAFF_MEMBER_NO });
    const repeated = await snapshot();
    expect(repeated).toEqual(before);
    const changed = await trpc("service.tickets.assign", {
      input: { ticketId: createdId, dept: "数据质量组", assignee: STAFF_MEMBER_NO }, token: bToken, method: "mutation",
    });
    expect(changed.error).toBeNull();
    expect((changed.data as { ticket: Record<string, unknown> }).ticket).toMatchObject({ status: "assigned", dept: "数据质量组", assignee: STAFF_MEMBER_NO });
    const after = await snapshot();
    expect(after.events.ticket_events).toBe(before.events.ticket_events + 1);
    expect(after.events.audit_events).toBe(before.events.audit_events + 1);
    const timeline = await trpc("service.tickets.timeline", { input: { ticketId: createdId }, token: bToken });
    expect((timeline.data as { timeline: Array<{ detail: { reassigned?: boolean } }> }).timeline.at(-1)?.detail.reassigned).toBe(true);
  });

  // MC193-map: MC193-e2e-088
  it("tickets.advance start → processing（留痕）", async () => {
    const r = await trpc("service.tickets.advance", { input: { ticketId: cTicketId, action: "start" }, token: bToken, method: "mutation" });
    expect((r.data as { ticket: { status: string } }).ticket.status).toBe("processing");
  });

  // MC193-map: MC193-e2e-089
  it("tickets.advance 非 start 动作 → 留痕不变状态", async () => {
    const r = await trpc("service.tickets.advance", { input: { ticketId: cTicketId, action: "备注：客人催促一次" }, token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    expect((r.data as { ticket: { status: string } }).ticket.status).toBe("processing");
  });

  // MC193-map: MC193-e2e-090
  it("tickets.complete → done + C 端收到完成通知", async () => {
    const r = await trpc("service.tickets.complete", { input: { ticketId: cTicketId, result: "已送达" }, token: bToken, method: "mutation" });
    expect((r.data as { ticket: { status: string } }).ticket.status).toBe("done");
    const n = (await (await cReq("/notifications", {}, cToken, "203.0.113.121")).json()) as { notifications: Array<{ kind: string; payload: { ticketId?: string } }> };
    expect(n.notifications.some((x) => x.kind === "ticket.completed" && x.payload.ticketId === cTicketId)).toBe(true);
  });

  // MC193-map: MC193-e2e-091
  it("tickets.timeline 完整回放（create/assign/start/备注/complete）", async () => {
    const r = await trpc("service.tickets.timeline", { input: { ticketId: cTicketId }, token: bToken });
    const actions = (r.data as { timeline: Array<{ action: string }> }).timeline.map((e) => e.action);
    expect(actions).toContain("create");
    expect(actions).toContain("assign");
    expect(actions).toContain("start");
    expect(actions).toContain("备注：客人催促一次");
    expect(actions).toContain("complete");
  });

  // MC193-map: MC193-e2e-092
  it("tickets.slaScan 返回 {escalated:number}", async () => {
    const r = await trpc("service.tickets.slaScan", { token: bToken, method: "mutation" });
    expect(r.error).toBeNull();
    expect(typeof (r.data as { escalated: number }).escalated).toBe("number");
  });
});

describeDb("G B 端 · stats.overview 指标", () => {
  // MC193-map: MC193-e2e-093
  it("字段齐全：date/sessions/qaCount/avgConfidence/groundedRate/avgLatencyMs/ticketsToday/completionRate/slaBreached/avgRating", async () => {
    const r = await trpc("service.stats.overview", { token: bToken });
    expect(r.error).toBeNull();
    const d = r.data as Record<string, unknown>;
    for (const k of ["date", "sessions", "qaCount", "avgConfidence", "groundedRate", "avgLatencyMs", "ticketsToday", "completionRate", "slaBreached", "avgRating"]) {
      expect(d, k).toHaveProperty(k);
    }
    expect(String(d.date)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  // MC193-map: MC193-e2e-094
  it("指标合理性：比率 ∈ [0,1]、计数非负、今日有问答与工单", async () => {
    const r = await trpc("service.stats.overview", { token: bToken });
    const d = r.data as Record<string, number | null>;
    expect(d.sessions).toBeGreaterThan(0);
    expect(d.qaCount).toBeGreaterThan(0);
    expect(d.ticketsToday).toBeGreaterThan(0);
    expect(d.groundedRate).toBeGreaterThanOrEqual(0);
    expect(d.groundedRate!).toBeLessThanOrEqual(1);
    if (d.completionRate !== null) {
      expect(d.completionRate).toBeGreaterThanOrEqual(0);
      expect(d.completionRate).toBeLessThanOrEqual(1);
    }
    expect(d.avgLatencyMs).toBeGreaterThanOrEqual(0);
    expect(d.avgConfidence).toBeGreaterThan(0);
    expect(d.slaBreached).toBeGreaterThanOrEqual(0);
  });

  // MC193-map: MC193-e2e-095
  it("有据率口径：有引用助手消息 / 全部助手消息（抽查一致性）", async () => {
    const r = await trpc("service.stats.overview", { token: bToken });
    const d = r.data as { groundedRate: number | null };
    const q = await db.query<{ grounded: string; answered: string }>(
      `SELECT count(*) FILTER (WHERE role='assistant' AND jsonb_array_length(citations) > 0)::text AS grounded,
              count(*) FILTER (WHERE role='assistant')::text AS answered
       FROM c_messages WHERE workspace_id=$1 AND created_at >= date_trunc('day', now())`,
      [WORKSPACE_ID],
    );
    const expectRate = Number((Number(q.rows[0]!.grounded) / Number(q.rows[0]!.answered)).toFixed(3));
    expect(d.groundedRate).toBe(expectRate);
  });

  // MC193-map: MC193-e2e-096
  it("完结率随办结单提升（F 旅程 done 单计入）", async () => {
    const r = await trpc("service.stats.overview", { token: bToken });
    const d = r.data as { completionRate: number | null };
    expect(d.completionRate).not.toBeNull();
    expect(d.completionRate!).toBeGreaterThan(0);
  });
});
