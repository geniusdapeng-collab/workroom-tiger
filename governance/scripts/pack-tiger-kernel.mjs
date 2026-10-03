#!/usr/bin/env node
/** Industry-owned portable kernel extension. Every external byte is locked to an official digest. */
import { createHash } from "node:crypto";
import { chmodSync, cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const GOVERNANCE = resolve(HERE, "..");
const ROOT = resolve(GOVERNANCE, "..");
const LOCK_FILE = join(HERE, "tiger-python-assets.json");
const { tarExtractionPlan } = createRequire(import.meta.url)("../apps/desktop/electron/bootstrap.cjs");
const REQUIRED_KERNEL = Object.freeze(["main.py", "product.manifest.json", "trading_system/agent_api.py", "trading_system/config.py", "scripts/tiger-agent.mjs", "scripts/tiger-agent-mcp.mjs", "scripts/tiger-agent-runtime.mjs", "bundles/trading/capabilities/repo.mjs"]);
const REQUIRED_GOVERNANCE = Object.freeze(["scripts/proposal-bridge.ts", "scripts/proposal-bridge-contract.mjs", "apps/desktop/electron/diagnostic-redaction.cjs"]);
export function sha256(data) { return createHash("sha256").update(data).digest("hex"); }
export function platformTarget(platform, arch) {
  if (platform === "mac" && arch === "arm64") return "mac-arm64";
  if (platform === "win" && arch === "x64") return "win-x64";
  throw new Error(`Tiger 支持 Mac M 芯片与 Windows x64；不支持 ${platform}-${arch}`);
}

function verifiedAsset(asset, directory, wheelDirectory) {
  if (!/^[a-f0-9]{64}$/u.test(asset.sha256) || !/^[A-Za-z0-9_.+-]+$/u.test(asset.file)
      || !/^https:\/\/(?:files\.pythonhosted\.org\/|github\.com\/astral-sh\/python-build-standalone\/)/u.test(asset.url)) throw new Error("Python 资产锁格式或来源无效");
  const candidates = [join(directory, asset.file), ...(wheelDirectory ? [join(directory, "wheels", wheelDirectory, asset.file)] : [])];
  let file = candidates.find((candidate) => existsSync(candidate));
  if (!file) {
    mkdirSync(directory, { recursive: true });
    file = candidates[0];
    const temporary = `${file}.part-${process.pid}`;
    const curl = process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "curl.exe") : "/usr/bin/curl";
    try {
      execFileSync(curl, ["--fail", "--location", "--retry", "3", "--connect-timeout", "20", "--max-time", "300", "--output", temporary, asset.url], { stdio: ["ignore", "ignore", "pipe"], timeout: 340_000 });
      if (sha256(readFileSync(temporary)) !== asset.sha256) throw new Error(`Python 上游资产摘要不匹配：${asset.file}`);
      renameSync(temporary, file);
    } finally { rmSync(temporary, { force: true }); }
  }
  if (!lstatSync(file).isFile() || lstatSync(file).isSymbolicLink() || sha256(readFileSync(file)) !== asset.sha256) throw new Error(`Python 缓存资产摘要不匹配：${asset.file}`);
  return file;
}

function filesUnder(root) {
  const found = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const full = join(directory, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`内核载荷禁止符号链接：${relative(root, full)}`);
      if (entry.isDirectory()) visit(full);
      else if (entry.isFile()) found.push({ path: relative(root, full).replaceAll("\\", "/"), sha256: sha256(readFileSync(full)), bytes: lstatSync(full).size });
    }
  };
  visit(root);
  return found.sort((left, right) => left.path.localeCompare(right.path));
}

