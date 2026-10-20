/**
 * MC-035: real LoomMate / production CSS / local HTTP fixtures in Chrome.
 * No backend, database, model or broker is contacted. Existing vector2d mode
 * renders the real avatar without requiring WebGL. Pointer paths use ordinary
 * click and native scrolling; keyboard paths use sequential Tab, never focus().
 * MC035_SOURCE_DIR selects a frozen pre-repair component for the red test.
 */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const TEST_FILE = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(TEST_FILE), "../../../..");
const WEB = path.join(ROOT, "governance/apps/web");
const COMPONENT = "governance/apps/web/src/components/loommate/LoomMate.tsx";
const SOURCE_ROOT = path.resolve(process.env.MC035_SOURCE_DIR ?? ROOT);
const OUT = process.env.MC035_EVIDENCE_DIR ? path.resolve(process.env.MC035_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const { chromium, expect } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  COMPONENT, "governance/apps/web/src/App.tsx",
  ...["trpc.ts", "product.ts", "useAskRail.ts"].map((f) => `governance/apps/web/src/lib/${f}`),
  "governance/apps/web/src/shell/NavMenu.tsx",
  ...["VoiceEngine.ts", "neuralVoice.ts"].map((f) => `governance/apps/web/src/voice/${f}`),
  ...["MateLive2D.tsx", "speechText.ts"].map((f) => `governance/apps/web/src/components/loommate/${f}`),
  ...["index.ts", "LoomBall.tsx", "engine.ts", "agent-emotion.ts", "emotions-workloom.ts", "useStableEmotion.ts"]
    .map((f) => `governance/apps/web/src/components/loomball/${f}`),
  ...["tokens.css", "subtitle.css"].map((f) => `governance/apps/web/src/styles/${f}`),
  "governance/apps/web/vite.config.ts", "governance/apps/web/package.json",
  ...["vite-product.mjs", "product-runtime.mjs"].map((f) => `governance/scripts/${f}`),
  "governance/apps/web/node_modules/@workloom/ui/package.json",
  ...["index.js", "language.js", "icons.js", "managed-surface.js", "modal-document-state.js",
    "overlay-manager.js", "overlay-stack.js", "tokens.css", "content-safety.css", "components.css"]
    .map((f) => `governance/apps/web/node_modules/@workloom/ui/dist/${f}`),
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [
  file, sha256(await readFile(path.join(file === COMPONENT ? SOURCE_ROOT : ROOT, file))),
])));
const SURFACE = '[data-wl-managed-surface="assistant-companion"]';
const avatar = (page) => page.getByRole("button", { name: /^小织助手；/ });
const screenAvatar = (page) => page.getByRole("dialog", { name: "小织屏保", exact: true })
  .getByRole("button").filter({ has: page.locator('[data-avatar-ready="true"], img[alt="小织"]') });
