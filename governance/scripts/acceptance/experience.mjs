#!/usr/bin/env node
/**
 * experience.mjs · L4 交互层 + L5 体验层 真机走查（RDAS v1）
 *
 * 角色 × 旅程 × 维度：每条走查都留 前置条件/步骤/预期/实测/证据（截图 + JSON），
 * 维度机检：术语一致性（裸动作码/裸 id/裸 ISO）、对比度（WCAG AA 近似）、打扰预算。
 *
 * 用法：pnpm acceptance:experience [--out <dir>] [--profile <path>] [--only EXP-02,EXP-09]
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";
import { cliArgs, findRepoRoot, loadProfile, urlsOf } from "./lib/profile.mjs";
import { loadChromium } from "./lib/playwright.mjs";
import { loginAsMember, productIdOf } from "./lib/session.mjs";
import { canonicalJson, dailyDecisionEvidence, evaluateJourney, pageStateEvidence, rpcExchange, visibleIdentifier, workspaceIdentityEvidence } from "./lib/ui-evidence.mjs";

const args = cliArgs();
const REPO_ROOT = findRepoRoot();
const { profile, warnings: profileWarnings } = loadProfile(REPO_ROOT, args.profilePath);
const OUT_DIR = resolve(args.outDir ?? join(REPO_ROOT, "outputs", "acceptance", "experience"));
const SHOT_DIR = join(OUT_DIR, "shots");
mkdirSync(SHOT_DIR, { recursive: true });

const chromium = loadChromium(REPO_ROOT);

function readEnvValue(key) {
  const path = join(REPO_ROOT, ".env");
  if (!existsSync(path)) return undefined;
  const line = readFileSync(path, "utf-8").split("\n").find((l) => l.startsWith(`${key}=`));
  return line ? line.slice(key.length + 1).trim() : undefined;
}
function productId() {
  try {
    return JSON.parse(readFileSync(join(REPO_ROOT, "product.manifest.json"), "utf-8")).productId ?? "workloom";
  } catch {
    return "workloom";
  }
}

const URLs = urlsOf(profile);
const R = profile.routes;
const T = profile.thresholds;
const WORKSPACE_SLUG = profile.identity?.workspaceSlug ?? null;
const MEMBER_NO = profile.identity?.human ?? "MEM-001";

const REPORT = { at: new Date().toISOString(), spec: "docs/REAL-DEVICE-ACCEPTANCE-SPEC.md@rdas/v1", profileWarnings, checks: [], dimensions: {}, issues: [] };
const only = (() => {
  const i = process.argv.indexOf("--only");
  return i >= 0 ? new Set(String(process.argv[i + 1]).split(",")) : null;
})();

const now = () => Date.now();
function record(entry) {
  if (only && !only.has(entry.id)) return;
  REPORT.checks.push(entry);
  if (!entry.pass) REPORT.issues.push({ id: entry.id, name: entry.name, actual: entry.actual });
  console.log(`${entry.pass ? "✓" : "✗"} [${entry.persona}·${entry.journey}] ${entry.id} ${entry.name}${entry.metric ? ` · ${entry.metric}` : ""}`);
}

const browser = await chromium.launch({ headless: true });
const pcContext = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
const page = await pcContext.newPage();
let consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().split("\n")[0]); });
page.on("pageerror", (e) => consoleErrors.push(String(e).split("\n")[0]));
const shot = async (target, name) => {
  const file = join(SHOT_DIR, `${name}.png`);
  await target.screenshot({ path: file });
  return file;
};

/* 登录（PC 成员态）：必须清除演示直登游客标记，否则审批等成员能力会被误判为无权 */
const TOKEN = await loginAsMember(page, { urls: URLs, workspaceSlug: WORKSPACE_SLUG, memberNo: MEMBER_NO, productId: productIdOf(REPO_ROOT) });

/** Expand the real context drawer; a failed click must fail the journey. */
async function ensureRightPanel(target, controlSelector = null) {
  const panelReady = async () => {
    const panels = [
      target.locator(".wl-bridge-right:visible").first(),
      target.getByRole("dialog", { name: "任务上下文", exact: true }).first(),
    ];
    for (const panel of panels) {
      if (await panel.count() === 0 || !await panel.isVisible()) continue;
      if (!controlSelector) return true;
      const control = panel.locator(controlSelector).first();
      if (await control.count() > 0 && await control.isVisible() && await control.isEnabled()) return true;
    }
    return false;
  };
  if (await panelReady()) return false;
  const toggle = target.locator('button[aria-label="显示任务上下文面板"]').first();
  if (await toggle.count() && await toggle.isVisible() && await toggle.isEnabled()) {
    await toggle.click({ timeout: 6000 });
    // In Bridge's compact layout aria-pressed describes the hidden grid
    // column. It correctly stays false when the separate drawer opens.
    if (!await waitUntil(target, panelReady, 6000)) throw new Error("上下文面板未在点击后显示所需内容");
    return true;
  }
  return false;
}

const normalized = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const compact = (value) => normalized(value).replace(/\s+/g, "");
const idShown = visibleIdentifier;
const digest = (value) => value == null ? null : createHash("sha256").update(canonicalJson(value)).digest("hex");

