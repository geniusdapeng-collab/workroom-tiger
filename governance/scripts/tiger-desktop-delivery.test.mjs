import assert from "node:assert/strict";
import { generateKeyPairSync, createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync, readdirSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve, win32 } from "node:path";
import test from "node:test";
import YAML from "yaml";
import { archiveSignedPayload, buildTarPlan, releasePolicy } from "./build-tiger-desktop.mjs";
import { execute, smokeDesktop, verifyJobArtifacts, verifyRunningDesktop } from "./tiger-desktop-smoke.mjs";

const hash = (data) => createHash("sha256").update(data).digest("hex");
const ed25519 = generateKeyPairSync("ed25519").privateKey.export({ type: "pkcs8", format: "pem" });
const bundle = { BUNDLE_SIGNING_PRIVATE_KEY: ed25519, BUNDLE_SIGNING_KEY_ID: "isolated-test" };
const host = { platform: "mac", hostPlatform: "darwin", hostArch: "arm64" };
const require = createRequire(import.meta.url);

test("native release rejects a cross-platform claim", () => {
  assert.throws(() => releasePolicy({ ...host, platform: "win", candidate: true, environment: bundle }), /原生/u);
});
test("an unsigned platform candidate still requires industry trust", () => {
  assert.throws(() => releasePolicy({ ...host, candidate: true, environment: {} }), /Ed25519/u);
  assert.deepEqual(releasePolicy({ ...host, candidate: true, environment: bundle }), { native: true, candidate: true, unsignedPlatform: true });
});
test("a signed release fails closed without its platform certificate", () => {
  assert.throws(() => releasePolicy({ ...host, candidate: false, environment: bundle }), /平台签名/u);
});
test("Mac production requires notarization credentials in addition to the certificate", () => {
  const cert = { ...bundle, CSC_LINK: "fixture.p12", CSC_KEY_PASSWORD: "fixture" };
  assert.throws(() => releasePolicy({ ...host, candidate: false, environment: cert }), /公证/u);
  assert.equal(releasePolicy({ ...host, candidate: false, environment: { ...cert, APPLE_ID: "fixture@example.invalid",
    APPLE_APP_SPECIFIC_PASSWORD: "fixture", APPLE_TEAM_ID: "TEST" } }).unsignedPlatform, false);
});
test("Windows native production validates its certificate without inventing Apple requirements", () => {
  assert.equal(releasePolicy({ platform: "win", hostPlatform: "win32", hostArch: "x64", candidate: false,
    environment: { ...bundle, CSC_LINK: "fixture.p12", CSC_KEY_PASSWORD: "fixture" } }).native, true);
});

function fixture() {
  const workspace = mkdtempSync(join(tmpdir(), "tiger-client-receipt-test-"));
  const job = join(workspace, "jobs", "local", "fixture001");
  mkdirSync(join(job, "artifacts"), { recursive: true });
  const content = Buffer.from("real artifact fixture\n");
  writeFileSync(join(job, "artifacts", "out.txt"), content);
  const receipt = { schemaVersion: "tiger.agent-receipt/v1", status: "succeeded", integrityVerified: true,
    environment: "simulation", provider: "demo", governanceSynced: false, permissions: { approvals: false, brokerOrders: false, parameterApplication: false },
    jobId: "fixture001", artifacts: [{ name: "out.txt", bytes: content.length, sha256: hash(content) }] };
  writeFileSync(join(job, "receipt.json"), JSON.stringify(receipt));
  writeFileSync(join(job, "completion.json"), JSON.stringify({ receiptSha256: hash(readFileSync(join(job, "receipt.json"))) }));
  return { workspace, job, receipt };
}
test("smoke verifies receipt completion and every artifact", () => {
  const item = fixture();
  try { assert.equal(verifyJobArtifacts(item.workspace, item.receipt).artifacts, 1); }
  finally { rmSync(item.workspace, { recursive: true }); }
});
test("post-completion artifact corruption cannot be reported as passed", () => {
  const item = fixture();
  try {
    writeFileSync(join(item.job, "artifacts", "out.txt"), "changed payload");
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt));
  } finally { rmSync(item.workspace, { recursive: true }); }
});
test("an altered completion digest cannot be reported as passed", () => {
  const item = fixture();
  try {
    writeFileSync(join(item.job, "completion.json"), JSON.stringify({ receiptSha256: "0".repeat(64) }));
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt), /immutable receipt/u);
  } finally { rmSync(item.workspace, { recursive: true }); }
});

function persistReceipt(item) {
  writeFileSync(join(item.job, "receipt.json"), JSON.stringify(item.receipt));
  writeFileSync(join(item.job, "completion.json"), JSON.stringify({ receiptSha256: hash(readFileSync(join(item.job, "receipt.json"))) }));
}

function pipelineReceiptFixture(t, mode, status = "degraded") {
  const item = fixture();
  t.after(() => rmSync(item.workspace, { recursive: true, force: true }));
  Object.assign(item.receipt, { mode, status,
    stepTrace: Array.from({ length: 21 }, (_, index) => ({ step: `fixture-stage-${index + 1}`, status: "executed" })),
    degradedSteps: status === "degraded" ? ["fixture-disabled-model"] : [] });
  // Ordinary local byte fixtures exercise the helper's integrity and mode
  // contract; these files do not claim a Python run or a packaged installation.
  for (const [name, role, mediaType, content] of [
    ["result.json", "pipeline-result", "application/json", '{"environment":"simulation","fixture":true}\n'],
    ["report.html", "output", "text/html", "<!doctype html><p>simulation fixture</p>\n"],
    ["governance_events.jsonl", "governance-events", "application/x-ndjson", '{"environment":"simulation","fixture":true}\n'],
  ]) {
    const bytes = Buffer.from(content);
    writeFileSync(join(item.job, "artifacts", name), bytes);
    item.receipt.artifacts.push({ name, role, mediaType, bytes: bytes.length, sha256: hash(bytes) });
  }
  persistReceipt(item);
  return item;
}

