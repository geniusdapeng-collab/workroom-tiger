/**
 * MC-038: actual P6/Bridge/SideNav/StarRing, production CSS and real HTTP client.
 * All API data and mutation receipts are explicitly synthetic. No live service,
 * database, external network or model. No geometry mocks, force clicks or CSS
 * overrides. The real shell reserves its normal navigation and assistant space.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const TEST_FILE = fileURLToPath(import.meta.url);
const ROOT = path.resolve(process.env.MC038_SOURCE_DIR ?? path.join(path.dirname(TEST_FILE), "../../../.."));
const WEB = path.join(ROOT, "governance/apps/web");
const OUT = process.env.MC038_EVIDENCE_DIR ? path.resolve(process.env.MC038_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const { chromium } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  "governance/apps/web/src/pages/p6/P6.tsx",
  "governance/apps/web/src/App.tsx",
  "governance/apps/web/src/main.tsx",
  "governance/apps/web/src/shell/Bridge.tsx",
  "governance/apps/web/src/shell/SideNav.tsx",
  "governance/apps/web/src/shell/NavigationAccess.tsx",
  "governance/apps/web/src/shell/NavMenu.tsx",
  "governance/apps/web/src/shell/PlanSwitcher.tsx",
  "governance/apps/web/src/components/star-ring/StarRing.tsx",
  "governance/apps/web/src/components/hud/feedback.tsx",
  "governance/apps/web/src/components/hud/NightStatusPill.tsx",
  "governance/apps/web/src/components/hud/EmergencyBrake.tsx",
  "governance/apps/web/src/components/SimBanner.tsx",
  "governance/apps/web/src/components/SkillDistBanner.tsx",
  "governance/apps/web/src/lib/useAskRail.ts",
  "governance/apps/web/src/lib/display.ts",
  "governance/apps/web/src/lib/trpc.ts",
  "governance/apps/web/src/lib/product.ts",
  "governance/apps/web/src/styles/tokens.css",
  "governance/apps/web/src/styles/subtitle.css",
  "governance/apps/web/vite.config.ts",
  "governance/apps/web/package.json",
  "governance/apps/web/node_modules/@workloom/ui/dist/tokens.css",
  "governance/apps/web/node_modules/@workloom/ui/dist/content-safety.css",
  "governance/apps/web/node_modules/@workloom/ui/dist/components.css",
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [
  file, sha256(await readFile(path.join(ROOT, file))),
])));
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const DEFAULT_SUGGESTIONS = Array.from({ length: 7 }, (_, index) => ({
  key: `member::mc038-synthetic-${index}`, objectType: "member",
  actionCategory: index === 0 ? "成员资料核对" : `工作记录核对第${index}项`,
  count: 17 - index, windowDays: 7, threshold: index === 6 ? 6 : 3,
  sampleEventIds: [`MC038-SYNTHETIC-EVENT-${index}`],
}));
const LONG_SUGGESTIONS = DEFAULT_SUGGESTIONS.map((suggestion) => ({
  ...suggestion, actionCategory: "跨市场经营资料持续整理并核对异常原因与后续处理记录".repeat(3),
  objectType: "需要人工审阅并按原有安全规则处理的工作记录".repeat(2),
  count: 999999, windowDays: 365, threshold: 6,
}));
const SKILL = {
  id: "mc038-synthetic-skill", level: "official", bundle: "mc038",
  name: "资料核对", version: "1.0.0", description: "资料核对：布局回归中的明示模拟技能。",
  fence_bindings: [], desensitized: true,
};
const fixtureId = path.join(WEB, "__mc038_skills_fixture__.tsx");
const fixtureSource = `
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route, useLocation } from "react-router";
import { OverlayManager } from "@workloom/ui";
import P6 from "./src/pages/p6/P6";
import { SideNav } from "./src/shell/SideNav";
import { StarRing } from "./src/components/star-ring/StarRing";
import { NavigationAccessProvider, NavigationAccessBoundary } from "./src/shell/NavigationAccess";
import { useAskRailPadding } from "./src/lib/useAskRail";
import { setToken } from "./src/lib/trpc";
import "./src/styles/tokens.css";
import "./src/styles/subtitle.css";
import "@workloom/ui/tokens.css";
import "@workloom/ui/content-safety.css";
import "@workloom/ui/components.css";
setToken("MC038-SYNTHETIC-LAYOUT-SESSION");
function FixtureShell() {
  const railW = useAskRailPadding();
  const { pathname } = useLocation();
  return <div data-workloom-client="b-pc" data-wl-theme="dark"
    className="flex min-h-screen min-w-0 max-w-full overflow-x-clip">
    <SideNav />
    <div className="wl-page-host min-w-0 flex-1" style={{paddingRight:railW}}>
      <StarRing />
      <NavigationAccessBoundary pathname={pathname}>
        <Routes>
          <Route path="/skills" element={<P6/>}/>
          <Route path="/skills/create" element={<P6/>}/>
        </Routes>
      </NavigationAccessBoundary>
      <p className="p-3 text-body">布局回归 · 明示模拟数据与回执 · 不连接业务服务或数据库</p>
    </div>
  </div>;
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode><OverlayManager><BrowserRouter>
    <NavigationAccessProvider><FixtureShell/></NavigationAccessProvider>
  </BrowserRouter></OverlayManager></React.StrictMode>
);
`;
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const contains = (outer, inner) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1
  && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
const overlaps = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
  && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;

function cardLocator(page) {
  return page.locator(".wl-bridge-grid main .rounded-lg").filter({
    has: page.getByText("AI 副官建议", { exact: true }),
  }).first();
}
function rowLocator(page, index) { return cardLocator(page).locator(":scope > div").nth(index); }
function actionLocator(page, index, action) {
  const labels = index === 0
    ? { confirm: "一键固化为定时任务", draft: "生成装备草稿", reject: "驳回并减少同类建议" }
    : { confirm: "固化", draft: "草稿", reject: "驳回" };
  return rowLocator(page, index).getByRole("button", { name: labels[action], exact: true });
}
async function measure(page) {
  return page.evaluate(() => {
    const rect = (node) => {
      const { left, right, top, bottom, width, height } = node.getBoundingClientRect();
      return { left, right, top, bottom, width, height };
    };
    const text = (element) => {
      const boxes = [];
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (!node.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        boxes.push(...[...range.getClientRects()].map((box) => ({
          left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height,
        })).filter((box) => box.width > 0 && box.height > 0));
      }
      const style = getComputedStyle(element);
      return { value: element.textContent.trim(), rect: rect(element), boxes,
        fontSize: parseFloat(style.fontSize), lines: new Set(boxes.map((box) => Math.round(box.top))).size,
        visible: style.display !== "none" && style.visibility === "visible" && Number(style.opacity) > 0 };
    };
    const main = document.querySelector(".wl-bridge-grid main");
    const title = [...(main?.querySelectorAll("b") ?? [])].find((node) => node.textContent === "AI 副官建议");
    let card = title;
    while (card && !card.classList.contains("border-holo/35")) card = card.parentElement;
    const rows = card ? [...card.children].map((row, index) => {
      const copy = index === 0 ? [...row.querySelectorAll("div")].find((node) =>
        [...node.childNodes].some((child) => child.nodeType === Node.TEXT_NODE && child.textContent.includes("检测到高频任务：")),
      ) : row.querySelector("span");
      if (!copy) throw new Error(`Real P6 copy not found for row ${index}`);
      const buttons = [...row.querySelectorAll("button")].map((button) => ({
        ...text(button), disabled: button.disabled, clientWidth: button.clientWidth, scrollWidth: button.scrollWidth,
      }));
      return { index, rect: rect(row), copy: text(copy), buttons, clientWidth: row.clientWidth, scrollWidth: row.scrollWidth };
    }) : [];
    const pending = card ? [...card.querySelectorAll("span")].find((node) => /^待确认 \d+ 条$/.test(node.textContent)) : null;
    const host = document.querySelector(".wl-page-host");
    const grid = document.querySelector(".wl-bridge-grid");
    return { card: card ? rect(card) : null, rows, title: title ? text(title) : null,
      pending: pending ? text(pending) : null,
      document: { width: innerWidth, scrollWidth: document.documentElement.scrollWidth },
      main: main ? { ...rect(main), clientWidth: main.clientWidth, scrollWidth: main.scrollWidth } : null,
      shell: { host: rect(host), paddingRight: parseFloat(getComputedStyle(host).paddingRight),
        bridge: grid ? { ...rect(grid), columns: getComputedStyle(grid).gridTemplateColumns,
          leftVisible: !grid.querySelector(".wl-bridge-left").hidden,
          rightVisible: !grid.querySelector(".wl-bridge-right").hidden } : null,
        navWidth: document.querySelector(".wl-side-navigation")?.getBoundingClientRect().width ?? 0 } };
  });
}
function checkLayout(observation, expected, canManage = true) {
  const m = observation.measurement;
  const check = (condition, message) => { if (!condition) observation.failures.push(message); };
  check(m.document.scrollWidth <= m.document.width + 1, "document has horizontal overflow");
  check(m.main && m.main.scrollWidth <= m.main.clientWidth + 1, "main content is clipped horizontally");
  check(m.rows.length === expected.length, "suggestion count changed");
  if (expected.length === 0) { check(m.card === null, "empty suggestions still render a card"); return; }
  check(m.title.visible && m.title.lines === 1, "suggestion title is compressed or hidden");
  check(m.title.boxes.every((box) => contains(m.card, box)), "suggestion title glyph is clipped");
  if (expected.length > 1) check(m.pending?.visible && m.pending.lines === 1
    && m.pending.value === `待确认 ${expected.length} 条`
    && m.pending.boxes.every((box) => contains(m.card, box)), "pending count is compressed, hidden or changed");
  else check(m.pending === null, "single suggestion unexpectedly renders a pending count");
  for (const row of m.rows) {
    const wanted = expected[row.index];
    const prefix = `row ${row.index}: `;
    check(contains(m.card, row.rect), prefix + "row escapes card");
    check(row.copy.visible && row.copy.boxes.length > 0, prefix + "copy is hidden");
    check(row.scrollWidth <= row.clientWidth + 1, prefix + "row has horizontal overflow");
    check(row.copy.rect.width >= Math.min(160, m.card.width - 64) - 1, prefix + "copy is compressed into a narrow column");
    check(Math.max(...row.copy.boxes.map((box) => box.width)) >= Math.min(row.copy.fontSize * 8, m.card.width - 64),
      prefix + "copy has no readable horizontal text line");
    check(row.copy.value.includes(wanted.actionCategory) && row.copy.value.includes(String(wanted.count))
      && row.copy.value.includes(String(wanted.windowDays)), prefix + "suggestion content changed");
    check(row.buttons.length === (canManage ? 3 : 0), prefix + "action permission/count changed");
    for (const box of row.copy.boxes) check(contains(m.card, box), prefix + "copy glyph is clipped");
    for (const button of row.buttons) {
      check(contains(m.card, button.rect), prefix + "button escapes card");
      check(button.scrollWidth <= button.clientWidth + 1, prefix + "button overflows");
      check(button.visible && button.boxes.every((box) => contains(button.rect, box)), prefix + "button text is clipped");
      check(row.copy.boxes.every((box) => !overlaps(button.rect, box)), prefix + "button overlaps copy");
    }
    for (let i = 0; i < row.buttons.length; i += 1) {
      for (let j = i + 1; j < row.buttons.length; j += 1) {
        check(!overlaps(row.buttons[i].rect, row.buttons[j].rect), prefix + "action buttons overlap");
      }
    }
  }
}

test("MC-038 suggestions remain readable and operable in the actual responsive shell", { timeout: 360_000 }, async (t) => {
  const before = await sourceHashes();
  const receipt = { card: "MC-038", environment: "local-preview", startedAt: new Date().toISOString(),
    runnerSha256: sha256(await readFile(TEST_FILE)), sourceBefore: before,
    fixture: "actual P6/Bridge/SideNav/StarRing/providers/client and production CSS; synthetic HTTP data/receipts; no LoomMate/backend/DB/model",
    browserChannel: process.env.MC038_BROWSER_CHANNEL ?? "chromium",
    observations: [], queries: [], mutations: [], blockedRequests: [], pageErrors: [], consoleErrors: [], cleanupErrors: [] };
  let server;
  let browser;
  let origin;
  let integrityPassed = false;
  if (OUT) await mkdir(OUT, { recursive: true });
  const scene = async (label, viewport, options, body) => t.test(label, async () => {
    const context = await browser.newContext({ viewport, serviceWorkers: "block", reducedMotion: "reduce" });
    const observation = { label, viewport, options, failures: [], interactions: [] };
    receipt.observations.push(observation);
    const state = { suggestions: structuredClone(options.long ? LONG_SUGGESTIONS : DEFAULT_SUGGESTIONS).slice(0, options.count ?? 7),
      canManage: options.canManage !== false, mutations: [], queryCount: 0,
      loadGate: options.loading ? deferred() : null, confirmGate: null, failConfirm: false };
    let phase = "load";
    let page;
    try {
      await context.route("**/*", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.origin !== origin || /^\/(?:api|health)(?:\/|$)/.test(url.pathname)) {
          receipt.blockedRequests.push({ label, url: url.href });
          return route.abort("blockedbyclient");
        }
        if (!url.pathname.startsWith("/trpc/")) return route.continue();
        const operations = decodeURIComponent(url.pathname.slice(6)).split(",");
        let inputs;
        try { inputs = JSON.parse(request.method() === "GET" ? url.searchParams.get("input") ?? "{}" : request.postData() ?? "{}"); }
        catch (error) { receipt.blockedRequests.push({ label, error: String(error) }); return route.abort("blockedbyclient"); }
        const results = [];
        for (let index = 0; index < operations.length; index += 1) {
          const operation = operations[index];
          const input = url.searchParams.get("batch") === "1" ? inputs[index] : inputs;
          const access = {
            subject: { kind: "member", id: "mc038", name: "布局回归模拟成员", role: state.canManage ? "manager" : "readonly" },
            scope: { tenantId: "mc038", workspaceId: "mc038" }, plan: "pro", capabilities: {},
            partnerCapabilities: [], availableScopes: [], authority: { source: "server", evaluatedAt: "2026-10-20T00:00:00Z", grantIds: [] },
            navigationPermissions: options.denied ? [] : ["skills.read"], actionPermissions: state.canManage ? ["skill.manage"] : [],
            bundle: { configured: false, reason: "explicit-synthetic-layout-fixture" },
          };
          const queries = {
            "access.me": access, "skills.list": [SKILL], "skills.installs": [], "skills.usage": {},
            "skills.awareness.suggestions": state.suggestions,
            "onboarding.status": { dataMode: "simulated", declaredDataMode: "simulated", defaultToolMode: "simulated",
              formalModeActive: false, llm: { provider: "mock", model: "mc038-synthetic", baseUrl: "", real: false },
              workspace: { name: "布局回归模拟工作区", events: 0, members: 1, agents: 0, memories: 0 },
              bundle: { id: "mc038-fixture", isExample: true } },
            "skills.skillOps.status": { recentLoaded: [], pendingCount: 0 }, "fence.rules": [],
          };
          if (request.method() === "GET" && Object.hasOwn(queries, operation)) {
            receipt.queries.push({ label, phase, operation, source: "synthetic-http-fixture" });
            if (operation.startsWith("skills.") && operation !== "skills.skillOps.status") {
              state.queryCount += 1;
              if (state.loadGate) await state.loadGate.promise;
            }
            results.push({ result: { data: queries[operation] } });
          } else if (request.method() === "POST" && state.canManage
            && ["skills.awareness.confirm", "skills.awareness.reject"].includes(operation)) {
            const mutation = { label, phase, operation, input, source: "synthetic-http-fixture" };
            state.mutations.push(mutation);
            receipt.mutations.push(mutation);
            if (operation.endsWith(".confirm")) {
              if (state.confirmGate) await state.confirmGate.promise;
              if (state.failConfirm) {
                results.push({ error: { message: "明示模拟确认失败", code: -32003,
                  data: { code: "FORBIDDEN", httpStatus: 403, path: operation } } });
              } else results.push({ result: { data: { artifactId: "MC038-SYNTHETIC-ARTIFACT-1", eventId: "MC038-SYNTHETIC-EVENT-1" } } });
            } else results.push({ result: { data: { eventId: "MC038-SYNTHETIC-REJECTION-1" } } });
          } else {
            receipt.blockedRequests.push({ label, operation, method: request.method(), input });
            return route.abort("blockedbyclient");
          }
        }
        return route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify(url.searchParams.get("batch") === "1" ? results : results[0]) });
      });
      page = await context.newPage();
      page.setDefaultTimeout(12_000);
      page.on("pageerror", (error) => receipt.pageErrors.push({ label, phase, error: String(error) }));
      page.on("console", (message) => {
        if (message.type() === "error") receipt.consoleErrors.push({ label, phase, error: message.text() });
      });
      const ready = async () => {
        await page.getByText("官方技能 · 随行业包分发", { exact: true }).waitFor();
        await page.evaluate(async () => { await document.fonts.ready; });
        await page.waitForFunction((width) => {
          const host = document.querySelector(".wl-page-host");
          const grid = document.querySelector(".wl-bridge-grid");
          if (!host || !grid) return false;
          const gridWidth = grid.getBoundingClientRect().width;
          return parseFloat(getComputedStyle(host).paddingRight) === (width > 820 ? 320 : 56)
            && !grid.querySelector(".wl-bridge-left").hidden === (gridWidth > 650)
            && !grid.querySelector(".wl-bridge-right").hidden === (gridWidth > 880);
        }, viewport.width);
      };
      await page.goto(`${origin}/skills`);
      if (!options.loading && !options.denied) await ready();
      await body({ page, state, observation, ready, setPhase: (value) => { phase = value; } });
      if (OUT) await page.screenshot({ path: path.join(OUT, `${label}.png`), fullPage: true, animations: "disabled" });
      assert.deepEqual(observation.failures, [], observation.failures.join("\n"));
    } catch (error) {
      observation.error = String(error);
      if (OUT && page && !page.isClosed()) {
        await page.screenshot({ path: path.join(OUT, `${label}-failure.png`), fullPage: true, animations: "disabled" })
          .catch((screenshotError) => { observation.screenshotError = String(screenshotError); });
      }
      throw error;
    } finally {
      state.loadGate?.resolve();
      state.confirmGate?.resolve();
      try { await context.close(); } catch (error) { receipt.cleanupErrors.push({ label, error: String(error) }); }
      if (OUT) await writeFile(path.join(OUT, `${label}.json`), JSON.stringify(observation, null, 2) + "\n");
    }
  });
  try {
    server = await createServer({
      root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{
        name: "mc038-real-skills-fixture", enforce: "pre",
        resolveId(id) { if (id === "/__mc038_skills_fixture__.tsx") return fixtureId; },
        load(id) { if (id === fixtureId) return fixtureSource; },
        configureServer(vite) {
          vite.middlewares.use(async (req, res, next) => {
            if (!["/skills", "/skills/create"].includes(req.url)) return next();
            try {
              const html = await vite.transformIndexHtml(req.url, `<!doctype html><html lang="zh-CN" data-wl-theme="dark">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title data-workloom-product-title>技能建议布局回归</title></head>
<body><div id="root"></div><script type="module" src="/__mc038_skills_fixture__.tsx"></script></body></html>`);
              res.statusCode = 200;
              res.setHeader("Content-Type", "text/html; charset=utf-8");
              res.end(html);
            } catch (error) { res.statusCode = 500; res.end(String(error)); }
          });
        },
      }],
    });
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address === "object", "real local fixture listener required");
    origin = `http://127.0.0.1:${address.port}`;
    receipt.origin = origin;
    browser = await chromium.launch({ ...(process.env.MC038_BROWSER_CHANNEL ? { channel: process.env.MC038_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"] });
    receipt.browserVersion = browser.version();
    for (const viewport of VIEWPORTS) {
      for (const options of [{ count: 0 }, { count: 1 }, { count: 7 }, { count: 7, long: true }, { count: 7, canManage: false }]) {
        const label = `layout-${viewport.width}-${options.count}${options.long ? "-long" : ""}${options.canManage === false ? "-readonly" : ""}`;
        await scene(label, viewport, options, async ({ page, state, observation }) => {
          observation.measurement = await measure(page);
          checkLayout(observation, state.suggestions, state.canManage);
          assert.equal(observation.measurement.shell.paddingRight, viewport.width > 820 ? 320 : 56,
            "real assistant must reserve the normal product width");
          assert.equal(state.mutations.length, 0, "rendering suggestions must never confirm or reject them");
        });
      }
      await scene(`loading-${viewport.width}`, viewport, { count: 7, loading: true }, async ({ page, state, observation, ready }) => {
        await page.getByRole("status", { name: "技能列表正在加载" }).waitFor();
        assert.equal(await page.getByText("AI 副官建议", { exact: true }).count(), 0);
        assert.equal(await page.getByRole("button", { name: "一键固化为定时任务", exact: true }).count(), 0);
        state.loadGate.resolve();
        await ready();
        observation.measurement = await measure(page);
        checkLayout(observation, state.suggestions);
      });
      await scene(`denied-${viewport.width}`, viewport, { denied: true }, async ({ page, state, observation }) => {
        await page.getByText("当前身份不能访问此页面", { exact: true }).waitFor();
        assert.equal(await page.getByText("AI 副官建议", { exact: true }).count(), 0);
        assert.equal(state.queryCount, 0, "forbidden route must not load skill data");
        assert.equal(state.mutations.length, 0);
        observation.permissionBoundary = "actual NavigationAccessBoundary denied the route";
      });
      for (const rowIndex of [0, 1]) {
        await scene(`actions-${viewport.width}-row${rowIndex}`, viewport, { count: 7 }, async ({ page, state, observation, ready, setPhase }) => {
          for (const inputMode of ["pointer", "Enter", "Space"]) {
            for (const action of ["confirm", "draft", "reject"]) {
              setPhase(`${inputMode}-${action}`);
              await page.goto(`${origin}/skills`);
              await ready();
              const actionButton = actionLocator(page, rowIndex, action);
              const initial = state.mutations.length;
              if (inputMode === "pointer") await actionButton.click();
              else {
                let reached = false;
                for (let step = 0; step < 100; step += 1) {
                  await page.keyboard.press("Tab");
                  reached = await actionButton.evaluate((node) => node === document.activeElement);
                  if (reached) break;
                }
                assert.ok(reached, "sequential Tab must reach the actual suggestion action");
                await page.keyboard.press(inputMode);
              }
              if (action === "draft") {
                await page.getByText("已带入系统建议内容", { exact: true }).waitFor();
                assert.equal(new URL(page.url()).pathname, "/skills/create");
                assert.equal(await page.getByPlaceholder("如：周一经营复盘").inputValue(), state.suggestions[rowIndex].actionCategory);
                assert.ok((await page.getByPlaceholder("每周一 08:00，或核心指标连续 3 天下滑时").inputValue())
                  .includes(`高频样本 ${state.suggestions[rowIndex].count} 次`));
                assert.equal(state.mutations.length, initial, "draft navigation must not mutate");
              } else {
                await page.getByText(action === "confirm" ? /已固化为每周一 08:00/ : /已驳回「/).first().waitFor();
                assert.equal(state.mutations.length, initial + 1, "one activation must issue exactly one request");
                const mutation = state.mutations.at(-1);
                assert.equal(mutation.operation, `skills.awareness.${action}`);
                assert.deepEqual(mutation.input, action === "confirm"
                  ? { suggestion: state.suggestions[rowIndex], target: "trigger", schedule: "0 8 * * 1" }
                  : { key: state.suggestions[rowIndex].key });
              }
              observation.interactions.push({ rowIndex, inputMode, action, passed: true });
            }
          }
        });
        await scene(`confirm-error-${viewport.width}-row${rowIndex}`, viewport, { count: 7 }, async ({ page, state, observation, setPhase }) => {
          state.confirmGate = deferred();
          state.failConfirm = true;
          setPhase("synthetic-confirm-failure");
          await actionLocator(page, rowIndex, "confirm").click();
          await page.waitForFunction(() => [...document.querySelectorAll(".wl-bridge-grid main button")].some((button) => button.disabled));
          assert.ok(await actionLocator(page, rowIndex, "confirm").isDisabled());
          assert.ok(await actionLocator(page, rowIndex, "reject").isDisabled());
          assert.ok(await actionLocator(page, rowIndex, "draft").isEnabled(), "preserve the existing draft availability");
          state.confirmGate.resolve();
          await page.getByRole("alert").filter({ hasText: "暂时无法固化定时任务" }).waitFor();
          assert.ok(await actionLocator(page, rowIndex, "confirm").isEnabled());
          observation.measurement = await measure(page);
          checkLayout(observation, state.suggestions);
          state.confirmGate = null;
          state.failConfirm = false;
          setPhase("retry-confirm");
          await actionLocator(page, rowIndex, "confirm").click();
          await page.getByText(/已固化为每周一 08:00/).first().waitFor();
          assert.equal(state.mutations.length, 2, "failed request and explicit retry must each have one request");
          observation.interactions.push({ disabledDuringRequest: true, errorVisible: true, explicitRetry: true });
        });
      }
    }
    await scene("shell-controls-1440", VIEWPORTS[0], { count: 7 }, async ({ page, state, observation }) => {
      observation.shellStates = [];
      const inspect = async (name, paddingRight, leftVisible, rightVisible) => {
        await page.waitForFunction(({ padding, left, right }) => {
          const host = document.querySelector(".wl-page-host");
          const grid = document.querySelector(".wl-bridge-grid");
          return host && grid && parseFloat(getComputedStyle(host).paddingRight) === padding
            && !grid.querySelector(".wl-bridge-left").hidden === left
            && !grid.querySelector(".wl-bridge-right").hidden === right;
        }, { padding: paddingRight, left: leftVisible, right: rightVisible });
        observation.measurement = await measure(page);
        checkLayout(observation, state.suggestions);
        observation.shellStates.push({ name, measurement: observation.measurement });
        if (OUT) await page.screenshot({ path: path.join(OUT, `shell-controls-1440-${name}.png`), fullPage: true, animations: "disabled" });
      };
      await page.getByRole("button", { name: "收起 AI 助手", exact: true }).click();
      await inspect("assistant-compact-both-panels", 56, true, true);
      await page.getByRole("button", { name: "隐藏任务会话面板", exact: true }).click();
      await inspect("conversation-hidden", 56, false, true);
      await page.getByRole("button", { name: "隐藏任务上下文面板", exact: true }).click();
      await inspect("both-panels-hidden", 56, false, false);
      await page.getByRole("button", { name: "显示任务会话面板", exact: true }).click();
      await page.getByRole("button", { name: "显示任务上下文面板", exact: true }).click();
      await page.getByRole("button", { name: "展开 AI 助手", exact: true }).first().click();
      await inspect("default-restored", 320, true, false);
      assert.equal(state.mutations.length, 0, "layout controls must not confirm or reject a suggestion");
    });
    await t.test("no unexpected network/errors, source changes or unfinished scenes", async () => {
      assert.deepEqual(receipt.blockedRequests, []);
      assert.deepEqual(receipt.pageErrors, []);
      assert.deepEqual(receipt.consoleErrors, []);
      assert.deepEqual(await sourceHashes(), before);
      assert.equal(receipt.observations.length, 23);
      assert.ok(receipt.observations.every((item) => !item.error && item.failures.length === 0));
      integrityPassed = true;
    });
  } finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors.push(...cleanup.filter((result) => result.status === "rejected").map((result) => String(result.reason)));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.finishedAt = new Date().toISOString();
    receipt.passed = integrityPassed && receipt.sourceStable && receipt.cleanupErrors.length === 0;
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, [], "owned fixture server and browser must close");
  }
});
