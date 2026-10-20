import { randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { appRouter } from "./router.js";
import { validateStaffing } from "../service/bundle.js";
import { staffingDraftHash } from "../service/onboarding-truth.js";

const RUN_DB = process.env.RUN_FRONTEND_CONTRACT_PG_TESTS === "1";
const envKeys = ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"] as const;
function caller(identity: Identity | null) {
  return appRouter.createCaller({ session: identity, identity, partnerIdentity: null, headers: new Headers() });
}

describe.skipIf(!RUN_DB)("前端契约接线（真实 tRPC / PostgreSQL，模型仅本机 HTTP 夹具）", () => {
  let db: pg.Pool;
  let identity: Identity;
  let server: Server;
  let modelUrl: string;
  let sandbox: string;
  let previousCwd: string;
  let probeFails = false;
  const originalEnv = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
  const restoreEnv = () => {
    for (const key of envKeys) {
      const value = originalEnv[key];
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  };
  const draftInput = { expectedVersion: 0, currentStep: 1, payload: { businessName: "本机契约测试夹具", model: "fixture-model" } };

  async function makeIdentity(): Promise<Identity> {
    const id = randomUUID();
    const tenantId = `mc012-t-${id}`, workspaceId = `mc012-w-${id}`, memberId = `mc012-m-${id}`;
    await db.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'本机测试租户','pro')", [tenantId]);
    await db.query("INSERT INTO workspaces (id,tenant_id,slug,name,industry) VALUES ($1,$2,$1,'本机测试区','fixture')", [workspaceId, tenantId]);
    await db.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-FRONT','测试责任人','owner')", [memberId, workspaceId]);
    await db.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'fixture','{}')", [workspaceId, tenantId]);
    return { memberId, memberNo: "MEM-FRONT", name: "测试责任人", role: "owner", tenantId, workspaceId, plan: "pro" };
  }
  async function events(who = identity) {
    return (await db.query("SELECT event_id,session_id,payload FROM biz_events WHERE workspace_id=$1 ORDER BY seq", [who.workspaceId])).rows;
  }
  async function memory(status = "recalled", who = identity) {
    const id = `mc012-memory-${randomUUID()}`;
    await db.query("INSERT INTO org_memory (memory_id,tenant_id,workspace_id,scope,kind,content,status) VALUES ($1,$2,$3,'workspace','preference','使用有来源的资料',$4)", [id, who.tenantId, who.workspaceId, status]);
    return id;
  }
  async function thread(who = identity) {
    const id = `mc012-thread-${randomUUID()}`;
    await db.query("INSERT INTO threads (id,tenant_id,workspace_id,title,mode,status,created_by) VALUES ($1,$2,$3,'分析当前研究资料','quest','paused',$4)", [id, who.tenantId, who.workspaceId, who.memberNo]);
    return id;
  }
  async function saveModel() {
    return caller(identity).onboarding.saveLlmConfig({ provider: "openai", baseUrl: modelUrl, model: "fixture-model", apiKey: "local-fixture-key" });
  }
  async function readyFixture() {
    // 显式离线测试夹具，不是实际行业包上线或外部模型验收证据。
    await saveModel();
    await caller(identity).onboarding.setupWorkspace({ displayName: "用户确认的测试主体", industry: "fixture", note: "离线验收夹具" });
    const agentId = `mc012-agent-${randomUUID()}`, fenceId = `mc012-fence-${randomUUID()}`, installId = `mc012-install-${randomUUID()}`;
    await db.query("INSERT INTO agents (id,workspace_id,preset_key,name,version,kind,status) VALUES ($1,$2,'fixture-reader','夹具岗位','1','analyst','ready')", [agentId, identity.workspaceId]);
    await db.query("INSERT INTO fence_rules (id,rule_id,version,workspace_id,name,level,match_spec,action,status,created_by) VALUES ($1,'RF12','1',$2,'夹具围栏','review','{}','{}','active',$3)", [fenceId, identity.workspaceId, identity.memberNo]);
    await db.query("INSERT INTO bundle_installs (id,workspace_id,bundle_id,assets,status) VALUES ($1,$2,'fixture',$3,'active')", [installId, identity.workspaceId, JSON.stringify({ preset_ids: [agentId], fence_rule_ids: [fenceId], skill_ids: [] })]);
    await db.query("UPDATE workspaces SET bundle_id='fixture',is_example=false WHERE id=$1", [identity.workspaceId]);
    return { agentId, fenceId, installId };
  }
  async function withEventFailure(run: () => Promise<void>) {
    const name = `mc012_fail_${randomUUID().replaceAll("-", "")}`;
    await db.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'MC012 injected event failure'; END $$`);
    try {
      await db.query(`CREATE TRIGGER ${name} BEFORE INSERT ON biz_events FOR EACH ROW WHEN (NEW.workspace_id='${identity.workspaceId}') EXECUTE FUNCTION ${name}()`);
      await run();
    } finally {
      await db.query(`DROP TRIGGER IF EXISTS ${name} ON biz_events`);
      await db.query(`DROP FUNCTION ${name}()`);
    }
  }
  async function qualifiedCustomFixture() {
    // 仅构造既有考试服务结束后的数据库状态，测试激活入口对资格证据的绑定。
    // 不调用付费模型，也不将这些显式夹具记为真实岗位考试通过。
    await saveModel();
    await caller(identity).onboarding.setupWorkspace({ displayName: "定制资格测试主体", industry: "fixture", note: "显式离线夹具" });
    const draftId = `mc012-draft-${randomUUID()}`;
    const industryText = "仅本机资格绑定回归";
    const compiled = validateStaffing({
      team: [{ preset_key: "fixture-reader", role_title: "夹具研究员", kind: "analyst", description: "只读核实资料",
        readonly: true, night_shift: false, fence_bindings: ["RF12"], skills: [] }],
      fences: [{ rule_id: "RF12", name: "夹具资料变更人审", level: "review", match: { object_types: ["research"], actions: ["research.change"] }, when: "", match_desc: "资料变更" }],
      skills_suggested: [],
    });
    const draftHash = staffingDraftHash(industryText, compiled);
    await db.query("INSERT INTO wizard_staffing_drafts (id,workspace_id,industry_text,compiled,status,generation_mode,draft_hash) VALUES ($1,$2,$3,$4,'draft','real',$5)", [draftId, identity.workspaceId, industryText, JSON.stringify(compiled), draftHash]);
    const assembly = await caller(identity).service.bundle.confirmAndAssembleStaffing({ draftId, expectedDraftHash: draftHash });
    const assets = (await db.query("SELECT assets FROM bundle_installs WHERE id=$1", [assembly.installId])).rows[0].assets as { preset_ids: string[]; fence_rule_ids: string[] };
    const examId = `mc012-exam-${randomUUID()}`;
    await db.query(`INSERT INTO eval_exams (id,workspace_id,exam_type,trigger_source,status,verdict,total_score,assessment_kind,target_install_id,target_draft_id,target_version,target_hash)
      VALUES ($1,$2,'onboarding','wizard','done','pass',100,'candidate-role',$3,$4,$5,$6)`, [examId, identity.workspaceId, assembly.installId, draftId, assembly.assemblyVersion, assembly.assemblyHash]);
    await db.query(`INSERT INTO eval_candidate_results (id,workspace_id,exam_id,install_id,draft_id,assembly_version,assembly_hash,agent_id,role_title,question,dimension_scores,passed)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'夹具研究员','{}','{}',true)`, [`mc012-answer-${randomUUID()}`, identity.workspaceId, examId, assembly.installId, draftId, assembly.assemblyVersion, assembly.assemblyHash, assets.preset_ids[0]]);
    await db.query("UPDATE agents SET status='ready' WHERE workspace_id=$1 AND id=ANY($2)", [identity.workspaceId, assets.preset_ids]);
    await db.query("UPDATE fence_rules SET status='active' WHERE workspace_id=$1 AND id=ANY($2)", [identity.workspaceId, assets.fence_rule_ids]);
    await db.query("UPDATE wizard_staffing_drafts SET status='active' WHERE id=$1", [draftId]);
    await db.query("UPDATE bundle_installs SET status='active',qualified_exam_id=$2 WHERE id=$1", [assembly.installId, examId]);
    await db.query("UPDATE workspaces SET bundle_id='custom',is_example=false WHERE id=$1", [identity.workspaceId]);
    return { ...assembly, draftId, examId, agentId: assets.preset_ids[0]!, fenceId: assets.fence_rule_ids[0]! };
  }

  beforeAll(async () => {
    const urls = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"].map((key) => new URL(process.env[key] ?? "http://invalid"));
    if (urls.some((url) => !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.port !== "55432" || !["postgres:", "postgresql:"].includes(url.protocol)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname) || url.pathname !== urls[0]!.pathname)) {
      throw new Error("只允许同一 workloom_tiger_ 前缀的本机隔离测试库，三种数据库角色必须指向该库");
    }
    db = new pg.Pool({ connectionString: process.env.DATABASE_URL });
    expect((await getAppPool().query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
      .toEqual({ current_user: "workloom_app", rolsuper: false, rolbypassrls: false });
    previousCwd = process.cwd();
    sandbox = mkdtempSync(join(tmpdir(), "workloom-mc012-"));
    writeFileSync(join(sandbox, "pnpm-workspace.yaml"), "packages: []\n");
    process.chdir(sandbox); // 模型保存只能写测试目录，不能改运行中的治理服务 .env。
    server = createServer((req, res) => {
      req.resume();
      res.writeHead(probeFails ? 500 : 200, { "content-type": "application/json" });
      res.end(JSON.stringify(probeFails ? { error: { message: "fixture refusal" } } : { choices: [{ message: { role: "assistant", content: "本机 HTTP 夹具响应，不代表真实模型验收" } }], usage: { prompt_tokens: 1, completion_tokens: 1 } }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("本机 HTTP 夹具未启动");
    modelUrl = `http://127.0.0.1:${address.port}/v1`;
  });
  beforeEach(async () => { restoreEnv(); probeFails = false; identity = await makeIdentity(); });
  afterAll(async () => {
    restoreEnv();
    if (previousCwd) process.chdir(previousCwd);
    await Promise.all([server ? new Promise<void>((resolve) => server.close(() => resolve())) : Promise.resolve(), db?.end(), closeAllPools()]);
    if (sandbox) rmSync(sandbox, { recursive: true, force: true });
  });

  it("草稿保存、刷新与完成接入真实服务，版本冲突不能覆盖", async () => {
    expect(await caller(identity).onboarding.wizardDraft()).toBeNull();
    const saved = await caller(identity).onboarding.saveWizardDraft(draftInput);
    expect(saved).toMatchObject({ version: 1, currentStep: 1, payload: draftInput.payload, responsible: { memberId: identity.memberId } });
    expect(await caller(identity).onboarding.wizardDraft()).toEqual(saved);
    await expect(caller(identity).onboarding.saveWizardDraft(draftInput)).rejects.toMatchObject({ code: "CONFLICT", message: expect.stringMatching(/版本|更新/) });
    expect((await events()).map((e) => e.payload.decision.action)).toEqual(["onboarding.draft.saved"]);
    const completed = await caller(identity).onboarding.completeWizardDraft({ expectedVersion: 1 });
    expect(completed).toMatchObject({ status: "completed", version: 2 });
    expect((await events())[1].payload.who.id).toBe(identity.memberNo);
    await expect(caller(identity).onboarding.saveWizardDraft({ ...draftInput, expectedVersion: 2 })).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    await expect(caller(identity).onboarding.completeWizardDraft({ expectedVersion: 2 })).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
  });
  it("草稿并发只接受同版本的一次更新，外区读不到", async () => {
    await caller(identity).onboarding.saveWizardDraft(draftInput);
    const results = await Promise.allSettled([2, 3].map((currentStep) => caller(identity).onboarding.saveWizardDraft({ ...draftInput, expectedVersion: 1, currentStep })));
    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
    expect(await caller(await makeIdentity()).onboarding.wizardDraft()).toBeNull();
  });
  it("草稿秘密字段拒收，事件失败时草稿不残留", async () => {
    await expect(caller(identity).onboarding.saveWizardDraft({ ...draftInput, payload: { ...draftInput.payload, apiKey: "do-not-save" } } as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await withEventFailure(async () => {
      await expect(caller(identity).onboarding.saveWizardDraft(draftInput)).rejects.toThrow();
      expect(await caller(identity).onboarding.wizardDraft()).toBeNull();
    });
  });
  it("格式损坏的技能声明不能被当成空技能而通过门禁", async () => {
    const fixture = await readyFixture();
    await db.query("UPDATE bundle_installs SET assets=jsonb_set(assets,'{skill_ids}','[42]') WHERE id=$1", [fixture.installId]);
    expect((await caller(identity).onboarding.status()).activationGate.canActivate).toBe(false);
    await expect(caller(identity).onboarding.activateRealMode()).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
  });
  it("发布事实按当前工作区计算，不凭前端占位宣称上线", async () => {
    const result = await caller(identity).onboarding.serviceFrontPublication();
    expect(result).toMatchObject({ overall: "blocked", workspaceRoutingReady: false, qrAvailable: false });
  });
  it("记忆影响预览、单条和批量恢复使用真实状态与审计事件", async () => {
    const active = await memory("active"), first = await memory(), second = await memory();
    expect((await caller(identity).memory.impact({ memoryIds: [active, first] })).affectedMemoryIds).toEqual([active]);
    const one = await caller(identity).memory.reactivate({ memoryId: first });
    const batch = await caller(identity).memory.restore({ memoryIds: [second, second] });
    expect(batch.restored).toEqual([second]);
    expect((await events()).map((e) => e.event_id)).toEqual([one.calibrateEventId, ...batch.calibrateEventIds]);
    expect((await db.query("SELECT status FROM org_memory WHERE memory_id=ANY($1)", [[first, second]])).rows.every((r) => r.status === "active")).toBe(true);
  });
  it("外区记忆不可查看或恢复，事件失败不能复活记忆", async () => {
    const local = await memory(), external = await memory("active", await makeIdentity());
    expect((await caller(identity).memory.impact({ memoryIds: [external] })).affectedMemoryIds).toEqual([]);
    await expect(caller(identity).memory.reactivate({ memoryId: external })).rejects.toMatchObject({ code: "NOT_FOUND" });
    expect((await caller(identity).memory.restore({ memoryIds: [external] })).restored).toEqual([]);
    await withEventFailure(async () => { await expect(caller(identity).memory.reactivate({ memoryId: local })).rejects.toThrow(); });
    expect((await db.query("SELECT status FROM org_memory WHERE memory_id=$1", [local])).rows[0].status).toBe("recalled");
  });
  it("覆盖层挂载现有流水线，持久化草稿可回读且外区不可见", async () => {
    const saved = await caller(identity).overlay.l1Intake({ baseBundle: "fixture", baseVersion: "1.0.0", intents: [{ kind: "faq", question: "营业时间", answer: "仅测试夹具" }] });
    expect(saved).toBeTruthy();
    expect(await caller(identity).overlay.versions({ baseBundle: "fixture" })).toHaveLength(1);
    expect(await caller(await makeIdentity()).overlay.versions({ baseBundle: "fixture" })).toEqual([]);
    expect(await caller(identity).overlay.myStatus({ baseBundle: "fixture" })).toMatchObject({ hasOverlay: false });
  });
  it("线程留言写入原线程事件流，不改目标、进度或执行状态", async () => {
    const id = await thread();
    const result = await caller(identity).threads.note({ threadId: id, text: "备注：保留原始资料引用" });
    const rows = await caller(identity).threads.events({ threadId: id });
    expect(result.threadId).toBe(id);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ who: { id: identity.memberNo }, decision: { action: "thread.note", after: { text: "备注：保留原始资料引用" } } });
    expect(await caller(identity).threads.get({ threadId: id })).toMatchObject({ title: "分析当前研究资料", status: "paused", progress_done: 0 });
    expect((await events())[0].session_id).toBe(id);
  });
  it("线程留言拒绝缺失、跨区和空白输入，失败不返回成功回执", async () => {
    const external = await thread(await makeIdentity()), local = await thread();
    await expect(caller(identity).threads.note({ threadId: external, text: "备注" })).rejects.toMatchObject({ code: "NOT_FOUND" });
    await expect(caller(identity).threads.note({ threadId: "missing", text: "备注" })).rejects.toMatchObject({ code: "NOT_FOUND" });
    await expect(caller(identity).threads.note({ threadId: local, text: "   " })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await withEventFailure(async () => { await expect(caller(identity).threads.note({ threadId: local, text: "备注" })).rejects.toThrow(); });
    expect(await events()).toEqual([]);
  });
  it("未认证请求不能访问新读入口", async () => {
    await expect(caller(null).onboarding.wizardDraft()).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await expect(caller(null).memory.impact({ memoryIds: [] })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await expect(caller(null).overlay.health()).rejects.toMatchObject({ code: "UNAUTHORIZED" });
  });
  it.each(["readonly", "staff"] as const)("%s 配置与记忆写入遵循既有权限，旧 JWT 不能放宽", async (role) => {
    const id = await memory();
    await db.query("UPDATE members SET role=$2 WHERE id=$1", [identity.memberId, role]);
    await expect(caller(identity).onboarding.saveWizardDraft(draftInput)).rejects.toMatchObject({ code: "FORBIDDEN" });
    if (role === "readonly") {
      await expect(caller(identity).memory.reactivate({ memoryId: id })).rejects.toMatchObject({ code: "FORBIDDEN" });
    } else {
      // 既有 STAFF_ACTIONS 包含 memory.manage，不能为让测试通过而收紧产品权限。
      expect((await caller(identity).memory.reactivate({ memoryId: id })).calibrateEventId).toBeTruthy();
    }
    await expect(caller(identity).overlay.l1Intake({ baseBundle: "fixture", baseVersion: "1", intents: [{ kind: "faq", question: "问题", answer: "回答" }] })).rejects.toMatchObject({ code: "FORBIDDEN" });
  });
  it("显式 deny 和游客令牌不能追加线程留言", async () => {
    const id = await thread();
    await db.query("UPDATE members SET permissions=$2 WHERE id=$1", [identity.memberId, JSON.stringify({ deny: ["task.dispatch"] })]);
    await expect(caller(identity).threads.note({ threadId: id, text: "备注" })).rejects.toMatchObject({ code: "FORBIDDEN" });
    await expect(caller({ ...identity, memberNo: "GUEST" }).threads.note({ threadId: id, text: "备注" })).rejects.toMatchObject({ code: "FORBIDDEN" });
  });
  it("空库和纯环境变量模型配置不能直接激活 real", async () => {
    process.env.LLM_PROVIDER = "openai"; process.env.LLM_BASE_URL = modelUrl; process.env.LLM_MODEL = "fixture-model";
    const before = await events();
    await expect(caller(identity).onboarding.activateRealMode()).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    expect((await caller(identity).onboarding.status()).activationGate.canActivate).toBe(false);
    expect((await caller(identity).onboarding.status()).llm.real).toBe(false);
    expect((await caller(identity).onboarding.status()).dataMode).toBe("simulated");
    expect(await events()).toEqual(before);
  });
  it("测试成功才绑定模型凭据；失败不能保存配置或提升状态", async () => {
    await saveModel();
    expect((await caller(identity).onboarding.status()).llm.real).toBe(true);
    const file = readFileSync(join(sandbox, ".env"), "utf8");
    probeFails = true;
    await expect(caller(identity).onboarding.saveLlmConfig({ provider: "openai", baseUrl: modelUrl, model: "bad-fixture", apiKey: "not-saved" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(readFileSync(join(sandbox, ".env"), "utf8")).toBe(file);
    process.env.LLM_API_KEY = "changed-fixture-key";
    expect((await caller(identity).onboarding.status()).llm.real).toBe(false);
  });
  it("满足既有资格的显式夹具可以激活，草稿仅推进且同事务留痕", async () => {
    await readyFixture();
    await caller(identity).onboarding.saveWizardDraft(draftInput);
    const status = await caller(identity).onboarding.status();
    expect(status.business).toMatchObject({ name: "用户确认的测试主体", industry: "fixture", source: "user" });
    expect(status.activationGate.canActivate).toBe(true);
    await caller(identity).onboarding.activateRealMode();
    expect((await caller(identity).onboarding.status()).dataMode).toBe("real");
    expect(await caller(identity).onboarding.wizardDraft()).toMatchObject({ status: "draft", currentStep: 4, version: 2 });
    expect((await events()).at(-1)?.payload.decision.action).toBe("onboarding.real_mode_activated");
  });
  it.each(["owner", "business", "example", "agents", "fences", "install", "llm", "exam"])("%s 事实不满足时启用失败且模式不变", async (kind) => {
    const fixture = await readyFixture();
    if (kind === "owner") await db.query("UPDATE members SET role='manager' WHERE id=$1", [identity.memberId]);
    if (kind === "business") await db.query("UPDATE profiles SET archive=jsonb_set(archive,'{business,source}','\"seed\"') WHERE workspace_id=$1", [identity.workspaceId]);
    if (kind === "example") await db.query("UPDATE workspaces SET is_example=true WHERE id=$1", [identity.workspaceId]);
    if (kind === "agents") await db.query("UPDATE agents SET status='disabled' WHERE id=$1", [fixture.agentId]);
    if (kind === "fences") await db.query("UPDATE fence_rules SET status='rolled_back' WHERE id=$1", [fixture.fenceId]);
    if (kind === "install") await db.query("UPDATE workspaces SET bundle_id='other' WHERE id=$1", [identity.workspaceId]);
    if (kind === "llm") process.env.LLM_MODEL = "other-config";
    if (kind === "exam") {
      await db.query("UPDATE bundle_installs SET bundle_id='custom' WHERE id=$1", [fixture.installId]);
      await db.query("UPDATE workspaces SET bundle_id='custom' WHERE id=$1", [identity.workspaceId]);
    }
    const before = await events();
    expect((await caller(identity).onboarding.status()).activationGate.canActivate).toBe(false);
    await expect(caller(identity).onboarding.activateRealMode()).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    expect((await caller(identity).onboarding.status()).dataMode).toBe("simulated");
    expect(await events()).toEqual(before);
  });
  it("激活事件写失败会回滚模式与草稿进度", async () => {
    await readyFixture();
    await caller(identity).onboarding.saveWizardDraft(draftInput);
    await withEventFailure(async () => { await expect(caller(identity).onboarding.activateRealMode()).rejects.toThrow(); });
    expect((await caller(identity).onboarding.status()).dataMode).toBe("simulated");
    expect(await caller(identity).onboarding.wizardDraft()).toMatchObject({ currentStep: 1, version: 1 });
  });
  it("旧 real 标签没有本次资格凭据时按模拟展示", async () => {
    await readyFixture();
    await db.query("UPDATE profiles SET archive=jsonb_set(archive,'{dataMode}','\"real\"') WHERE workspace_id=$1", [identity.workspaceId]);
    expect(await caller(identity).onboarding.status()).toMatchObject({ persistedDataMode: "real", dataMode: "simulated", formalActivationRecorded: false });
  });
  it("真实状态对观察成员一致，资格失效后展示降级但不抹掉历史标签", async () => {
    const fixture = await readyFixture();
    await caller(identity).onboarding.activateRealMode();
    await db.query("UPDATE members SET role='readonly' WHERE id=$1", [identity.memberId]);
    expect(await caller(identity).onboarding.status()).toMatchObject({ dataMode: "real", formalActivationRecorded: true, activationGate: { canActivate: false } });
    await db.query("UPDATE agents SET status='disabled' WHERE id=$1", [fixture.agentId]);
    expect(await caller(identity).onboarding.status()).toMatchObject({ dataMode: "simulated", persistedDataMode: "real", formalActivationRecorded: true });
  });
  it("并发重复激活仅推进一次草稿并记录一次正式启用", async () => {
    await readyFixture();
    await caller(identity).onboarding.saveWizardDraft(draftInput);
    await Promise.all([caller(identity).onboarding.activateRealMode(), caller(identity).onboarding.activateRealMode()]);
    expect((await events()).filter((event) => event.payload.decision.action === "onboarding.real_mode_activated")).toHaveLength(1);
    expect(await caller(identity).onboarding.wizardDraft()).toMatchObject({ version: 2, currentStep: 4 });
  });
  it("定制装配全部候选与版本指纹相符的显式夹具可激活", async () => {
    await qualifiedCustomFixture();
    expect((await caller(identity).onboarding.status()).activationGate.canActivate).toBe(true);
    await caller(identity).onboarding.activateRealMode();
    expect(await caller(identity).onboarding.status()).toMatchObject({ dataMode: "real", formalActivationRecorded: true });
  });
  it.each(["exam-hash", "candidate-id", "missing-answer", "failed-answer", "draft-hash", "materialized", "fence-owner"])("定制资格 %s 不符时拒绝激活", async (kind) => {
    const fixture = await qualifiedCustomFixture();
    if (kind === "exam-hash") await db.query("UPDATE eval_exams SET target_hash=$2 WHERE id=$1", [fixture.examId, "f".repeat(64)]);
    if (kind === "candidate-id") await db.query("UPDATE eval_candidate_results SET agent_id='unknown-candidate' WHERE exam_id=$1", [fixture.examId]);
    if (kind === "missing-answer") await db.query("DELETE FROM eval_candidate_results WHERE exam_id=$1", [fixture.examId]);
    if (kind === "failed-answer") await db.query("UPDATE eval_candidate_results SET passed=false WHERE exam_id=$1", [fixture.examId]);
    if (kind === "draft-hash") await db.query("UPDATE wizard_staffing_drafts SET draft_hash=$2 WHERE id=$1", [fixture.draftId, "e".repeat(64)]);
    if (kind === "materialized") await db.query("UPDATE agents SET name='未经考试的新岗位' WHERE id=$1", [fixture.agentId]);
    if (kind === "fence-owner") await db.query("UPDATE fence_rules SET created_by='other-member' WHERE id=$1", [fixture.fenceId]);
    expect((await caller(identity).onboarding.status()).activationGate.canActivate).toBe(false);
    const before = await events();
    await expect(caller(identity).onboarding.activateRealMode()).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
    expect(await events()).toEqual(before);
  });
});
