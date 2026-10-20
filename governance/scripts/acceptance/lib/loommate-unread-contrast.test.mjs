/**
 * MC-034: real LoomMate, tRPC client, Vite and production CSS in a browser.
 * Only HTTP responses are synthetic; no backend, database or model is contacted.
 * The product's existing ?render=vector2d path avoids a WebGL requirement.
 * Run from the repository root with node --test. CI uses Chromium by default;
 * MC034_BROWSER_CHANNEL=chrome selects installed Chrome for local runs.
 * MC034_SOURCE_DIR is exclusively a red-test source override: it contains the
 * original component at its repository-relative path. Acceptance must unset it.
 * Override source is explicitly registered with Tailwind so removed old utilities
 * are still generated from that real source, without substituting CSS rules.
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
const STYLES = "governance/apps/web/src/styles/tokens.css";
const SOURCE_ROOT = path.resolve(process.env.MC034_SOURCE_DIR ?? ROOT);
const tailwindAdditionalSource = process.env.MC034_SOURCE_DIR
  ? path.relative(path.dirname(path.join(ROOT, STYLES)), path.join(SOURCE_ROOT, COMPONENT)).split(path.sep).join("/")
  : null;
const OUT = process.env.MC034_EVIDENCE_DIR ? path.resolve(process.env.MC034_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const requireGovernance = createRequire(path.join(ROOT, "governance/package.json"));
const { chromium, expect } = requireWeb("@playwright/test");
const { AxeBuilder } = requireGovernance("@axe-core/playwright");
const { createServer } = await import(requireWeb.resolve("vite"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  COMPONENT,
  "governance/apps/web/src/App.tsx",
  "governance/apps/web/src/lib/trpc.ts",
  "governance/apps/web/src/lib/product.ts",
  "governance/apps/web/src/lib/useAskRail.ts",
  "governance/apps/web/src/shell/NavMenu.tsx",
  "governance/apps/web/src/voice/VoiceEngine.ts",
  "governance/apps/web/src/voice/neuralVoice.ts",
  "governance/apps/web/src/components/loommate/MateLive2D.tsx",
  "governance/apps/web/src/components/loommate/speechText.ts",
  "governance/apps/web/src/components/loomball/index.ts",
  "governance/apps/web/src/components/loomball/LoomBall.tsx",
  "governance/apps/web/src/components/loomball/engine.ts",
  "governance/apps/web/src/components/loomball/agent-emotion.ts",
  "governance/apps/web/src/components/loomball/emotions-workloom.ts",
  "governance/apps/web/src/components/loomball/useStableEmotion.ts",
  "governance/apps/web/src/styles/tokens.css",
  "governance/apps/web/src/styles/subtitle.css",
  "governance/apps/web/vite.config.ts",
  "governance/apps/web/package.json",
  "governance/scripts/vite-product.mjs",
  "governance/scripts/product-runtime.mjs",
  "governance/apps/web/node_modules/@workloom/ui/package.json",
  ...["index.js", "language.js", "icons.js", "managed-surface.js", "tokens.css", "content-safety.css", "components.css"]
    .map((file) => `governance/apps/web/node_modules/@workloom/ui/dist/${file}`),
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [
  file, sha256(await readFile(path.join(file === COMPONENT ? SOURCE_ROOT : ROOT, file))),
])));
const SURFACE = '[data-wl-managed-surface="assistant-companion"]';
const NUMBER_SPANS = `${SURFACE} span`;
const avatarControl = (page) => page.getByRole("button", { name: /^小织助手；/ });
const unreadBadge = (page, count) => page.locator(NUMBER_SPANS).filter({ hasText: new RegExp(`^${count}$`) });
const fixtureId = path.join(WEB, "__mc034_loommate_fixture__.tsx");
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
  <BrowserRouter>
    {/* Match the real App shell's min-h-screen. A short body plus the modal's
        real scroll lock otherwise makes axe treat a fixed badge as clipped. */}
    <main className="min-h-screen" style={{padding:16}}>
      <h1>未读数量回归 · 明示模拟数据</h1>
      <p>真实助手与生产样式；接口响应为本地夹具，无数据库或外部操作。</p>
      <LoomMate />
    </main>
  </BrowserRouter>
);
`;

function itemsFor(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `MC034-${index + 1}`, kind: "judge", level: "high",
    title: `模拟提醒 ${index + 1}`, body: "这是模拟数据，仅验证数量和本地交互。",
    actions: [{ label: "查看事项", link: `/p8/agent/MC034-${index + 1}` }],
    link: `/p8/agent/MC034-${index + 1}`, status: "unread", created_at: "2026-10-20T04:00:00.000Z",
  }));
}

function fixtureState(mode, count) {
  return {
    settings: {
      member_no: "MC034-MOCK", display_name: "测试用户", persona_key: "tianmei",
      persona_custom: {}, voice_key: "sweet", voice_on: false,
      widget_size: mode === "fullscreen" ? "fullscreen" : "large",
      quiet_start: "22:00", quiet_end: "08:00", channels: {},
    },
    items: itemsFor(count), calls: [], failInbox: false,
  };
}

async function routeFixture(route, state, origin, observation) {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin !== origin) {
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  if (url.pathname === "/api/voice/status") {
    return route.fulfill({ json: { enabled: false, configured: false, dataMode: "simulated" } });
  }
  if (url.pathname.startsWith("/trpc/")) {
    const names = decodeURIComponent(url.pathname.slice("/trpc/".length)).split(",");
    const inputs = JSON.parse(request.method() === "GET"
      ? url.searchParams.get("input") ?? "{}" : request.postData() ?? "{}");
    const responses = names.map((name, index) => {
      const input = inputs[String(index)] ?? {};
      state.calls.push({ name, method: request.method(), input });
      let data;
      switch (name) {
        case "accounts.auth.guestEnter":
          data = { token: randomUUID() };
          break;
        case "access.me":
          data = { role: "readonly", dataMode: "simulated" };
          break;
        case "service.secretary.settings":
          data = { settings: state.settings };
          break;
        case "service.secretary.inbox":
          if (state.failInbox) return {
            error: {
              message: "模拟收件箱暂时不可用", code: -32603,
              data: { code: "INTERNAL_SERVER_ERROR", httpStatus: 500, path: name },
            },
          };
          data = { items: state.items };
          break;
        case "service.secretary.scan":
          data = { added: 0 };
          break;
        case "service.secretary.saveSettings":
          state.settings = { ...state.settings, ...input };
          data = { settings: state.settings };
          break;
        case "service.secretary.markInbox":
          assert.ok(Array.isArray(input.ids), "Real tRPC markInbox input must contain ids");
          assert.ok(["read", "acted"].includes(input.status));
          state.items = state.items.filter((item) => !input.ids.includes(item.id));
          data = { dataMode: "simulated" };
          break;
        default:
          observation.unexpectedProcedures.push(name);
          return { error: { message: "未登记的模拟调用", code: -32601,
            data: { code: "NOT_FOUND", httpStatus: 404, path: name } } };
      }
      return { result: { data } };
    });
    return route.fulfill({ json: url.searchParams.get("batch") === "1" ? responses : responses[0] });
  }
  if (/^\/(?:api|health)(?:\/|$)/.test(url.pathname)) {
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  return route.continue();
}

function contrastOf(foreground, background) {
  const luminance = (css) => {
    const match = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/.exec(css);
    assert.ok(match, `Expected an opaque browser-resolved RGB color, got ${css}`);
    const [r, g, b] = match.slice(1).map(Number).map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const a = luminance(foreground), b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

async function measure(page, count) {
  const badge = unreadBadge(page, count);
  const documentSize = await page.evaluate(() => ({
    width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
  }));
  if (count === 0) {
    const digits = await page.locator(NUMBER_SPANS).filter({ hasText: /^\d+$/ }).count();
    return { count, numberSpans: digits, document: documentSize };
  }
  await expect(badge).toHaveCount(1);
  await expect(badge).toBeVisible();
  const actual = await badge.evaluate((element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(element);
    const text = range.getBoundingClientRect();
    element.setAttribute("data-mc034-unread", "observed");
    return {
      text: element.textContent.trim(), foreground: style.color, background: style.backgroundColor,
      fontSize: parseFloat(style.fontSize), fontWeight: Number(style.fontWeight),
      width: parseFloat(style.width), height: parseFloat(style.height),
      minWidth: parseFloat(style.minWidth), paddingLeft: parseFloat(style.paddingLeft),
      paddingRight: parseFloat(style.paddingRight), opacity: Number(style.opacity),
      visibility: style.visibility, display: style.display,
      rect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
      textRect: { left: text.left, right: text.right, top: text.top, bottom: text.bottom },
      focusable: element.tabIndex >= 0,
    };
  });
  const axe = await new AxeBuilder({ page })
    .include("[data-mc034-unread]").withRules(["color-contrast"]).analyze();
  return {
    count, ...actual, contrast: contrastOf(actual.foreground, actual.background), document: documentSize,
    axe: {
      violations: axe.violations, incomplete: axe.incomplete,
      passes: axe.passes.filter((rule) => rule.id === "color-contrast"),
    },
  };
}

function checkMeasurement(observation, measurement, mode, theme) {
  observation.measurements.push(measurement);
  const check = (ok, message) => { if (!ok) observation.failures.push(message); };
  check(measurement.document.scrollWidth <= observation.viewport.width + 1, "Horizontal document overflow");
  if (measurement.count === 0) {
    check(measurement.numberSpans === 0, "Zero unread must not render a numeric badge");
    return;
  }
  const prefix = `${mode}/${theme}/${measurement.count}: `;
  check(measurement.text === String(measurement.count), prefix + "Unread text changed");
  check(measurement.contrast >= 4.5, prefix + `Contrast ${measurement.contrast} is below 4.5:1`);
  check(measurement.background === (theme === "dark" ? "rgb(255, 125, 125)" : "rgb(201, 42, 42)"),
    prefix + "Existing alert background changed");
  check(measurement.fontSize === (mode === "fullscreen" ? 16 : 14) && measurement.fontWeight === 700,
    prefix + "Existing normal-sized bold typography changed");
  const height = { mini: 20, expanded: 24, fullscreen: 40 }[mode];
  check(measurement.height === height, prefix + "Existing badge height changed");
  check(measurement.paddingLeft === (mode === "fullscreen" ? 8 : 4)
    && measurement.paddingRight === (mode === "fullscreen" ? 8 : 4), prefix + "Badge padding changed");
  check(measurement.opacity === 1 && measurement.visibility === "visible" && measurement.display !== "none",
    prefix + "Badge was hidden instead of made readable");
  check(!measurement.focusable, prefix + "Display-only count acquired a tab stop");
  check(measurement.axe.violations.length === 0 && measurement.axe.incomplete.length === 0,
    prefix + "axe color-contrast failed or could not determine a result");
  check(measurement.axe.passes.some((rule) => rule.nodes.length > 0),
    prefix + "axe must positively evaluate the real unread text");
}

async function tabTo(page, locator) {
  for (let attempt = 0; attempt < 30; attempt++) {
    if (await locator.evaluate((element) => element === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  assert.fail("The existing assistant control was not reachable by sequential Tab");
}

async function recordPointerGeometry(page, locator, observation, name) {
  const geometry = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    return {
      viewport: { width: innerWidth, height: innerHeight },
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
      insideViewport: rect.left >= 0 && rect.top >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
      receivesCenterPointer: hit === element || element.contains(hit),
      target: element.outerHTML,
      hit: hit?.outerHTML ?? null,
      hitParent: hit?.parentElement?.outerHTML ?? null,
    };
  });
  observation.pointerGeometry.push({ name, ...geometry });
  if (OUT && (!geometry.insideViewport || !geometry.receivesCenterPointer)) {
    await page.screenshot({ path: path.join(OUT, `${observation.label}-${name}.png`),
      fullPage: false, animations: "disabled" });
  }
}

async function interact(page, state, mode, observation) {
  const closePanel = async () => {
    const close = page.getByRole("button", { name: "关闭小织助手面板", exact: true });
    // The original large assistant can put this control above the viewport.
    // Retain that separate layout evidence; keyboard activation is its own path,
    // and is not reported as a successful pointer activation.
    await recordPointerGeometry(page, close, observation, "panel-close");
    await tabTo(page, close);
    await page.keyboard.press("Enter");
    await expect(page.getByRole("region", { name: "小织助手面板" })).toHaveCount(0);
  };
  const panel = page.getByRole("region", { name: "小织助手面板" });
  const screenAvatar = () => page.getByRole("dialog", { name: "小织屏保", exact: true })
    .getByRole("button").filter({ has: page.locator('[data-avatar-ready="true"], img[alt="小织"]') });
  if (mode === "mini") {
    await avatarControl(page).click();
    await expect(page.getByTitle("切换大小", { exact: true })).toBeVisible();
    assert.equal(await panel.count(), 0, "Mini pointer click keeps its existing expand-only behavior");
    assert.equal(await page.evaluate(() => localStorage.getItem("loommate.mini")), "0");
    await page.getByTitle("收起为小球（不挡界面）", { exact: true }).click();
  } else {
    await (mode === "fullscreen" ? screenAvatar() : avatarControl(page)).click();
    await expect(panel).toBeVisible();
    await closePanel();
  }
  for (const key of ["Enter", "Space"]) {
    const target = mode === "fullscreen" ? screenAvatar() : avatarControl(page);
    await tabTo(page, target);
    await page.keyboard.press(key);
    await expect(panel).toBeVisible();
    await closePanel();
    if (mode === "mini") await page.getByTitle("收起为小球（不挡界面）", { exact: true }).click();
  }
  if (mode === "fullscreen") {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "小织屏保" })).toHaveCount(0);
    await expect.poll(() => state.settings.widget_size).toBe("small");
    for (const next of ["large", "fullscreen"]) {
      await page.getByTitle("切换大小", { exact: true }).click();
      await expect.poll(() => state.settings.widget_size).toBe(next);
    }
    await expect(page.getByRole("dialog", { name: "小织屏保" })).toBeVisible();
  } else {
    await tabTo(page, avatarControl(page));
    await page.keyboard.press("ArrowLeft");
    const first = await page.evaluate(() => JSON.parse(localStorage.getItem("loommate.pos")));
    await page.keyboard.press("Shift+ArrowRight");
    const second = await page.evaluate(() => JSON.parse(localStorage.getItem("loommate.pos")));
    assert.ok(Number.isFinite(first.x) && Number.isFinite(second.x) && second.x >= first.x
      && second.x - first.x <= 48, "Keyboard movement remains bounded and persists the position");
  }
  observation.interactions.push({ avatarPointer: true, panelClose: "sequential Tab + Enter",
    sequentialTab: true, enter: true, space: true,
    escapeAndSizeCycle: mode === "fullscreen", boundedArrowMovement: mode !== "fullscreen" });

  state.items = itemsFor(13);
  const scans = state.calls.filter((call) => call.name === "service.secretary.scan").length;
  await page.clock.fastForward(20_000);
  await expect(unreadBadge(page, 13)).toHaveCount(1);
  assert.ok(state.calls.filter((call) => call.name === "service.secretary.scan").length > scans,
    "Count increase must use the real component heartbeat, not a remount or DOM mutation");
  if (mode === "mini") await page.getByRole("button", { name: /^查看未读提醒/ }).click();
  const later = page.getByRole("button", { name: "稍后", exact: true }).first();
  // At 390px the original fullscreen live strip can cover this button. Observe
  // that geometry explicitly without conflating it with badge contrast coverage.
  await recordPointerGeometry(page, later, observation, "later");
  await tabTo(page, later);
  await page.keyboard.press("Enter");
  await expect(unreadBadge(page, 12)).toHaveCount(1);
  assert.deepEqual(state.calls.filter((call) => call.name === "service.secretary.markInbox").at(-1).input,
    { ids: ["MC034-1"], status: "read" });
  await tabTo(page, page.getByRole("button", { name: "查看事项", exact: true }).first());
  await page.keyboard.press("Enter");
  await expect(unreadBadge(page, 11)).toHaveCount(1);
  assert.deepEqual(state.calls.filter((call) => call.name === "service.secretary.markInbox").at(-1).input,
    { ids: ["MC034-2"], status: "acted" });
  assert.equal(new URL(page.url()).pathname, "/agents/MC034-2", "Existing canonical navigation must remain intact");
  observation.interactions.push({ heartbeat: "12→13", later: "13→12/read", act: "12→11/acted",
    navigation: "/agents/MC034-2" });

  state.failInbox = true;
  await page.clock.fastForward(20_000);
  await expect(page.locator(NUMBER_SPANS).filter({ hasText: /^\d+$/ })).toHaveCount(0);
  state.failInbox = false;
  state.items = itemsFor(1);
  await page.clock.fastForward(20_000);
  await expect(unreadBadge(page, 1)).toHaveCount(1);
  if (mode === "mini") await page.getByRole("button", { name: /^查看未读提醒/ }).click();
  await tabTo(page, page.getByRole("button", { name: "稍后", exact: true }).first());
  await page.keyboard.press("Enter");
  await expect(page.locator(NUMBER_SPANS).filter({ hasText: /^\d+$/ })).toHaveCount(0);
  observation.interactions.push({ inboxFailure: "existing empty fallback", recovery: "1", lastRead: "1→0" });
}

async function closeAndRecord(context, observation, receipt) {
  let failure;
  try {
    await context.close();
  } catch (error) {
    failure = error;
    observation.error ??= String(error);
    receipt.cleanupErrors.push(`${observation.label}: ${String(error)}`);
  }
  try {
    if (OUT) await writeFile(path.join(OUT, `${observation.label}.json`), JSON.stringify(observation, null, 2) + "\n");
  } catch (error) {
    failure ??= error;
    observation.error ??= String(error);
  }
  if (failure) throw failure;
}

test("MC-034 unread numbers remain readable across actual assistant modes", { timeout: 240_000 }, async (t) => {
  const receipt = {
    card: "MC-034", role: "repair", environment: "local-preview", startedAt: new Date().toISOString(),
    sourceOverride: process.env.MC034_SOURCE_DIR ? SOURCE_ROOT : null, tailwindAdditionalSource,
    sourceBefore: await sourceHashes(), runnerSha256: sha256(await readFile(TEST_FILE)),
    fixture: "Real LoomMate, tRPC client, layout hooks, shared UI, LoomBall and production CSS; synthetic local HTTP responses; existing vector2d avatar path; no database, model or broker",
    browserChannel: process.env.MC034_BROWSER_CHANNEL ?? "chromium", observations: [], cleanupErrors: [],
  };
  let server;
  let browser;
  let failure = null;
  if (OUT) await mkdir(OUT, { recursive: true });
  try {
    const originalComponent = process.env.MC034_SOURCE_DIR
      ? await readFile(path.join(SOURCE_ROOT, COMPONENT), "utf8") : null;
    const originalSourceStyles = tailwindAdditionalSource === null ? null
      : (await readFile(path.join(ROOT, STYLES), "utf8")) + `\n@source ${JSON.stringify(tailwindAdditionalSource)};\n`;
    receipt.stylesWithAdditionalSourceSha256 = originalSourceStyles === null ? null : sha256(originalSourceStyles);
    server = await createServer({
      root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{
        name: "mc034-loommate-browser-fixture", enforce: "pre",
        resolveId(id) { if (id === "/__mc034_loommate_fixture__.tsx") return fixtureId; },
        load(id) {
          if (id === fixtureId) return fixtureSource;
          if (originalComponent !== null && id === path.join(ROOT, COMPONENT)) return originalComponent;
          if (originalSourceStyles !== null && id === path.join(ROOT, STYLES)) return originalSourceStyles;
        },
        configureServer(vite) {
          vite.middlewares.use(async (req, res, next) => {
            if (!req.url?.startsWith("/__mc034__?")) return next();
            try {
              const theme = new URL(req.url, "http://fixture.local").searchParams.get("theme") === "light" ? "light" : "dark";
              const html = await vite.transformIndexHtml("/__mc034__", `<!doctype html>