export function assertKernelPayload(payloadRoot, target) {
  const runtime = join(payloadRoot, "runtime");
  const contract = JSON.parse(readFileSync(join(runtime, "industry-runtime.json"), "utf8"));
  if (contract.schemaVersion !== "workloom.industry-runtime/v1" || contract.productId !== "workroom-tiger" || contract.target !== target || contract.kernelDirectory !== "kernel") throw new Error("Tiger 内核载荷契约不匹配");
  const python = join(payloadRoot, "python", target === "win-x64" ? "python.exe" : "bin/python3");
  if (!existsSync(python)) throw new Error("Tiger Python 运行时缺失");
  for (const entry of REQUIRED_KERNEL) if (!existsSync(join(runtime, "kernel", entry))) throw new Error(`Tiger 内核载荷缺失：${entry}`);
  for (const entry of REQUIRED_GOVERNANCE) if (!existsSync(join(runtime, entry))) throw new Error(`Tiger 审批桥载荷缺失：${entry}`);
  const inventory = JSON.parse(readFileSync(join(runtime, "kernel-files.json"), "utf8"));
  const actual = filesUnder(join(runtime, "kernel"));
  if (JSON.stringify(inventory) !== JSON.stringify(actual)) throw new Error("Tiger 内核文件摘要与装配清单不一致");
  return { files: actual.length, target, kernelSha256: sha256(JSON.stringify(actual)) };
}