const pipelineVerification = (expectedMode) => ({ expectedMode, requirePipelineArtifacts: true });
for (const mode of ["daily", "premarket"]) {
  for (const status of ["succeeded", "degraded"]) test(`mode-aware ${mode} accepts an intact ${status} pipeline receipt`, (t) => {
    const item = pipelineReceiptFixture(t, mode, status);
    const actual = verifyJobArtifacts(item.workspace, item.receipt, pipelineVerification(mode));
    assert.equal(actual.status, status); assert.equal(actual.stages, 21); assert.equal(actual.artifacts, 4);
    assert.deepEqual(actual.degradedSteps, item.receipt.degradedSteps);
  });

  test(`mode-aware ${mode} accepts disclosed pipeline stages beyond the minimum`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    item.receipt.stepTrace.push({ step: "fixture-extension-stage", status: "executed" });
    persistReceipt(item);
    assert.equal(verifyJobArtifacts(item.workspace, item.receipt, pipelineVerification(mode)).stages, 22);
  });

  for (const [name, mutate] of [
    ["an incomplete stage trace", (f) => { f.receipt.stepTrace.pop(); }],
    ["a missing pipeline result", (f) => { f.receipt.artifacts.find((a) => a.role === "pipeline-result").role = "output"; }],
    ["a missing HTML report", (f) => { f.receipt.artifacts.find((a) => a.mediaType === "text/html").mediaType = "text/plain"; }],
    ["missing governance events", (f) => { f.receipt.artifacts.find((a) => a.role === "governance-events").role = "output"; }],
    ["missing degraded-step disclosure", (f) => { f.receipt.degradedSteps = []; }],
    ["a live environment", (f) => { f.receipt.environment = "live"; }],
    ["broker-order permissions", (f) => { f.receipt.permissions.brokerOrders = true; }],
    ["approval permissions", (f) => { f.receipt.permissions.approvals = true; }],
    ["parameter-application permissions", (f) => { f.receipt.permissions.parameterApplication = true; }],
    ["a false server-sync claim", (f) => { f.receipt.governanceSynced = true; }],
    ["an altered artifact byte count", (f) => { f.receipt.artifacts[0].bytes += 1; }],
    ["an escaped artifact path", (f) => { f.receipt.artifacts[0].name = "../outside.txt"; }],
  ]) test(`mode-aware ${mode} rejects ${name} after a valid receipt completion`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    mutate(item); persistReceipt(item);
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, pipelineVerification(mode)));
  });

  test(`mode-aware ${mode} rejects artifact changes that retain the declared byte count`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    const artifact = item.receipt.artifacts[0];
    writeFileSync(join(item.job, "artifacts", artifact.name), Buffer.alloc(artifact.bytes, 120));
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, pipelineVerification(mode)), /artifact changed/u);
  });

  test(`mode-aware ${mode} rejects an altered completion digest`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    writeFileSync(join(item.job, "completion.json"), JSON.stringify({ receiptSha256: "0".repeat(64) }));
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, pipelineVerification(mode)), /immutable receipt/u);
  });

  test(`mode-aware ${mode} rejects a returned receipt that differs from the bound disk receipt`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    const returned = { ...item.receipt, kernelDigest: "0".repeat(64) };
    assert.throws(() => verifyJobArtifacts(item.workspace, returned, pipelineVerification(mode)));
  });
}

for (const mode of ["daily", "premarket", "intraday", "backtest", "tune", "review"]) {
  test(`mode-aware ${mode} rejects a different returned mode with valid local bytes`, (t) => {
    const differentMode = mode === "daily" ? "premarket" : "daily";
    const item = pipelineReceiptFixture(t, differentMode);
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, {
      expectedMode: mode, requirePipelineArtifacts: ["daily", "premarket"].includes(mode),
    }), /receipt mode/u);
  });

  test(`mode-aware ${mode} rejects a missing returned mode with valid local bytes`, (t) => {
    const item = pipelineReceiptFixture(t, mode);
    delete item.receipt.mode; persistReceipt(item);
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, {
      expectedMode: mode, requirePipelineArtifacts: ["daily", "premarket"].includes(mode),
    }), /receipt mode/u);
  });
}

for (const mode of ["intraday", "backtest", "tune", "review"]) test(`mode-aware ${mode} verifies its receipt without daily pipeline requirements`, (t) => {
  const item = fixture(); t.after(() => rmSync(item.workspace, { recursive: true, force: true }));
  item.receipt.mode = mode; persistReceipt(item);
  const actual = verifyJobArtifacts(item.workspace, item.receipt, { expectedMode: mode });
  assert.equal(actual.artifacts, 1); assert.equal(actual.stages, 0);
});

for (const [name, options] of [["the legacy true flag", true], ["the legacy false flag", false], ["null", null],
  ["an array", []], ["a string flag", "daily"], ["a nonboolean pipeline flag", { expectedMode: "daily", requirePipelineArtifacts: "true" }],
  ["pipeline requirements without a requested mode", { requirePipelineArtifacts: true }]]) {
  test(`mode-aware verification rejects ${name} instead of skipping its checks`, (t) => {
    const item = pipelineReceiptFixture(t, "daily");
    assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, options), /options|requirePipelineArtifacts|expectedMode/u);
  });
}

test("smoke timeout terminates an uncooperative child and returns a failure", async () => {
  const started = Date.now();
  await assert.rejects(execute(process.execPath, ["-e", 'process.on("SIGTERM",()=>{});setInterval(()=>{},1000)'], {
    environment: { ...process.env, PATH: "" }, cwd: tmpdir(), label: "timeout fixture", timeout: 150,
  }), /超时/u);
  assert.ok(Date.now() - started < 10000, "a stalled process must not hang the native gate");
});

