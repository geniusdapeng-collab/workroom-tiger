#!/usr/bin/env node
/** Exercise the packaged customer runtime with no developer executables on PATH. */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { StringDecoder } from "node:string_decoder";
import { existsSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { assertKernelPayload } from "./pack-tiger-kernel.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { redactText } = require("../apps/desktop/electron/diagnostic-redaction.cjs");
const sha = (data) => createHash("sha256").update(data).digest("hex");
const arg = (name, fallback) => { const i = process.argv.indexOf(name); return i < 0 ? fallback : process.argv[i + 1]; };

async function availablePort() {
  const server = createServer();
  await new Promise((yes, no) => { server.once("error", no); server.listen(0, "127.0.0.1", yes); });
  const port = server.address().port;
  await new Promise((yes, no) => server.close((error) => error ? no(error) : yes()));
  return port;
}

export function execute(command, args, { environment, cwd, input = "", timeout = 180000, codes = [0], label, onStdoutLine = null }) {
  return new Promise((yes, no) => {
    const child = spawn(command, args, { env: environment, cwd, stdio: ["pipe", "pipe", "pipe"], windowsHide: true, detached: process.platform !== "win32" });
    const out = [], err = [];
    const pending = [];
    const lineDecoder = new StringDecoder("utf8");
    let bytes = 0, failure = null, forceTimer = null, closed = false, settled = false, inputClosed = false, lineBuffer = "";
    let timer;
    const alive = () => !closed && child.exitCode === null && child.signalCode === null;
    const settle = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(forceTimer);
      if (error) no(error); else yes(value);
    };
    const kill = (force) => {
      if (!child.pid) return;
      if (process.platform === "win32") {
        if (child.exitCode !== null || child.signalCode !== null) return;
        const taskkill = join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
        const result = spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true, timeout: 10000 });
        if (result.error || result.status !== 0) child.kill("SIGKILL");
      } else {
        try { process.kill(-child.pid, force ? "SIGKILL" : "SIGTERM"); }
        catch (error) { if (error.code !== "ESRCH") child.kill(force ? "SIGKILL" : "SIGTERM"); }
      }
    };
    const stop = (error) => {
      if (failure) return;
      failure = error;
      if (closed) { settle(error); return; }
      kill(false);
      forceTimer = setTimeout(() => kill(true), 3000);
    };
    timer = setTimeout(() => stop(new Error(`${label} 超时`)), timeout);
    const context = {
      isAlive: alive,
      sendLine(value) {
        if (!alive() || inputClosed) throw new Error(`${label} 已退出或输入已关闭`);
        child.stdin.write(JSON.stringify(value) + "\n", (error) => { if (error) stop(error); });
      },
      closeInput() { if (!inputClosed) { inputClosed = true; child.stdin.end(); } },
    };
    for (const [stream, destination] of [[child.stdout, out], [child.stderr, err]]) stream.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > 8 * 1024 * 1024) stop(new Error(`${label} 输出超过 8 MiB`));
      else {
        destination.push(chunk);
        if (stream === child.stdout && typeof onStdoutLine === "function" && !failure) {
          lineBuffer += lineDecoder.write(chunk);
          let newline;
          while ((newline = lineBuffer.indexOf("\n")) >= 0) {
            const line = lineBuffer.slice(0, newline);
            lineBuffer = lineBuffer.slice(newline + 1);
            if (Buffer.byteLength(line) > 65536) { stop(new Error(`${label} 单行输出超过限制`)); break; }
            const callback = Promise.resolve().then(() => onStdoutLine(line, context)).catch(stop);
            pending.push(callback);
          }
          if (Buffer.byteLength(lineBuffer) > 65536) stop(new Error(`${label} 单行输出超过限制`));
        }
      }
    });
    child.once("error", (error) => settle(error));
    child.once("close", async (code, signal) => {
      closed = true;
      const stdout = Buffer.concat(out).toString("utf8"), stderr = redactText(Buffer.concat(err).toString("utf8"));
      if (failure || !codes.includes(code)) {
        settle(failure || new Error(`${label} exit=${code}, signal=${signal}: ${stderr.slice(-1800)} ${redactText(stdout).slice(-1000)}`));
        return;
      }
      await Promise.all(pending);
      settle(failure, { code, stdout, stderr });
    });
    child.stdin.on("error", (error) => { if (error.code !== "EPIPE") stop(error); });
    if (typeof onStdoutLine === "function") { if (input) child.stdin.write(input); }
    else { inputClosed = true; child.stdin.end(input); }
  });
}