const panel = (page) => page.getByRole("region", { name: "小织助手面板", exact: true });
const closePanel = (page) => page.getByRole("button", { name: "关闭小织助手面板", exact: true });
const badge = (page, count) => page.locator(`${SURFACE} span`).filter({ hasText: new RegExp(`^${count}$`) });
const fixtureId = path.join(WEB, "__mc035_fixture__.tsx");
const fixtureSource = `
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { LoomMate } from "./src/components/loommate/LoomMate";
import "./src/styles/tokens.css";
import "./src/styles/subtitle.css";
import "@workloom/ui/tokens.css";
import "@workloom/ui/content-safety.css";
import "@workloom/ui/components.css";
createRoot(document.getElementById("root")!).render(
  <BrowserRouter><main className="min-h-screen" style={{padding:16}}>
    <h1>助手视口回归 · 明示模拟数据</h1>
    <p>真实组件与生产样式；接口为本地夹具，无数据库、模型或交易操作。</p>
    <LoomMate />
  </main></BrowserRouter>
);
`;
function itemsFor(count, long = count > 1) {
  return Array.from({ length: count }, (_, index) => ({
    id: `MC035-${index + 1}`, kind: "judge", level: "high",
    title: `模拟提醒 ${index + 1}${long ? "：请复核今日已完成事项与明日待处理计划，保留证据后再确认。" : ""}`,
    body: long ? "这是一条本地模拟的较长提醒，用来检查窄屏和短视口中的真实换行、滚动、操作可达性。请核对任务记录、负责人、处理时间以及异常说明；它不会触发真实业务操作。"
      : "本地模拟提醒，仅验证操作可达性。",
    actions: [{ label: "查看事项", link: `/p8/agent/MC035-${index + 1}` }],
    link: `/p8/agent/MC035-${index + 1}`, status: "unread", created_at: "2026-10-20T04:00:00.000Z",
  }));
}
function stateFor(mode, count) {
  return {
    settings: { member_no: "MC035-MOCK", display_name: "测试用户", persona_key: "tianmei", persona_custom: {},
      voice_key: "sweet", voice_on: false, widget_size: mode === "mini" ? "large" : mode,
      quiet_start: "22:00", quiet_end: "08:00", channels: {} },
    items: itemsFor(count), calls: [], failChat: false, holdChat: null, holdReadResponse: null,
    memory: { facts: Array.from({ length: 12 }, (_, index) => ({
      id: `memory-${index + 1}`, layer: "facts", mkey: `test-${index}`, source: "said",
      content: `模拟记忆 ${index + 1}：只记录本地测试输入，长内容必须能够滚动阅读与逐条删除。`,
      confidence: "1", created_at: "2026-10-20T04:00:00.000Z",
    })) },
  };
}
async function routeFixture(route, state, origin, observation) {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) {
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  if (url.pathname === "/api/voice/status") {
    return route.fulfill({ json: { enabled: false, configured: false, dataMode: "simulated" } });
  }
  if (url.pathname.startsWith("/trpc/")) {
    const names = decodeURIComponent(url.pathname.slice(6)).split(",");
    const inputs = JSON.parse(request.method() === "GET"
      ? url.searchParams.get("input") ?? "{}" : request.postData() ?? "{}");
    const responses = await Promise.all(names.map(async (name, index) => {
      const input = inputs[String(index)] ?? {};
      state.calls.push({ name, method: request.method(), input });
      let data;
      switch (name) {
        case "accounts.auth.guestEnter": data = { token: randomUUID() }; break;
        case "access.me": data = { role: "readonly", dataMode: "simulated" }; break;
        case "service.secretary.settings": data = { settings: state.settings }; break;
        case "service.secretary.inbox": data = { items: state.items }; break;
        case "service.secretary.scan": data = { added: 0 }; break;
        case "service.secretary.saveSettings":
          state.settings = { ...state.settings, ...input }; data = { settings: state.settings }; break;
        case "service.secretary.markInbox":
          assert.ok(Array.isArray(input.ids));
          assert.ok(["read", "acted"].includes(input.status));
          state.items = state.items.filter((item) => !input.ids.includes(item.id));
          if (input.status === "read" && state.holdReadResponse) await state.holdReadResponse;
          data = { dataMode: "simulated" }; break;
        case "service.secretary.memoryPanel": data = { memory: state.memory }; break;
        case "service.secretary.forget":
          assert.ok(state.memory.facts.some((row) => row.id === input.memoryId));
          state.memory.facts = state.memory.facts.filter((row) => row.id !== input.memoryId);
          data = { dataMode: "simulated" }; break;
        case "service.secretary.chat":
          if (state.holdChat) await state.holdChat;
          if (state.failChat) return { error: { message: "模拟对话暂时失败", code: -32603,
            data: { code: "INTERNAL_SERVER_ERROR", httpStatus: 500, path: name } } };
          data = { reply: `已收到本地模拟消息：${input.text}` }; break;
        default:
          observation.unexpectedProcedures.push(name);
          return { error: { message: "未登记的本地调用", code: -32601,
            data: { code: "NOT_FOUND", httpStatus: 404, path: name } } };
      }
      return { result: { data } };
    }));
    return route.fulfill({ json: url.searchParams.get("batch") === "1" ? responses : responses[0] });
  }
  if (/^\/(?:api|health)(?:\/|$)/.test(url.pathname)) {
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  return route.continue();
}
async function screenshot(page, observation, name) {
  if (OUT) await page.screenshot({ path: path.join(OUT, `${observation.label}-${name}.png`),
    fullPage: false, animations: "disabled" });
}
async function geometry(locator) {
  return locator.evaluate((element) => {
    const r = element.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    const ancestors = [];
    for (let node = element.parentElement; node; node = node.parentElement) {
      const s = getComputedStyle(node);
      if (/(auto|scroll|hidden|clip)/.test(s.overflowY)) ancestors.push({
        tag: node.tagName, role: node.getAttribute("role"), overflowY: s.overflowY,
        clientHeight: node.clientHeight, scrollHeight: node.scrollHeight, scrollTop: node.scrollTop,
      });
    }
    return { rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
      inside: r.left >= -1 && r.top >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
      receives: hit === element || element.contains(hit), hit: hit?.outerHTML.slice(0, 700) ?? null, ancestors };
  });
}
async function pointer(page, locator, observation, name) {
  let actual;
  try {
    // Resize and mode changes update through React and ResizeObserver. Wait for
    // real actionability, as an ordinary browser click does; never rewrite DOM.
    await expect.poll(async () => {
      await locator.scrollIntoViewIfNeeded();
      actual = await geometry(locator);
      return actual.inside && actual.receives;
    }, { timeout: 3_000, intervals: [50, 100, 200] }).toBe(true);
  } catch (error) {
    if (actual) observation.geometry.push({ name, ...actual });
    await screenshot(page, observation, name);
    if (actual) {
      assert.ok(actual.inside, `${name} must be inside the viewport after native scrolling`);
      assert.ok(actual.receives, `${name} must receive an ordinary pointer at its center`);
    }
    throw error;
  }
  observation.geometry.push({ name, ...actual });
  await locator.click();
  observation.actions.push({ name, method: "ordinary pointer click after native scroll" });
}
async function tabTo(page, locator, observation, name) {
  for (let i = 0; i < 100; i++) {
    if (await locator.evaluate((element) => element === document.activeElement)) {
      observation.actions.push({ name, method: "sequential Tab", presses: i });
      return;
    }
    await page.keyboard.press("Tab");
  }
  assert.fail(`${name} must be reachable through sequential Tab`);
}
async function noHorizontalOverflow(page, observation, name) {
  const value = await page.evaluate((selector) => ({
    documentWidth: document.documentElement.scrollWidth, viewport: innerWidth,
    surfaces: [...document.querySelectorAll(selector)].map((element) => ({
      width: element.clientWidth, scrollWidth: element.scrollWidth,
    })),
  }), '[role="region"][aria-label="小织助手面板"]');
  observation.geometry.push({ name, ...value });
  assert.ok(value.documentWidth <= value.viewport + 1, `${name}: document horizontal overflow`);
  assert.ok(value.surfaces.every((s) => s.scrollWidth <= s.width + 1), `${name}: assistant horizontal overflow`);
}
async function readableMiniBadge(page, count, observation, name) {
  const target = badge(page, count);
  await expect(target).toHaveCount(1);
  await target.scrollIntoViewIfNeeded();
  const actual = await target.evaluate((element) => {
    const text = [...element.childNodes].find((node) => node.nodeType === Node.TEXT_NODE);
    if (!text) throw new Error("The live numeric badge must contain a text node");
    const points = [];
    for (let i = 0; i < text.length; i++) {
      const range = document.createRange();
      range.setStart(text, i); range.setEnd(text, i + 1);
      const r = range.getBoundingClientRect();
      for (const fx of [0.25, 0.5, 0.75]) for (const fy of [0.3, 0.5, 0.7]) {
        const x = r.left + r.width * fx, y = r.top + r.height * fy;
        const hit = document.elementFromPoint(x, y);
        points.push({ character: text.textContent[i], x, y,
          visible: hit === element || element.contains(hit), hit: hit?.tagName ?? null });
      }
    }
    const r = element.getBoundingClientRect();
    return { points, text: element.textContent, width: r.width, height: r.height };
  });
  observation.geometry.push({ name, ...actual });
  if (!actual.points.every((p) => p.visible)) await screenshot(page, observation, name);
  assert.ok(actual.points.length > 0 && actual.points.every((p) => p.visible),
    `${name}: every sampled interior of each rendered digit must escape avatar clipping`);
}
async function avatarDimensions(page, mode, observation) {
  if (mode === "mini") return;
  const rendered = page.locator(`${SURFACE} [data-avatar-ready="true"]`);
  await expect(rendered).toHaveCount(1);
  const actual = await rendered.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { width: rect.width, height: rect.height,
      declaredWidth: parseFloat(element.style.width), declaredHeight: parseFloat(element.style.height) };
  });
  observation.geometry.push({ name: "avatar-size-preserved", ...actual });
  assert.ok(Math.abs(actual.width - actual.declaredWidth) < 1 && Math.abs(actual.height - actual.declaredHeight) < 1,
    "The real avatar keeps its declared product size instead of shrinking to fit");
  if (mode !== "fullscreen") {
    const hit = await avatar(page).evaluate((element) => {
      const rect = element.getBoundingClientRect(), parent = element.parentElement.getBoundingClientRect();
      return { widthRatio: rect.width / parent.width, heightRatio: rect.height / parent.height };
    });
    observation.geometry.push({ name: "corner-hit-area-preserved", ...hit });
    assert.ok(Math.abs(hit.widthRatio - 0.5) < 0.001 && Math.abs(hit.heightRatio - 0.44) < 0.001,
      "The corner interaction target remains 50% wide and 44% high");
  }
}
async function openChat(page, mode, observation) {
  if (mode === "mini") {
    await pointer(page, avatar(page), observation, "mini-expand");
    assert.equal(await panel(page).count(), 0, "Mini pointer keeps expand-only semantics");
    assert.equal(await page.evaluate(() => localStorage.getItem("loommate.mini")), "0");
  }
  await pointer(page, mode === "fullscreen" ? screenAvatar(page) : avatar(page), observation, "avatar-open");
  await expect(panel(page)).toBeVisible();
}
async function panelPaths(page, state, observation) {
  await pointer(page, closePanel(page), observation, "close-panel");
  await expect(panel(page)).toHaveCount(0);
  const target = () => state.settings.widget_size === "fullscreen" ? screenAvatar(page) : avatar(page);
  for (const key of ["Enter", "Space"]) {
    await tabTo(page, target(), observation, `avatar-${key}`);
    await page.keyboard.press(key);
    await expect(panel(page)).toBeVisible();
    await tabTo(page, closePanel(page), observation, `close-${key}`);
    await page.keyboard.press(key);
    await expect(panel(page)).toHaveCount(0);
  }
  await pointer(page, target(), observation, "reopen-panel");
  const input = page.getByPlaceholder("跟小织说点什么……", { exact: true });
  await input.scrollIntoViewIfNeeded();
  await input.fill("本地模拟：滚动后输入，编号１２３，标点！？");
  await pointer(page, page.getByRole("button", { name: "发送", exact: true }), observation, "send");
  await expect.poll(() => state.calls.filter((c) => c.name === "service.secretary.chat").length).toBe(1);
  await expect(panel(page).getByText("已收到本地模拟消息：本地模拟：滚动后输入，编号１２３，标点！？", { exact: true })).toBeVisible();
  await pointer(page, page.getByRole("button", { name: "设置", exact: true }), observation, "settings");
  await page.getByLabel("它怎么称呼您", { exact: true }).fill("滚动测试用户");
  await pointer(page, page.getByRole("button", { name: "保存（小织立即生效）", exact: true }), observation, "settings-save");
  await expect.poll(() => state.settings.display_name).toBe("滚动测试用户");
  await pointer(page, page.getByRole("button", { name: "记忆", exact: true }), observation, "memory");
  await expect(page.getByRole("button", { name: "删除这条记忆", exact: true })).toHaveCount(12);
  await pointer(page, page.getByRole("button", { name: "删除这条记忆", exact: true }).last(), observation, "memory-delete-last");
  await expect(page.getByRole("button", { name: "删除这条记忆", exact: true })).toHaveCount(11);
  await expect.poll(() => state.calls.filter((c) => c.name === "service.secretary.forget").length).toBe(1);
  assert.deepEqual(state.calls.filter((c) => c.name === "service.secretary.forget").at(-1).input, { memoryId: "memory-12" });
  await noHorizontalOverflow(page, observation, "memory-layout");
  await screenshot(page, observation, "memory");
  await pointer(page, closePanel(page), observation, "close-memory");
  await expect(panel(page)).toHaveCount(0);
}
async function notificationPaths(page, state, observation) {
  const [first, next] = state.items;
  const card = (item) => page.locator("div:has(> .wl-action-row)")
    .filter({ has: page.getByText(item.title, { exact: true }) });
  await pointer(page, card(first).getByRole("button", { name: "稍后", exact: true }), observation, "later");
  await expect.poll(() => state.items.length).toBe(observation.count - 1);
  assert.deepEqual(state.calls.filter((c) => c.name === "service.secretary.markInbox").at(-1).input,
    { ids: ["MC035-1"], status: "read" });
  // The HTTP fixture changes state before React receives the response. Wait for
  // the dismissed card to leave the real DOM before targeting the next item.
  await expect(page.getByText(first.title, { exact: true })).toHaveCount(0);
  if (observation.count > 1) {
    await expect(card(next)).toBeVisible();
    await pointer(page, card(next).getByRole("button", { name: "查看事项", exact: true }), observation, "act");
    await expect.poll(() => state.items.length).toBe(observation.count - 2);
    assert.deepEqual(state.calls.filter((c) => c.name === "service.secretary.markInbox").at(-1).input,
      { ids: ["MC035-2"], status: "acted" });
    await expect(page.getByText(next.title, { exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/\/agents\/MC035-2$/);
  }
}

test("MC-035 assistant controls and unread digits remain usable in limited viewports", { timeout: 600_000 }, async (t) => {
  const receipt = { card: "MC-035", role: "repair", environment: "local-preview",
    startedAt: new Date().toISOString(), sourceOverride: process.env.MC035_SOURCE_DIR ? SOURCE_ROOT : null,
    sourceBefore: await sourceHashes(), runnerSha256: sha256(await readFile(TEST_FILE)),
    fixture: "Real LoomMate / production CSS / vector2d; synthetic local HTTP only; no backend, DB, model or broker",
    browserChannel: process.env.MC035_BROWSER_CHANNEL ?? "chromium", observations: [], checks: [] };
  let server, browser, failure = null;
  if (OUT) await mkdir(OUT, { recursive: true });
  try {
    const original = process.env.MC035_SOURCE_DIR ? await readFile(path.join(SOURCE_ROOT, COMPONENT), "utf8") : null;
    const tokensPath = path.join(WEB, "src/styles/tokens.css");
    // The old component must contribute its own utilities when running a frozen
    // source. Vite's module override alone is not visible to Tailwind's scanner.
    const originalTokens = original === null ? null : `${await readFile(tokensPath, "utf8")}\n@source ${
      JSON.stringify(path.relative(path.dirname(tokensPath), path.join(SOURCE_ROOT, COMPONENT)).replaceAll("\\", "/"))
    };\n`;
    receipt.tailwindSourceOverride = original === null ? null : {
      productionTokens: "governance/apps/web/src/styles/tokens.css", component: path.join(SOURCE_ROOT, COMPONENT),
    };
    server = await createServer({ root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{ name: "mc035-browser-fixture", enforce: "pre",
        resolveId(id) { if (id === "/__mc035_fixture__.tsx") return fixtureId; },
        load(id) {
          if (id === fixtureId) return fixtureSource;
          if (original !== null && id === path.join(ROOT, COMPONENT)) return original;
          if (originalTokens !== null && id === tokensPath) return originalTokens;
        },
        configureServer(vite) {
          vite.middlewares.use(async (req, res, next) => {
            if (!req.url?.startsWith("/__mc035__?")) return next();
            try {
              const html = await vite.transformIndexHtml("/__mc035__", `<!doctype html><html lang="zh-CN" data-wl-theme="dark">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title data-workloom-product-title>助手视口回归</title></head><body data-workloom-client="b-pc"><div id="root"></div>
<script type="module" src="/__mc035_fixture__.tsx"></script></body></html>`);
              res.statusCode = 200; res.setHeader("Content-Type", "text/html; charset=utf-8"); res.end(html);
            } catch (error) { res.statusCode = 500; res.end(String(error)); }
          });
        },
      }],
    });
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address === "object");
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({ ...(process.env.MC035_BROWSER_CHANNEL ? { channel: process.env.MC035_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"] });
    receipt.browserVersion = browser.version();
    async function scenario(label, viewport, mode, count, run, init = {}) {
      await t.test(label, async () => {
        const observation = { label, viewport, mode, count, preconditions: { freshContext: true, syntheticHTTP: true, ...init },
          expected: "Native scroll and ordinary pointer / keyboard reach controls; no clipped digits or horizontal overflow",
          geometry: [], actions: [], pageErrors: [], consoleErrors: [], responseErrors: [],
          blockedRequests: [], unexpectedProcedures: [], passed: false, recordWritten: !OUT };
        receipt.observations.push(observation);
        const state = stateFor(mode, count);
        const context = await browser.newContext({ viewport, locale: "zh-CN", serviceWorkers: "block", reducedMotion: "reduce" });
        try {
          await context.route("**/*", (route) => routeFixture(route, state, origin, observation));
          await context.addInitScript(({ mode, init }) => {
            if (!init.defaultMini) localStorage.setItem("loommate.mini", mode === "mini" ? "1" : "0");
            if (init.pos) localStorage.setItem("loommate.pos", JSON.stringify(init.pos));
            localStorage.setItem("wl-voice-gate", "captions");
          }, { mode, init });
          const page = await context.newPage();
          page.setDefaultTimeout(15_000);
          page.setDefaultNavigationTimeout(30_000);
          page.on("pageerror", (error) => observation.pageErrors.push(String(error)));
          page.on("console", (message) => { if (message.type() === "error") observation.consoleErrors.push(message.text()); });
          page.on("response", (response) => {
            if (response.status() >= 400) observation.responseErrors.push({ status: response.status(), url: response.url() });
          });
          await page.clock.install({ time: new Date("2026-10-20T04:00:00Z") });
          await page.goto(`${origin}/__mc035__?render=vector2d`);
          await page.locator(SURFACE).waitFor();
          await expect.poll(() => state.calls.filter((c) => c.name === "service.secretary.inbox").length).toBeGreaterThan(0);
          if (count > 0) await expect(badge(page, count)).toHaveCount(1);
          if (mode === "fullscreen") await expect(page.getByRole("dialog", { name: "小织屏保" })).toBeVisible();
          else if (mode === "mini") await expect(avatar(page)).toHaveJSProperty("tagName", "BUTTON");
          else await expect(page.getByTitle("切换大小", { exact: true })).toBeVisible();
          await page.evaluate(async () => { await document.fonts.ready; });
          await screenshot(page, observation, "initial");
          await run(page, state, observation);
          assert.deepEqual(observation.pageErrors, []);
          assert.deepEqual(observation.consoleErrors, []);
          assert.deepEqual(observation.responseErrors, []);
          assert.deepEqual(observation.blockedRequests, []);
          assert.deepEqual(observation.unexpectedProcedures, []);
          observation.passed = true;
        } catch (error) {
          observation.error = String(error); throw error;
        } finally {
          observation.calls = state.calls;
          try { await context.close(); } catch (error) { observation.passed = false; observation.cleanupError = String(error); throw error; }
          finally {
            if (OUT) {
              try {
                observation.recordWritten = true;
                await writeFile(path.join(OUT, `${label}.json`), JSON.stringify(observation, null, 2) + "\n");
              } catch (error) {
                observation.recordWritten = false; observation.passed = false;
                observation.outputError = String(error); throw error;
              }
            }
          }
        }
      });
    }
    const viewports = [{ width: 1440, height: 900 }, { width: 390, height: 844 },
      { width: 1440, height: 360 }, { width: 390, height: 360 }];
    for (const viewport of viewports) for (const mode of ["mini", "small", "large", "fullscreen"]) {
      for (const count of [0, 1, 12]) {
        await scenario(`${viewport.width}x${viewport.height}-${mode}-${count}`, viewport, mode, count,
          async (page, state, observation) => {
            await noHorizontalOverflow(page, observation, "initial-layout");
            await avatarDimensions(page, mode, observation);
            if (mode === "mini" && count > 0) {
              await readableMiniBadge(page, count, observation, "digits-default");
              await avatar(page).hover();
              await screenshot(page, observation, "hover");
              await readableMiniBadge(page, count, observation, "digits-hover");
              await page.mouse.move(1, 1);
            }
            if (count > 0) await notificationPaths(page, state, observation);
            await openChat(page, mode, observation);
            await noHorizontalOverflow(page, observation, "chat-layout");
            await screenshot(page, observation, "chat");
            if (count === 12) await panelPaths(page, state, observation);
            else {
              await pointer(page, closePanel(page), observation, "close-panel");
              await expect(panel(page)).toHaveCount(0);
            }
            if (mode === "fullscreen") {
              await pointer(page, page.getByRole("button", { name: "退出屏保（Esc）", exact: true }), observation, "exit");
              await expect.poll(() => state.settings.widget_size).toBe("small");
              await expect(page.getByRole("dialog", { name: "小织屏保" })).toHaveCount(0);
            }
          });
      }
    }
    for (const viewport of viewports) {
      await scenario(`${viewport.width}x${viewport.height}-movement`, viewport, "small", 0, async (page, state, observation) => {
        await tabTo(page, avatar(page), observation, "move-avatar");
        const before = await avatar(page).boundingBox();
        await page.keyboard.press("ArrowLeft");
        const first = await page.evaluate(() => JSON.parse(localStorage.getItem("loommate.pos")));
        await page.keyboard.press("Shift+ArrowRight");
        const second = await page.evaluate(() => JSON.parse(localStorage.getItem("loommate.pos")));
        assert.ok(Number.isFinite(first.x) && Number.isFinite(second.x) && second.x >= first.x && second.x - first.x <= 48);
        observation.actions.push({ name: "arrows-persist", before, first, second });
        await avatar(page).scrollIntoViewIfNeeded();
        const r = await avatar(page).boundingBox();
        await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
        await page.mouse.down();
        await page.mouse.move(r.x - 100, Math.max(5, r.y - 70), { steps: 8 });
        await page.mouse.up();
        assert.equal(await panel(page).count(), 0, "Dragging beyond 6px must not open chat");
        const position = await page.evaluate(() => JSON.parse(localStorage.getItem("loommate.pos")));
        assert.ok(Number.isFinite(position.x) && Number.isFinite(position.y));
        await openChat(page, "small", observation);
        await pointer(page, closePanel(page), observation, "positioned-panel-close");
        await expect(panel(page)).toHaveCount(0);
        await pointer(page, page.getByTitle("切换大小", { exact: true }), observation, "size-large");
        await expect.poll(() => state.settings.widget_size).toBe("large");
        await openChat(page, "large", observation);
        await pointer(page, closePanel(page), observation, "resized-panel-close");
        // Each width has its own real layout-hook initialization above. This
        // component fixture does not mount the App shell's breakpoint broadcasts.
        await page.setViewportSize({ width: viewport.width, height: 360 });
        await openChat(page, "large", observation);
        await pointer(page, closePanel(page), observation, "viewport-resized-panel-close");
        await noHorizontalOverflow(page, observation, "resized");
        await screenshot(page, observation, "resized");
        await pointer(page, page.getByTitle("切换大小", { exact: true }), observation, "size-fullscreen");
        await expect(page.getByRole("dialog", { name: "小织屏保" })).toBeVisible();
        await page.keyboard.press("Escape");
        await expect.poll(() => state.settings.widget_size).toBe("small");
        await expect(page.getByRole("dialog", { name: "小织屏保" })).toHaveCount(0);
        await pointer(page, page.getByTitle("隐藏（屏幕边缘留「小织」把手，点一下唤回）", { exact: true }), observation, "hide");
        assert.equal(await page.evaluate(() => localStorage.getItem("loommate.hidden")), "1");
        await expect(page.locator(SURFACE)).toHaveCount(0);
        await pointer(page, page.getByTitle("唤回小织", { exact: true }), observation, "show-handle");
        assert.equal(await page.evaluate(() => localStorage.getItem("loommate.hidden")), "0");
        await page.reload();
        await expect(page.getByTitle("切换大小", { exact: true })).toBeVisible();
        await openChat(page, "small", observation);
        await pointer(page, closePanel(page), observation, "persisted-position-close");
        await expect(panel(page)).toHaveCount(0);
        await noHorizontalOverflow(page, observation, "persisted-position");
      }, { pos: { x: 10000, y: 10000 } });
    }
    await scenario("default-mini-and-chat-failure", { width: 390, height: 360 }, "mini", 0, async (page, state, observation) => {
      assert.equal(await page.evaluate(() => localStorage.getItem("loommate.mini")), null, "First visit keeps the current mini default");
      await tabTo(page, avatar(page), observation, "default-mini");
      await page.keyboard.press("Enter");
      await expect(panel(page)).toBeVisible();
      assert.equal(await page.evaluate(() => localStorage.getItem("loommate.mini")), "0");
      const input = page.getByPlaceholder("跟小织说点什么……", { exact: true });
      await input.fill("   ");
      await pointer(page, page.getByRole("button", { name: "发送", exact: true }), observation, "empty-send");
      assert.equal(state.calls.filter((c) => c.name === "service.secretary.chat").length, 0);
      let release;
      state.holdChat = new Promise((resolve) => { release = resolve; });
      state.failChat = true;
      await input.fill("模拟失败请求");
      await pointer(page, page.getByRole("button", { name: "发送", exact: true }), observation, "failing-send");
      await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
      release();
      await expect(page.getByText("呜……信号暂时不稳定。刚才的话已保留，请稍后再试。", { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
      state.failChat = false; state.holdChat = null;
      await input.fill("模拟恢复请求");
      await input.press("Enter");
      await expect(page.getByText("已收到本地模拟消息：模拟恢复请求", { exact: true })).toBeVisible();
      await pointer(page, closePanel(page), observation, "failure-recovery-close");
      await pointer(page, page.getByTitle("收起为小球（不挡界面）", { exact: true }), observation, "collapse");
      assert.equal(await page.evaluate(() => localStorage.getItem("loommate.mini")), "1");
      await page.reload();
      await expect(avatar(page)).toHaveJSProperty("tagName", "BUTTON");
      await expect(panel(page)).toHaveCount(0);
      observation.actions.push({ name: "mini-preference-reload", passed: true });
    }, { defaultMini: true });
    await scenario("notification-response-before-dom-commit", { width: 1440, height: 900 }, "mini", 12,
      async (page, state, observation) => {
        const [first, next] = state.items;
        let release;
        state.holdReadResponse = new Promise((resolve) => { release = resolve; });
        // Observe the same flow as every layout scenario while only its HTTP
        // response is pending. No sleep, DOM mutation, or click bypass is used.
        const flow = notificationPaths(page, state, observation);
        const outcome = flow.then(() => ({ passed: true }), (error) => ({ passed: false, error }));
        try {
          await expect.poll(() => state.items.length).toBe(11);
          await expect(page.getByText(first.title, { exact: true })).toHaveCount(1);
          const acted = state.calls.filter((c) => c.name === "service.secretary.markInbox" && c.input.status === "acted");
          observation.notificationSynchronization = {
            beforeResponse: { serverItemCount: state.items.length,
              firstTitlePresent: await page.getByText(first.title, { exact: true }).count(),
              firstId: first.id, nextId: next.id, actedCalls: acted },
          };
          assert.deepEqual(acted, [], "The next action must wait while the dismissed card is still in the DOM");
          await screenshot(page, observation, "read-response-held");
        } finally {
          release();
          state.holdReadResponse = null;
          const result = await outcome;
          if (!result.passed) throw result.error;
        }
        observation.notificationSynchronization.afterResponse = {
          serverItemCount: state.items.length,
          firstTitlePresent: await page.getByText(first.title, { exact: true }).count(),
          nextTitlePresent: await page.getByText(next.title, { exact: true }).count(),
          actedCalls: state.calls.filter((c) => c.name === "service.secretary.markInbox" && c.input.status === "acted"),
          pathname: new URL(page.url()).pathname,
        };
        assert.equal(observation.notificationSynchronization.afterResponse.firstTitlePresent, 0);
        assert.equal(observation.notificationSynchronization.afterResponse.nextTitlePresent, 0);
        await screenshot(page, observation, "correct-item-after-release");
      });
    await t.test("Source and runner bytes remain stable", async () => {
      const check = { name: "source-stability", passed: false }; receipt.checks.push(check);
      assert.deepEqual(await sourceHashes(), receipt.sourceBefore);
      assert.equal(sha256(await readFile(TEST_FILE)), receipt.runnerSha256);
      check.passed = true;
    });
  } catch (error) { failure = String(error); throw error; }
  finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors = cleanup.filter((result) => result.status === "rejected").map((result) => String(result.reason));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.runnerStable = sha256(await readFile(TEST_FILE)) === receipt.runnerSha256;
    receipt.finishedAt = new Date().toISOString(); receipt.error = failure;
    receipt.passed = !failure && receipt.observations.length === 54 && receipt.sourceStable && receipt.runnerStable
      && receipt.cleanupErrors.length === 0 && receipt.checks.length === 1 && receipt.checks.every((c) => c.passed)
      && receipt.observations.every((o) => o.passed && o.recordWritten);
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, [], "Fixture and browser close cleanly");
  }
});
