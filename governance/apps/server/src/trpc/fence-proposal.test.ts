import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { closeAllPools, getAppPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { fenceActivationFromProposal } from "@workloom/base/fence-engine";
import { appRouter } from "./router.js";

// 显式选用已迁移的专用测试库；真实路由、RLS、网关、审批与事务均不 mock。
// 事件只增不删，每个用例使用独立工作区，避免改写种子或业务数据。
const RUN_DB = process.env.RUN_DB_TESTS === "1" || process.env.RUN_FENCE_PROPOSAL_PG_TESTS === "1";
const rule = {
  ruleId: "R7001",
  name: "测试围栏 · 金额需审核",
  level: "review" as const,
  objectTypes: ["order"],
  actions: ["order.reconcile"],
  when: "params.amount > 10",
};

function caller(identity: Identity | null) {
  return appRouter.createCaller({
    session: identity,
    identity,
    partnerIdentity: null,
    headers: new Headers(),
  });
}

describe.skipIf(!RUN_DB)("围栏提案 API（真库、版本绑定与原子提交）", () => {
  let owner: pg.Pool;
  let identity: Identity;

  async function makeIdentity(): Promise<Identity> {
    const suffix = randomUUID();
    const tenantId = `mc007-t-${suffix}`;
    const workspaceId = `mc007-w-${suffix}`;
    const memberId = `mc007-m-${suffix}`;
    await owner.query("INSERT INTO tenants (id,name) VALUES ($1,$2)", [tenantId, "围栏 API 测试租户"]);
    await owner.query(
      "INSERT INTO workspaces (id,tenant_id,slug,name,industry) VALUES ($1,$2,$1,$3,'test')",
      [workspaceId, tenantId, "围栏 API 测试工作区"],
    );
    await owner.query(
      "INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MEM-TEST',$3,'owner')",
      [memberId, workspaceId, "测试裁决者"],
    );
    return { memberId, memberNo: "MEM-TEST", name: "测试裁决者", role: "owner", tenantId, workspaceId, plan: "pro" };
  }

  async function dryRun() {
    return caller(identity).fence.dryRun(rule);
  }

  async function state(dryRunId: string, scope = identity) {
    const result = await owner.query<{
      status: string | null; rules: number; events: number; approvals: number;
    }>(
      `SELECT (SELECT status FROM fence_dry_runs WHERE id=$1) AS status,
              (SELECT count(*)::int FROM fence_rules WHERE workspace_id=$2 AND is_baseline=false) AS rules,
              (SELECT count(*)::int FROM biz_events WHERE workspace_id=$2) AS events,
              (SELECT count(*)::int FROM approvals WHERE workspace_id=$2) AS approvals`,
      [dryRunId, scope.workspaceId],
    );
    return result.rows[0];
  }

  async function expectNoProposal(dryRunId: string) {
    expect(await state(dryRunId)).toEqual({ status: "pending", rules: 0, events: 0, approvals: 0 });
  }

  async function insertHistory(version: string, status: "active" | "rolled_back", baseline: boolean) {
    const id = `fr-r7001-${version}-${identity.workspaceId}`;
    await owner.query(
      `INSERT INTO fence_rules
       (id,rule_id,version,workspace_id,name,level,match_spec,action,is_baseline,status,created_by)
       VALUES ($1,$2,$3,$4,'测试历史围栏','auto',$5,'{"result":"pass"}',$6,$7,'test')`,
      [id, rule.ruleId, version, identity.workspaceId,
        JSON.stringify({ object_types: rule.objectTypes, actions: rule.actions, when: rule.when }), baseline, status],
    );
    return id;
  }

  // 数据库端注入失败：覆盖 SQL 写入以及延迟到 COMMIT 才触发的错误。
  // 触发器只命中当前测试工作区，不替代被测 SQL 或事务实现。
  async function withInsertFailure(
    table: "fence_rules" | "biz_events" | "approvals",
    deferred: boolean,
    run: () => Promise<void>,
  ) {
    const identifier = `mc007_fail_${randomUUID().replaceAll("-", "")}`;
    const workspace = identity.workspaceId.replaceAll("'", "''");
    await owner.query(
      `CREATE FUNCTION ${identifier}() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'MC007 injected ${table} failure'; END $$`,
    );
    try {
      await owner.query(deferred
        ? `CREATE CONSTRAINT TRIGGER ${identifier} AFTER INSERT ON ${table}
           DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
           WHEN (NEW.workspace_id='${workspace}') EXECUTE FUNCTION ${identifier}()`
        : `CREATE TRIGGER ${identifier} BEFORE INSERT ON ${table}
           FOR EACH ROW WHEN (NEW.workspace_id='${workspace}') EXECUTE FUNCTION ${identifier}()`);
      await run();
    } finally {
      try {
        await owner.query(`DROP TRIGGER IF EXISTS ${identifier} ON ${table}`);
      } finally {
        await owner.query(`DROP FUNCTION ${identifier}()`);
      }
    }
  }

  beforeAll(async () => {
    if (!process.env.DATABASE_URL || !process.env.DATABASE_APP_URL || !process.env.DATABASE_GATEWAY_URL) {
      throw new Error("围栏 PG 测试需要已迁移的专用 DATABASE_URL / DATABASE_APP_URL / DATABASE_GATEWAY_URL");
    }
    owner = new pg.Pool({ connectionString: process.env.DATABASE_URL });
    const appRole = await getAppPool().query<{ current_user: string }>("SELECT current_user");
    expect(appRole.rows[0]?.current_user).toBe("workloom_app");
  });

  beforeEach(async () => { identity = await makeIdentity(); });
  afterAll(async () => { await Promise.all([owner?.end(), closeAllPools()]); });

  it("提案行、事件、审批快照绑定同一版本与 dry-run，审批前不激活", async () => {
    const { dryRunId } = await dryRun();
    const result = await caller(identity).fence.confirmDryRun({ dryRunId, rule });
    const saved = await owner.query(
      `SELECT f.id,f.version,f.status,e.payload,a.snapshot,a.status AS approval_status
       FROM fence_rules f JOIN biz_events e ON e.event_id=$1
       JOIN approvals a ON a.event_id=e.event_id
       WHERE f.workspace_id=$2 AND f.rule_id=$3`,
      [result.eventId, identity.workspaceId, rule.ruleId],
    );
    expect(saved.rows).toHaveLength(1);
    const row = saved.rows[0]!;
    expect(row).toMatchObject({
      id: `fr-r7001-v1-${identity.workspaceId}`, version: "v1", status: "pending_approval", approval_status: "pending",
    });
    const after = { ...rule, dryRunId, ruleRowId: row.id, version: row.version };
    expect(row.payload.decision.after).toEqual(after);
    expect(row.snapshot).toEqual({ after, high_risk: true });
    expect(fenceActivationFromProposal(row.payload, identity.workspaceId)).toEqual({ ruleRowId: row.id, dryRunId });
    expect(await state(dryRunId)).toEqual({ status: "confirmed", rules: 1, events: 1, approvals: 1 });
  });

  it("同一规则连续提案经人工审批接口分别生效，前一覆盖版本回滚、基线保留", async () => {
    const baselineId = await insertHistory("v1", "active", true);
    for (const [level, version] of [["review", "v2"], ["block", "v3"]] as const) {
      const nextRule = { ...rule, level };
      const { dryRunId } = await caller(identity).fence.dryRun(nextRule);
      const proposal = await caller(identity).fence.confirmDryRun({ dryRunId, rule: nextRule });
      const approvalId = `apr-${proposal.eventId.toLowerCase()}`;
      const approved = await caller(identity).approvals.decide({ approvalId, gesture: "approve" });
      expect(approved.status).toBe("approved");
      const rows = await owner.query(
        "SELECT id,version,level,status,approved_event_id FROM fence_rules WHERE workspace_id=$1 AND rule_id=$2 AND is_baseline=false AND status='active'",
        [identity.workspaceId, rule.ruleId],
      );
      expect(rows.rows).toHaveLength(1);
      expect(rows.rows[0]).toMatchObject({ id: `fr-r7001-${version}-${identity.workspaceId}`, version, level, status: "active" });
      expect(rows.rows[0]?.approved_event_id).toBeTruthy();
    }
    const history = await owner.query("SELECT id,status FROM fence_rules WHERE workspace_id=$1 ORDER BY version", [identity.workspaceId]);
    expect(history.rows).toEqual([
      { id: baselineId, status: "active" },
      { id: `fr-r7001-v2-${identity.workspaceId}`, status: "rolled_back" },
      { id: `fr-r7001-v3-${identity.workspaceId}`, status: "active" },
    ]);
  });

  it("已回滚的历史版本仍占号，下一提案不复用其行 ID", async () => {
    await insertHistory("v4", "rolled_back", false);
    const { dryRunId } = await dryRun();
    await caller(identity).fence.confirmDryRun({ dryRunId, rule });
    const rows = await owner.query("SELECT id,version FROM fence_rules WHERE workspace_id=$1 AND status='pending_approval'", [identity.workspaceId]);
    expect(rows.rows).toEqual([{ id: `fr-r7001-v5-${identity.workspaceId}`, version: "v5" }]);
  });

  it("重复确认拒绝且不再生成提案、事件或审批", async () => {
    const { dryRunId } = await dryRun();
    await caller(identity).fence.confirmDryRun({ dryRunId, rule });
    const before = await state(dryRunId);
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule })).rejects.toThrow(/不存在或非 pending/);
    expect(await state(dryRunId)).toEqual(before);
  });

  it("并发确认同一 dry-run 只有一次成功，失败请求没有部分写入", async () => {
    const { dryRunId } = await dryRun();
    const results = await Promise.allSettled([
      caller(identity).fence.confirmDryRun({ dryRunId, rule }),
      caller(identity).fence.confirmDryRun({ dryRunId, rule }),
    ]);
    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
    expect(await state(dryRunId)).toEqual({ status: "confirmed", rules: 1, events: 1, approvals: 1 });
  });

  it("另一租户和工作区的 dry-run 不可确认", async () => {
    const { dryRunId } = await dryRun();
    const other = await makeIdentity();
    await expect(caller(other).fence.confirmDryRun({ dryRunId, rule })).rejects.toThrow(/不存在或非 pending/);
    await expectNoProposal(dryRunId);
    expect(await state(dryRunId, other)).toEqual({ status: "pending", rules: 0, events: 0, approvals: 0 });
  });

  it("不存在的 dry-run 被拒且不产生候选行或审批", async () => {
    await expect(caller(identity).fence.confirmDryRun({ dryRunId: "fdr-does-not-exist", rule })).rejects.toThrow(/不存在或非 pending/);
    expect(await state("fdr-does-not-exist")).toEqual({ status: null, rules: 0, events: 0, approvals: 0 });
  });

  it.each([
    { ...rule, ruleId: "R7002" },
    { ...rule, when: "false" },
    { ...rule, level: "auto" as const },
    { ...rule, name: "未经回放的新名称" },
    { ...rule, objectTypes: ["refund"] },
    { ...rule, actions: ["order.delete"] },
  ])("未经回放的规则改写被拒：$ruleId / $when", async (changedRule) => {
    const { dryRunId } = await dryRun();
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule: changedRule })).rejects.toMatchObject({ code: "BAD_REQUEST", message: expect.stringMatching(/dry-run.*(?:不一致|重新)/) });
    await expectNoProposal(dryRunId);
  });

  it("缺少输入快照的旧报告要求重新回放，不能猜测补齐后确认", async () => {
    const { dryRunId } = await dryRun();
    await owner.query("UPDATE fence_dry_runs SET report=report-'candidateInput' WHERE id=$1", [dryRunId]);
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule })).rejects.toMatchObject({ code: "BAD_REQUEST", message: expect.stringMatching(/重新.*回放/) });
    await expectNoProposal(dryRunId);
    const fresh = await dryRun();
    await caller(identity).fence.confirmDryRun({ dryRunId: fresh.dryRunId, rule });
    expect((await state(fresh.dryRunId))?.status).toBe("confirmed");
  });

  it("回放默认级别发生变化时拒绝确认", async () => {
    const { dryRunId } = await dryRun();
    await owner.query("UPDATE fence_dry_runs SET report=jsonb_set(report,'{candidateInput,defaultLevel}','\"auto\"') WHERE id=$1", [dryRunId]);
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expectNoProposal(dryRunId);
  });

  it("对象键顺序变化不改变同一输入的确认结果", async () => {
    const { dryRunId } = await dryRun();
    const reordered = Object.fromEntries(Object.entries(rule).reverse()) as typeof rule;
    await caller(identity).fence.confirmDryRun({ dryRunId, rule: reordered });
    expect(await state(dryRunId)).toEqual({ status: "confirmed", rules: 1, events: 1, approvals: 1 });
  });

  it("非法输入在事务前拒绝", async () => {
    const { dryRunId } = await dryRun();
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule: { ...rule, level: "invalid" as "review" } })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expectNoProposal(dryRunId);
  });

  it("匿名请求不能确认 dry-run", async () => {
    const { dryRunId } = await dryRun();
    await expect(caller(null).fence.confirmDryRun({ dryRunId, rule })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    await expectNoProposal(dryRunId);
  });

  it.each(["readonly", "deny"] as const)("当前成员 %s 限制阻止持旧 owner 声明的写入", async (mode) => {
    const { dryRunId } = await dryRun();
    if (mode === "readonly") {
      await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [identity.memberId]);
    } else {
      await owner.query("UPDATE members SET permissions=$2 WHERE id=$1", [identity.memberId, { deny: ["workspace.write"] }]);
    }
    await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule })).rejects.toMatchObject({ code: "FORBIDDEN" });
    await expectNoProposal(dryRunId);
  });

  it.each([
    ["fence_rules", false], ["biz_events", false], ["approvals", false], ["approvals", true],
  ] as const)("%s 写入失败（延迟到提交=%s）时四个状态一起回滚", async (table, deferred) => {
    const { dryRunId } = await dryRun();
    await withInsertFailure(table, deferred, async () => {
      await expect(caller(identity).fence.confirmDryRun({ dryRunId, rule })).rejects.toThrow(`MC007 injected ${table} failure`);
      await expectNoProposal(dryRunId);
    });
    // 失败后的同一份报告仍可重试，下一次成功也只有一套完整记录。
    await caller(identity).fence.confirmDryRun({ dryRunId, rule });
    expect(await state(dryRunId)).toEqual({ status: "confirmed", rules: 1, events: 1, approvals: 1 });
  });
});