async function screen(target) {
  const snapshot = await target.evaluate(() => {
    const roots = [...document.querySelectorAll("main")];
    const root = roots.at(-1) ?? document.body;
    const text = root.innerText ?? "";
    const visible = (element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const outsideChat = (element) => !element.closest(".rounded-tl-sm,.rounded-br-sm");
    const visibleNodes = [document.body, ...document.querySelectorAll("body *")]
      .filter((element) => visible(element) && outsideChat(element) && !element.matches("script,style"));
    const stateNodes = visibleNodes.filter((element) => element.matches('.wl-state,[role="status"],[role="alert"]'));
    return {
      text,
      directTexts: visibleNodes.map((element) => [...element.childNodes]
        .filter((node) => node.nodeType === Node.TEXT_NODE).map((node) => node.textContent).join("").trim()).filter(Boolean),
      stateTexts: stateNodes.map((element) => element.innerText ?? ""),
      loadingState: stateNodes.some((element) => element.matches(".wl-state--loading")),
      errorState: stateNodes.some((element) => element.matches(".wl-state--error,.wl-state--forbidden")),
      ready: document.querySelector('[data-product-ready="true"]') !== null,
    };
  });
  return pageStateEvidence(snapshot);
}

async function waitUntil(target, predicate, timeout = 8000) {
  const end = now() + timeout;
  do {
    if (await predicate()) return true;
    await target.waitForTimeout(100);
  } while (now() < end);
  return false;
}

/** Observe browser requests, never substitute a direct mutation for a UI action.
 * Request headers/tokens are deliberately neither retained nor reported. */
function observe(target) {
  const responses = [];
  const requests = [];
  const pending = new Set();
  const requestTimes = new WeakMap();
  const inputOf = (request) => {
    try { return request.postDataJSON(); }
    catch { return null; }
  };
  const onRequest = (request) => {
    requestTimes.set(request, now());
    const url = new URL(request.url());
    if (request.method() !== "POST") return;
    const names = url.pathname.includes("/trpc/") ? decodeURIComponent(url.pathname.split("/trpc/")[1]).split(",") : url.pathname === "/c/chat" ? ["c.chat"] : [];
    for (const procedure of names) requests.push({ procedure, at: now() });
  };
  const onResponse = (response) => {
    const request = response.request();
    const url = new URL(request.url());
    const names = url.pathname.includes("/trpc/") ? decodeURIComponent(url.pathname.split("/trpc/")[1]).split(",") : url.pathname === "/c/chat" ? ["c.chat"] : [];
    if (names.length === 0) return;
    const task = (async () => {
      let json;
      try { json = await response.json(); }
      catch { json = null; }
      for (const procedure of names) {
        const exchange = procedure === "c.chat"
          ? { procedure, input: inputOf(request), data: json, status: response.status(), ok: response.ok() && typeof json?.answer === "string" && Boolean(json.answer.trim()) }
          : rpcExchange(request.url(), request.method(), inputOf(request), response.status(), json, procedure);
        if (exchange) responses.push({ ...exchange, method: request.method(), requestAt: requestTimes.get(request) ?? null, at: now() });
      }
    })();
    pending.add(task);
    void task.finally(() => pending.delete(task));
  };
  target.on("request", onRequest);
  target.on("response", onResponse);
  return {
    responses, requests,
    latest: (name, match = () => true) => responses.findLast((entry) => entry.procedure === name && match(entry)) ?? null,
    async wait(name, match = () => true, timeout = 8000) {
      await waitUntil(target, () => Boolean(this.latest(name, match)), timeout);
      return this.latest(name, match);
    },
    async stop() {
      target.off("request", onRequest); target.off("response", onResponse);
      await Promise.all(pending);
    },
  };
}

// These reads corroborate UI results; every mutating operation remains a real
// browser click. A failed read produces missing evidence, never an empty success.
async function readRpc(name, input) {
  const query = input === undefined ? "" : `?input=${encodeURIComponent(JSON.stringify(input))}`;
  try {
    const response = await page.request.get(`${URLs.api}/trpc/${name}${query}`, {
      headers: { authorization: `Bearer ${TOKEN}` }, timeout: 15000,
    });
    const json = await response.json();
    return rpcExchange(response.url(), "GET", null, response.status(), json, name);
  } catch (error) {
    return { procedure: name, ok: false, data: null, error: error?.name ?? "Error" };
  }
}

function checked(entry, actual) {
  const verdict = evaluateJourney(entry.id, actual, T);
  return { ...entry, actual: { ...actual, evidenceChecks: verdict.checks, missingEvidence: verdict.missing, failedEvidence: verdict.failed }, status: verdict.status, pass: verdict.pass };
}

async function firstValue() {
  const watcher = observe(page);
  try {
    const t0 = now();
    await page.goto(`${URLs.pc}${R.home}`, { waitUntil: "domcontentloaded" });
    const skip = page.getByRole("button", { name: /跳过开场|跳过/ }).first();
    await waitUntil(page, async () => (await skip.count()) > 0 || (await screen(page)).ready, T.firstValueMs);
    const ceremony = await skip.count() > 0 && await skip.isVisible();
    let skipSucceeded = !ceremony;
    if (ceremony) {
      await skip.click({ timeout: 6000 });
      skipSucceeded = await waitUntil(page, async () => await skip.count() === 0 || !await skip.isVisible(), 6000);
    }
    let valueVisible = false;
    let dom;
    await waitUntil(page, async () => {
      dom = await screen(page);
      const theater = watcher.latest("captain.theater");
      const queue = watcher.latest("captain.chairmanQueue");
      const briefing = theater?.ok ? theater.data?.latestBriefing?.text : null;
      const pending = queue?.ok && Array.isArray(queue.data) ? queue.data : [];
      valueVisible = Boolean(dom.healthy && (briefing && compact(dom.text).includes(compact(briefing))
        || pending.some((row) => row.snapshot?.title && dom.text.includes(row.snapshot.title))));
      return valueVisible;
    }, Math.max(1, T.firstValueMs - (now() - t0)));
    const firstValueMs = valueVisible ? now() - t0 : null;
    const approvals = await readRpc("approvals.list");
    const daily = dailyDecisionEvidence(approvals?.ok ? approvals.data : null);
    return checked({
      id: "EXP-01", persona: "owner", journey: "首启（0–10 分钟）", dimension: "首启与首价值 / 掌控感",
      name: "打开工作台即看到今日经营与待拍板事项",
      precondition: "成员态登录；首页有经营事实；审批来源可回读",
      steps: ["打开首页", "如有欢迎仪式则真实跳过", "核对主内容与当日唯一决策对象"],
      expected: `首启仪式可跳过；T+${T.firstValueMs / 1000}s 内见价值；决策包 ≤${T.decisionQuota} 件/日`,
      evidence: await shot(page, "exp-01-first-value"),
    }, { healthy: dom?.healthy, ceremony, skipSucceeded, valueVisible, firstValueMs, decisionCount: daily.count, dailyCountComplete: daily.complete, decisionWindow: daily });
  } finally { await watcher.stop(); }
}

async function approval() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.approvals}`, { waitUntil: "domcontentloaded" });
    const initial = await watcher.wait("approvals.list");
    const pending = initial?.ok && Array.isArray(initial.data) ? initial.data.filter((row) => row.status === "pending") : [];
    const selected = pending[0];
    let openedPanel = false;
    if (selected) {
      const candidates = page.getByRole("button").filter({ hasText: /必审|逐步审|高风险/ });
      for (let index = 0; index < await candidates.count(); index += 1) {
        const candidate = candidates.nth(index);
        if (idShown(await candidate.innerText(), selected.approval_id) && await candidate.isVisible()) {
          await candidate.click({ timeout: 8000 });
          break;
        }
      }
    }
    if (await page.getByRole("button", { name: /^推进/ }).count() === 0) openedPanel = await ensureRightPanel(page);
    const gestures = {};
    for (const [key, name] of [["advance", /^推进/], ["calibrate", /^校准/], ["brake", /^制动/]]) {
      const button = page.getByRole("button", { name }).first();
      gestures[key] = await button.count() > 0 && await button.isVisible() && await button.isEnabled();
    }
    const t0 = now();
    let clicked = false;
    let exchange = null;
    if (selected && gestures.advance) {
      await page.getByRole("button", { name: /^推进/ }).first().click({ timeout: 8000 });
      clicked = true;
      exchange = await watcher.wait("approvals.decide", (row) => row.requestAt >= t0 && row.method === "POST" && row.input?.approvalId === selected.approval_id && row.input?.gesture === "approve", T.approvalMs);
    }
    const result = exchange?.ok ? exchange.data : null;
    let receiptVisible = false;
    if (result?.gestureEventId) {
      receiptVisible = await waitUntil(page, async () => {
        const dom = await screen(page);
        return dom.healthy && /已写入事件账本/.test(dom.text) && idShown(dom.text, result.gestureEventId);
      }, Math.max(1, T.approvalMs - (now() - t0)));
    }
    const elapsedMs = now() - t0;
    const readback = await readRpc("approvals.list");
    const current = readback?.ok && Array.isArray(readback.data) ? readback.data.find((row) => row.approval_id === selected?.approval_id) : null;
    return checked({
      id: "EXP-02", persona: "owner", journey: "日常治理（每天 10 分钟）", dimension: "效率与恢复 / 信任与透明",
      name: "审批三手势可用且单件裁决达标",
      precondition: "存在可处理的合成待审项；成员具备审批权限",
      steps: ["打开并选中待审对象", "真实点按推进", "核对同一审批回执、账本编号与服务端回读"],
      expected: `三手势齐全；单件 ≤${T.approvalMs / 1000}s；写回有账本编号`,
      evidence: await shot(page, "exp-02-approval"),
    }, { healthy: (await screen(page)).healthy, openedPanel, pendingItems: pending.length, selectedApprovalId: selected?.approval_id, gestures, clicked, requestApprovalId: exchange?.input?.approvalId, receiptApprovalId: result?.approvalId, gestureEventId: result?.gestureEventId, approvalStatus: result?.status, receiptVisible, readbackStatus: current?.status, elapsedMs });
  } finally { await watcher.stop(); }
}

async function traceability() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.events}`, { waitUntil: "domcontentloaded" });
    await watcher.wait("threads.events");
    await waitUntil(page, async () => await page.locator("main article").count() > 0);
    const card = page.locator("main article").first();
    const cardText = await card.count() ? await card.innerText() : "";
    const link = card.getByRole("button").first();
    const title = await link.count() ? await link.getAttribute("title") ?? await link.innerText() : null;
    const list = watcher.latest("threads.list");
    const matching = list?.ok && Array.isArray(list.data) ? list.data.filter((row) => row.title === title) : [];
    const selectedThread = matching.length === 1 ? matching[0] : null;
    const original = watcher.latest("threads.events", (row) => row.input?.threadId === selectedThread?.id);
    const matchingEvents = original?.ok && Array.isArray(original.data) ? original.data.filter((event) => idShown(cardText, event.event_id)) : [];
    const selectedEvent = matchingEvents.length === 1 ? matchingEvents[0] : null;
    let clicked = false;
    let clicks = 0;
    let navigationAt = Infinity;
    if (selectedThread && selectedEvent && await link.isVisible()) {
      navigationAt = now();
      await link.click({ timeout: 8000 });
      clicks += 1; clicked = true;
      await watcher.wait("threads.get", (row) => row.requestAt >= navigationAt && row.input?.threadId === selectedThread.id);
    }
    const thread = watcher.latest("threads.get", (row) => row.requestAt >= navigationAt && row.input?.threadId === selectedThread?.id);
    // Thread and event requests may finish in either order. Both must have
    // started after this click; the originating card's response is not proof.
    const chain = clicked ? await watcher.wait("threads.events", (row) => row.requestAt >= navigationAt && row.input?.threadId === selectedThread?.id) : null;
    const routeMatches = selectedThread && new URL(page.url()).pathname === `${R.tasks}/${encodeURIComponent(selectedThread.id)}`;
    const dom = await screen(page);
    const destinationHasEvent = Boolean(routeMatches && thread?.ok && chain?.ok && Array.isArray(chain.data)
      && chain.data.some((event) => event.event_id === selectedEvent?.event_id) && idShown(dom.text, selectedEvent?.event_id));
    return checked({
      id: "EXP-03", persona: "owner", journey: "日常治理", dimension: "信任与透明",
      name: "任一结论可一键溯源到事件/回执",
      precondition: "账本有可唯一绑定到任务的历史事件",
      steps: ["读取一张真实事件卡", "点击该卡的关联任务", "在同一任务链核对原事件"],
      expected: `证据同屏可见；≤${T.traceClicks} 次点击进入链路`,
      evidence: await shot(page, "exp-03-traceability"),
    }, { healthy: dom.healthy, eventId: selectedEvent?.event_id, threadId: selectedThread?.id, cardActor: /·/.test(cardText), cardEventVisible: Boolean(selectedEvent && /事件/.test(cardText)), clicked, clicks, destinationThreadId: routeMatches && thread?.ok ? thread.data?.id : null, destinationHasEvent });
  } finally { await watcher.stop(); }
}

