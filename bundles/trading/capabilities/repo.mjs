/**
 * trading 行业能力 · 仓库只读/预览（T-2026-1001-0011，通用模板）
 *
 * 供桌面 Agent（Codex / DeepSeek Harness）通过 `scripts/workloom-agent.mjs` 或
 * `scripts/workloom-agent-mcp.mjs` 调用。**只读/预览，不执行写操作**（loader 硬约束）：
 *   repoStatus     本仓自述：product.manifest 摘要 + bundles/docs/脚本计数
 *   repoDocs       docs/*.md 索引（文件名 + 一级标题），给 Agent 找文档用
 *   bundleSummary  默认行业包概览：skills / fences / pipelines / presets 计数
 *   repoReadiness  接入就绪度：Python 引擎与 governance 治理壳布局、Node、MCP 入口
 *   opsInventory   平台运营目录概览（platform-ops/，目录不存在时如实说明）
 *   assetsInventory 素材库概览（governance/bundles/<bundle>/library/**，目录不存在时如实说明）
 *
 * 边界：只读本仓文件；路径监狱（仓内相对路径）；单文件 8MB / 2 万行上限；
 * 不读取用户环境/密钥文件；不联网、不写盘、不触发任何业务动作。
 */
import { closeSync, constants, existsSync, fstatSync, lstatSync, openSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

const BUNDLE_ID = "trading";
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const DOC_LIMIT = 60;

export class RepoCapabilityError extends Error {
  constructor(message, code = "BAD_REQUEST") {
    super(message);
    this.name = "RepoCapabilityError";
    this.code = code;
  }
}

function root() {
  return resolve(import.meta.dirname, "..", "..", "..");
}

function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assertKeys(input, allowed) {
  const value = input === undefined ? {} : input;
  if (!plain(value)) throw new RepoCapabilityError("输入须为 JSON 对象");
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new RepoCapabilityError(`不支持字段 ${key}`);
  }
  return value;
}