async function liveHttp(url, maxBytes) {
  const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(8000) });
  assert.equal(response.status, 200, "本次客户端服务必须直接返回 HTTP200");
  const reader = response.body?.getReader();
  assert.ok(reader, "服务响应没有正文");
  let bytes = 0;
  const chunks = [];
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      bytes += item.value.byteLength;
      assert.ok(bytes <= maxBytes, "服务身份正文超过限制");
      chunks.push(Buffer.from(item.value));
    }
  } finally { await reader.cancel(); }
  return { response, text: Buffer.concat(chunks).toString("utf8") };
}

/** Read the single public installed-identity contract while its actual App process is alive. */
export async function verifyRunningDesktop({ supportDir, ports, build, advertised, isAlive }) {
  assert.equal(isAlive(), true, "客户端在身份核验前已退出");
  const { inspectClientIdentity } = await import("./acceptance/lib/client-identity.mjs");
  const urls = { api: `http://127.0.0.1:${ports.server}`, pc: `http://127.0.0.1:${ports.web}` };
  const installed = inspectClientIdentity({ supportDir, urls, expectedProductId: build.productId });
  assert.equal(installed.ok, true, "实际安装身份与完整不可变载荷不一致");
  const identity = installed.identity;
  assert.deepEqual(advertised, identity, "App 握手必须绑定实际普通安装状态");
  assert.equal(identity.payloadVersion, build.version);
  assert.equal(identity.payloadIntegritySha256, build.payloadIntegrity?.payloadIntegritySha256);
  assert.equal(identity.productManifestSha256, build.payloadIntegrity?.productManifestSha256);
  assert.deepEqual(identity.ports, ports);
  const [api, web] = await Promise.all([liveHttp(`${urls.api}/health`, 64000), liveHttp(`${urls.pc}/`, 2000000)]);
  let health;
  try { health = JSON.parse(api.text); }
  catch { throw new Error("客户端健康应答 JSON 格式不合法"); }
  assert.equal(health.ok, true);
  assert.equal(health.service, "workloom-im-server");
  assert.equal(health.instanceId, identity.instanceId, "健康应答必须来自本次 launch UUID");
  assert.equal(web.response.headers.get("x-workloom-instance-id"), identity.instanceId);
  assert.equal(web.response.headers.get("x-workloom-product-id"), identity.productId);
  const { hasControlledProductMarker } = require("../apps/desktop/electron/product-surface.cjs");
  assert.equal(hasControlledProductMarker(web.text, identity.productId), true, "工作台页面缺少真实受控产品身份");
  assert.equal(isAlive(), true, "客户端在服务身份核验期间退出");
  const after = inspectClientIdentity({ supportDir, urls, expectedProductId: build.productId });
  assert.equal(after.ok, true, "实际安装身份在服务核验期间变化");
  assert.deepEqual(after.identity, identity);
  assert.equal(isAlive(), true, "客户端在完成身份核验前已退出");
  return { identity, installState: installed.installState, serverVerified: true, webVerified: true, processObservedAlive: true };
}