export function packTigerKernel({ platform, arch, payloadRoot, cache, builderPython = process.env.TIGER_BUILD_PYTHON_EXE }) {
  const target = platformTarget(platform, arch);
  const lock = JSON.parse(readFileSync(LOCK_FILE, "utf8"));
  if (lock.schemaVersion !== "tiger.python-assets/v1" || lock.pythonVersion !== "3.12.15" || !lock.targets[target]) throw new Error("Tiger Python 资产锁无效");
  const pinned = lock.targets[target];
  const output = resolve(payloadRoot);
  const cacheDir = resolve(cache);
  // Keep staging on the destination volume: Windows runners commonly put TEMP on C:
  // and the workspace on D:, where rename would otherwise fail with EXDEV.
  mkdirSync(dirname(output), { recursive: true });
  const stage = mkdtempSync(join(dirname(output), ".tiger-python-pack-"));
  try {
    const archive = verifiedAsset(pinned.runtime, cacheDir);
    const tar = process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "/usr/bin/tar";
    const extraction = tarExtractionPlan(archive, stage);
    try {
      if (extraction.stagedArchive) cpSync(archive, extraction.stagedArchive, { errorOnExist: true, force: false });
      execFileSync(tar, ["-xzf", extraction.archiveArg], { cwd: extraction.cwd, stdio: "pipe" });
    } finally { if (extraction.stagedArchive) rmSync(extraction.stagedArchive, { force: true }); }
    const pythonStage = join(stage, "python");
    const wheelStage = join(stage, "wheels");
    mkdirSync(wheelStage);
    const requirements = [];
    for (const wheel of pinned.wheels) {
      const source = verifiedAsset(wheel, cacheDir, target);
      cpSync(source, join(wheelStage, wheel.file));
      requirements.push(`${wheel.name}==${wheel.version} --hash=sha256:${wheel.sha256}`);
    }
    const requirementsFile = join(stage, "requirements.txt");
    writeFileSync(requirementsFile, requirements.join("\n") + "\n");
    const native = (target === "mac-arm64" && process.platform === "darwin" && process.arch === "arm64") || (target === "win-x64" && process.platform === "win32" && process.arch === "x64");
    const python = native ? join(pythonStage, target === "win-x64" ? "python.exe" : "bin/python3") : builderPython;
    if (!python || !isAbsolute(python) || !existsSync(python)) throw new Error("跨平台轮子装配必须提供绝对 TIGER_BUILD_PYTHON_EXE；原生构建使用内嵌 Python");
    const site = join(pythonStage, target === "win-x64" ? "Lib/site-packages" : "lib/python3.12/site-packages");
    execFileSync(python, ["-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--no-index", "--no-deps", "--require-hashes", "--only-binary=:all:", "--find-links", wheelStage, "--target", site, ...(native ? [] : ["--platform", target === "win-x64" ? "win_amd64" : "macosx_11_0_arm64", "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312", "--abi", "abi3", "--abi", "none"]), "-r", requirementsFile], { stdio: "inherit", timeout: 120_000 });
    const kernel = join(output, "runtime", "kernel");
    rmSync(kernel, { recursive: true, force: true });
    mkdirSync(kernel, { recursive: true });
    for (const entry of ["main.py", "trading_system", "product.manifest.json", "scripts/tiger-agent.mjs", "scripts/tiger-agent-mcp.mjs", "scripts/tiger-agent-runtime.mjs", "bundles/trading/capabilities/repo.mjs"]) {
      const source = join(ROOT, entry);
      if (!existsSync(source)) throw new Error(`Tiger 内核源文件缺失：${entry}`);
      mkdirSync(dirname(join(kernel, entry)), { recursive: true });
      cpSync(source, join(kernel, entry), { recursive: true, filter: (file) => !/(?:^|[\\/])(?:__pycache__|\.pytest_cache)(?:[\\/]|$)|\.py[co]$/u.test(file) });
    }
    for (const entry of REQUIRED_KERNEL) if (!existsSync(join(kernel, entry))) throw new Error(`Tiger 内核源文件缺失：${entry}`);
    for (const entry of REQUIRED_GOVERNANCE) {
      const source = join(GOVERNANCE, entry);
      if (!existsSync(source)) throw new Error(`Tiger 审批桥源文件缺失：${entry}`);
      mkdirSync(dirname(join(output, "runtime", entry)), { recursive: true });
      cpSync(source, join(output, "runtime", entry));
    }
    cpSync(requirementsFile, join(kernel, "requirements.runtime.txt"));
    cpSync(LOCK_FILE, join(output, "runtime", "python-assets.json"));
    const inventory = filesUnder(kernel);
    writeFileSync(join(output, "runtime", "kernel-files.json"), JSON.stringify(inventory, null, 2) + "\n");
    rmSync(join(output, "python"), { recursive: true, force: true });
    renameSync(pythonStage, join(output, "python"));
    if (target === "mac-arm64") chmodSync(join(output, "python", "bin", "python3"), 0o755);
    const pythonPath = target === "win-x64" ? "python/python.exe" : "python/bin/python3";
    const nodePath = target === "win-x64" ? "node/node.exe" : "node/bin/node";
    writeFileSync(join(output, "runtime", "industry-runtime.json"), JSON.stringify({ schemaVersion: "workloom.industry-runtime/v1", productId: "workroom-tiger", target,
      requiredParts: ["python"], kernelDirectory: "kernel", pythonVersion: lock.pythonVersion, pythonAssetSha256: pinned.runtime.sha256,
      wheelCount: pinned.wheels.length, kernelSha256: sha256(JSON.stringify(inventory)), outputs: "user-support-or-explicit-workspace",
      requiredFiles: [{ root: "support", path: pythonPath }, { root: "support", path: nodePath }, ...REQUIRED_KERNEL.map((path) => ({ root: "runtime", path: `kernel/${path}` }))],
      environment: { TIGER_KERNEL_ROOT: { root: "runtime", path: "kernel", required: true }, TIGER_PYTHON_EXE: { root: "support", path: pythonPath, required: true },
        TIGER_NODE_EXE: { root: "support", path: nodePath, required: true }, TIGER_PROPOSALS_DIR: { root: "support", path: "tiger/reports/review_proposals" },
        TIGER_EXECUTION_ENVIRONMENT: "paper", PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" },
      selftests: [{ name: "tiger-python-imports", executable: { root: "support", path: pythonPath },
        args: ["-I", "-c", "import pandas,numpy,requests,yfinance,yaml; print('tiger-python-ready')"], expectedStdout: "tiger-python-ready" }] }, null, 2) + "\n");
    return assertKernelPayload(output, target);
  } finally { rmSync(stage, { recursive: true, force: true }); }
}

function arg(name, fallback) { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i + 1] : fallback; }
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const target = platformTarget(arg("--platform"), arg("--arch"));
    const payloadRoot = arg("--payload", join(GOVERNANCE, "dist-payload"));
    const result = process.argv.includes("--verify") ? assertKernelPayload(payloadRoot, target)
      : packTigerKernel({ platform: arg("--platform"), arch: arg("--arch"), payloadRoot, cache: arg("--cache", process.env.TIGER_PYTHON_ASSET_CACHE || join(GOVERNANCE, "vendor", "python-assets")) });
    console.log(`Tiger Python/CLI/MCP 载荷已核验：${JSON.stringify(result)}`);
  } catch (error) { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; }
}