test("smoke deadline stops an owned descendant after its group leader has exited", { skip: process.platform === "win32" }, async () => {
  const root = mkdtempSync(join(tmpdir(), "tiger-smoke-descendant-test-"));
  const pids = join(root, "pids.json");
  const descendant = `process.on('SIGTERM',()=>{});setInterval(()=>{},1000);process.send('ready');`;
  const program = `const {spawn}=require('node:child_process');const fs=require('node:fs');
    const child=spawn(process.execPath,['-e',${JSON.stringify(descendant)}],{stdio:['ignore','inherit','inherit','ipc']});
    child.once('message',()=>{fs.writeFileSync(${JSON.stringify(pids)},JSON.stringify({leader:process.pid,descendant:child.pid}));child.disconnect();process.exit(0)});`;
  let deadline;
  const run = execute(process.execPath, ["-e", program], { environment: { ...process.env, PATH: "" }, cwd: root,
    label: "owned descendant fixture", timeout: 1000 });
  try {
    await assert.rejects(Promise.race([run, new Promise((_yes, no) => {
      deadline = setTimeout(() => no(new Error("owned process group did not stop within its deadline")), 6000);
    })]), /owned descendant fixture 超时/u);
    const state = JSON.parse(readFileSync(pids, "utf8"));
    assert.throws(() => process.kill(state.descendant, 0), { code: "ESRCH" });
  } finally {
    clearTimeout(deadline);
    if (existsSync(pids)) {
      const state = JSON.parse(readFileSync(pids, "utf8"));
      try { process.kill(-state.leader, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    await run.catch(() => {});
    rmSync(root, { recursive: true, force: true });
  }
});

test("Windows tar arguments use relative paths instead of a drive interpreted as host:path", () => {
  assert.deepEqual(buildTarPlan("D:\\build\\payload", "D:\\build\\release\\payload.tar.gz", win32), {
    cwd: "D:\\build\\release", args: ["-czf", "payload.tar.gz", "-C", "../payload", "."],
  });
  assert.throws(() => buildTarPlan("C:\\payload", "D:\\release\\payload.tar.gz", win32), /同一个卷/u);
});

test("CNB keeps source PR gates and requires both native platforms for explicit candidates and production", () => {
  const governance = resolve(import.meta.dirname, "..");
  const repository = resolve(governance, "..");
  const product = JSON.parse(readFileSync(join(repository, "product.manifest.json"), "utf8"));
  assert.deepEqual(JSON.parse(readFileSync(join(governance, "product.manifest.json"), "utf8")), product);
  assert.equal(product.release.workflow, ".cnb.yml");
  const workflow = YAML.parse(readFileSync(join(repository, product.release.workflow), "utf8"));
  const sourceJobs = workflow["**"].pull_request;
  assert.deepEqual(sourceJobs.map((job) => job.name).sort(), ["oss-gate", "protocol-gate", "py-gate"]);
  assert.ok(sourceJobs.every((job) => !job.runner?.namespace && !job.allow_failure && !job.allowFailure && !job.if));
  for (const [branch, event, candidate] of [["main", "push", false], ["**", "api_trigger_tiger_native_candidate", true]]) {
    const jobs = workflow[branch][event];
    for (const [platform, tags] of [["mac", ["mac", "arm64"]], ["win", ["windows"]]]) {
      const matches = jobs.filter((job) => job.runner?.namespace === "group" && tags.every((tag) => job.runner.tags.includes(tag)));
      assert.equal(matches.length, 1, `${branch}/${event}/${platform} native gate`);
      const job = matches[0];
      assert.ok(!job.allow_failure && !job.allowFailure && !job.if, `${job.name} must be mandatory`);
      const invocation = job.stages.map((stage) => stage.script).join("\n").replaceAll("\\", "/");
      assert.ok(invocation.includes(product.desktop.nativeBuilders[platform]));
      assert.equal(/--candidate|-Candidate/u.test(invocation), candidate);
      const native = readFileSync(join(governance, product.desktop.nativeBuilders[platform]), "utf8");
      assert.ok(native.includes("projections:check"));
      assert.ok(native.includes("verify-product-content.mjs"));
      assert.ok(native.includes("industry-runtime.test.cjs"));
      assert.ok(native.includes(product.desktop.smokeRunner));
      assert.ok(native.includes("--render"));
    }
  }
});

test("CNB separates optional source macro smoke from mandatory opt-in live data checks", () => {
  const repository = resolve(import.meta.dirname, "../..");
  const workflow = YAML.parse(readFileSync(join(repository, ".cnb.yml"), "utf8"));
  const source = workflow["**"].pull_request;
  const python = source.find((job) => job.name === "py-gate");
  assert.equal(python.env.RUN_TIGER_LIVE_MACRO_TESTS, "0");
  const sourceTest = python.stages.find((stage) => stage.name === "交易内核测试（pytest）");
  assert.ok(sourceTest.script.split("\n").includes("export RUN_TIGER_LIVE_MACRO_TESTS=0"));
  assert.ok(sourceTest.script.split("\n").includes("python -m pytest -q -rs"));
  const live = workflow["**"].api_trigger_tiger_live_macro;
  assert.equal(live.length, 1);
  assert.equal(live[0].name, "tiger-live-macro");
  assert.equal(live[0].env.RUN_TIGER_LIVE_MACRO_TESTS, "1");
  assert.deepEqual(live[0].docker, python.docker);
  assert.deepEqual(live[0].stages[0], python.stages[0]);
  assert.equal(live[0].stages.length, 2);
  const checks = [live[0], ...live[0].stages];
  assert.ok(checks.every((item) => !item.allow_failure && !item.allowFailure && !item.if));
  const script = live[0].stages[1].script;
  assert.deepEqual(script.trim().split("\n"), ["set -eu", "export RUN_TIGER_LIVE_MACRO_TESTS=1",
    "python -m pytest -q -rs tests/test_provider_official.py::test_smoke_real_fred_cboe"]);
  assert.ok(!source.some((job) => job.name === live[0].name));
  assert.ok(!workflow.main.push.some((job) => job.name === live[0].name));
});

test("interactive smoke verifies while the child is alive, then releases the same instance", async () => {
  const instance = "11111111-1111-4111-8111-111111111111";
  const program = `const readline=require('node:readline');const lines=readline.createInterface({input:process.stdin});
    lines.once('line',line=>{const release=JSON.parse(line);process.exit(release.instanceId===${JSON.stringify(instance)}?0:9)});
    console.log('READY '+JSON.stringify({instanceId:${JSON.stringify(instance)}}));`;
  let observed = 0;
  const result = await execute(process.execPath, ["-e", program], { environment: { ...process.env, PATH: "" }, cwd: tmpdir(),
    label: "live child handshake fixture", timeout: 2000, onStdoutLine: async (line, context) => {
      assert.equal(context.isAlive(), true);
      await new Promise((done) => setTimeout(done, 25));
      assert.equal(context.isAlive(), true);
      const frame = JSON.parse(line.slice(6));
      assert.equal(frame.instanceId, instance);
      observed += 1;
      context.sendLine({ instanceId: frame.instanceId }); context.closeInput();
    } });
  assert.equal(result.code, 0); assert.equal(observed, 1);
});

test("interactive smoke preserves UTF8 identity text split across real pipe chunks", async () => {
  const expected = "READY 客户目录/模拟交易";
  const program = `const bytes=Buffer.from(${JSON.stringify(expected + "\n")});let offset=0;
    const timer=setInterval(()=>{if(offset<bytes.length)process.stdout.write(bytes.subarray(offset,++offset));else clearInterval(timer)},2);
    process.stdin.once('data',()=>process.exit(0));`;
  let observed = null;
  await execute(process.execPath, ["-e", program], { environment: { ...process.env, PATH: "" }, cwd: tmpdir(),
    label: "UTF8 child fixture", timeout: 2000, onStdoutLine: async (line, context) => {
      observed = line; context.sendLine({ release: true }); context.closeInput();
    } });
  assert.equal(observed, expected);
});

test("a child exiting before asynchronous verification cannot produce a successful live check", async () => {
  await assert.rejects(execute(process.execPath, ["-e", "console.log('READY');process.exit(0)"], {
    environment: { ...process.env, PATH: "" }, cwd: tmpdir(), label: "early exit fixture", timeout: 1000,
    onStdoutLine: async (_line, context) => {
      await new Promise((done) => setTimeout(done, 50));
      assert.equal(context.isAlive(), true, "process must remain alive during actual verification");
    },
  }), /process must remain alive/u);
});

test("an asynchronous inspection rejection propagates and terminates the child", async () => {
  const started = Date.now();
  await assert.rejects(execute(process.execPath, ["-e", "console.log('READY');setInterval(()=>{},1000)"], {
    environment: { ...process.env, PATH: "" }, cwd: tmpdir(), label: "rejected inspection fixture", timeout: 2000,
    onStdoutLine: async () => { await new Promise((done) => setTimeout(done, 20)); throw new Error("inspection rejected fixture"); },
  }), /inspection rejected fixture/u);
  assert.ok(Date.now() - started < 5000);
});

test("an inspection that stalls after child exit remains bounded by the smoke deadline", async () => {
  const started = Date.now();
  await assert.rejects(execute(process.execPath, ["-e", "console.log('READY')"], {
    environment: { ...process.env, PATH: "" }, cwd: tmpdir(), label: "stalled inspection fixture", timeout: 200,
    onStdoutLine: () => new Promise(() => {}),
  }), /超时/u);
  assert.ok(Date.now() - started < 2000);
});

test("overlong child handshake lines fail the gate instead of growing the parser without a bound", async () => {
  await assert.rejects(execute(process.execPath, ["-e", "console.log('x'.repeat(65537));setInterval(()=>{},1000)"], {
    environment: { ...process.env, PATH: "" }, cwd: tmpdir(), label: "overlong handshake fixture", timeout: 2000,
    onStdoutLine: () => {},
  }), /单行输出超过限制/u);
});

test("noninteractive CLI and MCP input still receives EOF after all request frames", async () => {
  const program = "let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>console.log(input.split('\\n').filter(Boolean).length))";
  const result = await execute(process.execPath, ["-e", program], { environment: { ...process.env, PATH: "" }, cwd: tmpdir(),
    label: "MCP input fixture", timeout: 2000, input: '{"id":1}\n{"id":2}\n' });
  assert.equal(result.stdout.trim(), "2");
});

test("native installed identity cannot be inspected after the actual App process has exited", async () => {
  await assert.rejects(verifyRunningDesktop({ isAlive: () => false }), /身份核验前已退出/u);
});

test("a failed native smoke rerun revokes its previous success receipt", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "tiger-smoke-stale-receipt-test-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const resources = join(root, "resources"), output = join(root, "checks"), buildFile = join(root, "desktop-build.json");
  mkdirSync(resources); mkdirSync(output);
  writeFileSync(join(resources, "payload.tar.gz"), "mutated archive fixture");
  writeFileSync(join(output, "desktop-smoke.json"), JSON.stringify({ finalAppVerified: true, prior: true }));
  writeFileSync(buildFile, JSON.stringify({ schemaVersion: "tiger.desktop-build/v1", platform: process.platform === "win32" ? "win" : "mac",
    arch: process.arch, signedArchiveVerified: true, smokeVerified: true, smokeReceipt: join(output, "desktop-smoke.json"),
    resources, payloadSha256: "0".repeat(64) }));
  await assert.rejects(smokeDesktop({ buildFile, outputRoot: output }));
  assert.equal(existsSync(join(output, "desktop-smoke.json")), false);
  const build = JSON.parse(readFileSync(buildFile, "utf8"));
  assert.equal(build.smokeVerified, false);
  assert.equal(build.smokeReceipt, undefined);
});

function archiveFixture(t) {
  const root = mkdtempSync(join(tmpdir(), "tiger-signed-archive-test-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const payload = join(root, "payload"), archive = join(root, "release", "payload.tar.gz");
  mkdirSync(dirname(archive), { recursive: true });
  for (const folder of ["runtime", "node", "pg", "nats"]) mkdirSync(join(payload, folder), { recursive: true });
  for (const [file, contents] of [["VERSION", "1.0.0\n"], ["PAYLOAD_VERSION", "1.0.0\n"], ["runtime/VERSION", "1.0.0\n"],
    ["runtime/.env.defaults", "LLM_PROVIDER=mock\n"], ["runtime/scripts/desktop-bootstrap-db.mjs", "// ordinary indexed fixture asset\n"],
    ["runtime/product.manifest.json", JSON.stringify({ productId: "workroom-tiger" })],
    ["runtime/bundles/trading/bundle.json", JSON.stringify({ bundle_id: "trading", signature: "before" })], ["runtime/immutable.txt", "immutable payload\n"],
    ["node/bin/node", "node fixture\n"], ["pg/bin/postgres", "pg fixture\n"], ["pg/bin/pg_ctl", "pg_ctl fixture\n"],
    ["pg/bin/initdb", "initdb fixture\n"], ["nats/nats-server", "nats fixture\n"]]) {
    mkdirSync(dirname(join(payload, file)), { recursive: true }); writeFileSync(join(payload, file), contents);
  }
  const integrity = require("../apps/desktop/electron/payload-integrity.cjs");
  const before = integrity.generatePayloadIntegrity(payload, { expectedProductId: "workroom-tiger", expectedVersion: "1.0.0" });
  return { root, payload, archive, integrity, productManifestSha256: before.productManifestSha256 };
}
const archiveOptions = (f) => ({ payload: f.payload, archive: f.archive, productId: "workroom-tiger", version: "1.0.0", productManifestSha256: f.productManifestSha256 });

test("signed bundle bytes are reindexed before the actual archive is sealed and extracted", (t) => {
  const f = archiveFixture(t);
  const result = archiveSignedPayload({ ...archiveOptions(f), signBundles: () => {
    writeFileSync(join(f.payload, "runtime/bundles/trading/bundle.json"), JSON.stringify({ bundle_id: "trading", signature: "signed fixture manifest bytes" }));
  } });
  assert.equal(result.signedArchiveVerified, true); assert.equal(hash(readFileSync(f.archive)), result.payloadSha256);
  const extracted = join(f.root, "extracted"); mkdirSync(extracted);
  const tar = process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "/usr/bin/tar";
  execFileSync(tar, ["-xzf", f.archive], { cwd: extracted });
  assert.deepEqual(f.integrity.verifyPayloadIntegrity(extracted, { expectedProductId: "workroom-tiger", expectedVersion: "1.0.0" }), result.payloadIntegrity);
  assert.equal(readdirSync(dirname(f.archive)).some((name) => name.startsWith(".tiger-archive-check-")), false);
});

for (const mutation of ["immutable", "manifest", "added-file"]) test(`a bundle signer cannot change ${mutation} outside the allowed manifest bytes`, (t) => {
  const f = archiveFixture(t);
  assert.throws(() => archiveSignedPayload({ ...archiveOptions(f), signBundles: () => {
    if (mutation === "immutable") writeFileSync(join(f.payload, "runtime/immutable.txt"), "changed payload\n");
    if (mutation === "manifest") writeFileSync(join(f.payload, "runtime/product.manifest.json"), JSON.stringify({ productId: "foreign-product" }));
    if (mutation === "added-file") writeFileSync(join(f.payload, "runtime/extra.txt"), "new file\n");
  } }));
  assert.equal(existsSync(f.archive), false);
});

test("a failed signing operation cannot leave a previous release archive at the requested output", (t) => {
  const f = archiveFixture(t);
  writeFileSync(f.archive, "stale previous archive");
  assert.throws(() => archiveSignedPayload({ ...archiveOptions(f), signBundles: () => { throw new Error("signer failed fixture"); } }), /signer failed/u);
  assert.equal(existsSync(f.archive), false);
});

import { realpathSync, renameSync, symlinkSync } from "node:fs";
import { normalizeRequest, verifyReceipt } from "../../scripts/tiger-agent-runtime.mjs";

const receiptCases = [
  ...["daily", "premarket", "intraday", "backtest", "tune", "review"].map((mode) => ({ operation: "pipeline", mode })),
  ...["scanner", "mrs", "risk", "review"].map((employee) => ({ operation: "employee", employee })),
];
const receiptOptions = ({ mode }, requireLauncherIntegrity = true) => ({
  ...(mode ? { expectedMode: mode, requirePipelineArtifacts: ["daily", "premarket"].includes(mode) } : {}), requireLauncherIntegrity,
});
const canonicalFixtureJson = (value) => value === null || typeof value !== "object" ? JSON.stringify(value)
  : Array.isArray(value) ? `[${value.map(canonicalFixtureJson).join(",")}]`
    : `{${Object.keys(value).filter((key) => value[key] !== null).sort().map((key) => `${JSON.stringify(key)}:${canonicalFixtureJson(value[key])}`).join(",")}}`;

function fullCanonicalReceiptFixture(t, selected = { operation: "pipeline", mode: "daily" }, status = "degraded") {
  const item = fixture();
  // The real launcher rejects symlink aliases. macOS tmpdir can start with
  // /var; use the actual directory for this launcher integration fixture.
  item.workspace = realpathSync(item.workspace);
  item.job = join(item.workspace, "jobs", "local", item.receipt.jobId);
  t.after(() => rmSync(item.workspace, { recursive: true, force: true }));
  const needsSource = ["intraday", "review"].includes(selected.mode) || selected.employee === "review";
  const request = normalizeRequest({ ...selected, idempotencyKey: item.receipt.jobId,
    environment: "simulation", provider: "demo", llmMode: "disabled", maxPicks: 2,
    ...(needsSource ? { sourceJob: { jobId: "fixture-source001", resultSha256: "1".repeat(64) } } : {}) });
  const gateParams = { ...request.riskLimits, max_picks: request.maxPicks };
  const pipeline = ["daily", "premarket"].includes(selected.mode);
  const role = selected.operation === "employee" && selected.employee !== "review" ? "employee-result"
    : pipeline ? "pipeline-result" : ["backtest", "tune"].includes(selected.mode) ? "research-result" : "source-result";
  // These ordinary JSON/HTML/event byte fixtures do not claim a Python run,
  // model invocation, packaged installation or genuine business result.
  const result = pipeline ? { raw: { risk_limits: request.riskLimits, gate_params: gateParams } }
    : { riskLimits: request.riskLimits, gateParams };
  const payload = { who: { type: "agent", id: "ordinary-fixture" }, context: { stage: "simulation" },
    object: { type: "report", id: item.receipt.jobId }, decision: { action: "fixture.completed" }, rule_impact: [] };
  const event = { payload, prev_hash: "GENESIS", hash: hash("GENESIS" + canonicalFixtureJson(payload)) };
  const artifacts = [];
  for (const [name, entryRole, mediaType, content] of [
    ["result.json", role, "application/json", JSON.stringify(result) + "\n"],
    ["report.html", "output", "text/html", "<!doctype html><p>simulation ordinary fixture</p>\n"],
    ["governance_events.jsonl", "governance-events", "application/x-ndjson", JSON.stringify(event) + "\n"],
  ]) {
    const bytes = Buffer.from(content);
    writeFileSync(join(item.job, "artifacts", name), bytes);
    artifacts.push({ name, role: entryRole, mediaType, bytes: bytes.length, sha256: hash(bytes) });
  }
  item.receipt = { schemaVersion: "tiger.agent-receipt/v1", jobId: request.idempotencyKey, idempotencyKey: request.idempotencyKey,
    inputSha256: hash(canonicalFixtureJson(request)), operation: selected.operation, requestedRiskLimits: request.riskLimits,
    ...(selected.mode ? { mode: selected.mode } : { employee: selected.employee }),
    environment: request.environment, provider: request.provider, market: request.market, dataMode: "synthetic", status,
    startedAt: "2026-10-01T00:00:00.000+00:00", finishedAt: "2026-10-01T00:00:01.000+00:00", exitCode: status === "degraded" ? 10 : 0,
    sourceCommit: null, kernelDigest: hash("ordinary fixture kernel"), configDigest: hash("ordinary fixture config"),
    stepTrace: Array.from({ length: pipeline ? 21 : 2 }, (_, index) => ({ step: `fixture-stage-${index + 1}`, status: "executed", ms: 0, note: "ordinary fixture" })),
    degradedSteps: status === "degraded" ? ["fixture-disabled-model"] : [], artifacts,
    summary: { fixture: true, detail: { source: "ordinary local bytes" } }, integrityVerified: true, governanceSynced: false,
    receipt: { synced: true, scope: "local-kernel", meaning: "Ordinary fixture; no server synchronization" },
    permissions: { brokerOrders: false, approvals: false, parameterApplication: false },
    resultSha256: artifacts[0].sha256, resultArtifact: { name: artifacts[0].name, role }, riskLimits: request.riskLimits, gateParams };
  writeFileSync(join(item.job, "request.json"), JSON.stringify({ request, inputSha256: item.receipt.inputSha256 }));
  persistReceipt(item);
  return item;
}

async function fullLauncherReceipt(item, replayed = false) {
  return verifyReceipt({ workspace: item.workspace, tenant: "local" }, { ...item.receipt, ...(replayed ? { replayed: true } : {}) });
}

for (const selected of receiptCases) {
  const name = `${selected.operation} ${selected.mode ?? selected.employee}`;
  for (const status of ["succeeded", "degraded"]) test(`MC197 ${name} binds every full canonical field and actual launcher field for ${status}`, async (t) => {
    const item = fullCanonicalReceiptFixture(t, selected, status);
    const storedBefore = readFileSync(join(item.job, "receipt.json"));
    const completionBefore = readFileSync(join(item.job, "completion.json"));
    const canonicalCheck = verifyJobArtifacts(item.workspace, item.receipt, receiptOptions(selected, false));
    const returned = await fullLauncherReceipt(item);
    assert.equal(returned.artifactRoot, join(item.job, "artifacts"));
    assert.equal(returned.launcherIntegrityVerified, true);
    assert.deepEqual(Object.keys(returned).filter((key) => !Object.hasOwn(item.receipt, key)).sort(), ["artifactRoot", "launcherIntegrityVerified"]);
    const actual = verifyJobArtifacts(item.workspace, returned, receiptOptions(selected));
    assert.deepEqual(actual, canonicalCheck);
    assert.deepEqual(readFileSync(join(item.job, "receipt.json")), storedBefore);
    assert.deepEqual(readFileSync(join(item.job, "completion.json")), completionBefore);
  });

  test(`MC197 ${name} rejects missing or altered canonical fields in a complete launcher response`, async (t) => {
    const item = fullCanonicalReceiptFixture(t, selected);
    const returned = await fullLauncherReceipt(item);
    for (const field of Object.keys(item.receipt)) {
      const missing = structuredClone(returned); delete missing[field];
      assert.throws(() => verifyJobArtifacts(item.workspace, missing, receiptOptions(selected)), `missing canonical ${field}`);
      const changed = structuredClone(returned); changed[field] = { tampered: field };
      assert.throws(() => verifyJobArtifacts(item.workspace, changed, receiptOptions(selected)), `altered canonical ${field}`);
    }
  });
}

test("MC197 complete canonical helper compatibility never satisfies required launcher integrity", (t) => {
  const item = fullCanonicalReceiptFixture(t);
  assert.equal(verifyJobArtifacts(item.workspace, item.receipt, receiptOptions(item.receipt, false)).artifacts, 3);
  assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, receiptOptions(item.receipt)), /launcher/u);
});

for (const flag of ["true", 1, null, undefined]) test(`MC197 requires a boolean launcher option for ${String(flag)}`, (t) => {
  const item = fullCanonicalReceiptFixture(t);
  assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, { expectedMode: "daily", requirePipelineArtifacts: true,
    requireLauncherIntegrity: flag }), /requireLauncherIntegrity/u);
});