export function verifyJobArtifacts(workspace, receipt, options = {}) {
  assert.ok(options !== null && typeof options === "object" && !Array.isArray(options), "artifact verification options must be an object");
  const { expectedMode = null, requirePipelineArtifacts = false } = options;
  assert.equal(typeof requirePipelineArtifacts, "boolean", "requirePipelineArtifacts must be a boolean");
  const requireLauncherIntegrity = Object.hasOwn(options, "requireLauncherIntegrity") ? options.requireLauncherIntegrity : false;
  assert.equal(typeof requireLauncherIntegrity, "boolean", "requireLauncherIntegrity must be a boolean");
  if (requirePipelineArtifacts) assert.ok(["daily", "premarket"].includes(expectedMode), "requirePipelineArtifacts requires a daily or premarket expectedMode");
  assert.ok(receipt !== null && typeof receipt === "object" && !Array.isArray(receipt), "receipt must be an object");
  assert.equal(receipt.schemaVersion, "tiger.agent-receipt/v1");
  assert.ok(["succeeded", "degraded"].includes(receipt.status));
  assert.equal(receipt.integrityVerified, true);
  assert.equal(receipt.environment, "simulation");
  assert.equal(receipt.provider, "demo");
  assert.equal(receipt.governanceSynced, false);
  assert.deepEqual(receipt.permissions, { approvals: false, brokerOrders: false, parameterApplication: false });
  if (expectedMode !== null) assert.equal(receipt.mode, expectedMode, "receipt mode must match the requested mode");
  assert.match(receipt.jobId, /^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/u);
  const job = join(workspace, "jobs", "local", receipt.jobId);
  const stored = readFileSync(join(job, "receipt.json"));
  const completion = JSON.parse(readFileSync(join(job, "completion.json"), "utf8"));
  assert.equal(completion.receiptSha256, sha(stored), "completion must bind the immutable receipt");
  const canonical = JSON.parse(stored);
  assert.ok(canonical !== null && typeof canonical === "object" && !Array.isArray(canonical), "canonical receipt must be an object");
  const launcherFields = ["artifactRoot", "launcherIntegrityVerified", "replayed"];
  assert.ok(launcherFields.every((field) => !Object.hasOwn(canonical, field)), "canonical receipt must not contain launcher-only fields");
  assert.ok(Reflect.ownKeys(receipt).every((field) => Object.hasOwn(canonical, field) || launcherFields.includes(field)), "receipt contains an unknown launcher field");
  const returnedCanonical = Object.fromEntries(Object.keys(canonical).map((field) => {
    assert.ok(Object.hasOwn(receipt, field), `receipt is missing canonical field: ${field}`);
    return [field, receipt[field]];
  }));
  assert.deepEqual(canonical, returnedCanonical, "returned canonical fields must match the immutable receipt");
  const artifacts = join(job, "artifacts");
  if (requireLauncherIntegrity || launcherFields.some((field) => Object.hasOwn(receipt, field))) {
    assert.ok(Object.hasOwn(receipt, "artifactRoot") && Object.hasOwn(receipt, "launcherIntegrityVerified"), "launcher must supply artifactRoot and launcherIntegrityVerified together");
    assert.equal(receipt.launcherIntegrityVerified, true, "launcher integrity must be verified");
    assert.equal(receipt.artifactRoot, resolve(artifacts), "launcher artifactRoot must be the canonical job artifacts directory");
    assert.equal(realpathSync(artifacts), receipt.artifactRoot, "launcher artifactRoot must not use a path alias");
    if (Object.hasOwn(receipt, "replayed")) assert.equal(receipt.replayed, true, "launcher replay disclosure must be true");
  }
  for (const entry of receipt.artifacts) {
    const file = resolve(artifacts, entry.name);
    assert.ok(file.startsWith(artifacts + sep), "artifact must remain inside the job");
    const content = readFileSync(file);
    assert.equal(content.length, entry.bytes);
    assert.equal(sha(content), entry.sha256, `artifact changed: ${entry.name}`);
  }
  if (requirePipelineArtifacts) {
    assert.ok(receipt.stepTrace.length >= 21, "all registered pipeline stages must be disclosed");
    assert.ok(receipt.artifacts.some((entry) => entry.role === "pipeline-result"));
    assert.ok(receipt.artifacts.some((entry) => entry.mediaType === "text/html"));
    assert.ok(receipt.artifacts.some((entry) => entry.role === "governance-events"));
    if (receipt.status === "degraded") assert.ok(receipt.degradedSteps.length > 0);
  }
  return { jobId: receipt.jobId, status: receipt.status, artifacts: receipt.artifacts.length,
    kernelDigest: receipt.kernelDigest, stages: receipt.stepTrace?.length ?? 0, degradedSteps: receipt.degradedSteps ?? [] };
}