async function brake() {
  const watcher = observe(page);
  try {
    let entry = null;
    let where = null;
    for (const [name, route] of [["reports", R.reports], ["night", "/night"], ["approvals", R.approvals]]) {
      await page.goto(`${URLs.pc}${route}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(1200);
      const candidates = page.getByRole("button", { name: /紧急制动/ });
      for (let index = 0; index < await candidates.count(); index += 1) {
        const candidate = candidates.nth(index);
        if (await candidate.isVisible() && await candidate.isEnabled()) { entry = candidate; where = name; break; }
      }
      if (entry) break;
    }
    let opened = false, scopeVisible = false, cancelled = false, closed = false;
    if (entry) {
      await entry.click({ timeout: 8000 });
      const confirm = page.getByRole("button", { name: /^确认制动（全端 ≤60s 生效）$/ }).first();
      opened = await waitUntil(page, async () => await confirm.count() > 0 && await confirm.isVisible());
      scopeVisible = opened && /全端.*60s/.test(await confirm.innerText());
      const cancel = page.getByRole("button", { name: /^撤回$/ }).first();
      if (opened && await cancel.count() && await cancel.isEnabled()) {
        await cancel.click({ timeout: 6000 }); cancelled = true;
        closed = await waitUntil(page, async () => (await confirm.count() === 0 || !await confirm.isVisible())
          && await page.getByRole("button", { name: /紧急制动/ }).count() > 0, 3000);
      }
    }
    return checked({
      id: "EXP-04", persona: "owner", journey: "异常处理", dimension: "掌控感",
      name: "随时可暂停/制动（入口、二次确认与撤回）",
      precondition: "存在可操作的制动入口",
      steps: ["定位可执行制动入口", "实际打开确认层", "点击撤回并核对确认层消失"],
      expected: "确认层写明生效范围；撤回恢复入口；不发送暂停请求",
      evidence: await shot(page, "exp-04-brake"),
    }, { healthy: (await screen(page)).healthy, where, entryEnabled: Boolean(entry), opened, scopeVisible, cancelled, closed, pauseRequests: watcher.requests.filter((row) => /pause|brake/i.test(row.procedure)).length });
  } finally { await watcher.stop(); }
}

async function dispatch() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.tasks}`, { waitUntil: "domcontentloaded" });
    const input = page.locator('input[placeholder^="说出一句话目标"], textarea[placeholder^="说出一句话目标"]').first();
    await waitUntil(page, async () => {
      const dom = await screen(page);
      const usable = await input.count() > 0 && await input.isVisible() && await input.isEnabled();
      return dom.terminalError || usable && !dom.loading;
    }, 5000);
    const title = `本地验收：整理本周工作记录并形成复盘摘要，周五交付（${new Date().toISOString()}）`;
    const inputVisible = await input.count() > 0 && await input.isVisible() && await input.isEnabled();
    const entryState = await screen(page);
    let filled = false, clicked = false, steps = 0, dispatchMs = null, exchange = null, dispatchAt = null;
    if (inputVisible && entryState.healthy) {
      await input.fill(title); filled = await input.inputValue() === title; steps += 1;
      dispatchAt = now();
      const send = page.getByRole("button", { name: /^启航/ }).first();
      if (filled && await send.count() && await send.isEnabled()) {
        await send.click({ timeout: 8000 }); clicked = true; steps += 1;
        exchange = await watcher.wait("threads.dispatch", (row) => row.requestAt >= dispatchAt && row.method === "POST" && row.input?.title === title, T.dispatchMs);
      }
    }
    const threadId = exchange?.ok ? exchange.data?.threadId : null;
    let detail = null, chain = null, plan = [], planVisible = false;
    if (threadId) {
      const link = page.locator(`a[href="${R.tasks}/${encodeURIComponent(threadId)}"]`).first();
      if (await waitUntil(page, async () => await link.count() > 0 && await link.isVisible())) {
        const navigationAt = now();
        await link.click({ timeout: 8000 }); steps += 1;
        detail = await watcher.wait("threads.get", (row) => row.requestAt >= navigationAt && row.input?.threadId === threadId);
        chain = await watcher.wait("threads.events", (row) => row.requestAt >= navigationAt && row.input?.threadId === threadId);
        if (detail?.ok && Array.isArray(detail.data?.plan)) plan = detail.data.plan;
        else if (chain?.ok && Array.isArray(chain.data)) plan = chain.data.flatMap((event) => Array.isArray(event.decision?.after?.steps) ? event.decision.after.steps : []);
        if (plan.length) {
          planVisible = await waitUntil(page, async () => {
            const dom = await screen(page);
            return dom.healthy && plan.some((step) => [step.description, step.title, step.summary, step.action].some((value) => typeof value === "string" && value.length > 1 && dom.text.includes(value)));
          });
        }
        if (planVisible && dispatchAt !== null) dispatchMs = now() - dispatchAt;
      }
    }
    return checked({
      id: "EXP-05", persona: "manager", journey: "首日（Day-1）", dimension: "效率与恢复",
      name: "把一条需求派给岗位并看到实际拆解",
      precondition: "成员具备派发权限；工作区未触并发上限",
      steps: ["填写页面目标输入", "点击启航", "点击新任务并核对同一任务的拆解"],
      expected: `≤3 步完成派活；详情可读拆解；派发 ≤${T.dispatchMs / 1000}s`,
      evidence: await shot(page, "exp-05-dispatch"),
    }, { healthy: (await screen(page)).healthy, entryState: { healthy: entryState.healthy, loading: entryState.loading, terminalError: entryState.terminalError }, inputVisible, filled, clicked, title, requestTitle: exchange?.input?.title, threadId, responseOk: exchange?.ok ?? false, detailThreadId: detail?.ok ? detail.data?.id : null, detailTitle: detail?.ok ? detail.data?.title : null, planSteps: plan.length, planVisible, steps, dispatchMs, responseKind: exchange?.data?.kind ?? null });
  } finally { await watcher.stop(); }
}

async function inbox() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.inbox}`, { waitUntil: "domcontentloaded" });
    const source = await watcher.wait("accounts.inbox.unified");
    const groups = source?.ok && Array.isArray(source.data?.groups) ? source.data.groups : null;
    await waitUntil(page, async () => await page.getByRole("heading", { name: "统一待办", exact: true }).count() > 0);
    const rendered = await page.evaluate(() => {
      const main = document.querySelector("main") ?? document.body;
      const text = main.innerText ?? "";
      return {
        titleVisible: [...main.querySelectorAll("h1,h2")].some((el) => el.innerText.trim() === "统一待办"),
        renderedGroups: [...main.querySelectorAll("button")].filter((el) => /我的角色/.test(el.innerText)).map((el) => ({
          workspaceName: el.querySelector(".font-semibold")?.textContent?.trim() ?? "",
          pendingApprovals: /无待办/.test(el.innerText) ? 0 : Number(/(\d+)\s*待审批/.exec(el.innerText)?.[1] ?? NaN),
        })),
        totalVisible: /有\s*(\d+)\s*项待审批/.test(text) ? Number(/有\s*(\d+)\s*项待审批/.exec(text)[1]) : null,
        emptyNextStep: /暂无工作区成员关系/.test(text) && /接受邀请|注册开通/.test(text),
      };
    });
    return checked({
      id: "EXP-06", persona: "manager", journey: "日常治理", dimension: "理解度 / 效率与恢复",
      name: "统一待办按来源核对聚合（不丢事项）", precondition: "统一待办来源接口可用",
      steps: ["打开统一待办", "逐工作区核对条目、数量与总数"], expected: "来源对象与页面逐项一致；空态给出下一步",
      evidence: await shot(page, "exp-06-inbox"),
    }, { healthy: (await screen(page)).healthy, sourceOk: source?.ok ?? false, expectedGroups: groups, ...rendered });
  } finally { await watcher.stop(); }
}

async function mobileStaff() {
  // This read uses the exact TOKEN returned by loginAsMember for WORKSPACE_SLUG,
  // independently of the mobile access response being checked.
  const session = await readRpc("members.me");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const key = profile.storage?.bMobileTokenKey ?? `workloom:${productId()}:b-mobile:access-token`;
  await context.addInitScript(({ t, slug, k }) => {
    sessionStorage.setItem(k, t); localStorage.setItem(k.replace("access-token", "workspace"), slug);
    localStorage.removeItem(k.replace("access-token", "guest"));
  }, { t: TOKEN, slug: WORKSPACE_SLUG, k: key });
  const mobile = await context.newPage();
  const watcher = observe(mobile);
  try {
    await mobile.goto(`${URLs.bMobile}${R.tasks}`, { waitUntil: "domcontentloaded" });
    const source = await watcher.wait("threads.list", () => true, 10000);
    const access = await watcher.wait("access.me", () => true, 10000);
    const workspaceIdentity = workspaceIdentityEvidence(profile, session?.ok ? session.data?.identity : null, access?.ok ? access.data : null);
    const expectedTasks = source?.ok && Array.isArray(source.data) ? source.data.map(({ id, title }) => ({ id, title })) : null;
    const cards = await mobile.evaluate(() => [...document.querySelectorAll('[data-wl-pattern="agent-work-card"],main article')]
      .map((el) => ({ title: el.querySelector("h2,h3")?.textContent?.trim() ?? "", receipt: el.querySelector(".wl-receipt")?.textContent ?? el.innerText })));
    // Some clients display only a non-reversible receipt suffix. In that case a
    // unique title within this exact response is also required; ambiguity fails.
    const renderedTasks = cards.map((card) => {
      const matches = expectedTasks?.filter((row) => row.title === card.title && (idShown(card.receipt, row.id) || /尾号\s*\d+/.test(card.receipt) && card.receipt.includes(String(row.id).match(/(\d{2,6})$/)?.[1] ?? "\0"))) ?? [];
      return { title: card.title, id: matches.length === 1 ? matches[0].id : null };
    });
    const dom = await screen(mobile);
    const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
    return checked({
      id: "EXP-07", persona: "staff", journey: "首日", dimension: "理解度 / 响应式",
      name: "移动端能接到真实任务（390px 无横向溢出）", precondition: "B 端移动成员已登录且有任务",
      steps: ["打开移动任务页", "核对成员、工作区和任务对象", "检查390px横向溢出"],
      expected: "成员与任务来源一致；任务内容可见；390px 无横向滚动", evidence: await shot(mobile, "exp-07-mobile"),
    }, { healthy: dom.healthy, member: access?.ok && access.data?.subject?.kind === "member" && access.data.subject.memberNo === MEMBER_NO,
      workspaceMatches: workspaceIdentity.matches, workspaceIdentity,
      titleVisible: await mobile.getByRole("heading", { name: "任务", exact: true }).count() > 0,
      sourceOk: source?.ok ?? false, expectedTasks, renderedTasks, overflow });
  } finally { await watcher.stop(); await context.close(); }
}

async function partnerScope() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.partners}`, { waitUntil: "domcontentloaded" });
    const source = await watcher.wait("accounts.admin.grants");
    const memberships = await watcher.wait("accounts.my.memberships");
    const workspaceNames = new Map(memberships?.ok && Array.isArray(memberships.data)
      ? memberships.data.map((row) => [row.workspace_id, row.workspace_name]) : []);
    // P31's public capability labels. An unknown/fallback label cannot prove
    // what authority was granted and deliberately remains unverifiable.
    const capabilityLabels = {
      "ticket.handle": "工单处理", "ops.execute": "日常执行", "report.view": "经营报表",
      "deliverable.view": "交付物查看", "workorder.self": "仅本人工单", "read:*": "只读访问", "write:*": "业务写入",
    };
    const joinedLabels = (values, label) => Array.isArray(values) && values.length > 0 && values.every((value) => label(value))
      ? values.map(label).join("、") : null;
    const expectedGrants = source?.ok && Array.isArray(source.data) ? source.data.map((grant) => ({
      id: grant.id, name: grant.partner_name,
      workspaces: joinedLabels(grant.workspaces, (id) => workspaceNames.get(id)),
      capabilities: joinedLabels(grant.capabilities, (name) => capabilityLabels[name]),
      expires: Number.isFinite(new Date(grant.expires_at).getTime()) ? new Date(grant.expires_at).toLocaleDateString("zh-CN") : null,
      revoked: Boolean(grant.revoked_at),
    })) : null;
    await waitUntil(page, async () => await page.getByRole("heading", { name: "伙伴授权", exact: true }).count() > 0);
    const dom = await screen(page);
    const renderedGrants = await page.evaluate(() => [...document.querySelectorAll("main article")]
      .filter((el) => el.getBoundingClientRect().width > 0).map((el) => {
        const text = el.innerText;
        return {
          name: el.querySelector(".font-medium")?.textContent?.trim() ?? "",
          workspaces: /工作区：([^\n]+)/.exec(text)?.[1]?.trim() ?? null,
          capabilities: /能力：(.*?)\s*·\s*有效期至/.exec(text)?.[1]?.trim() ?? null,
          expires: /有效期至\s*(\d{4}\/\d{1,2}\/\d{1,2})/.exec(text)?.[1] ?? null,
          revoked: /已吊销/.test(text),
        };
      }));
    return checked({
      id: "EXP-08", persona: "partner", journey: "首周", dimension: "信任与透明 / 理解度",
      name: "伙伴授权范围、期限与资金红线可核对", precondition: "存在可核对的伙伴授权",
      steps: ["打开伙伴授权页", "核对来源授权条目、范围、能力、期限和红线"],
      expected: "权限逐条可见；资金类红线可见", evidence: await shot(page, "exp-08-partner"),
    }, { healthy: dom.healthy, titleVisible: await page.getByRole("heading", { name: "伙伴授权", exact: true }).count() > 0,
      sourceOk: Boolean(source?.ok && memberships?.ok), expectedGrants,
      renderedGrants, moneyRedline: /资金相关能力不向伙伴开放/.test(dom.text) });
  } finally { await watcher.stop(); }
}

