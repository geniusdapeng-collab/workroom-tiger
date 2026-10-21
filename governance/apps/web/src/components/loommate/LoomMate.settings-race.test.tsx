// @vitest-environment jsdom
/** MC-054: mount the actual component and control only the network settlement order. */
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  login: vi.fn(), settings: vi.fn(), inbox: vi.fn(), save: vi.fn(), scan: vi.fn(), speak: vi.fn(),
}));
vi.mock("../../lib/trpc", () => ({
  ensureDemoLogin: api.login,
  trpc: { service: { secretary: {
    settings: { query: api.settings }, inbox: { query: api.inbox },
    saveSettings: { mutate: api.save }, scan: { mutate: api.scan },
  } } },
}));
vi.mock("../../voice/VoiceEngine", () => ({ VoiceEngine: { speak: api.speak } }));
vi.mock("./MateLive2D", () => ({ MateLive2D: () => <span>数字人测试占位</span> }));
vi.mock("../loomball", () => ({
  LoomBall: () => <span />, loomBallEnabled: false,
  emotionLabelOf: () => "待机", emotionOfSystem: () => "02", useStableEmotion: (value: string) => value,
}));
vi.mock("../../lib/useAskRail", () => ({ useAskRailPadding: () => 0, useSideNavWidth: () => 0 }));
vi.mock("../../shell/NavMenu", () => ({ canonicalNavigationPath: (value: string) => value }));
vi.mock("react-router", () => ({ useNavigate: () => vi.fn() }));
import { LoomMate } from "./LoomMate";

