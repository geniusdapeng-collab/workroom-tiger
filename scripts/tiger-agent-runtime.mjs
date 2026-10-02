/** Tiger-owned research launcher. Shared WorkLoom capability loader stays read-only. */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export const MAX_INPUT_BYTES = 64 * 1024;
export const MAX_FRAME_BYTES = 128 * 1024;
const MAX_OUTPUT_BYTES = 2 * 1024 * 1024;
const KEY = /^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/u;
const TENANT = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/u;
const SHA = /^[a-f0-9]{64}$/u;
const MODES = ["daily", "premarket", "intraday", "backtest", "tune", "review"];
const EMPLOYEES = ["scanner", "mrs", "risk", "review"];
const TERMINAL = new Set(["succeeded", "degraded", "failed", "cancelled", "timed_out"]);
const hash = (data) => createHash("sha256").update(data).digest("hex");
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const integer = (minimum, maximum, defaultValue) => ({ type: "integer", minimum, maximum, default: defaultValue });
const RISK_LIMIT_DEFAULTS = Object.freeze({ risk_r_pct: .008, max_single_position_pct: .20, gross_cap: .90 });
const riskSchema = { type: "object", properties: Object.fromEntries(Object.entries(RISK_LIMIT_DEFAULTS)
  .map(([name, maximum]) => [name, { type: "number", exclusiveMinimum: 0, maximum }])),
  required: Object.keys(RISK_LIMIT_DEFAULTS), additionalProperties: false, default: RISK_LIMIT_DEFAULTS };
const sourceSchema = { type: "object", properties: {
  jobId: { type: "string", pattern: KEY.source }, resultSha256: { type: "string", pattern: SHA.source },
}, required: ["jobId", "resultSha256"], additionalProperties: false };
const common = {
  environment: { type: "string", enum: ["simulation", "paper"] },
  idempotencyKey: { type: "string", minLength: 8, maxLength: 80, pattern: KEY.source },
  provider: { type: "string", enum: ["demo", "yahoo", "stooq", "tencent", "sina", "eastmoney"], default: "demo" },
  market: { type: "string", enum: ["us", "cn", "hk"], default: "us" },
  universe: { type: "string", enum: ["core", "extended"], default: "core" },
  topN: integer(1, 100, 20), maxPicks: integer(1, 25, 5),
  account: { type: "number", minimum: 100, maximum: 100_000_000, default: 100_000 },
  timeoutSeconds: integer(1, 900, 300),
  llmMode: { type: "string", enum: ["disabled", "configured"], default: "disabled" },
  riskLimits: riskSchema,
};
const pipelineSchema = { type: "object", properties: {
  ...common, mode: { type: "string", enum: MODES }, sourceJob: sourceSchema,
  btDays: integer(5, 490, 260), trainDays: integer(20, 252, 126),
  testDays: integer(5, 126, 63), stepDays: integer(5, 126, 63),
  cycles: integer(1, 100, 1), intervalSeconds: integer(0, 3600, 0),
  reviewFrequency: { type: "string", enum: ["daily", "weekly"], default: "daily" },
}, required: ["environment", "idempotencyKey", "mode"], additionalProperties: false,
allOf: [
  { if: { properties: { mode: { enum: ["intraday", "review"] } } }, then: { required: ["sourceJob"] } },
  { if: { properties: { mode: { enum: ["backtest", "tune"] } } }, then: { properties: { market: { const: "us" } } } },
] };
const employeeSchema = { type: "object", properties: {
  ...common, employee: { type: "string", enum: EMPLOYEES }, sourceJob: sourceSchema,
  reviewFrequency: { type: "string", enum: ["daily", "weekly"], default: "daily" },
}, required: ["environment", "idempotencyKey", "employee"], additionalProperties: false,
allOf: [{ if: { properties: { employee: { const: "review" } } }, then: { required: ["sourceJob"] } }] };
const jobSchema = { type: "object", properties: { jobId: { type: "string", pattern: KEY.source } },
  required: ["jobId"], additionalProperties: false };

