/**
 * MC-033: render real LevelBadge and P8 components with production styles.
 * P8 receives explicitly synthetic read-only API fixtures through the real client.
 * No mocked rectangles, duplicate component, backend, database or model call.
 * Run from the repository root with node --test. Chromium is required in CI;
 * MC033_BROWSER_CHANNEL=chrome selects installed Chrome for a local run.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const TEST_FILE = fileURLToPath(import.meta.url);
const ROOT = path.resolve(process.env.MC033_SOURCE_DIR ?? path.join(path.dirname(TEST_FILE), "../../../.."));
const WEB = path.join(ROOT, "governance/apps/web");
const OUT = process.env.MC033_EVIDENCE_DIR ? path.resolve(process.env.MC033_EVIDENCE_DIR) : null;
const requireWeb = createRequire(path.join(WEB, "package.json"));
const { chromium } = requireWeb("@playwright/test");
const { createServer } = await import(requireWeb.resolve("vite"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const SOURCE_PATHS = [
  "governance/apps/web/src/components/hud/gamification.tsx",
  "governance/apps/web/src/pages/p8/P8.tsx",
  "governance/apps/web/src/components/hud/index.ts",
  "governance/apps/web/src/components/hud/feedback.tsx",
  "governance/apps/web/src/components/hud/NightStatusPill.tsx",
  "governance/apps/web/src/components/hud/EmergencyBrake.tsx",
  "governance/apps/web/src/components/SimBanner.tsx",
  "governance/apps/web/src/components/SkillDistBanner.tsx",
  "governance/apps/web/src/shell/Bridge.tsx",
  "governance/apps/web/src/shell/NavigationAccess.tsx",
  "governance/apps/web/src/shell/NavMenu.tsx",
  "governance/apps/web/src/shell/PlanSwitcher.tsx",
  "governance/apps/web/src/lib/display.ts",
  "governance/apps/web/src/lib/naming.ts",
  "governance/apps/web/src/lib/product.ts",
  "governance/apps/web/src/lib/trpc.ts",
  "governance/apps/web/src/styles/tokens.css",
  "governance/apps/web/src/styles/subtitle.css",
  "governance/apps/web/vite.config.ts",
  "governance/apps/web/package.json",
  "governance/scripts/vite-product.mjs",
  "governance/scripts/product-runtime.mjs",
  "governance/apps/web/node_modules/@workloom/ui/package.json",
  "governance/apps/web/node_modules/@workloom/ui/dist/index.js",
  "governance/apps/web/node_modules/@workloom/ui/dist/language.js",
  "governance/apps/web/node_modules/@workloom/ui/dist/icons.js",
  "governance/apps/web/node_modules/@workloom/ui/dist/components.js",
  "governance/apps/web/node_modules/@workloom/ui/dist/tokens.css",
  "governance/apps/web/node_modules/@workloom/ui/dist/content-safety.css",
  "governance/apps/web/node_modules/@workloom/ui/dist/components.css",
];
const sourceHashes = async () => Object.fromEntries(await Promise.all(SOURCE_PATHS.map(async (file) => [
  file, sha256(await readFile(path.join(ROOT, file))),
])));
const CASES = [
  { id: "current", label: "当前员工版本", name: "全市场扫描员", level: 1, rank: "青铜", version: "6.3", expectedVersion: "第 6.3 版" },
  { id: "missing", label: "未提供版本", name: "只读成员", level: 2, rank: "白银", expectedVersion: null },
  { id: "empty", label: "空版本", name: "空版本成员", level: 3, rank: "铂金", version: "", expectedVersion: null },
  { id: "captain", label: "主理人", name: "王店长", level: 12, rank: "黄金", captain: true, expectedVersion: null },
  { id: "captain-version", label: "主理人不展示员工版本", name: "王店长", level: 12, rank: "黄金", captain: true, version: "6.3", expectedVersion: null },
  { id: "prefixed", label: "带前缀的合法版本", name: "前缀版本成员", level: 7, rank: "白银", version: "bundle/v12.34.5678", expectedVersion: "第 12.34.5678 版" },
  { id: "long", label: "较长合法版本", name: "长版本成员", level: 8, rank: "星钻", version: "release/v2026.10.20.12345678901234567890", expectedVersion: "第 2026.10.20.12345678901234567890 版" },
  { id: "unknown", label: "内部版本标识兜底", name: "未识别版本成员", level: 9, rank: "青铜", version: "bundle_internal_sha", expectedVersion: "版本已记录" },
];
const WIDTHS = [{ id: "profile", px: 280 }, { id: "dense", px: 160 }];
const FIXTURE_CASES = WIDTHS.flatMap((width) => CASES.map((row) => ({
  ...row, key: `${width.id}-${row.id}`, container: width.id, width: width.px,
})));
const fixtureId = path.join(WEB, "__mc033_level_badge_fixture__.tsx");
const profileFixtureId = path.join(WEB, "__mc033_profile_fixture__.tsx");
const PROFILE_PATH = "/agents/mc033-layout-fixture";
const SKILLS = [
  { id: "short-installed", name: "技能能力", version: "0.1.0", installed: true, fence_bindings: ["a", "b", "c", "d", "e", "f"],
    expectedName: "技能能力", expectedVersion: "第 0.1.0 版" },
  { id: "short-uninstalled", name: "市场扫描", version: "6.3", installed: false, fence_bindings: [],
    expectedName: "市场扫描", expectedVersion: "第 6.3 版" },
  { id: "long-name", name: "多市场交易机会扫描与风险约束交叉核验能力", version: "12.34.5678", installed: true, fence_bindings: Array.from({ length: 12 }, (_, i) => String(i)),
    expectedName: "多市场交易机会扫描与风险约束交叉核验能力", expectedVersion: "第 12.34.5678 版" },
  { id: "long-version", name: "风险审阅", version: "release/v2026.10.20.12345678901234567890", installed: false, fence_bindings: [],
    expectedName: "风险审阅", expectedVersion: "第 2026.10.20.12345678901234567890 版" },
  { id: "unknown", name: "internal_skill_identifier", version: "bundle_internal_sha", installed: true, fence_bindings: [],
    expectedName: "技能能力", expectedVersion: "版本已记录" },
  { id: "empty-version", name: "工作记录", version: "", installed: false, fence_bindings: [],
    expectedName: "工作记录", expectedVersion: "版本待确认" },
].map((skill) => ({ ...skill, level: "L1",
  expectedStatus: skill.installed ? `已装备 · ${skill.fence_bindings.length} 条关联安全规则` : "未安装" }));
const PROFILE = {
  agent: { id: "mc033-layout-fixture", presetKey: "mc033-layout-fixture", name: "全市场扫描员", version: "6.3", kind: "agent",
    readonly: true, status: "valid", invalidReason: null, description: "布局回归中的明示模拟员工资料。",
    nightShift: false, highRisk: false, tools: [], writeBack: [], constraints: [] },
  workspaceName: "布局回归工作区", bundle: "布局回归行业包",
  nightWindow: { open: false, range: "22:00–08:00" }, fences: [], skills: SKILLS,
  stats: { actions30: 0, adopted30: 0, rejected30: 0, adoptionRate: null, credits30: 0, offPeakRatio: null },
  game: { level: 1, rank: "青铜", xp: 0, xpFloor: 0, xpNext: 100 }, events: [],
};
const API_FIXTURES = {
  "access.me": {
    subject: { kind: "guest", id: "mc033-reader", name: "布局回归只读成员", role: "readonly" },
    scope: { tenantId: "mc033", workspaceId: "mc033" }, plan: "community", capabilities: {},
    partnerCapabilities: [], availableScopes: [], authority: { source: "server", evaluatedAt: "2026-10-20T00:00:00Z", grantIds: [] },
    navigationPermissions: [], actionPermissions: [], bundle: { configured: false, reason: "explicit-layout-fixture" },
  },
  "roster.profile": PROFILE,
  "roster.list": { nightWindow: PROFILE.nightWindow, humans: [], agents: [{ ...PROFILE.agent,
    fenceBindings: [], skills: SKILLS.map((skill) => skill.id), online: false, lastAction: null, lastActionAt: null,
    pendingApprovals: 0, blockedRecent: 0, stats: PROFILE.stats, game: PROFILE.game }] },
  "onboarding.status": {
    dataMode: "simulated", declaredDataMode: "simulated", defaultToolMode: "simulated", formalModeActive: false,
    llm: { provider: "mock", model: "layout-fixture", baseUrl: "", real: false },
    workspace: { name: "布局回归工作区", events: 0, members: 1, agents: 1, memories: 0 },
    bundle: { id: "mc033-fixture", isExample: false },
  },
  "skills.skillOps.status": { recentLoaded: [], pendingCount: 0 },
};
const fixtureSource = `
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { Button } from "@workloom/ui";
import { LevelBadge } from "./src/components/hud/gamification";
import "./src/styles/tokens.css";
import "./src/styles/subtitle.css";
import "@workloom/ui/tokens.css";
import "@workloom/ui/content-safety.css";
import "@workloom/ui/components.css";
const rows = ${JSON.stringify(FIXTURE_CASES)};
function Fixture() {
  const [clicks, setClicks] = useState({});
  return <main style={{padding:16}}>
    <h1>版本徽章布局回归 · 明示模拟组件数据</h1>
    <p>真实组件与产品样式；无数据库、后端或外部操作。</p>
    <div style={{display:"flex",flexWrap:"wrap",alignItems:"flex-start",gap:16}}>
      {rows.map(({key, label, container, width, expectedVersion, ...props}) =>
        <section key={key} data-mc033-case={key} style={{width,maxWidth:"100%"}}>
          <h2 className="text-body" style={{marginBottom:8}}>{label} · {width} 像素</h2>
          <article className="rounded-msg border border-line bg-card p-3.5">
            <div data-mc033-badge><LevelBadge {...props} /></div>
            <div style={{marginTop:12}}>
              <Button variant="quiet" data-mc033-action={key}
                onClick={() => setClicks(current => ({...current,[key]:(current[key] ?? 0)+1}))}>
                查看档案
              </Button>
            </div>
          </article>
          <output data-mc033-clicks={key} aria-live="polite">{clicks[key] ?? 0}</output>
        </section>
      )}
    </div>
  </main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
`;
const profileFixtureSource = `
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import P8 from "./src/pages/p8/P8";
import { NavigationAccessProvider } from "./src/shell/NavigationAccess";
import { setToken } from "./src/lib/trpc";
import "./src/styles/tokens.css";
import "./src/styles/subtitle.css";
import "@workloom/ui/tokens.css";
import "@workloom/ui/content-safety.css";
import "@workloom/ui/components.css";
setToken("mc033-synthetic-read-only-layout-fixture");
createRoot(document.getElementById("root")!).render(
  <BrowserRouter><NavigationAccessProvider>
    <p data-mc033-profile-notice className="p-3 text-body">真实档案组件与生产样式 · 明示模拟只读数据 · 无业务操作</p>
    <Routes>
      <Route path="/agents/:agentId" element={<P8 />} />
      <Route path="/skills" element={<p data-mc033-destination>技能中心导航已到达（仅验证真实入口的路由目标）</p>} />
    </Routes>
  </NavigationAccessProvider></BrowserRouter>
);
`;

function overlaps(a, b) {
  return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
}
function contains(outer, inner) {
  return inner.left >= outer.left - 1 && inner.right <= outer.right + 1
    && inner.top >= outer.top - 1 && inner.bottom <= outer.bottom + 1;
}

async function applyTextScale(page, scale) {
  // Wait for the observed glyph size, not just the root style assignment.
  await page.evaluate((value) => {
    document.documentElement.style.fontSize = value === 100 ? "" : `${value}%`;
  }, scale);
  await page.waitForFunction((value) => {
    const badge = document.querySelector('[data-mc033-case="profile-current"] [data-mc033-badge]')
      ?? document.querySelector(".wl-bridge-grid main");
    const level = [...(badge?.querySelectorAll("*") ?? [])]
      .find((node) => node.children.length === 0 && node.textContent.trim() === "等级 1");
    return level && Math.abs(parseFloat(getComputedStyle(level).fontSize) - 14 * value / 100) < 0.5;
  }, scale);
  await page.evaluate(async () => { await document.fonts.ready; });
}

async function measure(page) {
  return page.evaluate((cases) => {
    const rect = (node) => {
      const { left, right, top, bottom, width, height } = node.getBoundingClientRect();
      return { left, right, top, bottom, width, height };
    };
    const rows = cases.map((row) => {
      const section = document.querySelector(`[data-mc033-case="${row.key}"]`);
      const card = section.querySelector("article");
      const badge = section.querySelector("[data-mc033-badge]").firstElementChild;
      const leaves = [...badge.querySelectorAll("*")].filter((node) => node.children.length === 0);
      const exact = (text) => leaves.find((node) => node.textContent.trim() === text);
      const avatar = exact(row.name.slice(0, 1));
      const level = exact(`等级 ${row.level}`);
      const rank = exact(row.rank);
      const version = row.expectedVersion ? exact(row.expectedVersion) : null;
      if (!avatar || !level || !rank || (row.expectedVersion && !version)) {
        throw new Error(`Missing real LevelBadge content: ${row.key}`);
      }
      const textRects = [];
      if (version) {
        const range = document.createRange();
        range.selectNodeContents(version);
        for (const box of range.getClientRects()) {
          textRects.push({ left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height });
        }
      }
      const versionStyle = version ? getComputedStyle(version) : null;
      const avatarStyle = getComputedStyle(avatar);
      return {
        key: row.key,
        badgeText: badge.innerText,
        rects: { card: rect(card), badge: rect(badge), avatar: rect(avatar), level: rect(level), rank: rect(rank),
          version: version ? rect(version) : null, button: rect(section.querySelector("button")) },
        versionText: version?.textContent ?? null,
        versionLines: new Set(textRects.map((box) => Math.round(box.top))).size,
        textRects,
        versionVisible: versionStyle ? versionStyle.display !== "none" && versionStyle.visibility === "visible"
          && Number(versionStyle.opacity) > 0 && textRects.length > 0 : null,
        avatarRadius: parseFloat(avatarStyle.borderTopLeftRadius),
        avatarBorderColor: avatarStyle.borderTopColor,
        levelFontSize: parseFloat(getComputedStyle(level).fontSize),
        badgeTabStops: [...badge.querySelectorAll("a[href],button,input,select,textarea,[tabindex]")].length,
        cardScrollWidth: card.scrollWidth, cardClientWidth: card.clientWidth,
      };
    });
    const rootStyle = getComputedStyle(document.documentElement);
    return { rows, document: { width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      fontSize: rootStyle.fontSize, inlineFontSize: document.documentElement.style.fontSize,
      textBody: rootStyle.getPropertyValue("--wl-text-body"), spacing: rootStyle.getPropertyValue("--spacing") } };
  }, FIXTURE_CASES);
}

async function measureProfile(page) {
  return page.evaluate((skills) => {
    const rect = (node) => {
      const { left, right, top, bottom, width, height } = node.getBoundingClientRect();
      return { left, right, top, bottom, width, height };
    };
    const textBoxes = (parent, text) => {
      const walker = document.createTreeWalker(parent, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const start = node.textContent.indexOf(text);
        if (start < 0) continue;
        const range = document.createRange();
        range.setStart(node, start);
        range.setEnd(node, start + text.length);
        const boxes = [...range.getClientRects()].map((box) => ({
          left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height,
        })).filter((box) => box.width > 0 && box.height > 0);
        const style = getComputedStyle(node.parentElement);
        return { text, boxes, fontSize: parseFloat(style.fontSize),
          lines: new Set(boxes.map((box) => Math.round(box.top))).size,
          visible: style.display !== "none" && style.visibility === "visible" && Number(style.opacity) > 0 };
      }
      throw new Error(`Real P8 text missing: ${text}`);
    };
    const buttons = [...document.querySelectorAll('button[title="前往技能中心"]')];
    if (buttons.length !== skills.length) throw new Error("Real P8 skill count changed");
    const rows = skills.map((skill, index) => {
      const button = buttons[index];
      return {
        id: skill.id, text: button.innerText, button: rect(button), card: rect(button.parentElement.parentElement),
        icon: rect(button.querySelector("svg")), name: textBoxes(button, skill.expectedName),
        version: textBoxes(button, skill.expectedVersion), status: textBoxes(button, skill.expectedStatus),
        scrollWidth: button.scrollWidth, clientWidth: button.clientWidth,
        nestedActions: button.querySelectorAll("a[href],button,input,select,textarea,[tabindex]").length,
      };
    });
    const main = document.querySelector(".wl-bridge-grid main");
    const level = [...main.querySelectorAll("div")].find((node) => node.children.length === 0 && node.textContent === "等级 1");
    const badge = level.parentElement.parentElement;
    const avatar = [...badge.querySelectorAll("div")].find((node) => node.children.length === 0 && node.textContent === "全");
    const version = textBoxes(badge, "第 6.3 版");
    return { rows, profileBadge: { badge: rect(badge), avatar: rect(avatar), level: rect(level), version },
      document: { width: innerWidth, scrollWidth: document.documentElement.scrollWidth },
      main: { width: main.clientWidth, scrollWidth: main.scrollWidth },
      dispatchVisible: [...document.querySelectorAll("button")].some((button) => button.textContent.includes("发消息并派遣任务")) };
  }, SKILLS);
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

test("MC-033 LevelBadge and P8 skills use readable, non-overlapping browser layout", { timeout: 240_000 }, async (t) => {
  const startedAt = new Date().toISOString();
  const before = await sourceHashes();
  const receipt = { card: "MC-033", environment: "local-preview", startedAt,
    sourceBefore: before, runnerSha256: sha256(await readFile(TEST_FILE)),
    fixture: "Real LevelBadge, P8, Bridge, router, client and production CSS; synthetic props/read-only API data; no backend",
    browserChannel: process.env.MC033_BROWSER_CHANNEL ?? "chromium",
    observations: [], pageErrors: [], consoleErrors: [], responseErrors: [], blockedRequests: [], fixtureQueries: [],
    cleanupErrors: [], integrityPassed: false };
  let server;
  let browser;
  let failure = null;
  if (OUT) await mkdir(OUT, { recursive: true });
  try {
    server = await createServer({
      root: WEB, configFile: path.join(WEB, "vite.config.ts"), logLevel: "warn",
      server: { host: "127.0.0.1", port: 0, strictPort: true, hmr: false },
      plugins: [{
        name: "mc033-level-badge-browser-fixture", enforce: "pre",
        resolveId(id) {
          if (id === "/__mc033_level_badge_fixture__.tsx") return fixtureId;
          if (id === "/__mc033_profile_fixture__.tsx") return profileFixtureId;
        },
        load(id) {
          if (id === fixtureId) return fixtureSource;
          if (id === profileFixtureId) return profileFixtureSource;
        },
        configureServer(vite) {
          vite.middlewares.use(async (req, res, next) => {
            const isProfile = req.url === PROFILE_PATH || req.url === "/skills";
            if (req.url !== "/__mc033__" && !isProfile) return next();
            try {
              const html = await vite.transformIndexHtml(req.url, `<!doctype html>
<html lang="zh-CN" data-wl-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title data-workloom-product-title>组件布局回归</title>
</head><body><div id="root"></div><script type="module" src="${isProfile ? "/__mc033_profile_fixture__.tsx" : "/__mc033_level_badge_fixture__.tsx"}"></script></body></html>`);
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
    assert.ok(address && typeof address === "object", "Vite must bind a real local listener");
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await chromium.launch({
      ...(process.env.MC033_BROWSER_CHANNEL ? { channel: process.env.MC033_BROWSER_CHANNEL } : {}),
      headless: true, args: ["--disable-background-networking"],
    });
    receipt.browserVersion = browser.version();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      for (const textScale of [100, 200]) {
        await t.test(`${viewport.width}px at ${textScale}% text: all versions and both container widths`, async () => {
          const context = await browser.newContext({ viewport, serviceWorkers: "block", reducedMotion: "reduce" });
          const label = `${viewport.width}-${textScale}`;
          const observation = { label, viewport, textScale, rows: [], failures: [], interactions: [] };
          receipt.observations.push(observation);
          try {
            await context.route("**/*", async (route) => {
              const url = new URL(route.request().url());
              if (url.origin !== origin || /^\/(?:trpc|api|health)(?:\/|$)/.test(url.pathname)) {
                receipt.blockedRequests.push({ label, url: url.href });
                await route.abort("blockedbyclient");
              } else await route.continue();
            });
            const page = await context.newPage();
            page.setDefaultTimeout(20_000);
            page.on("pageerror", (error) => receipt.pageErrors.push({ label, error: String(error) }));
            page.on("console", (message) => {
              if (message.type() === "error") receipt.consoleErrors.push({ label, error: message.text() });
            });
            page.on("response", (response) => {
              if (response.status() >= 400) receipt.responseErrors.push({ label, status: response.status(), url: response.url() });
            });
            await page.goto(`${origin}/__mc033__`);
            await page.locator("[data-mc033-case]").last().waitFor();
            await applyTextScale(page, textScale);
            const measurement = await measure(page);
            observation.rows = measurement.rows;
            observation.document = measurement.document;
            const check = (ok, message) => { if (!ok) observation.failures.push(message); };
            check(measurement.document.scrollWidth <= viewport.width + 1, "Document has horizontal overflow");
            for (const row of FIXTURE_CASES) {
              const found = measurement.rows.find((item) => item.key === row.key);
              const r = found.rects;
              const prefix = `${row.key}: `;
              check(Math.abs(found.levelFontSize - 14 * textScale / 100) < 0.5, prefix + "Real product text scale was not applied");
              check(Math.abs(r.avatar.width - 40 * textScale / 100) < 0.5
                && Math.abs(r.avatar.height - 40 * textScale / 100) < 0.5, prefix + "Avatar lost its intended square size");
              check(contains(r.card, r.badge), prefix + "Badge escaped its card");
              for (const key of ["avatar", "level", "rank"]) {
                check(contains(r.badge, r[key]), prefix + key + " escaped the badge");
              }
              check(!overlaps(r.avatar, r.level) && !overlaps(r.avatar, r.rank), prefix + "Avatar overlaps level or rank");
              check(!overlaps(r.level, r.rank), prefix + "Level overlaps rank");
              check(!overlaps(r.badge, r.button), prefix + "Badge overlaps adjacent action");
              check(found.cardScrollWidth <= found.cardClientWidth + 1, prefix + "Card has horizontal overflow");
              check(found.badgeTabStops === 0, prefix + "Display-only badge acquired a tab stop");
              check(found.badgeText.includes(`等级 ${row.level}`) && found.badgeText.includes(row.rank), prefix + "Level or rank changed");
              if (row.captain) check(found.avatarRadius >= r.avatar.width / 2, prefix + "Captain avatar is not circular");
              else check(found.avatarRadius < r.avatar.width / 2, prefix + "Crew avatar lost its square shape");
              if (row.expectedVersion) {
                check(found.versionText === row.expectedVersion, prefix + "versionText output changed");
                check(found.versionVisible, prefix + "Version is not visibly rendered");
                check(contains(r.badge, r.version) && contains(r.card, r.version), prefix + "Version escaped layout bounds");
                for (const key of ["avatar", "level", "rank", "button"]) {
                  check(!overlaps(r.version, r[key]), prefix + "Version overlaps " + key);
                }
                check(found.textRects.every((line) => contains(r.version, line) && contains(r.card, line)), prefix + "Version glyphs are clipped or outside the card");
                if (row.id === "current" && textScale === 100) {
                  check(found.versionLines === 1, prefix + "Current version should fit on one line");
                }
              } else check(!found.badgeText.includes("版"), prefix + "Version appeared for captain or missing/empty version");
            }
            if (OUT) await page.screenshot({ path: path.join(OUT, `${label}.png`), fullPage: true, animations: "disabled" });
            for (const row of FIXTURE_CASES) {
              const button = page.locator(`[data-mc033-action="${row.key}"]`);
              await button.click();
              assert.equal(await page.locator(`[data-mc033-clicks="${row.key}"]`).textContent(), "1");
            }
            // Start before the first card, then use only sequential keyboard navigation.
            await page.evaluate(() => {
              if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
              window.getSelection()?.removeAllRanges();
              window.scrollTo(0, 0);
            });
            await page.goto(`${origin}/__mc033__`);
            await page.locator("[data-mc033-case]").last().waitFor();
            await applyTextScale(page, textScale);
            for (const row of FIXTURE_CASES) {
              await page.keyboard.press("Tab");
              assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("data-mc033-action")), row.key,
                "Sequential Tab must reach the next real action, never the decorative badge");
              await page.keyboard.press("Enter");
              await page.keyboard.press("Space");
              assert.equal(await page.locator(`[data-mc033-clicks="${row.key}"]`).textContent(), "2");
              observation.interactions.push({ key: row.key, pointer: true, tab: true, enter: true, space: true });
            }
            assert.equal(observation.failures.length, 0, observation.failures.join("\n"));
          } catch (error) {
            observation.error = String(error);
            throw error;
          } finally {
            await closeAndRecord(context, observation, receipt);
          }
        });
      }
    }
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      for (const textScale of [100, 200]) {
        await t.test(`Real P8 at ${viewport.width}px and ${textScale}% text: skill content and navigation`, async () => {
          const context = await browser.newContext({ viewport, serviceWorkers: "block", reducedMotion: "reduce" });
          const label = `p8-${viewport.width}-${textScale}`;
          const observation = { label, viewport, textScale, rows: [], failures: [], interactions: [] };
          receipt.observations.push(observation);
          try {
            await context.route("**/*", async (route) => {
              const request = route.request();
              const url = new URL(request.url());
              if (url.origin !== origin || /^\/(?:api|health)(?:\/|$)/.test(url.pathname)) {
                receipt.blockedRequests.push({ label, url: url.href });
                return route.abort("blockedbyclient");
              }
              if (url.pathname.startsWith("/trpc/")) {
                const operations = decodeURIComponent(url.pathname.slice("/trpc/".length)).split(",");
                const inputs = JSON.parse(url.searchParams.get("input") ?? "{}");
                if (request.method() !== "GET" || !operations.every((operation, index) => (
                  Object.hasOwn(API_FIXTURES, operation)
                  && (operation !== "roster.profile" || inputs[index]?.agentId === PROFILE.agent.id)
                ))) {
                  receipt.blockedRequests.push({ label, operations, method: request.method() });
                  return route.abort("blockedbyclient");
                }
                receipt.fixtureQueries.push({ label, operations, source: "explicit-synthetic-read-only-api-fixture" });
                const results = operations.map((operation) => ({ result: { data: API_FIXTURES[operation] } }));
                return route.fulfill({ status: 200, contentType: "application/json",
                  body: JSON.stringify(url.searchParams.get("batch") === "1" ? results : results[0]) });
              }
              if (url.pathname === "/trpc") {
                receipt.blockedRequests.push({ label, url: url.href });
                return route.abort("blockedbyclient");
              }
              await route.continue();
            });
            const page = await context.newPage();
            page.setDefaultTimeout(20_000);
            page.on("pageerror", (error) => receipt.pageErrors.push({ label, error: String(error) }));
            page.on("console", (message) => {
              if (message.type() === "error") receipt.consoleErrors.push({ label, error: message.text() });
            });
            page.on("response", (response) => {
              if (response.status() >= 400) receipt.responseErrors.push({ label, status: response.status(), url: response.url() });
            });
            const ready = async () => {
              await page.locator('button[title="前往技能中心"]').last().waitFor();
              assert.equal(await page.locator('button[title="前往技能中心"]').count(), SKILLS.length);
              await applyTextScale(page, textScale);
            };
            await page.goto(`${origin}${PROFILE_PATH}`);
            await ready();
            const measurement = await measureProfile(page);
            Object.assign(observation, measurement);
            const check = (ok, message) => { if (!ok) observation.failures.push(message); };
            check(measurement.document.scrollWidth <= viewport.width + 1, "P8 document has horizontal overflow");
            check(measurement.main.scrollWidth <= measurement.main.width + 1, "P8 content is clipped horizontally");
            check(!measurement.dispatchVisible, "Read-only P8 fixture exposed a dispatch action");
            const badge = measurement.profileBadge;
            check(contains(badge.badge, badge.avatar), "P8 avatar escaped its real badge");
            check(!overlaps(badge.avatar, badge.level), "P8 avatar overlaps level");
            for (const line of badge.version.boxes) {
              check(contains(badge.badge, line), "P8 version escaped its real badge");
              check(!overlaps(line, badge.avatar) && !overlaps(line, badge.level), "P8 version overlaps avatar or level");
            }
            for (const row of measurement.rows) {
              const expected = SKILLS.find((skill) => skill.id === row.id);
              const prefix = `${row.id}: `;
              check(contains(row.card, row.button), prefix + "Skill button escaped the card");
              check(row.scrollWidth <= row.clientWidth + 1, prefix + "Skill button has horizontal overflow");
              check(row.nestedActions === 0, prefix + "Skill button acquired nested actions");
              for (const key of ["name", "version", "status"]) {
                const text = row[key];
                check(text.visible && text.boxes.length > 0, prefix + key + " is not visibly rendered");
                check(Math.abs(text.fontSize - 14 * textScale / 100) < 0.5, prefix + key + " did not use the product text scale");
                check(text.boxes.every((line) => contains(row.button, line) && contains(row.card, line)), prefix + key + " glyphs are clipped or outside the card");
                check(text.boxes.every((line) => !overlaps(row.icon, line)), prefix + key + " overlaps the skill icon");
              }
              check(Math.max(...row.name.boxes.map((box) => box.width)) >= 3 * row.name.fontSize - 1,
                prefix + "Skill name is compressed into a vertical column");
              check(Math.max(...row.version.boxes.map((box) => box.width)) >= 3 * row.version.fontSize - 1,
                prefix + "Skill version is compressed into a vertical column");
              for (const [first, second] of [["name", "version"], ["name", "status"], ["version", "status"]]) {
                check(row[first].boxes.every((a) => row[second].boxes.every((b) => !overlaps(a, b))),
                  prefix + first + " overlaps " + second);
              }
              if (textScale === 100 && (expected.id === "short-installed" || expected.id === "short-uninstalled")) {
                check(row.name.lines === 1 && row.version.lines === 1, prefix + "Short skill name and version should each fit on one line");
              }
              check(row.text.includes(expected.expectedName) && row.text.includes(expected.expectedVersion)
                && row.text.includes(expected.expectedStatus), prefix + "Skill content or installed status changed");
            }
            if (OUT) await page.screenshot({ path: path.join(OUT, `${label}.png`), fullPage: true, animations: "disabled" });
            for (let index = 0; index < SKILLS.length; index += 1) {
              await page.locator('button[title="前往技能中心"]').nth(index).click();
              await page.locator("[data-mc033-destination]").waitFor();
              assert.equal(new URL(page.url()).pathname, "/skills", "Real skill click must reach the skills route");
              await page.goBack();
              await ready();
              observation.interactions.push({ id: SKILLS[index].id, pointer: true, tab: false, enter: false, space: false });
            }
            for (const key of ["Enter", "Space"]) {
              await page.goto(`${origin}${PROFILE_PATH}`);
              await ready();
              for (let index = 0; index < SKILLS.length; index += 1) {
                let reached = false;
                for (let step = 0; step < 80; step += 1) {
                  await page.keyboard.press("Tab");
                  reached = await page.evaluate((wanted) => (
                    [...document.querySelectorAll('button[title="前往技能中心"]')][wanted] === document.activeElement
                  ), index);
                  if (reached) break;
                }
                assert.ok(reached, `Sequential Tab did not reach real P8 skill ${SKILLS[index].id}`);
                await page.keyboard.press(key);
                await page.locator("[data-mc033-destination]").waitFor();
                assert.equal(new URL(page.url()).pathname, "/skills", "Keyboard activation must reach the skills route");
                observation.interactions[index].tab = true;
                observation.interactions[index][key.toLowerCase()] = true;
                await page.goBack();
                await ready();
              }
            }
            assert.equal(observation.failures.length, 0, observation.failures.join("\n"));
          } catch (error) {
            observation.error = String(error);
            throw error;
          } finally {
            await closeAndRecord(context, observation, receipt);
          }
        });
      }
    }
    await t.test("No browser error, unexpected network call or source mutation", async () => {
      assert.deepEqual(receipt.pageErrors, []);
      assert.deepEqual(receipt.consoleErrors, []);
      assert.deepEqual(receipt.responseErrors, []);
      assert.deepEqual(receipt.blockedRequests, []);
      for (const operation of Object.keys(API_FIXTURES)) {
        assert.ok(receipt.fixtureQueries.some((query) => query.operations.includes(operation)),
          `Real P8 client did not request the expected fixture: ${operation}`);
      }
      assert.deepEqual(await sourceHashes(), before);
      receipt.integrityPassed = true;
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
    receipt.passed = !failure && receipt.integrityPassed && receipt.observations.length === 8
      && receipt.observations.every((observation) => !observation.error && observation.failures.length === 0)
      && [receipt.pageErrors, receipt.consoleErrors, receipt.responseErrors, receipt.blockedRequests, receipt.cleanupErrors].every((errors) => errors.length === 0)
      && receipt.sourceStable;
    if (OUT) await writeFile(path.join(OUT, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
    assert.deepEqual(receipt.cleanupErrors, [], "Browser and fixture server must close cleanly");
  }
});
