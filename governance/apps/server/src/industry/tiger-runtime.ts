/** Tiger-owned composition root: verified research jobs use the public Quest runner. */
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
import { TRPCError } from "@trpc/server";
import type pg from "pg";
import { getAppPool } from "@workloom/db";
import { readVerifiedBundleJsonAsset } from "@workloom/base/bundles";
import { gatewayAppendOnClient } from "@workloom/base/workdata";
import { assemblePreset, type QuestStep } from "@workloom/runtime";
import { resolveWorkspaceActiveBundle } from "../service/active-bundle.js";
import {
  registerIndustryGoalClaim, registerIndustryQuestPlanner, resolveThreadPresetKey,
  runQuestForThread, type Scope, type ThreadQuestInput, type ThreadQuestOutcome,
} from "../runtime/thread-runner.js";
import { resetToolExecutorCache, type DeploymentToolExecutor } from "../runtime/tool-executor.js";

const SHA = /^[a-f0-9]{64}$/;
const FACADE_URL = new URL("../../../../../scripts/tiger-agent-runtime.mjs", import.meta.url);
const KERNEL_ROOT = resolve(dirname(fileURLToPath(FACADE_URL)), "..");
const CURRENCY = { us: "USD", cn: "CNY", hk: "HKD" } as const;
const PRESETS = {
  "universe-scanner": { employee: "scanner", tool: "tiger.scanner.run", label: "实际全市场扫描" },
  mrs: { employee: "mrs", tool: "tiger.mrs.run", label: "实际大盘许可计算" },
  "risk-manager": { employee: "risk", tool: "tiger.risk.run", label: "实际风险预算复核" },
  "review-chief": { employee: "review", tool: "tiger.review.run", label: "实际只读归因复盘" },
  "kernel-orchestrator": { employee: null, tool: "tiger.pipeline.run", label: "实际内核研究管线" },
} as const;
type PresetKey = keyof typeof PRESETS;

export const TOOL_PATTERNS = Object.values(PRESETS).map((entry) => entry.tool);
export const tigerResearchSchema = z.object({
  provider: z.enum(["demo", "yahoo", "stooq", "tencent", "sina", "eastmoney"]).optional(),
  market: z.enum(["us", "cn", "hk"]).optional(),
  universe: z.enum(["core", "extended"]).optional(),
  topN: z.number().int().min(1).max(100).optional(),
  maxPicks: z.number().int().min(1).max(25).optional(),
  account: z.number().finite().min(100).max(100_000_000).optional(),
  accountCurrency: z.enum(["USD", "CNY", "HKD"]).optional(),
  mode: z.enum(["daily", "premarket", "intraday", "backtest", "tune", "review"]).optional(),
  sourceJob: z.object({ jobId: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/), resultSha256: z.string().regex(SHA) }).strict().optional(),
  timeoutSeconds: z.number().int().min(1).max(900).optional(),
}).strict();
export type TigerResearchOptions = z.infer<typeof tigerResearchSchema>;

interface RiskLimits { risk_r_pct: number; max_single_position_pct: number; gross_cap: number }
interface LauncherOptions { workspace: string; tenant: string; kernel: string; python: string }
interface Artifact { name: string; role: string; sha256: string; bytes: number }
interface KernelReceipt {
  schemaVersion?: string; jobId?: string; status?: string; inputSha256?: string;
  resultSha256?: string; integrityVerified?: boolean; launcherIntegrityVerified?: boolean;
  dataMode?: string; artifacts?: Artifact[]; receipt?: { synced?: boolean; governanceServerSynced?: boolean };
  riskLimits?: RiskLimits; requestedRiskLimits?: RiskLimits; error?: { code?: string };
  [key: string]: unknown;
}
interface TigerFacade {
  normalizeRequest(input: Record<string, unknown>): Record<string, unknown>;
  launcherOptions(argv: string[]): { options: LauncherOptions };
  runRequest(options: LauncherOptions, input: Record<string, unknown>): Promise<KernelReceipt>;
  verifyReceipt(options: LauncherOptions, receipt: KernelReceipt): Promise<KernelReceipt>;
}
let facadePromise: Promise<TigerFacade> | undefined;
function facade(): Promise<TigerFacade> {
  facadePromise ??= import(FACADE_URL.href) as Promise<TigerFacade>;
  return facadePromise;
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== undefined)
    .sort(([a], [b]) => a.localeCompare(b, "en")).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
}
function hash(value: string | Buffer): string { return createHash("sha256").update(value).digest("hex"); }
function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}
function isPresetKey(value: string): value is PresetKey { return Object.hasOwn(PRESETS, value); }
export function tigerScopeKey(scope: Scope): string { return hash(canonical(scope)).slice(0, 40); }