export const TOOLS = [
  { name: "tiger.pipeline.run", title: "Run Tiger research pipeline",
    description: "Runs real kernel daily/premarket, US backtest/WFA research, or verified-source intraday/read-only review. Only simulation/paper; no approvals, parameter application or brokerage orders. Missing LLM output is explicitly degraded. Mode-specific extraneous fields are rejected.",
    inputSchema: pipelineSchema, annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true } },
  { name: "tiger.employee.run", title: "Run a directed Tiger employee",
    description: "Calls the actual scanner, MRS, risk manager or review chief implementation. Risk prepares the full pipeline; review requires a verified daily source job. Governance preset declarations are separate; no live orders.",
    inputSchema: employeeSchema, annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true } },
  { name: "tiger.job.get", title: "Read and verify a Tiger job receipt",
    description: "Verifies completion, receipt and every artifact SHA256 before returning a local job receipt. Server synchronization is separately marked false.",
    inputSchema: jobSchema, annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false } },
  { name: "tiger.job.artifacts", title: "Read verified Tiger artifacts",
    description: "Lists artifact checksums or reads one listed artifact as UTF-8 (up to 512 KiB); names are matched to the verified manifest, never arbitrary file paths.",
    inputSchema: { ...jobSchema, properties: { ...jobSchema.properties, name: { type: "string", minLength: 1, maxLength: 240 } } },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false } },
  { name: "tiger.job.cancel", title: "Cancel an active local Tiger job",
    description: "Cancels a job owned by this launcher, stops its kernel process and records a cancelled receipt. Jobs running in another launcher cannot be killed by this tool.",
    inputSchema: jobSchema, annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false } },
];

export class TigerAgentError extends Error {
  constructor(code, message) { super(message); this.name = "TigerAgentError"; this.code = code; }
}
const reject = (code, message) => { throw new TigerAgentError(code, message); };

export function validateObject(input, allowed, required = []) {
  if (!object(input)) reject("INVALID_INPUT", "Input must be a JSON object");
  if (Object.keys(input).some((key) => !allowed.includes(key)) || required.some((key) => !Object.hasOwn(input, key))) {
    reject("INVALID_INPUT", "Unexpected or missing input fields");
  }
  return input;
}

function normalizeRiskLimits(input) {
  validateObject(input, Object.keys(RISK_LIMIT_DEFAULTS), Object.keys(RISK_LIMIT_DEFAULTS));
  for (const [name, maximum] of Object.entries(RISK_LIMIT_DEFAULTS)) {
    if (typeof input[name] !== "number" || !Number.isFinite(input[name]) || input[name] <= 0 || input[name] > maximum) {
      reject("INVALID_INPUT", `riskLimits.${name} must be finite, positive and no greater than the kernel limit`);
    }
  }
  return { ...input };
}

export function normalizeRequest(input) {
  validateObject(input, ["operation", ...Object.keys(pipelineSchema.properties), "employee"],
    ["operation", "environment", "idempotencyKey"]);
  const operation = input.operation;
  if (!["pipeline", "employee"].includes(operation)) reject("INVALID_INPUT", "operation must be pipeline or employee");
  const mode = operation === "pipeline" ? input.mode : null;
  const employee = operation === "employee" ? input.employee : null;
  if ((operation === "pipeline" && !MODES.includes(mode)) || (operation === "employee" && !EMPLOYEES.includes(employee))) {
    reject("INVALID_INPUT", "Unsupported mode or employee");
  }
  const allowed = ["operation", ...Object.keys(common), operation === "pipeline" ? "mode" : "employee"];
  if (["backtest", "tune"].includes(mode)) allowed.push("btDays");
  if (mode === "tune") allowed.push("trainDays", "testDays", "stepDays");
  if (mode === "intraday") allowed.push("sourceJob", "cycles", "intervalSeconds");
  if (mode === "review" || employee === "review") allowed.push("sourceJob", "reviewFrequency");
  validateObject(input, allowed);
  const normalized = { operation, ...(mode ? { mode } : { employee }) };
  const properties = { ...common };
  if (["backtest", "tune"].includes(mode)) properties.btDays = integer(5, 490, mode === "tune" ? 380 : 260);
  if (mode === "tune") Object.assign(properties, {
    trainDays: integer(20, 252, 126), testDays: integer(5, 126, 63), stepDays: integer(5, 126, 63),
  });
  if (mode === "intraday") Object.assign(properties, { cycles: integer(1, 100, 1), intervalSeconds: integer(0, 3600, 0) });
  if (mode === "review" || employee === "review") properties.reviewFrequency = { type: "string", enum: ["daily", "weekly"], default: "daily" };
  for (const [name, schema] of Object.entries(properties)) {
    const value = Object.hasOwn(input, name) ? input[name] : schema.default;
    if (name === "riskLimits") {
      normalized[name] = normalizeRiskLimits(value);
      continue;
    } else if (schema.type === "string") {
      if (typeof value !== "string" || (schema.enum && !schema.enum.includes(value)) || (schema.pattern && !new RegExp(schema.pattern, "u").test(value))) {
        reject("INVALID_INPUT", `Unsupported ${name}`);
      }
    } else if (typeof value !== "number" || !Number.isFinite(value) || value < schema.minimum || value > schema.maximum || (schema.type === "integer" && !Number.isInteger(value))) {
      reject("INVALID_INPUT", `${name} is outside the supported finite range`);
    }
    normalized[name] = value;
  }
  if (["backtest", "tune"].includes(mode) && normalized.market !== "us") reject("UNSUPPORTED_MARKET", "Historical research currently supports us only");
  if (mode === "tune" && normalized.btDays < normalized.trainDays + normalized.testDays) reject("INVALID_INPUT", "btDays must cover trainDays plus testDays");
  if (["intraday", "review"].includes(mode) || employee === "review") {
    if (!object(input.sourceJob)) reject("INVALID_INPUT", "sourceJob must be a JSON object");
    const source = validateObject(input.sourceJob, ["jobId", "resultSha256"], ["jobId", "resultSha256"]);
    if (typeof source.jobId !== "string" || !KEY.test(source.jobId) || typeof source.resultSha256 !== "string" || !SHA.test(source.resultSha256) || source.jobId === normalized.idempotencyKey) {
      reject("INVALID_INPUT", "sourceJob requires a different jobId and SHA256");
    }
    normalized.sourceJob = { jobId: source.jobId, resultSha256: source.resultSha256 };
  }
  return normalized;
}