async function chatSnapshot(target) {
  return target.evaluate(() => {
    const nodes = [...document.querySelectorAll(".rounded-tl-sm,.rounded-br-sm")];
    return nodes.map((el, order) => ({
      role: el.classList.contains("rounded-tl-sm") ? "assistant" : "user", order,
      text: [...el.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE).map((node) => node.textContent).join("").trim(),
      complete: !el.querySelector(".animate-pulse"),
      demo: /演示|模拟/.test([...el.querySelectorAll("span")].map((node) => node.innerText).join(" ")),
      handoff: [...(el.parentElement?.querySelectorAll("button") ?? [])].some((button) => /^(转人工|联系人工)$/.test(button.innerText.trim()) && !button.disabled && button.getBoundingClientRect().width > 0),
    }));
  });
}

async function guestService() {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "zh-CN" });
  const cPage = await context.newPage();
  const watcher = observe(cPage);
  try {
    await cPage.goto(`${URLs.cMobile}/`, { waitUntil: "domcontentloaded" });
    const input = cPage.locator('input[placeholder="请输入您的需求…"], textarea[placeholder="请输入您的需求…"]').first();
    await waitUntil(cPage, async () => await input.count() > 0);
    const inputVisible = await input.count() > 0 && await input.isVisible() && await input.isEnabled();
    // Let the initial welcome settle before taking the identity baseline.
    await waitUntil(cPage, async () => (await chatSnapshot(cPage)).every((message) => message.complete), 5000);
    const before = await chatSnapshot(cPage);
    const question = "我想了解一下你们的服务，有发票吗？";
    let clicked = false, firstReplyMs = null, exchange = null, after = before;
    if (inputVisible) {
      await input.fill(question);
      const send = cPage.getByRole("button", { name: /发送/ }).first();
      if (await send.count() && await send.isEnabled()) {
        const start = now();
        await send.click({ timeout: 8000 }); clicked = true;
        await waitUntil(cPage, async () => {
          exchange = watcher.latest("c.chat", (row) => row.requestAt >= start && row.method === "POST" && row.input?.text === question);
          after = await chatSnapshot(cPage);
          const reply = after.filter((row) => row.role === "assistant")[before.filter((row) => row.role === "assistant").length];
          if (exchange?.ok && reply?.text && compact(exchange.data.answer).startsWith(compact(reply.text)) && !/正在思考|发送失败|连接失败/.test(reply.text)) {
            if (firstReplyMs === null) firstReplyMs = now() - start;
            return reply.complete;
          }
          return Boolean(exchange && !exchange.ok);
        }, T.guestFirstReplyMs);
      }
    }
    const assistants = after.filter((message) => message.role === "assistant");
    const users = after.filter((message) => message.role === "user");
    const oldAssistants = before.filter((message) => message.role === "assistant").length;
    const oldUsers = before.filter((message) => message.role === "user").length;
    const reply = assistants[oldAssistants];
    const user = users[oldUsers];
    const dom = await screen(cPage);
    const ticketsEntry = await cPage.locator('a[href="#tickets"],button[aria-label="工单"]').count() > 0
      || await cPage.getByRole("button", { name: /^工单$/ }).count() > 0;
    return checked({
      id: "EXP-09", persona: "guest", journey: "异常处理", dimension: "效率与恢复 / 人格与打扰",
      name: "C 端本次首响达标、可转人工、工单入口可见", precondition: "C 端服务配置与会话可用",
      steps: ["记录现有消息边界", "输入并发送问题", "核对本次响应与新增助手气泡、转人工及工单入口"],
      expected: `首响 ≤${T.guestFirstReplyMs / 1000}s；有转人工路径；模拟结果明确标记`,
      metric: firstReplyMs === null ? "首响未捕获" : `首响 ${(firstReplyMs / 1000).toFixed(1)}s`,
      evidence: await shot(cPage, "exp-09-guest"),
    }, { healthy: dom.healthy, inputVisible, clicked, question, requestText: exchange?.input?.text, requestOk: exchange?.ok ?? false,
      responseText: exchange?.ok ? exchange.data.answer : null, conversationId: exchange?.data?.conversationId,
      assistantBefore: oldAssistants, assistantAfter: assistants.length, userBefore: oldUsers, userAfter: users.length,
      newUserText: user?.text, newAssistantText: reply?.text, newAssistantComplete: reply?.complete,
      assistantAfterUser: Boolean(reply && user && reply.order > user.order), firstReplyMs, replyHasHandoff: reply?.handoff ?? false,
      ticketsEntry, replyMock: exchange?.ok ? exchange.data.mock === true : null, demoLabeled: reply?.demo ?? false });
  } finally { await watcher.stop(); await context.close(); }
}