for (const [name, mutate] of [
  ["artifactRoot without a launcher integrity flag", (r) => { delete r.launcherIntegrityVerified; }],
  ["launcher integrity flag without artifactRoot", (r) => { delete r.artifactRoot; }],
  ["false launcher integrity", (r) => { r.launcherIntegrityVerified = false; }],
  ["string launcher integrity", (r) => { r.launcherIntegrityVerified = "true"; }],
  ["numeric launcher integrity", (r) => { r.launcherIntegrityVerified = 1; }],
  ["null launcher integrity", (r) => { r.launcherIntegrityVerified = null; }],
  ["undefined launcher integrity", (r) => { r.launcherIntegrityVerified = undefined; }],
  ["a relative artifactRoot", (r) => { r.artifactRoot = "artifacts"; }],
  ["another job artifactRoot", (r, f) => { r.artifactRoot = join(f.workspace, "jobs", "local", "different001", "artifacts"); }],
  ["another workspace artifactRoot", (r, f) => { r.artifactRoot = join(f.workspace, "outside", "jobs", "local", r.jobId, "artifacts"); }],
  ["a root with a dot segment", (r) => { r.artifactRoot += "/."; }],
  ["a root with a parent segment", (r) => { r.artifactRoot += "/../artifacts"; }],
  ["a root with a trailing separator", (r) => { r.artifactRoot += "/"; }],
  ["a URL artifactRoot", (r) => { r.artifactRoot = "file:///fixture/artifacts"; }],
  ["a null artifactRoot", (r) => { r.artifactRoot = null; }],
  ["an object artifactRoot", (r) => { r.artifactRoot = { path: r.artifactRoot }; }],
  ["an unknown returned launcher field", (r) => { r.launcherReceiptVerified = true; }],
  ["an extra nested canonical field", (r) => { r.summary.detail.unbound = true; }],
  ["an altered risk snapshot", (r) => { r.riskLimits.gross_cap /= 2; }],
  ["an altered parameter snapshot", (r) => { r.gateParams.max_picks += 1; }],
  ["an altered local receipt scope", (r) => { r.receipt.scope = "server"; }],
  ["a false replay flag", (r) => { r.replayed = false; }],
  ["a string replay flag", (r) => { r.replayed = "true"; }],
  ["a null replay flag", (r) => { r.replayed = null; }],
  ["a replay flag with no launcher pair", (r) => { delete r.artifactRoot; delete r.launcherIntegrityVerified; r.replayed = true; }],
]) test(`MC197 rejects ${name} even when launcher integrity is optional`, async (t) => {
  const item = fullCanonicalReceiptFixture(t);
  const returned = structuredClone(await fullLauncherReceipt(item));
  mutate(returned, item);
  assert.throws(() => verifyJobArtifacts(item.workspace, returned, receiptOptions(item.receipt, false)));
});