export function launcherOptions(argv) {
  const options = {};
  const rest = [];
  const keys = new Map([["--workspace", "workspace"], ["--tenant", "tenant"], ["--kernel", "kernel"], ["--python", "python"]]);
  for (let index = 0; index < argv.length; index += 1) {
    const key = keys.get(argv[index]);
    if (!key) { rest.push(argv[index]); continue; }
    if (Object.hasOwn(options, key) || !argv[index + 1] || argv[index + 1].startsWith("--")) reject("INVALID_ARGUMENTS", "Missing or duplicate launcher option");
    options[key] = argv[++index];
  }
  options.kernel ??= process.env.TIGER_KERNEL_ROOT ?? resolve(import.meta.dirname, "..");
  options.tenant ??= process.env.TIGER_TENANT_ID ?? "local";
  options.python ??= process.env.TIGER_PYTHON_EXE ?? join(options.kernel, "..", "runtime", "python", process.platform === "win32" ? "python.exe" : "bin/python3");
  if (!options.workspace || !isAbsolute(options.workspace) || !isAbsolute(options.kernel) || !isAbsolute(options.python) || !TENANT.test(options.tenant)) {
    reject("INVALID_ARGUMENTS", "Explicit absolute --workspace and trusted absolute kernel/Python paths are required");
  }
  return { options, rest };
}