function riskLimitsOf(archive: Record<string, unknown>): RiskLimits {
  const account = object(archive.account);
  const result = {
    risk_r_pct: account?.risk_per_trade_pct,
    max_single_position_pct: account?.max_position_per_ticker_pct,
    gross_cap: account?.gross_cap_pct,
  };
  if (Object.values(result).some((value) => typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 1)) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "工作区缺少有效的三项客户风险预算" });
  }
  return result as RiskLimits;
}

function deploymentLlmMode(): "disabled" | "configured" {
  const mode = process.env.TIGER_RESEARCH_LLM_MODE ?? "disabled";
  if (mode !== "disabled" && mode !== "configured") {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "部署 TIGER_RESEARCH_LLM_MODE 无效，研究桥失败关闭" });
  }
  return mode;
}

export async function deriveTigerRequest(input: {
  scope: Scope; threadId: string; goal: string; presetKey: string; archive: Record<string, unknown>;
  stage: string; research?: TigerResearchOptions;
}): Promise<Record<string, unknown>> {
  if (input.stage !== "paper") throw new TRPCError({ code: "PRECONDITION_FAILED", message: "Tiger 执行桥只支持 paper 阶段的独立研究，不能连接实盘" });
  if (!isPresetKey(input.presetKey)) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "此岗位尚未声明实际执行适配器，无法把角色定义当完成回执" });
  const research = tigerResearchSchema.parse(input.research ?? {});
  const selected = PRESETS[input.presetKey];
  const account = object(input.archive.account);
  const market = research.market ?? "us";
  if (research.accountCurrency && research.accountCurrency !== CURRENCY[market]) {
    throw new TRPCError({ code: "BAD_REQUEST", message: "研究账户币种与所选市场不一致，不能隐式换算" });
  }
  if (research.account !== undefined && !research.accountCurrency) {
    throw new TRPCError({ code: "BAD_REQUEST", message: "显式研究账户金额必须同时声明市场币种" });
  }
  if (market !== "us" && research.account === undefined && account?.base_currency !== CURRENCY[market]) {
    throw new TRPCError({ code: "BAD_REQUEST", message: "跨市场研究须给出该市场币种的独立研究账户金额" });
  }
  const { accountCurrency: _currency, ...fields } = research;
  const environment = input.archive.dataMode === "simulated" ? "simulation" : "paper";
  const requestBase = {
    ...fields, operation: selected.employee ? "employee" : "pipeline",
    ...(selected.employee ? { employee: selected.employee } : { mode: research.mode ?? "daily" }),
    market, environment, provider: research.provider ?? "demo", llmMode: deploymentLlmMode(),
    riskLimits: riskLimitsOf(input.archive),
  };
  // Job identity includes tenant/workspace/thread/goal and every request value; no caller path or key is accepted.
  const idempotencyKey = `quest-${hash(canonical({ scope: input.scope, threadId: input.threadId,
    goal: input.goal, presetKey: input.presetKey, request: requestBase }))}`;
  try { return (await facade()).normalizeRequest({ ...requestBase, idempotencyKey }); }
  catch (error) {
    const code = object(error)?.code;
    const detail = code === "INVALID_INPUT" ? "模式参数无效；复盘和盘中需要绑定 sourceJob，员工模式不能带管线参数" : "研究请求无法通过内核输入契约";
    throw new TRPCError({ code: "BAD_REQUEST", message: detail });
  }
}

async function scopedTx<T>(scope: Scope, fn: (client: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await getAppPool().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.workspace_id',$1,true)", [scope.workspaceId]);
    await client.query("SELECT set_config('app.tenant_id',$1,true)", [scope.tenantId]);
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    try { await client.query("ROLLBACK"); }
    catch (rollbackError) { throw new AggregateError([error, rollbackError], "研究事务失败且回滚未成功"); }
    throw error;
  } finally { client.release(); }
}

