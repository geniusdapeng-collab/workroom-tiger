// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Onboarding from "./Onboarding";
import { SimBanner, type OnboardingStatus } from "../../components/SimBanner";

const calls = vi.hoisted(() => ({
  status: vi.fn(), draft: vi.fn(), publication: vi.fn(), save: vi.fn(), activate: vi.fn(),
}));
vi.mock("../../lib/trpc", () => ({
  ensureDemoLogin: vi.fn(async () => undefined),
  trpc: { onboarding: {
    status: { query: calls.status }, wizardDraft: { query: calls.draft },
    serviceFrontPublication: { query: calls.publication }, saveWizardDraft: { mutate: calls.save },
    activateRealMode: { mutate: calls.activate },
  } },
}));
vi.mock("./CustomizeWizard", () => ({ default: () => <div>定制向导</div> }));
vi.mock("../../components/UrlQrCode", () => ({ UrlQrCode: () => <div>二维码</div> }));

function status(patch: Partial<OnboardingStatus> = {}): OnboardingStatus {
  return {
    dataMode: "simulated", persistedDataMode: "simulated", formalActivationRecorded: false,
    declaredDataMode: "unconfigured", defaultToolMode: "connector-required", formalModeActive: false,
    llm: { provider: "mock", model: "", baseUrl: "", real: false },
    workspace: { name: "模式测试工作区", events: 0, agents: 1, members: 1, memories: 0 },
    bundle: { id: "fixture", isExample: false },
    activationGate: { canActivate: false, blockers: ["配置尚未完成"], checks: [{ key: "business", label: "经营主体", detail: "尚未保存", ok: false }] },
    ...patch,
  };
}
function draft(step = 0, completed = false) {
  return { id: "mc027-draft", version: 1, status: completed ? "completed" : "draft", currentStep: step,
    payload: {}, responsible: { memberId: "fixture-owner", memberNo: "MEM-MODE", name: "模式责任人", role: "owner" },
    updatedAt: "2026-10-20T00:00:00Z" };
}