export async function assertSafePath(path, missing = false) {
  if (!isAbsolute(path) || path.split(/[\\/]/u).includes("..")) reject("UNSAFE_PATH", "An absolute canonical path is required");
  let current = resolve(path);
  while (true) {
    try {
      if ((await lstat(current)).isSymbolicLink()) reject("UNSAFE_PATH", "Symbolic links are not allowed");
    } catch (error) {
      if (error.code !== "ENOENT" || !missing) throw error;
    }
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return path;
}

export function safeError(error) {
  return { status: "failed", error: { code: error instanceof TigerAgentError ? error.code : "LAUNCHER_FAILED",
    message: error instanceof TigerAgentError ? error.message : "Tiger launcher failed; no successful completion receipt" } };
}

function childEnvironment(options, key, request) {
  const runtime = join(options.workspace, "jobs", options.tenant, key ?? "_reader", "_runtime");
  const env = {};
  for (const name of ["PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"]) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  if (request?.provider !== "demo") {
    for (const name of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "KIMI_API_KEY", "TIINGO_API_KEY", "IFIND_GW_BASE", "IFIND_GW_TOKEN"]) {
      if (process.env[name] !== undefined) env[name] = process.env[name];
    }
  }
  if (request?.llmMode === "configured") {
    for (const [name, value] of Object.entries(process.env)) {
      if (/^(?:LLM_|OPENAI_|KIMI_)/u.test(name)) env[name] = value;
    }
    env.LLM_BACKEND ??= "auto";
  } else env.LLM_BACKEND = "kimi";
  Object.assign(env, { PYTHONPATH: options.kernel, PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1",
    PYTHONUNBUFFERED: "1", PYTHONUTF8: "1", TIGER_RUN_HOME: join(runtime, "home"),
    XDG_CACHE_HOME: join(runtime, "cache"), XDG_CONFIG_HOME: join(runtime, "config"),
    APPDATA: join(runtime, "config"), LOCALAPPDATA: join(runtime, "cache"),
    TEMP: runtime, TMP: runtime, TMPDIR: runtime, TS_PROVIDER: request?.provider ?? "demo" });
  // macOS urllib can inherit system proxies even with a small environment.
  // Loopback model fixtures and explicitly configured local endpoints stay local.
  env.NO_PROXY = [process.env.NO_PROXY ?? process.env.no_proxy ?? "", "127.0.0.1", "localhost", "::1"].filter(Boolean).join(",");
  if (/^[a-f0-9]{40,64}$/u.test(process.env.TIGER_SOURCE_COMMIT ?? "")) env.TIGER_SOURCE_COMMIT = process.env.TIGER_SOURCE_COMMIT;
  return env;
}

async function pythonCall(options, args, request, { signal, timeoutMs = 30_000, interruptible = false } = {}) {
  await assertSafePath(options.workspace, true);
  await assertSafePath(options.kernel);
  // Python venv executable may itself be a controlled symlink; kernel/data
  // paths still reject all symlinks. Resolve the launcher-selected executable.
  await realpath(options.python);
  // Preserve the selected venv path when launching: resolving its Python
  // symlink as argv[0] would lose pyvenv.cfg and its installed dependencies.
  const child = spawn(options.python, ["-s", "-m", "trading_system.agent_api", "--workspace", options.workspace,
    "--tenant", options.tenant, ...args], { cwd: options.kernel,
    env: childEnvironment(options, request?.idempotencyKey, request), windowsHide: true,
    detached: process.platform !== "win32", stdio: ["pipe", "pipe", "pipe"] });
  let output = "";
  let bytes = 0;
  let stopReason = null;
  let spawnFailed = false;
  let terminationFailed = false;
  let stopPromise;
  const terminateTree = async () => {
    if (!child.pid) return;
    if (process.platform === "win32") {
      // Windows signals only stop the direct child. Use the operating system's
      // /t tree primitive with the exact PID owned by this launcher, no shell.
      const systemRoot = process.env.SystemRoot ?? process.env.SYSTEMROOT ?? process.env.WINDIR;
      if (!systemRoot || !isAbsolute(systemRoot)) throw new Error("Missing Windows system directory");
      const killer = spawn(join(systemRoot, "System32", "taskkill.exe"),
        ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" });
      const forced = setTimeout(() => { killer.kill(); child.kill(); }, 2000);
      forced.unref();
      try {
        const code = await new Promise((resolveKill, rejectKill) => {
          killer.once("error", rejectKill);
          killer.once("close", resolveKill);
        });
        if (code !== 0) throw new Error("Windows process tree stop failed");
      } finally { clearTimeout(forced); }
      return;
    }
    const kill = (kind) => {
      try { process.kill(-child.pid, kind); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    };
    kill("SIGTERM");
    // Keep this promise alive even when the direct child closes first: a
    // descendant that ignores TERM still belongs to the detached group.
    await delay(500);
    kill("SIGKILL");
  };
  const stop = (reason) => {
    if (stopReason || child.exitCode !== null) return;
    stopReason = reason;
    stopPromise = terminateTree().catch(() => {
      terminationFailed = true;
      // Stop the direct kernel even if the platform tree primitive failed.
      // The failed receipt will disclose that tree termination was incomplete.
      try { child.kill("SIGKILL"); } catch { spawnFailed = true; }
    });
  };
  const abort = () => stop("cancelled");
  signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => stop("timed_out"), timeoutMs);
  timer.unref();
  const result = await new Promise((resolveCall) => {
    child.on("error", () => { spawnFailed = true; });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > MAX_OUTPUT_BYTES) stop("output_limit");
      else output += chunk;
    });
    // Upstream stderr can contain credentials; drain it without persistence.
    child.stderr.resume();
    child.stdin.on("error", (error) => {
      // Replay or early validation may exit before consuming stdin. EPIPE is
      // resolved by the child's JSON outcome; other input failures stop it.
      if (error.code !== "EPIPE") stop("input_failure");
    });
    child.on("close", (code) => resolveCall({ code }));
    child.stdin.end(request ? JSON.stringify(request) : undefined);
    if (signal?.aborted) abort();
  });
  clearTimeout(timer);
  await stopPromise;
  signal?.removeEventListener("abort", abort);
  if (stopReason) {
    if (interruptible && child.pid) {
      const interrupted = !terminationFailed && ["cancelled", "timed_out"].includes(stopReason) ? stopReason : "failed";
      const terminal = await pythonCall(options, ["--interrupted", interrupted, "--expected-pid", String(child.pid),
        "--stop-reason", terminationFailed ? "termination_failed" : stopReason], request);
      if (terminal.schemaVersion === "tiger.agent-receipt/v1" && TERMINAL.has(terminal.status)) return terminal;
    }
    return { status: stopReason === "cancelled" ? "cancelled" : stopReason === "timed_out" ? "timed_out" : "failed",
      jobId: request?.idempotencyKey, integrityVerified: false,
      error: { code: stopReason.toUpperCase(), message: "Kernel process stopped before verified completion" } };
  }
  if (spawnFailed) reject("RUNTIME_UNAVAILABLE", "Bundled Python could not be launched");
  let data;
  try { data = JSON.parse(output); }
  catch { reject("INVALID_KERNEL_OUTPUT", "Kernel did not return one valid JSON receipt"); }
  if (!object(data)) reject("INVALID_KERNEL_OUTPUT", "Kernel returned invalid output");
  if (result.code !== 0 && !data.error) reject("KERNEL_FAILED", "Kernel process exited without a failure receipt");
  return data;
}