interface Binding {
  schemaVersion: "tiger.quest-binding/v1"; scope: Scope; threadId: string; goal: string;
  indexTitle: string;
  presetKey: PresetKey; agentId: string; archiveSha256: string; bundleDigest: string;
  request: Record<string, unknown>; research: TigerResearchOptions; bindingHash: string;
}
function bindingHash(binding: Omit<Binding, "bindingHash">): string { return hash(canonical(binding)); }
function requireBinding(params: Record<string, unknown>): Binding {
  const binding = object(params.binding);
  if (Object.keys(params).length !== 1 || !binding || binding.schemaVersion !== "tiger.quest-binding/v1"
    || typeof binding.indexTitle !== "string" || typeof binding.goal !== "string"
    || typeof binding.bindingHash !== "string" || !SHA.test(binding.bindingHash)) throw new Error("研究计划缺少可信线程绑定");
  return binding as unknown as Binding;
}

async function verifiedBundle(scope: Scope): Promise<string> {
  const active = await resolveWorkspaceActiveBundle(scope.workspaceId);
  if (active.state !== "ready" || active.bundleId !== "trading" || !active.projection?.ui.permissions.includes("trading.research.execute")) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前已验活动装配没有 Tiger 研究执行权限" });
  }
  const risk = object(readVerifiedBundleJsonAsset("trading", "schemas/risk-defaults.json"));
  const source = object(risk?.source);
  const configHash = hash(await readFile(join(process.env.TIGER_KERNEL_ROOT ?? KERNEL_ROOT, "trading_system/config.py")));
  if (risk?.schemaVersion !== "trading.risk-defaults/v1" || source?.sha256 !== configHash) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "行业包风险资产与当前内核 config 摘要不一致，须重新生成并封装" });
  }
  return active.projection.integrityDigest!;
}

/** Called inside the dispatch transaction so queued work already contains its immutable request. */
export async function prepareTigerThreadOn(client: pg.PoolClient, scope: Scope, input: {
  threadId: string; goal: string; presetRef?: string | null; research?: TigerResearchOptions;
}): Promise<{ presetKey: PresetKey; agentId: string; plan: QuestStep[] }> {
  const ws = (await client.query<{ tenant_id: string; stage: string }>(
    "SELECT tenant_id,stage FROM workspaces WHERE id=$1 AND tenant_id=$2", [scope.workspaceId, scope.tenantId])).rows[0];
  if (!ws) throw new TRPCError({ code: "NOT_FOUND", message: "工作区不存在或无权访问" });
  const thread = (await client.query<{ title: string; plan: QuestStep[] | null }>(
    "SELECT title,plan FROM threads WHERE id=$1 AND workspace_id=$2 FOR UPDATE", [input.threadId, scope.workspaceId])).rows[0];
  if (!thread) throw new TRPCError({ code: "NOT_FOUND", message: "研究线程不存在或无权访问" });
  if (thread.plan?.length) throw new TRPCError({ code: "CONFLICT", message: "研究线程已有绑定计划，不能静默重绑" });
  if (input.presetRef) {
    const present = await client.query("SELECT 1 FROM agents WHERE workspace_id=$1 AND (id=$2 OR preset_key=$2)",
      [scope.workspaceId, input.presetRef]);
    if (!present.rowCount) throw new TRPCError({ code: "BAD_REQUEST", message: "指定岗位不在当前工作区名册中" });
  }
  const presetKey = await resolveThreadPresetKey(scope, input.presetRef ?? "kernel-orchestrator", input.goal);
  if (!isPresetKey(presetKey)) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "此岗位尚未接通实际执行，不能派发假完成任务" });
  const preset = await assemblePreset(getAppPool(), scope, { workspaceId: scope.workspaceId, presetKey, goal: input.goal });
  if (!preset.tools.some((tool) => tool.name === PRESETS[presetKey].tool)) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前岗位未装配 Tiger 研究工具，请先补齐行业资产" });
  }
  const bundleDigest = await verifiedBundle(scope);
  const research = tigerResearchSchema.parse(input.research ?? {});
  const request = await deriveTigerRequest({ scope, threadId: input.threadId, goal: input.goal, presetKey,
    archive: preset.essentials.archive, stage: ws.stage, research });
  const unsigned: Omit<Binding, "bindingHash"> = { schemaVersion: "tiger.quest-binding/v1", scope,
    threadId: input.threadId, goal: input.goal, indexTitle: thread.title, presetKey, agentId: preset.agentId,
    archiveSha256: hash(canonical(preset.essentials.archive)), bundleDigest, request, research };
  const binding: Binding = { ...unsigned, bindingHash: bindingHash(unsigned) };
  const step: QuestStep = { stepId: "s1", action: presetKey === "kernel-orchestrator" ? "pipeline.daily" : "tiger.research.calculate",
    objectType: "report", objectId: input.threadId, tool: PRESETS[presetKey].tool,
    params: { binding }, context: { stage: ws.stage, market: request.market, environment: request.environment },
    label: PRESETS[presetKey].label };
  const written = await client.query("UPDATE threads SET agent_id=$3,plan=$4::jsonb,plan_version=1,updated_at=now() WHERE id=$1 AND workspace_id=$2 AND (plan IS NULL OR plan='[]'::jsonb)",
    [input.threadId, scope.workspaceId, preset.agentId, JSON.stringify([step])]);
  if (written.rowCount !== 1) throw new TRPCError({ code: "CONFLICT", message: "研究计划已被其他派遣者绑定" });
  return { presetKey, agentId: preset.agentId, plan: [step] };
}

