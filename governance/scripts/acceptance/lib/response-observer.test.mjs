import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import { createServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const HERE = dirname(fileURLToPath(import.meta.url));
const ACCEPTANCE = resolve(HERE, "..");
const SOURCE = process.env.MC036_SOURCE_DIR ? resolve(process.env.MC036_SOURCE_DIR) : ACCEPTANCE;
const GOVERNANCE = resolve(ACCEPTANCE, "../..");
const EVIDENCE = process.env.MC036_EVIDENCE_DIR ? resolve(process.env.MC036_EVIDENCE_DIR) : null;
const BODY_LIMIT = 8000;
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const sourceHashes = () => Object.fromEntries(["experience.mjs", "lib/response-observer.mjs", "lib/ui-evidence.mjs"]
  .map((file) => [file, existsSync(join(SOURCE, file)) ? hash(readFileSync(join(SOURCE, file))) : null]));
const flush = () => new Promise((done) => setImmediate(done));
const helper = () => import(pathToFileURL(join(SOURCE, "lib/ui-evidence.mjs")).href);

async function loadObserver() {
  const modulePath = join(SOURCE, "lib/response-observer.mjs");
  if (existsSync(modulePath)) return (await import(pathToFileURL(modulePath).href)).observeResponses;
  // Red replay uses the exact original collector, without launching the CLI.
  // This branch is unavailable in a normal installed test run.
  assert.ok(process.env.MC036_SOURCE_DIR, "the response observer module must be installed");
  const source = readFileSync(join(SOURCE, "experience.mjs"), "utf8");
  const start = source.indexOf("async function waitUntil(");
  const end = source.indexOf("// These reads corroborate UI results;");
  assert.ok(start >= 0 && end > start && source.slice(start, end).includes("function observe(target) {"));
  const { rpcExchange } = await helper();
  return new Function("rpcExchange", `const now = () => Date.now();\n${source.slice(start, end)}\nreturn observe;`)(rpcExchange);
}

function deferred() {
  let resolveValue, rejectValue;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolveValue = resolvePromise; rejectValue = rejectPromise;
  });
  return { promise, resolve: resolveValue, reject: rejectValue };
}

function fakePage() {
  const page = new EventEmitter();
  page.waitForTimeout = (ms) => new Promise((done) => setTimeout(done, ms));
  return page;
}

function requestOf(path = "/trpc/approvals.decide", method = "POST", input = { approvalId: "approval-1", gesture: "approve" }) {
  return {
    url: () => `http://127.0.0.1${path}`, method: () => method,
    postDataJSON: () => input, failure: () => ({ errorText: "net::ERR_ABORTED" }),
  };
}

function responseOf(request, json, status = 200) {
  let reads = 0;
  return {
    request: () => request, status: () => status, ok: () => status >= 200 && status < 300,
    json: () => { reads += 1; return typeof json === "function" ? json() : Promise.resolve(json); },
    reads: () => reads,
  };
}

function emit(page, request, response) {
  page.emit("request", request);
  page.emit("response", response);
}

function saveUnit(name, observer, extra = {}) {
  if (!EVIDENCE) return;
  const output = join(EVIDENCE, "unit");
  mkdirSync(output, { recursive: true });
  writeFileSync(join(output, `${name}.json`), JSON.stringify({
    sourceHashes: sourceHashes(), testSha256: hash(readFileSync(fileURLToPath(import.meta.url))),
    responses: observer.responses, failures: observer.failures ?? null, ...extra,
  }, null, 2));
}

test("unit: normal, batch and chat replies retain request and response identity", async () => {
  const observe = await loadObserver();
  const page = fakePage();
  const watcher = observe(page);
  const batch = requestOf("/trpc/threads.list,threads.dispatch?batch=1", "POST", { 0: {}, 1: { title: "验收目标" } });
  const batchResponse = responseOf(batch, [{ result: { data: [] } }, { result: { data: { threadId: "T-1" } } }]);
  emit(page, batch, batchResponse);
  const get = requestOf(`/trpc/threads.get?input=${encodeURIComponent(JSON.stringify({ threadId: "T-1" }))}`, "GET", null);
  emit(page, get, responseOf(get, { result: { data: { id: "T-1" } } }));
  const chat = requestOf("/c/chat", "POST", { text: "本次问题" });
  emit(page, chat, responseOf(chat, { answer: "本次回复", conversationId: "conversation-1" }));
  await watcher.stop();
  assert.equal(watcher.responses.length, 4);
  assert.deepEqual(watcher.latest("threads.dispatch").input, { title: "验收目标" });
  assert.deepEqual(watcher.latest("threads.get").input, { threadId: "T-1" });
  assert.equal(watcher.latest("threads.dispatch", (row) => row.input?.title === "other"), null);
  assert.equal(watcher.latest("c.chat").data.answer, "本次回复");
  assert.ok(watcher.responses.every((row) => row.ok && Number.isFinite(row.requestAt)));
  assert.equal(batchResponse.reads(), 1, "a batch body must be read once, without retries");
  assert.deepEqual(watcher.requests.map((row) => row.procedure), ["threads.list", "threads.dispatch", "c.chat"]);
  assert.equal((watcher.failures ?? []).length, 0);
});