function canonicalEvent(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalEvent).join(",")}]`;
  return `{${Object.keys(value).filter((key) => value[key] !== null).sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalEvent(value[key])}`).join(",")}}`;
}

export async function verifyReceipt(options, data) {
  if (!data.schemaVersion || data.status === "running") return data;
  if (data.schemaVersion !== "tiger.agent-receipt/v1" || !KEY.test(data.jobId ?? "") || !TERMINAL.has(data.status)) reject("INTEGRITY_ERROR", "Invalid receipt schema");
  const job = join(options.workspace, "jobs", options.tenant, data.jobId);
  await assertSafePath(job);
  const completion = JSON.parse(await readFile(await assertSafePath(join(job, "completion.json")), "utf8"));
  const bytes = await readFile(await assertSafePath(join(job, "receipt.json")));
  if (completion.receiptSha256 !== hash(bytes)) reject("INTEGRITY_ERROR", "Completion checksum mismatch");
  const stored = JSON.parse(bytes);
  if (stored.inputSha256 !== data.inputSha256 || stored.status !== data.status) reject("INTEGRITY_ERROR", "Returned receipt does not match disk");
  const request = JSON.parse(await readFile(await assertSafePath(join(job, "request.json")), "utf8"));
  if (request.inputSha256 !== stored.inputSha256) reject("INTEGRITY_ERROR", "Request checksum mismatch");
  let governanceVerified = false;
  let resultVerified = false;
  for (const artifact of stored.artifacts) {
    if (typeof artifact.name !== "string" || artifact.name.includes("\\") || isAbsolute(artifact.name)
        || artifact.name.split("/").some((part) => !part || part === "." || part === "..") || /^[A-Za-z]:/u.test(artifact.name)) reject("INTEGRITY_ERROR", "Unsafe artifact name");
    const file = join(job, "artifacts", artifact.name);
    const relation = relative(join(job, "artifacts"), file);
    if (!relation || relation.startsWith(`..${sep}`)) reject("INTEGRITY_ERROR", "Artifact escaped workspace");
    const content = await readFile(await assertSafePath(file));
    if (content.length !== artifact.bytes || hash(content) !== artifact.sha256) reject("INTEGRITY_ERROR", "Artifact checksum mismatch");
    if (artifact.name === stored.resultArtifact?.name) {
      if (artifact.role !== stored.resultArtifact.role || artifact.sha256 !== stored.resultSha256) reject("INTEGRITY_ERROR", "Canonical result checksum or role mismatch");
      const result = JSON.parse(content.toString("utf8"));
      const source = artifact.role === "pipeline-result" ? result.raw : result;
      const limits = artifact.role === "pipeline-result" ? source?.risk_limits : source?.riskLimits;
      const params = artifact.role === "pipeline-result" ? source?.gate_params : source?.gateParams;
      try {
        normalizeRiskLimits(limits);
        normalizeRiskLimits(stored.requestedRiskLimits);
      } catch { reject("INTEGRITY_ERROR", "Actual result lacks a valid risk snapshot"); }
      if (!object(params) || canonicalEvent(limits) !== canonicalEvent(stored.riskLimits)
          || canonicalEvent(params) !== canonicalEvent(stored.gateParams)
          || canonicalEvent(stored.requestedRiskLimits) !== canonicalEvent(request.request?.riskLimits)
          || Object.entries(limits).some(([name, number]) => number > stored.requestedRiskLimits[name] || params[name] !== number)) {
        reject("INTEGRITY_ERROR", "Receipt risk and parameter snapshots do not match the bound request and result");
      }
      resultVerified = true;
    }
    if (artifact.role === "governance-events") {
      let previous = "GENESIS";
      const lines = content.toString("utf8").trim().split("\n");
      for (const line of lines) {
        const record = JSON.parse(line);
        if (record.prev_hash !== previous || record.hash !== hash(previous + canonicalEvent(record.payload))) reject("INTEGRITY_ERROR", "Governance hash chain mismatch");
        previous = record.hash;
      }
      governanceVerified = lines.length > 0;
    }
  }
  if (["succeeded", "degraded"].includes(stored.status) && (!resultVerified || !governanceVerified || stored.integrityVerified !== true || stored.receipt?.synced !== true)) reject("INTEGRITY_ERROR", "Successful receipt lacks verified local evidence");
  return { ...stored, ...(data.replayed ? { replayed: true } : {}), artifactRoot: join(job, "artifacts"),
    launcherIntegrityVerified: true };
}

