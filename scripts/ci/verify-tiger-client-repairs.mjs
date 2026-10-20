#!/usr/bin/env node
/** Tiger-only, exact reviewed repair overlay; the upstream state/verifier stay intact. */
import { createHash } from "node:crypto";
import { existsSync, lstatSync, readFileSync, realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { verifyClientFoundationConsumer } from "../../governance/scripts/verify-client-foundation-consumer.mjs";

const MANIFEST = ".workloom-client-repairs.json";
const REPOSITORY = "geniusdapeng-collab/workroom-tiger";
const TASK = "T-2026-1020-0901";
// Deliberately not a general extension allowlist or an upstream rollout policy.
const REPAIR_PATHS = new Map([
  ["apps/web/src/components/star-ring/StarRing.tsx", "MC-012"],
  ["apps/webc/src/lib/api.ts", "MC-018"],
  ["apps/web/consumer-e2e/clients.playwright.ts", "MC-015"],
  ["apps/web/src/pages/onboarding/Onboarding.tsx", "MC-027"],
  ["apps/web/src/components/SimBanner.tsx", "MC-027"],
]);
const TEST_PATHS = new Map([
  ["apps/web/src/components/star-ring/StarRing.contract.test.tsx", {
    card: "MC-012", repairs: ["apps/web/src/components/star-ring/StarRing.tsx"],
  }],
  ["apps/web/src/pages/onboarding/Onboarding.mode.test.tsx", {
    card: "MC-027", repairs: ["apps/web/src/pages/onboarding/Onboarding.tsx", "apps/web/src/components/SimBanner.tsx"],
  }],
]);
const SHA256 = /^[a-f0-9]{64}$/;
const COMMIT = /^[a-f0-9]{40}$/;
const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");

function regularFile(root, path) {
  if (typeof path !== "string" || !path || path.startsWith("/") || path.includes("\\")
    || path.split("/").some((part) => !part || part === "." || part === "..")) throw new Error("非法修复路径");
  let cursor = root;
  for (const part of path.split("/")) {
    cursor = join(cursor, part);
    if (lstatSync(cursor).isSymbolicLink()) throw new Error(`修复路径禁止 symlink：${path}`);
  }
  if (!lstatSync(cursor).isFile()) throw new Error(`修复路径不是文件：${path}`);
  return cursor;
}

export function verifyTigerClientRepairs(repoRoot) {
  const errors = [];
  let root;
  try { root = realpathSync(resolve(repoRoot, "governance")); }
  catch { return ["缺少可读取的 Tiger governance 根目录"]; }
  let original;
  try { original = verifyClientFoundationConsumer(root); }
  catch (error) { return [`客户端基座校验失败：${error.message}`]; }
  if (!existsSync(join(root, MANIFEST))) return original;
  const registered = new Set();
  try {
    const manifestPath = regularFile(root, MANIFEST);
    if (lstatSync(manifestPath).size > 65536) throw new Error("修复登记超过大小限制");
    const doc = JSON.parse(readFileSync(manifestPath, "utf8"));
    const statePath = regularFile(root, ".workloom-client-foundation.json");
    const stateBytes = readFileSync(statePath);
    const state = JSON.parse(stateBytes);
    if (doc?.schemaVersion !== "workloom.tiger-client-repairs/v1" || doc.repository !== REPOSITORY || doc.taskId !== TASK) {
      throw new Error("修复登记的 schema、仓库或任务不匹配");
    }
    if (doc.authorization?.basis !== "user-requested-repository-repair"
      || doc.authorization.issue !== `https://github.com/${REPOSITORY}/issues/24`
      || doc.authorization.pullRequest !== `https://github.com/${REPOSITORY}/pull/25`) {
      throw new Error("修复登记缺少本任务的授权来源与审查入口");
    }
    if (doc.base?.version !== state.version || doc.base.sourceCommit !== state.sourceCommit
      || !COMMIT.test(doc.base.sourceCommit) || doc.base.stateSha256 !== hash(stateBytes)
      || doc.base.verifierSha256 !== hash(readFileSync(regularFile(root, "scripts/verify-client-foundation-consumer.mjs")))) {
      throw new Error("修复登记与原基座版本、state 或原始校验器摘要不一致");
    }
    if (!Array.isArray(doc.repairs) || doc.repairs.length < 1 || doc.repairs.length > REPAIR_PATHS.size) throw new Error("修复文件数非法");
    const seen = new Set();
    for (const repair of doc.repairs) {
      if (!repair || !REPAIR_PATHS.has(repair.path) || REPAIR_PATHS.get(repair.path) !== repair.card || seen.has(repair.path)) throw new Error("修复路径未授权、卡号错误或重复");
      seen.add(repair.path);
      if (!SHA256.test(repair.upstreamSha256) || !SHA256.test(repair.repairedSha256)
        || repair.upstreamSha256 === repair.repairedSha256 || !COMMIT.test(repair.sourceCommit)
        || typeof repair.reason !== "string" || repair.reason.trim().length < 20) throw new Error(`修复登记字段不完整：${repair.path}`);
      if (state.managedFiles?.[repair.path]?.sha256 !== repair.upstreamSha256) throw new Error(`原基座文件摘要不匹配：${repair.path}`);
      if (hash(readFileSync(regularFile(root, repair.path))) !== repair.repairedSha256) throw new Error(`已审查修复摘要不匹配：${repair.path}`);
      registered.add(`受管客户端文件指纹漂移：${repair.path}`);
    }
    const requiredTests = new Set([...TEST_PATHS].filter(([, binding]) => binding.repairs.some((path) => seen.has(path))).map(([path]) => path));
    if (!Array.isArray(doc.testAdditions) || doc.testAdditions.length !== requiredTests.size) {
      throw new Error("客户端修复必须绑定对应的精确回归测试文件");
    }
    const seenTests = new Set();
    for (const test of doc.testAdditions) {
      if (!test || !requiredTests.has(test.path) || seenTests.has(test.path)
        || test.card !== TEST_PATHS.get(test.path)?.card || !SHA256.test(test.sha256)
        || state.managedFiles?.[test.path]) throw new Error("回归测试登记路径、卡号或摘要非法");
      seenTests.add(test.path);
      const file = regularFile(root, test.path);
      if (hash(readFileSync(file)) !== test.sha256 || (lstatSync(file).mode & 0o111)) throw new Error("回归测试文件摘要或模式不匹配");
      registered.add(`客户端根存在非白名单行业文件：${test.path}`);
    }
  } catch (error) {
    errors.push(`Tiger 本仓修复登记无效：${error.message}`);
  }
  // Only the exact, hash-bound source/test differences above may be accepted. Missing files,
  // modes, versions, symlinks and every other original gate error remain failures.
  return [...new Set([...original.filter((error) => errors.length > 0 || !registered.has(error)), ...errors])];
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  const errors = verifyTigerClientRepairs(process.cwd());
  if (errors.length) {
    for (const error of errors) console.error(error);
    process.exitCode = 1;
  } else if (existsSync(resolve("governance", MANIFEST))) {
    console.log("✅ 原客户端基座与校验器摘要保留；本任务登记的客户端修复及对应回归测试已逐文件核验，其余受管文件零漂移");
  } else console.log("✅ Tiger 客户端原基座严格校验通过；无本仓修复登记");
}
