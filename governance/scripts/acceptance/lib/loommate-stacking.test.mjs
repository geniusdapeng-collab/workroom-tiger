/**
 * MC-037: the actual main/App compositions keep inline banners below LoomMate.
 * Browser requests use explicit local HTTP fixtures. No backend, database,
 * model or broker is contacted. The real /agents (P8/Bridge), / (P0) and
 * /members (P30 modal regression) routes are mounted without component mocks.
 * MC037_SOURCE_DIR supplies only the two frozen banner sources for paired red.
 * The pass/fail contract is banner actionability and overlay priority. Original
 * axe contrast results are also retained: bgOverlap is explicitly unverified,
 * never a contrast pass. MC034 retains its separate positive-contrast contract.
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
const BANNERS = ["SimBanner.tsx", "SkillDistBanner.tsx"]
  .map((name) => `governance/apps/web/src/components/${name}`);
const SOURCE_ROOT = path.resolve(process.env.MC037_SOURCE_DIR ?? ROOT);
const OUT = process.env.MC037_EVIDENCE_DIR ? path.resolve(process.env.MC037_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const requireGovernance = createRequire(path.join(ROOT, "governance/package.json"));
const { chromium, expect } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const requireAxe = createRequire(requireGovernance.resolve("@axe-core/playwright"));
const axeSource = requireAxe("axe-core").source;
const { BASE_NAVIGATION } = await import(pathToFileURL(
  path.join(WEB, "node_modules/@workloom/ui/dist/navigation.js")));
const manifest = JSON.parse(await readFile(path.join(ROOT, "product.manifest.json"), "utf8"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  ...BANNERS,
  ...["main.tsx", "App.tsx"].map((name) => `governance/apps/web/src/${name}`),
  ...["SideNav.tsx", "NavigationAccess.tsx", "NavMenu.tsx", "Bridge.tsx", "PlanSwitcher.tsx", "IndustryRoutes.tsx"]
    .map((name) => `governance/apps/web/src/shell/${name}`),
  ...["p0/P0.tsx", "p8/P8.tsx", "p30/P30.tsx"].map((name) => `governance/apps/web/src/pages/${name}`),
  ...["trpc.ts", "product.ts", "useAskRail.ts", "display.ts"]
    .map((name) => `governance/apps/web/src/lib/${name}`),
  ...["BackendGate.tsx", "loommate/LoomMate.tsx", "loommate/MateLive2D.tsx",
    "star-ring/StarRing.tsx", "ValueCounters.tsx", "hud/EmergencyBrake.tsx", "hud/NightStatusPill.tsx"]
    .map((name) => `governance/apps/web/src/components/${name}`),
  ...["tokens.css", "subtitle.css"].map((name) => `governance/apps/web/src/styles/${name}`),
  ...["vite.config.ts", "package.json", "index.html"].map((name) => `governance/apps/web/${name}`),
  ...["vite-product.mjs", "product-runtime.mjs"].map((name) => `governance/scripts/${name}`),
  "product.manifest.json", "governance/apps/web/node_modules/@workloom/ui/package.json",
  ...["tokens.css", "components.css", "content-safety.css", "managed-surface.js",
    "modal-document-state.js", "overlay-stack.js", "overlay-manager.js", "components.js",
    "navigation.js", "language.js", "icons.js", "forms.js"]
    .map((name) => `governance/apps/web/node_modules/@workloom/ui/dist/${name}`),
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [
  file, sha256(await readFile(path.join(BANNERS.includes(file) ? SOURCE_ROOT : ROOT, file))),
])));
const SURFACE = '[data-wl-managed-surface="assistant-companion"]';
const avatar = (page) => page.getByRole("button", { name: /^小织助手；/ });
const panel = (page) => page.getByRole("region", { name: "小织助手面板", exact: true });
const closePanel = (page) => page.getByRole("button", { name: "关闭小织助手面板", exact: true });
const badge = (page, count) => page.locator(`${SURFACE} span`).filter({ hasText: new RegExp(`^${count}$`) });
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 },
  { width: 1440, height: 360 }, { width: 390, height: 360 }];
const EDGE_VIEWPORTS = [VIEWPORTS[0], VIEWPORTS[3]];
const BRANCHES = ["example", "warning", "status-error", "skills-pending", "skills-loaded"];
const NAV_PERMISSIONS = BASE_NAVIGATION.map((entry) => entry.permission).filter(Boolean);
const ACTION_PERMISSIONS = ["member.manage", "member.role.manage", "workspace.configure", "agent.manage"];
const FROZEN_TIME = "2026-10-20T04:00:00.000Z";

function itemsFor(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `MC037-${index + 1}`, kind: "done", level: "high", status: "unread",
    title: `本地模拟提醒 ${index + 1}`,
    body: "这是一条明确标注为模拟的较长提醒：请核对已完成事项、处理时间、负责人和异常说明；仅用于检查真实页面上的换行、层叠与操作可达性。",
    actions: [{ label: "查看事项", link: "/agents" }], link: "/agents", created_at: FROZEN_TIME,
  }));
}
function stateFor(branch, count = 10, size = "large") {
  return {
    branch, items: itemsFor(count), calls: [], denied: false,
    settings: { member_no: "MC037-LOCAL", display_name: "本地模拟用户", persona_key: "tianmei",
      persona_custom: {}, voice_key: "sweet", voice_on: false, widget_size: size,
      quiet_start: "22:00", quiet_end: "08:00", channels: {} },
  };
}
function onboardingFor(state) {
  const isExample = ["example", "both"].includes(state.branch);
  const configured = ["none", "skills-pending", "skills-loaded"].includes(state.branch);
  return { dataMode: configured ? "real" : "simulated",
    declaredDataMode: configured ? "real" : "simulated",
    defaultToolMode: configured ? "connector-required" : "simulated",
    formalModeActive: configured, formalActivationRecorded: configured,
    llm: { provider: configured ? "local-fixture" : "mock", model: "local-fixture", baseUrl: "", real: configured },
    workspace: { name: "本地模拟层叠检查工作区（长名称与中文标点）", events: 12, members: 1, agents: 37, memories: 0 },
    bundle: { id: null, isExample }, business: null,
  };
}
function skillsFor(state) {
  return { pendingCount: state.branch === "skills-pending" ? 3 : 0,
    recentLoaded: ["skills-loaded", "both"].includes(state.branch) ? [
      { skillId: "local-one", name: "本地模拟数据核对与异常记录", version: "1.2.3", tier: "L1", at: FROZEN_TIME, auto: true },
      { skillId: "local-two", name: "本地模拟经营计划跟踪与事项复核", version: "2.3.4", tier: "L1", at: FROZEN_TIME, auto: true },
    ] : [] };
}
function accessFor(state) {
  return { subject: { kind: "member", id: "MC037-local", memberNo: "MC037-LOCAL", name: "本地模拟用户", role: state.denied ? "readonly" : "owner" },
    scope: { tenantId: "MC037-tenant", workspaceId: "MC037-workspace" }, plan: "pro",
    capabilities: {}, partnerCapabilities: [], availableScopes: [],
    authority: { source: "server", evaluatedAt: FROZEN_TIME, grantIds: ["MC037-local-fixture"] },
    navigationPermissions: state.denied ? ["today.read", "account.read"] : NAV_PERMISSIONS,
    actionPermissions: state.denied ? [] : ACTION_PERMISSIONS,
    bundle: { configured: false, reason: "Explicit local HTTP fixture, no industry data" } };
}
function rosterFor() {
  const game = { level: 1, rank: "青铜", xp: 0, xpFloor: 0, xpNext: 100 };
  return { nightWindow: { open: false, range: "22:00–08:00" },
    humans: [{ memberNo: "MC037-LOCAL", name: "本地模拟用户", role: "owner", online: false,
      stats: { decided30: 0, dispatched30: 0, settled30: 0 }, game }],
    agents: Array.from({ length: 37 }, (_, index) => ({
      id: `local-agent-${index + 1}`, presetKey: `local-agent-${index + 1}`, name: `模拟数字员工${index + 1}`,
      alias: null, version: "1.0", kind: "agent", readonly: true, status: "active", invalidReason: null,
      fenceBindings: [], skills: [], nightShift: false, highRisk: false, description: "本地模拟员工。",
      online: false, lastAction: null, lastActionAt: null, pendingApprovals: 0, blockedRecent: 0,
      stats: { actions30: 0, adopted30: 0, rejected30: 0, adoptionRate: null, credits30: 0, offPeakRatio: null }, game,
    })) };
}
async function routeFixture(route, state, origin, observation) {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) {
    // The product index requests a public font stylesheet. An explicit empty
    // response keeps this isolated fixture on the product's system-font fallback.
    if (url.hostname === "fonts.googleapis.com" && request.resourceType() === "stylesheet") {
      observation.fontFallbacks.push(url.href);
      return route.fulfill({ status: 200, contentType: "text/css", body: "/* MC037 isolated system-font fallback */" });
    }
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  if (url.pathname === "/health") return route.fulfill({ json: { ok: true, fixture: "MC037" } });
  if (url.pathname === "/api/voice/status") return route.fulfill({ json: { enabled: false, configured: false, dataMode: "simulated" } });
  if (url.pathname.startsWith("/trpc/")) {
    const names = decodeURIComponent(url.pathname.slice(6)).split(",");
    const inputs = JSON.parse(request.method() === "GET" ? url.searchParams.get("input") ?? "{}" : request.postData() ?? "{}");
    const responses = names.map((name, index) => {
      const envelope = url.searchParams.get("batch") === "1" ? inputs[String(index)] ?? {} : inputs;
      const input = envelope?.json ?? envelope;
      state.calls.push({ name, method: request.method(), input });
      let data;
      switch (name) {
        case "access.me": data = accessFor(state); break;
        case "onboarding.status":
          if (state.branch === "status-error") return { error: { message: "本地模拟状态查询失败", code: -32603,
            data: { code: "INTERNAL_SERVER_ERROR", httpStatus: 500, path: name } } };
          data = onboardingFor(state); break;
        case "onboarding.welcomeStatus":
          data = { status: "completed", currentStep: "done", journeyVersion: 1, replayCount: 0,
            shouldShow: false, persisted: true, role: "owner" }; break;
        case "skills.skillOps.status": data = skillsFor(state); break;
        case "roster.list": data = rosterFor(); break;
        case "captain.theater":
          data = { mode: "disabled", ceoName: "模拟公司负责人", pendingByTier: {}, latestBriefing: null, satellites: [], ticker: [] }; break;
        case "captain.chairmanQueue": data = []; break;
        case "members.list":
          data = [{ id: "MC037-local", memberNo: "MC037-LOCAL", name: "本地模拟用户", role: "owner", status: "active" }]; break;
        case "accounts.admin.approvalPolicies":
        case "accounts.admin.apiKeys": data = []; break;
        case "service.secretary.settings": data = { settings: state.settings }; break;
        case "service.secretary.inbox": data = { items: state.items }; break;
        case "service.secretary.scan": data = { added: 0 }; break;
        case "service.secretary.saveSettings":
          assert.deepEqual(Object.keys(input), ["widget_size"], "Only local size preference changes are exercised");
          assert.ok(["small", "large", "fullscreen"].includes(input.widget_size));
          state.settings = { ...state.settings, ...input }; data = { settings: state.settings }; break;
        case "service.secretary.markInbox":
          assert.equal(input.status, "read");
          assert.deepEqual(input.ids, ["MC037-1"]);
          state.items = state.items.filter((item) => !input.ids.includes(item.id));
          data = { dataMode: "simulated" }; break;
        default:
          observation.unexpectedProcedures.push(name);
          return { error: { message: "未登记的本地模拟调用", code: -32601,
            data: { code: "NOT_FOUND", httpStatus: 404, path: name } } };
      }
      return { result: { data } };
    });
    return route.fulfill({ json: url.searchParams.get("batch") === "1" ? responses : responses[0] });
  }
  if (/^\/api(?:\/|$)/.test(url.pathname)) {
    observation.blockedRequests.push(url.href);
    return route.abort("blockedbyclient");
  }
  return route.continue();
}
function banner(page, branch) {
  if (branch === "status-error") return page.getByRole("status").filter({ hasText: "运行状态暂时无法确认" });
  const target = branch === "example" || branch === "both"
    ? page.getByRole("link", { name: "定制我的行业版 →", exact: true })
    : branch === "warning" ? page.getByRole("link", { name: "查看落地向导 →", exact: true })
      : branch === "skills-pending" ? page.getByRole("link", { name: "去审批 →", exact: true })
        : page.getByRole("link", { name: "去技能中心 →", exact: true });
  return target.locator("..");
}
async function screenshot(page, observation, name) {
  if (OUT) await page.screenshot({ path: path.join(OUT, `${observation.label}-${name}.png`), fullPage: false, animations: "disabled" });
}
async function geometry(locator, position = null) {
  return locator.evaluate((element, position) => {
    const rect = element.getBoundingClientRect();
    const x = rect.left + (position?.x ?? rect.width / 2), y = rect.top + (position?.y ?? rect.height / 2);
    const hit = document.elementFromPoint(x, y);
    const describe = (node) => node ? { tag: node.tagName, role: node.getAttribute("role"),
      label: node.getAttribute("aria-label"), class: node.className?.baseVal ?? node.className,
      text: node.textContent?.trim().slice(0, 200), zIndex: getComputedStyle(node).zIndex } : null;
    return { rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height },
      inside: rect.left >= -1 && rect.top >= -1 && rect.right <= innerWidth + 1 && rect.bottom <= innerHeight + 1,
      receives: hit === element || element.contains(hit), hit: describe(hit),
      stack: document.elementsFromPoint(x, y).slice(0, 10).map(describe) };
  }, position);
}
async function pointer(page, target, observation, name, trial = false, position = null) {
  await target.scrollIntoViewIfNeeded();
  observation.geometry.push({ name, phase: "before-actionability", position, ...await geometry(target, position) });
  try {
    // Native actionability waits for the actual React/ResizeObserver layout.
    // Geometry sampled before that wait can belong to the preceding panel size.
    await target.click({ trial: true, timeout: 15000, ...(position ? { position } : {}) });
    const actual = await geometry(target, position);
    observation.geometry.push({ name, phase: "after-actionability", position, ...actual });
    assert.ok(actual.inside, `${name}: control must be within the viewport after native scroll`);
    assert.ok(actual.receives, `${name}: control must receive the ordinary pointer at the observed point`);
    if (!trial) await target.click(position ? { position } : {});
    observation.actions.push({ name, method: trial ? "ordinary pointer actionability" : "ordinary pointer click" });
  } catch (error) {
    await screenshot(page, observation, `${name}-failure`);
    throw error;
  }
}
async function tabTo(page, target, observation, name) {
  for (let index = 0; index < 180; index++) {
    if (await target.evaluate((element) => element === document.activeElement)) {
      observation.actions.push({ name, method: "sequential Tab", presses: index }); return;
    }
    await page.keyboard.press("Tab");
  }
  assert.fail(`${name}: target must be reachable by sequential Tab`);
}
async function noHorizontalOverflow(page, observation) {
  const result = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  observation.geometry.push({ name: "document-width", ...result });
  assert.ok(result.scrollWidth <= result.width + 1, "The real App must not gain horizontal overflow");
}
async function measureBadge(page, count, observation, phase) {
  const target = badge(page, count);
  await expect(target).toHaveCount(1);
  await target.scrollIntoViewIfNeeded();
  const measurement = await target.evaluate((element) => {
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    const range = document.createRange(); range.selectNodeContents(element);
    const textRect = range.getBoundingClientRect();
    return { text: element.textContent, foreground: style.color, background: style.backgroundColor,
      fontSize: style.fontSize, fontWeight: style.fontWeight, pointerEvents: style.pointerEvents,
      renderers: [...element.closest('[data-wl-managed-surface="assistant-companion"]')
        .querySelectorAll("[data-render-mode]")].map((renderer) => ({
          mode: renderer.getAttribute("data-render-mode"), ready: renderer.getAttribute("data-avatar-ready") })),
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
      textRect: { left: textRect.left, top: textRect.top, right: textRect.right, bottom: textRect.bottom } };
  });
  await page.evaluate(axeSource);
  const selector = `${SURFACE} span`;
  const results = await page.evaluate(async ({ selector, count }) => {
    const node = [...document.querySelectorAll(selector)].find((element) => element.textContent.trim() === String(count));
    if (!node) throw new Error("Live numeric badge not found");
    // First run the untouched original contrast rule. Then use the documented
    // axe.setup/teardown lifecycle for a separate read-only virtual-stack sample.
    // No product DOM, style or axe rule is changed to obtain that observation.
    const result = await axe.run(node, { runOnly: { type: "rule", values: ["color-contrast"] } });
    let diagnostic = null, diagnosticError = null;
    try {
      axe.setup(document.documentElement);
      const describe = (candidate) => {
        const rect = candidate.getBoundingClientRect(), style = getComputedStyle(candidate);
        return { tag: candidate.tagName, class: candidate.className?.baseVal ?? candidate.className,
          role: candidate.getAttribute("role"), label: candidate.getAttribute("aria-label"),
          sameAsBadge: candidate === node, text: candidate.textContent?.trim().slice(0, 120),
          rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
          zIndex: style.zIndex, pointerEvents: style.pointerEvents };
      };
      const range = document.createRange(); range.selectNodeContents(node);
      const textRect = range.getBoundingClientRect();
      diagnostic = { textStack: axe.commons.dom.getTextElementStack(node).map((stack) => stack.slice(0, 16).map(describe)),
        pointerStack: document.elementsFromPoint(textRect.left + textRect.width / 2, textRect.top + textRect.height / 2)
          .slice(0, 16).map(describe) };
    } catch (error) { diagnosticError = String(error); }
    finally { axe.teardown(); }
    return { diagnostic, diagnosticError, violations: result.violations.filter((rule) => rule.id === "color-contrast"),
      incomplete: result.incomplete.filter((rule) => rule.id === "color-contrast"),
      passes: result.passes.filter((rule) => rule.id === "color-contrast"),
      version: axe.version };
  }, { selector, count });
  const positiveContrast = results.passes.some((rule) => rule.nodes.length > 0);
  const contrastStatus = results.violations.length > 0 ? "failed"
    : results.incomplete.length > 0 ? "unverified" : positiveContrast ? "passed" : "not-evaluated";
  observation.measurements.push({ phase, ...measurement, contrastStatus, axe: results });
  await screenshot(page, observation, phase);
  assert.equal(measurement.text, String(count));
  assert.equal(results.diagnosticError, null, `${phase}: the observation-only axe stack sample completes`);
  assert.ok(results.diagnostic?.textStack.length > 0, `${phase}: the live text has a nonempty axe paint stack`);
  assert.deepEqual(results.violations, [], `${phase}: unread text has no proved contrast failure`);
  if (results.incomplete.length > 0) {
    assert.ok(results.incomplete.every((rule) => rule.nodes.length > 0 && rule.nodes.every((node) => {
      const checks = [...node.any, ...node.all, ...node.none];
      return checks.length > 0 && checks.every((check) => check.data?.messageKey === "bgOverlap");
    })), `${phase}: any unresolved contrast reason other than the separately recorded bgOverlap remains a failure`);
  } else {
    assert.ok(positiveContrast, `${phase}: absent an explicit bgOverlap result, the original axe rule must positively evaluate unread text`);
  }
}
async function assistantPaths(page, state, observation) {
  await pointer(page, avatar(page), observation, "mini-expand");
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByTitle("切换大小", { exact: true })).toBeVisible();
  await expect(page.locator(`${SURFACE} [data-avatar-ready="true"]`)).toHaveCount(1);
  await pointer(page, avatar(page), observation, "expanded-open");
  await expect(panel(page)).toBeVisible();
  await pointer(page, closePanel(page), observation, "panel-close");
  await expect(panel(page)).toHaveCount(0);
  await tabTo(page, avatar(page), observation, "keyboard-open");
  await page.keyboard.press("Enter");
  await expect(panel(page)).toBeVisible();
  await tabTo(page, closePanel(page), observation, "keyboard-close");
  await page.keyboard.press("Space");
  await expect(panel(page)).toHaveCount(0);
  assert.equal(state.calls.filter((call) => call.name === "service.secretary.chat").length, 0);
}
async function forcedBannerOverlap(page, state, observation) {
  const targetBanner = banner(page, state.branch);
  await targetBanner.scrollIntoViewIfNeeded();
  const initialBanner = await targetBanner.boundingBox();
  assert.ok(initialBanner);
  const maxAvatarCenter = observation.viewport.height - 138;
  if (initialBanner.y > maxAvatarCenter + 24) {
    // A short page can place a banner below the assistant's legal keyboard
    // range. Scroll the document using the ordinary pointer before moving the
    // assistant; never claim overlap when their visible rectangles are disjoint.
    await page.mouse.move(initialBanner.x + 20, Math.min(observation.viewport.height - 20, initialBanner.y + 20));
    await page.mouse.wheel(0, Math.ceil(initialBanner.y - maxAvatarCenter + 40));
    await expect.poll(async () => (await targetBanner.boundingBox()).y).toBeLessThan(initialBanner.y);
    observation.geometry.push({ name: "native-page-scroll-for-overlap", before: initialBanner,
      after: await targetBanner.boundingBox(), method: "ordinary mouse wheel" });
  }
  await tabTo(page, avatar(page), observation, "move-mini-over-banner");
  let current;
  for (let attempt = 0; attempt < 60; attempt++) {
    const bounds = await targetBanner.boundingBox(), widget = await avatar(page).boundingBox();
    assert.ok(bounds && widget);
    // The product keyboard movement bounds use the selected small widget's
    // 170px height even while its 64px mini button is displayed. Preserve those
    // real bounds and test the visible intersection, including partial overlap.
    const desired = Math.min(observation.viewport.height - 138, Math.max(48, bounds.y + bounds.height / 2));
    const delta = desired - (widget.y + widget.height / 2);
    current = { banner: bounds, avatar: widget, desired, delta };
    if (Math.abs(delta) <= 8) break;
    await page.keyboard.press(delta < 0 ? "ArrowUp" : "ArrowDown");
    await expect.poll(async () => (await avatar(page).boundingBox()).y,
      { timeout: 15000, message: "The product must render each ordinary keyboard movement before another key is sent" }).not.toBe(widget.y);
  }
  observation.geometry.push({ name: "real-keyboard-overlap", ...current });
  const { banner: bounds, avatar: widget } = current;
  const intersection = { left: Math.max(bounds.x, widget.x, 0), top: Math.max(bounds.y, widget.y, 0),
    right: Math.min(bounds.x + bounds.width, widget.x + widget.width, observation.viewport.width),
    bottom: Math.min(bounds.y + bounds.height, widget.y + widget.height, observation.viewport.height) };
  assert.ok(intersection.right - intersection.left > 4 && intersection.bottom - intersection.top > 4,
    "The ordinary click target must actually overlap this banner, not merely sit elsewhere");
  const position = { x: (intersection.left + intersection.right) / 2 - widget.x,
    y: (intersection.top + intersection.bottom) / 2 - widget.y };
  observation.geometry.push({ name: "actual-overlap-point", intersection, position });
  await screenshot(page, observation, "banner-overlap");
  await pointer(page, avatar(page), observation, "overlapping-mini-open", false, position);
  await expect(panel(page)).toHaveCount(0);
  await pointer(page, avatar(page), observation, "overlapping-expanded-open");
  await expect(panel(page)).toBeVisible();
  await pointer(page, closePanel(page), observation, "overlapping-panel-close");
}
async function modalPaths(page, state, observation) {
  await page.getByLabel("成员姓名（必填）", { exact: true }).fill("本地模拟待邀请者");
  await page.getByLabel("成员手机号（必填）", { exact: true }).fill("13800000000");
  await pointer(page, page.getByRole("button", { name: "预览邀请", exact: true }), observation, "preview-invitation");
  const modal = page.getByRole("dialog", { name: "确认创建成员邀请", exact: true });
  await expect(modal).toBeVisible();
  const priority = await modal.evaluate((element) => {
    const surface = document.querySelector('[data-wl-managed-surface="assistant-companion"]');
    const rect = surface.getBoundingClientRect();
    const x = Math.min(innerWidth - 1, Math.max(1, rect.right - 20));
    const y = Math.min(innerHeight - 1, Math.max(1, rect.bottom - 20));
    const hit = document.elementFromPoint(x, y);
    const overlay = element.closest(".wl-overlay");
    return { backgroundInert: Boolean(surface.closest("[inert]")),
      modalReceives: overlay === hit || overlay.contains(hit), point: { x, y }, hit: hit?.outerHTML.slice(0, 500) };
  });
  observation.geometry.push({ name: "dialog-priority", ...priority });
  assert.ok(priority.backgroundInert && priority.modalReceives, "The real modal takes precedence over the assistant");
  await screenshot(page, observation, "invitation-modal");
  await pointer(page, modal.getByRole("button", { name: "取消", exact: true }), observation, "cancel-preview");
  await expect(modal).toHaveCount(0);
  assert.equal(state.calls.filter((call) => call.name.startsWith("accounts.") && call.method === "POST").length, 0);
  if (observation.viewport.width < 820) {
    await pointer(page, page.getByRole("button", { name: "展开主导航", exact: true }), observation, "open-navigation");
    const nav = page.getByRole("dialog", { name: "主导航", exact: true });
    await expect(nav).toBeVisible();
    await expect(nav.getByRole("searchbox", { name: "搜索主导航", exact: true })).toBeFocused();
    await screenshot(page, observation, "navigation-modal");
    await page.keyboard.press("Escape");
    await expect(nav).toHaveCount(0);
    await expect(page.getByRole("button", { name: "展开主导航", exact: true })).toBeFocused();
    await pointer(page, page.getByRole("button", { name: "展开 AI 助手", exact: true }).first(), observation, "open-global-assistant");
    const rail = page.getByRole("dialog", { name: "AI 助手", exact: true });
    await expect(rail).toBeVisible();
    await screenshot(page, observation, "global-assistant-modal");
    await page.keyboard.press("Escape");
    await expect(rail).toHaveCount(0);
  }
}

