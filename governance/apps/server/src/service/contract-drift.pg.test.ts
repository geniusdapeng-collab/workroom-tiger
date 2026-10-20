import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { charterSchema, effectiveAutonomy } from "@workloom/base/captain";
import { appRouter } from "../trpc/router.js";
import { BUNDLED_INSPECTION_ADAPTERS } from "../industry/inspection-adapter-catalog.js";
import { validateStaffing } from "./bundle.js";
import { staffingDraftHash } from "./onboarding-truth.js";
import { runExam } from "./eval.js";
import { LOW_REFUSAL } from "./dialog.js";

const RUN_DB = process.env.RUN_CONTRACT_DRIFT_PG_TESTS === "1";
const autonomy = {
  ranges: { exposure: { label: "风险敞口比例", lower: 0.8, upper: 1.2, anchor: 1 } },
  caps: { research: { label: "研究预算", limit: 100 } },
};

function caller(identity: Identity | null) {
  return appRouter.createCaller({ session: identity, identity, partnerIdentity: null, headers: new Headers() });
}

// 真实 tRPC、RLS、业务服务、事件网关和事务。只在显式指定的隔离测试库运行；
// 模型固定 mock，用于证明预览不能装配、未通过逐岗考试不能激活，不伪造真实模型通过。
describe.skipIf(!RUN_DB)("治理 API 契约漂移（真实 PostgreSQL）", () => {
  let owner: pg.Pool;
  let identity: Identity;

  async function makeIdentity(): Promise<Identity> {
    const suffix = randomUUID();
    const tenantId = `mc010-t-${suffix}`;
    const workspaceId = `mc010-w-${suffix}`;
    const memberId = `mc010-m-${suffix}`;
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'契约回归租户','pro')", [tenantId]);
    await owner.query(
      "INSERT INTO workspaces (id,tenant_id,slug,name,industry) VALUES ($1,$2,$1,'契约回归工作区','test')",
      [workspaceId, tenantId],
    );
    await owner.query(
      "INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-CONTRACT','回归操作人','owner')",
      [memberId, workspaceId],
    );
    await owner.query(
      "INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'test','{}')",
      [workspaceId, tenantId],
    );
    return { memberId, memberNo: "MEM-CONTRACT", name: "回归操作人", role: "owner", tenantId, workspaceId, plan: "pro" };
  }

  async function events(scope = identity) {
    return (await owner.query(
      "SELECT event_id,payload FROM biz_events WHERE workspace_id=$1 ORDER BY seq", [scope.workspaceId],
    )).rows;
  }

  async function state(scope = identity) {
    return (await owner.query(
      `SELECT
       (SELECT count(*)::int FROM wizard_staffing_drafts WHERE workspace_id=$1) AS drafts,
       (SELECT count(*)::int FROM bundle_installs WHERE workspace_id=$1) AS installs,
       (SELECT count(*)::int FROM agents WHERE workspace_id=$1) AS agents,
       (SELECT count(*)::int FROM fence_rules WHERE workspace_id=$1) AS fences,
       (SELECT count(*)::int FROM eval_exams WHERE workspace_id=$1) AS exams,
       (SELECT count(*)::int FROM biz_events WHERE workspace_id=$1) AS events`, [scope.workspaceId],
    )).rows[0];
  }

  async function grantInput() {
    const { requiredClauses } = await caller(identity).captain.state();
    return { clauses: requiredClauses, autonomy, identityConfirmed: true };
  }

  async function realDraftFixture() {
    // 显式持久化测试夹具；不通过改写 mock 草案冒充真实生成结果。
    const draftId = `mc010-d-${randomUUID()}`;
    const industryText = "离线契约测试团队";
    const compiled = validateStaffing({
      team: [{ preset_key: "research-reader", role_title: "研究观察员", kind: "analyst",
        description: "只读研究资料并说明边界", readonly: true, night_shift: false, fence_bindings: ["R10"], skills: [] }],
      fences: [{ rule_id: "R10", name: "变更必须人审", level: "review",
        match: { object_types: ["research"], actions: ["research.change"] }, when: "", match_desc: "资料变更" }],
      skills_suggested: [],
    });
    const draftHash = staffingDraftHash(industryText, compiled);
    await owner.query(
      `INSERT INTO wizard_staffing_drafts (id,workspace_id,industry_text,compiled,status,generation_mode,draft_hash)
       VALUES ($1,$2,$3,$4,'draft','real',$5)`,
      [draftId, identity.workspaceId, industryText, JSON.stringify(compiled), draftHash],
    );
    return { draftId, expectedDraftHash: draftHash };
  }

  async function withEventFailure(run: () => Promise<void>) {
    const name = `mc010_fail_${randomUUID().replaceAll("-", "")}`;
    const workspace = identity.workspaceId.replaceAll("'", "''");
    await owner.query(`CREATE FUNCTION ${name}() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'MC010 injected event failure'; END $$`);
    try {
      await owner.query(`CREATE TRIGGER ${name} BEFORE INSERT ON biz_events FOR EACH ROW
        WHEN (NEW.workspace_id='${workspace}') EXECUTE FUNCTION ${name}()`);
      await run();
    } finally {
      try { await owner.query(`DROP TRIGGER IF EXISTS ${name} ON biz_events`); }
      finally { await owner.query(`DROP FUNCTION ${name}()`); }
    }
  }

  beforeAll(async () => {
    if (!process.env.DATABASE_URL || !process.env.DATABASE_APP_URL || !process.env.DATABASE_GATEWAY_URL) {
      throw new Error("契约 PG 回归需要已迁移的专用 owner/app/gateway 测试库配置");
    }
    if (process.env.LLM_PROVIDER !== "mock") throw new Error("本测试只允许 LLM_PROVIDER=mock");
    owner = new pg.Pool({ connectionString: process.env.DATABASE_URL });
    expect((await getAppPool().query("SELECT current_user")).rows[0]?.current_user).toBe("workloom_app");
  });
  beforeEach(async () => { identity = await makeIdentity(); });
  afterAll(async () => { await Promise.all([owner?.end(), closeAllPools()]); });

  it("交易巡检目录是不可变空目录，不依赖已删除的酒店适配器", () => {
    expect(BUNDLED_INSPECTION_ADAPTERS).toEqual([]);
    expect(Object.isFrozen(BUNDLED_INSPECTION_ADAPTERS)).toBe(true);
  });

  it("生成预览写入操作者事件，刷新恢复草案，并禁止模拟草案装配", async () => {
    const result = await caller(identity).service.bundle.generateStaffing({ industryText: "证券研究与风险管理" });
    expect(result.mock).toBe(true);
    expect(result.draftHash).toMatch(/^[a-f0-9]{64}$/);
    const saved = (await owner.query("SELECT generation_mode,draft_hash,status FROM wizard_staffing_drafts WHERE id=$1", [result.draftId])).rows[0];
    expect(saved).toEqual({ generation_mode: "mock", draft_hash: result.draftHash, status: "draft" });
    expect((await events())[0]?.payload).toMatchObject({
      who: { id: identity.memberNo, type: "human" }, decision: { action: "wizard.staffing.generated" },
    });
    const restored = await caller(identity).service.bundle.customizationStatus();
    expect(restored).toMatchObject({ phase: "staffing", draft: { id: result.draftId, hash: result.draftHash, generationMode: "mock" }, assembly: null });
    await expect(caller(identity).service.bundle.confirmAndAssembleStaffing({ draftId: result.draftId, expectedDraftHash: result.draftHash })).rejects.toThrow("模拟骨架");
    expect(await state()).toMatchObject({ drafts: 1, installs: 0, agents: 0, fences: 0, events: 1 });
  });

  it("刷新接口只返回当前工作区事实", async () => {
    const fixture = await realDraftFixture();
    expect(await caller(identity).service.bundle.customizationStatus()).toMatchObject({ draft: { id: fixture.draftId } });
    const other = await makeIdentity();
    expect(await caller(other).service.bundle.customizationStatus()).toMatchObject({ phase: "preview", draft: null, assembly: null, exam: null });
  });

  it("确认接口使用真人身份形成停用候选，幂等回读同一版本", async () => {
    const fixture = await realDraftFixture();
    const result = await caller(identity).service.bundle.confirmAndAssembleStaffing(fixture);
    expect(result).toMatchObject({ status: "staged", assemblyVersion: 1 });
    expect(await caller(identity).service.bundle.confirmAndAssembleStaffing(fixture)).toEqual(result);
    expect(await caller(identity).service.bundle.customizationStatus()).toMatchObject({
      phase: "exam", assembly: { installId: result.installId, version: 1, hash: result.assemblyHash, status: "staged" },
    });
    expect((await owner.query("SELECT confirmed_by,status FROM wizard_staffing_drafts WHERE id=$1", [fixture.draftId])).rows[0])
      .toEqual({ confirmed_by: identity.memberNo, status: "assembled" });
    expect((await events())[0]?.payload).toMatchObject({
      who: { id: identity.memberNo, type: "human" }, decision: { action: "wizard.staffing.assembled" },
    });
    expect((await owner.query("SELECT DISTINCT status FROM agents WHERE workspace_id=$1", [identity.workspaceId])).rows).toEqual([{ status: "disabled" }]);
  });

  it("草案指纹过期或来自其他工作区时拒绝装配且零写入", async () => {
    const fixture = await realDraftFixture();
    const before = await state();
    await expect(caller(identity).service.bundle.confirmAndAssembleStaffing({ ...fixture, expectedDraftHash: "0".repeat(64) })).rejects.toThrow("哈希已变化");
    const other = await makeIdentity();
    await expect(caller(other).service.bundle.confirmAndAssembleStaffing(fixture)).rejects.toThrow("不属于当前工作区");
    expect(await state()).toEqual(before);
    expect(await state(other)).toEqual({ drafts: 0, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
  });

  it.each([undefined, {}, { draftId: "", expectedDraftHash: "0".repeat(64) }, { draftId: "missing", expectedDraftHash: "short" }])
    ("确认装配拒绝缺失或畸形输入 %j", async (input) => {
      await expect(caller(identity).service.bundle.confirmAndAssembleStaffing(input as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
      expect(await state()).toEqual({ drafts: 0, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
    });

  it("并发确认同一草案只形成一个版本和一条装配事件", async () => {
    const fixture = await realDraftFixture();
    const [first, second] = await Promise.all([
      caller(identity).service.bundle.confirmAndAssembleStaffing(fixture),
      caller(identity).service.bundle.confirmAndAssembleStaffing(fixture),
    ]);
    expect(first).toEqual(second);
    expect(await state()).toEqual({ drafts: 1, installs: 1, agents: 1, fences: 1, exams: 0, events: 1 });
  });

  it("事件写入失败时草案与候选装配一起回滚", async () => {
    await withEventFailure(async () => {
      await expect(caller(identity).service.bundle.generateStaffing({ industryText: "证券研究与风险管理" })).rejects.toThrow("MC010 injected event failure");
      expect(await state()).toEqual({ drafts: 0, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
      const fixture = await realDraftFixture();
      await expect(caller(identity).service.bundle.confirmAndAssembleStaffing(fixture)).rejects.toThrow("MC010 injected event failure");
      expect(await state()).toEqual({ drafts: 1, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
    });
  });

  it("上岗考拒绝过期指纹与跨工作区装配，不生成考试或激活", async () => {
    const result = await caller(identity).service.bundle.confirmAndAssembleStaffing(await realDraftFixture());
    const before = await state();
    await expect(caller(identity).service.bundle.onboardingExam({ installId: result.installId, expectedAssemblyHash: "0".repeat(64) })).rejects.toThrow("哈希已过期");
    const other = await makeIdentity();
    await expect(caller(other).service.bundle.onboardingExam({ installId: result.installId, expectedAssemblyHash: result.assemblyHash })).rejects.toThrow("装配不存在");
    expect(await state()).toEqual(before);
    expect(await state(other)).toEqual({ drafts: 0, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
  });

  it("正确指纹送入逐岗考试，mock 失败关闭且记录真人操作", async () => {
    const assembly = await caller(identity).service.bundle.confirmAndAssembleStaffing(await realDraftFixture());
    const result = await caller(identity).service.bundle.onboardingExam({ installId: assembly.installId, expectedAssemblyHash: assembly.assemblyHash });
    expect(result).toMatchObject({ passed: false, activated: false, installStatus: "staged", verdict: "fail" });
    expect(result.candidates).toHaveLength(1);
    expect(result.candidates[0]?.passed).toBe(false);
    const rows = (await events()).filter((event) => event.payload.decision.action === "wizard.exam.failed");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.payload).toMatchObject({ who: { id: identity.memberNo, type: "human" }, decision: { after: { assembly_hash: assembly.assemblyHash } } });
    expect(await caller(identity).service.bundle.customizationStatus()).toMatchObject({ phase: "exam", exam: { id: result.examId, verdict: "fail" }, assembly: { status: "staged" } });
  });

  it.each([undefined, {}, { installId: "", expectedAssemblyHash: "0".repeat(64) }, { installId: "missing", expectedAssemblyHash: "short" }])
    ("上岗考拒绝缺失或畸形输入 %j", async (input) => {
      await expect(caller(identity).service.bundle.onboardingExam(input as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
      expect(await state()).toEqual({ drafts: 0, installs: 0, agents: 0, fences: 0, exams: 0, events: 0 });
    });

  it("宪章授权持久化通用边界与真实事件引用，试用按既有规则收紧", async () => {
    const result = await caller(identity).captain.grant(await grantInput());
    expect(result.mode).toBe("shadow");
    const readback = await caller(identity).captain.state();
    expect(charterSchema.parse(readback.charter).autonomy).toEqual(autonomy);
    expect(readback.charter.grant?.event_id).toBe(result.grantEventId);
    expect((await events())[0]).toMatchObject({ event_id: result.grantEventId, payload: { who: { id: identity.memberNo, type: "human" }, decision: { params: { autonomy } } } });
    await caller(identity).captain.transit({ kind: "advance" });
    const trial = await caller(identity).captain.state();
    expect(trial.charter.mode).toBe("trial");
    expect(effectiveAutonomy(trial.charter)).toEqual({
      ranges: { exposure: { label: "风险敞口比例", lower: 0.9, upper: 1.1, anchor: 1 } },
      caps: { research: { label: "研究预算", limit: 50 } },
    });
  });

  it.each([
    { price_band: [0.8, 1.2], procurement_cap: 100, campaign_cap: 100 },
    { ranges: { bad: { label: "逆序", lower: 2, upper: 1, anchor: 1 } }, caps: {} },
    { ranges: {}, caps: { bad: { label: "负值", limit: -1 } } },
    undefined,
  ])("宪章拒绝旧字段、非法边界或省略授权边界 %j", async (invalid) => {
    await expect(caller(identity).captain.grant({ ...await grantInput(), autonomy: invalid } as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(await events()).toEqual([]);
    expect((await caller(identity).captain.state()).charter.mode).toBe("disabled");
  });

  it("条款和身份核验必须完成，事件失败不会提前保存宪章", async () => {
    const input = await grantInput();
    await expect(caller(identity).captain.grant({ ...input, clauses: [] })).rejects.toThrow("授权条款未全部确认");
    await expect(caller(identity).captain.grant({ ...input, identityConfirmed: false })).rejects.toThrow("未完成身份核验");
    await withEventFailure(async () => { await expect(caller(identity).captain.grant(input)).rejects.toThrow("MC010 injected event failure"); });
    expect(await events()).toEqual([]);
    expect((await caller(identity).captain.state()).charter.mode).toBe("disabled");
  });

  it.each(["anonymous", "readonly", "denied"] as const)("%s 无法调用定制写操作与宪章授权", async (mode) => {
    const fixture = await realDraftFixture();
    const input = await grantInput();
    if (mode === "readonly") await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [identity.memberId]);
    if (mode === "denied") await owner.query("UPDATE members SET permissions=$2 WHERE id=$1", [identity.memberId, JSON.stringify({ deny: ["workspace.write"] })]);
    const api = caller(mode === "anonymous" ? null : identity);
    const code = mode === "anonymous" ? "UNAUTHORIZED" : "FORBIDDEN";
    const before = await state();
    await expect(api.service.bundle.generateStaffing({ industryText: "证券研究与风险管理" })).rejects.toMatchObject({ code });
    await expect(api.service.bundle.confirmAndAssembleStaffing(fixture)).rejects.toMatchObject({ code });
    await expect(api.service.bundle.onboardingExam({ installId: "missing", expectedAssemblyHash: "0".repeat(64) })).rejects.toMatchObject({ code });
    await expect(api.captain.grant(input)).rejects.toMatchObject({ code });
    if (mode === "anonymous") await expect(api.service.bundle.customizationStatus()).rejects.toMatchObject({ code });
    expect(await state()).toEqual(before);
  });

  it("考试使用真实对话服务并持久化答卷，无知识依据时诚实拒答", async () => {
    const other = await makeIdentity();
    // 对话考试与真实入口遵守同一个活动装配前置条件；只补本用例夹具，
    // 不给其他“未装配”契约用例隐式安装，也不绕过服务端装配校验。
    await owner.query("UPDATE workspaces SET industry='trading',bundle_id='trading' WHERE id=$1", [identity.workspaceId]);
    await owner.query(
      "INSERT INTO bundle_installs(id,workspace_id,bundle_id,status) VALUES($1,$2,'trading','active')",
      [`mc010-exam-install-${randomUUID()}`, identity.workspaceId],
    );
    const result = await runExam(identity.workspaceId, {
      examType: "on-change", perStructure: 1,
      questionFixture: { id: "mc010-unknown-fact", questions: [{
        subject: "skill", structure: "single-single", primaryDimensions: ["accuracy"],
        redLine: false, difficulty: "easy", source: "customer", tags: ["契约回归"],
        scenario: { turns: [{ role: "guest", input: "qxzv contractknowledge 8642" }] },
        assertions: [{ type: "fact_terms_present", expected: ["暂时无法准确回答"] }],
      }] },
    });
    expect(result.answers).toHaveLength(1);
    expect(result.answers[0]?.replies[0]?.text).toBe(LOW_REFUSAL);
    expect(result.answers[0]?.replies[0]?.citations).toEqual([]);
    const stored = await owner.query(`SELECT e.status,e.total_questions,
      (SELECT count(*)::int FROM eval_answers a WHERE a.exam_id=e.id) AS answers,
      (SELECT count(*)::int FROM c_messages m WHERE m.workspace_id=e.workspace_id) AS messages
      FROM eval_exams e WHERE e.workspace_id=$1`, [identity.workspaceId]);
    expect(stored.rows).toEqual([{ status: "done", total_questions: 1, answers: 1, messages: 2 }]);
    expect(await state(other)).toMatchObject({ exams: 0, events: 0 });
  });
});