async function fenceSnapshot() {
  const [rules, versions, approvals] = await Promise.all([readRpc("fence.rules"), readRpc("fence.versions"), readRpc("approvals.list")]);
  const byKey = (rows, key) => [...rows].sort((left, right) => String(left[key]).localeCompare(String(right[key])));
  return {
    // hits30 is an unrelated live counter, not rule content.
    // Only these top-level unordered row sets are sorted. Nested rule arrays
    // keep their exact order so semantic content changes cannot disappear.
    rules: rules?.ok && Array.isArray(rules.data) ? byKey(rules.data.map(({ hits30: _hits, ...rule }) => rule), "id") : null,
    versions: versions?.ok && Array.isArray(versions.data) ? byKey(versions.data, "version") : null,
    approvals: approvals?.ok && Array.isArray(approvals.data) ? byKey(approvals.data.map(({ approval_id, event_id, status }) => ({ approval_id, event_id, status })), "approval_id") : null,
  };
}

async function dryRun() {
  const watcher = observe(page);
  try {
    await page.goto(`${URLs.pc}${R.guardrails}`, { waitUntil: "domcontentloaded" });
    await watcher.wait("fence.rules");
    const before = await fenceSnapshot();
    const openedPanel = await ensureRightPanel(page, 'textarea[placeholder^="填写规则名称"]');
    const editor = page.locator('textarea[placeholder^="填写规则名称"]').first();
    const editorVisible = await editor.count() > 0 && await editor.isVisible() && await editor.isEnabled();
    let draftClicked = false, draftVisible = false, clicked = false, exchange = null, reportVisible = false, reportTextMatches = false, reportAbsentBefore = false;
    if (editorVisible) {
      await editor.fill("本地验收：所选范围必须经过负责人审批");
      const draft = page.getByRole("button", { name: /^生成结构化规则草稿$/ }).first();
      if (await draft.count() && await draft.isEnabled()) {
        await draft.click({ timeout: 8000 }); draftClicked = true;
        draftVisible = await waitUntil(page, async () => await page.getByText("草稿预览（确认后进入变更审批）", { exact: true }).count() > 0);
      }
      const dry = page.getByRole("button", { name: /模拟回放最近/ }).first();
      if (draftVisible && await dry.count() && await dry.isEnabled()) {
        reportAbsentBefore = await page.getByText("模拟回放报告", { exact: true }).count() === 0;
        const replayAt = now();
        await dry.click({ timeout: 8000 }); clicked = true;
        exchange = await watcher.wait("fence.dryRun", (row) => row.requestAt >= replayAt && row.method === "POST");
        if (exchange?.ok) {
          reportVisible = await waitUntil(page, async () => await page.getByText("模拟回放报告", { exact: true }).count() > 0);
          const node = page.getByText("模拟回放报告", { exact: true }).first();
          const reportText = reportVisible ? await node.locator("..").innerText() : "";
          reportTextMatches = Boolean(exchange.data?.report?.impact && reportText.includes(exchange.data.report.impact));
        }
      }
    }
    const after = await fenceSnapshot();
    return checked({
      id: "EXP-10", persona: "owner", journey: "异常处理", dimension: "掌控感 / 信任与透明",
      name: "改规则前先模拟回放并核对未确认不落规则库", precondition: "围栏页可用；成员具备治理权限",
      steps: ["回读规则、版本和审批基线", "填写并生成草稿", "实际点击回放", "核对新报告与未变的规则、版本和审批"],
      expected: "dry-run 出本次报告；未确认不创建规则或审批", evidence: await shot(page, "exp-10-dryrun"),
    }, { healthy: (await screen(page)).healthy, openedPanel, editorVisible, draftClicked, draftVisible, clicked, responseOk: exchange?.ok ?? false,
      dryRunId: exchange?.data?.dryRunId, reportRuleId: exchange?.data?.report?.ruleId, requestedRuleId: exchange?.input?.ruleId, reportVisible, reportAbsentBefore, reportTextMatches,
      beforeRulesDigest: digest(before.rules), afterRulesDigest: digest(after.rules), beforeVersionsDigest: digest(before.versions), afterVersionsDigest: digest(after.versions),
      beforeApprovalsDigest: digest(before.approvals), afterApprovalsDigest: digest(after.approvals), confirmRequests: watcher.requests.filter((row) => row.procedure === "fence.confirmDryRun").length });
  } finally { await watcher.stop(); }
}