function runtimeOptions(scope: Scope, bridge: TigerFacade): LauncherOptions {
  const root = process.env.TIGER_RESEARCH_ROOT;
  if (!root || !isAbsolute(root)) throw new Error("部署未配置绝对 TIGER_RESEARCH_ROOT，研究桥失败关闭");
  return bridge.launcherOptions(["--workspace", join(root, tigerScopeKey(scope)), "--tenant", `ws-${tigerScopeKey(scope)}`]).options;
}

async function loadCurrentBinding(scope: Scope, name: string, params: Record<string, unknown>): Promise<Binding> {
  const supplied = requireBinding(params);
  if (canonical(supplied.scope) !== canonical(scope) || !isPresetKey(supplied.presetKey) || PRESETS[supplied.presetKey].tool !== name) {
    throw new Error("研究工具、租户或工作区绑定不一致");
  }
  const expected = await scopedTx(scope, async (client) => {
    const thread = (await client.query<{ title: string; agent_id: string; plan: QuestStep[]; status: string }>(
      "SELECT title,agent_id,plan,status FROM threads WHERE id=$1 AND workspace_id=$2", [supplied.threadId, scope.workspaceId])).rows[0];
    if (!thread || thread.status !== "running" || thread.plan?.length !== 1 || thread.plan[0]?.tool !== name) throw new Error("研究线程不存在、未认领或计划已变化");
    const stored = requireBinding(thread.plan[0].params);
    if (canonical(stored) !== canonical(supplied) || thread.title !== supplied.indexTitle || thread.agent_id !== supplied.agentId) throw new Error("研究计划参数或目标已被替换");
    return stored;
  });
  const { bindingHash: actualHash, ...unsigned } = expected;
  if (bindingHash(unsigned) !== actualHash) throw new Error("研究计划摘要不匹配");
  const preset = await assemblePreset(getAppPool(), scope, { workspaceId: scope.workspaceId, presetKey: expected.presetKey, goal: expected.goal });
  if (preset.agentId !== expected.agentId || hash(canonical(preset.essentials.archive)) !== expected.archiveSha256
    || !preset.tools.some((tool) => tool.name === name) || await verifiedBundle(scope) !== expected.bundleDigest) {
    throw new Error("研究档案、岗位或已验行业包在派遣后发生变化，请重新派遣");
  }
  const derived = await deriveTigerRequest({ scope, threadId: expected.threadId, goal: expected.goal, presetKey: expected.presetKey,
    archive: preset.essentials.archive, stage: preset.essentials.stage, research: expected.research });
  if (canonical(derived) !== canonical(expected.request)) throw new Error("研究请求与当前档案不一致");
  return expected;
}

