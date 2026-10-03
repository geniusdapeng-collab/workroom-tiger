/**
 * trading 行业能力 · 仓库只读/预览（T-2026-1001-0011，通用模板）
 *
 * 供桌面 Agent（Codex / DeepSeek Harness）通过 `scripts/workloom-agent.mjs` 或
 * `scripts/workloom-agent-mcp.mjs` 调用。**只读/预览，不执行写操作**（loader 硬约束）：
 *   repoStatus     本仓自述：product.manifest 摘要 + bundles/docs/脚本计数
 *   repoDocs       docs/*.md 索引（文件名 + 一级标题），给 Agent 找文档用
 *   bundleSummary  默认行业包概览：skills / fences / pipelines / presets 计数
 *   repoReadiness  接入就绪度：Node、依赖、运行时资产、可选模型环境、MCP 入口、dsh-gate pin
 *   opsInventory   平台运营目录概览（platform-ops/，目录不存在时如实说明）
 *   assetsInventory 素材库概览（bundles/<bundle>/library/**，目录不存在时如实说明）
 *
 * 边界：只读本仓文件；路径监狱（仓内相对路径）；单文件 8MB 上限；
 * 不读取用户密钥文件、不回显环境密钥值；不联网、不写盘、不触发业务动作。
 */
import { existsSync, lstatSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
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

/** Industry-owned adapter reused by the source root and packaged governance entry. */
export function createRepoCapabilities(repositoryRoot) {
function root() { return resolve(repositoryRoot); }

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

/** 仓内路径监狱：只接受相对路径，realpath 复核防软链逃逸 */
function insideRepo(relativePath, label = "path") {
  const base = root();
  if (typeof relativePath !== "string" || !relativePath.trim() || relativePath.startsWith("/") || /^[A-Za-z]:[\\/]/u.test(relativePath)) {
    throw new RepoCapabilityError(`${label} 须为仓库内相对路径`);
  }
  const absolute = resolve(base, relativePath);
  const rel = relative(base, absolute);
  if (!rel || rel.startsWith("..") || rel.split(sep).includes("..")) {
    throw new RepoCapabilityError(`${label} 越出仓库根（路径监狱）`);
  }
  // Check each component even when the final file is missing; broken symlinks
  // and links to another file inside the repo are also rejected.
  let current = absolute;
  while (true) {
    try {
      if (lstatSync(current).isSymbolicLink()) throw new RepoCapabilityError(`${label} 不允许符号链接`, "UNSAFE_PATH");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const parent = resolve(current, "..");
    if (parent === current) break;
    current = parent;
  }
  if (existsSync(absolute)) {
    const real = relative(realpathSync(base), realpathSync(absolute));
    if (!real || real.startsWith("..") || real.split(sep).includes("..")) throw new RepoCapabilityError(`${label} 的真实路径越出仓库根`);
  }
  return absolute;
}

function readText(absolute, label) {
  insideRepo(relative(root(), absolute), label);
  if (!existsSync(absolute)) return null;
  const info = statSync(absolute);
  if (!info.isFile()) throw new RepoCapabilityError(`${label} 不是普通文件`);
  if (info.size > MAX_FILE_BYTES) throw new RepoCapabilityError(`${label} 超过读取上限`, "TOO_LARGE");
  return readFileSync(absolute, "utf8");
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
  insideRepo(relative(root(), dir), "directory");
  if (!existsSync(dir)) return 0;
  let total = 0;
  const walk = (current, depth) => {
    if (depth > 12) throw new RepoCapabilityError("目录深度超过读取上限", "TOO_LARGE");
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue;
      const child = join(current, entry.name);
      insideRepo(relative(root(), child), "directory entry");
      if (entry.isDirectory()) walk(child, depth + 1);
      else total += 1;
    }
  };
  walk(dir, 0);
  return total;
}

function listDirs(dir) {
  insideRepo(relative(root(), dir), "directory");
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => {
      if (entry.name.startsWith(".")) return false;
      insideRepo(relative(root(), join(dir, entry.name)), "directory entry");
      return entry.isDirectory();
    })
    .map((entry) => entry.name)
    .sort();
}

function envFileKeyPresence(name) {
  return { present: Boolean(process.env[name]?.trim()), source: "process-environment" };
}

function governanceRoot() {
  const candidate = insideRepo("governance", "governance");
  return existsSync(candidate) ? candidate : root();
}

function industryDir() {
  const nested = insideRepo(`governance/bundles/${BUNDLE_ID}`, "industry bundle");
  const manifest = insideRepo(`governance/bundles/${BUNDLE_ID}/bundle.json`, "industry bundle manifest");
  return existsSync(manifest) ? nested
    : insideRepo(`bundles/${BUNDLE_ID}`, "industry bundle");
}

/* ---------------------------------- 能力 ---------------------------------- */

/** 本仓自述：manifest 摘要 + 关键目录计数 */
function repoStatus(input) {
  assertKeys(input, []);
  const base = root();
  const manifest = readJson(join(base, "product.manifest.json"), "product.manifest.json") ?? {};
  const docsDir = insideRepo("docs", "docs directory");
  const docsCount = existsSync(docsDir)
    ? readdirSync(docsDir).filter((name) => name.endsWith(".md")).length
    : 0;
  return {
    productId: manifest.productId ?? null,
    defaultBundle: manifest.defaultBundle ?? BUNDLE_ID,
    demoWorkspaceSlug: manifest.demoWorkspaceSlug ?? null,
    desktopPortOffset: manifest.desktop?.portOffset ?? null,
    bundles: listDirs(join(base, "bundles")),
    docsMarkdownCount: docsCount,
    scriptsToolsFiles: countFiles(join(base, "scripts", "tools")),
    note: "只读自述；行业语义与运行能力以本仓文档与清单为准",
  };
}

/** docs/*.md 索引：文件名 + 一级标题（Agent 找文档用） */
function repoDocs(input) {
  const value = assertKeys(input, ["limit"]);
  if (value.limit !== undefined && (!Number.isInteger(value.limit) || value.limit < 1 || value.limit > DOC_LIMIT)) throw new RepoCapabilityError("limit 须为 1-60 整数");
  const limit = value.limit ?? DOC_LIMIT;
  const base = root();
  const docsDir = insideRepo("docs", "docs directory");
  if (!existsSync(docsDir)) return { found: false, docs: [], hint: "本仓没有 docs/ 目录" };
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
function bundleSummary(input) {
  assertKeys(input, []);
  const bundleDir = industryDir();
  if (!existsSync(bundleDir)) return { found: false, bundle: BUNDLE_ID, hint: "没有该行业包目录" };
  const manifest = readJson(join(bundleDir, "bundle.json"), `bundles/${BUNDLE_ID}/bundle.json`);
  const counts = {};
  for (const name of ["skills", "fences", "pipelines", "presets", "library"]) {
    counts[name] = countFiles(join(bundleDir, name));
  }
  const fenceIds = [];
  const fencesDir = join(bundleDir, "fences");
  if (existsSync(fencesDir)) {
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
    sourcePath: relative(root(), bundleDir).split(sep).join("/"),
    version: manifest?.version ?? null,
    counts,
    fenceRuleSample: fenceIds,
    note: "只读概览；围栏语义以 fences/*.yml 原文为准",
  };
}

/** 接入就绪度（预览，不执行）：Agent 上手前自检 */
function repoReadiness(input) {
  assertKeys(input, []);
  const base = governanceRoot();
  const nodeMajor = Number(process.versions.node.split(".")[0]);
  const gatePkg = readJson(join(base, "packages", "runtime", "dsh-gate", "package.json"), "dsh-gate/package.json");
  const ark = envFileKeyPresence("VOLCENGINE_ARK_API_KEY");
  const items = [
    { id: "node", ok: nodeMajor >= 24, detail: `Node ${process.versions.node}（要求 ≥ 24）` },
    { id: "deps", ok: existsSync(join(base, "node_modules")), detail: "node_modules（缺失先 pnpm install）" },
    { id: "runtime-deps", ok: existsSync(join(base, ".workloom-runtime-deps")), detail: "受控运行时资产清单" },
    { id: "mcp-entry", ok: existsSync(join(base, "scripts", "workloom-agent-mcp.mjs")), detail: "stdio MCP 入口" },
    { id: "optional-model-env", ok: ark.present, detail: ark.present ? "可选模型环境变量已配置（不回显值）" : "可选模型环境变量未配置；只读入口不需要" },
    { id: "dsh-gate", ok: Boolean(gatePkg), detail: `内置 DSH 组件 pin：${gatePkg ? { ...gatePkg.dependencies, ...gatePkg.devDependencies }["@deepseek-ai/dsh"] : "未登记"}` },
  ];
  return { ready: items.filter((item) => ["node", "deps", "mcp-entry"].includes(item.id)).every((item) => item.ok), items };
}

/** 平台运营目录概览（andromeda 专用；目录不存在时如实说明） */
function opsInventory(input) {
  assertKeys(input, []);
  const dir = join(root(), "platform-ops");
  if (!existsSync(dir)) return { found: false, hint: "本仓没有 platform-ops/ 目录（平台运营专属）" };
  const sections = listDirs(dir).map((name) => ({ name, files: countFiles(join(dir, name)) }));
  return { found: true, sections, totalFiles: countFiles(dir) };
}

/** 素材库概览（视频类行业仓专用；目录不存在时如实说明） */
function assetsInventory(input) {
  assertKeys(input, []);
  const dir = join(industryDir(), "library");
  if (!existsSync(dir)) return { found: false, bundle: BUNDLE_ID, hint: "本仓行业包没有 library/ 素材目录" };
  const sections = listDirs(dir).map((name) => ({ name, files: countFiles(join(dir, name)) }));
  return { found: true, bundle: BUNDLE_ID, sections, totalFiles: countFiles(dir) };
}
return { repoStatus, repoDocs, bundleSummary, repoReadiness, opsInventory, assetsInventory };
}

const capabilities = createRepoCapabilities(resolve(import.meta.dirname, "..", "..", ".."));
export const { repoStatus, repoDocs, bundleSummary, repoReadiness, opsInventory, assetsInventory } = capabilities;
