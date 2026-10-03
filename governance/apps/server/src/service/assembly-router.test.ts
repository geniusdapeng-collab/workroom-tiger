/** 真实路由/输入/权限中间件，装配服务使用模拟夹具，不装配真实经营体。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Identity } from "@workloom/base/tenancy";
const mocks = vi.hoisted(() => ({ authority: vi.fn(), generate: vi.fn(), confirm: vi.fn(), exam: vi.fn(), status: vi.fn() }));
vi.mock("./access-authority.js", async (original) => ({
  ...await original<typeof import("./access-authority.js")>(), currentMemberAuthority: mocks.authority,
}));
vi.mock("./bundle.js", async (original) => ({
  ...await original<typeof import("./bundle.js")>(),
  generateStaffing: mocks.generate, confirmAndAssembleStaffing: mocks.confirm,
  onboardingExam: mocks.exam, customizationStatus: mocks.status,
}));
import { serviceRouter } from "./router.js";

const identity: Identity = { kind: "member", memberId: "mem-mock", memberNo: "MEM-MOCK", name: "模拟负责人", role: "owner", plan: "pro", tenantId: "tenant-mock", workspaceId: "ws-mock" };
const context = { identity, session: identity, partnerIdentity: null, headers: new Headers() };
const caller = serviceRouter.createCaller(context);
const hash = "a".repeat(64);
beforeEach(() => {
  vi.clearAllMocks();
  mocks.authority.mockImplementation(async (current: Identity) => ({ identity: current, permissions: {} }));
  mocks.generate.mockResolvedValue({ draftId: "mock-draft" });
  mocks.confirm.mockResolvedValue({ installId: "mock-install" });
  mocks.exam.mockResolvedValue({ status: "activated", simulated: true });
  mocks.status.mockResolvedValue({ phase: "draft", simulated: true });
});
describe("装配路由版本与操作者绑定", () => {
  it("草案生成绑定当前工作区及认证操作者", async () => {
    await caller.bundle.generateStaffing({ industryText: "模拟服务经营体" });
    expect(mocks.generate).toHaveBeenCalledWith("ws-mock", "模拟服务经营体", { id: "MEM-MOCK", type: "human" });
  });
  it("向导恢复态来自服务端事实", async () => {
    await expect(caller.bundle.customizationStatus()).resolves.toMatchObject({ phase: "draft", simulated: true });
    expect(mocks.status).toHaveBeenCalledWith("ws-mock");
  });
  it("人审装配携带草案 ID/hash 与认证操作者", async () => {
    const input = { draftId: "mock-draft", expectedDraftHash: hash };
    await caller.bundle.confirmAndAssembleStaffing(input);
    expect(mocks.confirm).toHaveBeenCalledWith("ws-mock", input, { id: "MEM-MOCK", type: "human" });
  });
  it("上岗考携带指定 install/hash，不能退回无参当前版本", async () => {
    const input = { installId: "mock-install", expectedAssemblyHash: hash };
    await caller.bundle.onboardingExam(input);
    expect(mocks.exam).toHaveBeenCalledWith("ws-mock", input, { id: "MEM-MOCK", type: "human" });
  });
  it.each([
    ["缺少装配绑定", () => caller.bundle.onboardingExam(undefined as never)],
    ["非法装配哈希", () => caller.bundle.onboardingExam({ installId: "mock", expectedAssemblyHash: "bad" })],
    ["空草案 ID", () => caller.bundle.confirmAndAssembleStaffing({ draftId: "", expectedDraftHash: hash })],
    ["超长行业描述", () => caller.bundle.generateStaffing({ industryText: "模".repeat(2001) })],
  ])("%s 在调用服务前拒绝", async (_label, call) => {
    await expect(call()).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect(mocks.exam).not.toHaveBeenCalled();
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(mocks.generate).not.toHaveBeenCalled();
  });
  it("装配冲突向客户端映射为前置条件失败", async () => {
    mocks.confirm.mockRejectedValueOnce(new Error("模拟草案已过期"));
    await expect(caller.bundle.confirmAndAssembleStaffing({ draftId: "mock", expectedDraftHash: hash })).rejects.toMatchObject({ code: "PRECONDITION_FAILED", message: "模拟草案已过期" });
  });
  it("上岗考失败不伪造激活回执", async () => {
    mocks.exam.mockRejectedValueOnce(new Error("模拟考试失败"));
    await expect(caller.bundle.onboardingExam({ installId: "mock", expectedAssemblyHash: hash })).rejects.toMatchObject({ code: "PRECONDITION_FAILED" });
  });
  it("匿名及只读成员不能提交装配或考试", async () => {
    const anonymous = serviceRouter.createCaller({ session: null, identity: null, partnerIdentity: null, headers: new Headers() });
    await expect(anonymous.bundle.generateStaffing({ industryText: "模拟经营体" })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    const readonly = { ...identity, role: "readonly" as const };
    const restricted = serviceRouter.createCaller({ ...context, identity: readonly, session: readonly });
    await expect(restricted.bundle.onboardingExam({ installId: "mock", expectedAssemblyHash: hash })).rejects.toMatchObject({ code: "FORBIDDEN" });
    expect(mocks.exam).not.toHaveBeenCalled();
  });
});