test("unit: HTTP and business errors remain failed exchanges", async () => {
  const observe = await loadObserver();
  const page = fakePage();
  const watcher = observe(page);
  for (const [status, data] of [[503, { result: { data: { status: "approved" } } }], [200, { error: { message: "denied" } }], [200, null]]) {
    const request = requestOf();
    emit(page, request, responseOf(request, data, status));
  }
  const chat = requestOf("/c/chat");
  emit(page, chat, responseOf(chat, { answer: "   " }));
  await watcher.stop();
  assert.equal(watcher.responses.length, 4);
  assert.ok(watcher.responses.every((row) => row.ok === false));
});

for (const [name, error, code] of [
  ["invalid-json", new SyntaxError("fixture invalid JSON; private payload must not be reported"), "invalid-json"],
  ["body-rejected", new Error("fixture body unavailable; private URL must not be reported"), "body-read-failed"],
]) {
  test(`unit: ${name} leaves explicit safe failure evidence`, async () => {
    const observe = await loadObserver();
    const page = fakePage();
    const watcher = observe(page);
    const request = requestOf();
    emit(page, request, responseOf(request, () => Promise.reject(error)));
    await watcher.stop();
    saveUnit(name, watcher);
    const row = watcher.latest("approvals.decide");
    assert.equal(row?.ok, false);
    assert.equal(row?.data, null);
    assert.equal(row?.readError?.code, code);
    assert.equal(watcher.failures?.length, 1);
    assert.equal(watcher.failures[0].procedure, "approvals.decide");
    assert.equal(watcher.failures[0].readError.code, code);
    assert.equal(JSON.stringify(watcher.failures).includes("private"), false);
    assert.equal("input" in watcher.failures[0], false);
  });
}

test("unit: unfinished body and stop settle at the existing 8000ms boundary", async (t) => {
  const observe = await loadObserver();
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 100000 });
  const page = fakePage();
  const watcher = observe(page);
  const body = deferred();
  const request = requestOf();
  const response = responseOf(request, () => body.promise);
  emit(page, request, response);
  let stopped = false;
  const stopping = watcher.stop().then(() => { stopped = true; });
  try {
    await flush();
    t.mock.timers.tick(BODY_LIMIT - 1); await flush();
    assert.equal(stopped, false, "the original observation budget must not be shortened");
    t.mock.timers.tick(1); await flush();
    saveUnit("unfinished-body", watcher, { stoppedAtLimit: stopped, bodyLimitMs: BODY_LIMIT });
    assert.equal(stopped, true, "stop must not wait indefinitely for Response.body");
    const row = watcher.latest("approvals.decide");
    assert.equal(row?.ok, false);
    assert.equal(row?.data, null);
    assert.equal(row?.readError?.code, "body-timeout");
    assert.equal(row?.readError?.timeoutMs, BODY_LIMIT);
    assert.equal(watcher.failures?.length, 1);
    assert.equal(response.reads(), 1);
    await watcher.stop();
    assert.equal(page.listenerCount("request"), 0);
    assert.equal(page.listenerCount("response"), 0);
    assert.equal(page.listenerCount("requestfailed"), 0);
    assert.equal(page.listenerCount("close"), 0);
  } finally {
    body.resolve({ result: { data: { status: "approved" } } });
    await stopping; await flush();
  }
});

