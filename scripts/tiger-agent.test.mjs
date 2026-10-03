import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { existsSync, mkdtempSync, readFileSync, realpathSync } from "node:fs";
import { chmod, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { after, before, test } from "node:test";
import { setTimeout as pause } from "node:timers/promises";
import { createRepoCapabilities } from "../bundles/trading/capabilities/repo.mjs";
import { invokeCapability, loadCatalog } from "./agent-capabilities.mjs";
import { normalizeRequest, runRequest, TOOLS } from "./tiger-agent-runtime.mjs";

const root = resolve(import.meta.dirname, "..");
const python = process.env.TIGER_TEST_PYTHON ?? process.env.TIGER_PYTHON_EXE ?? join(root, ".venv/bin/python");
const fixture = mkdtempSync(join(realpathSync(process.env.TIGER_TEST_TMPDIR ?? tmpdir()), "tiger-agent-test-"));
const workspace = join(fixture, "workspace");
const cli = join(root, "scripts/tiger-agent.mjs");
const mcp = join(root, "scripts/tiger-agent-mcp.mjs");
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");
const base = { environment: "simulation", provider: "demo", topN: 5, maxPicks: 3, account: 250_000, timeoutSeconds: 120 };
const profileLimits = { risk_r_pct: .004, max_single_position_pct: .10, gross_cap: .50 };
const env = { ...process.env, TIGER_KERNEL_ROOT: root, TIGER_PYTHON_EXE: python };
let dailyReceipt;
let sourceJob;
let ledgerBefore;

function snapshots() {
  const reportRoot = join(root, "reports");
  return ["journal.json", "sim_portfolio.json", "tuned_params.json"].map((name) => {
    const path = name === "tuned_params.json" ? join(root, name) : join(reportRoot, name);
    return { path, digest: existsSync(path) ? hash(readFileSync(path)) : null };
  });
}

before(() => {
  assert.ok(existsSync(python), "Set TIGER_TEST_PYTHON to the installed kernel/bundled Python executable");
  ledgerBefore = snapshots();
});
after(async () => {
  assert.deepEqual(snapshots(), ledgerBefore, "Facade must not mutate the kernel's public accounting state or tuned parameters");
  if (process.env.TIGER_TEST_KEEP_WORKSPACE === "1") process.stdout.write(`# retained fixture: ${fixture}\n`);
  else await rm(fixture, { recursive: true, force: true });
});

async function command(args, input, overrides = {}, selectedWorkspace = workspace) {
  const child = spawn(process.execPath, [cli, "--workspace", selectedWorkspace, ...args],
    { cwd: fixture, env: { ...env, ...overrides }, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdin.end(input);
  const [code] = await once(child, "close");
  assert.equal(stderr, "", "CLI must not leak upstream logs to stderr");
  return { code, stdout, value: JSON.parse(stdout) };
}

function run(mode, key, extra = {}, overrides = {}) {
  const input = { ...base, operation: "pipeline", mode, idempotencyKey: key, ...extra };
  return command(["run", "--input-file", "-"], JSON.stringify(input), overrides);
}
function employee(name, key, extra = {}) {
  return command(["run", "--json", JSON.stringify({ ...base, operation: "employee", employee: name,
    idempotencyKey: key, ...extra })]);
}
async function verified(receipt) {
  assert.equal(receipt.integrityVerified, true);
  assert.equal(receipt.launcherIntegrityVerified, true);
  assert.equal(receipt.receipt.scope, "local-kernel");
  assert.equal(receipt.receipt.synced, true);
  assert.equal(receipt.governanceSynced, false);
  const result = receipt.artifacts.find((artifact) => artifact.name === receipt.resultArtifact.name);
  assert.equal(receipt.resultSha256, result.sha256);
  assert.equal(receipt.resultArtifact.role, result.role);
  for (const [name, value] of Object.entries(receipt.riskLimits)) {
    assert.ok(value > 0 && value <= receipt.requestedRiskLimits[name]);
    assert.equal(receipt.gateParams[name], value);
  }
  for (const artifact of receipt.artifacts) {
    const data = await readFile(join(receipt.artifactRoot, artifact.name));
    assert.equal(data.length, artifact.bytes);
    assert.equal(hash(data), artifact.sha256);
  }
}

test("dedicated catalog exposes six real modes, four directed employees and bounded job tools", async () => {
  const result = await command(["catalog"]);
  assert.equal(result.code, 0);
  assert.equal(result.value.tools.length, 5);
  assert.deepEqual(result.value.tools[0].inputSchema.properties.mode.enum,
    ["daily", "premarket", "intraday", "backtest", "tune", "review"]);
  assert.deepEqual(result.value.boundaries.employees, ["scanner", "mrs", "risk", "review"]);
  assert.equal(result.value.boundaries.brokerOrders, false);
  assert.equal(TOOLS.find((tool) => tool.name === "tiger.pipeline.run").annotations.readOnlyHint, false);
});

test("strict request validation rejects live/approval/path/identity fields, nonfinite numbers and wrong-mode options", () => {
  const input = { ...base, operation: "pipeline", mode: "daily", idempotencyKey: "validate001" };
  const cases = [null, [], { ...input, environment: "live" }, { ...input, approve: true },
    { ...input, account: NaN }, { ...input, account: Infinity }, { ...input, topN: 0 },
    { ...input, maxPicks: 26 }, { ...input, account: true }, { ...input, tenantId: "other" },
    { ...input, out: "/outside" }, { ...input, command: "echo" }, { ...input, sourceJob: {} },
    { ...input, idempotencyKey: "../../escape" }, { ...input, idempotencyKey: "unicode／001" },
    { ...input, mode: "tune", market: "cn" }, { ...input, mode: "review-approve" },
    { ...input, mode: "tune", btDays: 30, trainDays: 30, testDays: 10 },
    { ...input, operation: "employee", employee: "broker", mode: undefined }];
  for (const candidate of cases) assert.throws(() => normalizeRequest(candidate));
  assert.throws(() => normalizeRequest({ ...input, mode: "intraday" }), /sourceJob/u);
  assert.equal(normalizeRequest(input).provider, "demo");
});

test("profile risk limits are strictly bounded, normalized and published in the MCP input schema", () => {
  const input = { ...base, operation: "pipeline", mode: "daily", idempotencyKey: "risklimits001" };
  const limits = { risk_r_pct: .004, max_single_position_pct: .10, gross_cap: .50 };
  assert.deepEqual(normalizeRequest({ ...input, riskLimits: limits }).riskLimits, limits);
  assert.deepEqual(normalizeRequest(input).riskLimits, { risk_r_pct: .008, max_single_position_pct: .20, gross_cap: .90 });
  for (const invalid of [null, [], {}, { ...limits, unknown: .1 }, { ...limits, risk_r_pct: 0 },
    { ...limits, risk_r_pct: NaN }, { ...limits, risk_r_pct: true }, { ...limits, risk_r_pct: .009 },
    { ...limits, max_single_position_pct: .21 }, { ...limits, gross_cap: .91 }]) {
    assert.throws(() => normalizeRequest({ ...input, riskLimits: invalid }));
  }
  for (const name of ["tiger.pipeline.run", "tiger.employee.run"]) {
    const schema = TOOLS.find((tool) => tool.name === name).inputSchema.properties.riskLimits;
    assert.deepEqual(schema.required, Object.keys(limits));
    assert.equal(schema.additionalProperties, false);
  }
});

test("root and governance readonly catalogs expose the same actual trading bundle and block symlink docs", async () => {
  const catalogs = await Promise.all([loadCatalog(root), loadCatalog(join(root, "governance"))]);
  assert.deepEqual([...catalogs[0].entries.keys()], [...catalogs[1].entries.keys()]);
  const presets = (await readdir(join(root, "governance/bundles/trading/presets"))).filter((name) => name.endsWith(".yml"));
  for (const [index, catalog] of catalogs.entries()) {
    const output = await invokeCapability(catalog, "trading.bundle.summary", {});
    assert.equal(output.result.version, "0.1.0");
    assert.equal(output.result.counts.presets, presets.length);
    assert.equal(output.result.sourcePath, index === 0 ? "governance/bundles/trading" : "bundles/trading");
  }
  const repo = join(fixture, "readonly");
  await mkdir(join(repo, "docs"), { recursive: true });
  const outside = join(fixture, "outside.md");
  await writeFile(outside, "# task-owned outside marker\n");
  await symlink(outside, join(repo, "docs/escape.md"));
  assert.throws(() => createRepoCapabilities(repo).repoDocs({}), /符号链接/u);
  await rm(join(repo, "docs/escape.md"));
  await symlink(join(fixture, "not-present"), join(repo, "docs/broken.md"));
  assert.throws(() => createRepoCapabilities(repo).repoDocs({}), /符号链接/u);
  await rm(join(repo, "docs"), { recursive: true });
  await mkdir(join(fixture, "external-docs"));
  await symlink(join(fixture, "external-docs"), join(repo, "docs"));
  assert.throws(() => createRepoCapabilities(repo).repoDocs({}), /符号链接/u);
});

test("real daily CLI executes all registered steps, writes native reports, records degraded LLM and verifies every SHA", async () => {
  const result = await run("daily", "dailytest001");
  assert.equal(result.code, 10, JSON.stringify(result.value.error));
  dailyReceipt = result.value;
  assert.equal(dailyReceipt.status, "degraded");
  assert.equal(dailyReceipt.dataMode, "synthetic");
  assert.equal(dailyReceipt.stepTrace.length, 21);
  assert.ok(dailyReceipt.degradedSteps.includes("clean.llm_semantic"));
  assert.ok(dailyReceipt.stepTrace.every((step) => ["executed", "passthrough"].includes(step.status)));
  assert.equal(dailyReceipt.stepTrace.at(-1).step, "review.daily");
  for (const suffix of [".html", ".md", ".json", ".jsonl"]) assert.ok(dailyReceipt.artifacts.some((artifact) => artifact.name.endsWith(suffix)));
  assert.ok(dailyReceipt.artifacts.some((artifact) => artifact.name.startsWith("复盘_")));
  await verified(dailyReceipt);
  const portfolio = JSON.parse(await readFile(join(dailyReceipt.artifactRoot, "sim_portfolio.json"), "utf8"));
  assert.equal(portfolio.initial_cash, base.account);
  assert.equal(portfolio.cash, base.account);
  assert.equal(portfolio.positions.length, 0, "Today's close signals cannot fill on today's earlier open");
  assert.equal(portfolio.pending.length, dailyReceipt.summary.picks);
  const artifact = dailyReceipt.artifacts.find((entry) => entry.role === "pipeline-result");
  sourceJob = { jobId: dailyReceipt.jobId, resultSha256: artifact.sha256 };
});

test("real premarket mode adds its actual plan and returns a fully verified degraded receipt", async () => {
  const result = await run("premarket", "premarket001");
  assert.equal(result.code, 10, JSON.stringify(result.value.error));
  assert.ok(result.value.artifacts.some((artifact) => artifact.name.startsWith("盘前计划_")));
  await verified(result.value);
});

test("real intraday and daily/weekly review use verified source artifacts and preserve the source journal", async () => {
  const original = hash(await readFile(join(dailyReceipt.artifactRoot, "journal.json")));
  const intraday = await run("intraday", "intraday001", { sourceJob, cycles: 1, intervalSeconds: 0 });
  assert.equal(intraday.code, 0, JSON.stringify(intraday.value.error));
  await verified(intraday.value);
  const alertResult = JSON.parse(await readFile(join(intraday.value.artifactRoot, "intraday.json"), "utf8"));
  assert.ok(Array.isArray(alertResult.alerts));
  assert.equal(alertResult.cycles, 1);
  assert.equal(alertResult.quoteCoverage.requested, dailyReceipt.summary.picks);
  assert.equal(alertResult.quoteCoverage.ready, dailyReceipt.summary.picks);
  for (const frequency of ["daily", "weekly"]) {
    const review = await run("review", `review${frequency}001`, { sourceJob, reviewFrequency: frequency });
    assert.equal(review.code, 0, JSON.stringify(review.value.error));
    await verified(review.value);
    assert.equal(review.value.summary.approvalsExecuted, false);
  }
  assert.equal(hash(await readFile(join(dailyReceipt.artifactRoot, "journal.json"))), original);
});

test("an actual minimum-account daily job can produce no watch positions and a valid empty intraday receipt", async () => {
  const empty = await run("daily", "emptywatch001", { account: 100, topN: 1, maxPicks: 1 });
  assert.equal(empty.code, 10, JSON.stringify(empty.value.error));
  assert.equal(empty.value.summary.picks, 0);
  const result = empty.value.artifacts.find((item) => item.role === "pipeline-result");
  const intraday = await run("intraday", "emptyintraday001", { account: 100, topN: 1, maxPicks: 1,
    sourceJob: { jobId: empty.value.jobId, resultSha256: result.sha256 } });
  assert.equal(intraday.code, 0, JSON.stringify(intraday.value.error));
  await verified(intraday.value);
  assert.equal(intraday.value.summary.quoteCoverage.requested, 0);
  const blob = JSON.parse(await readFile(join(intraday.value.artifactRoot, "intraday.json"), "utf8"));
  assert.deepEqual(blob.watch, []);
  assert.deepEqual(blob.alerts, []);
});

test("real historical backtest produces statistics within the declared default research budget", async () => {
  const backtest = await run("backtest", "backtest001", { btDays: 45, timeoutSeconds: 300, riskLimits: profileLimits });
  assert.equal(backtest.code, 0, JSON.stringify(backtest.value.error));
  await verified(backtest.value);
  const output = JSON.parse(await readFile(join(backtest.value.artifactRoot, "backtest.json"), "utf8"));
  assert.equal(output.n_days, 45);
  assert.equal(output.account_usd, base.account);
  assert.equal(output.params.max_picks, base.maxPicks);
  assert.deepEqual(backtest.value.requestedRiskLimits, profileLimits);
  assert.deepEqual(output.riskLimits, backtest.value.riskLimits);
  assert.ok(Array.isArray(output.trades));
});

test("real WFA uses the declared default budget and produces a research proposal without applying parameters", async () => {
  const tune = await run("tune", "tuningtest001", { btDays: 55, trainDays: 30,
    testDays: 10, stepDays: 10, timeoutSeconds: 300, riskLimits: profileLimits });
  assert.equal(tune.code, 0, JSON.stringify(tune.value.error));
  await verified(tune.value);
  const wfa = JSON.parse(await readFile(join(tune.value.artifactRoot, "wfa.json"), "utf8"));
  assert.equal(wfa.account_usd, base.account);
  assert.equal(wfa.base_params.max_picks, base.maxPicks);
  assert.deepEqual(tune.value.requestedRiskLimits, profileLimits);
  assert.deepEqual(wfa.riskLimits, tune.value.riskLimits);
  const proposal = JSON.parse(await readFile(join(tune.value.artifactRoot, "wfa_research_proposal.json"), "utf8"));
  assert.equal(proposal.kind, "wfa_research");
  assert.equal(proposal.status, "pending_research_review");
  assert.equal(proposal.parametersApplied, false);
  assert.equal(typeof proposal.eligibleForApproval, "boolean");
  assert.ok(!existsSync(join(tune.value.artifactRoot, "tuned_params.json")));
});

test("historical collection obeys a caller's shorter hard budget and persists a verified timeout outcome", async () => {
  const outcome = await run("backtest", "historytimeout001", { btDays: 45, timeoutSeconds: 2 });
  assert.equal(outcome.code, 1);
  assert.equal(outcome.value.status, "timed_out");
  assert.equal(outcome.value.integrityVerified, false);
  const read = await command(["get", "historytimeout001"]);
  assert.equal(read.value.status, "timed_out");
  const owner = JSON.parse(await readFile(join(workspace, "jobs/local/historytimeout001/owner.json"), "utf8"));
  assert.throws(() => process.kill(owner.pid, 0));
});

test("four directed employees call their actual implementations and declare risk prerequisites", async () => {
  for (const name of ["scanner", "mrs", "risk", "review"]) {
    const result = await employee(name, `employee${name}001`, name === "review" ? { sourceJob } : { riskLimits: profileLimits });
    assert.ok([0, 10].includes(result.code), JSON.stringify(result.value.error));
    assert.equal(result.value.employee, name);
    assert.ok(result.value.stepTrace.some((step) => step.step === (name === "review" ? "review.chief.daily" : `employee.${name}.execute`)));
    await verified(result.value);
    if (name === "risk") {
      const data = JSON.parse(await readFile(join(result.value.artifactRoot, "employee_result.json"), "utf8"));
      assert.ok(data.prerequisites.includes("pipeline.run_pipeline"));
      assert.deepEqual(result.value.requestedRiskLimits, profileLimits);
      for (const pick of data.result) {
        assert.ok(pick.shares * pick.entry_price <= base.account * profileLimits.max_single_position_pct + 1e-8);
        assert.ok(pick.risk_usd <= base.account * profileLimits.risk_r_pct + 1e-8);
      }
    }
  }
});

test("replay preserves receipt and artifacts; changed inputs and concurrent same-key requests cannot duplicate execution", async () => {
  const replay = await run("daily", "dailytest001");
  assert.equal(replay.code, 10);
  assert.equal(replay.value.replayed, true);
  assert.equal(replay.value.finishedAt, dailyReceipt.finishedAt);
  assert.deepEqual(replay.value.artifacts, dailyReceipt.artifacts);
  const conflict = await run("daily", "dailytest001", { topN: 6 });
  assert.equal(conflict.code, 1);
  assert.equal(conflict.value.error.code, "IDEMPOTENCY_CONFLICT");
  const outputs = await Promise.all([run("daily", "concurrent001"), run("daily", "concurrent001")]);
  assert.ok(outputs.some((output) => output.code === 10));
  assert.ok(outputs.every((output) => output.code === 10 || output.value.error.code === "IN_PROGRESS"));
  const dirs = (await readdir(join(workspace, "jobs/local"))).filter((entry) => entry === "concurrent001");
  assert.equal(dirs.length, 1);
  const receipt = await command(["get", "concurrent001"]);
  await verified(receipt.value);
});

test("wrong source SHA/environment and tampered artifacts fail closed; arbitrary artifact names are rejected", async () => {
  const wrong = await run("intraday", "badsource001", { sourceJob: { ...sourceJob, resultSha256: "0".repeat(64) } });
  assert.equal(wrong.code, 1);
  assert.equal(wrong.value.status, "failed");
  assert.equal(wrong.value.integrityVerified, false);
  const mismatch = await run("review", "wrongenv001", { sourceJob, environment: "paper" });
  assert.equal(mismatch.code, 1);
  assert.equal(mismatch.value.error.code, "INVALID_SOURCE_JOB");
  const escape = await command(["artifacts", "dailytest001", "../../outside.md"]);
  assert.equal(escape.code, 1);
  assert.equal(escape.value.error.code, "NOT_FOUND");
  const target = join(dailyReceipt.artifactRoot, "journal.json");
  const original = await readFile(target);
  try {
    await writeFile(target, "[]\n");
    const corrupted = await command(["get", "dailytest001"]);
    assert.equal(corrupted.code, 1);
    assert.equal(corrupted.value.error.code, "INTEGRITY_ERROR");
  } finally { await writeFile(target, original); }
  const completion = join(workspace, "jobs/local/dailytest001/completion.json");
  const checksum = await readFile(completion);
  try {
    await rm(completion);
    const incomplete = await command(["get", "dailytest001"]);
    assert.equal(incomplete.code, 1);
    assert.equal(incomplete.value.error.code, "INTEGRITY_ERROR");
  } finally { await writeFile(completion, checksum); }
});

test("uninitialized nonempty/foreign workspaces and symlink components are rejected before kernel execution", async () => {
  const nonempty = join(fixture, "nonempty");
  await mkdir(nonempty);
  await writeFile(join(nonempty, "journal.json"), "[]\n");
  const unsafe = await command(["run", "--json", JSON.stringify({ ...base, operation: "pipeline", mode: "daily", idempotencyKey: "unsafe001" })], undefined, {}, nonempty);
  assert.equal(unsafe.code, 1);
  assert.equal(unsafe.value.error.code, "UNSAFE_WORKSPACE");
  assert.ok(!existsSync(join(nonempty, ".tiger-agent-workspace.json")));
  const link = join(fixture, "workspace-link");
  await symlink(workspace, link);
  const child = spawn(process.execPath, [cli, "--workspace", link, "run", "--json", JSON.stringify({ ...base, operation: "pipeline", mode: "daily", idempotencyKey: "symlink001" })], { env });
  let stdout = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  const [code] = await once(child, "close");
  assert.equal(code, 1);
  assert.equal(JSON.parse(stdout).error.code, "UNSAFE_PATH");
  const foreign = await command(["--tenant", "another", "get", "dailytest001"]);
  assert.equal(foreign.code, 1);
  assert.equal(foreign.value.error.code, "UNSAFE_WORKSPACE");
});

test("configured local failure fixture does real HTTP calls, degrades, and never persists echoed credentials", async () => {
  const canary = "task-only-secret-cli-mcp-8721904";
  let calls = 0;
  const server = createServer((req, res) => {
    calls += 1;
    req.resume();
    res.statusCode = 503;
    res.end(`upstream echoed ${canary} Bearer ${canary}`);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    const result = await run("daily", "redaction001", { llmMode: "configured" }, {
      LLM_BACKEND: "api", LLM_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`,
      LLM_MODEL: "task-owned-failure-fixture", LLM_API_KEY: canary,
    });
    assert.equal(result.code, 10, JSON.stringify(result.value.error));
    assert.ok(calls > 0);
    assert.ok(!result.stdout.includes(canary));
    for (const artifact of result.value.artifacts) assert.ok(!(await readFile(join(result.value.artifactRoot, artifact.name), "utf8")).includes(canary));
    assert.ok(result.value.stepTrace.some((step) => step.status === "passthrough" && step.note.length > 0),
      "A failed configured model must disclose its passthrough; sanitized errors may omit upstream text entirely");
  } finally { await new Promise((resolveClose) => server.close(resolveClose)); }
});

test("real long intraday timeout kills the kernel, persists timed_out and never leaves a running job", async () => {
  const result = await run("intraday", "timeoutjob001", { sourceJob, cycles: 100, intervalSeconds: 1, timeoutSeconds: 2 });
  assert.equal(result.code, 1);
  assert.equal(result.value.status, "timed_out");
  assert.equal(result.value.integrityVerified, false);
  const receipt = await command(["get", "timeoutjob001"]);
  assert.equal(receipt.value.status, "timed_out");
  const owner = JSON.parse(await readFile(join(workspace, "jobs/local/timeoutjob001/owner.json"), "utf8"));
  assert.throws(() => process.kill(owner.pid, 0));
});

test("bounded CLI input rejects oversized, malformed and special-character requests before creating jobs", async () => {
  for (const input of ["x".repeat(65 * 1024), "{bad-json", JSON.stringify({ ...base, operation: "pipeline", mode: "daily", idempotencyKey: "unicode／001" })]) {
    const outcome = await command(["run", "--input-file", "-"], input);
    assert.equal(outcome.code, 1);
    assert.ok(["INPUT_TOO_LARGE", "INVALID_INPUT"].includes(outcome.value.error.code));
  }
  assert.ok(!(await readdir(join(workspace, "jobs/local"))).some((name) => name.includes("unicode")));
});

test("a crashed real kernel is reported orphaned and cannot replay or claim running forever", async () => {
  const pending = run("daily", "crashedkernel001");
  const ownerPath = join(workspace, "jobs/local/crashedkernel001/owner.json");
  for (let index = 0; index < 400 && !existsSync(ownerPath); index += 1) await pause(25);
  assert.ok(existsSync(ownerPath));
  const owner = JSON.parse(await readFile(ownerPath, "utf8"));
  process.kill(owner.pid, "SIGKILL");
  const crashed = await pending;
  assert.equal(crashed.code, 1);
  const read = await command(["get", "crashedkernel001"]);
  assert.equal(read.code, 1);
  assert.equal(read.value.error.code, "ORPHANED_JOB");
  const replay = await run("daily", "crashedkernel001");
  assert.equal(replay.code, 1);
  assert.equal(replay.value.error.code, "ORPHANED_JOB");
});

test("timeout kills a real TERM-ignoring descendant even after the direct kernel exits", { skip: process.platform === "win32" }, async () => {
  const kernel = join(fixture, "descendant-kernel");
  const packagePath = join(kernel, "trading_system");
  await mkdir(packagePath, { recursive: true });
  const pidPath = join(kernel, "task-owned-descendant.pid");
  await writeFile(join(packagePath, "__init__.py"), `__path__.append(${JSON.stringify(join(root, "trading_system"))})\n`);
  await writeFile(join(packagePath, "agent_api.py"), `import importlib.util, pathlib, subprocess, sys\n` +
    `spec = importlib.util.spec_from_file_location("trading_system.actual_agent_api", ${JSON.stringify(join(root, "trading_system/agent_api.py"))})\n` +
    `api = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(api)\n` +
    `if "--get" not in sys.argv and "--interrupted" not in sys.argv:\n` +
    `    child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n` +
    `    pathlib.Path(${JSON.stringify(pidPath)}).write_text(str(child.pid))\n` +
    `raise SystemExit(api.main())\n`);
  let descendant;
  try {
    const outcome = await run("daily", "processtree001", { timeoutSeconds: 2 }, { TIGER_KERNEL_ROOT: kernel });
    assert.equal(outcome.code, 1);
    assert.equal(outcome.value.status, "timed_out", JSON.stringify(outcome.value.error));
    descendant = Number(await readFile(pidPath, "utf8"));
    assert.ok(Number.isSafeInteger(descendant) && descendant > 0);
    for (let index = 0; index < 40; index += 1) {
      try { process.kill(descendant, 0); } catch { break; }
      await pause(25);
    }
    assert.throws(() => process.kill(descendant, 0), "Descendant must be stopped, including one that ignored SIGTERM");
  } finally {
    if (descendant) { try { process.kill(descendant, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; } }
  }
});

function processAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === "ESRCH") return false; throw error; }
}

async function waitForControl(check, message, timeoutMs = 3500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    await pause(20);
  }
  assert.fail(message);
}

async function boundedControl(promise, timeoutMs, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, rejectDeadline) => {
      timer = setTimeout(() => rejectDeadline(new Error(message)), timeoutMs);
    })]);
  } finally { clearTimeout(timer); }
}

async function unrelatedProcess() {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000); process.send('ready');"],
    { stdio: ["ignore", "ignore", "ignore", "ipc"] });
  const closed = once(child, "close");
  try { await boundedControl(once(child, "message"), 3500, "Unrelated fixture must report IPC readiness"); }
  catch (error) { child.kill("SIGKILL"); await closed; throw error; }
  // Keep IPC attached until termination. An early parent disconnect can
  // suppress the real close event on supported Node releases (nodejs#65646).
  return { pid: child.pid, async close() {
    child.kill("SIGKILL");
    await boundedControl(closed, 3500, "Unrelated fixture must close after its own cleanup");
  } };
}

async function exitedLeaderCase(context, reason) {
  const directory = join(fixture, `exited-leader-${reason}`);
  await mkdir(directory);
  const pids = join(directory, "pids.json");
  const interrupt = join(directory, "interrupt.json");
  const releaseOutput = join(directory, "release-output");
  const executable = join(directory, "fixture-python");
  const descendant = `const fs = require('node:fs');
    process.on('SIGTERM', () => {});
    let emitted = false;
    setInterval(() => {
      if (!emitted && fs.existsSync(${JSON.stringify(releaseOutput)})) {
        emitted = true;
        process.stdout.write(Buffer.alloc(3 * 1024 * 1024, 120));
      }
    }, 10);
    process.send('ready');`;
  // This executable injects only process ownership/stdio failures. It never
  // produces a successful receipt or pretends to execute the trading kernel.
  await writeFile(executable, `#!${process.execPath}\n
    const fs = require('node:fs');
    if (process.argv.includes('--interrupted')) {
      const status = process.argv[process.argv.indexOf('--interrupted') + 1];
      const reason = process.argv[process.argv.indexOf('--stop-reason') + 1];
      fs.writeFileSync(${JSON.stringify(interrupt)}, JSON.stringify({ status, reason }));
      console.log(JSON.stringify({ status, integrityVerified: false }));
      process.exit(0);
    }
    const { spawn } = require('node:child_process');
    const save = value => fs.writeFileSync(${JSON.stringify(pids)}, JSON.stringify(value));
    save({ leader: process.pid, entered: true });
    const child = spawn(process.execPath, ['-e', ${JSON.stringify(descendant)}],
      { stdio: ['ignore', 'inherit', 'inherit', 'ipc'] });
    child.once('error', error => save({ leader: process.pid, errorCode: error.code }));
    let ready = false;
    child.once('exit', code => { if (!ready) save({ leader: process.pid, descendantExitCode: code }); });
    child.once('message', () => {
      ready = true;
      save({ leader: process.pid, descendant: child.pid });
      console.log(JSON.stringify({ status: 'running' }));
      process.exit(0);
    });\n`);
  await chmod(executable, 0o700);
  const other = await unrelatedProcess();
  const controller = new AbortController();
  const started = Date.now();
  const outcome = runRequest({ workspace: join(directory, "workspace"), tenant: "local", kernel: root, python: executable },
    { operation: "pipeline", mode: "daily", environment: "simulation", idempotencyKey: `leader${reason}001`,
      timeoutSeconds: reason === "timed_out" ? 5 : 30 }, { signal: controller.signal })
    .then((value) => ({ value }), (error) => ({ error }));
  const control = { reason, leaderEntered: false, descendantReady: false, leaderExited: false,
    descendantAliveBeforeTrigger: false, unrelatedAlive: false, reachedOuterDeadline: false };
  let state;
  let deadline;
  try {
    await waitForControl(async () => {
      if (!existsSync(pids)) return false;
      try { state = JSON.parse(await readFile(pids, "utf8")); }
      catch (error) { if (error instanceof SyntaxError) return false; throw error; }
      control.leaderEntered = true;
      return control.descendantReady = Number.isSafeInteger(state.descendant);
    }, "Fixture must reach its actual IPC readiness before exercising launcher shutdown");
    await waitForControl(() => !processAlive(state.leader), "Fixture leader must exit before the shutdown trigger");
    control.leaderExited = true;
    control.descendantAliveBeforeTrigger = processAlive(state.descendant);
    assert.equal(control.descendantAliveBeforeTrigger, true);
    control.unrelatedAlive = processAlive(other.pid);
    assert.equal(control.unrelatedAlive, true);
    if (reason === "cancelled") { controller.abort(); controller.abort(); }
    if (reason === "output_limit") await writeFile(releaseOutput, "task-owned output release\n");
    const settled = await Promise.race([outcome, new Promise((_resolve, rejectDeadline) => {
      deadline = setTimeout(() => {
        control.reachedOuterDeadline = true;
        control.descendantAliveAtOuterDeadline = processAlive(state.descendant);
        control.unrelatedAlive = processAlive(other.pid);
        rejectDeadline(new Error("Launcher did not finish within the owned 12-second cleanup bound"));
      }, 12_000);
    })]);
    if (settled.error) throw settled.error;
    control.receiptStatus = settled.value.status;
    control.elapsedMs = Date.now() - started;
    assert.equal(settled.value.status, reason === "output_limit" ? "failed" : reason);
    assert.equal(settled.value.integrityVerified, false);
    assert.equal(settled.value.error.code, reason.toUpperCase());
    assert.deepEqual(JSON.parse(await readFile(interrupt, "utf8")),
      { status: reason === "output_limit" ? "failed" : reason, reason });
    await waitForControl(() => !processAlive(state.descendant), "Owned descendant must be gone before returning control", 1000);
    control.descendantGone = true;
    control.unrelatedAlive = processAlive(other.pid);
    assert.equal(control.unrelatedAlive, true, "The unrelated process must not receive a group signal");
  } finally {
    clearTimeout(deadline);
    context.diagnostic(JSON.stringify(control));
    // RED runs need explicit cleanup too; only this fixture's detached group
    // is eligible, never the test runner's or the unrelated control's group.
    try {
      if (existsSync(pids)) {
        const owned = JSON.parse(await readFile(pids, "utf8"));
        try { process.kill(-owned.leader, "SIGKILL"); }
        catch (error) { if (error.code !== "ESRCH") throw error; }
      }
      await boundedControl(outcome, 3500, "Owned launcher must settle after its fixture group is killed");
    } finally { await other.close(); }
  }
}

for (const reason of ["timed_out", "cancelled", "output_limit"]) {
  test(`MC180 ${reason} stops a ready TERM-ignoring descendant after the leader exits without affecting another process`,
    { skip: process.platform === "win32" }, async (context) => exitedLeaderCase(context, reason));
}

test("MC180 a normally closed kernel returns its own result and later cancellation leaves another process alive",
  { skip: process.platform === "win32" }, async () => {
    const directory = join(fixture, "normally-closed-kernel");
    await mkdir(directory);
    const executable = join(directory, "fixture-python");
    const input = { status: "failed", integrityVerified: false, error: { code: "TASK_OWNED_FIXTURE_COMPLETED" } };
    await writeFile(executable, `#!${process.execPath}\nconsole.log(${JSON.stringify(JSON.stringify(input))});\n`);
    await chmod(executable, 0o700);
    const other = await unrelatedProcess();
    const controller = new AbortController();
    try {
      const receipt = await runRequest({ workspace: join(directory, "workspace"), tenant: "local", kernel: root, python: executable },
        { operation: "pipeline", mode: "daily", environment: "simulation", idempotencyKey: "normalclosed001", timeoutSeconds: 1 },
        { signal: controller.signal });
      assert.deepEqual(receipt, input);
      controller.abort();
      await pause(1150);
      assert.equal(processAlive(other.pid), true);
    } finally { await other.close(); }
  });

function client() {
  const child = spawn(process.execPath, [mcp, "--workspace", workspace], { env, cwd: fixture, stdio: ["pipe", "pipe", "pipe"] });
  let output = "", stderr = "";
  const pending = new Map();
  const messages = [];
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    output += chunk;
    let newline;
    while ((newline = output.indexOf("\n")) !== -1) {
      const value = JSON.parse(output.slice(0, newline));
      output = output.slice(newline + 1);
      messages.push(value);
      pending.get(value.id)?.(value);
      pending.delete(value.id);
    }
  });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  function send(message) {
    const promise = Object.hasOwn(message, "id") ? new Promise((resolveMessage) => pending.set(message.id, resolveMessage)) : Promise.resolve();
    child.stdin.write(JSON.stringify(message) + "\n");
    return promise;
  }
  return { child, messages, send,
    call: (id, name, input) => send({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: input } }),
    async close() {
      child.stdin.end();
      const [code] = await once(child, "close");
      assert.equal(code, 0, stderr);
      assert.equal(stderr, "");
      assert.equal(output, "");
    } };
}

async function handshake(connection) {
  const result = await connection.send({ jsonrpc: "2.0", id: "init", method: "initialize",
    params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "actual-test", version: "1" } } });
  assert.equal(result.result.protocolVersion, "2025-11-25");
  connection.send({ jsonrpc: "2.0", method: "notifications/initialized" });
}

test("actual stdio MCP performs handshake/list/call/artifact read and returns structured degraded/failure results", async () => {
  const connection = client();
  try {
    const premature = await connection.send({ jsonrpc: "2.0", id: 0, method: "tools/list" });
    assert.equal(premature.error.code, -32002);
    await handshake(connection);
    const list = await connection.send({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} });
    assert.equal(list.result.tools.length, 5);
    const call = await connection.call(2, "tiger.pipeline.run", { ...base, mode: "daily", idempotencyKey: "mcpdaily001", riskLimits: profileLimits });
    assert.equal(call.result.isError, false);
    assert.equal(call.result.structuredContent.status, "degraded");
    assert.deepEqual(call.result.structuredContent.requestedRiskLimits, profileLimits);
    await verified(call.result.structuredContent);
    const read = await connection.call(3, "tiger.job.artifacts", { jobId: "mcpdaily001", name: "execution_trace.json" });
    assert.equal(read.result.isError, false);
    assert.equal(JSON.parse(read.result.structuredContent.text).steps.length, 21);
    const invalid = await connection.call(4, "tiger.pipeline.run", { ...base, mode: "daily", idempotencyKey: "invalidmcp001", approve: true });
    assert.equal(invalid.result.isError, true);
    assert.equal(invalid.result.structuredContent.error.code, "INVALID_INPUT");
    assert.ok(!existsSync(join(workspace, "jobs/local/invalidmcp001")));
    const unknown = await connection.call(5, "unregistered.tool", {});
    assert.equal(unknown.error.code, -32602);
  } finally { await connection.close(); }
});

test("MCP cancellation notification suppresses the call response and persists cancelled; explicit cancel returns final receipt", async () => {
  const connection = client();
  try {
    await handshake(connection);
    connection.call("cancel-me", "tiger.pipeline.run", { ...base, mode: "intraday", idempotencyKey: "cancelnotify001", sourceJob, cycles: 100, intervalSeconds: 1 });
    const ownerPath = join(workspace, "jobs/local/cancelnotify001/owner.json");
    for (let index = 0; index < 400 && !existsSync(ownerPath); index += 1) await pause(25);
    assert.ok(existsSync(ownerPath));
    connection.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: "cancel-me", reason: "task-owned cancellation test" } });
    let receipt;
    for (let index = 0; index < 100; index += 1) {
      const response = await connection.call(`poll-${index}`, "tiger.job.get", { jobId: "cancelnotify001" });
      receipt = response.result.structuredContent;
      if (receipt.status === "cancelled") break;
      await pause(25);
    }
    assert.equal(receipt.status, "cancelled");
    assert.ok(!connection.messages.some((message) => message.id === "cancel-me"));
    connection.call("cancel-tool-job", "tiger.pipeline.run", { ...base, mode: "intraday", idempotencyKey: "canceltool001", sourceJob, cycles: 100, intervalSeconds: 1 });
    const secondOwner = join(workspace, "jobs/local/canceltool001/owner.json");
    for (let index = 0; index < 400 && !existsSync(secondOwner); index += 1) await pause(25);
    assert.ok(existsSync(secondOwner));
    const cancelled = await connection.call("cancel-tool", "tiger.job.cancel", { jobId: "canceltool001" });
    assert.equal(cancelled.result.structuredContent.status, "cancelled");
    assert.equal(cancelled.result.isError, false);
  } finally { await connection.close(); }
});

test("stdio oversized/malformed frames recover cleanly and EOF waits for accepted real work", async () => {
  const connection = client();
  await handshake(connection);
  connection.child.stdin.write("x".repeat(140 * 1024) + "\n");
  connection.child.stdin.write("{broken\n");
  const ping = await connection.send({ jsonrpc: "2.0", id: "after-bad-frame", method: "ping" });
  assert.deepEqual(ping.result, {});
  const pending = connection.call("eof-work", "tiger.employee.run", { ...base, employee: "scanner", idempotencyKey: "eofscanner001" });
  const close = connection.close();
  const result = await pending;
  assert.equal(result.result.structuredContent.status, "succeeded");
  await close;
  assert.equal(connection.messages.filter((message) => message.error?.code === -32700).length, 2);
});
