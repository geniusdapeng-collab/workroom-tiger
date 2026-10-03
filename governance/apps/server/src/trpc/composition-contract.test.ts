/** 真实组合根与权限/校验；业务服务为模拟委托，不执行真实经营动作。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Identity } from "@workloom/base/tenancy";
const mocks = vi.hoisted(() => ({ authority: vi.fn(), draft: vi.fn(), save: vi.fn(), complete: vi.fn(), assign: vi.fn(), publication: vi.fn(), impact: vi.fn(), reactivate: vi.fn(), restore: vi.fn() }));
vi.mock("../service/access-authority.js", async (original) => ({
  ...await original<typeof import("../service/access-authority.js")>(), currentMemberAuthority: mocks.authority,
}));
vi.mock("@workloom/db", async (original) => ({
  ...await original<typeof import("@workloom/db")>(), getAppPool: () => ({ simulated: true }), getGatewayPool: () => ({ simulated: true }),
}));
vi.mock("../service/onboarding-continuity.js", async (original) => ({
  ...await original<typeof import("../service/onboarding-continuity.js")>(),
  getWizardDraft: mocks.draft, saveWizardDraft: mocks.save, completeWizardDraft: mocks.complete, assignWizardDraft: mocks.assign,
}));
vi.mock("../service/service-front-publication.js", async (original) => ({
  ...await original<typeof import("../service/service-front-publication.js")>(),
  bundledServiceFrontAvailable: () => false, resolveServiceFrontPublication: mocks.publication,
}));
vi.mock("@workloom/base/evolve", async (original) => ({
  ...await original<typeof import("@workloom/base/evolve")>(),
  previewMemoryImpact: mocks.impact, reactivateMemory: mocks.reactivate, restoreMemories: mocks.restore,
}));
import { appRouter } from "./router.js";

const identity: Identity = { kind: "member", memberId: "mem-mock", memberNo: "MEM-MOCK", name: "模拟负责人", role: "owner", plan: "pro", tenantId: "tenant-mock", workspaceId: "ws-mock" };
const context = { session: identity, identity, partnerIdentity: null, headers: new Headers() };
const caller = appRouter.createCaller(context);
beforeEach(() => {
  vi.clearAllMocks();
  mocks.authority.mockImplementation(async (current: Identity) => ({ identity: current, permissions: {} }));
  for (const fn of [mocks.draft, mocks.save, mocks.complete, mocks.assign, mocks.publication, mocks.impact, mocks.reactivate, mocks.restore]) fn.mockResolvedValue({ simulated: true });
});
describe("Tiger 已有客户端调用契约", () => {
  it("向导读取与发布状态绑定当前工作区", async () => {
    await caller.onboarding.wizardDraft();
    await caller.onboarding.serviceFrontPublication();
    expect(mocks.draft).toHaveBeenCalledWith("ws-mock");
    expect(mocks.publication).toHaveBeenCalledWith({ workspaceId: "ws-mock", bundledClientAvailable: false });
  });
  it("续办草稿携带版本与认证成员；完成使用指定版本", async () => {
    const input = { expectedVersion: 2, currentStep: 3, payload: { businessName: "模拟经营体" } };
    await caller.onboarding.saveWizardDraft(input);
    await caller.onboarding.completeWizardDraft({ expectedVersion: 3 });
    expect(mocks.save).toHaveBeenCalledWith("ws-mock", { memberId: identity.memberId, memberNo: identity.memberNo, role: identity.role }, input);
    expect(mocks.complete).toHaveBeenCalledWith("ws-mock", expect.objectContaining({ memberId: identity.memberId }), 3);
  });
  it("草稿负版本、超出步骤和秘密字段在服务调用前拒绝", async () => {
    for (const invalid of [
      { expectedVersion: -1, currentStep: 1, payload: {} },
      { expectedVersion: 1, currentStep: 5, payload: {} },
      { expectedVersion: 1, currentStep: 1, payload: { apiKey: "mock-rejected-field" } },
    ]) await expect(caller.onboarding.saveWizardDraft(invalid as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("委托失败传播，不返回伪造续办成功", async () => {
    mocks.save.mockRejectedValueOnce(new Error("模拟版本冲突"));
    await expect(caller.onboarding.saveWizardDraft({ expectedVersion: 1, currentStep: 1, payload: {} })).rejects.toThrow("模拟版本冲突");
  });
  it("记忆影响预览只委托当前工作区指定对象", async () => {
    await caller.memory.impact({ memoryId: "mock-memory" });
    expect(mocks.impact).toHaveBeenCalledWith(expect.anything(), { tenantId: "tenant-mock", workspaceId: "ws-mock" }, { memoryIds: ["mock-memory"], sourceMemberId: undefined });
  });
  it("记忆预览必须且只能指定一种来源", async () => {
    await expect(caller.memory.impact({})).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(caller.memory.impact({ memoryId: "mock", memberId: "mock" })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(mocks.impact).not.toHaveBeenCalled();
  });
  it("记忆重新启用与批次恢复绑定认证操作者和作用域", async () => {
    await caller.memory.reactivate({ memoryId: "mock-memory" });
    await caller.memory.restore({ memoryIds: ["mock-a", "mock-b"] });
    expect(mocks.reactivate).toHaveBeenCalledWith(expect.anything(), expect.anything(), { tenantId: "tenant-mock", workspaceId: "ws-mock" }, { memberNo: identity.memberNo }, "mock-memory");
    expect(mocks.restore).toHaveBeenCalledWith(expect.anything(), expect.anything(), { tenantId: "tenant-mock", workspaceId: "ws-mock" }, { memberNo: identity.memberNo }, ["mock-a", "mock-b"]);
  });
  it("空批次与超出 50 条的恢复请求拒绝", async () => {
    for (const memoryIds of [[], Array.from({ length: 51 }, (_, i) => `mock-${i}`)]) {
      await expect(caller.memory.restore({ memoryIds })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    }
    expect(mocks.restore).not.toHaveBeenCalled();
  });
  it("覆盖层挂载已有路由，匿名读和只读写都被拒绝", async () => {
    const anonymous = appRouter.createCaller({ session: null, identity: null, partnerIdentity: null, headers: new Headers() });
    await expect(anonymous.overlay.myStatus({ baseBundle: "mock" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    const readonly = { ...identity, role: "readonly" as const };
    const restricted = appRouter.createCaller({ ...context, identity: readonly, session: readonly });
    await expect(restricted.overlay.saveDraft({ baseBundle: "mock", baseVersion: "1.0.0", items: [{ type: "threshold" }] })).rejects.toMatchObject({ code: "FORBIDDEN" });
  });
  it("明确 deny 记忆权限可阻断新恢复端点", async () => {
    mocks.authority.mockResolvedValueOnce({ identity, permissions: { deny: ["memory.manage"] } });
    await expect(caller.memory.reactivate({ memoryId: "mock" })).rejects.toMatchObject({ code: "FORBIDDEN" });
    expect(mocks.reactivate).not.toHaveBeenCalled();
  });
});