async function ingestKernelObservations(scope: Scope, binding: Binding, options: LauncherOptions, receipt: KernelReceipt) {
  const artifact = receipt.artifacts?.find((item) => item.role === "governance-events");
  if (!artifact) throw new Error("内核缺少实际五元事件产物");
  const bytes = await readFile(join(options.workspace, "jobs", options.tenant, receipt.jobId!, "artifacts", artifact.name));
  if (hash(bytes) !== artifact.sha256 || bytes.length !== artifact.bytes) throw new Error("内核事件产物在核验后发生变化");
  const records = bytes.toString("utf8").trim().split("\n").map((line) => {
    const record = object(JSON.parse(line));
    if (!record || !object(record.payload) || typeof record.hash !== "string" || !SHA.test(record.hash)) throw new Error("内核五元事件结构不合法");
    return record;
  });
  return scopedTx(scope, async (client) => {
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`tiger-observe:${binding.bindingHash}`]);
    const eventIds: string[] = [];
    for (const record of records) {
      const prior = await client.query<{ event_id: string }>(
        "SELECT event_id FROM biz_events WHERE workspace_id=$1 AND session_id=$2 AND payload->'decision'->>'kernel_hash'=$3 AND payload->'decision'->>'binding_hash'=$4",
        [scope.workspaceId, binding.threadId, record.hash, binding.bindingHash]);
      if (prior.rows[0]) { eventIds.push(prior.rows[0].event_id); continue; }
      const event = await gatewayAppendOnClient(client, { ...scope,
        actor: { id: binding.presetKey, type: "agent" }, sessionId: binding.threadId }, {
        who: { type: "agent", id: binding.presetKey, version: "tiger-research/v1" },
        context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
        object: { type: "report", id: binding.threadId },
        decision: { action: "tiger.research.observed", effect: "read", kind: "observation",
          after: { kernel_record: record, job_id: receipt.jobId, kernel_status: receipt.status,
            data_mode: receipt.dataMode, result_sha256: receipt.resultSha256 },
          kernel_hash: record.hash, binding_hash: binding.bindingHash,
          basis: ["核验实际 Python 产物及本地五元哈希链后归一观察事件；原始对象与参数保留在 kernel_record，未执行真实资金动作"] },
        rule_impact: [], receipt: { synced: true, mode: receipt.dataMode === "synthetic" ? "simulated" : "real",
          snapshot_uri: `tiger-research://${receipt.jobId}/${artifact.sha256}`, verified_at: new Date().toISOString() },
      });
      eventIds.push(event.eventId);
    }
    return { synced: true, eventIds, events: records.length, kernelArtifactSha256: artifact.sha256,
      scope: "workloom-governance" as const };
  });
}

function riskSnapshotValid(binding: Binding, receipt: KernelReceipt): boolean {
  if (binding.presetKey !== "kernel-orchestrator" && binding.presetKey !== "risk-manager") return true;
  const requested = object(binding.request.riskLimits);
  const actual = object(receipt.riskLimits);
  return Boolean(requested && actual && canonical(receipt.requestedRiskLimits) === canonical(requested)
    && Object.entries(requested).every(([key, maximum]) => {
      const value = actual[key];
      return typeof value === "number" && Number.isFinite(value) && value > 0 && typeof maximum === "number" && value <= maximum;
    }));
}

export function createToolExecutorForScope(scope?: Partial<Scope>): DeploymentToolExecutor | undefined {
  if (!scope?.tenantId || !scope.workspaceId) return undefined;
  const boundScope: Scope = { tenantId: scope.tenantId, workspaceId: scope.workspaceId };
  return async (name, params) => {
    if (!TOOL_PATTERNS.includes(name as typeof TOOL_PATTERNS[number])) throw new Error("Tiger 桥未声明此工具");
    const binding = await loadCurrentBinding(boundScope, name, params);
    const bridge = await facade();
    const options = runtimeOptions(boundScope, bridge);
    let receipt: KernelReceipt;
    try { receipt = await bridge.verifyReceipt(options, await bridge.runRequest(options, binding.request)); }
    catch { throw new Error("内核启动或产物完整性核验失败；未获得可交付回执"); }
    const evidenceValid = receipt.schemaVersion === "tiger.agent-receipt/v1" && receipt.integrityVerified === true
      && receipt.launcherIntegrityVerified === true && receipt.receipt?.synced === true && riskSnapshotValid(binding, receipt);
    const governance = evidenceValid ? await ingestKernelObservations(boundScope, binding, options, receipt) : { synced: false, events: 0 };
    const succeeded = evidenceValid && governance.synced === true && receipt.status === "succeeded";
    return { result: { state: succeeded ? "verified-research" : "unverified-research", kernelStatus: receipt.status,
      jobId: receipt.jobId, inputSha256: receipt.inputSha256, resultSha256: receipt.resultSha256,
      artifacts: receipt.artifacts, dataMode: receipt.dataMode, environment: binding.request.environment,
      market: binding.request.market, account: binding.request.account,
      currency: CURRENCY[binding.request.market as keyof typeof CURRENCY],
      riskLimits: receipt.riskLimits, requestedRiskLimits: binding.request.riskLimits,
      kernelReceipt: { synced: receipt.receipt?.synced === true, scope: "local-kernel", governanceServerSynced: false },
      governance, bindingHash: binding.bindingHash, degradedSteps: receipt.degradedSteps,
      errorCode: receipt.error?.code ?? (evidenceValid ? undefined : "EVIDENCE_MISMATCH") },
      receipt: { synced: succeeded, mode: receipt.dataMode === "synthetic" ? "simulated" : "real",
        ...(evidenceValid ? { snapshot_uri: `tiger-research://${receipt.jobId}/${receipt.resultSha256}`, verified_at: new Date().toISOString() } : {}) } };
  };
}

