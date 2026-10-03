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

test("CNB requires both native platforms for production and candidate triggers", () => {
  const governance = resolve(import.meta.dirname, "..");
  const repository = resolve(governance, "..");
  const product = JSON.parse(readFileSync(join(repository, "product.manifest.json"), "utf8"));
  assert.deepEqual(JSON.parse(readFileSync(join(governance, "product.manifest.json"), "utf8")), product);
  assert.equal(product.release.workflow, ".cnb.yml");
  const workflow = YAML.parse(readFileSync(join(repository, product.release.workflow), "utf8"));
  for (const [branch, event, candidate] of [["main", "push", false], ["**", "pull_request", true]]) {
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
