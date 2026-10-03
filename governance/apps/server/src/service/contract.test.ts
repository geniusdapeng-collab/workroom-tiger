/**
 * server · C 端网关契约 fixture 单测（H6：以 webc types.ts 为准断言响应形状）
 * 直连 Hono handler（serviceGateway.request），显式 RUN_DB_TESTS=1 才连接自有沙箱活库。
 * 每轮构造独立 trading 工作区/装配/知识库，禁止依赖酒店种子或读取 .env。
 * 覆盖：session 形状与渠道门控/限流、cards kind/data、statusText 中文枚举、
 *      /member /orders /notifications 形状、建单幂等重放、rate 409、404 requestId、低置信拒答 ticketDraft。
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Hono } from "hono";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import pg from "pg";

const RUN_DB = process.env.RUN_DB_TESTS === "1";
const describeDb = RUN_DB ? describe.sequential : describe.skip;
const DB_KEYS = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const;
if (RUN_DB) {
  if (process.env.SERVICE_TEST_FIXTURES !== "owned") {
    throw new Error("MC193 contract: RUN_DB_TESTS=1 requires SERVICE_TEST_FIXTURES=owned");
  }
  const databases = DB_KEYS.map((key) => {
    const value = process.env[key];
    if (!value?.trim()) throw new Error(`MC193 contract: RUN_DB_TESTS=1 requires ${key}`);
    try {
      const url = new URL(value);
      if (!["postgres:", "postgresql:"].includes(url.protocol)
        || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
        || url.pathname.length <= 1) throw new Error("invalid fixture database");
      return `${url.hostname}:${url.port || "5432"}${url.pathname}`;
    } catch {
      throw new Error(`MC193 contract: ${key} must select an explicit loopback fixture database`);
    }
  });
  if (new Set(databases).size !== 1) throw new Error("MC193 contract: all database roles must select the same fixture database");
}

let app: Hono;
const RUN = `mc193-contract-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;
const TENANT_ID = `tenant-${RUN}`;
const WORKSPACE_ID = `ws-${RUN}`;
const INSTALL_ID = `bi-${RUN}`;
const COLLECTION_ID = `kbc-${RUN}`;
const DOCUMENT_ID = `kbd-${RUN}`;
const KB_QUESTION = "本轮契约服务说明是什么";
const KB_CONTENT = `${KB_QUESTION}？本轮契约夹具仅用于模拟服务说明，不代表真实经营政策。${RUN}`;
const IP = "198.51.100.77"; // TEST-NET-2，与其它用例隔离限流桶
let fixtureDb: pg.Client | undefined;
let fixtureCommitted = false;
let fixtureConnectionFailed = false;
const ENV_KEYS = ["NODE_ENV", "SERVICE_C_DEMO_AUTH", "SERVICE_C_SECRET", "SERVICE_C_WORKSPACE_ID", "SERVICE_C_WORKSPACE_MAP", "SERVICE_C_WECHAT_APPID", "SERVICE_C_WECHAT_SECRET", "SERVICE_C_ALIPAY_APPID", "SERVICE_C_ALIPAY_KEY", "LLM_PROVIDER", "BUNDLES_ROOT"] as const;
const originalEnv = new Map(ENV_KEYS.map((key) => [key, process.env[key]]));

async function prepareFixture(): Promise<void> {
  fixtureDb = new pg.Client({ connectionString: process.env.DATABASE_URL!, connectionTimeoutMillis: 5_000, statement_timeout: 30_000 });
  fixtureDb.on("error", () => { fixtureConnectionFailed = true; });
  try {
    await fixtureDb.connect();
    await fixtureDb.query("BEGIN");
    await fixtureDb.query(`INSERT INTO tenants (id,name,plan) VALUES ($1,$2,'pro')`, [TENANT_ID, RUN]);
    await fixtureDb.query(
      `INSERT INTO workspaces (id,tenant_id,name,slug,industry,bundle_id,is_example) VALUES ($1,$2,$3,$3,'trading','trading',false)`,
      [WORKSPACE_ID, TENANT_ID, RUN],
    );
    await fixtureDb.query(
      `INSERT INTO bundle_installs (id,workspace_id,bundle_id,assets,status) VALUES ($1,$2,'trading',$3,'active')`,
      [INSTALL_ID, WORKSPACE_ID, JSON.stringify({ fixture: "MC193", run: RUN, nativeInstallation: false })],
    );
    await fixtureDb.query(`INSERT INTO kb_collections (id,workspace_id,name) VALUES ($1,$2,$3)`, [COLLECTION_ID, WORKSPACE_ID, RUN]);
    await fixtureDb.query(
      `INSERT INTO kb_documents (id,workspace_id,collection_id,title,source_kind,status,content_md,hash) VALUES ($1,$2,$3,$4,'manual','active',$5,$6)`,
      [DOCUMENT_ID, WORKSPACE_ID, COLLECTION_ID, `${RUN}-服务说明`, KB_CONTENT, createHash("sha256").update(KB_CONTENT).digest("hex")],
    );
    await fixtureDb.query(
      `INSERT INTO kb_chunks (workspace_id,document_id,chunk_index,heading,content) VALUES ($1,$2,0,$3,$4)`,
      [WORKSPACE_ID, DOCUMENT_ID, KB_QUESTION, KB_CONTENT],
    );
    await fixtureDb.query("COMMIT");
    fixtureCommitted = true;
  } catch {
    try { await fixtureDb.query("ROLLBACK"); } catch { throw new Error("MC193 contract fixture preparation and rollback failed"); }
    throw new Error("MC193 contract fixture preparation failed");
  }
}

async function cleanFixture(): Promise<void> {
  if (!fixtureCommitted || !fixtureDb) return;
  if (fixtureConnectionFailed) throw new Error("MC193 contract fixture connection failed before cleanup");
  try {
    await fixtureDb.query("BEGIN");
    const owned = await fixtureDb.query(`SELECT 1 FROM workspaces WHERE id=$1 AND tenant_id=$2 AND name=$3`, [WORKSPACE_ID, TENANT_ID, RUN]);
    if (owned.rowCount !== 1) throw new Error("fixture ownership mismatch");
    for (const table of ["c_notifications", "c_ticket_events", "c_tickets", "c_messages", "c_conversations", "c_users", "kb_chunks", "kb_documents", "kb_sources", "kb_collections"] as const) {
      await fixtureDb.query(`DELETE FROM ${table} WHERE workspace_id=$1`, [WORKSPACE_ID]);
    }
    await fixtureDb.query(`DELETE FROM bundle_installs WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, INSTALL_ID]);
    // biz_events/approvals 永不删除；保留本轮 tenant/workspace 作为审计身份锚点。
    await fixtureDb.query("COMMIT");
  } catch {
    try { await fixtureDb.query("ROLLBACK"); } catch { throw new Error("MC193 contract fixture cleanup and rollback failed"); }
    throw new Error("MC193 contract fixture cleanup failed");
  }
}

async function req(path: string, init?: RequestInit, token?: string): Promise<Response> {
  return app.request(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "x-forwarded-for": IP,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
}

async function makeSession(openid: string): Promise<{ token: string; user: Record<string, unknown> }> {
  const res = await req("/session", { method: "POST", body: JSON.stringify({ channel: "h5", openid, nickname: "契约测试" }) });
  expect(res.status).toBe(200);
  return (await res.json()) as { token: string; user: Record<string, unknown> };
}

beforeAll(async () => {
  if (!RUN_DB) return;
  process.env.NODE_ENV = "test";
  process.env.SERVICE_C_DEMO_AUTH = "true";
  process.env.SERVICE_C_SECRET = "mc193-contract-fixture-only-c-secret-000000";
  process.env.SERVICE_C_WORKSPACE_ID = WORKSPACE_ID;
  delete process.env.SERVICE_C_WORKSPACE_MAP;
  for (const key of ["SERVICE_C_WECHAT_APPID", "SERVICE_C_WECHAT_SECRET", "SERVICE_C_ALIPAY_APPID", "SERVICE_C_ALIPAY_KEY"] as const) delete process.env[key];
  process.env.LLM_PROVIDER = "mock";
  process.env.BUNDLES_ROOT = fileURLToPath(new URL("../../../../bundles", import.meta.url));
  await prepareFixture();
  ({ serviceGateway: app } = await import("./gateway.js"));
  const { resolveWorkspaceBusinessAdapter } = await import("./adapters/business-registry.js");
  const binding = await resolveWorkspaceBusinessAdapter(WORKSPACE_ID);
  if (binding.state !== "adapter-not-declared" || binding.bundleId !== "trading" || binding.adapter !== null) {
    throw new Error("MC193 contract requires the verified trading bundle with no declared business adapter");
  }
}, 90_000);

afterAll(async () => {
  if (!RUN_DB) return;
  const failures: string[] = [];
  try { await cleanFixture(); } catch { failures.push("fixture_cleanup_failed"); }
  try { await fixtureDb?.end(); } catch { failures.push("fixture_connection_close_failed"); }
  try { const { closeAllPools } = await import("@workloom/db"); await closeAllPools(); } catch { failures.push("service_pool_close_failed"); }
  for (const [key, value] of originalEnv) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
  if (failures.length) throw new Error(`MC193 contract cleanup failed: ${failures.join(",")}`);
});

describeDb("契约 · session（S2）", () => {
  // MC193-map: MC193-contract-001
  it("h5 演示直登返回 {token, user}", async () => {
    const s = await makeSession(`${RUN}-a`);
    expect(typeof s.token).toBe("string");
    expect(s.user).toMatchObject({ channel: "h5", openid: `${RUN}-a` });
    expect(typeof s.user.id).toBe("string");
  });

  // MC193-map: MC193-contract-002
  it("wechat-mini 无 code → 400；有 code 无凭据 → 503 渠道未配置", async () => {
    const r1 = await req("/session", { method: "POST", body: JSON.stringify({ channel: "wechat-mini" }) });
    expect(r1.status).toBe(400);
    const r2 = await req("/session", { method: "POST", body: JSON.stringify({ channel: "wechat-mini", code: "abc" }) });
    expect(r2.status).toBe(503);
    expect(((await r2.json()) as { error: string }).error).toContain("渠道未配置");
  });

  // MC193-map: MC193-contract-003
  it("session IP+channel 限流 60 次/分", async () => {
    let last = 0;
    for (let i = 0; i < 65; i++) {
      const res = await app.request("/session", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-forwarded-for": "203.0.113.9" },
        body: JSON.stringify({ channel: "h5", openid: `${RUN}-rl` }),
      });
      last = res.status;
      if (last === 429) break;
    }
    expect(last).toBe(429);
  });
});

describeDb("契约 · member / orders / notifications（H6）", () => {
  // MC193-new: contract-identity-unavailable
  it("有效格式的身份绑定请求失败关闭为 503，且不修改 C 用户身份", async () => {
    const session = await makeSession(`${RUN}-identity`);
    for (const path of ["/identity/code", "/identity/bind"]) {
      const response = await req(path, {
        method: "POST", body: JSON.stringify({ phone: "19900000193", code: "123456" }),
      }, session.token);
      expect(response.status).toBe(503);
      const body = await response.json() as { code?: string; requestId?: string };
      expect(body.code).toBe("IDENTITY_ADAPTER_UNAVAILABLE");
      expect(typeof body.requestId).toBe("string");
    }
    const user = await fixtureDb!.query<{ member_id: string | null; phone_hash: string | null }>(
      `SELECT member_id,phone_hash FROM c_users WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, session.user.id],
    );
    expect(user.rows).toEqual([{ member_id: null, phone_hash: null }]);
  });

  // MC193-new: contract-no-active-install
  it("本轮装配停用后业务接口返回 503，不回退其他工作区或酒店示例", async () => {
    const { token } = await makeSession(`${RUN}-no-install`);
    const changed = await fixtureDb!.query(`UPDATE bundle_installs SET status='uninstalled' WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, INSTALL_ID]);
    if (changed.rowCount !== 1) throw new Error("MC193 contract active fixture was not selected");
    try {
      for (const path of ["/orders", "/member"]) {
        const response = await req(path, {}, token);
        expect(response.status).toBe(503);
        expect(await response.json()).toMatchObject({ code: "BUSINESS_ADAPTER_UNAVAILABLE" });
      }
    } finally {
      const restored = await fixtureDb!.query(`UPDATE bundle_installs SET status='active' WHERE workspace_id=$1 AND id=$2`, [WORKSPACE_ID, INSTALL_ID]);
      if (restored.rowCount !== 1) throw new Error("MC193 contract active fixture restore failed");
    }
  });

  // MC193-map: MC193-contract-004
  it("Tiger 未声明业务适配器：/member 与 /orders 明示 unavailable，不投影酒店身份", async () => {
    const { token } = await makeSession(`${RUN}-guest`);
    const m = (await (await req("/member", {}, token)).json()) as Record<string, unknown>;
    expect(m).toMatchObject({ title: "权益信息不可用", demo: false, available: false });
    expect(Array.isArray(m.benefits)).toBe(true);
    expect(m.benefits).toEqual([]);
    for (const field of ["level", "points", "hint", "bindRequired"]) expect(m).not.toHaveProperty(field);

    const o = (await (await req("/orders", {}, token)).json()) as Record<string, unknown>;
    expect(o).toMatchObject({ demo: false, available: false });
    expect(o.orders).toEqual([]);
  });

  // MC193-map: MC193-contract-005
  it("/notifications 每项含 read:false 占位", async () => {
    const { token } = await makeSession(`${RUN}-ntf`);
    // 先建一单触发受理通知
    await req("/tickets", {
      method: "POST",
      body: JSON.stringify({ kind: "other", title: "契约测试通知", payload: {}, idempotencyKey: `${RUN}-ntf-t1` }),
    }, token);
    const n = (await (await req("/notifications", {}, token)).json()) as { notifications: Array<Record<string, unknown>> };
    expect(n.notifications.length).toBeGreaterThan(0);
    for (const item of n.notifications) {
      expect(item).toMatchObject({ read: false });
      expect(typeof item.id).toBe("string");
      expect(typeof item.kind).toBe("string");
      expect(typeof item.createdAt).toBe("string");
    }
  });
});

describeDb("契约 · 工单（H1/H2/H6/L9/M9）", () => {
  // MC193-map: MC193-contract-006
  it("建单 → {ticket:{id,kind,title,status,statusText}}；同键重放 idempotentReplay 且同 id", async () => {
    const { token } = await makeSession(`${RUN}-t`);
    const body = JSON.stringify({ kind: "repair", title: "契约测试-模拟数据质量检查", payload: { fixture: RUN }, idempotencyKey: `${RUN}-t1` });
    const r1 = (await (await req("/tickets", { method: "POST", body }, token)).json()) as { ticket: Record<string, unknown> };
    expect(r1.ticket).toMatchObject({ kind: "repair", title: "契约测试-模拟数据质量检查", status: "assigned", statusText: "已受理" });
    expect(typeof r1.ticket.id).toBe("string");

    const r2 = (await (await req("/tickets", { method: "POST", body }, token)).json()) as { ticket: { id: string }; idempotentReplay?: boolean };
    expect(r2.idempotentReplay).toBe(true);
    expect(r2.ticket.id).toBe(r1.ticket.id);

    // 列表项同样带 statusText
    const list = (await (await req("/tickets", {}, token)).json()) as { tickets: Array<Record<string, unknown>> };
    const mine = list.tickets.find((t) => t.id === r1.ticket.id)!;
    expect(mine).toMatchObject({ status: "assigned", statusText: "已受理" });
  });

  // MC193-map: MC193-contract-007
  it("非 done 工单评价 → 409；不存在工单详情 → 404 带 requestId", async () => {
    const { token } = await makeSession(`${RUN}-r`);
    const created = (await (await req("/tickets", {
      method: "POST",
      body: JSON.stringify({ kind: "delivery", title: "契约测试-模拟报告服务", payload: {}, idempotencyKey: `${RUN}-r1` }),
    }, token)).json()) as { ticket: { id: string } };
    const rate = await req(`/tickets/${created.ticket.id}/rate`, { method: "POST", body: JSON.stringify({ score: 5 }) }, token);
    expect(rate.status).toBe(409);

    const missing = await req("/tickets/tck-not-exist", {}, token);
    expect(missing.status).toBe(404);
    const mj = (await missing.json()) as { requestId?: string };
    expect(typeof mj.requestId).toBe("string");
  });

  // MC193-map: MC193-contract-008
  it("M9：非法 kind / 超长 title → 400", async () => {
    const { token } = await makeSession(`${RUN}-v`);
    const bad = await req("/tickets", { method: "POST", body: JSON.stringify({ kind: "hack", title: "x", payload: {} }) }, token);
    expect(bad.status).toBe(400);
    const long = await req("/tickets", { method: "POST", body: JSON.stringify({ kind: "other", title: "长".repeat(121), payload: {} }) }, token);
    expect(long.status).toBe(400);
  });
});

describeDb("契约 · chat（H5/H6/M9）", () => {
  // MC193-map: MC193-contract-009
  it("KB 高置信问答：citations 非空、cards 为 {kind,data} 契约", async () => {
    const { token } = await makeSession(`${RUN}-c`);
    const r = (await (await req("/chat", {
      method: "POST",
      body: JSON.stringify({ text: KB_QUESTION }),
    }, token)).json()) as Record<string, unknown>;
    expect(r).toMatchObject({ intent: "kb_qa" });
    expect(typeof r.answer).toBe("string");
    const conf = r.confidence as number;
    expect(conf).toBeGreaterThan(0);
    expect(conf).toBeLessThanOrEqual(1);
    expect(conf).toBeGreaterThanOrEqual(0.72);
    expect((r.citations as unknown[]).length).toBeGreaterThan(0);
    expect((r.citations as Array<{ documentTitle: string }>).some((citation) => citation.documentTitle === `${RUN}-服务说明`)).toBe(true);
    expect(r.cards).toEqual([]);
    for (const card of (r.cards ?? []) as Array<{ kind: string; data: unknown }>) {
      expect(["order", "member", "catalog"]).toContain(card.kind);
      expect(typeof card.data).toBe("object");
    }
  });

  // MC193-map: MC193-contract-010
  it("低置信问题诚实拒答 + ticketDraft（无 citations）", async () => {
    const { token } = await makeSession(`${RUN}-c2`);
    const r = (await (await req("/chat", {
      method: "POST",
      body: JSON.stringify({ text: "火星移民船票怎么买" }),
    }, token)).json()) as Record<string, unknown>;
    expect(r.confidence as number).toBeLessThan(0.5);
    expect((r.citations as unknown[]).length).toBe(0);
    expect(r.ticketDraft).toMatchObject({ kind: "other" });
    expect(String(r.answer)).toContain("无法准确回答");
  });

  // MC193-map: MC193-contract-011
  it("text 超 2000 字符 → 400", async () => {
    const { token } = await makeSession(`${RUN}-c3`);
    const res = await req("/chat", { method: "POST", body: JSON.stringify({ text: "长".repeat(2001) }) }, token);
    expect(res.status).toBe(400);
  });
});