const BUILTINS = {
  "builtin:first-value": firstValue,
  "builtin:approval": approval,
  "builtin:traceability": traceability,
  "builtin:brake": brake,
  "builtin:dispatch": dispatch,
  "builtin:inbox": inbox,
  "builtin:mobile-staff": mobileStaff,
  "builtin:partner-scope": partnerScope,
  "builtin:guest-service": guestService,
  "builtin:dry-run": dryRun,
};

for (const journey of profile.journeys) {
  if (only && !only.has(journey.id)) continue;
  const impl = BUILTINS[journey.script];
  if (!impl) {
    record({
      id: journey.id, persona: journey.persona ?? "unknown", journey: journey.title ?? "(未命名)", dimension: "自定义",
      name: journey.title ?? journey.id, steps: [], expected: "自定义脚本", actual: { error: `未知脚本 ${journey.script}` }, pass: false,
    });
    continue;
  }
  try {
    record(await impl());
  } catch (err) {
    record({
      id: journey.id, persona: journey.persona ?? "unknown", journey: journey.title ?? "(未命名)", dimension: "—",
      name: journey.title ?? journey.id, steps: [], expected: "—",
      actual: { error: String(err).split("\n")[0], errors: consoleErrors.slice(0, 2) }, pass: false,
    });
  }
}