export async function runRequest(options, input, { signal } = {}) {
  const request = normalizeRequest(input);
  const data = await pythonCall(options, [], request, {
    signal, timeoutMs: request.timeoutSeconds * 1000, interruptible: true,
  });
  return verifyReceipt(options, data);
}

export async function getJob(options, input) {
  validateObject(input, ["jobId"], ["jobId"]);
  if (typeof input.jobId !== "string" || !KEY.test(input.jobId)) reject("INVALID_INPUT", "Invalid jobId");
  return verifyReceipt(options, await pythonCall(options, ["--get", input.jobId]));
}

export async function getArtifacts(options, input) {
  validateObject(input, ["jobId", "name"], ["jobId"]);
  if (input.name !== undefined && (typeof input.name !== "string" || !input.name || input.name.length > 240)) reject("INVALID_INPUT", "Invalid artifact name");
  const receipt = await getJob(options, { jobId: input.jobId });
  if (receipt.status === "running" || receipt.error) return receipt;
  if (input.name === undefined) return { jobId: input.jobId, status: receipt.status,
    artifactRoot: receipt.artifactRoot, artifacts: receipt.artifacts, integrityVerified: true };
  const artifact = receipt.artifacts.find((item) => item.name === input.name);
  if (!artifact) reject("NOT_FOUND", "Artifact is not in the verified manifest");
  if (artifact.bytes > 512 * 1024) reject("ARTIFACT_TOO_LARGE", "Artifact exceeds inline limit; use the verified local artifact path");
  const content = await readFile(await assertSafePath(join(receipt.artifactRoot, artifact.name)), "utf8");
  if (hash(content) !== artifact.sha256) reject("INTEGRITY_ERROR", "Artifact changed during read");
  return { jobId: input.jobId, status: receipt.status, artifact, text: content, integrityVerified: true };
}

export function catalog() {
  return { schemaVersion: "tiger.agent-catalog/v1", productId: "workroom-tiger", tools: TOOLS,
    boundaries: { environments: ["simulation", "paper"], employees: EMPLOYEES, historicalMarkets: ["us"],
      brokerOrders: false, approvals: false, parameterApplication: false, workspaceChosenByLauncher: true,
      missingModelStatus: "degraded", localReceiptIsServerSync: false } };
}