for (const field of ["artifactRoot", "launcherIntegrityVerified", "replayed"]) test(`MC197 rejects canonical disk pollution with launcher-only ${field}`, (t) => {
  const item = fullCanonicalReceiptFixture(t);
  item.receipt[field] = field === "artifactRoot" ? join(item.job, "artifacts") : true;
  persistReceipt(item);
  assert.throws(() => verifyJobArtifacts(item.workspace, item.receipt, receiptOptions(item.receipt, false)), /canonical.*launcher|launcher-only/u);
});

test("MC197 accepts only the actual launcher's true replay disclosure with its complete verified pair", async (t) => {
  const item = fullCanonicalReceiptFixture(t);
  const returned = await fullLauncherReceipt(item, true);
  assert.equal(returned.replayed, true);
  assert.equal(Object.hasOwn(JSON.parse(readFileSync(join(item.job, "receipt.json"), "utf8")), "replayed"), false);
  assert.equal(verifyJobArtifacts(item.workspace, returned, receiptOptions(item.receipt)).artifacts, 3);
});

for (const [name, corrupt] of [
  ["completion", (f) => { writeFileSync(join(f.job, "completion.json"), JSON.stringify({ receiptSha256: "0".repeat(64) })); }],
  ["stored canonical receipt", (f) => { writeFileSync(join(f.job, "receipt.json"), "{}"); }],
  ["artifact byte count", (f) => { writeFileSync(join(f.job, "artifacts", "report.html"), "short"); }],
  ["same-length artifact hash", (f) => { writeFileSync(join(f.job, "artifacts", "report.html"), Buffer.alloc(f.receipt.artifacts[1].bytes, 120)); }],
]) test(`MC197 does not trust a true launcher flag after ${name} corruption`, async (t) => {
  const item = fullCanonicalReceiptFixture(t);
  const returned = await fullLauncherReceipt(item);
  corrupt(item);
  assert.throws(() => verifyJobArtifacts(item.workspace, returned, receiptOptions(item.receipt)));
});

