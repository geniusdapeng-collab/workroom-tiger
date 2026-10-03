/** 模拟执行器，真实作用域 SQL；不连接券商或执行实际交易。 */
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Identity } from "@workloom/base/tenancy";
const mocks = vi.hoisted(() => ({ authority: vi.fn(), quest: vi.fn(), ask: vi.fn() }));
vi.mock("../service/access-authority.js", async (original) => ({
  ...await original<typeof import("../service/access-authority.js")>(), currentMemberAuthority: mocks.authority,
}));
vi.mock("@workloom/runtime", async (original) => ({
  ...await original<typeof import("@workloom/runtime")>(), runQuest: mocks.quest, runAsk: mocks.ask,
}));
import { appRouter } from "./router.js";
import { closeAllPools } from "@workloom/db";
const RUN_DB = process.env.RUN_DB_TESTS === "1" && Boolean(process.env.DATABASE_URL) && Boolean(process.env.DATABASE_APP_URL);
describe.runIf(RUN_DB)("Tiger 续跑目标真实作用域回归（模拟执行器）", () => {
  const owner = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  const tenantId = `tenant-resume-${suffix}`, workspaceId = `ws-resume-${suffix}`, otherWorkspace = `ws-resume-other-${suffix}`;
  const threadId = `T-resume-${suffix}`, otherThread = `T-resume-other-${suffix}`;
  const title = "模拟目标：生成 A/B「报告」，不执行交易";
  const identity: Identity = { kind: "member", memberId: `mem-${suffix}`, memberNo: "MEM-001", name: "模拟负责人", role: "owner", plan: "pro", tenantId, workspaceId };
  const context = { session: identity, identity, partnerIdentity: null, headers: new Headers() };
  const caller = appRouter.createCaller(context);
  beforeAll(async () => {
    await owner.query(`INSERT INTO tenants (id,name,plan) VALUES ($1,'模拟续跑租户','pro')`, [tenantId]);
    for (const ws of [workspaceId, otherWorkspace]) await owner.query(`INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'模拟续跑工作区',$1,'general')`, [ws, tenantId]);
    for (const [id, ws] of [[threadId, workspaceId], [otherThread, otherWorkspace]]) {
      await owner.query(`INSERT INTO threads (id,tenant_id,workspace_id,title,mode,status,created_by) VALUES ($1,$2,$3,$4,'quest','queued','MEM-001')`, [id, tenantId, ws, title]);
    }
  });
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.authority.mockImplementation(async (current: Identity) => ({ identity: current, permissions: {} }));
    mocks.quest.mockResolvedValue({ status: "queued", simulated: true });
    mocks.ask.mockResolvedValue({ status: "completed", simulated: true });
  });
  afterAll(async () => { await closeAllPools(); await owner.end(); });
  it("只传 threadId 时读取已保存目标，不用‘继续’重写规划目标", async () => {
    await caller.threads.run({ threadId });
    expect(mocks.quest.mock.calls[0]![3]).toMatchObject({ threadId, goal: title, mode: "quest" });
    const row = await owner.query(`SELECT title FROM threads WHERE id=$1`, [threadId]);
    expect(row.rows[0].title).toBe(title);
  });
  it("保留已有客户端显式目标参数的兼容性", async () => {
    await caller.threads.run({ threadId, goal: "模拟显式目标" });
    expect(mocks.quest.mock.calls[0]![3]).toMatchObject({ goal: "模拟显式目标" });
  });
  it("跨工作区及不存在的线程被拒，不调用执行器", async () => {
    for (const id of [otherThread, "T-mock-missing"]) await expect(caller.threads.run({ threadId: id })).rejects.toMatchObject({ code: "NOT_FOUND" });
    expect(mocks.quest).not.toHaveBeenCalled();
    expect(mocks.ask).not.toHaveBeenCalled();
  });
  it("只读成员不能续跑", async () => {
    const readonly = { ...identity, role: "readonly" as const };
    const restricted = appRouter.createCaller({ ...context, identity: readonly, session: readonly });
    await expect(restricted.threads.run({ threadId })).rejects.toMatchObject({ code: "FORBIDDEN" });
    expect(mocks.quest).not.toHaveBeenCalled();
  });
});