for (const late of ["resolve", "reject"]) {
  test(`unit: late ${late} after timeout cannot change either journey`, async (t) => {
    const observe = await loadObserver();
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 100000 });
    const page = fakePage();
    const first = observe(page);
    const body = deferred();
    const oldRequest = requestOf();
    emit(page, oldRequest, responseOf(oldRequest, () => body.promise));
    await flush();
    t.mock.timers.tick(BODY_LIMIT); await flush();
    const before = JSON.stringify({ responses: first.responses, failures: first.failures });
    const stopping = first.stop();
    const second = observe(page);
    try {
      const freshRequest = requestOf("/trpc/threads.list", "GET", null);
      emit(page, freshRequest, responseOf(freshRequest, { result: { data: [] } }));
      if (late === "resolve") body.resolve({ result: { data: { status: "approved" } } });
      else body.reject(new Error("late fixture rejection"));
      await flush();
      // A delayed response from a request owned by the previous observer also
      // cannot be adopted by the next journey.
      page.emit("response", responseOf(oldRequest, { result: { data: { status: "approved" } } }));
      await flush();
      await second.stop();
      saveUnit(`late-${late}`, first, { nextResponses: second.responses });
      assert.equal(JSON.stringify({ responses: first.responses, failures: first.failures }), before);
      assert.equal(first.latest("approvals.decide")?.ok, false);
      assert.equal(second.latest("approvals.decide"), null);
      assert.equal(second.latest("threads.list")?.ok, true);
    } finally {
      body.resolve(null);
      await stopping;
      await second.stop();
    }
  });
}

test("unit: navigation request failure cancels a pending read once", async () => {
  const observe = await loadObserver();
  const page = fakePage();
  const watcher = observe(page);
  const body = deferred();
  const request = requestOf();
  emit(page, request, responseOf(request, () => body.promise));
  page.emit("requestfailed", request);
  const stopping = watcher.stop();
  body.reject(new Error("navigation cancelled the response body"));
  await stopping;
  saveUnit("navigation-cancelled", watcher);
  assert.equal(watcher.latest("approvals.decide")?.ok, false);
  assert.equal(watcher.latest("approvals.decide")?.readError?.code, "request-failed");
  assert.equal(watcher.failures?.length, 1);
  assert.equal(watcher.responses.length, 1);
});

test("unit: page closure releases pending reads without a successful receipt", async () => {
  const observe = await loadObserver();
  const page = fakePage();
  const watcher = observe(page);
  const body = deferred();
  const request = requestOf();
  emit(page, request, responseOf(request, () => body.promise));
  page.emit("close");
  const stopping = watcher.stop();
  body.reject(new Error("Target closed"));
  await stopping;
  saveUnit("page-closed", watcher);
  assert.equal(watcher.latest("approvals.decide")?.ok, false);
  assert.equal(watcher.latest("approvals.decide")?.readError?.code, "page-closed");
  assert.equal(watcher.failures?.length, 1);
});

test("unit: a request that fails before response headers is retained as failure", async () => {
  const observe = await loadObserver();
  const page = fakePage();
  const watcher = observe(page);
  const request = requestOf();
  page.emit("request", request);
  page.emit("requestfailed", request);
  await watcher.stop();
  saveUnit("no-response", watcher);
  assert.equal(watcher.latest("approvals.decide")?.ok, false);
  assert.equal(watcher.latest("approvals.decide")?.status, null);
  assert.equal(watcher.latest("approvals.decide")?.readError?.code, "request-failed");
});

test("unit: missing response stays missing after the unchanged default wait budget", async (t) => {
  const observe = await loadObserver();
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 100000 });
  const page = fakePage();
  const watcher = observe(page);
  let settled = false, result;
  const waiting = watcher.wait("approvals.decide").then((row) => { settled = true; result = row; });
  await flush();
  for (let elapsed = 0; elapsed < BODY_LIMIT; elapsed += 100) {
    t.mock.timers.tick(100); await flush();
    if (elapsed < BODY_LIMIT - 100) assert.equal(settled, false);
  }
  await waiting;
  await watcher.stop();
  assert.equal(result, null);
  assert.equal(watcher.responses.length, 0);
});

test("unit: stopping detaches new input while a pending response uses its remaining deadline", async (t) => {
  const observe = await loadObserver();
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 100000 });
  const page = fakePage();
  const watcher = observe(page);
  const body = deferred();
  const request = requestOf();
  emit(page, request, responseOf(request, () => body.promise));
  await flush();
  t.mock.timers.tick(3000); await flush();
  let stopped = false;
  const stopping = watcher.stop().then(() => { stopped = true; });
  const ignored = requestOf("/trpc/threads.dispatch");
  emit(page, ignored, responseOf(ignored, { result: { data: { threadId: "late" } } }));
  try {
    t.mock.timers.tick(4999); await flush();
    assert.equal(stopped, false);
    t.mock.timers.tick(1); await flush();
    saveUnit("stop-remaining-budget", watcher, { stopped, elapsedMs: BODY_LIMIT });
    assert.equal(stopped, true, "stop must not restart the eight-second body budget");
    assert.equal(watcher.latest("threads.dispatch"), null);
    assert.equal(watcher.requests.length, 1);
  } finally {
    body.resolve(null);
    await stopping;
  }
});

