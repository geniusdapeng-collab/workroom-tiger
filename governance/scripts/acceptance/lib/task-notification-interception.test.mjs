/**
 * MC-039: the real /tasks route remains actionable with unread reminders.
 * Real main/App, P1, LoomMate and production CSS; explicitly synthetic HTTP.
 * No database, backend, model, broker or real task is contacted.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const TEST_FILE = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(TEST_FILE), "../../../..");
const WEB = path.join(ROOT, "governance/apps/web");
const OUT = process.env.MC039_EVIDENCE_DIR ? path.resolve(process.env.MC039_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const { chromium, expect } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const { BASE_NAVIGATION } = await import(pathToFileURL(path.join(WEB, "node_modules/@workloom/ui/dist/navigation.js")));
const manifest = JSON.parse(await readFile(path.join(ROOT, "product.manifest.json"), "utf8"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  "governance/apps/web/src/components/loommate/LoomMate.tsx",
  ...["main.tsx", "App.tsx", "pages/p1/P1.tsx", "components/hud/DispatchBar.tsx",
    "components/CreditsPanel.tsx", "components/SimBanner.tsx", "components/SkillDistBanner.tsx",
    "components/BackendGate.tsx", "components/loommate/MateLive2D.tsx",
    "shell/Bridge.tsx", "shell/NavigationAccess.tsx", "shell/SideNav.tsx",
    "components/star-ring/StarRing.tsx", "lib/trpc.ts", "lib/product.ts", "lib/useAskRail.ts",
    "styles/tokens.css", "styles/subtitle.css"].map((file) => `governance/apps/web/src/${file}`),
  ...["tokens.css", "components.css", "content-safety.css", "managed-surface.js",
    "overlay-stack.js", "overlay-manager.js", "modal-document-state.js"]
    .map((file) => `governance/apps/web/node_modules/@workloom/ui/dist/${file}`),
  "governance/apps/web/vite.config.ts", "product.manifest.json",
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) =>
  [file, sha256(await readFile(path.join(ROOT, file)))])));
const SURFACE = '[data-wl-managed-surface="assistant-companion"]';
const avatar = (page) => page.getByRole("button", { name: /^小织助手；/ });
const reminders = (page) => page.getByRole("region", { name: "小织未读提醒", exact: true });
const toggle = (page) => page.getByRole("button", { name: /^(查看|收起)未读提醒/ });
const launch = (page) => page.getByRole("button", { name: /^启航/ });
const goalInput = (page) => page.getByPlaceholder("说出一句话目标…（最多 500 字）", { exact: true });
const TIME = "2026-10-20T04:00:00.000Z";
const GOAL = "本地模拟：生成本周运行复盘，列出三项下一步任务；编号１２３。";
// Existing shared-ui rocket path parse error, reproduced on the original source.
// Keep the raw console log and separate this exact message from new failures.
const KNOWN_ROCKET_ERROR = 'Error: <path> attribute d: Expected number, "…-1 0 1 1 3-1 6-3l-6 6-4-2z".';
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 },
  { width: 1440, height: 360 }, { width: 390, height: 360 }];
function stateFor(count, options) {
  return {
    calls: [], threads: Array.from({ length: 16 }, (_, i) => ({
      id: `MC039-old-${i}`, title: `本地模拟历史任务 ${i + 1}`, mode: "quest", status: "completed",
      progress_done: 2, progress_total: 2, agent_id: "local-agent", created_at: TIME,
    })),
    items: Array.from({ length: count }, (_, i) => ({
      id: `MC039-${i + 1}`, kind: i === 0 ? "alert" : "done", level: i === 0 ? "red" : "high",
      title: `本地模拟提醒 ${i + 1}`,
      body: count > 1
        ? "本地模拟提醒：已完成事项需要核对负责人、处理日期、异常说明与输出凭证。较长中文与编号１２３用于验证窄屏换行和真实滚动；不会触发任何外部业务。"
        : "本地模拟提醒，仅验证展示和操作。",
      actions: [{ label: "查看事项", link: "/tasks" }], link: "/tasks",
      status: "unread", created_at: TIME,
    })),
    settings: { member_no: "MC039-LOCAL", display_name: "本地模拟用户", persona_key: "tianmei",
      persona_custom: {}, voice_key: "sweet", voice_on: false, widget_size: options.expanded ? "large" : "small",
      quiet_start: "22:00", quiet_end: "08:00", channels: {} },
    denied: options.denied ?? false, failDispatch: options.failDispatch ?? false, holdDispatch: null,
  };
}
function accessFor(state) {
  return { subject: { kind: "member", id: "MC039-local", memberNo: "MC039-LOCAL", name: "本地模拟用户", role: state.denied ? "readonly" : "owner" },
    scope: { tenantId: "MC039-tenant", workspaceId: "MC039-workspace" }, plan: "pro",
    capabilities: {}, partnerCapabilities: [], availableScopes: [],
    authority: { source: "server", evaluatedAt: TIME, grantIds: ["MC039-local-fixture"] },
    navigationPermissions: BASE_NAVIGATION.map((entry) => entry.permission).filter(Boolean),
    actionPermissions: state.denied ? [] : ["task.dispatch"],
    bundle: { configured: false, reason: "Explicit synthetic local fixture" } };
}
async function routeFixture(route, state, origin, observation) {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) {
    if (url.hostname === "fonts.googleapis.com" && request.resourceType() === "stylesheet") {
      observation.fontFallbacks.push(url.href);
      return route.fulfill({ status: 200, contentType: "text/css", body: "/* MC039 explicit system-font fallback */" });
    }
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  if (url.pathname === "/health") return route.fulfill({ json: { ok: true, fixture: "MC039" } });
  if (url.pathname === "/api/voice/status") return route.fulfill({ json: { enabled: false, configured: false, dataMode: "simulated" } });
  if (url.pathname.startsWith("/trpc/")) {
    const names = decodeURIComponent(url.pathname.slice(6)).split(",");
    const inputs = JSON.parse(request.method() === "GET" ? url.searchParams.get("input") ?? "{}" : request.postData() ?? "{}");
    const responses = await Promise.all(names.map(async (name, index) => {
      const envelope = url.searchParams.get("batch") === "1" ? inputs[String(index)] ?? {} : inputs;
      const input = envelope?.json ?? envelope;
      state.calls.push({ name, method: request.method(), input });
      let data;
      switch (name) {
        case "access.me": data = accessFor(state); break;
        case "onboarding.status":
          data = { dataMode: "simulated", declaredDataMode: "simulated", defaultToolMode: "simulated",
            formalModeActive: false, formalActivationRecorded: false,
            llm: { provider: "mock", model: "local-fixture", real: false },
            workspace: { name: "本地模拟工作区", events: 16, members: 1, agents: 6, memories: 0 },
            bundle: { id: null, isExample: true }, business: null }; break;
        case "skills.skillOps.status":
          data = { pendingCount: 0, recentLoaded: [{ skillId: "local-skill", name: "本地模拟核对", version: "1.0", tier: "L1", at: TIME, auto: true }] }; break;
        case "threads.list": data = state.threads; break;
        case "workspace.profile": data = { name: "本地模拟工作区", stage: null }; break;
        case "workspace.agents":
          data = Array.from({ length: 6 }, (_, i) => ({ preset_key: `local-agent-${i + 1}`, name: `本地模拟员工${i + 1}`, version: "1.0", kind: "agent", status: "ready" })); break;
        case "members.list": data = [{ memberNo: "MC039-LOCAL", name: "本地模拟用户", role: "owner" }]; break;
        case "nightShift.current": data = { configured: true }; break;
        case "inspection.status": data = { lastRunAt: TIME, totalChecks: 0, okCount: 0, attention: [] }; break;
        case "approvals.list": data = []; break;
        case "credits.balance": data = { pools: [], balance: 0, totals: { granted: 0, purchased: 0, consumed: 0, platformSubsidy: 0 }, recent: [] }; break;
        case "credits.packs": data = { packs: [] }; break;
        case "service.secretary.settings": data = { settings: state.settings }; break;
        case "service.secretary.inbox": data = { items: state.items }; break;
        case "service.secretary.scan": data = { added: 0 }; break;
        case "service.secretary.markInbox":
          assert.equal(request.method(), "POST");
          assert.ok(["read", "acted"].includes(input.status));
          assert.equal(input.ids.length, 1);
          assert.ok(state.items.some((item) => item.id === input.ids[0]));
          state.items = state.items.filter((item) => !input.ids.includes(item.id));
          data = { dataMode: "simulated" }; break;
        case "service.secretary.saveSettings":
          assert.deepEqual(Object.keys(input), ["widget_size"]);
          state.settings = { ...state.settings, ...input }; data = { settings: state.settings }; break;
        case "threads.dispatch":
          assert.equal(request.method(), "POST");
          assert.equal(state.denied, false, "Readonly fixture must never receive dispatch");
          assert.deepEqual(input, { title: GOAL, presetKey: "local-agent-1" });
          if (state.holdDispatch) await state.holdDispatch;
          if (state.failDispatch) return { error: { message: "本地模拟任务暂时失败", code: -32603,
            data: { code: "INTERNAL_SERVER_ERROR", httpStatus: 500, path: name } } };
          state.threads = [{ id: "MC039-created", title: GOAL, mode: "quest", status: "queued",
            progress_done: 0, progress_total: 2, agent_id: "local-agent-1", created_at: TIME }, ...state.threads];
          data = { kind: "created", threadId: "MC039-created", dataMode: "simulated" }; break;
        default:
          observation.unexpectedProcedures.push(name);
          return { error: { message: "未登记的本地模拟调用", code: -32601,
            data: { code: "NOT_FOUND", httpStatus: 404, path: name } } };
      }
      return { result: { data } };
    }));
    return route.fulfill({ json: url.searchParams.get("batch") === "1" ? responses : responses[0] });
  }
  if (/^\/api(?:\/|$)/.test(url.pathname)) {
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
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      inside: rect.left >= -1 && rect.top >= -1 && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1,
      receives: hit === element || element.contains(hit), hit: hit?.outerHTML.slice(0, 900) ?? null };
  });
}
async function pointer(page, target, observation, name) {
  await target.scrollIntoViewIfNeeded();
  observation.geometry.push({ name, phase: "before", ...await geometry(target) });
  try {
    await target.click({ trial: true, timeout: 3000 });
    const actual = await geometry(target);
    observation.geometry.push({ name, phase: "after", ...actual });
    assert.ok(actual.inside && actual.receives, `${name}: ordinary pointer reaches the visible target`);
    await target.click();
    observation.actions.push({ name, method: "ordinary pointer click" });
  } catch (error) {
    await screenshot(page, observation, `${name}-failure`);
    throw error;
  }
}
async function tabTo(page, target, observation, name) {
  for (let i = 0; i < 180; i++) {
    if (await target.evaluate((element) => element === document.activeElement)) {
      observation.actions.push({ name, method: "sequential Tab", presses: i }); return;
    }
    await page.keyboard.press("Tab");
  }
  assert.fail(`${name}: sequential Tab reaches the target`);
}
async function noOverflow(page, observation) {
  const value = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  observation.geometry.push({ name: "horizontal-overflow", ...value });
  assert.ok(value.scrollWidth <= value.width + 1);
}
async function dispatchSuccess(page, state, observation) {
  await expect(launch(page)).toBeDisabled();
  await goalInput(page).fill(GOAL);
  await expect(launch(page)).toBeEnabled();
  await pointer(page, launch(page), observation, "launch-task");
  await expect.poll(() => state.calls.filter((call) => call.name === "threads.dispatch").length).toBe(1);
  await expect(goalInput(page)).toHaveValue("");
  await expect(page.getByText(`${GOAL}（进度 0/2）`, { exact: true })).toHaveCount(1);
  assert.equal(state.threads[0].title, GOAL);
  observation.readback = { threadId: state.threads[0].id, title: state.threads[0].title, dataMode: "simulated",
    listQueries: state.calls.filter((call) => call.name === "threads.list").length };
  assert.ok(observation.readback.listQueries >= 2, "Success is read back through the real task list query");
}
async function reminderActions(page, state, observation, keyboard = false) {
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await expect(reminders(page)).toHaveCount(0);
  if (keyboard) {
    await tabTo(page, toggle(page), observation, "notification-open-keyboard");
    await page.keyboard.press("Enter");
  } else await pointer(page, toggle(page), observation, "notification-open");
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
  await expect(reminders(page)).toBeVisible();
  await screenshot(page, observation, "notifications-open");
  const first = state.items[0];
  const card = reminders(page).locator("div:has(> .wl-action-row)").filter({ has: page.getByText(first.title, { exact: true }) });
  await pointer(page, card.getByRole("button", { name: "稍后", exact: true }), observation, "notification-later");
  await expect(page.getByText(first.title, { exact: true })).toHaveCount(0);
  assert.deepEqual(state.calls.filter((call) => call.name === "service.secretary.markInbox").at(-1).input,
    { ids: [first.id], status: "read" });
  if (state.items.length > 0) {
    const second = state.items[0];
    const nextCard = reminders(page).locator("div:has(> .wl-action-row)").filter({ has: page.getByText(second.title, { exact: true }) });
    await pointer(page, nextCard.getByRole("button", { name: "查看事项", exact: true }), observation, "notification-act");
    await expect(page.getByText(second.title, { exact: true })).toHaveCount(0);
    assert.deepEqual(state.calls.filter((call) => call.name === "service.secretary.markInbox").at(-1).input,
      { ids: [second.id], status: "acted" });
    await expect(reminders(page)).toHaveCount(0);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
    await pointer(page, toggle(page), observation, "notification-reopen");
    await page.keyboard.press("Escape");
    await expect(reminders(page)).toHaveCount(0);
    await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  } else {
    await expect(toggle(page)).toHaveCount(0);
    await expect(reminders(page)).toHaveCount(0);
  }
}