test("MC197 rejects an artifact directory alias with a complete matching launcher pair", async (t) => {
  const item = fullCanonicalReceiptFixture(t);
  const returned = await fullLauncherReceipt(item);
  const artifacts = join(item.job, "artifacts"), actual = join(item.job, "actual-artifacts");
  renameSync(artifacts, actual);
  symlinkSync(actual, artifacts, process.platform === "win32" ? "junction" : "dir");
  assert.equal(returned.artifactRoot, artifacts);
  assert.notEqual(realpathSync(artifacts), artifacts);
  assert.throws(() => verifyJobArtifacts(item.workspace, returned, receiptOptions(item.receipt)), /path alias/u);
});

test("MC197 rejects a workspace directory alias with a complete matching launcher pair", async (t) => {
  const item = fullCanonicalReceiptFixture(t);
  const returned = await fullLauncherReceipt(item);
  const alias = item.workspace + "-alias";
  symlinkSync(item.workspace, alias, process.platform === "win32" ? "junction" : "dir");
  t.after(() => rmSync(alias, { recursive: true, force: true }));
  returned.artifactRoot = join(alias, "jobs", "local", returned.jobId, "artifacts");
  assert.notEqual(realpathSync(returned.artifactRoot), returned.artifactRoot);
  assert.throws(() => verifyJobArtifacts(alias, returned, receiptOptions(item.receipt)), /path alias/u);
});