test("MC-037 inline banners and assistant coexist in the actual App shell", { timeout: 2_400_000 }, async (t) => {
  const receipt = { card: "MC-037", actor: "repair", environment: "local-preview", startedAt: new Date().toISOString(),
    passScope: "Inline banner actionability, ordinary pointer/keyboard paths and overlay priority; contrast diagnostics are reported separately",
    fixture: "Real main/App, routes, overlays and production CSS; explicit local HTTP fixtures; no backend, DB, model or broker",
    fontPolicy: "The remote font stylesheet receives an explicit empty fixture; the unchanged product system-font fallback is used",
    sourceOverride: process.env.MC037_SOURCE_DIR ? SOURCE_ROOT : null,
    sourceBefore: await sourceHashes(), runnerSha256: sha256(await readFile(TEST_FILE)),
    axeSha256: sha256(axeSource), browserChannel: process.env.MC037_BROWSER_CHANNEL ?? "chromium",
    observations: [], checks: [], cleanupErrors: [] };
  let server, browser, failure = null;
  if (OUT) await mkdir(OUT, { recursive: true });
  try {
    const originals = process.env.MC037_SOURCE_DIR
      ? new Map(await Promise.all(BANNERS.map(async (file) => [path.join(ROOT, file), await readFile(path.join(SOURCE_ROOT, file), "utf8")])))
      : new Map();
    const tokensPath = path.join(WEB, "src/styles/tokens.css");
    const overrideTokens = originals.size
      ? `${await readFile(tokensPath, "utf8")}\n${BANNERS.map((file) => `@source ${JSON.stringify(
        path.relative(path.dirname(tokensPath), path.join(SOURCE_ROOT, file)).split(path.sep).join("/"))};`).join("\n")}\n` : null;
    server = await createServer({ root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{ name: "mc037-frozen-banners", enforce: "pre",
        load(id) { if (originals.has(id)) return originals.get(id); if (overrideTokens !== null && id === tokensPath) return overrideTokens; } }],
    });
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address === "object");
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({ ...(process.env.MC037_BROWSER_CHANNEL ? { channel: process.env.MC037_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"] });
    receipt.browserVersion = browser.version();
    async function scenario(label, route, viewport, branch, count, size, run, options = {}) {
      await t.test(label, async () => {
        const observation = { label, route, viewport, branch, count, size, options, passed: false,
          preconditions: "Fresh browser context, real application entry and authorized synthetic local identity",
          expected: "Banners never intercept the overlapping assistant; counts match source data; ordinary pointer/keyboard and modal priority remain usable; original contrast diagnostics retain unverified results",
          geometry: [], measurements: [], actions: [], pageErrors: [], consoleErrors: [], responseErrors: [],
          fontFallbacks: [], blockedRequests: [], unexpectedProcedures: [] };
        receipt.observations.push(observation);
        const state = stateFor(branch, count, size); state.denied = options.denied ?? false;
        const context = await browser.newContext({ viewport, locale: "zh-CN", serviceWorkers: "block", reducedMotion: "reduce" });
        let page;
        try {
          await context.route("**/*", (request) => routeFixture(request, state, origin, observation));
          await context.addInitScript(({ productId, time }) => {
            localStorage.setItem(`workloom:${productId}:b-pc:access-token`, "MC037-explicit-local-http-fixture");
            localStorage.setItem("loommate.mini", "1");
            localStorage.setItem("wl-voice-gate", "captions");
            localStorage.setItem(`theater-ceremony-${new Date(time).toDateString()}`, "1");
          }, { productId: manifest.productId, time: FROZEN_TIME });
          page = await context.newPage();
          page.setDefaultTimeout(15000);
          page.setDefaultNavigationTimeout(45000);
          page.on("pageerror", (error) => observation.pageErrors.push(String(error)));
          page.on("console", (message) => { if (message.type() === "error") observation.consoleErrors.push(message.text()); });
          page.on("response", (response) => { if (response.status() >= 400) observation.responseErrors.push({ status: response.status(), url: response.url() }); });
          await page.clock.install({ time: new Date(FROZEN_TIME) });
          await page.goto(`${origin}${route}`);
          await expect(page.locator(".wl-page-host")).toHaveCount(1);
          await expect(avatar(page)).toBeVisible();
          if (options.denied) {
            await expect(page.getByText("当前身份不能访问此页面", { exact: true })).toBeVisible();
          } else if (route === "/agents") {
            await expect(page.getByRole("heading", { name: "团队成员", exact: true })).toBeVisible();
            await expect(page.locator(".wl-bridge-shell")).toHaveCount(1);
          } else if (route === "/") {
            await expect(page.locator('[data-product-ready="true"]')).toHaveCount(1);
          } else {
            await expect(page.getByRole("heading", { name: "成员管理", exact: true })).toBeVisible();
          }
          if (branch !== "none" && route !== "/members" && !options.denied) await expect(banner(page, branch)).toBeVisible();
          if (count > 0) await expect(badge(page, count)).toHaveCount(1);
          await page.evaluate(async () => { await document.fonts.ready; });
          observation.actualPath = new URL(page.url()).pathname;
          assert.equal(observation.actualPath, route, "The intended real route is mounted");
          await screenshot(page, observation, "initial");
          await run(page, state, observation);
          await noHorizontalOverflow(page, observation);
          assert.deepEqual(observation.pageErrors, []);
          assert.deepEqual(observation.consoleErrors, []);
          assert.deepEqual(observation.responseErrors, []);
          assert.deepEqual(observation.blockedRequests, []);
          assert.deepEqual(observation.unexpectedProcedures, []);
          observation.passed = true;
        } catch (error) {
          observation.error = String(error);
          if (page && !page.isClosed()) {
            await screenshot(page, observation, "failure");
            observation.failureDom = await page.locator("body").innerText();
          }
          throw error;
        } finally {
          observation.calls = state.calls;
          try { await context.close(); }
          catch (error) { observation.passed = false; observation.cleanupError = String(error); throw error; }
          finally { if (OUT) await writeFile(path.join(OUT, `${label}.json`), JSON.stringify(observation, null, 2) + "\n"); }
        }
      });
    }
    for (const route of ["/agents", "/"]) for (const viewport of VIEWPORTS) {
      const key = route === "/" ? "home" : "agents";
      await scenario(`${key}-${viewport.width}x${viewport.height}-actual-notifications`, route, viewport, "example", 10, "large",
        async (page, state, observation) => {
          await pointer(page, avatar(page), observation, "initial-mini", true);
          await measureBadge(page, 10, observation, "mini");
          await assistantPaths(page, state, observation);
          await measureBadge(page, 10, observation, "large");
          await pointer(page, page.getByTitle("切换大小", { exact: true }), observation, "enter-fullscreen");
          await expect(page.getByRole("dialog", { name: "小织屏保", exact: true })).toBeVisible();
          await expect(page.locator(`${SURFACE} [data-avatar-ready="true"]`)).toHaveCount(1);
          await measureBadge(page, 10, observation, "fullscreen");
          await pointer(page, page.getByRole("button", { name: "退出屏保（Esc）", exact: true }), observation, "exit-fullscreen");
          await expect.poll(() => state.settings.widget_size).toBe("small");
        });
    }
    for (const route of ["/agents", "/"]) for (const viewport of EDGE_VIEWPORTS) for (const branch of BRANCHES) {
      const key = route === "/" ? "home" : "agents";
      await scenario(`${key}-${viewport.width}x${viewport.height}-${branch}-overlap`, route, viewport, branch, 0, "small",
        forcedBannerOverlap);
    }
    for (const route of ["/agents", "/"]) for (const viewport of EDGE_VIEWPORTS) {
      const key = route === "/" ? "home" : "agents";
      await scenario(`${key}-${viewport.width}x${viewport.height}-both-banners`, route, viewport, "both", 10, "large",
        async (page, state, observation) => {
          await expect(banner(page, "skills-loaded")).toBeVisible();
          await pointer(page, avatar(page), observation, "two-banner-mini", true);
          await pointer(page, page.getByRole("button", { name: /^查看未读提醒/ }), observation, "open-notifications");
          const first = state.items[0];
          const card = page.locator("div:has(> .wl-action-row)").filter({ has: page.getByText(first.title, { exact: true }) });
          await pointer(page, card.getByRole("button", { name: "稍后", exact: true }), observation, "read-first-notification");
          await expect(page.getByText(first.title, { exact: true })).toHaveCount(0);
          await expect(badge(page, 9)).toHaveCount(1);
          await assistantPaths(page, state, observation);
        });
      await scenario(`${key}-${viewport.width}x${viewport.height}-failure-recovery`, route, viewport, "none", 0, "small",
        async (page, state, observation) => {
          assert.equal(await page.getByRole("link", { name: /定制我的行业版|查看落地向导|去审批|去技能中心/ }).count(), 0);
          state.branch = "status-error";
          await page.clock.fastForward(15000);
          await expect(banner(page, "status-error")).toBeVisible();
          await pointer(page, avatar(page), observation, "failure-mini", true);
          state.branch = "warning";
          await page.clock.fastForward(15000);
          await expect(banner(page, "warning")).toBeVisible();
          await expect(banner(page, "status-error")).toHaveCount(0);
          state.branch = "skills-loaded";
          await page.clock.fastForward(30000);
          await expect(banner(page, "skills-loaded")).toBeVisible();
          await pointer(page, page.getByRole("button", { name: "今日不再提示", exact: true }), observation, "dismiss-skill-banner");
          await expect(banner(page, "skills-loaded")).toHaveCount(0);
          await page.reload();
          await expect(avatar(page)).toBeVisible();
          await expect.poll(() => state.calls.filter((call) => call.name === "skills.skillOps.status").length).toBeGreaterThan(1);
          await expect(banner(page, "skills-loaded")).toHaveCount(0);
          await assistantPaths(page, state, observation);
        });
    }
    for (const viewport of EDGE_VIEWPORTS) {
      await scenario(`members-${viewport.width}x${viewport.height}-modal-priority`, "/members", viewport, "none", 0, "large", modalPaths);
      await scenario(`agents-${viewport.width}x${viewport.height}-permission-boundary`, "/agents", viewport, "none", 0, "small",
        async (page, state, observation) => {
          assert.equal(await page.getByRole("heading", { name: "团队成员", exact: true }).count(), 0);
          assert.equal(state.calls.filter((call) => call.name === "roster.list").length, 0);
          await assistantPaths(page, state, observation);
        }, { denied: true });
    }
    await t.test("The paired runner and relevant application sources remain unchanged", async () => {
      const check = { name: "source-stability", passed: false }; receipt.checks.push(check);
      assert.deepEqual(await sourceHashes(), receipt.sourceBefore);
      assert.equal(sha256(await readFile(TEST_FILE)), receipt.runnerSha256);
      check.passed = true;
    });
  } catch (error) { failure = String(error); throw error; }
  finally {
    const cleanup = await Promise.allSettled([browser?.close(), server?.close()]);
    receipt.cleanupErrors.push(...cleanup.filter((result) => result.status === "rejected").map((result) => String(result.reason)));
    receipt.sourceAfter = await sourceHashes();
    receipt.sourceStable = JSON.stringify(receipt.sourceBefore) === JSON.stringify(receipt.sourceAfter);
    receipt.runnerStable = sha256(await readFile(TEST_FILE)) === receipt.runnerSha256;
    receipt.finishedAt = new Date().toISOString(); receipt.error = failure;
    const contrast = receipt.observations.flatMap((observation) => observation.measurements);
    receipt.contrastSummary = Object.fromEntries(["passed", "failed", "unverified", "not-evaluated"]
      .map((status) => [status, contrast.filter((measurement) => measurement.contrastStatus === status).length]));
    receipt.contrastPassed = contrast.length > 0 && contrast.every((measurement) => measurement.contrastStatus === "passed");
    receipt.passed = !failure && receipt.observations.length === 40 && receipt.sourceStable && receipt.runnerStable
      && receipt.cleanupErrors.length === 0 && receipt.checks.length === 1 && receipt.checks.every((check) => check.passed)
      && receipt.observations.every((observation) => observation.passed);
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, [], "The isolated browser and Vite fixture close cleanly");
  }
});
