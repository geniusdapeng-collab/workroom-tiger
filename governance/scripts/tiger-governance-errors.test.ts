/** Actual owned handlers + isolated PostgreSQL fault injection; no real provider or external message. */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { afterAll, beforeAll, describe, it, vi } from "vitest";
import pg from "pg";
import { closeAllPools } from "../packages/db/src/client.js";
import { seedTrading } from "./seed-trading.js";
import { appRouter } from "../apps/server/src/trpc/router.js";
import { handleMessage } from "../apps/server/src/service/dialog.js";
import { issueH5EntryToken } from "../apps/server/src/service/channels.js";
import type { Identity } from "@workloom/base/tenancy";

// Only the external model dependency fails synthetically. The real dialog,
// KB retrieval, conversation writes, permission middleware and PG transactions
// run unchanged. Other scenes use the actual mock-disabled assembly.
const injectedModel = vi.hoisted(() => ({ message: "", calls: 0 }));
vi.mock("../apps/server/src/service/llm.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../apps/server/src/service/llm.js")>();
  return { ...actual, llmCall: (scene = "generic") => injectedModel.message
    ? async () => { injectedModel.calls += 1; throw new Error(injectedModel.message); }
    : actual.llmCall(scene) };
});

if (!process.env.TIGER_GOVERNANCE_TEST_DATABASE_URL || !process.env.DATABASE_URL || !process.env.DATABASE_APP_URL
  || !process.env.DATABASE_GATEWAY_URL || !process.env.TIGER_GOVERNANCE_ERROR_EVIDENCE_DIR) {
  throw new Error("Owned error regression requires explicit isolated database URLs and evidence directory");
}
const fixture = `mc140-errors-${randomUUID().replaceAll("-", "").slice(0, 12)}`;
const scope = { tenantId: fixture, workspaceId: fixture };
const canary = `SYNTHETIC_ONLY_${randomUUID().replaceAll("-", "")}`;
const suffix = randomUUID().replaceAll("-", "").slice(0, 12);
const triggerFunction = `mc140_errors_trigger_${suffix}`;
const queryFunction = `mc140_errors_query_${suffix}`;
const triggerName = `mc140_errors_${suffix}`;
const pool = new pg.Pool({ connectionString: process.env.TIGER_GOVERNANCE_TEST_DATABASE_URL });
const evidenceDir = resolve(process.env.TIGER_GOVERNANCE_ERROR_EVIDENCE_DIR);
const cases: Array<Record<string, unknown>> = [];
let identity: Identity;
let cUserId: string;
let token: string;
let gateway: typeof import("../apps/server/src/service/gateway.js").serviceGateway;
const envKeys = ["NODE_ENV", "LLM_PROVIDER", "SERVICE_C_DEMO_AUTH", "SERVICE_C_SECRET", "SERVICE_C_H5_ENTRY_SECRET", "SERVICE_C_WORKSPACE_ID", "SERVICE_C_WORKSPACE_MAP"] as const;
const originalEnv = Object.fromEntries(envKeys.map(key => [key, process.env[key]]));

function safeLiteral(value: string): string {
  assert.match(value, /^[A-Za-z0-9_-]+$/);
  return `'${value}'`;
}

async function rowCounts(): Promise<{ tickets: number; events: number; drafts: number }> {
  return (await pool.query(`SELECT
    (SELECT count(*) FROM c_tickets WHERE workspace_id=$1)::int tickets,
    (SELECT count(*) FROM biz_events WHERE workspace_id=$1)::int events,
    (SELECT count(*) FROM wizard_staffing_drafts WHERE workspace_id=$1)::int drafts`, [scope.workspaceId])).rows[0];
}

function owner() { return appRouter.createCaller({ identity, session: identity, partnerIdentity: null, headers: new Headers() }); }

async function captureWarnings<T>(fn: () => Promise<T> | T): Promise<{ result: T; warnings: number; containsCanary: boolean; categories: string[]; requestIds: string[] }> {
  const warnings: unknown[][] = [];
  const spy = vi.spyOn(console, "warn").mockImplementation((...args: unknown[]) => { warnings.push(args); });
  try {
    const result = await fn();
    const texts = warnings.map(args => args.map(value => String(value)).join(" "));
    return { result, warnings: warnings.length,
      containsCanary: texts.some(value => value.includes(canary)),
      categories: texts.flatMap(value => [...value.matchAll(/category=([a-z_]+)/g)].map(match => match[1]!)),
      requestIds: texts.flatMap(value => [...value.matchAll(/requestId=([a-f0-9-]{36})/g)].map(match => match[1]!)),
    };
  } finally { spy.mockRestore(); }
}