<html lang="zh-CN" data-wl-theme="${theme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title data-workloom-product-title>未读对比度回归</title></head>
<body data-workloom-client="b-pc"><div id="root"></div>
<script type="module" src="/__mc034_loommate_fixture__.tsx"></script></body></html>`);
              res.statusCode = 200;
              res.setHeader("Content-Type", "text/html; charset=utf-8");
              res.end(html);
            } catch (error) {
              res.statusCode = 500;
              res.end(String(error));
            }
          });
        },
      }],
    });
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address === "object", "Vite must bind a real dynamic local port");
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({
      ...(process.env.MC034_BROWSER_CHANNEL ? { channel: process.env.MC034_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"],
    });
    receipt.browserVersion = browser.version();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      for (const mode of ["mini", "expanded", "fullscreen"]) {
        for (const theme of ["dark", "light"]) {
          for (const count of theme === "dark" ? [0, 1, 12] : [12]) {
            const label = `${viewport.width}-${mode}-${theme}-${count}`;
            await t.test(label, async () => {
              const observation = {
                label, viewport, mode, theme, count,
                preconditions: "Fresh browser context, synthetic secretary settings/inbox, real production component/CSS",
                steps: ["Mount selected mode", "Read actual computed colors/geometry", "Run axe on actual numeric span",
                  ...(count === 12 && theme === "dark" ? ["Exercise pointer, sequential keyboard, heartbeat, read/acted updates and failure recovery"] : [])],
                expected: "Contrast ≥4.5 without rounding; zero hides; background/font/size preserved; no horizontal overflow; existing interaction behavior",
                measurements: [], interactions: [], pointerGeometry: [], failures: [], pageErrors: [], consoleErrors: [],
                responseErrors: [], blockedRequests: [], unexpectedProcedures: [],
              };
              receipt.observations.push(observation);
              const state = fixtureState(mode, count);
              const context = await browser.newContext({ viewport, locale: "zh-CN",
                serviceWorkers: "block", reducedMotion: "reduce" });
              try {
                await context.route("**/*", (route) => routeFixture(route, state, origin, observation));
                await context.addInitScript((mini) => {
                  localStorage.setItem("loommate.mini", mini ? "1" : "0");
                  localStorage.setItem("wl-voice-gate", "captions");
                }, mode === "mini");
                const page = await context.newPage();
                page.setDefaultTimeout(15_000);
                page.on("pageerror", (error) => observation.pageErrors.push(String(error)));
                page.on("console", (message) => {
                  if (message.type() === "error") observation.consoleErrors.push(message.text());
                });
                page.on("response", (response) => {
                  if (response.status() >= 400) observation.responseErrors.push({ status: response.status(), url: response.url() });
                });
                await page.clock.install({ time: new Date("2026-10-20T04:00:00Z") });
                await page.goto(`${origin}/__mc034__?render=vector2d&theme=${theme}`);
                await page.locator(SURFACE).waitFor();
                await expect.poll(() => state.calls.filter((call) => call.name === "service.secretary.inbox").length).toBeGreaterThan(0);
                if (mode === "fullscreen") await expect(page.getByRole("dialog", { name: "小织屏保" })).toBeVisible();
                else if (mode === "expanded") await expect(page.getByTitle("切换大小", { exact: true })).toBeVisible();
                else await expect(avatarControl(page)).toHaveJSProperty("tagName", "BUTTON");
                await page.evaluate(async () => { await document.fonts.ready; });
                checkMeasurement(observation, await measure(page, count), mode, theme);
                if (count > 0) {
                  const box = await unreadBadge(page, count).boundingBox();
                  assert.ok(box, "The visible numeric badge must have actual browser geometry");
                  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
                  checkMeasurement(observation, { ...await measure(page, count), state: "hover" }, mode, theme);
                  await page.mouse.move(1, 1);
                }
                if (OUT) await page.screenshot({ path: path.join(OUT, `${label}.png`), fullPage: false, animations: "disabled" });
                if (count === 12 && theme === "dark") {
                  await interact(page, state, mode, observation);
                  checkMeasurement(observation, await measure(page, 0), mode, theme);
                }
                assert.deepEqual(observation.pageErrors, []);
                assert.deepEqual(observation.consoleErrors, []);
                assert.deepEqual(observation.responseErrors, []);
                assert.deepEqual(observation.blockedRequests, []);
                assert.deepEqual(observation.unexpectedProcedures, []);
                assert.equal(observation.failures.length, 0, observation.failures.join("\n"));
              } catch (error) {
                observation.error = String(error);
                throw error;
              } finally {
                observation.calls = state.calls;
                await closeAndRecord(context, observation, receipt);
              }
            });
          }
        }
      }
    }
    await t.test("Source bytes remain stable throughout the browser matrix", async () => {
      assert.deepEqual(await sourceHashes(), receipt.sourceBefore);
    });
  } catch (error) {
    failure = String(error);
    throw error;
  } finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors.push(...cleanup.filter((result) => result.status === "rejected").map((result) => String(result.reason)));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.finishedAt = new Date().toISOString();
    receipt.error = failure;
    receipt.passed = !failure && receipt.observations.length === 24 && receipt.sourceStable
      && receipt.cleanupErrors.length === 0
      && receipt.observations.every((observation) => !observation.error && observation.failures.length === 0);
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, [], "Fixture server and browser must close cleanly");
  }
});