// MC199 exercises the production checker as a real subprocess. These are local
// source-policy fixtures; no fixture claims a native build or installed App run.
import { spawnSync as mc199SpawnChecker } from "node:child_process";

const mc199Governance = resolve(import.meta.dirname, "..");
const mc199Repository = resolve(mc199Governance, "..");
const mc199Checker = join(mc199Governance, "scripts/verify-product-content.mjs");
const mc199Lanes = [
  { branch: "main", event: "push", candidate: false },
  { branch: "**", event: "api_trigger_tiger_native_candidate", candidate: true },
];

function mc199ProductFixture(t) {
  const root = mkdtempSync(join(tmpdir(), "tiger-native-policy-product-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  // Copy real non-native contract inputs instead of reimplementing their checks.
  // The fixture is a flat, non-Git product export, so native command paths are
  // relative to this export and optional website/seed inputs are absent.
  for (const file of ["product.manifest.json", "package.json", "electron-builder.yml",
    "apps/web/package.json", "apps/webb/package.json", "apps/webc/package.json",
    "scripts/build-tiger-desktop-native.sh", "scripts/build-tiger-desktop-native.ps1", "scripts/tiger-desktop-smoke.mjs",
    "apps/web/src/components/welcomeScripts.ts", "apps/web/src/voice/VoiceEngine.ts",
    "apps/web/src/components/Floor3D.tsx", "apps/web/src/components/Stage3D.tsx", "apps/web/src/components/CeremonyStage.tsx"]) {
    mkdirSync(dirname(join(root, file)), { recursive: true });
    writeFileSync(join(root, file), readFileSync(join(mc199Governance, file)));
  }
  mkdirSync(join(root, "bundles/trading"), { recursive: true });
  writeFileSync(join(root, "bundles/trading/bundle.json"), JSON.stringify({ bundle_id: "trading", version: "0.1.0",
    description: "Isolated source-policy fixture; no business or native runtime claim", workloom: { provides: {} } }));
  const workflow = YAML.parse(readFileSync(join(mc199Repository, ".cnb.yml"), "utf8"));
  for (const lane of mc199Lanes) for (const platform of ["mac", "win"]) {
    const job = mc199NativeJob({ workflow }, lane, platform);
    for (const stage of job.stages) stage.script = stage.script.replaceAll("governance/scripts/", "scripts/")
      .replaceAll("governance\\scripts\\", "scripts\\");
  }
  return { root, workflow };
}

function mc199NativeName(lane, platform) {
  return `tiger-${platform === "mac" ? "mac" : "windows"}-native-${lane.candidate ? "candidate" : "release"}`;
}

function mc199NativeJob(item, lane, platform) {
  const job = item.workflow[lane.branch][lane.event].find((entry) => entry?.name === mc199NativeName(lane, platform));
  assert.ok(job, "fixture must select the real named native job");
  return job;
}

function mc199CheckProduct(item) {
  writeFileSync(join(item.root, ".cnb.yml"), YAML.stringify(item.workflow));
  const environment = { ...process.env, GIT_CEILING_DIRECTORIES: dirname(item.root) };
  delete environment.NODE_TEST_CONTEXT;
  delete environment.GIT_DIR;
  delete environment.GIT_WORK_TREE;
  const actual = mc199SpawnChecker(process.execPath, [mc199Checker], { cwd: item.root, env: environment,
    encoding: "utf8", timeout: 10000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(actual.error, undefined, "the actual checker must launch and finish");
  assert.equal(actual.signal, null, "the actual checker must finish without a signal");
  return actual;
}

for (const [name, mutate] of [
  ["the published separated source, candidate and production lanes", () => {}],
  ["reordered source and native jobs", (item) => {
    item.workflow["**"].pull_request.reverse();
    for (const lane of mc199Lanes) item.workflow[lane.branch][lane.event].reverse();
  }],
  ["extra required runner tags on both native platforms", (item) => {
    for (const lane of mc199Lanes) for (const platform of ["mac", "win"]) mc199NativeJob(item, lane, platform).runner.tags.push("fixture-native");
  }],
]) test(`MC199 actual product checker accepts ${name}`, (t) => {
  const item = mc199ProductFixture(t); mutate(item);
  const actual = mc199CheckProduct(item);
  assert.equal(actual.status, 0, actual.stderr);
  assert.match(actual.stdout, /产品内容完整性检查通过/u);
});

const mc199NativeMutations = [
  ["a missing platform job", (item, lane, platform) => {
    item.workflow[lane.branch][lane.event] = item.workflow[lane.branch][lane.event].filter((job) => job.name !== mc199NativeName(lane, platform));
  }],
  ["duplicate native platform jobs", (item, lane, platform) => {
    const duplicate = structuredClone(mc199NativeJob(item, lane, platform)); duplicate.name += "-duplicate";
    item.workflow[lane.branch][lane.event].push(duplicate);
  }],
  ["a wrong runner tag", (item, lane, platform) => { mc199NativeJob(item, lane, platform).runner.tags = ["linux"]; }],
  ["a non-array runner tag declaration", (item, lane, platform) => { mc199NativeJob(item, lane, platform).runner.tags = "mac,arm64,windows"; }],
  ["a non-group runner namespace", (item, lane, platform) => { mc199NativeJob(item, lane, platform).runner.namespace = "project"; }],
  ["a missing runner", (item, lane, platform) => { delete mc199NativeJob(item, lane, platform).runner; }],
  ["a Docker override on a native runner", (item, lane, platform) => { mc199NativeJob(item, lane, platform).docker = { image: "fixture/never-executed" }; }],
  ["missing stages", (item, lane, platform) => { delete mc199NativeJob(item, lane, platform).stages; }],
  ["an empty stage list", (item, lane, platform) => { mc199NativeJob(item, lane, platform).stages = []; }],
  ["a non-array stage declaration", (item, lane, platform) => { mc199NativeJob(item, lane, platform).stages = { script: "fixture" }; }],
  ["an extra null stage", (item, lane, platform) => { mc199NativeJob(item, lane, platform).stages.push(null); }],
  ["an extra empty stage", (item, lane, platform) => { mc199NativeJob(item, lane, platform).stages.push({ name: "empty fixture", script: " " }); }],
  ["a wrong builder entry", (item, lane, platform) => {
    const stage = mc199NativeJob(item, lane, platform).stages[0];
    stage.script = stage.script.replaceAll("build-tiger-desktop-native", "build-other-desktop-native");
  }],
  ["a builder in a different directory", (item, lane, platform) => {
    const stage = mc199NativeJob(item, lane, platform).stages[0];
    stage.script = stage.script.replaceAll("scripts/", "foreign/scripts/").replaceAll("scripts\\", "foreign\\scripts\\");
  }],
  ["a builder filename with an extra suffix", (item, lane, platform) => {
    const stage = mc199NativeJob(item, lane, platform).stages[0];
    stage.script = stage.script.replace(platform === "mac" ? ".sh " : ".ps1 ", platform === "mac" ? ".sh.backup " : ".ps1.backup ");
  }],
  ["a builder entry mentioned only in a comment", (item, lane, platform) => {
    const stage = mc199NativeJob(item, lane, platform).stages[0];
    stage.script = stage.script.split("\n").map((line) => /build-tiger-desktop-native/u.test(line) ? `# ${line}` : line).join("\n");
  }],
  ["a builder entry mentioned only as output", (item, lane, platform) => {
    mc199NativeJob(item, lane, platform).stages[0].script = platform === "mac"
      ? `echo scripts/build-tiger-desktop-native.sh fixture${lane.candidate ? " --candidate" : ""}`
      : `Write-Output scripts/build-tiger-desktop-native.ps1${lane.candidate ? " -Candidate" : ""}`;
  }],
];

for (const lane of mc199Lanes) for (const platform of ["mac", "win"]) {
  const label = `${lane.branch}/${lane.event}/${platform}`;
  for (const [name, mutate] of mc199NativeMutations) test(`MC199 actual product checker rejects ${label} ${name}`, (t) => {
    const item = mc199ProductFixture(t); mutate(item, lane, platform);
    const actual = mc199CheckProduct(item);
    assert.equal(actual.status, 1, actual.stdout);
    assert.ok(actual.stderr.includes(`CNB ${lane.branch}/${lane.event} 缺少必需的 ${platform} 原生桌面门禁`), actual.stderr);
  });
  for (const target of ["job", "stage"]) for (const [key, value] of [
    ["allow_failure", true], ["allowFailure", true], ["if", "$FIXTURE_BYPASS"],
    ["allow_failure", false], ["allowFailure", false], ["if", false],
  ]) test(`MC199 actual product checker rejects ${label} ${target} ${key}=${String(value)}`, (t) => {
    const item = mc199ProductFixture(t), job = mc199NativeJob(item, lane, platform);
    (target === "job" ? job : job.stages[0])[key] = value;
    const actual = mc199CheckProduct(item);
    assert.equal(actual.status, 1, actual.stdout);
    assert.ok(actual.stderr.includes(`CNB ${lane.branch}/${lane.event} 缺少必需的 ${platform} 原生桌面门禁`), actual.stderr);
  });
  test(`MC199 actual product checker rejects ${label} the wrong candidate mode`, (t) => {
    const item = mc199ProductFixture(t), job = mc199NativeJob(item, lane, platform);
    const flag = platform === "mac" ? "--candidate" : "-Candidate";
    job.stages[0].script = lane.candidate ? job.stages[0].script.replace(` ${flag}`, "") : job.stages[0].script.trimEnd() + ` ${flag}\n`;
    const actual = mc199CheckProduct(item);
    assert.equal(actual.status, 1, actual.stdout);
    assert.ok(actual.stderr.includes(`CNB ${lane.branch}/${lane.event} 缺少必需的 ${platform} 原生桌面门禁`), actual.stderr);
  });
}

for (const lane of mc199Lanes) test(`MC199 actual product checker rejects ${lane.branch}/${lane.event}/mac a missing arm64 tag`, (t) => {
  const item = mc199ProductFixture(t); mc199NativeJob(item, lane, "mac").runner.tags = ["mac"];
  const actual = mc199CheckProduct(item);
  assert.equal(actual.status, 1);
  assert.ok(actual.stderr.includes(`CNB ${lane.branch}/${lane.event} 缺少必需的 mac 原生桌面门禁`), actual.stderr);
});

for (const [name, mutate] of [
  ["missing source gates", (item) => { item.workflow["**"].pull_request.pop(); }],
  ["duplicate source gates", (item) => { item.workflow["**"].pull_request.push(structuredClone(item.workflow["**"].pull_request[0])); }],
  ["native jobs restored to source PRs", (item) => { item.workflow["**"].pull_request.push(structuredClone(mc199NativeJob(item, mc199Lanes[1], "mac"))); }],
  ["an optional source gate", (item) => { item.workflow["**"].pull_request[0].allow_failure = true; }],
  ["a conditional source stage", (item) => { item.workflow["**"].pull_request[0].stages[0].if = "$FIXTURE_BYPASS"; }],
]) test(`MC199 actual product checker rejects ${name}`, (t) => {
  const item = mc199ProductFixture(t); mutate(item);
  const actual = mc199CheckProduct(item);
  assert.equal(actual.status, 1, actual.stdout);
  assert.match(actual.stderr, /CNB \*\*\/pull_request 必须仅包含必需的 py-gate、oss-gate、protocol-gate 源码门禁/u);
});

test("MC199 actual product checker rejects a false PowerShell candidate switch", (t) => {
  const item = mc199ProductFixture(t), lane = mc199Lanes[1];
  const stage = mc199NativeJob(item, lane, "win").stages[0];
  stage.script = stage.script.replace(" -Candidate", " -Candidate:$false");
  const actual = mc199CheckProduct(item);
  assert.equal(actual.status, 1);
  assert.ok(actual.stderr.includes("CNB **/api_trigger_tiger_native_candidate 缺少必需的 win 原生桌面门禁"), actual.stderr);
});