export async function smokeDesktop({ buildFile, payloadRoot, outputRoot, render = false }) {
  const output = resolve(outputRoot);
  mkdirSync(output, { recursive: true });
  rmSync(join(output, "desktop-smoke.json"), { force: true });
  let build = buildFile ? JSON.parse(readFileSync(resolve(buildFile), "utf8")) : null;
  if (build) {
    assert.equal(build.schemaVersion, "tiger.desktop-build/v1");
    build = { ...build, smokeVerified: false };
    delete build.smokeReceipt;
    writeFileSync(resolve(buildFile), JSON.stringify(build, null, 2) + "\n");
  }
  const nativeTarget = process.platform === "darwin" && process.arch === "arm64" ? "mac-arm64"
    : process.platform === "win32" && process.arch === "x64" ? "win-x64" : null;
  if (!nativeTarget) throw new Error("只能在 Mac arm64 或 Windows x64 原生执行客户运行时冒烟");
  const support = join(output, "support");
  const workspace = join(output, "jobs-workspace");
  mkdirSync(support, { recursive: true });
  mkdirSync(workspace, { recursive: true });
  const environment = { ...process.env, PATH: "", PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" };
  for (const name of Object.keys(environment)) {
    if (/^(?:BUNDLE_SIGNING_|CSC_|APPLE_|OPENAI_|DEEPSEEK_|ARK_|ANTHROPIC_|LLM_|AGENT_GW_|PYTHONPATH$|PYTHONHOME$)/u.test(name)) delete environment[name];
  }
  environment.LLM_MODE = "disabled";
  let runtime = payloadRoot ? resolve(payloadRoot) : support;
  const appChecks = [];
  if (buildFile) {
    assert.equal(`${build.platform}-${build.arch}`, nativeTarget);
    assert.equal(build.signedArchiveVerified, true);
    assert.equal(sha(readFileSync(join(build.resources, "payload.tar.gz"))), build.payloadSha256);
    assert.equal(sha(readFileSync(join(build.resources, "bundle-trust.json"))), build.trustSha256);
    const ports = await Promise.all([availablePort(), availablePort(), availablePort(), availablePort()]);
    assert.equal(new Set(ports).size, 4);
    Object.assign(environment, { WORKLOOM_SUPPORT_DIR: support, WORKLOOM_RESOURCES: build.resources,
      WORKLOOM_PG_PORT: String(ports[0]), WORKLOOM_SERVER_PORT: String(ports[1]), WORKLOOM_WEB_PORT: String(ports[2]), WORKLOOM_NATS_PORT: String(ports[3]) });
    for (const mode of ["bootstrap", ...(render ? ["render-default", "render-safe"] : [])]) {
      const appEnvironment = { ...environment, WORKLOOM_APP_SMOKE: mode === "bootstrap" ? "1" : "0",
        WORKLOOM_RENDER_SMOKE: mode === "bootstrap" ? "0" : "1", WORKLOOM_SAFE_RENDERING: mode === "render-safe" ? "1" : "0",
        WORKLOOM_APP_SMOKE_TEST: "1", WORKLOOM_APP_SMOKE_WAIT_MS: "60000" };
      let readiness = null;
      let handshakeCount = 0;
      const result = await execute(build.executable, [], { environment: appEnvironment, cwd: output, label: mode, timeout: 300000,
        onStdoutLine: async (line, context) => {
          const prefix = "WORKLOOM_APP_SMOKE_READY ";
          if (!line.startsWith(prefix)) return;
          assert.equal(++handshakeCount, 1, "重复的 App ready 握手");
          const frame = JSON.parse(line.slice(prefix.length));
          assert.equal(frame.schemaVersion, "workloom.app-smoke-ready/v1");
          readiness = await verifyRunningDesktop({ supportDir: support, ports: { server: ports[1], web: ports[2] }, build,
            advertised: frame.runtimeIdentity, isAlive: context.isAlive });
          context.sendLine({ schemaVersion: "workloom.app-smoke-release/v1", instanceId: readiness.identity.instanceId });
          context.closeInput();
        },
      });
      writeFileSync(join(output, `${mode}.log`), redactText(result.stdout) + "\n" + result.stderr);
      assert.equal(handshakeCount, 1, "客户端未给出可核验的活进程 ready 握手");
      assert.ok(readiness?.processObservedAlive);
      const checkpoint = JSON.parse(readFileSync(join(support, "install-state.json"), "utf8"));
      assert.notEqual(checkpoint.phase, "ready", "停止后的客户端不可仍称 ready");
      assert.notEqual(checkpoint.status, "complete", "停止后的客户端不可仍称当前安装就绪");
      assert.equal(checkpoint.runtimeIdentity, undefined, "停止后的运行身份必须已撤销");
      appChecks.push({ mode, code: result.code, ...readiness, afterStop: { status: checkpoint.status, phase: checkpoint.phase, identityCleared: true } });
    }
    runtime = support;
  } else if (!payloadRoot) throw new Error("必须提供最终客户端 build receipt 或明确的仅内核 payload 路径");
  const kernelInventory = assertKernelPayload(runtime, nativeTarget);
  const kernel = join(runtime, "runtime", "kernel");
  const python = join(runtime, "python", process.platform === "win32" ? "python.exe" : "bin/python3");
  const node = join(runtime, "node", process.platform === "win32" ? "node.exe" : "bin/node");
  Object.assign(environment, { TIGER_KERNEL_ROOT: kernel, TIGER_PYTHON_EXE: python, TIGER_NODE_EXE: node });
  const imports = await execute(python, ["-I", "-c", "import pandas,numpy,requests,yfinance,yaml; print('tiger-python-ready')"],
    { environment, cwd: workspace, label: "offline Python imports", timeout: 60000 });
  assert.equal(imports.stdout.trim(), "tiger-python-ready");
  const request = { operation: "pipeline", mode: "daily", environment: "simulation", provider: "demo", llmMode: "disabled",
    idempotencyKey: "desktopcli001", timeoutSeconds: 120, topN: 10, maxPicks: 2 };
  const cli = await execute(node, [join(kernel, "scripts", "tiger-agent.mjs"), "--workspace", workspace, "run", "--json", JSON.stringify(request)],
    { environment, cwd: workspace, label: "packaged CLI daily pipeline", codes: [0, 10] });
  const cliReceipt = JSON.parse(cli.stdout.trim());
  const cliCheck = verifyJobArtifacts(workspace, cliReceipt, { expectedMode: request.mode, requirePipelineArtifacts: true, requireLauncherIntegrity: true });
  writeFileSync(join(output, "cli-receipt.json"), JSON.stringify(cliReceipt, null, 2) + "\n");
  const sourceJob = { jobId: cliReceipt.jobId, resultSha256: cliReceipt.resultSha256 };
  const cliModes = { daily: cliCheck }, cliModeReceipts = { daily: cliReceipt };
  for (const mode of ["premarket", "intraday", "backtest", "tune", "review"]) {
    const selected = { ...request, mode, idempotencyKey: `desktopcli-${mode}`,
      ...(["intraday", "review"].includes(mode) ? { sourceJob } : {}),
      ...(mode === "intraday" ? { cycles: 1, intervalSeconds: 0 } : {}),
      ...(["backtest", "tune"].includes(mode) ? { btDays: 60 } : {}),
      ...(mode === "tune" ? { trainDays: 20, testDays: 10, stepDays: 10 } : {}) };
    const executed = await execute(node, [join(kernel, "scripts", "tiger-agent.mjs"), "--workspace", workspace,
      "run", "--json", JSON.stringify(selected)], { environment, cwd: workspace, label: `packaged CLI ${mode}`, codes: [0, 10] });
    const receipt = JSON.parse(executed.stdout.trim());
    assert.equal(receipt.mode, mode);
    cliModes[mode] = verifyJobArtifacts(workspace, receipt, { expectedMode: mode, requirePipelineArtifacts: mode === "premarket", requireLauncherIntegrity: true });
    cliModeReceipts[mode] = receipt;
  }
  writeFileSync(join(output, "cli-mode-receipts.json"), JSON.stringify(cliModeReceipts, null, 2) + "\n");
  const { operation, ...toolRequest } = request;
  const messages = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "tiger-desktop-smoke", version: "1.0.0" } } },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "tiger.pipeline.run", arguments: { ...toolRequest, idempotencyKey: "desktopmcp001" } } },
    { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "tiger.employee.run", arguments: { environment: "simulation", provider: "demo", llmMode: "disabled", employee: "mrs", idempotencyKey: "desktopmrs001", timeoutSeconds: 120 } } },
  ];
  const mcp = await execute(node, [join(kernel, "scripts", "tiger-agent-mcp.mjs"), "--workspace", workspace], {
    environment, cwd: workspace, label: "packaged MCP pipeline and employee", input: messages.map((message) => JSON.stringify(message)).join("\n") + "\n",
  });
  const frames = mcp.stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.ok(frames.every((frame) => frame.jsonrpc === "2.0" && !frame.error));
  const frame = (id) => { const found = frames.filter((item) => item.id === id); assert.equal(found.length, 1); return found[0]; };
  assert.ok(frame(2).result.tools.some((tool) => tool.name === "tiger.pipeline.run"));
  assert.equal(frame(3).result.isError, false);
  assert.equal(frame(4).result.isError, false);
  const mcpCheck = verifyJobArtifacts(workspace, frame(3).result.structuredContent, { expectedMode: toolRequest.mode, requirePipelineArtifacts: true, requireLauncherIntegrity: true });
  const employeeCheck = verifyJobArtifacts(workspace, frame(4).result.structuredContent, { requireLauncherIntegrity: true });
  assert.equal(frame(4).result.structuredContent.employee, "mrs");
  writeFileSync(join(output, "mcp-frames.json"), JSON.stringify(frames, null, 2) + "\n");
  const employees = { mrs: employeeCheck };
  for (const employee of ["scanner", "risk", "review"]) {
    const arguments_ = { environment: "simulation", provider: "demo", llmMode: "disabled", employee,
      idempotencyKey: `desktopmcp-${employee}`, timeoutSeconds: 120, topN: 10, maxPicks: 2,
      ...(employee === "review" ? { sourceJob } : {}) };
    const messages_ = [messages[0], messages[1], messages[2],
      { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "tiger.employee.run", arguments: arguments_ } }];
    const executed = await execute(node, [join(kernel, "scripts", "tiger-agent-mcp.mjs"), "--workspace", workspace], {
      environment, cwd: workspace, label: `packaged MCP employee ${employee}`,
      input: messages_.map((message) => JSON.stringify(message)).join("\n") + "\n",
    });
    const frames_ = executed.stdout.trim().split("\n").map((line) => JSON.parse(line));
    assert.ok(frames_.every((item) => item.jsonrpc === "2.0" && !item.error));
    for (const id of [1, 2, 3]) assert.equal(frames_.filter((item) => item.id === id).length, 1);
    const reply = frames_.find((item) => item.id === 3).result;
    assert.equal(reply.isError, false); assert.equal(reply.structuredContent.employee, employee);
    employees[employee] = verifyJobArtifacts(workspace, reply.structuredContent, { requireLauncherIntegrity: true });
    writeFileSync(join(output, `mcp-${employee}-frames.json`), JSON.stringify(frames_, null, 2) + "\n");
  }
  const result = { schemaVersion: "tiger.desktop-smoke/v1", target: nativeTarget, emptyPath: true,
    finalAppVerified: Boolean(build), platformSigned: build ? !build.unsignedPlatform : false,
    candidate: build?.candidate ?? true, payloadSha256: build?.payloadSha256 ?? null,
    offlineImports: true, kernelInventory, appChecks, cli: cliCheck, cliModes, mcp: mcpCheck, employee: employeeCheck, employees,
    source: build ? "final-client-resources" : "assembled-industry-payload-only", checkedAt: new Date().toISOString() };
  writeFileSync(join(output, "desktop-smoke.json"), JSON.stringify(result, null, 2) + "\n");
  if (build) writeFileSync(resolve(buildFile), JSON.stringify({ ...build, smokeVerified: true, smokeReceipt: join(output, "desktop-smoke.json") }, null, 2) + "\n");
  return result;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await smokeDesktop({ buildFile: arg("--build"), payloadRoot: arg("--payload"),
      outputRoot: arg("--output", join(ROOT, "release", "tiger-smoke")), render: process.argv.includes("--render") });
    console.log(`Tiger 桌面原生冒烟通过：${JSON.stringify(result)}`);
  } catch (error) { console.error(redactText(error instanceof Error ? error.stack : String(error))); process.exitCode = 1; }
}
