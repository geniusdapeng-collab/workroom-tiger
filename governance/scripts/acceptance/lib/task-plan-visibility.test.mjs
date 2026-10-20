/**
 * MC-041 actual P2/Bridge/navigation/client and production CSS.
 * Synthetic HTTP only; no database, execution request, model or external call.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { taskPlanDom } from "./task-plan-evidence.mjs";

const TEST_FILE = fileURLToPath(import.meta.url);
const ROOT = path.resolve(process.env.MC041_SOURCE_DIR ?? path.join(path.dirname(TEST_FILE), "../../../.."));
const WEB = path.join(ROOT, "governance/apps/web");
const OUT = process.env.MC041_EVIDENCE_DIR ? path.resolve(process.env.MC041_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const { chromium, expect } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = ["src/pages/p2/P2.tsx", "src/shell/Bridge.tsx", "src/shell/SideNav.tsx",
  "src/shell/NavigationAccess.tsx", "src/components/star-ring/StarRing.tsx",
  "src/lib/trpc.ts", "src/styles/tokens.css", "src/styles/subtitle.css", "vite.config.ts", "package.json",
  "node_modules/@workloom/ui/dist/tokens.css", "node_modules/@workloom/ui/dist/content-safety.css",
  "node_modules/@workloom/ui/dist/components.css"];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [file, hash(await readFile(path.join(WEB, file)))])));
const PLAN = [
  { stepId: "s2", label: "核对公开工作记录，并整理待确认事项" },
  { stepId: "s1", label: "【确定性兜底：参数待补全】复核资料 '雪' <script>alert(1)</script>" },
];
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const exactPlanItems = async (region, expected, { timeout = 3000, onSample } = {}) => {
  await expect.poll(async () => {
    const actual = await region.getByRole("listitem").allTextContents();
    onSample?.(actual);
    return actual;
  }, { timeout }).toEqual(expected);
  assert.deepEqual(await region.getByRole("listitem").allTextContents(), expected);
};
const thread = (id = "mc041-task", additions = {}) => ({
  id, title: id === "mc041-task" ? "本周工作记录复盘" : "另一项独立任务",
  mode: "quest", status: "running", progress_done: 1, progress_total: 2,
  agent_id: null, created_by: "MEM-MC041", created_at: "2026-10-20T00:00:00Z",
  plan: PLAN, plan_state: "ready", plan_version: 3, ...additions,
});
const fixtureId = path.join(WEB, "__mc041_plan_fixture__.tsx");
const fixtureSource = `
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route, Link, useLocation } from "react-router";
import { OverlayManager } from "@workloom/ui";
import P2 from "./src/pages/p2/P2";
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
setToken("MC041-SYNTHETIC-SESSION");
function Shell() {
  const railW = useAskRailPadding();
  const { pathname } = useLocation();
  return <div data-workloom-client="b-pc" data-wl-theme="dark" className="flex min-h-screen min-w-0 max-w-full overflow-x-clip">
    <SideNav/><div className="wl-page-host min-w-0 flex-1" style={{paddingRight:railW}}>
      <StarRing/><NavigationAccessBoundary pathname={pathname}>
        <Routes><Route path="/tasks/:threadId" element={<P2/>}/></Routes>
      </NavigationAccessBoundary>
      <Link to="/tasks/mc041-other">切换到另一项独立任务</Link>
      <p>计划展示回归 · 明示模拟数据 · 不连接业务服务</p>
    </div></div>;
}
createRoot(document.getElementById("root")!).render(<React.StrictMode><OverlayManager><BrowserRouter>
  <NavigationAccessProvider><Shell/></NavigationAccessProvider>
</BrowserRouter></OverlayManager></React.StrictMode>);
`;

test("MC-041 real task breakdown at desktop/mobile widths", { timeout: 240_000 }, async (t) => {
  const before = await sourceHashes();
  const receipt = { card: "MC-041", actor: process.env.MC041_ACTOR ?? null, startedAt: new Date().toISOString(),
    fixture: "actual P2/Bridge/SideNav/StarRing/providers/client/CSS; synthetic HTTP data only",
    runnerSha256: hash(await readFile(TEST_FILE)), sourceBefore: before,
    browserChannel: process.env.MC041_BROWSER_CHANNEL ?? "chromium",
    scenes: [], requests: [], unexpected: [], pageErrors: [], cleanupErrors: [] };
  let server, browser, origin;
  if (OUT) await mkdir(OUT, { recursive: true });
  const scene = async (name, width, options, body) => t.test(name, async () => {
    const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: "block", reducedMotion: "reduce" });
    const row = { name, width, options };
    receipt.scenes.push(row);
    const state = { detail: options.missing ? null : thread("mc041-task", options.detail),
      gate: options.loading ? deferred() : null, fail: Boolean(options.fail), reads: 0, otherGate: null };
    let page;
    try {
      await context.route("**/*", async (route) => {
        const request = route.request(), url = new URL(request.url());
        if (url.origin !== origin) { receipt.unexpected.push({ name, url: url.origin }); return route.abort("blockedbyclient"); }
        if (!url.pathname.startsWith("/trpc/")) return route.continue();
        if (request.method() !== "GET") { receipt.unexpected.push({ name, method: request.method() }); return route.abort("blockedbyclient"); }
        const names = decodeURIComponent(url.pathname.slice(6)).split(",");
        const inputs = JSON.parse(url.searchParams.get("input") ?? "{}");
        const results = [];
        for (let index = 0; index < names.length; index += 1) {
          const operation = names[index], input = url.searchParams.get("batch") === "1" ? inputs[index] : inputs;
          receipt.requests.push({ name, operation, input });
          const access = { subject: { kind: "member", id: "mc041", name: "回归成员", role: "readonly" },
            scope: { tenantId: "mc041", workspaceId: "mc041" }, plan: "pro", capabilities: {},
            partnerCapabilities: [], availableScopes: [], authority: { source: "server", evaluatedAt: "2026-10-20", grantIds: [] },
            navigationPermissions: options.denied ? [] : ["tasks.read"], actionPermissions: [],
            bundle: { configured: false, reason: "synthetic" } };
          const values = {
            "access.me": access, "threads.list": [thread()], "threads.events": [], "approvals.list": [],
            "onboarding.status": { dataMode: "simulated", declaredDataMode: "simulated", defaultToolMode: "simulated",
              formalModeActive: false, llm: { provider: "mock", model: "synthetic", baseUrl: "", real: false },
              workspace: { name: "模拟工作区", events: 0, members: 1, agents: 0, memories: 0 }, bundle: { id: "mc041", isExample: true } },
            "skills.skillOps.status": { recentLoaded: [], pendingCount: 0 }, "fence.rules": [],
          };
          if (operation === "threads.get") {
            state.reads += 1;
            // Capture the old response before delaying it, like a real server.
            const value = structuredClone(input.threadId === "mc041-other" ? thread("mc041-other", { plan: null, plan_state: "pending" }) : state.detail);
            const failed = state.fail;
            if (state.gate) await state.gate.promise;
            if (input.threadId === "mc041-other" && state.otherGate) await state.otherGate.promise;
            results.push(failed ? { error: { message: "模拟读取失败", code: -32603, data: { code: "INTERNAL_SERVER_ERROR", httpStatus: 500, path: operation } } }
              : { result: { data: value } });
          } else if (Object.hasOwn(values, operation)) results.push({ result: { data: values[operation] } });
          else { receipt.unexpected.push({ name, operation }); return route.abort("blockedbyclient"); }
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(url.searchParams.get("batch") === "1" ? results : results[0]) });
      });
      page = await context.newPage();
      page.setDefaultTimeout(3000);
      page.on("pageerror", (error) => receipt.pageErrors.push({ name, error: String(error) }));
      await page.goto(`${origin}/tasks/mc041-task`);
      const region = page.getByRole("region", { name: "任务拆解", exact: true });
      await body({ page, region, state, row });
      if (OUT) await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true, animations: "disabled" });
      row.passed = true;
    } catch (error) {
      row.passed = false; row.error = String(error);
      if (OUT && page) await page.screenshot({ path: path.join(OUT, `${name}-failure.png`), fullPage: true, animations: "disabled" })
        .catch((error) => { row.screenshotError = String(error); });
      throw error;
    } finally {
      state.gate?.resolve(); state.otherGate?.resolve();
      try { await context.close(); } catch (error) { receipt.cleanupErrors.push(String(error)); }
    }
  });
  try {
    server = await createServer({ root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{ name: "mc041-real-p2-fixture", enforce: "pre",
        resolveId(id) { if (id === "/__mc041_plan_fixture__.tsx") return fixtureId; },
        load(id) { if (id === fixtureId) return fixtureSource; },
        configureServer(vite) { vite.middlewares.use(async (req, res, next) => {
          if (!req.url.startsWith("/tasks/")) return next();
          try {
            const html = await vite.transformIndexHtml(req.url, `<!doctype html><html lang="zh-CN" data-wl-theme="dark">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title data-workloom-product-title>计划展示回归</title></head>
<body><div id="root"></div><script type="module" src="/__mc041_plan_fixture__.tsx"></script></body></html>`);
            res.statusCode = 200; res.setHeader("Content-Type", "text/html; charset=utf-8"); res.end(html);
          } catch (error) { res.statusCode = 500; res.end(String(error)); }
        }); },
      }] });
    await server.listen();
    origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await chromium.launch({ ...(process.env.MC041_BROWSER_CHANNEL ? { channel: process.env.MC041_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"] });
    receipt.browserVersion = browser.version(); receipt.origin = origin;
    for (const width of [1440, 390]) {
      for (const long of [false, true]) await scene(`ordered-${width}${long ? "-long" : ""}`, width, {
        detail: { plan: long ? PLAN.map((step) => ({ ...step, label: step.label + "\n" + "公开资料核对".repeat(100) })) : PLAN },
      }, async ({ region, page, state, row }) => {
        await region.waitFor();
        await exactPlanItems(region, state.detail.plan.map((step) => step.label));
        assert.equal(await region.locator("script").count(), 0);
        assert.equal(await region.getByRole("button").count(), 0);
        assert.equal(await page.getByPlaceholder(/^说出一句话目标/).count(), 0, "readonly member has no task mutation composer");
        row.geometry = await region.evaluate((node) => {
          const box = node.getBoundingClientRect();
          const items = [...node.querySelectorAll("li")].map((item) => {
            const range = document.createRange(); range.selectNodeContents(item);
            return { clientWidth: item.clientWidth, scrollWidth: item.scrollWidth,
              rects: [...range.getClientRects()].map((rect) => ({ left: rect.left, right: rect.right, width: rect.width })) };
          });
          return { left: box.left, right: box.right, width: box.width, viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth, items };
        });
        assert.ok(row.geometry.scrollWidth <= width + 1);
        assert.ok(row.geometry.items.every((item) => item.scrollWidth <= item.clientWidth + 1
          && item.rects.every((rect) => rect.left >= row.geometry.left && rect.right <= row.geometry.right)));
      });
      await scene(`readiness-gate-${width}`, width, { loading: true }, async ({ region, state, page, row }) => {
        await region.getByRole("status", { name: "任务拆解正在加载" }).waitFor();
        await region.waitFor();
        row.beforeReady = await region.getByRole("listitem").allTextContents();
        assert.deepEqual(row.beforeReady, [], "visible section can still be a loading skeleton");
        const firstSample = deferred();
        const waiting = exactPlanItems(region, PLAN.map((step) => step.label), {
          onSample: (actual) => firstSample.resolve(actual),
        }).then(() => ({ ok: true }), (error) => ({ ok: false, error }));
        assert.deepEqual(await firstSample.promise, []);
        state.gate.resolve();
        const outcome = await waiting;
        if (!outcome.ok) throw outcome.error;
        row.afterReady = await region.getByRole("listitem").allTextContents();
        row.geometry = await region.evaluate((node) => {
          const box = node.getBoundingClientRect();
          return { left: box.left, right: box.right, scrollWidth: document.documentElement.scrollWidth };
        });
        assert.ok(row.geometry.left >= 0 && row.geometry.right <= width + 1);
        assert.ok(row.geometry.scrollWidth <= width + 1);
        assert.equal(await page.getByPlaceholder(/^说出一句话目标/).count(), 0);
      });
      for (const [name, detail, text] of [
        ["pending", { plan: null, plan_state: "pending" }, "正在等待任务拆解"],
        ["empty", { plan: [], plan_state: "empty" }, "当前计划没有步骤"],
        ["invalid", { plan: null, plan_state: "invalid" }, "任务拆解暂时无法显示"],
        ["terminal", { status: "completed", plan: null, plan_state: "pending" }, "此任务没有保存可展示的拆解"],
        ["failed", { status: "failed", plan: null, plan_state: "pending" }, "此任务没有保存可展示的拆解"],
        ["cancelled", { status: "cancelled", plan: null, plan_state: "pending" }, "此任务没有保存可展示的拆解"],
        ["paused", { status: "paused", plan: null, plan_state: "pending" }, "任务已暂停，尚无可展示的拆解"],
        ["ask-running", { mode: "ask", status: "running", plan: null, plan_state: "pending" }, "此任务为问询，不生成步骤拆解"],
        ["ask-completed", { mode: "ask", status: "completed", plan: null, plan_state: "pending" }, "此任务为问询，不生成步骤拆解"],
      ]) await scene(`${name}-${width}`, width, { detail }, async ({ region }) => {
        await region.getByText(text, { exact: true }).waitFor();
        assert.equal(await region.getByRole("listitem").count(), 0);
      });
      await scene(`loading-${width}`, width, { loading: true }, async ({ region, state }) => {
        await region.getByRole("status", { name: "任务拆解正在加载" }).waitFor();
        assert.equal(await region.getByRole("listitem").count(), 0);
        state.gate.resolve();
        await region.getByText(PLAN[0].label, { exact: true }).waitFor();
      });
      await scene(`initial-error-${width}`, width, { fail: true }, async ({ region, page }) => {
        await region.getByText("暂时无法获取任务拆解，正在重连", { exact: true }).waitFor();
        assert.equal(await region.getByRole("listitem").count(), 0);
        assert.equal(await page.getByText("这个任务不存在，或已被清理", { exact: true }).count(), 0);
      });
      await scene(`denied-${width}`, width, { denied: true }, async ({ page, state }) => {
        await page.getByText("当前身份不能访问此页面", { exact: true }).waitFor();
        assert.equal(state.reads, 0);
      });
      await scene(`missing-${width}`, width, { missing: true }, async ({ page, region }) => {
        await page.getByText("这个任务不存在，或已被清理", { exact: true }).waitFor();
        assert.equal(await region.getByRole("listitem").count(), 0);
      });
    }
    for (const [name, options] of [
      ["never-ready", { loading: true }],
      ["wrong-order", { detail: { plan: [...PLAN].reverse() } }],
      ["missing-row", { detail: { plan: [PLAN[0]] } }],
    ]) await scene(`readiness-rejects-${name}`, 1440, options, async ({ region, row }) => {
      await region.waitFor();
      if (!options.loading) await region.getByRole("listitem").first().waitFor();
      row.observedLabels = await region.getByRole("listitem").allTextContents();
      assert.deepEqual(row.observedLabels, options.loading ? [] : options.detail.plan.map((step) => step.label));
      await assert.rejects(
        exactPlanItems(region, PLAN.map((step) => step.label), { timeout: 200 }),
      );
      row.strictOracleRejected = true;
    });
    await scene("delayed-poll", 1440, { detail: { plan: null, plan_state: "pending" } }, async ({ region, state, page }) => {
      await region.getByText("正在等待任务拆解", { exact: true }).waitFor();
      state.detail = thread();
      await region.getByText(PLAN[0].label, { exact: true }).waitFor({ timeout: 8000 });
      assert.ok(state.reads >= 2);
      assert.deepEqual(await region.getByRole("listitem").allTextContents(), PLAN.map((step) => step.label));
      state.fail = true;
      await region.getByText("连接中断，以下为上次成功获取的任务拆解", { exact: true }).waitFor({ timeout: 8000 });
      assert.deepEqual(await region.getByRole("listitem").allTextContents(), PLAN.map((step) => step.label));
      assert.ok((await page.getByText(/连接中断，正在重连/).count()) > 0);
    });
    await scene("route-change-late-response", 1440, { loading: true }, async ({ region, state, page }) => {
      await region.getByRole("status", { name: "任务拆解正在加载" }).waitFor();
      state.otherGate = deferred();
      await page.getByRole("link", { name: "切换到另一项独立任务", exact: true }).click();
      state.gate.resolve();
      await page.waitForTimeout(150);
      assert.equal(await region.getByText(PLAN[0].label, { exact: true }).count(), 0, "old task response must never populate the new route");
      state.otherGate.resolve();
      await region.getByText("正在等待任务拆解", { exact: true }).waitFor();
      assert.equal(await region.getByRole("listitem").count(), 0);
    });
    await scene("real-dom-hidden-evidence", 1440, {}, async ({ region, page, row }) => {
      await region.getByText(PLAN[0].label, { exact: true }).waitFor();
      assert.deepEqual((await taskPlanDom(page)).labels, PLAN.map((step) => step.label));
      const section = page.locator('section[aria-label="任务拆解"]');
      await section.evaluate((node) => { node.hidden = true; });
      assert.equal((await taskPlanDom(page)).visible, false);
      assert.deepEqual((await taskPlanDom(page)).labels, []);
      row.hiddenRegion = await taskPlanDom(page);
      await section.evaluate((node) => { node.hidden = false; });
      await section.locator("li").first().evaluate((node) => { node.style.opacity = "0"; });
      // A style change may first enter the production transition. Assert the
      // actual computed state before testing the DOM evidence collector.
      await page.waitForFunction(() => getComputedStyle(document.querySelector('section[aria-label="任务拆解"] li')).opacity === "0");
      row.transparentItem = await section.locator("li").first().evaluate((node) => ({
        inline: node.style.opacity, computed: getComputedStyle(node).opacity,
      }));
      assert.deepEqual((await taskPlanDom(page)).labels, [PLAN[1].label]);
      await section.locator("li").first().evaluate((node) => { node.style.opacity = ""; });
    });
    await t.test("no hidden mutations, external calls, errors or source drift", async () => {
      assert.deepEqual(receipt.unexpected, []);
      assert.deepEqual(receipt.pageErrors, []);
      assert.deepEqual(await sourceHashes(), before);
      assert.ok(receipt.scenes.every((row) => row.passed));
    });
  } finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors.push(...cleanup.filter((row) => row.status === "rejected").map((row) => String(row.reason)));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.finishedAt = new Date().toISOString();
    receipt.passed = receipt.scenes.every((row) => row.passed) && !receipt.unexpected.length && !receipt.pageErrors.length
      && receipt.sourceStable && !receipt.cleanupErrors.length;
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, []);
  }
});
