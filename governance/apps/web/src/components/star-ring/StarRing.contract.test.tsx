// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StarRing } from "./StarRing";

const calls = vi.hoisted(() => ({ get: vi.fn(), run: vi.fn(), note: vi.fn(), dispatch: vi.fn() }));
vi.mock("../../lib/trpc", () => ({
  ensureDemoLogin: vi.fn(async () => undefined),
  trpc: { threads: { get: { query: calls.get }, run: { mutate: calls.run }, note: { mutate: calls.note }, dispatch: { mutate: calls.dispatch } }, service: {} },
}));
vi.mock("../AgentAvatar", () => ({ AgentAvatar: () => <span>助手</span> }));
vi.mock("../AIFeedback", () => ({ AIFeedback: () => null }));

// 挂载真实 StarRing 组件，通过公开页面事件发送；仅网络客户端与装饰性子组件替身。
describe("StarRing 线程契约", () => {
  let root: Root;
  let container: HTMLDivElement;
  beforeEach(async () => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
    HTMLElement.prototype.scrollTo = vi.fn();
    calls.get.mockResolvedValue({ title: "分析当前持仓风险", status: "paused" });
    calls.run.mockResolvedValue({ status: "completed", stepsDone: 1, stepsTotal: 1 });
    calls.note.mockResolvedValue({ eventId: "E-TEST", threadId: "T-TEST" });
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
    await act(async () => root.render(<MemoryRouter initialEntries={["/tasks/T-TEST"]}><StarRing /></MemoryRouter>));
  });
  afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.useRealTimers(); });
  async function send(intent: "advance" | "note", text: string) {
    await act(async () => {
      window.dispatchEvent(new CustomEvent("workloom:assistant-intent", { detail: { intent, presetText: text } }));
      await vi.advanceTimersByTimeAsync(150);
    });
  }
  it("推进使用服务端既定目标，口语指令不能覆盖目标", async () => {
    await send("advance", "继续推进吧");
    expect(calls.get).toHaveBeenCalledWith({ threadId: "T-TEST" });
    expect(calls.run).toHaveBeenCalledExactlyOnceWith({ threadId: "T-TEST", goal: "分析当前持仓风险" });
    expect(calls.dispatch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("已续跑");
  });
  it.each([null, { title: "   " }])("线程或目标缺失时不上报成功、不执行", async (detail) => {
    calls.get.mockResolvedValue(detail);
    await send("advance", "继续");
    expect(calls.run).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("已续跑");
    expect(container.querySelector("input")?.value).toBe("继续");
  });
  it("回读或执行失败保留重试输入", async () => {
    calls.get.mockRejectedValue(new Error("连接暂时失败"));
    await send("advance", "继续");
    expect(calls.run).not.toHaveBeenCalled();
    expect(container.querySelector("input")?.value).toBe("继续");
    calls.get.mockResolvedValue({ title: "分析当前持仓风险" });
    calls.run.mockRejectedValue(new Error("执行失败"));
    await send("advance", "继续推进");
    expect(container.querySelector("input")?.value).toBe("继续推进");
    expect(container.textContent).not.toContain("已续跑");
  });
  it("留言只追加线程事件，成功收到回执后显示入账", async () => {
    await send("note", "备注：先保留研究来源");
    expect(calls.note).toHaveBeenCalledExactlyOnceWith({ threadId: "T-TEST", text: "备注：先保留研究来源" });
    expect(calls.run).not.toHaveBeenCalled(); expect(calls.dispatch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("留言只留痕");
  });
});
