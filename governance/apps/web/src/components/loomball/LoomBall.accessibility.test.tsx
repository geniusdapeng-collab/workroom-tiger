// @vitest-environment jsdom
/**
 * MC032: a decorative, aria-hidden ball must not become a keyboard destination.
 * Use the real SVG engine; only the browser clock and media preference are controlled.
 * Native browser Tab/Enter/Space coverage for the actual P8 controls is recorded separately.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LoomBall, type LoomBallProps } from "./LoomBall";
import { loomBallApi } from "./engine";
import { emotionLabelOf } from "./agent-emotion";
import { WORKLOOM_EMOTION_IDS } from "./emotions-workloom";

describe("MC032 decorative LoomBall accessibility", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  let media: MediaQueryList;
  let reduced: boolean;
  let frames: Map<number, FrameRequestCallback>;
  let nextFrame: number;
  let frameTime: number;
  let requestFrame: ReturnType<typeof vi.fn>;

  const render = async (element: React.ReactNode) => {
    await act(async () => root!.render(element));
  };
  const ball = () => {
    const node = container.querySelector<HTMLElement>('[data-loomball="on"]');
    expect(node).not.toBeNull();
    return node!;
  };
  const frame = () => {
    const callbacks = [...frames.values()];
    frames.clear();
    frameTime += 16;
    callbacks.forEach((callback) => callback(frameTime));
  };
  const pointer = async (node: HTMLElement, entering: boolean) => {
    await act(async () => node.dispatchEvent(new MouseEvent(entering ? "pointerover" : "pointerout", {
      bubbles: true, relatedTarget: entering ? null : document.body,
    })));
  };
  const setReducedMotion = async (value: boolean) => {
    reduced = value;
    const event = new Event("change");
    Object.defineProperty(event, "matches", { value });
    await act(async () => media.dispatchEvent(event));
  };
  const expectDecorative = (node: HTMLElement) => {
    expect(node.getAttribute("aria-hidden")).toBe("true");
    expect(node.getAttribute("role")).not.toBe("button");
    expect(node.hasAttribute("tabindex")).toBe(false);
    const sequential = [node, ...node.querySelectorAll<HTMLElement>("*")]
      .filter((element) => element.tabIndex >= 0);
    expect(sequential).toEqual([]);
  };

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    frames = new Map();
    nextFrame = 0;
    frameTime = performance.now();
    requestFrame = vi.fn((callback: FrameRequestCallback) => {
      const id = ++nextFrame;
      frames.set(id, callback);
      return id;
    });
    vi.stubGlobal("requestAnimationFrame", requestFrame);
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => frames.delete(id)));
    reduced = false;
    const target = new EventTarget();
    media = Object.assign(target, {
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
    }) as unknown as MediaQueryList;
    Object.defineProperty(media, "matches", { get: () => reduced });
    vi.stubGlobal("matchMedia", vi.fn(() => media));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    if (root) await act(async () => root!.unmount());
    // The upstream ticker completes its pending frame after its final instance stops.
    frame();
    expect(frames.size).toBe(0);
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  const modes: Array<[string, Partial<LoomBallProps>]> = [
    ["default live", {}],
    ["live with hover disabled", { live: true, hoverActivate: false }],
    ["live with hover enabled", { live: true, hoverActivate: true }],
    ["default static hover", { live: false }],
    ["static with hover enabled", { live: false, hoverActivate: true }],
    ["static with hover disabled", { live: false, hoverActivate: false }],
  ];

  it.each(modes)("%s keeps the decorative subtree out of keyboard focus", async (_name, props) => {
    await render(<>
      <button type="button">查看档案</button>
      <LoomBall emotion="02" size={40} {...props} />
      <button type="button">修改别名</button>
    </>);
    const node = ball();
    expectDecorative(node);
    expect(node.style.width).toBe("40px");
    expect(node.style.height).toBe("40px");
    expect(node.title).toBe("待机");
    expect(node.querySelectorAll("svg")).toHaveLength(1);
    const buttons = container.querySelectorAll("button");
    buttons[0]!.focus();
    node.focus();
    expect(document.activeElement).toBe(buttons[0]);
    buttons[1]!.focus();
    expect(document.activeElement).toBe(buttons[1]);
  });

  it("pointer entry and exit retain animation without replacing the SVG or adding focus", async () => {
    await render(<LoomBall emotion="32" live={false} hoverActivate followGaze={false} />);
    const node = ball();
    const svg = node.querySelector("svg");
    expect(node.dataset.loomballActive).toBe("0");
    expect(requestFrame).not.toHaveBeenCalled();
    await pointer(node, true);
    expect(node.dataset.loomballActive).toBe("1");
    expect(requestFrame).toHaveBeenCalledOnce();
    frame();
    expect(requestFrame).toHaveBeenCalledTimes(2);
    await pointer(node, false);
    expect(node.dataset.loomballActive).toBe("0");
    frame();
    expect(frames.size).toBe(0);
    expect(node.querySelector("svg")).toBe(svg);
    expectDecorative(node);
  });

  it("hoverActivate=false keeps static balls still even under pointer and focus events", async () => {
    await render(<LoomBall emotion="02" live={false} hoverActivate={false} />);
    const node = ball();
    await pointer(node, true);
    await act(async () => node.dispatchEvent(new FocusEvent("focusin", { bubbles: true })));
    expect(node.dataset.loomballActive).toBe("0");
    expect(requestFrame).not.toHaveBeenCalled();
  });

  it("synthetic focus events cannot activate a static decorative ball", async () => {
    await render(<LoomBall emotion="02" live={false} hoverActivate />);
    await act(async () => ball().dispatchEvent(new FocusEvent("focusin", { bubbles: true })));
    expect(ball().dataset.loomballActive).toBe("0");
    expect(requestFrame).not.toHaveBeenCalled();
  });

  it("live animation continues after pointer exit", async () => {
    await render(<LoomBall emotion="32" live hoverActivate followGaze={false} />);
    await pointer(ball(), true);
    await pointer(ball(), false);
    expect(ball().dataset.loomballActive).toBe("1");
    const before = requestFrame.mock.calls.length;
    frame();
    expect(requestFrame.mock.calls.length).toBeGreaterThan(before);
  });

  it.each([false, true])("reduced motion suppresses live=%s and hover animation", async (live) => {
    reduced = true;
    await render(<LoomBall emotion="32" live={live} hoverActivate />);
    await pointer(ball(), true);
    expect(ball().dataset.loomballActive).toBe("0");
    expect(requestFrame).not.toHaveBeenCalled();
    expectDecorative(ball());
  });

  it("preference changes stop and resume the existing engine, and unmount destroys it", async () => {
    const create = vi.spyOn(loomBallApi(), "create");
    await render(<LoomBall emotion="32" live followGaze={false} />);
    expect(create).toHaveBeenCalledOnce();
    const engine = create.mock.results[0]!.value;
    const destroy = vi.spyOn(engine, "destroy");
    const svg = ball().querySelector("svg");
    await setReducedMotion(true);
    expect(ball().dataset.loomballActive).toBe("0");
    frame();
    expect(frames.size).toBe(0);
    await setReducedMotion(false);
    expect(ball().dataset.loomballActive).toBe("1");
    expect(frames.size).toBe(1);
    expect(ball().querySelector("svg")).toBe(svg);
    expect(create).toHaveBeenCalledOnce();
    await act(async () => root!.unmount());
    root = null;
    expect(destroy).toHaveBeenCalledOnce();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("37 roster balls preserve status text outside aria-hidden and share only active animation", async () => {
    const statuses = ["02", "32", "33", "34", "39", "40", ...Object.values(WORKLOOM_EMOTION_IDS)];
    await render(<>{Array.from({ length: 37 }, (_, index) => {
      const emotion = statuses[index % statuses.length]!;
      return <article key={index}>
        <LoomBall emotion={emotion} live={false} hoverActivate followGaze={false} />
        <span data-status>{emotionLabelOf(emotion)}</span>
      </article>;
    })}</>);
    const balls = [...container.querySelectorAll<HTMLElement>('[data-loomball="on"]')];
    expect(balls).toHaveLength(37);
    expect(container.querySelectorAll("svg")).toHaveLength(37);
    expect(requestFrame).not.toHaveBeenCalled();
    for (const node of balls) {
      expectDecorative(node);
      const chip = node.parentElement!.querySelector("[data-status]")!;
      expect(chip.closest('[aria-hidden="true"]')).toBeNull();
      expect(chip.textContent).toBe(node.title);
    }
    await pointer(balls[0]!, true);
    expect(balls.filter((node) => node.dataset.loomballActive === "1")).toEqual([balls[0]]);
    expect(frames.size).toBe(1);
    await pointer(balls[0]!, false);
    frame();
    expect(frames.size).toBe(0);
  });

  it("the disabled build keeps its same-size fallback unfocusable with no engine instance", async () => {
    vi.stubEnv("VITE_LOOMBALL", "0");
    vi.resetModules();
    const { LoomBall: DisabledBall } = await import("./LoomBall");
    const disabledEngine = await import("./engine");
    const create = vi.spyOn(disabledEngine.loomBallApi(), "create");
    await render(<DisabledBall emotion="32" size={56} live hoverActivate />);
    const node = container.querySelector<HTMLElement>('[data-loomball="off"]')!;
    expect(node).not.toBeNull();
    expectDecorative(node);
    expect(node.style.width).toBe("56px");
    expect(node.style.height).toBe("56px");
    expect(node.querySelector("svg")).toBeNull();
    expect(create).not.toHaveBeenCalled();
    expect(requestFrame).not.toHaveBeenCalled();
  });
});