/** All file reads stay below this module's repository; symbolic and hard links are rejected. */
function insideRepo(relativePath, label = "path") {
  const base = root();
  if (typeof relativePath !== "string" || !relativePath.trim() || relativePath.startsWith("/") || /^[A-Za-z]:/u.test(relativePath)) {
    throw new RepoCapabilityError(`${label} 须为仓库内相对路径`);
  }
  const absolute = resolve(base, relativePath);
  const rel = relative(base, absolute);
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`)) throw new RepoCapabilityError(`${label} 越出仓库根`);
  let current = base;
  for (const part of rel.split(sep)) {
    current = join(current, part);
    let info;
    try { info = lstatSync(current); }
    catch (error) { if (error.code === "ENOENT") break; throw error; }
    if (info.isSymbolicLink()) throw new RepoCapabilityError(`${label} 含符号链接`, "UNSAFE_PATH");
  }
  if (existsSync(absolute)) {
    const real = relative(realpathSync(base), realpathSync(absolute));
    if (!real || real === ".." || real.startsWith(`..${sep}`)) throw new RepoCapabilityError(`${label} 的真实路径越出仓库根`, "UNSAFE_PATH");
  }
  return absolute;
}

function confined(absolute, label = "path") { return insideRepo(relative(root(), absolute), label); }
function directoryExists(absolute) {
  const path = confined(absolute);
  return existsSync(path) && lstatSync(path).isDirectory();
}
function fileExists(absolute) {
  const path = confined(absolute);
  return existsSync(path) && lstatSync(path).isFile();
}
function readText(absolute, label) {
  const path = confined(absolute, label);
  if (!existsSync(path)) return null;
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.nlink !== 1) throw new RepoCapabilityError(`${label} 不是独立普通文件`, "UNSAFE_PATH");
    if (info.size > MAX_FILE_BYTES) throw new RepoCapabilityError(`${label} 超过读取上限`, "TOO_LARGE");
    const text = readFileSync(fd, "utf8");
    if (Buffer.byteLength(text) > MAX_FILE_BYTES || text.split("\n").length > 20000) throw new RepoCapabilityError(`${label} 超过读取上限`, "TOO_LARGE");
    confined(path, label);
    return text;
  } finally { closeSync(fd); }
}

function readJson(absolute, label) {
  const text = readText(absolute, label);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new RepoCapabilityError(`${label} 不是合法 JSON：${error instanceof Error ? error.message : String(error)}`);
  }
}

function countFiles(dir) {
  if (!directoryExists(dir)) return 0;
  let total = 0;
  const walk = (current, depth) => {
    if (depth > 4) return;
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue;
      const child = confined(join(current, entry.name));
      if (entry.isDirectory()) walk(child, depth + 1);
      else if (entry.isFile()) total += 1;
    }
  };
  walk(dir, 0);
  return total;
}

function listDirs(dir) {
  if (!directoryExists(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith("."))
    .map((entry) => entry.name)
    .sort();
}

/* ---------------------------------- 能力 ---------------------------------- */

/** 本仓自述：manifest 摘要 + 关键目录计数 */
export function repoStatus(input) {
  assertKeys(input, []);
  const base = root();
  const manifest = readJson(join(base, "product.manifest.json"), "product.manifest.json") ?? {};
  const docsCount = directoryExists(join(base, "docs"))
    ? readdirSync(join(base, "docs")).filter((name) => name.endsWith(".md")).length
    : 0;
  return {
    engineRoot: base,
    governanceRoot: join(base, "governance"),
    productId: manifest.productId ?? null,
    defaultBundle: manifest.defaultBundle ?? BUNDLE_ID,
    demoWorkspaceSlug: manifest.demoWorkspaceSlug ?? null,
    desktopPortOffset: manifest.desktop?.portOffset ?? null,
    bundles: listDirs(join(base, "governance", "bundles")),
    docsMarkdownCount: docsCount,
    scriptsToolsFiles: countFiles(join(base, "scripts", "tools")),
    note: "只读自述；行业语义与运行能力以本仓文档与清单为准",
  };
}

/** docs/*.md 索引：文件名 + 一级标题（Agent 找文档用） */
export function repoDocs(input) {
  const value = assertKeys(input, ["limit"]);
  if (value.limit !== undefined && (!Number.isInteger(value.limit) || value.limit < 1 || value.limit > DOC_LIMIT)) {
    throw new RepoCapabilityError("limit 须为 1 到 60 的整数");
  }
  const limit = value.limit ?? DOC_LIMIT;
  const base = root();
  const docsDir = join(base, "docs");
  if (!directoryExists(docsDir)) return { found: false, docs: [], hint: "本仓没有 docs/ 目录" };
  const docs = readdirSync(docsDir)
    .filter((name) => name.endsWith(".md"))
    .sort()
    .slice(0, limit)
    .map((name) => {
      const text = readText(join(docsDir, name), `docs/${name}`) ?? "";
      const title = text.split("\n").find((line) => line.startsWith("# "))?.replace(/^#\s*/u, "").trim() ?? null;
      return { file: `docs/${name}`, title };
    });
  return { found: true, count: docs.length, docs };
}

/** 默认行业包概览：skills / fences / pipelines / presets 计数 + 围栏规则样例 */
export function bundleSummary(input) {
  assertKeys(input, []);
  const bundleDir = join(root(), "governance", "bundles", BUNDLE_ID);
  if (!directoryExists(bundleDir)) return { found: false, bundle: BUNDLE_ID, hint: "没有该行业包目录" };
  const manifest = readJson(join(bundleDir, "bundle.json"), `governance/bundles/${BUNDLE_ID}/bundle.json`);
  const counts = {};
  for (const name of ["skills", "fences", "pipelines", "presets", "library"]) {
    counts[name] = countFiles(join(bundleDir, name));
  }
  const fenceIds = [];
  const fencesDir = join(bundleDir, "fences");
  if (directoryExists(fencesDir)) {
    for (const name of readdirSync(fencesDir).sort()) {
      if (!/\.ya?ml$/u.test(name)) continue;
      const text = readText(join(fencesDir, name), `fences/${name}`) ?? "";
      for (const match of text.matchAll(/^\s*-?\s*(?:rule_id|id):\s*([A-Za-z0-9._-]+)/gmu)) {
        if (fenceIds.length < 12) fenceIds.push(match[1]);
      }
    }
  }
  return {
    found: true,
    bundle: BUNDLE_ID,
    location: `governance/bundles/${BUNDLE_ID}`,
    version: manifest?.version ?? null,
    counts,
    fenceRuleSample: fenceIds,
    note: "只读概览；围栏语义以 fences/*.yml 原文为准",
  };
}

/** 接入就绪度（预览，不执行）：Agent 上手前自检 */
export function repoReadiness(input) {
  assertKeys(input, []);
  const base = root();
  const governance = join(base, "governance");
  const nodeMajor = Number(process.versions.node.split(".")[0]);
  const gatePkg = readJson(join(governance, "packages", "runtime", "dsh-gate", "package.json"), "governance/dsh-gate/package.json");
  const items = [
    { id: "node", ok: nodeMajor >= 24, detail: `Node ${process.versions.node}（要求 ≥ 24）` },
    { id: "engine", ok: fileExists(join(base, "trading_system", "__init__.py")), detail: "根目录 Python 引擎；实际 Python 版本及依赖由 tiger-agent doctor 检查" },
    { id: "governance-deps", ok: directoryExists(join(governance, "node_modules")), detail: "governance/node_modules；独立 Tiger CLI/MCP 使用 Node 内置模块" },
    { id: "runtime-deps", ok: fileExists(join(governance, ".workloom-runtime-deps")) || directoryExists(join(governance, ".workloom-runtime-deps")), detail: "governance/.workloom-runtime-deps（治理壳运行时资产，非公开 paper 必需项）" },
    { id: "mcp-entry", ok: fileExists(join(base, "scripts", "workloom-agent-mcp.mjs")), detail: "原通用只读 stdio MCP 入口" },
    { id: "tiger-mcp-entry", ok: fileExists(join(base, "scripts", "tiger-agent-mcp.mjs")), detail: "独立 Tiger stdio MCP 入口，默认只读" },
    { id: "dsh-gate", ok: Boolean(gatePkg), detail: `governance 内置 DSH 组件 pin：${gatePkg ? { ...gatePkg.dependencies, ...gatePkg.devDependencies }["@deepseek-ai/dsh"] ?? "未登记" : "未登记"}` },
  ];
  return {
    ready: items.filter((item) => ["node", "engine", "mcp-entry", "tiger-mcp-entry"].includes(item.id)).every((item) => item.ok),
    engineRoot: base, governanceRoot: governance, items,
    note: "这是文件布局检查；未启动 Python、读取密钥、连接服务或验证业务执行。请继续调用 tiger_doctor。",
  };
}

/** 平台运营目录概览（andromeda 专用；目录不存在时如实说明） */
export function opsInventory(input) {
  assertKeys(input, []);
  const dir = join(root(), "platform-ops");
  if (!directoryExists(dir)) return { found: false, hint: "本仓没有 platform-ops/ 目录（平台运营专属）" };
  const sections = listDirs(dir).map((name) => ({ name, files: countFiles(join(dir, name)) }));
  return { found: true, sections, totalFiles: countFiles(dir) };
}

/** 素材库概览（视频类行业仓专用；目录不存在时如实说明） */
export function assetsInventory(input) {
  assertKeys(input, []);
  const dir = join(root(), "governance", "bundles", BUNDLE_ID, "library");
  if (!directoryExists(dir)) return { found: false, bundle: BUNDLE_ID, hint: "本仓行业包没有 library/ 素材目录" };
  const sections = listDirs(dir).map((name) => ({ name, files: countFiles(join(dir, name)) }));
  return { found: true, bundle: BUNDLE_ID, sections, totalFiles: countFiles(dir) };
}