test("MC-039 unread reminders keep the actual task launch and notification controls usable", { timeout: 600_000 }, async (t) => {
  const receipt = { card: "MC-039", actor: "repair", environment: "local-preview",
    startedAt: new Date().toISOString(), fixture: "Actual main/App /tasks, production CSS, explicit local HTTP only",
    browserChannel: process.env.MC039_BROWSER_CHANNEL ?? "chromium",
    sourceBefore: await sourceHashes(), runnerSha256: sha256(await readFile(TEST_FILE)),
    observations: [], checks: [], cleanupErrors: [] };
  let server, browser, failure = null;
  if (OUT) await mkdir(OUT, { recursive: true });
  try {
    server = await createServer({ root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false } });
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address === "object");
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({ ...(process.env.MC039_BROWSER_CHANNEL ? { channel: process.env.MC039_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"] });
    receipt.browserVersion = browser.version();
    async function scenario(label, viewport, count, run, options = {}) {
      await t.test(label, async () => {
        const observation = { label, viewport, count, options, passed: false,
          preconditions: "Fresh context with authorized synthetic identity, actual /tasks and default product shell",
          expected: "Unread reminders aggregate by default; task launch creates exactly one synthetic task and reads it back; explicit reminder controls remain accessible",
          actions: [], geometry: [], pageErrors: [], consoleErrors: [], knownConsoleErrors: [],
          blockedRequests: [], unexpectedProcedures: [], fontFallbacks: [] };
        receipt.observations.push(observation);
        const state = stateFor(count, options);
        const context = await browser.newContext({ viewport, locale: "zh-CN", serviceWorkers: "block", reducedMotion: "reduce" });
        let page;
        try {
          await context.route("**/*", (route) => routeFixture(route, state, origin, observation));
          await context.addInitScript(({ productId, expanded }) => {
            localStorage.setItem(`workloom:${productId}:b-pc:access-token`, "MC039-explicit-local-http-fixture");
            if (expanded) localStorage.setItem("loommate.mini", "0");
            localStorage.setItem("wl-voice-gate", "captions");
          }, { productId: manifest.productId, expanded: options.expanded ?? false });
          page = await context.newPage();
          page.setDefaultTimeout(15000); page.setDefaultNavigationTimeout(45000);
          page.on("pageerror", (error) => observation.pageErrors.push(String(error)));
          page.on("console", (message) => {
            if (message.type() !== "error") return;
            observation.consoleErrors.push(message.text());
            if (message.text() === KNOWN_ROCKET_ERROR) observation.knownConsoleErrors.push(message.text());
          });
          await page.clock.install({ time: new Date(TIME) });
          await page.goto(`${origin}/tasks`);
          await expect(page.getByRole("heading", { name: "工作台 · 总览", exact: true })).toBeVisible();
          await expect(avatar(page)).toBeVisible();
          await expect.poll(() => state.calls.filter((call) => call.name === "threads.list").length).toBeGreaterThan(0);
          await page.evaluate(async () => { await document.fonts.ready; });
          await screenshot(page, observation, "initial");
          await run(page, state, observation);
          await noOverflow(page, observation);
          assert.deepEqual(observation.pageErrors, []);
          const allowedErrors = options.failDispatch ? observation.consoleErrors.filter((message) =>
            /工作台派遣失败|TRPCClientError|Failed to load resource: the server responded with a status of 500/.test(message)) : [];
          assert.deepEqual(observation.consoleErrors.filter((message) =>
            message !== KNOWN_ROCKET_ERROR && !allowedErrors.includes(message)), []);
          assert.deepEqual(observation.blockedRequests, []);
          assert.deepEqual(observation.unexpectedProcedures, []);
          observation.passed = true;
        } catch (error) {
          observation.error = String(error);
          if (page && !page.isClosed()) await screenshot(page, observation, "failure");
          throw error;
        } finally {
          observation.calls = state.calls;
          try { await context.close(); }
          finally { if (OUT) await writeFile(path.join(OUT, `${label}.json`), JSON.stringify(observation, null, 2) + "\n"); }
        }
      });
    }
    for (const viewport of VIEWPORTS) for (const count of [0, 1, 10]) {
      await scenario(`${viewport.width}x${viewport.height}-unread-${count}`, viewport, count, async (page, state, observation) => {
        await dispatchSuccess(page, state, observation);
        if (count > 0) await reminderActions(page, state, observation, count === 1);
        else {
          await expect(toggle(page)).toHaveCount(0);
          await expect(reminders(page)).toHaveCount(0);
        }
        await screenshot(page, observation, "task-and-notifications");
      });
    }
    for (const viewport of [VIEWPORTS[0], VIEWPORTS[3]]) {
      await scenario(`${viewport.width}x${viewport.height}-dispatch-failure-retry`, viewport, 10, async (page, state, observation) => {
        let release;
        state.holdDispatch = new Promise((resolve) => { release = resolve; });
        await goalInput(page).fill(GOAL);
        try {
          await pointer(page, launch(page), observation, "launch-failing-task");
          await expect(page.getByRole("button", { name: "取消", exact: true })).toBeVisible();
          await expect(launch(page)).toHaveCount(0);
        } finally { release(); }
        await expect(launch(page)).toBeEnabled();
        await expect(goalInput(page)).toHaveValue(GOAL);
        await expect(page.getByText(`${GOAL}（进度 0/2）`, { exact: true })).toHaveCount(0);
        assert.equal(state.threads.length, 16);
        state.failDispatch = false; state.holdDispatch = null;
        await pointer(page, launch(page), observation, "retry-task");
        await expect(page.getByText(`${GOAL}（进度 0/2）`, { exact: true })).toHaveCount(1);
        await expect(goalInput(page)).toHaveValue("");
        assert.equal(state.calls.filter((call) => call.name === "threads.dispatch").length, 2);
      }, { failDispatch: true });
      await scenario(`${viewport.width}x${viewport.height}-readonly`, viewport, 10, async (page, state, observation) => {
        await expect(launch(page)).toHaveCount(0);
        await expect(goalInput(page)).toHaveCount(0);
        await reminderActions(page, state, observation);
        assert.equal(state.calls.filter((call) => call.name === "threads.dispatch").length, 0);
      }, { denied: true });
      await scenario(`${viewport.width}x${viewport.height}-expanded-then-mini`, viewport, 10, async (page, state, observation) => {
        await expect(page.getByText(state.items[0].title, { exact: true })).toBeVisible();
        await pointer(page, page.getByTitle("收起为小球（不挡界面）", { exact: true }), observation, "collapse-large-assistant");
        await expect(reminders(page)).toHaveCount(0);
        await dispatchSuccess(page, state, observation);
        await reminderActions(page, state, observation);
      }, { expanded: true });
    }
    await scenario("notification-poll-preserves-collapsed-state", VIEWPORTS[0], 1, async (page, state, observation) => {
      await reminderActions(page, state, observation, true);
      state.items = [{ ...stateFor(1, {}).items[0], id: "MC039-new", title: "本地模拟新提醒" }];
      await page.clock.fastForward(20000);
      await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
      await expect(reminders(page)).toHaveCount(0);
      await dispatchSuccess(page, state, observation);
      await pointer(page, toggle(page), observation, "new-notification-open");
      await expect(reminders(page).getByText("本地模拟新提醒", { exact: true })).toBeVisible();
      await pointer(page, toggle(page), observation, "new-notification-collapse");
      await expect(reminders(page)).toHaveCount(0);
    });
    await t.test("The runner and relevant source bytes remain stable", async () => {
      assert.deepEqual(await sourceHashes(), receipt.sourceBefore);
      assert.equal(sha256(await readFile(TEST_FILE)), receipt.runnerSha256);
      receipt.checks.push({ name: "source-stability", passed: true });
    });
  } catch (error) { failure = String(error); throw error; }
  finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors = cleanup.filter((result) => result.status === "rejected").map((result) => String(result.reason));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.runnerStable = sha256(await readFile(TEST_FILE)) === receipt.runnerSha256;
    receipt.finishedAt = new Date().toISOString(); receipt.error = failure;
    receipt.passed = !failure && receipt.observations.length === 19 && receipt.observations.every((row) => row.passed)
      && receipt.sourceStable && receipt.runnerStable && receipt.cleanupErrors.length === 0 && receipt.checks.length === 1;
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, []);
  }
});