interface Settings {
  member_no: string; display_name: string; persona_key: string;
  persona_custom: { name?: string; tone?: string }; voice_key: string; voice_on: boolean;
  widget_size: "small" | "large" | "fullscreen"; quiet_start: string; quiet_end: string;
  channels: { im?: { provider: string; target: string }; outbox_urls?: string[] };
}
const initial = (): Settings => ({
  member_no: "MC054-LOCAL", display_name: "本地模拟用户", persona_key: "tianmei",
  persona_custom: { name: "旧称呼", tone: "保留语气" }, voice_key: "sweet", voice_on: false,
  widget_size: "large", quiet_start: "22:00", quiet_end: "08:00",
  channels: { im: { provider: "local-test", target: "保留目标" }, outbox_urls: ["https://old.example.test"] },
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const snapshot = (settings: Settings) => ({ settings: structuredClone(settings) });
const reminder = (id: string, title = "本地模拟提醒") => ({
  id, kind: "judge", level: "high", title, body: "本地模拟收件箱内容",
  actions: [], link: null, status: "unread", created_at: "2026-10-20T00:00:00Z",
});

describe("MC-054 LoomMate settings request ordering", () => {
  let container: HTMLDivElement;
  let root: Root;
  let server: Settings;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    localStorage.setItem("loommate.mini", "0");
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    server = initial();
    api.login.mockReset().mockResolvedValue(undefined);
    api.settings.mockReset().mockImplementation(async () => snapshot(server));
    api.inbox.mockReset().mockResolvedValue({ items: [] });
    api.scan.mockReset().mockResolvedValue({ added: 0 });
    // Match the server's shallow patch semantics, including nested object replacement.
    api.save.mockReset().mockImplementation(async (patch: Partial<Settings>) => {
      server = { ...server, ...patch };
      return snapshot(server);
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
  async function mount() { await act(async () => root.render(<LoomMate />)); }
  function button(label: string): HTMLButtonElement {
    const element = [...container.querySelectorAll<HTMLButtonElement>("button")]
      .find(node => node.textContent?.trim() === label || node.getAttribute("aria-label") === label);
    if (!element) throw new Error(`Missing button: ${label}`);
    return element;
  }
  function sizeButton() { return container.querySelector<HTMLButtonElement>('[title="切换大小"]')!; }
  function dialog() { return container.querySelector('[role="dialog"][aria-label="小织屏保"]'); }
  async function click(element: HTMLElement) { await act(async () => element.click()); }
  async function settle<T>(pending: ReturnType<typeof deferred<T>>, value: T) {
    await act(async () => pending.resolve(value));
  }
  async function heartbeat() { await act(async () => vi.advanceTimersByTimeAsync(20_000)); }
  async function openSettings() {
    const avatar = container.querySelector<HTMLElement>('[aria-keyshortcuts]')!;
    await act(async () => avatar.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    await click(button("设置"));
  }
  async function input(element: HTMLInputElement, value: string, blur = false) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      if (blur) element.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
  }
  function nameInput() { return container.querySelector<HTMLInputElement>("label input")!; }
  function saveButton() { return [...container.querySelectorAll<HTMLButtonElement>("button")].find(node => /^保存（/.test(node.textContent ?? ""))!; }
  function unreadCounts() {
    return [...container.querySelectorAll("span")]
      .map(node => node.textContent?.trim() ?? "").filter(text => /^\d+$/.test(text));
  }

  it("clears reminders on the current inbox failure and recovers on the next heartbeat", async () => {
    api.inbox.mockResolvedValueOnce({ items: [reminder("before-failure", "失败前的旧提醒")] });
    await mount();
    expect(unreadCounts()).toEqual(["1"]);
    api.inbox.mockRejectedValueOnce(new Error("inbox unavailable"));
    await heartbeat();
    expect(unreadCounts()).toEqual([]);
    expect(container.textContent).not.toContain("失败前的旧提醒");
    expect(sizeButton().textContent).toBe("全屏");
    api.inbox.mockResolvedValueOnce({ items: [reminder("recovered-a", "恢复后的提醒"), reminder("recovered-b")] });
    await heartbeat();
    expect(unreadCounts()).toEqual(["2"]);
    expect(container.textContent).toContain("恢复后的提醒");
    expect(api.inbox).toHaveBeenCalledTimes(3);
    expect(api.scan).toHaveBeenCalledTimes(2);
  });

  it("consumes a successful inbox when settings fail without discarding accepted settings", async () => {
    api.inbox.mockResolvedValueOnce({ items: [reminder("initial", "初始提醒")] });
    await mount();
    api.settings.mockRejectedValueOnce(new Error("settings unavailable"));
    api.inbox.mockResolvedValueOnce({ items: [reminder("fresh-a", "独立刷新的提醒"), reminder("fresh-b")] });
    await heartbeat();
    expect(unreadCounts()).toEqual(["2"]);
    expect(container.textContent).toContain("独立刷新的提醒");
    expect(container.textContent).not.toContain("初始提醒");
    expect(sizeButton().textContent).toBe("全屏");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("读取");
    expect(api.save).not.toHaveBeenCalled();
  });

  it("clears a failed current inbox during a pending size save without rolling back fullscreen", async () => {
    api.inbox.mockResolvedValueOnce({ items: [reminder("pending-save")] });
    await mount();
    const saving = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(saving.promise);
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
    expect(unreadCounts()).toEqual(["1"]);
    api.inbox.mockRejectedValueOnce(new Error("inbox unavailable during save"));
    await heartbeat();
    expect(unreadCounts()).toEqual([]);
    expect(dialog()).not.toBeNull();
    expect(container.querySelector('[role="status"]')?.textContent).toContain("正在保存");
    server.widget_size = "fullscreen";
    await settle(saving, snapshot(server));
    expect(dialog()).not.toBeNull();
    expect(container.querySelector('[role="status"]')).toBeNull();
    api.inbox.mockResolvedValueOnce({ items: [reminder("after-save", "保存后的提醒")] });
    await heartbeat();
    expect(unreadCounts()).toEqual(["1"]);
    expect(container.textContent).toContain("保存后的提醒");
  });

  it("does not let an obsolete inbox failure clear a newer manual read", async () => {
    api.settings.mockRejectedValueOnce(new Error("initial settings unavailable"));
    api.inbox.mockResolvedValueOnce({ items: [reminder("initial")] });
    await mount();
    const oldSettings = deferred<{ settings: Settings }>();
    const oldInbox = deferred<{ items: unknown[] }>();
    api.settings.mockReturnValueOnce(oldSettings.promise);
    api.inbox.mockReturnValueOnce(oldInbox.promise);
    await click(button("重新读取"));
    server.widget_size = "small";
    api.inbox.mockResolvedValueOnce({ items: [reminder("new-a", "较新读取的提醒"), reminder("new-b")] });
    await click(button("重新读取"));
    expect(unreadCounts()).toEqual(["2"]);
    expect(sizeButton().textContent).toBe("变大");
    await act(async () => oldInbox.reject(new Error("obsolete inbox failure")));
    await settle(oldSettings, snapshot(initial()));
    expect(unreadCounts()).toEqual(["2"]);
    expect(container.textContent).toContain("较新读取的提醒");
    expect(sizeButton().textContent).toBe("变大");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("waits for initialization before enabling the size control", async () => {
    const first = deferred<{ settings: Settings }>();
    api.settings.mockReturnValueOnce(first.promise);
    await mount();
    expect(sizeButton().disabled).toBe(true);
    await click(sizeButton());
    expect(api.save).not.toHaveBeenCalled();
    await settle(first, snapshot(server));
    expect(sizeButton().disabled).toBe(false);
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
    expect(server.widget_size).toBe("fullscreen");
  });

  it("does not apply an old poll after a fullscreen save succeeds", async () => {
    await mount();
    const old = deferred<{ settings: Settings }>();
    const frozen = snapshot(server);
    api.settings.mockReturnValueOnce(old.promise);
    await heartbeat();
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
    await settle(old, frozen);
    expect(dialog()).not.toBeNull();
    expect(server.widget_size).toBe("fullscreen");
  });

  it("keeps optimistic fullscreen when a pre-save read settles during the save", async () => {
    await mount();
    const old = deferred<{ settings: Settings }>();
    const saving = deferred<{ settings: Settings }>();
    api.settings.mockReturnValueOnce(old.promise);
    await heartbeat();
    api.save.mockReturnValueOnce(saving.promise);
    await click(sizeButton());
    await settle(old, snapshot(server));
    expect(dialog()).not.toBeNull();
    server.widget_size = "fullscreen";
    await settle(saving, snapshot(server));
    expect(dialog()).not.toBeNull();
  });

  it("discards a read started during a pending save even if it resolves after success", async () => {
    await mount();
    const saving = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(saving.promise);
    await click(sizeButton());
    const during = deferred<{ settings: Settings }>();
    const frozen = snapshot(server);
    api.settings.mockReturnValueOnce(during.promise);
    await heartbeat();
    server.widget_size = "fullscreen";
    await settle(saving, snapshot(server));
    await settle(during, frozen);
    expect(dialog()).not.toBeNull();
  });

  it("keeps the latest parallel manual read and its inbox when responses arrive in reverse order", async () => {
    api.settings.mockRejectedValueOnce(new Error("initial read failed"));
    await mount();
    const old = deferred<{ settings: Settings }>();
    const oldBox = deferred<{ items: unknown[] }>();
    api.settings.mockReturnValueOnce(old.promise);
    api.inbox.mockReturnValueOnce(oldBox.promise);
    await click(button("重新读取"));
    server.widget_size = "small";
    await click(button("重新读取"));
    expect(sizeButton().textContent).toBe("变大");
    await settle(old, snapshot(initial()));
    await settle(oldBox, { items: [{ id: "old", kind: "done", level: "high", title: "过期提醒", body: "过期内容", actions: [], link: null, status: "unread", created_at: "2026-10-20T00:00:00Z" }] });
    expect(sizeButton().textContent).toBe("变大");
    expect(container.textContent).not.toContain("过期提醒");
  });

  it("accepts the canonical save response and later legitimate heartbeat refreshes", async () => {
    await mount();
    api.save.mockImplementationOnce(async () => {
      server = { ...server, widget_size: "small", display_name: "服务端规范称呼" };
      return snapshot(server);
    });
    await click(sizeButton());
    expect(sizeButton()?.textContent).toBe("变大");
    server.widget_size = "fullscreen";
    await heartbeat();
    expect(dialog()).not.toBeNull();
    server.widget_size = "large";
    await heartbeat();
    expect(sizeButton().textContent).toBe("全屏");
  });

  it("protects SettingsPanel saves from an old poll and sends only edited fields", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "新的本地称呼");
    const old = deferred<{ settings: Settings }>();
    const frozen = snapshot(server);
    api.settings.mockReturnValueOnce(old.promise);
    await heartbeat();
    await click(saveButton());
    expect(api.save).toHaveBeenLastCalledWith({ display_name: "新的本地称呼" });
    await settle(old, frozen);
    await click(button("关闭小织助手面板"));
    await openSettings();
    expect(nameInput().value).toBe("新的本地称呼");
  });

  it("rebases a dirty form on refreshed settings without restoring its old widget size", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "仅改称呼");
    server.widget_size = "small";
    await heartbeat();
    await click(saveButton());
    expect(api.save).toHaveBeenLastCalledWith({ display_name: "仅改称呼" });
    expect(server.widget_size).toBe("small");
  });

  it("serializes size intents and never renders an earlier acknowledged mode over a newer intent", async () => {
    await mount();
    const first = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise);
    await click(sizeButton());
    await click(button("退出屏保（Esc）"));
    expect(sizeButton().textContent).toBe("变大");
    expect(api.save).toHaveBeenCalledTimes(1);
    server.widget_size = "fullscreen";
    await settle(first, snapshot(server));
    expect(api.save).toHaveBeenCalledTimes(2);
    expect(api.save.mock.calls.map(([patch]) => patch)).toEqual([{ widget_size: "fullscreen" }, { widget_size: "small" }]);
    expect(sizeButton().textContent).toBe("变大");
    expect(server.widget_size).toBe("small");
  });

  it("removes a failed earlier patch while preserving a queued form edit and its error", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "保留后续编辑");
    await click(button("小元气 · 活力满满"));
    const second = deferred<{ settings: Settings }>();
    const first = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await click(sizeButton());
    // Changing the presentation must preserve the existing draft without typing it again.
    expect(nameInput().value).toBe("保留后续编辑");
    await click(saveButton());
    expect(api.save).toHaveBeenCalledTimes(1);
    await act(async () => first.reject(new Error("first save rejected")));
    expect(dialog()).toBeNull();
    expect(sizeButton().textContent).toBe("全屏");
    expect(container.querySelector('[aria-keyshortcuts]')?.getAttribute("aria-label")).toContain("小元气");
    expect(server.display_name).toBe("本地模拟用户");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    server = { ...server, display_name: "保留后续编辑", persona_key: "yuanqi" };
    await settle(second, snapshot(server));
    expect(server.display_name).toBe("保留后续编辑");
    expect(server.widget_size).toBe("large");
    expect(sizeButton().textContent).toBe("全屏");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    await heartbeat();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
  });

  it("rolls a failed later save back to the acknowledged earlier result, then retries", async () => {
    await mount();
    const first = deferred<{ settings: Settings }>();
    const second = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await click(sizeButton());
    await click(button("退出屏保（Esc）"));
    server.widget_size = "fullscreen";
    await settle(first, snapshot(server));
    await act(async () => second.reject(new Error("forbidden")));
    expect(dialog()).not.toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    await click(button("退出屏保（Esc）"));
    expect(server.widget_size).toBe("small");
    expect(sizeButton().textContent).toBe("变大");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("keeps rejected form drafts editable for explicit retry and disables duplicate submissions", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "失败后保留输入");
    const saving = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(saving.promise);
    await click(saveButton());
    expect(nameInput().matches(":disabled")).toBe(true);
    expect(saveButton()?.matches(":disabled") ?? button("保存中…").matches(":disabled")).toBe(true);
    await act(async () => saving.reject(new Error("save failed")));
    expect(nameInput().value).toBe("失败后保留输入");
    expect(nameInput().matches(":disabled")).toBe(false);
    expect(container.querySelector('[aria-label="小织助手面板"]')?.contains(container.querySelector('[role="alert"]'))).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    await click(saveButton());
    expect(server.display_name).toBe("失败后保留输入");
  });

  it("merges edited nested keys at dispatch time, preserving new unedited sibling fields", async () => {
    await mount();
    await openSettings();
    await click(button("自定义"));
    await input(container.querySelector<HTMLInputElement>('[placeholder="她的名字"]')!, "新名字");
    await input(container.querySelector<HTMLInputElement>('[placeholder="https://…（红线与高级别实时推送）"]')!, "https://new.example.test", true);
    server.persona_custom = { name: "外部旧名字", tone: "最新服务端语气" };
    server.channels = { im: { provider: "latest-test", target: "最新目标" }, outbox_urls: ["https://external.example.test"] };
    await heartbeat();
    await click(saveButton());
    expect(api.save).toHaveBeenLastCalledWith({
      persona_key: "custom", persona_custom: { name: "新名字", tone: "最新服务端语气" },
      channels: { im: { provider: "latest-test", target: "最新目标" }, outbox_urls: ["https://new.example.test"] },
    });
    expect(server.channels.im?.target).toBe("最新目标");
    expect(server.persona_custom.tone).toBe("最新服务端语气");
  });

  it("uses current voice preferences rather than a stale response after voice is disabled", async () => {
    server.voice_on = true;
    await mount();
    await openSettings();
    const old = deferred<{ settings: Settings }>();
    const oldBox = deferred<{ items: unknown[] }>();
    const frozen = snapshot(server);
    api.settings.mockReturnValueOnce(old.promise);
    api.inbox.mockReturnValueOnce(oldBox.promise);
    await heartbeat();
    await click(button("开"));
    await click(saveButton());
    await settle(old, frozen);
    await settle(oldBox, { items: [{ id: "new", kind: "done", level: "high", title: "新提醒", body: "测试正文", actions: [], link: null, status: "unread", created_at: "2026-10-20T00:00:00Z" }] });
    expect(server.voice_on).toBe(false);
    expect(api.speak).not.toHaveBeenCalled();
  });

  it("preserves a pending form's draft and disabled controls when the presentation remounts", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "跨形态保留");
    const first = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise);
    await click(saveButton());
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
    expect(nameInput().value).toBe("跨形态保留");
    expect(nameInput().matches(":disabled")).toBe(true);
    await click(button("保存中…"));
    expect(api.save).toHaveBeenCalledTimes(1);
    server.display_name = "跨形态保留";
    await settle(first, snapshot(server));
    expect(api.save).toHaveBeenCalledTimes(2);
    expect(api.save.mock.calls.map(([patch]) => patch)).toEqual([{ display_name: "跨形态保留" }, { widget_size: "fullscreen" }]);
    expect(nameInput().matches(":disabled")).toBe(false);
  });

  it("does not dispatch a queued save or refresh after its owner is unmounted", async () => {
    await mount();
    const first = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise);
    await click(sizeButton());
    await click(button("退出屏保（Esc）"));
    expect(api.save).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
    root = createRoot(container);
    server.widget_size = "fullscreen";
    await settle(first, snapshot(server));
    await heartbeat();
    expect(api.save).toHaveBeenCalledTimes(1);
    expect(api.settings).toHaveBeenCalledTimes(1);
    expect(container.textContent).toBe("");
  });

  it("ignores a StrictMode generation's late login and never issues its stale read", async () => {
    const login = deferred<void>();
    api.login.mockReturnValueOnce(login.promise);
    server.widget_size = "small";
    await act(async () => root.render(<StrictMode><LoomMate /></StrictMode>));
    expect(sizeButton().textContent).toBe("变大");
    server.widget_size = "large";
    await settle(login, undefined);
    expect(api.settings).toHaveBeenCalledTimes(1);
    expect(sizeButton().textContent).toBe("变大");
  });

  it("uses the newest synchronous intent for size clicks in one React batch", async () => {
    await mount();
    const toggle = sizeButton();
    await act(async () => { toggle.click(); toggle.click(); });
    expect(api.save.mock.calls.map(([patch]) => patch)).toEqual([{ widget_size: "fullscreen" }, { widget_size: "small" }]);
    expect(sizeButton().textContent).toBe("变大");
    expect(server.widget_size).toBe("small");
  });

  it("shows initialization failure and permits an explicit retry instead of a dead control", async () => {
    api.settings.mockRejectedValueOnce(new Error("read failed"));
    await mount();
    expect(sizeButton().disabled).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("读取");
    await click(button("重新读取"));
    expect(sizeButton().disabled).toBe(false);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
  });


  it("merges queued nested edits with the preceding save's canonical acknowledgement", async () => {
    await mount();
    await openSettings();
    await click(button("自定义"));
    await input(container.querySelector<HTMLInputElement>('[placeholder="她的名字"]')!, "排队新名字");
    await input(container.querySelector<HTMLInputElement>('[placeholder="https://…（红线与高级别实时推送）"]')!, "https://queued.example.test", true);
    const first = deferred<{ settings: Settings }>();
    api.save.mockReturnValueOnce(first.promise);
    await click(sizeButton());
    await click(saveButton());
    expect(api.save).toHaveBeenCalledTimes(1);
    server = { ...server, widget_size: "fullscreen", persona_custom: { name: "服务端名字", tone: "先前保存确认语气" },
      channels: { im: { provider: "ack-test", target: "先前保存确认目标" }, outbox_urls: ["https://ack.example.test"] } };
    await settle(first, snapshot(server));
    expect(api.save).toHaveBeenLastCalledWith({
      persona_key: "custom", persona_custom: { name: "排队新名字", tone: "先前保存确认语气" },
      channels: { im: { provider: "ack-test", target: "先前保存确认目标" }, outbox_urls: ["https://queued.example.test"] },
    });
  });

  it("keeps the read order across delayed authentication and recovers from login rejection", async () => {
    await mount();
    const login = deferred<void>();
    api.settings.mockRejectedValueOnce(new Error("refresh failed"));
    await heartbeat();
    api.login.mockReturnValueOnce(login.promise);
    await click(button("重新读取"));
    server.widget_size = "small";
    await click(button("重新读取"));
    expect(sizeButton().textContent).toBe("变大");
    const reads = api.settings.mock.calls.length;
    server.widget_size = "fullscreen";
    // The obsolete read is still pending: it must not starve subsequent polling.
    await act(async () => vi.advanceTimersByTimeAsync(40_000));
    expect(api.settings.mock.calls.length).toBeGreaterThan(reads);
    expect(dialog()).not.toBeNull();
    const refreshedReads = api.settings.mock.calls.length;
    server.widget_size = "large";
    await settle(login, undefined);
    expect(api.settings).toHaveBeenCalledTimes(refreshedReads);
    expect(dialog()).not.toBeNull();
    api.login.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("authentication unavailable"));
    await heartbeat();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("读取");
    await click(button("重新读取"));
    expect(sizeButton().textContent).toBe("全屏");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("does not clear another failed field when only one of two failed changes is retried", async () => {
    await mount();
    await openSettings();
    await input(nameInput(), "尚未保存称呼");
    api.save.mockRejectedValueOnce(new Error("name rejected"));
    await click(saveButton());
    api.save.mockRejectedValueOnce(new Error("size rejected"));
    await click(sizeButton());
    expect(dialog()).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    await click(sizeButton());
    expect(dialog()).not.toBeNull();
    expect(server.display_name).toBe("本地模拟用户");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("未保存");
    await click(saveButton());
    expect(server.display_name).toBe("尚未保存称呼");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });


  it("refreshes untouched callback text and preserves raw edited callback text across modes", async () => {
    await mount();
    await openSettings();
    const callback = () => container.querySelector<HTMLInputElement>('[placeholder="https://…（红线与高级别实时推送）"]')!;
    server.channels = { ...server.channels, outbox_urls: ["https://refresh.example.test"] };
    await heartbeat();
    expect(callback().value).toBe("https://refresh.example.test");
    await input(nameInput(), "只改称呼");
    await click(saveButton());
    expect(api.save).toHaveBeenLastCalledWith({ display_name: "只改称呼" });
    await input(callback(), "https://one.example.test,");
    expect(callback().value).toBe("https://one.example.test,");
    await click(sizeButton());
    expect(callback().value).toBe("https://one.example.test,");
    await input(callback(), "https://one.example.test, https://two.example.test");
    await click(saveButton());
    expect(server.channels.outbox_urls).toEqual(["https://one.example.test", "https://two.example.test"]);
  });


  it("eventually initializes and refreshes when every settings response is slower than the heartbeat", async () => {
    api.settings.mockImplementation(() => {
      const frozen = snapshot(server);
      return new Promise(resolve => setTimeout(() => resolve(frozen), 25_000));
    });
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(25_000));
    expect(api.settings).toHaveBeenCalledTimes(1);
    expect(sizeButton().disabled).toBe(false);
    expect(sizeButton().textContent).toBe("全屏");
    server.widget_size = "small";
    await act(async () => vi.advanceTimersByTimeAsync(40_000));
    expect(api.settings).toHaveBeenCalledTimes(2);
    expect(sizeButton().textContent).toBe("变大");
    server.widget_size = "fullscreen";
    await act(async () => vi.advanceTimersByTimeAsync(40_000));
    expect(api.settings).toHaveBeenCalledTimes(3);
    expect(dialog()).not.toBeNull();
  });

  it.each(["login", "scan", "settings"] as const)("retires an obsolete heartbeat hung in %s after a newer manual read succeeds", async (stage) => {
    api.settings.mockRejectedValueOnce(new Error("initial read failed"));
    await mount();
    const hanging = deferred<unknown>();
    const blocked = stage === "login" ? api.login : stage === "scan" ? api.scan : api.settings;
    blocked.mockReturnValueOnce(hanging.promise);
    await heartbeat();
    server.widget_size = "small";
    await click(button("重新读取"));
    expect(sizeButton().textContent).toBe("变大");
    const reads = api.settings.mock.calls.length;
    server.widget_size = "fullscreen";
    await act(async () => vi.advanceTimersByTimeAsync(40_000));
    expect(api.settings.mock.calls.length).toBeGreaterThan(reads);
    expect(dialog()).not.toBeNull();
    const refreshedReads = api.settings.mock.calls.length;
    server.widget_size = "large";
    await settle(hanging, stage === "settings" ? snapshot(server) : stage === "scan" ? { added: 0 } : undefined);
    expect(api.settings).toHaveBeenCalledTimes(refreshedReads);
    expect(dialog()).not.toBeNull();
  });

  it("keeps scan and refresh single-flight and prevents an old heartbeat from entering a new mount", async () => {
    await mount();
    const scan = deferred<{ added: number }>();
    api.scan.mockReturnValueOnce(scan.promise);
    await heartbeat();
    await heartbeat();
    expect(api.scan).toHaveBeenCalledTimes(1);
    expect(api.settings).toHaveBeenCalledTimes(1);
    await act(async () => root.render(<LoomMate key="new-owner" />));
    expect(api.settings).toHaveBeenCalledTimes(2);
    await settle(scan, { added: 0 });
    expect(api.settings).toHaveBeenCalledTimes(2);
    await heartbeat();
    expect(api.scan).toHaveBeenCalledTimes(2);
    expect(api.settings).toHaveBeenCalledTimes(3);
  });

});
