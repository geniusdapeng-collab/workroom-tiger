/** Actual Tiger-owned integration contracts; isolated fixture database and explicit paths are mandatory. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { after, before, test } from "node:test";
import pg from "pg";
import type { Identity, PartnerSessionIdentity } from "@workloom/base/tenancy";
import { closeAllPools, getAppPool, getGatewayPool } from "../packages/db/src/client.js";
import { MAX_CONCURRENT_THREADS } from "@workloom/shared";
import { withObjectLock, ObjectLockTimeout } from "@workloom/base/fence-engine";
import { MockEmbedder, upsertMemory } from "@workloom/base/workdata";
import { issueH5EntryToken } from "../apps/server/src/service/channels.js";
import { seedTrading } from "./seed-trading.js";
import { appRouter } from "../apps/server/src/trpc/router.js";
import type { TrpcContext } from "../apps/server/src/trpc/context.js";
import { applyKbPublishAfterApproval } from "../apps/server/src/service/kb.js";
import { tigerScopeKey } from "../apps/server/src/industry/tiger-runtime.js";
import { runTradingNightly } from "./quest-trading-nightly.js";

if (!process.env.TIGER_GOVERNANCE_TEST_DATABASE_URL || !process.env.DATABASE_URL || !process.env.DATABASE_APP_URL
  || !process.env.DATABASE_GATEWAY_URL || !process.env.TIGER_GOVERNANCE_TEST_EVIDENCE_DIR) {
  throw new Error("Tiger 治理集成测试必须显式配置隔离数据库与证据目录；不猜生产数据库或路径");
}
const pool = new pg.Pool({ connectionString: process.env.TIGER_GOVERNANCE_TEST_DATABASE_URL, connectionTimeoutMillis: 5000 });
const evidenceDir = resolve(process.env.TIGER_GOVERNANCE_TEST_EVIDENCE_DIR);
const fixture = `mc140-gov-${randomUUID().replaceAll("-", "").slice(0, 12)}`;
const scope = { tenantId: fixture, workspaceId: `${fixture}-a` };
let ownerIdentity: Identity;
let deniedIdentity: Identity;
let readonlyIdentity: Identity;
let otherIdentity: Identity;
const cases: Array<Record<string, unknown>> = [];

function context(identity: Identity | null, partner: PartnerSessionIdentity | null = null): TrpcContext {
  return { identity, session: identity ?? partner, partnerIdentity: partner, headers: new Headers() };
}
function caller(identity: Identity | null = ownerIdentity) { return appRouter.createCaller(context(identity)); }
function caseTest(name: string, details: { preconditions: string; steps: string; expected: string }, fn: () => Promise<void>) {
  test(name, { timeout: 600_000 }, async () => {
    const started = Date.now();
    try { await fn(); cases.push({ name, ...details, actual: "PASS", durationMs: Date.now() - started }); }
    catch (error) {
      cases.push({ name, ...details, actual: "FAIL", durationMs: Date.now() - started,
        errorType: error instanceof Error ? error.name : "unknown", code: (error as { code?: string }).code });
      throw error;
    }
  });
}

before(async () => {
  process.env.LLM_PROVIDER = "mock";
  process.env.TIGER_RESEARCH_LLM_MODE = "disabled";
  await mkdir(evidenceDir, { recursive: true });
  await seedTrading(pool, { ...scope, workspaceSlug: `${fixture}-a` });
  const member = (await pool.query<{ id: string }>("SELECT id FROM members WHERE workspace_id=$1 AND member_no='MEM-T001'", [scope.workspaceId])).rows[0]!;
  ownerIdentity = { kind: "member", memberId: member.id, memberNo: "MEM-T001", name: "测试主理人", role: "owner", plan: "pro", ...scope };
  const deny = ["skill.manage", "night.manage", "guardrail.manage", "bundle.manage", "approval.decide", "memory.manage", "agent.manage", "task.dispatch"];
  const memberId = `${fixture}-denied`;
  await pool.query(`INSERT INTO members(id,workspace_id,member_no,name,role,permissions)
    VALUES($1,$2,'MEM-DENY','权限收紧测试主理人','owner',$3)`, [memberId, scope.workspaceId, JSON.stringify({ deny })]);
  deniedIdentity = { ...ownerIdentity, memberId, memberNo: "MEM-DENY" };
  const readonlyId = `${fixture}-readonly`;
  await pool.query("INSERT INTO members(id,workspace_id,member_no,name,role) VALUES($1,$2,'MEM-READ','只读测试成员','readonly')", [readonlyId, scope.workspaceId]);
  readonlyIdentity = { ...ownerIdentity, memberId: readonlyId, memberNo: "MEM-READ", role: "readonly" };
  const otherScope = { tenantId: `${fixture}-other`, workspaceId: `${fixture}-b` };
  await seedTrading(pool, { ...otherScope, workspaceSlug: otherScope.workspaceId });
  const otherMember = (await pool.query<{ id: string }>("SELECT id FROM members WHERE workspace_id=$1 AND member_no='MEM-T001'", [otherScope.workspaceId])).rows[0]!;
  otherIdentity = { ...ownerIdentity, ...otherScope, memberId: otherMember.id };
});

after(async () => {
  const events = (await pool.query<{ n: string }>("SELECT count(*)::text n FROM biz_events WHERE tenant_id=$1", [scope.tenantId])).rows[0]?.n ?? "0";
  await writeFile(resolve(evidenceDir, "tiger-governance-cases.json"), `${JSON.stringify({
    evidenceLevel: "A", synthetic: true, fixture, scope, cases,
    fixtureRetention: "Synthetic task-owned fixture rows and immutable events retained for root chain verification; no customer data or paid calls.",
    immutableEvents: Number(events),
  }, null, 2)}\n`);
  await closeAllPools();
  await pool.end();
});

const deniedDetails = { preconditions: "真实数据库 owner 成员显式 deny 该动作，仍允许 workspace.write", steps: "直调合法形状的 tRPC 请求，目标不存在以避免副作用", expected: "FORBIDDEN，先于业务写入" };
caseTest("permission.skill.manage", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).skills.install({ skillId: "missing-skill" }), { code: "FORBIDDEN" });
});
caseTest("permission.night.manage", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).nightShift.resume({ runId: "missing-night" }), { code: "FORBIDDEN" });
});
caseTest("permission.guardrail.manage", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).fence.confirmDryRun({ dryRunId: "missing-dryrun", rule: {
    ruleId: "R99", name: "测试规则", level: "block", objectTypes: ["report"], actions: ["report.read"], when: "",
  } }), { code: "FORBIDDEN" });
});
caseTest("permission.bundle.manage", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).bundles.activate({ slug: "missing-bundle" }), { code: "FORBIDDEN" });
});
caseTest("permission.approval.decide.im", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).im.sendApprovalCard({ approvalId: "missing-approval", channel: "feishu", conversationId: "synthetic-conversation" }), { code: "FORBIDDEN" });
});
caseTest("permission.approval.decide.kb-active", deniedDetails, async () => {
  await assert.rejects(caller(deniedIdentity).service.kb.setStatus({ documentId: "missing-document", status: "active" }), { code: "FORBIDDEN" });
});

caseTest("kb.modified-content-requires-new-human-review", {
  preconditions: "工作区主理人已逐条审批发布一个真实知识文档", steps: "以同标题更新不同正文；回查状态与客户检索", expected: "新版本回到 pending_review，未经再次人审不能进入客户检索",
}, async () => {
  const col = await caller().service.kb.createCollection({ name: "实际研究规则" });
  const first = await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title: "研究限制", contentMd: "## 研究限制\n原始规则：必须人工复核。" });
  await caller().service.kb.approveDocument({ documentId: first.documentId });
  const second = await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title: "研究限制", contentMd: "## 研究限制\n改写规则：特殊识别词未审新正文。" });
  assert.equal(second.documentId, first.documentId);
  assert.equal(second.version, first.version + 1);
  const row = (await pool.query<{ status: string }>("SELECT status FROM kb_documents WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, first.documentId])).rows[0]!;
  assert.equal(row.status, "pending_review");
  assert.equal((await caller().service.kb.search({ query: "特殊识别词未审新正文" })).hits.length, 0);
});

caseTest("kb.publish-exact-version-and-replay", {
  preconditions: "真实 PG 中一个待审文档", steps: "批准生效、再次批准、再调 setStatus(active)，查询事件及审批快照", expected: "正文版本和 SHA 与人审事件相等，重放不新增事件或审批",
}, async () => {
  const col = await caller().service.kb.createCollection({ name: "人审精确绑定" });
  const doc = await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title: "人审精确绑定", contentMd: "## 人审精确绑定\n客户知识必须逐版审阅。" });
  const first = await caller().service.kb.approveDocument({ documentId: doc.documentId });
  const replay = await caller().service.kb.approveDocument({ documentId: doc.documentId });
  const activeReplay = await caller().service.kb.setStatus({ documentId: doc.documentId, status: "active" });
  assert.equal(first.deduped, false);
  assert.equal(replay.deduped, true);
  assert.equal(activeReplay.deduped, true);
  assert.equal(first.eventId, replay.eventId);
  assert.equal(first.approvalId, activeReplay.approvalId);
  const row = (await pool.query<{ version: number; hash: string; status: string; snapshot: { after: { version: number; content_sha256: string } }; decided_by: string }>(
    `SELECT d.version,d.hash,d.status,a.snapshot,a.decided_by FROM kb_documents d
      JOIN approvals a ON a.workspace_id=d.workspace_id AND a.approval_id=$3
      WHERE d.workspace_id=$1 AND d.id=$2`, [scope.workspaceId, doc.documentId, first.approvalId])).rows[0]!;
  assert.equal(row.status, "active");
  assert.equal(row.snapshot.after.version, row.version);
  assert.equal(row.snapshot.after.content_sha256, row.hash);
  assert.equal(row.decided_by, ownerIdentity.memberNo);
  assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='kb.publish' AND payload->'object'->>'id'=$2", [scope.workspaceId, doc.documentId])).rows[0].n), 1);
});

caseTest("kb.older-approval-cannot-publish-new-content", {
  preconditions: "版本一已发布且保留真实已批审批行，版本二已改写回待审", steps: "重放版本一的发布副作用，再回查版本二检索", expected: "旧 SHA/版本审批不激活新正文，不新增发布事件",
}, async () => {
  const col = await caller().service.kb.createCollection({ name: "旧审批不能复用" });
  const first = await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title: "旧审批不能复用", contentMd: "## 旧审批\n第一版内容已经审阅。" });
  const approved = await caller().service.kb.approveDocument({ documentId: first.documentId });
  await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title: "旧审批不能复用", contentMd: "## 旧审批\n新正文唯一识别串是不可借用旧审阅。" });
  const countBefore = Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  const result = await applyKbPublishAfterApproval(scope, approved.approvalId);
  assert.equal(result, null);
  assert.equal((await pool.query("SELECT status FROM kb_documents WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, first.documentId])).rows[0].status, "pending_review");
  assert.equal((await caller().service.kb.search({ query: "不可借用旧审阅" })).hits.some(hit => hit.documentId === first.documentId), false);
  assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), countBefore);
});

caseTest("kb.concurrent-updates-keep-distinct-review-versions", {
  preconditions: "版本一为待审文档", steps: "真实两个数据库事务并发以相同标题写入两份不同正文", expected: "两个更新分别为版本二、版本三，最终正文待审且索引与正文同版本",
}, async () => {
  const col = await caller().service.kb.createCollection({ name: "知识并发更新" });
  const title = "知识并发更新";
  const first = await caller().service.kb.upsertDocument({ collectionId: col.collection.id, title, contentMd: "## 并发知识\n初始版本。" });
  const updates = await Promise.all(["并发正文甲需要重新审核", "并发正文乙需要重新审核"].map(contentMd => caller().service.kb.upsertDocument({ collectionId: col.collection.id, title, contentMd: `## 并发知识\n${contentMd}` })));
  assert.deepEqual(updates.map(item => item.version).sort(), [2, 3]);
  assert.ok(updates.every(item => item.documentId === first.documentId));
  const row = (await pool.query("SELECT version,status,content_md FROM kb_documents WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, first.documentId])).rows[0];
  assert.equal(row.version, 3);
  assert.equal(row.status, "pending_review");
  const chunks = (await pool.query<{ content: string }>("SELECT content FROM kb_chunks WHERE workspace_id=$1 AND document_id=$2 ORDER BY chunk_index", [scope.workspaceId, first.documentId])).rows;
  assert.ok(chunks.some(item => row.content_md.includes(item.content)));
});

caseTest("kb.missing-target-does-not-create-phantom-event", {
  preconditions: "权限充足但文档标识不存在", steps: "依次启用、停用和批准不存在文档", expected: "三次 NOT_FOUND，事件与审批数量不变",
}, async () => {
  const beforeCount = Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  for (const status of ["active", "disabled"] as const) await assert.rejects(caller().service.kb.setStatus({ documentId: "missing-document", status }), { code: "NOT_FOUND" });
  await assert.rejects(caller().service.kb.approveDocument({ documentId: "missing-document" }), { code: "NOT_FOUND" });
  assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), beforeCount);
});

const fenceRule = { ruleId: "R90", name: "真实候选回放", level: "review" as const, objectTypes: ["report"], actions: ["report.read"], when: 'context.stage == "paper"' };
caseTest("fence.dry-run-candidate-cannot-be-replaced", {
  preconditions: "当前工作区主理人回放一个合法候选规则", steps: "以同 ruleId 将 level/动作/名称改成另一份候选后确认", expected: "BAD_REQUEST，dry-run 仍 pending，未新增规则或审批",
}, async () => {
  const dr = await caller().fence.dryRun(fenceRule);
  await assert.rejects(caller().fence.confirmDryRun({ dryRunId: dr.dryRunId, rule: { ...fenceRule, name: "未回放候选", level: "block", actions: ["report.write"] } }), { code: "BAD_REQUEST" });
  assert.equal((await pool.query("SELECT status FROM fence_dry_runs WHERE id=$1 AND workspace_id=$2", [dr.dryRunId, scope.workspaceId])).rows[0].status, "pending");
  assert.equal(Number((await pool.query("SELECT count(*) n FROM fence_rules WHERE rule_id=$1 AND workspace_id=$2", [fenceRule.ruleId, scope.workspaceId])).rows[0].n), 0);
});

caseTest("fence.dry-run-shows-current-rule-delta", {
  preconditions: "种子规则实际已加载，工作区有真实事件", steps: "回放一个自定义合法候选", expected: "报告包含现行规则差量，不能把放宽方向显示成普通无影响",
}, async () => {
  const dr = await caller().fence.dryRun({ ...fenceRule, ruleId: "R91" });
  assert.ok(dr.report.delta);
  assert.equal(typeof dr.report.delta.summary, "string");
});

caseTest("inspection.disabled-capability-is-explicit", {
  preconditions: "有效签名 Tiger 行业包声明巡检禁用", steps: "请求巡检状态与手动巡检", expected: "状态 enabled=false/bindingState=inspection-disabled，手动操作 PRECONDITION_FAILED，未生成假正常事件",
}, async () => {
  const result = await caller().inspection.status();
  assert.equal((result as unknown as { enabled?: boolean }).enabled, false);
  assert.equal((result as unknown as { bindingState?: string }).bindingState, "inspection-disabled");
  await assert.rejects(caller().inspection.run(), { code: "PRECONDITION_FAILED" });
});

caseTest("im.production-missing-bridge-key-fails-closed", {
  preconditions: "真实 PG、有效 owner、production 进程无 IM_BRIDGE_KEY", steps: "提交合法 IM 入站消息", expected: "UNAUTHORIZED，幂等占位和五元事件均未产生",
}, async () => {
  const original = process.env.NODE_ENV;
  const key = process.env.IM_BRIDGE_KEY;
  process.env.NODE_ENV = "production";
  delete process.env.IM_BRIDGE_KEY;
  const channelMsgId = `${fixture}-missing-key`;
  try {
    await assert.rejects(caller().im.inbound({ channel: "feishu", channelMsgId, conversationId: "synthetic-im", kind: "direct", senderOpenId: "synthetic-sender", text: "隔离测试消息" }), { code: "UNAUTHORIZED" });
    assert.equal(Number((await pool.query("SELECT count(*) n FROM im_inbound_dedupe WHERE workspace_id=$1 AND channel_msg_id=$2", [scope.workspaceId, channelMsgId])).rows[0].n), 0);
  } finally {
    if (original === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = original;
    if (key === undefined) delete process.env.IM_BRIDGE_KEY; else process.env.IM_BRIDGE_KEY = key;
  }
});

async function dispatch(title: string, presetKey: string, research: { provider?: "demo"; market?: "us" | "cn" | "hk"; topN?: number; maxPicks?: number; account?: number; accountCurrency?: "USD" | "CNY" | "HKD"; timeoutSeconds?: number } = {}) {
  const reply = await caller().threads.dispatch({ title, presetKey, research, runImmediately: false });
  if (reply.kind !== "routed") throw new Error("具体研究目标被错误路由为澄清");
  assert.equal(reply.status, "queued");
  return reply.threadId;
}
async function execution(threadId: string): Promise<Record<string, unknown>> {
  const row = (await pool.query<{ result: Record<string, unknown> }>(
    `SELECT payload->'decision'->'after'->'result' AS result FROM biz_events
      WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'kind'='execute'
        AND payload->'decision'->'after'->'result' IS NOT NULL ORDER BY seq DESC LIMIT 1`,
    [scope.workspaceId, threadId])).rows[0];
  assert.ok(row?.result, "工具实际执行事件必须包含内核结果回执");
  return row.result;
}
async function assertKernelArtifacts(result: Record<string, unknown>) {
  const root = process.env.TIGER_RESEARCH_ROOT;
  assert.ok(root, "真实执行矩阵必须显式部署隔离研究根目录");
  assert.match(String(result.resultSha256), /^[a-f0-9]{64}$/);
  const artifacts = result.artifacts as Array<{ name: string; role: string; sha256: string; bytes: number }>;
  assert.ok(artifacts.length > 0);
  const directory = join(root, tigerScopeKey(scope), "jobs", `ws-${tigerScopeKey(scope)}`, String(result.jobId), "artifacts");
  for (const artifact of artifacts) {
    const bytes = await readFile(join(directory, artifact.name));
    assert.equal(bytes.length, artifact.bytes);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), artifact.sha256);
  }
  const canonical = artifacts.find(artifact => artifact.role === "pipeline-result" || artifact.role === "employee-result");
  assert.ok(canonical);
  assert.equal(canonical.sha256, result.resultSha256);
  const governance = result.governance as { synced: boolean; eventIds: string[]; events: number };
  assert.equal(governance.synced, true);
  assert.ok(governance.events > 0);
  assert.equal(governance.eventIds.length, governance.events);
  for (const eventId of governance.eventIds) {
    const event = (await pool.query("SELECT tenant_id,workspace_id,payload FROM biz_events WHERE event_id=$1 AND workspace_id=$2", [eventId, scope.workspaceId])).rows[0];
    assert.equal(event.tenant_id, scope.tenantId);
    assert.equal(event.workspace_id, scope.workspaceId);
    assert.equal(event.payload.decision.action, "tiger.research.observed");
    assert.match(event.payload.decision.after.kernel_record.hash, /^[a-f0-9]{64}$/);
  }
  return { directory, artifacts };
}

caseTest("runtime.actual-scanner-and-idempotent-replay", {
  preconditions: "已签名当前38岗位 fixture、paper/simulated档案、模型禁用、真实PG与Python facade", steps: "dispatch 实际scanner→省略goal续跑→逐文件SHA和DB原始记录核验→再次续跑", expected: "实际岗位 completed，local-kernel/server同步事实分开记录，重放不新增产物执行事件",
}, async () => {
  const threadId = await dispatch("执行美股全市场候选扫描", "universe-scanner", { provider: "demo", market: "us", topN: 2, account: 10000, accountCurrency: "USD", timeoutSeconds: 60 });
  const run = await caller().threads.run({ threadId });
  assert.equal(run.status, "completed");
  assert.ok("stepsDone" in run);
  assert.equal(run.stepsDone, 1);
  const result = await execution(threadId);
  assert.equal(result.state, "verified-research");
  assert.equal(result.kernelStatus, "succeeded");
  assert.equal(result.dataMode, "synthetic");
  assert.equal(result.currency, "USD");
  assert.equal((result.kernelReceipt as { governanceServerSynced: boolean }).governanceServerSynced, false);
  await assertKernelArtifacts(result);
  const countBefore = Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2", [scope.workspaceId, threadId])).rows[0].n);
  assert.equal((await caller().threads.run({ threadId })).status, "completed");
  assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2", [scope.workspaceId, threadId])).rows[0].n), countBefore);
});

caseTest("runtime.actual-mrs-and-currency-binding", {
  preconditions: "有效当前包和真实MRS实现，模型禁用且模拟源未提供A股情绪与微观指标", steps: "以人民币独立研究账户计算A股大盘许可，核验本地SHA与PG记录", expected: "真实MRS降级原因包含缺失情绪/微观指标，线程failed；market=cn/currency=CNY/account=25000保持原请求",
}, async () => {
  const threadId = await dispatch("执行A股大盘许可计算", "mrs", { provider: "demo", market: "cn", account: 25000, accountCurrency: "CNY", timeoutSeconds: 60 });
  const run = await caller().threads.run({ threadId });
  assert.equal(run.status, "failed");
  assert.ok(run.unverified.includes("s1"));
  const result = await execution(threadId);
  assert.equal(result.kernelStatus, "degraded");
  assert.equal(result.state, "unverified-research");
  assert.deepEqual(result.degradedSteps, ["employee.mrs.sent.missing", "employee.mrs.micro.missing"]);
  assert.equal(result.market, "cn");
  assert.equal(result.currency, "CNY");
  assert.equal(result.account, 25000);
  await assertKernelArtifacts(result);
});

caseTest("runtime.disabled-llm-pipeline-is-degraded", {
  preconditions: "默认模型禁用的paper研究；账户风险档案收紧为 .004/.10/.50", steps: "运行实际每日管线并读取实际step-trace与风险快照", expected: "实际21阶段有降级，线程不completed，风险请求和实际快照均不放宽客户预算",
}, async () => {
  const original = (await pool.query("SELECT archive FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0].archive;
  const archive = { ...original, account: { ...original.account, risk_per_trade_pct: .004, max_position_per_ticker_pct: .10, gross_cap_pct: .50 } };
  await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(archive)]);
  try {
    const threadId = await dispatch("执行每日独立研究全管线", "kernel-orchestrator", { provider: "demo", topN: 1, maxPicks: 1, account: 25000, accountCurrency: "USD", timeoutSeconds: 180 });
    const run = await caller().threads.run({ threadId });
    assert.equal(run.status, "failed");
    assert.ok(run.unverified.includes("s1"));
    const result = await execution(threadId);
    assert.equal(result.kernelStatus, "degraded");
    assert.equal(result.state, "unverified-research");
    assert.deepEqual(result.requestedRiskLimits, { risk_r_pct: .004, max_single_position_pct: .10, gross_cap: .50 });
    for (const [key, maximum] of Object.entries(result.requestedRiskLimits as Record<string, number>)) {
      const actual = (result.riskLimits as Record<string, unknown>)[key];
      assert.ok(typeof actual === "number" && Number.isFinite(actual) && actual <= maximum);
    }
    const proof = await assertKernelArtifacts(result);
    const trace = proof.artifacts.find(artifact => artifact.name === "execution_trace.json");
    assert.ok(trace);
    const rows = JSON.parse(await readFile(join(proof.directory, trace.name), "utf8"));
    assert.equal(rows.steps.length, 21);
    assert.ok(rows.degradedSteps.length > 0);
    assert.deepEqual(rows.degradedSteps, result.degradedSteps);
    assert.ok(rows.steps.some((step: { status: string }) => step.status === "passthrough"));
  } finally { await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(original)]); }
});

async function eventCount(workspaceId = scope.workspaceId): Promise<number> {
  return Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1", [workspaceId])).rows[0].n);
}
async function queuedCount(): Promise<number> {
  return Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1 AND status IN ('queued','running')", [scope.workspaceId])).rows[0].n);
}
async function cancelFixtureThreads(ids: string[]): Promise<void> {
  if (ids.length) await pool.query("UPDATE threads SET status='cancelled',updated_at=now() WHERE workspace_id=$1 AND id=ANY($2::text[]) AND status<>'completed'", [scope.workspaceId, ids]);
}

caseTest("matrix.dispatch-permissions-before-effects", {
  preconditions: "真实 PG 中 owner 显式 deny task.dispatch、readonly、游客、伙伴四种身份", steps: "分别调用 dispatch 和 run，随后查询线程与事件数量", expected: "拒绝写入：成员deny/readonly FORBIDDEN，未登录与伙伴 UNAUTHORIZED；没有新线程或事件",
}, async () => {
  const originalCount = Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  const originalEvents = await eventCount();
  const partner: PartnerSessionIdentity = { kind: "partner", partnerId: "synthetic-partner", contactAccountId: "synthetic-contact", name: "隔离测试伙伴", grants: [] };
  for (const [actor, code] of [[caller(deniedIdentity), "FORBIDDEN"], [caller(readonlyIdentity), "FORBIDDEN"], [caller(null), "UNAUTHORIZED"], [appRouter.createCaller(context(null, partner)), "UNAUTHORIZED"]] as const) {
    await assert.rejects(actor.threads.dispatch({ title: "执行隔离权限扫描", presetKey: "universe-scanner", runImmediately: false }), { code });
    await assert.rejects(actor.threads.run({ threadId: "not-owned-thread" }), { code });
  }
  assert.equal(Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), originalCount);
  assert.equal(await eventCount(), originalEvents);
});

caseTest("matrix.research-input-errors-rollback-dispatch", {
  preconditions: "合法 owner 与已验活动包", steps: "空标题、越界topN、跨币种、金额无币种、管线员工参数混用、没有来源SHA的复盘、客户端llmMode与路径字段", expected: "BAD_REQUEST且新线程/派遣事件均不落库",
}, async () => {
  const originalCount = Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  const originalEvents = await eventCount();
  const requests: unknown[] = [
    { title: "", presetKey: "universe-scanner", research: {} },
    { title: "执行输入边界扫描", presetKey: "universe-scanner", research: { topN: 101 } },
    { title: "执行输入边界扫描", presetKey: "universe-scanner", research: { market: "cn", account: 10000, accountCurrency: "USD" } },
    { title: "执行输入边界扫描", presetKey: "universe-scanner", research: { account: 10000 } },
    { title: "执行输入边界扫描", presetKey: "universe-scanner", research: { mode: "daily" } },
    { title: "执行复盘研究", presetKey: "review-chief", research: {} },
    { title: "执行客户端绕过测试", presetKey: "universe-scanner", research: { llmMode: "configured" } },
    { title: "执行客户端路径测试", presetKey: "universe-scanner", research: { workspace: "/tmp/untrusted" } },
  ];
  for (const request of requests) await assert.rejects(caller().threads.dispatch({ ...(request as Record<string, unknown>), runImmediately: false } as never), { code: "BAD_REQUEST" });
  assert.equal(Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), originalCount);
  assert.equal(await eventCount(), originalEvents);
});

caseTest("matrix.unknown-unsupported-disabled-preset", {
  preconditions: "名册有38岗位但仅显式5适配器；另将实际scanner暂时置disabled", steps: "派发未知岗位、未接通bull-researcher，再派发disabled scanner", expected: "未知BAD_REQUEST，未接通PRECONDITION_FAILED，禁用岗位拒绝；没有孤儿线程或事件",
}, async () => {
  const originalCount = Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  const originalEvents = await eventCount();
  await assert.rejects(caller().threads.dispatch({ title: "执行不存在岗位扫描", presetKey: "missing-preset", runImmediately: false }), { code: "BAD_REQUEST" });
  await assert.rejects(caller().threads.dispatch({ title: "执行多头研究分析", presetKey: "bull-researcher", runImmediately: false }), { code: "PRECONDITION_FAILED" });
  const agent = (await pool.query("SELECT id,status FROM agents WHERE workspace_id=$1 AND preset_key='universe-scanner'", [scope.workspaceId])).rows[0];
  await pool.query("UPDATE agents SET status='disabled' WHERE id=$1", [agent.id]);
  try { await assert.rejects(caller().threads.dispatch({ title: "执行已禁用扫描", presetKey: "universe-scanner", runImmediately: false })); }
  finally { await pool.query("UPDATE agents SET status=$2 WHERE id=$1", [agent.id, agent.status]); }
  assert.equal(Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), originalCount);
  assert.equal(await eventCount(), originalEvents);
});

caseTest("matrix.foreign-thread-reads-and-run-are-isolated", {
  preconditions: "两个不同租户的真实工作区；工作区A有已绑定scanner线程", steps: "以B的合法owner读取A线程、读取A事件、尝试执行A线程", expected: "读取null/空，执行NOT_FOUND，A仍queued且B无新事件",
}, async () => {
  const threadId = await dispatch("执行租户隔离扫描", "universe-scanner");
  const beforeB = await eventCount(otherIdentity.workspaceId);
  try {
    assert.equal(await caller(otherIdentity).threads.get({ threadId }), null);
    assert.deepEqual(await caller(otherIdentity).threads.events({ threadId }), []);
    await assert.rejects(caller(otherIdentity).threads.run({ threadId }), { code: "NOT_FOUND" });
    assert.equal((await pool.query("SELECT status FROM threads WHERE id=$1 AND workspace_id=$2", [threadId, scope.workspaceId])).rows[0].status, "queued");
    assert.equal(await eventCount(otherIdentity.workspaceId), beforeB);
  } finally { await cancelFixtureThreads([threadId]); }
});

caseTest("matrix.dispatch-limit-is-atomic-and-ids-are-unique", {
  preconditions: "该隔离工作区无queued/running；运行位上限来自共享常量", steps: "同时发出上限+2个真实dispatch请求，读取失败码和数据库线程状态", expected: "恰好上限个queued，2个TOO_MANY_REQUESTS；线程ID全局唯一且每个线程只有1个真实派遣事件",
}, async () => {
  assert.equal(await queuedCount(), 0);
  const outcomes = await Promise.allSettled(Array.from({ length: MAX_CONCURRENT_THREADS + 2 }, (_, index) =>
    caller().threads.dispatch({ title: `执行并发研究扫描 ${index + 1}`, presetKey: "universe-scanner", runImmediately: false })));
  const accepted = outcomes.filter(outcome => outcome.status === "fulfilled").map(outcome => {
    const result = (outcome as PromiseFulfilledResult<Awaited<ReturnType<ReturnType<typeof caller>["threads"]["dispatch"]>>>).value;
    assert.equal(result.kind, "routed");
    if (result.kind !== "routed") throw new Error("并发派遣未路由");
    return result.threadId;
  });
  try {
    assert.equal(accepted.length, MAX_CONCURRENT_THREADS);
    assert.equal(new Set(accepted).size, accepted.length);
    assert.equal(outcomes.filter(outcome => outcome.status === "rejected" && (outcome.reason as { code?: string }).code === "TOO_MANY_REQUESTS").length, 2);
    assert.equal(await queuedCount(), MAX_CONCURRENT_THREADS);
    for (const threadId of accepted) {
      assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='thread.dispatch'", [scope.workspaceId, threadId])).rows[0].n), 1);
    }
  } finally { await cancelFixtureThreads(accepted); }
});

caseTest("matrix.changed-goal-or-preset-cannot-resume", {
  preconditions: "queued scanner已绑定原目标与岗位", steps: "分别更改请求goal、请求preset，然后修改数据库索引标题后续跑", expected: "请求改动BAD_REQUEST，存储标题变化PRECONDITION_FAILED；均未调用内核或写新工具事件",
}, async () => {
  const title = "执行目标不可替换扫描";
  const threadId = await dispatch(title, "universe-scanner");
  const beforeEvents = await eventCount();
  try {
    await assert.rejects(caller().threads.run({ threadId, goal: "执行另一个不同目标" }), { code: "BAD_REQUEST" });
    await assert.rejects(caller().threads.run({ threadId, presetKey: "mrs" }), { code: "BAD_REQUEST" });
    await pool.query("UPDATE threads SET title='更改后的索引标题' WHERE id=$1 AND workspace_id=$2", [threadId, scope.workspaceId]);
    await assert.rejects(caller().threads.run({ threadId }), { code: "PRECONDITION_FAILED" });
    assert.equal(await eventCount(), beforeEvents);
  } finally { await cancelFixtureThreads([threadId]); }
});

caseTest("matrix.changed-plan-or-profile-fails-before-facade", {
  preconditions: "queued真实绑定scanner线程", steps: "替换持久计划bindingHash后执行；另一线程在档案修改后执行", expected: "计划替换PRECONDITION_FAILED；档案变化failed且无核验内核观察记录，没有完成假回执",
}, async () => {
  const planThread = await dispatch("执行计划不可替换扫描", "universe-scanner");
  const archive = (await pool.query("SELECT archive FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0].archive;
  let profileThread: string | undefined;
  try {
    await pool.query("UPDATE threads SET plan=jsonb_set(plan,'{0,params,binding,bindingHash}',to_jsonb($3::text)) WHERE id=$1 AND workspace_id=$2", [planThread, scope.workspaceId, "0".repeat(64)]);
    await assert.rejects(caller().threads.run({ threadId: planThread }), { code: "PRECONDITION_FAILED" });
    profileThread = await dispatch("执行档案变化拒绝扫描", "universe-scanner");
    await pool.query("UPDATE profiles SET archive=archive||'{\"test_revision\":\"after-dispatch\"}'::jsonb WHERE workspace_id=$1", [scope.workspaceId]);
    const run = await caller().threads.run({ threadId: profileThread });
    assert.equal(run.status, "failed");
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='tiger.research.observed'", [scope.workspaceId, profileThread])).rows[0].n), 0);
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'receipt'->>'synced'='true' AND payload->'decision'->>'kind'='execute'", [scope.workspaceId, profileThread])).rows[0].n), 0);
  } finally {
    await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(archive)]);
    await cancelFixtureThreads([planThread, ...(profileThread ? [profileThread] : [])]);
  }
});

caseTest("matrix.missing-research-root-fails-without-completion", {
  preconditions: "合法排队任务但部署没有TIGER_RESEARCH_ROOT", steps: "删除该部署字段后续跑，读取终态与真实执行事件，再恢复字段", expected: "线程failed、未验证，没有内核产物观察和已同步执行回执",
}, async () => {
  const threadId = await dispatch("执行部署缺失拒绝扫描", "universe-scanner");
  const root = process.env.TIGER_RESEARCH_ROOT;
  delete process.env.TIGER_RESEARCH_ROOT;
  try {
    const run = await caller().threads.run({ threadId });
    assert.equal(run.status, "failed");
    assert.equal(run.stepsDone, 0);
    assert.match((await pool.query("SELECT error FROM threads WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, threadId])).rows[0].error, /TIGER_RESEARCH_ROOT/);
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='tiger.research.observed'", [scope.workspaceId, threadId])).rows[0].n), 0);
  } finally { if (root !== undefined) process.env.TIGER_RESEARCH_ROOT = root; await cancelFixtureThreads([threadId]); }
});

caseTest("matrix.fence-two-approved-versions-and-replay", {
  preconditions: "真实工作区没有自定义R92", steps: "回放/确认review候选、逐条批准生效，再提交更严block候选并批准，重放最后人审", expected: "版本v1、v2两个不同规则行；最终v2 active/v1 rolled_back，重放不新增激活事件",
}, async () => {
  const firstRule = { ...fenceRule, ruleId: "R92" };
  const firstDryRun = await caller().fence.dryRun(firstRule);
  const first = await caller().fence.confirmDryRun({ dryRunId: firstDryRun.dryRunId, rule: firstRule });
  const firstApprovalId = `apr-${first.eventId.toLowerCase()}`;
  assert.equal((await caller().approvals.decide({ approvalId: firstApprovalId, gesture: "approve" })).status, "approved");
  const secondRule = { ...firstRule, name: "第二次更严候选", level: "block" as const };
  const secondDryRun = await caller().fence.dryRun(secondRule);
  const second = await caller().fence.confirmDryRun({ dryRunId: secondDryRun.dryRunId, rule: secondRule });
  const secondApprovalId = `apr-${second.eventId.toLowerCase()}`;
  await caller().approvals.decide({ approvalId: secondApprovalId, gesture: "approve" });
  const rows = (await pool.query<{ id: string; version: string; status: string; level: string }>("SELECT id,version,status,level FROM fence_rules WHERE workspace_id=$1 AND rule_id='R92' ORDER BY version", [scope.workspaceId])).rows;
  assert.deepEqual(rows.map(row => [row.version, row.status, row.level]), [["v1", "rolled_back", "review"], ["v2", "active", "block"]]);
  assert.equal(new Set(rows.map(row => row.id)).size, 2);
  const beforeEvents = await eventCount();
  assert.equal((await caller().approvals.decide({ approvalId: secondApprovalId, gesture: "approve" })).deduped, true);
  assert.equal(await eventCount(), beforeEvents);
});

caseTest("matrix.fence-baseline-relaxation-rolls-back-everything", {
  preconditions: "真实R-T2基线block且未放宽", steps: "以现行条件/覆盖提议review级，确认dry-run后读取三类表", expected: "BAD_REQUEST；dry-run仍pending，无新候选规则、审批、提案事件",
}, async () => {
  const current = (await pool.query("SELECT name,match_spec FROM fence_rules WHERE workspace_id=$1 AND rule_id='R-T2' AND status='active'", [scope.workspaceId])).rows[0];
  const rule = { ruleId: "R-T2", name: current.name, level: "review" as const, objectTypes: current.match_spec.object_types, actions: current.match_spec.actions, when: current.match_spec.when };
  const dryRun = await caller().fence.dryRun(rule);
  const beforeEvents = await eventCount();
  const beforeRules = Number((await pool.query("SELECT count(*) n FROM fence_rules WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  const beforeApprovals = Number((await pool.query("SELECT count(*) n FROM approvals WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
  await assert.rejects(caller().fence.confirmDryRun({ dryRunId: dryRun.dryRunId, rule }), { code: "BAD_REQUEST" });
  assert.equal((await pool.query("SELECT status FROM fence_dry_runs WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, dryRun.dryRunId])).rows[0].status, "pending");
  assert.equal(await eventCount(), beforeEvents);
  assert.equal(Number((await pool.query("SELECT count(*) n FROM fence_rules WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), beforeRules);
  assert.equal(Number((await pool.query("SELECT count(*) n FROM approvals WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), beforeApprovals);
});

caseTest("matrix.fence-current-baseline-drift-needs-new-dry-run", {
  preconditions: "合法待确认R93回放绑定现行围栏SHA", steps: "在隔离fixture内更改现行R-T2名字后确认，finally恢复名字", expected: "BAD_REQUEST且dry-run仍pending，无候选审批",
}, async () => {
  const rule = { ...fenceRule, ruleId: "R93" };
  const dryRun = await caller().fence.dryRun(rule);
  const row = (await pool.query("SELECT id,name FROM fence_rules WHERE workspace_id=$1 AND rule_id='R-T2' AND status='active'", [scope.workspaceId])).rows[0];
  await pool.query("UPDATE fence_rules SET name=$2 WHERE id=$1", [row.id, `${row.name} fixture-drift`]);
  try {
    await assert.rejects(caller().fence.confirmDryRun({ dryRunId: dryRun.dryRunId, rule }), { code: "BAD_REQUEST" });
    assert.equal((await pool.query("SELECT status FROM fence_dry_runs WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, dryRun.dryRunId])).rows[0].status, "pending");
  } finally { await pool.query("UPDATE fence_rules SET name=$2 WHERE id=$1", [row.id, row.name]); }
});

caseTest("matrix.fence-expired-and-tampered-snapshot-cannot-activate", {
  preconditions: "真实提案由回放绑定候选，审批仍pending", steps: "一个快照设为过期后批准；另一个快照修改candidate SHA后批准", expected: "过期BAD_REQUEST且状态expired；篡改快照PRECONDITION_FAILED且不激活规则，不借用其他候选",
}, async () => {
  for (const kind of ["expired", "tampered"] as const) {
    const rule = { ...fenceRule, ruleId: kind === "expired" ? "R94" : "R95" };
    const dryRun = await caller().fence.dryRun(rule);
    const proposal = await caller().fence.confirmDryRun({ dryRunId: dryRun.dryRunId, rule });
    const approvalId = `apr-${proposal.eventId.toLowerCase()}`;
    if (kind === "expired") {
      await pool.query("UPDATE approvals SET snapshot=snapshot||jsonb_build_object('expires_at',$3::text) WHERE workspace_id=$1 AND approval_id=$2", [scope.workspaceId, approvalId, new Date(Date.now() - 1000).toISOString()]);
      await assert.rejects(caller().approvals.decide({ approvalId, gesture: "approve" }), { code: "BAD_REQUEST" });
      assert.equal((await pool.query("SELECT status FROM approvals WHERE workspace_id=$1 AND approval_id=$2", [scope.workspaceId, approvalId])).rows[0].status, "expired");
    } else {
      await pool.query("UPDATE approvals SET snapshot=jsonb_set(snapshot,'{candidate_sha256}',to_jsonb($3::text)) WHERE workspace_id=$1 AND approval_id=$2", [scope.workspaceId, approvalId, "0".repeat(64)]);
      await assert.rejects(caller().approvals.decide({ approvalId, gesture: "approve" }), { code: "PRECONDITION_FAILED" });
    }
    assert.equal((await pool.query("SELECT status FROM fence_rules WHERE workspace_id=$1 AND rule_id=$2", [scope.workspaceId, rule.ruleId])).rows[0].status, "pending_approval");
  }
});

caseTest("matrix.fence-object-lock-contention-and-release", {
  preconditions: "两条真实PG连接竞争该fixture独有对象锁", steps: "第一条持锁时第二条80ms超时，第一事务提交后第二条成功获取", expected: "ObjectLockTimeout只发生于锁等待；提交后锁被释放，业务SELECT正常完成",
}, async () => {
  const key = `mc140:${fixture}:lock`;
  await withObjectLock(getGatewayPool(), key, async client => {
    await assert.rejects(withObjectLock(getGatewayPool(), key, async other => { await other.query("SELECT 1"); }, 80), ObjectLockTimeout);
    await client.query("SELECT 1");
  });
  assert.equal(await withObjectLock(getGatewayPool(), key, async client => (await client.query("SELECT 1 n")).rows[0].n, 1000), 1);
});

caseTest("final.long-goal-reference-and-concurrent-claim", {
  preconditions: "档案保留超过500字真实目标，UI传短标题与goalRef；实际scanner适配器和PG", steps: "派遣后核验绑定全文/标题SHA，再两个调用同时省略goal续跑，核验实际产物与执行事件", expected: "全文不被标题截断；只有一次completed/内核执行，另一次running幂等退出；产物和原始PG观察均真实可核验",
}, async () => {
  const archive = (await pool.query("SELECT archive FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0].archive;
  const goal = `执行美股独立候选扫描。${"研究只用于验证候选与数据来源，不能下单或改变参数。".repeat(25)}`;
  assert.ok(goal.length > 500);
  const title = "执行完整长目标候选扫描";
  await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify({ ...archive, goals: { "fixture-long-goal": { text: goal, version: "v3" } } })]);
  let threadId: string | undefined;
  try {
    const reply = await caller().threads.dispatch({ title, goalRef: "fixture-long-goal", presetKey: "universe-scanner", research: { provider: "demo", topN: 1, account: 10000, accountCurrency: "USD", timeoutSeconds: 60 }, runImmediately: false });
    assert.equal(reply.kind, "routed");
    if (reply.kind !== "routed") throw new Error("长目标没有派遣");
    threadId = reply.threadId;
    const binding = (await pool.query("SELECT plan->0->'params'->'binding' b FROM threads WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, threadId])).rows[0].b;
    assert.equal(binding.goal, goal);
    assert.equal(binding.indexTitle, title);
    const outcomes = await Promise.all([caller().threads.run({ threadId }), caller().threads.run({ threadId })]);
    assert.deepEqual(outcomes.map(run => run.status).sort(), ["completed", "running"]);
    const result = await execution(threadId);
    await assertKernelArtifacts(result);
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'kind'='execute'", [scope.workspaceId, threadId])).rows[0].n), 1);
    const row = (await pool.query("SELECT status FROM threads WHERE workspace_id=$1 AND id=$2", [scope.workspaceId, threadId])).rows[0];
    assert.equal(row.status, "completed");
  } finally {
    await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(archive)]);
    if (threadId) await cancelFixtureThreads([threadId]);
  }
});

caseTest("final.nightly-is-actual-degraded-research", {
  preconditions: "paper/simulated隔离fixture，默认关闭模型", steps: "调用夜班真实组合入口，再读取工具回执、21阶段trace、产物SHA和数据库原始事件", expected: "实际夜班同公共围栏/CAS链；模型缺失则failed而不是completed，核验产物与native观察真实存在",
}, async () => {
  const run = await runTradingNightly({ scope, goal: "执行隔离夜班独立研究管线", research: { provider: "demo", topN: 1, maxPicks: 1, account: 10000, accountCurrency: "USD", timeoutSeconds: 180 } });
  assert.equal(run.status, "failed");
  assert.ok(run.unverified.includes("s1"));
  const result = await execution(run.threadId);
  assert.equal(result.kernelStatus, "degraded");
  const proof = await assertKernelArtifacts(result);
  const trace = proof.artifacts.find(artifact => artifact.name === "execution_trace.json");
  assert.ok(trace);
  assert.equal(JSON.parse(await readFile(join(proof.directory, trace.name), "utf8")).steps.length, 21);
  const event = (await pool.query("SELECT payload FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='thread.dispatch'", [scope.workspaceId, run.threadId])).rows[0].payload;
  assert.equal(event.decision.after.origin, "nightly");
});

caseTest("final.nightly-obeys-workspace-dispatch-limit", {
  preconditions: "上限个queued实际API绑定任务，其中一条已有kernel-orchestrator；临时无研究根目录以避免触发无关内核工作", steps: "拒绝夜班新任务后续跑已有线程，再回查线程与派遣事件数量", expected: "新建TOO_MANY_REQUESTS且无新增线程/派遣事件；已有任务不受新建容量闸阻断，部署缺失真实failed，无内核观察",
}, async () => {
  assert.equal(await queuedCount(), 0);
  const ids: string[] = [];
  const root = process.env.TIGER_RESEARCH_ROOT;
  const resumeGoal = "执行夜班上限既有研究";
  try {
    ids.push(await dispatch(resumeGoal, "kernel-orchestrator"));
    for (let index = 1; index < MAX_CONCURRENT_THREADS; index += 1) ids.push(await dispatch(`执行夜班上限研究扫描 ${index + 1}`, "universe-scanner"));
    const beforeCount = Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n);
    const beforeEvents = await eventCount();
    delete process.env.TIGER_RESEARCH_ROOT;
    await assert.rejects(runTradingNightly({ scope, goal: "执行夜班上限拒绝研究", research: { provider: "demo", timeoutSeconds: 1 } }), { code: "TOO_MANY_REQUESTS" });
    assert.equal(Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), beforeCount);
    assert.equal(await eventCount(), beforeEvents);
    assert.equal(await queuedCount(), MAX_CONCURRENT_THREADS);
    const resumed = await runTradingNightly({ scope, threadId: ids[0], goal: resumeGoal });
    assert.equal(resumed.threadId, ids[0]);
    assert.equal(resumed.status, "failed");
    assert.equal(Number((await pool.query("SELECT count(*) n FROM threads WHERE workspace_id=$1", [scope.workspaceId])).rows[0].n), beforeCount);
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='thread.dispatch'", [scope.workspaceId, ids[0]])).rows[0].n), 1);
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'action'='tiger.research.observed'", [scope.workspaceId, ids[0]])).rows[0].n), 0);
  } finally {
    if (root !== undefined) process.env.TIGER_RESEARCH_ROOT = root;
    await cancelFixtureThreads(ids);
  }
});

caseTest("final.onboarding-secret-version-and-activation-gate", {
  preconditions: "模型mock、未正式激活的真实工作区与新向导草稿", steps: "以合成canary保存mock模型配置；模型配置及向导草稿拒绝凭据URI；检查旧进程与旧草稿回显、磁盘/响应/事件；写草稿并发冲突与非法密钥字段，再尝试正式启用", expected: "秘密和密钥字符尾部不进入磁盘/响应/事件；URL非法先于DB/网络拒绝，旧进程URL显示invalid，旧草稿凭据URI不回显；草稿严格白名单，版本只一份更新；门禁未过仍simulated",
}, async () => {
  const canary = `SYNTHETIC_ONLY_${randomUUID()}`;
  const credentialUrl = new URL("https://model.invalid/v1");
  credentialUrl.username = "user";
  credentialUrl.password = canary;
  const directory = await mkdtemp(join(evidenceDir, "credential-canary-"));
  const originalCwd = process.cwd();
  const keys = ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"] as const;
  const environment = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  try {
    await writeFile(join(directory, "pnpm-workspace.yaml"), "packages: []\n");
    process.chdir(directory);
    const response = await caller().onboarding.saveLlmConfig({ provider: "mock", apiKey: canary });
    assert.equal(response.credentialStorage, "process");
    assert.match(response.restartNotice, /重启后/);
    assert.equal(existsSync(join(directory, ".env")), false);
    assert.equal(JSON.stringify(response).includes(canary), false);
    assert.equal(process.env.LLM_API_KEY, "");
    const events = (await pool.query<{ payload: { decision: { params: Record<string, unknown> } } }>("SELECT payload FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='onboarding.llm_configured'", [scope.workspaceId])).rows;
    assert.ok(events.length > 0);
    assert.equal(JSON.stringify(events).includes(canary), false);
    assert.ok(events.every(event => !Object.hasOwn(event.payload.decision.params, "key_mask")));
    assert.ok(events.every(event => typeof event.payload.decision.params.key_configured === "boolean"));
    for (const baseUrl of [credentialUrl.href, `https://model.invalid/v1?api_key=${canary}`, `https://model.invalid/v1#${canary}`, `file:///tmp/${canary}`]) {
      await assert.rejects(caller().onboarding.saveLlmConfig({ provider: "mock", baseUrl, apiKey: canary }), { code: "BAD_REQUEST" });
      await assert.rejects(caller().onboarding.testLlm({ baseUrl, apiKey: canary, model: "synthetic-only" }), { code: "BAD_REQUEST" });
    }
    assert.equal(Number((await pool.query("SELECT count(*) n FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='onboarding.llm_configured'", [scope.workspaceId])).rows[0].n), events.length);
    process.env.LLM_PROVIDER = "openai-compatible";
    process.env.LLM_BASE_URL = `${credentialUrl.href}?api_key=${canary}#${canary}`;
    const invalidStatus = await caller().onboarding.status();
    assert.equal(invalidStatus.llm.endpointState, "invalid");
    assert.equal(invalidStatus.llm.baseUrl, "");
    assert.equal(invalidStatus.llm.real, false);
    assert.equal(JSON.stringify(invalidStatus).includes(canary), false);
    process.env.LLM_PROVIDER = "mock";
    process.env.LLM_BASE_URL = "";
    const first = await caller().onboarding.saveWizardDraft({ expectedVersion: 0, currentStep: 0, payload: { note: "仅保存可续办的安全字段" } });
    assert.equal(first.version, 1);
    await assert.rejects(caller().onboarding.saveWizardDraft({ expectedVersion: 1, currentStep: 0, payload: { apiKey: canary } } as never), { code: "BAD_REQUEST" });
    const beforeDraftEvents = await eventCount();
    for (const baseUrl of [credentialUrl.href, `https://model.invalid/v1?api_key=${canary}`, `https://model.invalid/v1#${canary}`, `file:///tmp/${canary}`]) {
      await assert.rejects(caller().onboarding.saveWizardDraft({ expectedVersion: 1, currentStep: 0, payload: { baseUrl } }), { code: "BAD_REQUEST" });
    }
    assert.equal(await eventCount(), beforeDraftEvents);
    assert.equal((await caller().onboarding.wizardDraft())?.version, 1);
    await pool.query("UPDATE onboarding_wizard_drafts SET payload=jsonb_set(payload,'{baseUrl}',$2::jsonb,true) WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(credentialUrl.href)]);
    const legacyDraft = await caller().onboarding.wizardDraft();
    assert.equal(legacyDraft?.payload.baseUrl, "");
    assert.equal(JSON.stringify(legacyDraft).includes(canary), false);
    const updates = await Promise.allSettled(["并发草稿甲", "并发草稿乙"].map(note => caller().onboarding.saveWizardDraft({ expectedVersion: 1, currentStep: 1, payload: { note } })));
    assert.equal(updates.filter(update => update.status === "fulfilled").length, 1);
    assert.equal(updates.filter(update => update.status === "rejected").length, 1);
    const current = await caller().onboarding.wizardDraft();
    assert.equal(current?.version, 2);
    assert.equal(JSON.stringify(current).includes(canary), false);
    await assert.rejects(caller().onboarding.activateRealMode(), { code: "PRECONDITION_FAILED" });
    const status = await caller().onboarding.status();
    assert.equal(status.dataMode, "simulated");
    assert.equal(status.formalActivationRecorded, false);
  } finally {
    process.chdir(originalCwd);
    for (const key of keys) { if (environment[key] === undefined) delete process.env[key]; else process.env[key] = environment[key]; }
    await rm(directory, { recursive: true, force: true });
  }
});

caseTest("final.onboarding-business-merge-keeps-risk-budget", {
  preconditions: "客户已有收紧风险档案与自定义经营字段", steps: "保存真实经营主体，回查档案、工作区示例状态与事件", expected: "仅合并business主体字段，不覆盖风险预算与customerNote；仍由正式激活门禁控制dataMode",
}, async () => {
  const archive = (await pool.query("SELECT archive FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0].archive;
  const workspace = (await pool.query("SELECT name,industry,is_example FROM workspaces WHERE id=$1", [scope.workspaceId])).rows[0];
  const tightened = { ...archive, customerNote: "必须保留的客户档案", account: { ...archive.account, risk_per_trade_pct: .004, max_position_per_ticker_pct: .10, gross_cap_pct: .50 }, business: { customerField: "保留" } };
  await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(tightened)]);
  try {
    await caller().onboarding.setupWorkspace({ displayName: "隔离研究主体", industry: "trading", note: "实际测试主体合并" });
    const current = (await pool.query("SELECT archive FROM profiles WHERE workspace_id=$1", [scope.workspaceId])).rows[0].archive;
    assert.deepEqual(current.account, tightened.account);
    assert.equal(current.customerNote, tightened.customerNote);
    assert.equal(current.business.customerField, "保留");
    assert.equal(current.business.name, "隔离研究主体");
    assert.equal(current.dataMode, "simulated");
    assert.equal((await pool.query("SELECT is_example FROM workspaces WHERE id=$1", [scope.workspaceId])).rows[0].is_example, false);
  } finally {
    await pool.query("UPDATE profiles SET archive=$2::jsonb WHERE workspace_id=$1", [scope.workspaceId, JSON.stringify(archive)]);
    await pool.query("UPDATE workspaces SET name=$2,industry=$3,is_example=$4 WHERE id=$1", [scope.workspaceId, workspace.name, workspace.industry, workspace.is_example]);
  }
});

caseTest("final.memory-lifecycle-real-impact-and-isolation", {
  preconditions: "真实PG中一条作用于scanner的活动偏好记忆，来源数据为合成fixture", steps: "预览真实引用→deny成员禁用拒绝→owner禁用→reactivate→再次禁用+restore，另一租户尝试读取与恢复", expected: "影响只含真实绑定员工；每次合法变更同事务留memory.calibrate事件；跨租户无读取/恢复，历史不删除",
}, async () => {
  const agent = (await pool.query("SELECT id FROM agents WHERE workspace_id=$1 AND preset_key='universe-scanner'", [scope.workspaceId])).rows[0];
  const memoryId = `${fixture}-memory-lifecycle`;
  await upsertMemory(getAppPool(), scope, { memoryId, scope: "agent", kind: "preference", content: "研究候选必须显示实际数据来源。", sourceEvents: [], confidence: .7, subjectId: agent.id }, new MockEmbedder());
  const impact = await caller().memory.impact({ memoryId });
  assert.deepEqual(impact.affectedMemoryIds, [memoryId]);
  assert.deepEqual(impact.agents.map(item => item.id), [agent.id]);
  await assert.rejects(caller(deniedIdentity).memory.disable({ memoryId }), { code: "FORBIDDEN" });
  const disabled = await caller().memory.disable({ memoryId });
  assert.ok(disabled.calibrateEventId);
  assert.equal((await pool.query("SELECT status FROM org_memory WHERE workspace_id=$1 AND memory_id=$2", [scope.workspaceId, memoryId])).rows[0].status, "recalled");
  assert.deepEqual((await caller().memory.impact({ memoryId })).affectedMemoryIds, []);
  assert.ok((await caller().memory.reactivate({ memoryId })).calibrateEventId);
  await caller().memory.disable({ memoryId });
  assert.deepEqual((await caller(otherIdentity).memory.impact({ memoryId })).affectedMemoryIds, []);
  assert.deepEqual((await caller(otherIdentity).memory.restore({ memoryIds: [memoryId] })).restored, []);
  const beforeEvents = await eventCount();
  const restored = await caller().memory.restore({ memoryIds: [memoryId, memoryId] });
  assert.deepEqual(restored.restored, [memoryId]);
  assert.equal(restored.calibrateEventIds.length, 1);
  assert.equal(await eventCount(), beforeEvents + 1);
  assert.equal((await pool.query("SELECT status FROM org_memory WHERE workspace_id=$1 AND memory_id=$2", [scope.workspaceId, memoryId])).rows[0].status, "active");
  assert.deepEqual((await caller().memory.restore({ memoryIds: [memoryId] })).restored, []);
});

caseTest("final.im-unsigned-production-and-unknown-driver", {
  preconditions: "production无通道验签secret；真实pending围栏提案；驱动标签为未支持值", steps: "拒收未验签审批回调；请求未知驱动状态与发送卡片", expected: "未验签FORBIDDEN；driver=unavailable/available=false/demo=false，发送PRECONDITION_FAILED且未写假出站事件",
}, async () => {
  const original = process.env.NODE_ENV;
  const secret = process.env.IM_CHANNEL_SECRET_FEISHU;
  const driver = process.env.IM_DRIVER;
  const rule = { ...fenceRule, ruleId: "R96" };
  const dryRun = await caller().fence.dryRun(rule);
  const proposal = await caller().fence.confirmDryRun({ dryRunId: dryRun.dryRunId, rule });
  const approvalId = `apr-${proposal.eventId.toLowerCase()}`;
  const beforeEvents = await eventCount();
  try {
    process.env.NODE_ENV = "production";
    delete process.env.IM_CHANNEL_SECRET_FEISHU;
    await assert.rejects(caller().im.callback({ channel: "feishu", approvalId, conversationId: "synthetic-conversation", operatorOpenId: "synthetic-owner", gesture: "approve" }), { code: "FORBIDDEN" });
    process.env.IM_DRIVER = "unsupported-real-provider";
    assert.deepEqual(await caller().im.channels(), { driver: "unavailable", available: false, demo: false, channels: (await caller().im.channels()).channels });
    await assert.rejects(caller().im.sendApprovalCard({ approvalId, channel: "feishu", conversationId: "synthetic-conversation" }), { code: "PRECONDITION_FAILED" });
    assert.equal(await eventCount(), beforeEvents);
    assert.equal((await pool.query("SELECT status FROM approvals WHERE workspace_id=$1 AND approval_id=$2", [scope.workspaceId, approvalId])).rows[0].status, "pending");
  } finally {
    if (original === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = original;
    if (secret === undefined) delete process.env.IM_CHANNEL_SECRET_FEISHU; else process.env.IM_CHANNEL_SECRET_FEISHU = secret;
    if (driver === undefined) delete process.env.IM_DRIVER; else process.env.IM_DRIVER = driver;
  }
});

caseTest("final.service-gateway-signed-session-ticket-idempotency", {
  preconditions: "真实PG、生产认证网关、fixture专用32字节以上签名密钥；没有真实推送驱动", steps: "签名H5入口→合法会话→同键两次建单→另一个用户查询，非法参数与无token拒绝", expected: "有效会话只来自签名租户；同键只1工单/1事件，推送demo；另一用户404，非法请求400/401；不出现酒店样例业务数据",
}, async () => {
  const keys = ["NODE_ENV", "SERVICE_C_DEMO_AUTH", "SERVICE_C_SECRET", "SERVICE_C_H5_ENTRY_SECRET", "SERVICE_C_WORKSPACE_ID", "SERVICE_C_WORKSPACE_MAP"] as const;
  const environment = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  process.env.NODE_ENV = "production";
  process.env.SERVICE_C_DEMO_AUTH = "false";
  process.env.SERVICE_C_SECRET = `SYNTHETIC_C_SESSION_${randomUUID()}`;
  process.env.SERVICE_C_H5_ENTRY_SECRET = `SYNTHETIC_H5_ENTRY_${randomUUID()}`;
  delete process.env.SERVICE_C_WORKSPACE_ID;
  process.env.SERVICE_C_WORKSPACE_MAP = JSON.stringify({ [fixture]: scope.workspaceId });
  try {
    const { serviceGateway } = await import("../apps/server/src/service/gateway.js");
    const entryToken = await issueH5EntryToken({ workspaceKey: fixture, subject: "synthetic-user-a", appId: "fixture-front", secret: process.env.SERVICE_C_H5_ENTRY_SECRET! });
    const session = await serviceGateway.request("/session", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ channel: "h5", entryToken, workspaceKey: fixture }) });
    assert.equal(session.status, 200);
    const body = await session.json() as { token: string; user: { id: string; workspaceId: string; authMode: string } };
    assert.equal(body.user.workspaceId, scope.workspaceId);
    assert.equal(body.user.authMode, "channel");
    const headers = { "content-type": "application/json", authorization: `Bearer ${body.token}` };
    const ticketInput = { kind: "consult", title: "研究资料复核", payload: { reason: "需要人工协助" }, idempotencyKey: `${fixture}-ticket` };
    const firstResponse = await serviceGateway.request("/tickets", { method: "POST", headers, body: JSON.stringify(ticketInput) });
    assert.equal(firstResponse.status, 200);
    const first = await firstResponse.json() as { ticket: { id: string; statusText: string; dept: string }; receipt: { eventId: string; delivery: { state: string } } };
    assert.equal(first.ticket.statusText, "已受理");
    assert.equal(first.ticket.dept, "合规组");
    assert.equal(first.receipt.delivery.state, "demo");
    const beforeEvents = await eventCount();
    const replayResponse = await serviceGateway.request("/tickets", { method: "POST", headers, body: JSON.stringify(ticketInput) });
    assert.equal(replayResponse.status, 200);
    const replay = await replayResponse.json() as { ticket: { id: string }; receipt: { idempotentReplay: boolean } };
    assert.equal(replay.ticket.id, first.ticket.id);
    assert.equal(replay.receipt.idempotentReplay, true);
    assert.equal(await eventCount(), beforeEvents);
    const ordersResponse = await serviceGateway.request("/orders", { headers });
    assert.equal(ordersResponse.status, 200);
    assert.deepEqual(await ordersResponse.json(), { orders: [], demo: false, available: false });
    const entryB = await issueH5EntryToken({ workspaceKey: fixture, subject: "synthetic-user-b", appId: "fixture-front", secret: process.env.SERVICE_C_H5_ENTRY_SECRET! });
    const sessionB = await serviceGateway.request("/session", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ channel: "h5", entryToken: entryB }) });
    const bodyB = await sessionB.json() as { token: string };
    assert.equal((await serviceGateway.request(`/tickets/${first.ticket.id}`, { headers: { authorization: `Bearer ${bodyB.token}` } })).status, 404);
    assert.equal((await serviceGateway.request("/tickets", { method: "POST", headers, body: JSON.stringify({ ...ticketInput, kind: "untrusted-kind" }) })).status, 400);
    assert.equal((await serviceGateway.request("/tickets", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(ticketInput) })).status, 401);
  } finally { for (const key of keys) { if (environment[key] === undefined) delete process.env[key]; else process.env[key] = environment[key]; } }
});