describe("MC027 真实挂载的模式展示", () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.resetAllMocks();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.history.replaceState({}, "", "/onboarding");
    calls.status.mockResolvedValue(status());
    calls.draft.mockResolvedValue(draft());
    calls.publication.mockResolvedValue({ url: null, urlSource: "none", publicReachable: false, qrAvailable: false,
      workspaceRoutingReady: false, overall: "blocked", channels: [] });
    calls.save.mockResolvedValue({ ...draft(3), version: 2 });
    calls.activate.mockResolvedValue({ ok: true, dataMode: "real" });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
  });
  const render = async (element: React.ReactNode) => { await act(async () => { root.render(element); }); };
  const text = () => container.textContent ?? "";

  const cases: Array<[string, Partial<OnboardingStatus>, string]> = [
    ["missing", { declaredDataMode: "unconfigured" }, "数据模式未配置"],
    ["invalid", { declaredDataMode: "invalid" }, "数据模式未知"],
    ["simulated", { declaredDataMode: "simulated", defaultToolMode: "simulated" }, "已声明模拟模式"],
    ["real without activation", { declaredDataMode: "real", persistedDataMode: "real" }, "已声明非模拟模式"],
    ["real with stale qualification", { declaredDataMode: "real", persistedDataMode: "real", formalActivationRecorded: true }, "已声明非模拟模式"],
  ];
  it.each(cases)("向导的 %s 显示声明与未正式启用，不把资格失败当作模拟", async (_label, patch, expected) => {
    calls.status.mockResolvedValue(status(patch));
    await render(<Onboarding />);
    expect(text()).toContain(expected);
    expect(text()).toContain("未正式启用");
    expect(text()).not.toContain("模拟演示数据");
    expect(text()).not.toContain("全模拟运行态");
    expect(text()).not.toContain("系统仍保持模拟状态");
  });

  for (const isExample of [false, true]) {
    it.each(cases)(`${isExample ? "示例" : "常规"}横幅的 %s 不隐藏模式事实`, async (_label, patch, expected) => {
      calls.status.mockResolvedValue(status({ ...patch, bundle: { id: "fixture", isExample } }));
      await render(<SimBanner />);
      expect(text()).toContain(expected);
      expect(text()).toContain("未正式启用");
      expect(text()).toContain("回执");
      expect(text()).not.toContain("全模拟运行态");
      expect(text()).not.toContain("数据与团队可真实操作");
      expect(text()).not.toContain("已按模拟运行态展示");
      if (isExample) expect(text()).toContain("行业示例版");
    });
  }

  it.each([false, true])("完成页 formalModeActive=%s 使用正式资格，且不推断实际执行", async (active) => {
    calls.draft.mockResolvedValue(draft(4, true));
    calls.status.mockResolvedValue(status({
      declaredDataMode: "real", persistedDataMode: "real", formalModeActive: active,
      formalActivationRecorded: true, dataMode: active ? "real" : "simulated",
    }));
    await render(<Onboarding />);
    expect(text()).toContain(active ? "正式启用资格已确认" : "未正式启用");
    expect(text()).toContain("回执");
    expect(text()).not.toContain("系统仍保持模拟状态");
  });

  it("未接入新字段的旧 API 不被推断为已声明模拟", async () => {
    const legacy = status();
    delete legacy.declaredDataMode;
    delete legacy.defaultToolMode;
    delete legacy.formalModeActive;
    calls.status.mockResolvedValue(legacy);
    await render(<SimBanner />);
    expect(text()).toContain("数据模式未知");
    expect(text()).not.toContain("已声明模拟模式");
  });

  it("显式新资格字段优先于历史兼容字段", async () => {
    calls.status.mockResolvedValue(status({ declaredDataMode: "real", dataMode: "real", formalModeActive: false }));
    await render(<Onboarding />);
    expect(text()).toContain("未正式启用");
    expect(text()).not.toContain("正式启用资格已确认");
  });

  it("非模拟模型缺少试调凭据时不冒充内置模拟模型", async () => {
    calls.status.mockResolvedValue(status({ llm: { provider: "openai", baseUrl: "", model: "fixture-model", real: false } }));
    await render(<SimBanner />);
    expect(text()).toContain("当前模型配置尚未通过验证");
    expect(text()).not.toContain("内置模拟模型");
    expect(text()).not.toContain("内置确定性");
  });

  it("模型验证、正式资格和非模拟声明齐全时常规横幅可熄灭", async () => {
    calls.status.mockResolvedValue(status({
      declaredDataMode: "real", dataMode: "real", persistedDataMode: "real", formalModeActive: true, formalActivationRecorded: true,
      llm: { provider: "openai", baseUrl: "http://127.0.0.1:1", model: "fixture-model", real: true },
      activationGate: { canActivate: false, blockers: ["只读观察者"], checks: [] },
    }));
    await render(<SimBanner />);
    expect(text()).toBe("");
  });

  it("状态查询失败不能保留上一次已确认的运行事实", async () => {
    await render(<SimBanner />);
    calls.status.mockRejectedValue(new Error("MC027 injected read failure"));
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(text()).toContain("运行状态暂时无法确认");
    expect(text()).not.toContain("数据模式未配置");
    expect(text()).not.toContain("MC027 injected");
  });

  it("激活回读仍未取得正式资格时留在门禁页，不采用历史 real 别名", async () => {
    calls.draft.mockResolvedValue(draft(3));
    const gate = { canActivate: true, blockers: [], checks: [] };
    calls.status.mockResolvedValueOnce(status({ declaredDataMode: "real", activationGate: gate }))
      .mockResolvedValue(status({ declaredDataMode: "real", dataMode: "real", formalModeActive: false, activationGate: gate }));
    await render(<Onboarding />);
    const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "门禁全绿，启用正式经营模式");
    expect(button).toBeDefined();
    await act(async () => button!.click());
    expect(calls.activate).toHaveBeenCalledOnce();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("正式运行条件尚未全部通过");
    expect(text()).toContain("正式运行服务端门禁");
  });
});