async function scopedTrigger(table: "c_tickets" | "c_notifications" | "wizard_staffing_drafts", fn: () => Promise<void>): Promise<void> {
  await pool.query(`CREATE TRIGGER ${triggerName} BEFORE INSERT ON ${table} FOR EACH ROW EXECUTE FUNCTION ${triggerFunction}(${safeLiteral(scope.workspaceId)},${safeLiteral(canary)})`);
  try { await fn(); }
  finally { await pool.query(`DROP TRIGGER IF EXISTS ${triggerName} ON ${table}`); }
}

async function queryFault<T>(matches: (sql: string) => boolean, fn: () => Promise<T>): Promise<{ result: T; injectedQueries: number }> {
  const original = pg.Client.prototype.query;
  let injectedQueries = 0;
  const replacement = (function(this: pg.Client, ...args: unknown[]): unknown {
    const sql = typeof args[0] === "string" ? args[0] : "";
    const values = Array.isArray(args[1]) ? args[1] : [];
    if (matches(sql) && values.includes(scope.workspaceId)) {
      injectedQueries += 1;
      // Execute a real PostgreSQL RAISE on this same RLS transaction/client.
      return Reflect.apply(original, this, [`SELECT ${queryFunction}($1)`, [scope.workspaceId]]);
    }
    return Reflect.apply(original, this, args);
  }) as typeof pg.Client.prototype.query;
  const spy = vi.spyOn(pg.Client.prototype, "query").mockImplementation(replacement);
  try { return { result: await fn(), injectedQueries }; }
  finally { spy.mockRestore(); }
}

async function rejection(fn: () => Promise<unknown>): Promise<{ code: string | null; messageContainsCanary: boolean; causeContainsCanary: boolean; message: string }> {
  try { await fn(); }
  catch (error) {
    const data = error as { code?: unknown; message?: unknown; cause?: unknown };
    return { code: typeof data.code === "string" ? data.code : null,
      messageContainsCanary: String(data.message).includes(canary), causeContainsCanary: String(data.cause).includes(canary),
      message: String(data.message).includes(canary) ? "[synthetic canary withheld]" : String(data.message) };
  }
  throw new Error("Fault injection did not reject the actual handler");
}

function caseTest(name: string, details: { preconditions: string; steps: string; expected: string }, fn: (actual: Record<string, unknown>) => Promise<void>) {
  it(name, async () => {
    const actual: Record<string, unknown> = {};
    const started = Date.now();
    try { await fn(actual); cases.push({ name, ...details, actual: "PASS", evidenceLevel: "B", durationMs: Date.now() - started, observations: actual }); }
    catch (error) {
      cases.push({ name, ...details, actual: "FAIL", evidenceLevel: "B", durationMs: Date.now() - started,
        errorType: error instanceof Error ? error.name : "unknown", observations: actual });
      throw error;
    }
  }, 30_000);
}

beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true });
  process.env.LLM_PROVIDER = "mock";
  await seedTrading(pool, { ...scope, workspaceSlug: scope.workspaceId });
  const member = (await pool.query("SELECT id FROM members WHERE workspace_id=$1 AND member_no='MEM-T001'", [scope.workspaceId])).rows[0];
  identity = { kind: "member", memberId: member.id, memberNo: "MEM-T001", name: "隔离故障注入主理人", role: "owner", plan: "pro", ...scope };
  await pool.query(`CREATE FUNCTION ${triggerFunction}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
    IF NEW.workspace_id=TG_ARGV[0] THEN RAISE EXCEPTION USING MESSAGE=TG_ARGV[1]; END IF; RETURN NEW; END $$`);
  await pool.query(`CREATE FUNCTION ${queryFunction}(text) RETURNS void LANGUAGE plpgsql AS $$ BEGIN
    IF $1=${safeLiteral(scope.workspaceId)} THEN RAISE EXCEPTION USING MESSAGE=${safeLiteral(canary)}; END IF; END $$`);
  process.env.NODE_ENV = "production";
  process.env.SERVICE_C_DEMO_AUTH = "false";
  process.env.SERVICE_C_SECRET = "synthetic-mc140-signed-session-secret-32-plus";
  process.env.SERVICE_C_H5_ENTRY_SECRET = "synthetic-mc140-signed-entry-secret-32-plus";
  process.env.SERVICE_C_WORKSPACE_ID = scope.workspaceId;
  delete process.env.SERVICE_C_WORKSPACE_MAP;
  gateway = (await import("../apps/server/src/service/gateway.js")).serviceGateway;
  const entryToken = await issueH5EntryToken({ workspaceKey: scope.workspaceId, subject: "synthetic-error-user", appId: "isolated-errors", secret: process.env.SERVICE_C_H5_ENTRY_SECRET });
  const response = await gateway.request("/session", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ channel: "h5", entryToken }) });
  assert.equal(response.status, 200);
  const session = await response.json() as { token: string; user: { id: string } };
  token = session.token;
  cUserId = session.user.id;
});

afterAll(async () => {
  injectedModel.message = "";
  try {
    for (const table of ["c_tickets", "c_notifications", "wizard_staffing_drafts"]) await pool.query(`DROP TRIGGER IF EXISTS ${triggerName} ON ${table}`);
    await pool.query(`DROP FUNCTION IF EXISTS ${triggerFunction}()`);
    await pool.query(`DROP FUNCTION IF EXISTS ${queryFunction}(text)`);
    const cleanup = (await pool.query(`SELECT
      (SELECT count(*) FROM pg_proc WHERE proname=ANY($1::text[]))::int functions,
      (SELECT count(*) FROM pg_trigger WHERE tgname=$2)::int triggers`, [[triggerFunction, queryFunction], triggerName])).rows[0];
    assert.deepEqual(cleanup, { functions: 0, triggers: 0 });
    await writeFile(resolve(evidenceDir, "tiger-governance-error-cases.json"), JSON.stringify({
      evidenceLevel: "B", sourceAssertionEvidenceLevel: "A", synthetic: true, scope, paidCalls: 0, externalMessages: 0,
      faultScope: "Task-owned synthetic workspace only; real PostgreSQL exceptions and one controlled external-model dependency failure.",
      fixtureRetention: "Synthetic rows and immutable events retained; all temporary functions/triggers removed in finally.", cleanup, cases,
    }, null, 2) + "\n");
  } finally {
    for (const key of envKeys) { if (originalEnv[key] === undefined) delete process.env[key]; else process.env[key] = originalEnv[key]; }
    await closeAllPools();
    await pool.end();
  }
});