/** Fixed trusted module goes first; existing non-Tiger bridges retain their configured order. */
export function configureTigerRuntime(): void {
  const own = `${fileURLToPath(import.meta.url)}::::${TOOL_PATTERNS.join("|")}`;
  const existing = [process.env.WORKLOOM_TOOL_EXECUTOR_MODULE, process.env.WORKLOOM_TOOL_EXECUTOR_MODULES]
    .filter(Boolean).join(",").split(/[,\n]/).map((value) => value.trim()).filter((value) => value && value !== own);
  process.env.WORKLOOM_TOOL_EXECUTOR_MODULES = [own, ...existing].join(",");
  delete process.env.WORKLOOM_TOOL_EXECUTOR_MODULE;
  resetToolExecutorCache();
  registerIndustryQuestPlanner(() => () => { throw new Error("Tiger 研究计划须由已授权组合根绑定后运行"); });
  registerIndustryGoalClaim((_goal, preset) => isPresetKey(preset.presetKey));
}

export async function runTigerQuestForThread(scope: Scope, input: ThreadQuestInput): Promise<ThreadQuestOutcome> {
  configureTigerRuntime();
  const bound = await scopedTx(scope, async (client) => {
    const row = (await client.query<{ title: string; plan: QuestStep[] | null; agent_id: string | null }>(
      "SELECT title,plan,agent_id FROM threads WHERE id=$1 AND workspace_id=$2 FOR UPDATE", [input.threadId, scope.workspaceId])).rows[0];
    if (!row) throw new TRPCError({ code: "NOT_FOUND", message: "任务不存在或无权访问" });
    if (input.replan) throw new TRPCError({ code: "BAD_REQUEST", message: "研究任务不能静默重规划；改变目标请创建新任务" });
    if (!row.plan?.length) {
      if (input.goal !== row.title) throw new TRPCError({ code: "BAD_REQUEST", message: "未绑定线程的目标必须与建档标题一致" });
      const prepared = await prepareTigerThreadOn(client, scope, { threadId: input.threadId, goal: row.title, presetRef: input.presetRef ?? row.agent_id });
      return requireBinding(prepared.plan[0]!.params);
    }
    if (row.plan.length !== 1) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "研究任务计划结构无效" });
    const binding = requireBinding(row.plan[0]!.params);
    const { bindingHash: actualHash, ...unsigned } = binding;
    if (bindingHash(unsigned) !== actualHash || canonical(binding.scope) !== canonical(scope)
      || binding.threadId !== input.threadId || binding.indexTitle !== row.title || binding.agentId !== row.agent_id) {
      throw new TRPCError({ code: "PRECONDITION_FAILED", message: "研究任务目标或计划绑定已变化" });
    }
    if (input.goal !== binding.goal && input.goal !== binding.indexTitle) throw new TRPCError({ code: "BAD_REQUEST", message: "续跑须使用原任务目标；改变目标请创建新研究任务" });
    if (input.presetRef && input.presetRef !== binding.agentId && input.presetRef !== binding.presetKey) {
      throw new TRPCError({ code: "BAD_REQUEST", message: "续跑不能改变已绑定岗位" });
    }
    return binding;
  });
  return runQuestForThread(scope, { ...input, goal: bound.goal, presetRef: bound.presetKey });
}