function html(body, script) {
  return `<!doctype html><meta charset="utf-8"><style>body{font:16px sans-serif;margin:12px}button{min-height:32px;margin:8px}</style>${body}<script>${script}</script>`;
}

async function withBrowserServer(name, run) {
  const { loadChromium } = await import("./playwright.mjs");
  const chromium = loadChromium(GOVERNANCE);
  const lifecycle = [];
  const mark = (event) => lifecycle.push({ event, at: Date.now() });
  let server;
  let browserProcess;
  mark("launch-started");
  try {
    // The test process owns the browser, even if the old CLI must be terminated
    // by its watchdog. Killing that CLI must never orphan a Chrome process.
    server = await chromium.launchServer({
      headless: true, host: "127.0.0.1", timeout: 15000,
      ...(process.env.MC036_BROWSER_CHANNEL ? { channel: process.env.MC036_BROWSER_CHANNEL } : {}),
    });
    browserProcess = server.process();
    mark("browser-started");
    return await run({ chromium, endpoint: server.wsEndpoint(), mark });
  } finally {
    if (server) {
      await server.close();
      mark("browser-closed");
    }
    if (EVIDENCE) {
      const folder = join(EVIDENCE, "browser-lifecycle");
      mkdirSync(folder, { recursive: true });
      writeFileSync(join(folder, `${name}.json`), JSON.stringify({
        lifecycle, pid: browserProcess?.pid ?? null,
        exitCode: browserProcess?.exitCode ?? null, signal: browserProcess?.signalCode ?? null,
        processExited: !browserProcess || browserProcess.exitCode !== null || browserProcess.signalCode !== null,
        sourceHashes: sourceHashes(), testSha256: hash(readFileSync(fileURLToPath(import.meta.url))),
      }, null, 2));
    }
    if (browserProcess) assert.ok(browserProcess.exitCode !== null || browserProcess.signalCode !== null, "owned browser must exit");
  }
}

async function withServer(mode, run) {
  const requests = [];
  const sockets = new Set();
  let approvalStatus = "pending";
  const server = createServer(async (req, res) => {
    let payload = "";
    for await (const chunk of req) payload += chunk;
    const url = new URL(req.url, "http://127.0.0.1");
    let input;
    try { input = JSON.parse(payload || url.searchParams.get("input") || "null"); }
    catch { res.writeHead(400); res.end("invalid fixture input"); return; }
    requests.push({ method: req.method, path: url.pathname, input });
    if (url.pathname.startsWith("/trpc/")) {
      const procedure = url.pathname.slice(6);
      if (procedure === "captain.theater,captain.chairmanQueue" || procedure === "approvals.decide" && mode === "required-hang") {
        if (procedure === "approvals.decide") approvalStatus = "approved";
        res.writeHead(200, { "content-type": "application/json", "content-length": "16007" });
        res.flushHeaders();
        res.write('{"result":{"data":');
        return; // Deliberately leave a real finite-JSON response unfinished.
      }
      if (procedure === "approvals.decide") approvalStatus = "approved";
      if (procedure === "approvals.decide" && mode === "invalid-json") {
        res.writeHead(200, { "content-type": "application/json" }); res.end("{invalid");
        return;
      }
      const data = procedure === "approvals.list"
        ? [{ approval_id: "approval-1", event_id: "event-1", status: approvalStatus, created_at: new Date().toISOString() }]
        : procedure === "approvals.decide"
          ? { approvalId: input.approvalId, status: "approved", gestureEventId: "gesture-1" } : [];
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ result: { data } }));
      return;
    }
    res.writeHead(200, { "content-type": "text/html" });
    const rpc = `async function rpc(name,input){const r=await fetch('/trpc/'+name,{method:input?'POST':'GET',headers:{'content-type':'application/json'},body:input?JSON.stringify(input):undefined});return (await r.json()).result.data}`;
    const background = mode === "background-hang" ? `fetch('/trpc/captain.theater,captain.chairmanQueue?batch=1').catch(()=>{});` : "";
    const body = `<main><h2>审批中心</h2><button>approval-1 必审</button><button id="advance">推进</button><button>校准</button><button>制动</button><div id="notice"></div></main>`;
    const script = `${rpc};${background};rpc('approvals.list');document.querySelector('#advance').onclick=async()=>{try{const result=await rpc('approvals.decide',{approvalId:'approval-1',gesture:'approve'});document.querySelector('#notice').textContent='审批决定已写入事件账本，账本事件 '+result.gestureEventId;}catch{document.querySelector('#notice').textContent='响应无法读取';}}`;
    res.end(url.pathname === "/approvals" ? html(body, script) : html("<main>本地响应观察器夹具</main>", ""));
  });
  server.on("connection", (socket) => { sockets.add(socket); socket.on("close", () => sockets.delete(socket)); });
  await new Promise((done, fail) => { server.once("error", fail); server.listen(0, "127.0.0.1", done); });
  const base = `http://127.0.0.1:${server.address().port}`;
  try { return await run({ base, requests }); }
  finally {
    for (const socket of sockets) socket.destroy();
    await new Promise((done, fail) => server.close((error) => error ? fail(error) : done()));
  }
}