describe.sequential("MC026: owned unknown-error exits never release provider/DB text", () => {
  caseTest("dialog.actual-llm-failure-keeps-evidence-without-raw-log", {
    preconditions: "真实PG中知识文档已逐条人审，模型错误由受控依赖注入；无供应商请求",
    steps: "实际handleMessage检索高分知识→模型依赖抛合成canary→读取回答与捕获警告",
    expected: "保留确定性知识回答和引用；警告不含错误正文、响应不含canary",
  }, async (actual) => {
    const collection = await owner().service.kb.createCollection({ name: "故障注入研究知识" });
    const query = "研究来源校验";
    const document = await owner().service.kb.upsertDocument({ collectionId: collection.collection.id, title: query, contentMd: `## ${query}\n研究来源校验须保留原始来源和人工复核记录。` });
    await owner().service.kb.approveDocument({ documentId: document.documentId });
    injectedModel.message = canary;
    injectedModel.calls = 0;
    try {
      const captured = await captureWarnings(() => handleMessage({ workspaceId: scope.workspaceId, cUserId, channel: "h5", text: query }));
      Object.assign(actual, { injectedModelCalls: injectedModel.calls, warningCategories: captured.categories, warnings: captured.warnings, warningContainsCanary: captured.containsCanary,
        responseContainsCanary: JSON.stringify(captured.result).includes(canary), citations: captured.result.citations.length });
      assert.ok(captured.warnings > 0);
      assert.equal(injectedModel.calls, 1);
      assert.ok(captured.result.citations.some(citation => citation.documentTitle === query));
      assert.equal(JSON.stringify(captured.result).includes(canary), false);
      assert.equal(captured.containsCanary, false);
      assert.deepEqual(captured.categories, ["model_answer_failed"]);
    } finally { injectedModel.message = ""; }
  });

  caseTest("gateway.actual-db-failure-keeps-500-and-request-id-without-raw-log", {
    preconditions: "正式签名H5会话；仅该合成工作区c_tickets插入触发真实PG异常",
    steps: "实际POST/tickets→PG RAISE→读取HTTP响应、警告和事务后的业务数量",
    expected: "500+通用错误+requestId；0新工单/事件；警告和响应不含DB错误正文",
  }, async (actual) => {
    const before = await rowCounts();
    await scopedTrigger("c_tickets", async () => {
      const captured = await captureWarnings(() => gateway.request("/tickets", { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${token}` }, body: JSON.stringify({ kind: "other", title: "故障注入建单", idempotencyKey: `${fixture}-db-fail` }) }));
      const body = await captured.result.json() as { error: string; requestId: string };
      Object.assign(actual, { httpStatus: captured.result.status, requestIdPresent: Boolean(body.requestId), warningCategories: captured.categories,
        logRequestIdMatchesResponse: captured.requestIds.includes(body.requestId), warnings: captured.warnings,
        warningContainsCanary: captured.containsCanary, responseContainsCanary: JSON.stringify(body).includes(canary), before, after: await rowCounts() });
      assert.equal(captured.result.status, 500);
      assert.equal(body.error, "服务内部错误");
      assert.match(body.requestId, /^[a-f0-9-]{36}$/);
      assert.deepEqual(await rowCounts(), before);
      assert.equal(JSON.stringify(body).includes(canary), false);
      assert.equal(captured.containsCanary, false);
      assert.deepEqual(captured.categories, ["internal_failure"]);
      assert.ok(captured.requestIds.includes(body.requestId));
    });
  });

  caseTest("gateway.actual-notification-failure-keeps-accepted-ticket-and-failed-delivery", {
    preconditions: "正式签名H5会话；仅该工作区通知插入触发真实PG异常，未配置真实推送驱动",
    steps: "实际建单事务成功→两次通知写入失败→读取HTTP200业务回执与警告",
    expected: "工单和建单事件仍各新增1，delivery=failed；两条警告不含原始DB正文",
  }, async (actual) => {
    const before = await rowCounts();
    await scopedTrigger("c_notifications", async () => {
      const captured = await captureWarnings(() => gateway.request("/tickets", { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${token}` }, body: JSON.stringify({ kind: "other", title: "故障注入通知", idempotencyKey: `${fixture}-notify-fail` }) }));
      const body = await captured.result.json() as { ticket: { id: string }; receipt: { state: string; delivery: { state: string } } };
      const after = await rowCounts();
      Object.assign(actual, { httpStatus: captured.result.status, delivery: body.receipt?.delivery?.state,
        warningCategories: captured.categories, warnings: captured.warnings, warningContainsCanary: captured.containsCanary,
        responseContainsCanary: JSON.stringify(body).includes(canary), before, after });
      assert.equal(captured.result.status, 200);
      assert.equal(body.receipt.state, "accepted");
      assert.equal(body.receipt.delivery.state, "failed");
      assert.equal(after.tickets, before.tickets + 1);
      assert.equal(after.events, before.events + 1);
      assert.ok(captured.warnings >= 2);
      assert.equal(JSON.stringify(body).includes(canary), false);
      assert.equal(captured.containsCanary, false);
      assert.deepEqual(captured.categories, ["delivery_failed", "notification_write_failed"]);
    });
  });

  caseTest("service.generate-staffing-real-db-failure-keeps-code-without-raw-response", {
    preconditions: "合法owner与mock预览；仅该工作区草案插入触发真实PG异常",
    steps: "实际tRPC generateStaffing→真实PG异常→读取拒绝码/正文/cause及回滚后数量",
    expected: "PRECONDITION_FAILED，正文与cause不含canary；草案及事件数量不变",
  }, async (actual) => {
    const before = await rowCounts();
    await scopedTrigger("wizard_staffing_drafts", async () => {
      const result = await rejection(() => owner().service.bundle.generateStaffing({ industryText: "隔离研究故障注入场景" }));
      Object.assign(actual, { ...result, before, after: await rowCounts() });
      assert.equal(result.code, "PRECONDITION_FAILED");
      assert.deepEqual(await rowCounts(), before);
      assert.equal(result.messageContainsCanary, false);
      assert.equal(result.causeContainsCanary, false);
    });
  });

  for (const operation of ["confirm", "exam"] as const) {
    caseTest(`service.${operation}-real-db-failure-keeps-code-without-raw-response`, {
      preconditions: "合法owner；实际handler的目标表查询在同一PG/RLS客户端注入真实RAISE",
      steps: `${operation}实际tRPC调用→目标查询转换为该fixture专用PG RAISE→读取拒绝结果与事务回滚`,
      expected: "实际注入查询至少1次，PRECONDITION_FAILED；正文/cause无canary，无业务状态或事件改动",
    }, async (actual) => {
      const before = await rowCounts();
      const injected = await queryFault(sql => operation === "confirm"
        ? /FROM wizard_staffing_drafts/.test(sql)
        : /FROM bundle_installs[\s\S]*id=\$2/.test(sql),
      () => rejection(() => operation === "confirm"
        ? owner().service.bundle.confirmAndAssembleStaffing({ draftId: `${fixture}-missing-draft`, expectedDraftHash: "0".repeat(64) })
        : owner().service.bundle.onboardingExam({ installId: `${fixture}-missing-install`, expectedAssemblyHash: "0".repeat(64) })));
      Object.assign(actual, { ...injected.result, injectedQueries: injected.injectedQueries, before, after: await rowCounts() });
      assert.ok(injected.injectedQueries > 0);
      assert.equal(injected.result.code, "PRECONDITION_FAILED");
      assert.deepEqual(await rowCounts(), before);
      assert.equal(injected.result.messageContainsCanary, false);
      assert.equal(injected.result.causeContainsCanary, false);
    });
  }

  caseTest("service.mock-preview-keeps-readable-human-gate", {
    preconditions: "合法owner，LLM_PROVIDER=mock且未注入错误；活动trading装配已存在",
    steps: "实际生成模拟编制草案→用返回哈希确认装配→读取错误与PG前后数量",
    expected: "草案诚实标注mock；确认PRECONDITION_FAILED并保留需真实模型的固定提示，无确认写入、无原始cause",
  }, async (actual) => {
    const draft = await owner().service.bundle.generateStaffing({ industryText: "隔离研究模拟编制验证" });
    const before = await rowCounts();
    const result = await rejection(() => owner().service.bundle.confirmAndAssembleStaffing({
      draftId: draft.draftId, expectedDraftHash: draft.draftHash,
    }));
    Object.assign(actual, { mock: draft.mock, ...result, before, after: await rowCounts() });
    assert.equal(draft.mock, true);
    assert.equal(result.code, "PRECONDITION_FAILED");
    assert.equal(result.message, "当前是模拟骨架预览，不能装配上岗；请先接入真实模型并重新生成");
    assert.equal(result.causeContainsCanary, false);
    assert.deepEqual(await rowCounts(), before);
  });

  caseTest("service.missing-target-keeps-readable-precondition", {
    preconditions: "合法owner，未注入任何故障；草案和装配标识均不存在",
    steps: "实际确认不存在草案并对不存在装配开考→读取错误码与状态",
    expected: "两次均PRECONDITION_FAILED，保留固定不存在提示，0业务或事件改动",
  }, async (actual) => {
    const before = await rowCounts();
    const confirm = await rejection(() => owner().service.bundle.confirmAndAssembleStaffing({
      draftId: `${fixture}-missing-normal-draft`, expectedDraftHash: "0".repeat(64),
    }));
    const exam = await rejection(() => owner().service.bundle.onboardingExam({
      installId: `${fixture}-missing-normal-install`, expectedAssemblyHash: "0".repeat(64),
    }));
    Object.assign(actual, { confirm, exam, before, after: await rowCounts() });
    assert.equal(confirm.code, "PRECONDITION_FAILED");
    assert.equal(confirm.message, "编制草案不存在或不属于当前工作区");
    assert.equal(exam.code, "PRECONDITION_FAILED");
    assert.equal(exam.message, "待考的定制装配不存在");
    assert.deepEqual(await rowCounts(), before);
  });
});