/* ---------------- 维度机检 ---------------- */
if (!only) {
  try {
    const pages = profile.surfaces.pcRoutes.slice(0, 12);
    const found = [];
    for (const route of pages) {
      await page.goto(`${URLs.pc}${route}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(1600);
      const hits = await page.evaluate(() => {
        const text = document.body.innerText;
        const patterns = [
          [/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/g, "裸 ISO 时间"],
          [/\b[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*){2,}\b/g, "裸动作码"],
          [/\b(agt|ws|MEM|T)-\w+/g, "裸内部 ID"],
          [/\b[A-Z][A-Z_]{4,}\b/g, "裸大写枚举"],
        ];
        const out = [];
        for (const [re, label] of patterns) for (const m of text.matchAll(re)) out.push({ label, value: m[0] });
        return out;
      });
      for (const hit of hits) found.push({ route, ...hit });
    }
    const dataHits = found.filter((h) => /^T-suite/.test(h.value));
    const uiHits = found.filter((h) => !dataHits.includes(h));
    REPORT.dimensions.terminology = { scanned: pages.length, hits: found, uiDefectHits: uiHits.length, dataHygieneHits: dataHits.length };
    for (const hit of uiHits.slice(0, 8)) REPORT.issues.push({ id: "DIM-terminology", where: hit.route, detail: `${hit.label}：${hit.value}` });
    for (const hit of dataHits.slice(0, 3)) REPORT.issues.push({ id: "DIM-data-hygiene", where: hit.route, detail: `演示数据含测试夹具：${hit.value}` });
    console.log(`${uiHits.length === 0 ? "✓" : "✗"} [维度] 术语一致性：UI 缺陷 ${uiHits.length}；数据污染 ${dataHits.length}`);
  } catch (err) {
    REPORT.dimensions.terminology = { error: String(err).split("\n")[0] };
  }

  try {
    await page.goto(`${URLs.pc}${R.home}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    const contrast = await page.evaluate(() => {
      const parse = (rgb) => {
        const m = rgb.match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const parts = m[1].split(",").map((x) => Number.parseFloat(x.trim()));
        return { r: parts[0], g: parts[1], b: parts[2], a: parts[3] ?? 1 };
      };
      const lum = ({ r, g, b }) => {
        const f = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
      };
      const bgOf = (el) => {
        let node = el;
        while (node) {
          const bg = parse(getComputedStyle(node).backgroundColor);
          if (bg && bg.a > 0.5) return bg;
          node = node.parentElement;
        }
        return { r: 255, g: 255, b: 255, a: 1 };
      };
      const nodes = [...document.querySelectorAll("p,span,div,h1,h2,h3,button,a,td,li,small,strong")]
        .filter((el) => {
          const direct = [...el.childNodes].some((n) => n.nodeType === 3 && (n.nodeValue ?? "").trim().length >= 4);
          if (!direct) return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        })
        .slice(0, 600);
      const bad = [];
      for (const el of nodes) {
        const cs = getComputedStyle(el);
        const fg = parse(cs.color);
        if (!fg || fg.a < 0.5) continue;
        const l1 = lum(fg), l2 = lum(bgOf(el));
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        const size = Number.parseFloat(cs.fontSize);
        const large = size >= 24 || (size >= 18.66 && Number.parseInt(cs.fontWeight, 10) >= 700);
        const threshold = large ? 3 : 4.5;
        if (ratio < threshold) bad.push({ text: (el.textContent ?? "").trim().slice(0, 26), ratio: Number(ratio.toFixed(2)), threshold });
      }
      return { sampled: nodes.length, violationCount: bad.length, violations: bad.slice(0, 12) };
    });
    const before = await page.evaluate(() => document.querySelectorAll('[role="dialog"], [role="alert"], [role="alertdialog"]').length);
    await page.waitForTimeout(T.idleInterruptionWindowS * 1000);
    const after = await page.evaluate(() => document.querySelectorAll('[role="dialog"], [role="alert"], [role="alertdialog"]').length);
    REPORT.dimensions.contrast = contrast;
    REPORT.dimensions.interruption = { before, after, delta: after - before, windowSeconds: T.idleInterruptionWindowS };
    console.log(`✓ [维度] 对比度抽样 ${contrast.sampled} 节点，低于阈值 ${contrast.violationCount}；静置 ${T.idleInterruptionWindowS}s 打扰 ${after - before}`);
  } catch (err) {
    REPORT.dimensions.contrast = { error: String(err).split("\n")[0] };
  }
}

await browser.close();

const passed = REPORT.checks.filter((c) => c.pass).length;
REPORT.totals = {
  checks: REPORT.checks.length, passed, failed: REPORT.checks.length - passed,
  issues: REPORT.issues.length,
  byPersona: REPORT.checks.reduce((acc, c) => {
    acc[c.persona] = acc[c.persona] ?? { total: 0, passed: 0 };
    acc[c.persona].total += 1;
    if (c.pass) acc[c.persona].passed += 1;
    return acc;
  }, {}),
};

const md = [];
md.push("# 体验走查报告（角色 × 旅程 × 维度）");
md.push("");
md.push(`- 规范：docs/REAL-DEVICE-ACCEPTANCE-SPEC.md（rdas/v1）；生成时间：${REPORT.at}`);
md.push(`- 通过：${REPORT.totals.passed}/${REPORT.totals.checks}；问题：${REPORT.totals.issues}`);
if (profileWarnings.length) md.push(`- profile 告警：${profileWarnings.join("；")}`);
md.push("");
md.push("| 走查 | 角色 | 旅程 | 维度 | 预期 | 实测 | 结论 |");
md.push("|---|---|---|---|---|---|---|");
for (const c of REPORT.checks) md.push(`| ${c.id} ${c.name} | ${c.persona} | ${c.journey} | ${c.dimension} | ${c.expected} | ${JSON.stringify(c.actual)} | ${c.pass ? "通过" : "**未通过**"} |`);
if (REPORT.dimensions.terminology) {
  md.push("");
  md.push("## 术语一致性扫描");
  md.push("");
  md.push(`扫描 ${REPORT.dimensions.terminology.scanned} 个页面：UI 文案缺陷 ${REPORT.dimensions.terminology.uiDefectHits} 处；演示数据污染 ${REPORT.dimensions.terminology.dataHygieneHits} 处。`);
}
if (REPORT.dimensions.contrast) {
  md.push("");
  md.push("## 对比度与打扰预算");
  md.push("");
  md.push(`对比度抽样 ${REPORT.dimensions.contrast.sampled ?? "-"} 节点，低于 AA 阈值 ${REPORT.dimensions.contrast.violationCount ?? "-"} 处；`);
  md.push(`首页静置 ${REPORT.dimensions.interruption?.windowSeconds ?? "-"} 秒新增打扰 ${REPORT.dimensions.interruption?.delta ?? "-"} 次。`);
}

writeFileSync(join(OUT_DIR, "experience-report.json"), JSON.stringify(REPORT, null, 1));
writeFileSync(join(OUT_DIR, "experience-report.md"), `${md.join("\n")}\n`);
console.log(`[acceptance:experience] 通过 ${REPORT.totals.passed}/${REPORT.totals.checks}；输出 ${OUT_DIR}`);
if (REPORT.totals.issues > 0) process.exitCode = 1;