async function runCliFixture(mode) {
  const temporary = mkdtempSync(join(tmpdir(), "workloom-response-observer-"));
  const output = EVIDENCE ? join(EVIDENCE, mode) : join(temporary, "evidence");
  mkdirSync(output, { recursive: true });
  try {
    return await withBrowserServer(mode, async ({ endpoint, mark }) => withServer(mode, async ({ base, requests }) => {
      const scripts = join(temporary, "scripts/acceptance");
      mkdirSync(join(scripts, "lib"), { recursive: true });
      for (const file of ["experience.mjs", "lib/ui-evidence.mjs", "lib/response-observer.mjs"]) {
        if (existsSync(join(SOURCE, file))) writeFileSync(join(scripts, file), readFileSync(join(SOURCE, file)));
      }
      const profile = {
        identity: { workspaceSlug: "fixture", human: "MEM-001" },
        routes: { approvals: "/approvals" },
        thresholds: { approvalMs: 3000 },
        journeys: [{ id: "EXP-02", script: "builtin:approval", title: "审批响应证据" }],
      };
      writeFileSync(join(temporary, "package.json"), '{"type":"module"}');
      writeFileSync(join(temporary, "product.manifest.json"), '{"productId":"fixture"}');
      writeFileSync(join(scripts, "lib/profile.mjs"), `export const cliArgs=()=>({outDir:${JSON.stringify(output)}});export const findRepoRoot=()=>${JSON.stringify(temporary)};export const loadProfile=()=>({profile:${JSON.stringify(profile)},warnings:[]});export const urlsOf=()=>(${JSON.stringify({ pc: base, api: base })});`);
      writeFileSync(join(scripts, "lib/session.mjs"), `export const loginAsMember=async()=> "fixture-only";export const productIdOf=()=> "fixture";`);
      writeFileSync(join(scripts, "lib/playwright.mjs"), `
        import {loadChromium as real} from ${JSON.stringify(pathToFileURL(join(HERE, "playwright.mjs")).href)};
        export function loadChromium(){return{launch:async(options)=>{
          const browser=await real(${JSON.stringify(GOVERNANCE)}).connect(${JSON.stringify(endpoint)},{timeout:5000});
          const newContext=browser.newContext.bind(browser);
          browser.newContext=async(opts)=>{const context=await newContext(opts);await context.route("**/*",route=>new URL(route.request().url()).hostname==="127.0.0.1"?route.continue():route.abort());return context};
          return browser;
        }}}
      `);
      const argv = [join(scripts, "experience.mjs"), "--only", "EXP-02"];
      const started = Date.now();
      mark("cli-started");
      const child = spawn(process.execPath, argv, { cwd: temporary, env: { ...process.env, DATABASE_URL: "" }, stdio: ["ignore", "pipe", "pipe"] });
      let log = "", killed = false;
      child.stdout.on("data", (chunk) => { log += chunk; });
      child.stderr.on("data", (chunk) => { log += chunk; });
      const watchdog = setTimeout(() => { killed = true; child.kill("SIGKILL"); }, 12000);
      let exitCode;
      try { exitCode = await new Promise((done, fail) => { child.once("error", fail); child.once("close", done); }); }
      finally { clearTimeout(watchdog); }
      mark("cli-exited");
      writeFileSync(join(output, "fixture-process.log"), log);
      const reportPath = join(output, "experience-report.json");
      const report = existsSync(reportPath) ? JSON.parse(readFileSync(reportPath, "utf8")) : null;
      writeFileSync(join(output, "fixture-receipt.json"), JSON.stringify({
        mode, argv, sourceHashes: sourceHashes(), testSha256: hash(readFileSync(fileURLToPath(import.meta.url))),
        exitCode, killed, elapsedMs: Date.now() - started, requests,
        bind: "127.0.0.1", productDatabase: false, productPorts: false,
        bodyLimitMs: BODY_LIMIT, browserChannel: process.env.MC036_BROWSER_CHANNEL ?? "chromium",
        reportWritten: Boolean(report), logSha256: hash(log),
      }, null, 2));
      assert.equal(killed, false, `${mode}: original CLI did not finish its response observation\n${log}`);
      assert.ok(report, `${mode}: CLI must write evidence even when a response body fails\n${log}`);
      return { report, exitCode, requests };
    }));
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

for (const [mode, expectedPass, failureProcedure, code] of [
  ["normal", true, null, null],
  ["background-hang", true, "captain.theater", "body-timeout"],
  ["required-hang", false, "approvals.decide", "body-timeout"],
  ["invalid-json", false, "approvals.decide", "invalid-json"],
]) {
  // The fixture watchdog remains 12s and the body budget remains 8s. This outer
  // budget additionally permits browser startup and verified process cleanup.
  test(`real browser CLI: ${mode} preserves the business verdict and failure evidence`, { timeout: 40000 }, async () => {
    const { report, exitCode, requests } = await runCliFixture(mode);
    assert.deepEqual(report.checks.map((row) => row.id), ["EXP-02"]);
    assert.equal(report.checks[0].pass, expectedPass, JSON.stringify(report.checks[0].actual));
    assert.equal(exitCode, expectedPass ? 0 : 1);
    assert.ok(requests.some((row) => row.path === "/trpc/approvals.decide" && row.method === "POST"));
    assert.equal(report.responseObservations?.length, 1);
    const observation = report.responseObservations[0];
    assert.equal(observation.journeyId, "EXP-02");
    if (failureProcedure) {
      const failure = observation.readFailures.find((row) => row.procedure === failureProcedure);
      assert.equal(failure?.ok, false);
      assert.equal(failure?.readError?.code, code);
      if (mode === "background-hang") {
        assert.equal(report.checks[0].actual.receiptApprovalId, "approval-1");
        assert.equal(report.checks[0].actual.gestureEventId, "gesture-1");
        assert.equal(report.checks[0].actual.readbackStatus, "approved");
      } else {
        assert.notEqual(report.checks[0].status, "passed");
        assert.equal(report.checks[0].actual.gestureEventId, undefined);
      }
    } else assert.deepEqual(observation.readFailures, []);
  });
}

test("real browser: navigation cancels an unfinished HTTP JSON body with retained evidence", { timeout: 40000 }, async () => {
  const observe = await loadObserver();
  await withBrowserServer("navigation", async ({ chromium, endpoint, mark }) => {
    const browser = await chromium.connect(endpoint, { timeout: 5000 });
    try {
      await withServer("normal", async ({ base }) => {
        const page = await browser.newPage();
        await page.goto(base, { waitUntil: "domcontentloaded" });
        const watcher = observe(page);
        const received = page.waitForResponse((response) => response.url().includes("captain.theater"));
        await page.evaluate(() => { void fetch("/trpc/captain.theater,captain.chairmanQueue?batch=1").catch(() => undefined); });
        await received;
        mark("unfinished-response-observed");
        await page.goto(`${base}/next`, { waitUntil: "domcontentloaded" });
        const started = Date.now();
        let finished = false;
        const stopping = watcher.stop().then(() => { finished = true; });
        let watchdog;
        try {
          await Promise.race([stopping, new Promise((done) => { watchdog = setTimeout(done, 9000); })]);
          mark("observation-deadline-returned");
          saveUnit("real-navigation", watcher, { finished, elapsedMs: Date.now() - started });
          assert.equal(finished, true);
          const failure = watcher.latest("captain.theater");
          assert.equal(failure?.ok, false);
          assert.ok(["request-failed", "body-read-failed", "body-timeout"].includes(failure?.readError?.code));
          assert.equal(watcher.failures?.length, 2);
        } finally {
          clearTimeout(watchdog);
          await page.close();
          mark("page-closed");
          await stopping;
        }
      });
    } finally {
      await browser.close();
    }
  });
});
