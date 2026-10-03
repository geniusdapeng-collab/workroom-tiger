/**
 * tRPC 根路由（B11 状态：system / auth / members / threads / approvals / inspection / skills
 * / workspace / nightShift / fence / roster / im 挂载；其余 router（events / bundle）后续按需要挂载）
 * 已挂载：B10 inspection（巡检 M9）/ skills（技能+意识 M8）；F3 workspace（档案/成员）/ nightShift（夜班投影）；
 * F8 fence（围栏版本化+dry-run）；F9 roster（P8 船员名册：人机混编投影 + 工时聚合 L6.3 + 档案全字段）；
 * B11 im（IM 通道域 D14：注册表/入站幂等/审批卡片出站/手势回调，Mock 驱动默认）
 * F11 bundles（P7 舰船换装坞：六槽注册表投影/起飞前检查单/profile 激活切换/五要素草稿向导）
 * 本文件同时是前端类型源：apps/web 经 `@workloom/server/router` 导入 AppRouter 类型。
 */
import { z } from "zod";
import { TRPCError } from "@trpc/server";
import { existsSync, readFileSync } from "node:fs";
import type pg from "pg";
import { createHash, timingSafeEqual } from "node:crypto";
import { dirname, join } from "node:path";
import { getAppPool, getGatewayPool, getOwnerPool } from "@workloom/db";
import {
  getCapabilities,
  getMember,
  listMembers,
  signDemoToken,
  type Identity,
} from "@workloom/base/tenancy";
import { gatewayAppend, gatewayAppendOnClient, insertWithReadableId, THREAD_ID_SOURCE, MockEmbedder, upsertMemoryInTx } from "@workloom/base/workdata";
import { makeReadableId } from "@workloom/shared";
import { actionProcedure, capabilityActionProcedure, capabilityWriteProcedure, protectedProcedure, publicProcedure, router, scopeOf, sessionProcedure, writeProcedure } from "./context.js";
import { overlayRouter } from "./overlay-router.js";
import { evaluateRealModeReadiness, type ActivationGateResult, type ExamBinding } from "../service/onboarding-truth.js";
import { bundledServiceFrontAvailable, resolveServiceFrontPublication } from "../service/service-front-publication.js";
import { accountsRouter } from "./accounts-router.js";
import {
  ApprovalError,
  batchApprove,
  decide,
  expireSweep,
  listQueue,
} from "@workloom/base/review-console";
import { routeIntent, runAsk } from "@workloom/runtime";
import { loadGoalArchive } from "../runtime/thread-runner.js";
import { configureTigerRuntime, prepareTigerThreadOn, runTigerQuestForThread, tigerResearchSchema } from "../industry/tiger-runtime.js";
configureTigerRuntime();
import { LlmIntentClassifier, type IntentClassifier } from "@workloom/runtime";
import { providerFromEnv, OpenAiCompatibleProvider } from "@workloom/base/model-router";
import { routedLlmCall, resetLlmAssembly } from "../service/llm.js";
import { creditsRouter, modelFeedbackRouter } from "./credits-router.js";
import { runRouterReviewBeat } from "@workloom/base/model-router";
import {
  loadCharter, parseCharter, transition, defaultCharter,
  runBriefingBeat, runQueueBeat, runDeviationBeat, runBreakerBeat, buildScorecard,
  runOutcomeReviewBeat, runHrReviewBeat, runBoardPackBeat, runOrgScanBeat, applyReplacement,
  buildFloor,
  type CeoTransition,
} from "@workloom/base/captain";
import {
  buildCandidateList,
  confirmNight,
  deliverPackage,
  ensureReady,
  NightTransitionError,
  pauseAll,
  resumeNight,
} from "@workloom/base/night-shift";
import {
  activateRuleVersion,
  confirmDryRunOnTx,
  loadActiveRulesInTx,
  nextRuleRowIdentity,
  checkCandidateAgainstBaseline,
  type RuleRowIdentity,
  createDryRun,
  fenceActivationFromProposal,
} from "@workloom/base/fence-engine";
import { MAX_CONCURRENT_THREADS, PLAN_TIERS } from "@workloom/shared";
import {
  dispatchFromAnomaly,
  DispatchError,
  inspectionStatusBar,
  resolveAnomaly,
  runInspectionScan,
} from "@workloom/base/inspection";
import {
  confirmSuggestion,
  createSkillDraft,
  detectSuggestions,
  dryRunSkill,
  installSkill,
  listInstalls,
  listSkills,
  rejectSuggestion,
  SkillError,
  uninstallSkill,
} from "@workloom/base/skills";
import {
  distStatus,
  loadStaging,
  rollbackSkill,
  setSilentMode,
  SkillOpsError,
  syncDistribution,
  type InstanceProfile,
} from "@workloom/base/skill-ops";
import {
  getRefluxOptIn,
  previewReflux,
  RefluxError,
  sendReflux,
  setRefluxOptIn,
} from "@workloom/base/skill-ops";
import {
  buildManifest,
  consoleHealth,
  ConsoleError,
  listInbox,
  officializeDraft,
  reviewRefluxDraft,
} from "@workloom/base/skill-ops";
import {
  boundOpenidOfMember,
  ChannelError,
  composeApprovalCard,
  handleGestureCallback,
  ingestInbound,
  listChannels,
  MockChannelDriver,
  sendApprovalCard,
  stableStringify,
  verifyChannelSignature,
  type ChannelDriver,
  type ApprovalChannel,
} from "@workloom/base/im-channels";
import {
  BundleError,
  activateBundle,
  computeAssembly,
  createBundleDraft,
  listProfileSlugs,
  recheckBundle,
} from "@workloom/base/bundles";
import { serviceRouter } from "../service/router.js";
import { applyKbPublishAfterApproval } from "../service/kb.js";
import { resolveWorkspaceInspectionAdapter } from "../service/inspection-adapter.js";
import {
  AccessAuthorityError,
  resolveAuthoritativeClientAccess,
} from "../service/access-authority.js";
import {
  advanceWizardDraftOn, assignWizardDraft, completeWizardDraft, getWizardDraft, saveWizardDraft, type WizardDraftView,
  replayWelcome,
  saveWelcomeProgress,
  welcomeProgress,
  WELCOME_STEPS,
} from "../service/onboarding-continuity.js";
import {
  buildEvolutionScorecard,
  decayMemories,
  disableMemory,
  editMemoryContent,
  getFeedbackEnums, previewMemoryImpact, reactivateMemory, restoreMemories,
  recallMemoriesByMember,
  runMemoryMinerBeat,
} from "@workloom/base/evolve";
import { getMemorySources, searchMemories } from "@workloom/base/workdata";

/** system router：健康检查（公开） */
const systemRouter = router({
  health: publicProcedure.query(async () => {
    let db: "up" | "down" = "down";
    try {
      await getAppPool().query("SELECT 1");
      db = "up";
    } catch {
      db = "down";
    }
    return {
      ok: true,
      service: "workloom-im-server",
      phase: "阶段二 后端 API（B5）",
      db,
      time: new Date().toISOString(),
    };
  }),
});

/* ================= 落地向导（D24：模拟运行态 → 真实经营 切换面） =================
 * 契约：首次安装开箱即为「全模拟运行态」（种子数据 + mock 模型），P0/工作台横幅常显提示；
 * 向导四步（自检 → 真实大模型 → 经营主体 → 启用真实模式）尽量自动化：
 *  - saveLlmConfig 真实试调通过后仅更新当前进程装配，重启须重新注入或配置
 *  - activateRealMode 依照当前装配、主体和资产事实门禁启用真实模式
 * 全程五元事件留痕；密钥只记录配置状态，URL 不接收凭据、查询参数或片段。
 */

/** 向导凭据仅当前进程持有；重启由部署环境注入，禁止秘密写入工作区文件。 */
const LLM_RESTART_NOTICE = "模型凭据仅在当前服务进程有效；重启后须通过部署环境注入或重新配置。";
function safeModelEndpoint(value: string): string | null {
  try {
    const endpoint = new URL(value);
    if (!["http:", "https:"].includes(endpoint.protocol) || !endpoint.hostname
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) return null;
    return endpoint.toString();
  } catch { return null; }
}
const modelEndpointSchema = z.string().trim().max(200).refine(
  value => !value || safeModelEndpoint(value) !== null,
  { message: "模型端点仅支持无凭据、查询参数和片段的 HTTP(S) 地址" },
);

/** 历史草稿可能由旧接口保存了凭据 URI；所有续办响应均按当前端点边界投影。 */
function safeWizardDraftView(draft: WizardDraftView): WizardDraftView {
  const baseUrl = draft.payload.baseUrl;
  return { ...draft, payload: { ...draft.payload, ...(baseUrl ? { baseUrl: safeModelEndpoint(baseUrl) ?? "" } : {}) } };
}

function persistLlmEnv(cfg: { provider: string; baseUrl: string; apiKey: string; model: string }): void {
  process.env.LLM_PROVIDER = cfg.provider;
  process.env.LLM_BASE_URL = cfg.baseUrl;
  process.env.LLM_API_KEY = cfg.provider === "mock" ? "" : cfg.apiKey;
  process.env.LLM_MODEL = cfg.model;
  cachedLlmCall = undefined;
  cachedClassifier = undefined;
  cachedIndustry = undefined;
  resetLlmAssembly();
}

/** 指纹只用于当前凭据与已实测版本匹配；不回传、不进入事件明文。 */
function llmCredentialFingerprint(apiKey: string): string {
  return createHash("sha256").update(`workloom-llm-credential/v1:${apiKey}`, "utf8").digest("hex");
}

/** 旧进程环境的非法 URL 只显示 invalid；不能把其中的凭据反射给客户端。 */
function llmAssembly(): { provider: string; model: string; baseUrl: string; real: boolean; endpointState: "configured" | "unconfigured" | "invalid" } {
  const provider = process.env.LLM_PROVIDER ?? "mock";
  const rawBaseUrl = process.env.LLM_BASE_URL ?? "";
  const baseUrl = rawBaseUrl ? safeModelEndpoint(rawBaseUrl) : null;
  return {
    provider,
    model: process.env.LLM_MODEL ?? "",
    baseUrl: baseUrl ?? "",
    endpointState: !rawBaseUrl ? "unconfigured" : baseUrl ? "configured" : "invalid",
    real: provider !== "mock" && baseUrl !== null,
  };
}

/** 真实试调探针（落地向导「测试连接」：真实 round-trip 通过才允许保存） */
async function probeLlm(cfg: { baseUrl: string; apiKey?: string; model: string }): Promise<{ reply: string; latencyMs: number }> {
  const provider = new OpenAiCompatibleProvider(cfg.model, { baseUrl: cfg.baseUrl, apiKey: cfg.apiKey || undefined });
  const t0 = Date.now();
  const res = await Promise.race([
    provider.chat([{ role: "user", content: "你是企业经营系统的数字员工。请用一句中文回答：你已在线，可以开始工作。" }]),
    new Promise<never>((_, rej) => setTimeout(() => rej(new Error("模型响应超时（25s）")), 25_000)),
  ]);
  if (!res.text.trim()) throw new Error("模型返回为空");
  return { reply: res.text.trim().slice(0, 200), latencyMs: Date.now() - t0 };
}

interface OnboardingGateSnapshot {
  persistedDataMode: "simulated" | "real";
  llmVerified: boolean;
  formalActivationRecorded: boolean;
  business: { name: string; industry: string; note: string; configuredAt: string; source: string } | null;
  workspace: { name: string; bundleId: string | null; isExample: boolean };
  runtimeGate: ActivationGateResult;
  gate: ActivationGateResult;
}

/**
 * 在调用方事务内加载正式运行门禁事实。装配资产按台账 ID 精确核验，
 * 不能用工作区里任意 ready 员工/active 围栏凑数。
 */
async function loadOnboardingGate(
  client: pg.PoolClient,
  workspaceId: string,
  activationOwner: boolean,
): Promise<OnboardingGateSnapshot> {
  const profileRows = await client.query<{ archive: Record<string, unknown> }>(
    `SELECT archive FROM profiles WHERE workspace_id=$1`, [workspaceId],
  );
  const workspaceRows = await client.query<{ name: string; bundle_id: string | null; is_example: boolean }>(
    `SELECT name, bundle_id, is_example FROM workspaces WHERE id=$1`, [workspaceId],
  );
  const profile = profileRows.rows[0];
  const workspace = workspaceRows.rows[0];
  const archive = profile?.archive ?? {};
  const rawBusiness = archive.business;
  const business = rawBusiness && typeof rawBusiness === "object"
    ? rawBusiness as { name?: string; industry?: string; note?: string; configured_at?: string; source?: string }
    : null;
  const currentLlm = llmAssembly();
  const rawLlmVerification = archive.llmVerification;
  const llmVerification = rawLlmVerification && typeof rawLlmVerification === "object"
    ? rawLlmVerification as { provider?: string; baseUrl?: string; model?: string; credentialFingerprint?: string; verified?: boolean }
    : null;
  const llmVerified = Boolean(
    currentLlm.real
    && llmVerification?.verified
    && llmVerification.provider === currentLlm.provider
    && safeModelEndpoint(llmVerification.baseUrl ?? "") === currentLlm.baseUrl
    && llmVerification.model === currentLlm.model
    && llmVerification.credentialFingerprint === llmCredentialFingerprint(process.env.LLM_API_KEY ?? ""),
  );
  const rawFormalActivation = archive.realModeActivation;
  const formalActivationRecorded = Boolean(
    rawFormalActivation
    && typeof rawFormalActivation === "object"
    && (rawFormalActivation as { gateVersion?: number }).gateVersion === 1,
  );

  const installRows = await client.query<{
    id: string; bundle_id: string; assets: {
      preset_ids?: string[];
      fence_rule_ids?: string[];
      skill_ids?: string[];
      candidate?: { agents?: Array<{ id: string; skills?: string[]; meta?: Record<string, unknown> }> };
    };
    status: string; draft_id: string | null; assembly_version: number | null;
    assembly_hash: string | null; qualified_exam_id: string | null;
  }>(
    `SELECT id, bundle_id, assets, status, draft_id, assembly_version, assembly_hash, qualified_exam_id
     FROM bundle_installs WHERE workspace_id=$1 AND status='active'
     ORDER BY COALESCE(activated_at, installed_at) DESC`,
    [workspaceId],
  );
  const install = installRows.rows[0];
  const exactlyOneActiveInstall = installRows.rows.length === 1;
  const presetIds = [...new Set(install?.assets?.preset_ids ?? [])];
  const fenceIds = [...new Set(install?.assets?.fence_rule_ids ?? [])];
  const skillIds = [...new Set(install?.assets?.skill_ids ?? [])];
  const readyAgentRows = presetIds.length === 0 ? [] : (await client.query<{
    id: string; skills: string[]; meta: Record<string, unknown>;
  }>(
    `SELECT id, skills, meta FROM agents
     WHERE workspace_id=$1 AND id=ANY($2::text[]) AND status='ready'`,
    [workspaceId, presetIds],
  )).rows;
  const readyAgents = readyAgentRows.length;
  const activeFenceRows = fenceIds.length === 0 ? [] : (await client.query<{ id: string; created_by: string }>(
    `SELECT id, created_by FROM fence_rules
     WHERE workspace_id=$1 AND id=ANY($2::text[]) AND status='active'`,
    [workspaceId, fenceIds],
  )).rows;
  const activeFences = activeFenceRows.length;
  let fenceResponsibilityReady = activeFenceRows.length === fenceIds.length
    && activeFenceRows.every((fence) => Boolean(fence.created_by?.trim()));
  const installedSkills = skillIds.length === 0 ? 0 : Number((await client.query<{ n: string }>(
    `SELECT count(*)::text AS n FROM skill_installs
     WHERE workspace_id=$1 AND skill_id=ANY($2::text[])`,
    [workspaceId, skillIds],
  )).rows[0]?.n ?? 0);
  const candidateAgents = install?.assets?.candidate?.agents ?? [];
  const actualAgentById = new Map(readyAgentRows.map((agent) => [agent.id, agent]));
  let pendingCapabilities = 0;
  const capabilityClaimsSafe = install?.bundle_id !== "custom" || (
    candidateAgents.length === presetIds.length
    && candidateAgents.every((expected) => {
      const actual = actualAgentById.get(expected.id);
      const actualSkills = Array.isArray(actual?.skills) ? actual.skills : [];
      const actualTools = Array.isArray(actual?.meta?.tools) ? actual.meta.tools : [];
      const declarations = Array.isArray(expected.meta?.capability_declarations)
        ? expected.meta.capability_declarations as Array<{ status?: unknown }>
        : [];
      pendingCapabilities += declarations.filter((item) => item?.status === "pending_approval").length;
      return Boolean(actual)
        && JSON.stringify(actualSkills) === JSON.stringify(expected.skills ?? [])
        && JSON.stringify(actualTools) === JSON.stringify(Array.isArray(expected.meta?.tools) ? expected.meta.tools : [])
        && declarations.every((item) => item?.status === "pending_approval");
    })
  );

  let customAssembly: Parameters<typeof evaluateRealModeReadiness>[0]["customAssembly"] = {
    required: install?.bundle_id === "custom",
  };
  if (install?.bundle_id === "custom") {
    const draftRows = install.draft_id ? await client.query<{ status: string; confirmed_by: string | null }>(
      `SELECT status, confirmed_by FROM wizard_staffing_drafts WHERE workspace_id=$1 AND id=$2`,
      [workspaceId, install.draft_id],
    ) : null;
    const examRows = install.qualified_exam_id ? await client.query<{
      id: string; status: string; verdict: string | null; assessment_kind: string;
      target_install_id: string | null; target_draft_id: string | null;
      target_version: number | null; target_hash: string | null;
    }>(
      `SELECT id, status, verdict, assessment_kind,
              target_install_id, target_draft_id, target_version, target_hash
       FROM eval_exams WHERE workspace_id=$1 AND id=$2`,
      [workspaceId, install.qualified_exam_id],
    ) : null;
    const exam = examRows?.rows[0];
    const candidateEvidence = exam ? await client.query<{ agent_id: string; passed: boolean; red_line_hit: boolean }>(
      `SELECT agent_id, passed, red_line_hit FROM eval_candidate_results
       WHERE workspace_id=$1 AND exam_id=$2 AND install_id=$3 AND draft_id=$4
         AND assembly_version=$5 AND assembly_hash=$6
       ORDER BY agent_id`,
      [workspaceId, exam.id, install.id, install.draft_id, install.assembly_version, install.assembly_hash],
    ) : null;
    const candidateRows = candidateEvidence?.rows ?? [];
    const candidateIdsMatch = JSON.stringify(candidateRows.map((row) => row.agent_id))
      === JSON.stringify([...presetIds].sort());
    const installBinding: ExamBinding | null = install.draft_id && install.assembly_version !== null && install.assembly_hash
      ? { installId: install.id, draftId: install.draft_id, version: install.assembly_version, hash: install.assembly_hash }
      : null;
    const examBinding: ExamBinding | null = exam?.target_install_id && exam.target_draft_id
      && exam.target_version !== null && exam.target_hash
      ? { installId: exam.target_install_id, draftId: exam.target_draft_id, version: exam.target_version, hash: exam.target_hash }
      : null;
    const confirmedBy = draftRows?.rows[0]?.confirmed_by?.trim() ?? "";
    fenceResponsibilityReady = fenceResponsibilityReady
      && Boolean(confirmedBy)
      && activeFenceRows.every((fence) => fence.created_by === confirmedBy);
    customAssembly = {
      required: true,
      installStatus: install.status,
      draftStatus: draftRows?.rows[0]?.status ?? null,
      examStatus: exam?.status ?? null,
      examVerdict: exam?.verdict ?? null,
      examAssessmentKind: exam?.assessment_kind ?? null,
      candidateExpected: presetIds.length,
      candidateTotal: candidateRows.length,
      candidatePassed: candidateIdsMatch
        ? candidateRows.filter((row) => row.passed && !row.red_line_hit).length
        : -1,
      installBinding,
      examBinding,
    };
  }

  const readinessFacts = {
    llmReal: llmVerified,
    workspaceIsExample: Boolean(workspace?.is_example),
    business: business ? {
      name: business.name,
      industry: business.industry,
      configuredAt: business.configured_at,
      source: business.source,
    } : null,
    activeInstall: exactlyOneActiveInstall,
    expectedAgents: presetIds.length,
    readyInstalledAgents: readyAgents,
    expectedFences: fenceIds.length,
    activeInstalledFences: activeFences,
    fenceResponsibilityReady,
    expectedSkills: skillIds.length,
    installedSkills,
    capabilityClaimsSafe,
    pendingCapabilities,
    customAssembly,
  };
  return {
    persistedDataMode: archive.dataMode === "real" ? "real" : "simulated",
    llmVerified,
    formalActivationRecorded,
    business: business ? {
      name: business.name ?? "",
      industry: business.industry ?? "",
      note: business.note ?? "",
      configuredAt: business.configured_at ?? "",
      source: business.source ?? "",
    } : null,
    workspace: {
      name: workspace?.name ?? "",
      bundleId: workspace?.bundle_id ?? null,
      isExample: Boolean(workspace?.is_example),
    },
    // runtimeGate 判定工作区是否仍可被称为“正式”；不因只读/经理查看而降级。
    runtimeGate: evaluateRealModeReadiness({ ...readinessFacts, activationOwner: true }),
    // gate 是当前操作者的激活资格，非 owner 会看到明确阻断项。
    gate: evaluateRealModeReadiness({ ...readinessFacts, activationOwner }),
  };
}

const onboardingRouter = router({
  /** 首次欢迎按账号/角色/工作区持久化；只读成员也只能写自己的欢迎进度。 */
  welcomeStatus: protectedProcedure.query(async ({ ctx }) => ({
    ...(await welcomeProgress(ctx.identity.workspaceId, {
      memberId: ctx.identity.memberId,
      memberNo: ctx.identity.memberNo,
      role: ctx.identity.role,
    })),
    role: ctx.identity.role,
  })),

  saveWelcomeProgress: protectedProcedure
    .input(z.object({
      status: z.enum(["in_progress", "paused", "completed"]),
      currentStep: z.enum(WELCOME_STEPS),
    }))
    .mutation(async ({ ctx, input }) => saveWelcomeProgress(ctx.identity.workspaceId, {
      memberId: ctx.identity.memberId,
      memberNo: ctx.identity.memberNo,
      role: ctx.identity.role,
    }, input)),

  replayWelcome: protectedProcedure.mutation(async ({ ctx }) => replayWelcome(ctx.identity.workspaceId, {
    memberId: ctx.identity.memberId,
    memberNo: ctx.identity.memberNo,
    role: ctx.identity.role,
  })),

  /** 标准落地向导草稿：显式版本号防多人覆盖，payload 白名单不接收密钥和文档正文。 */
  wizardDraft: protectedProcedure.query(async ({ ctx }) => {
    const draft = await getWizardDraft(ctx.identity.workspaceId);
    return draft ? safeWizardDraftView(draft) : null;
  }),

  saveWizardDraft: actionProcedure("workspace.configure")
    .input(z.object({
      expectedVersion: z.number().int().min(0),
      currentStep: z.number().int().min(0).max(4),
      payload: z.object({
        provider: z.string().max(40).optional(),
        baseUrl: modelEndpointSchema.optional(),
        model: z.string().max(80).optional(),
        businessName: z.string().max(60).optional(),
        industry: z.string().max(40).optional(),
        note: z.string().max(300).optional(),
        siteUrl: z.string().max(500).optional(),
        documentTitle: z.string().max(120).optional(),
        testQuestion: z.string().max(500).optional(),
      }).strict(),
    }))
    .mutation(async ({ ctx, input }) => safeWizardDraftView(await saveWizardDraft(ctx.identity.workspaceId, {
      memberId: ctx.identity.memberId,
      memberNo: ctx.identity.memberNo,
      role: ctx.identity.role,
    }, input))),

  assignWizardDraft: actionProcedure("workspace.configure")
    .input(z.object({ memberId: z.string().min(1), expectedVersion: z.number().int().min(1) }))
    .mutation(async ({ ctx, input }) => safeWizardDraftView(await assignWizardDraft(ctx.identity.workspaceId, {
      memberId: ctx.identity.memberId,
      memberNo: ctx.identity.memberNo,
      role: ctx.identity.role,
    }, input))),

  completeWizardDraft: actionProcedure("workspace.configure")
    .input(z.object({ expectedVersion: z.number().int().min(1) }))
    .mutation(async ({ ctx, input }) => safeWizardDraftView(await completeWizardDraft(ctx.identity.workspaceId, {
      memberId: ctx.identity.memberId,
      memberNo: ctx.identity.memberNo,
      role: ctx.identity.role,
    }, input.expectedVersion))),

  /** C 端发布结果必须来自部署环境、工作区路由和渠道接入事实。 */
  serviceFrontPublication: protectedProcedure.query(({ ctx }) => resolveServiceFrontPublication({
    workspaceId: ctx.identity.workspaceId,
    bundledClientAvailable: bundledServiceFrontAvailable(),
  })),

  /** 运行态总览（P0 横幅/落地向导同一事实源）：数据模式 + LLM 装配 + 工作区规模 */
  status: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
      const gateSnapshot = await loadOnboardingGate(client, scope.workspaceId, ctx.identity.role === "owner");
      const n = async (sql: string) => Number((await client.query<{ n: string }>(sql, [scope.workspaceId])).rows[0]?.n ?? 0);
      const events = await n(`SELECT count(*)::text AS n FROM biz_events WHERE workspace_id=$1`);
      const members = await n(`SELECT count(*)::text AS n FROM members WHERE workspace_id=$1`);
      const agents = await n(`SELECT count(*)::text AS n FROM agents WHERE workspace_id=$1`);
      const memories = await n(`SELECT count(*)::text AS n FROM org_memory WHERE workspace_id=$1`);
      await client.query("COMMIT");
      return {
        // 旧库即使曾被直接写成 real，只要当前事实未过门禁，客户端仍按 simulated 展示。
        dataMode: (gateSnapshot.persistedDataMode === "real"
          && gateSnapshot.formalActivationRecorded
          && gateSnapshot.runtimeGate.canActivate ? "real" : "simulated") as "simulated" | "real",
        persistedDataMode: gateSnapshot.persistedDataMode,
        formalActivationRecorded: gateSnapshot.formalActivationRecorded,
        activationGate: gateSnapshot.gate,
        business: gateSnapshot.business,
        llm: { ...llmAssembly(), real: gateSnapshot.llmVerified, credentialStorage: "process" as const, restartNotice: LLM_RESTART_NOTICE },
        workspace: { name: gateSnapshot.workspace.name, events, members, agents, memories },
        workspaceId: scope.workspaceId,
        // V4 §2 示例明示：示例包装配标记（SimBanner 银带语义事实源）
        bundle: { id: gateSnapshot.workspace.bundleId, isExample: gateSnapshot.workspace.isExample },
      };
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }),

  /** 第①步：真实大模型「测试连接」（真实 round-trip；不落盘、不留痕 key） */
  testLlm: actionProcedure("workspace.configure")
    .input(z.object({
      baseUrl: modelEndpointSchema.refine(value => value.length > 0, { message: "模型端点不能为空" }),
      apiKey: z.string().max(200).default(""),
      model: z.string().min(1).max(80),
    }))
    .mutation(async ({ input }) => {
      try {
        const r = await probeLlm({ ...input, baseUrl: safeModelEndpoint(input.baseUrl)! });
        return { ok: true as const, ...r, reply: `${r.reply}\n${LLM_RESTART_NOTICE}`, credentialStorage: "process" as const, restartNotice: LLM_RESTART_NOTICE };
      } catch (err) {
        return { ok: false as const, error: "模型连接测试未通过；请检查端点、模型名称和凭据，配置尚未生效。" };
      }
    }),

  /** 第①步保存：真实试调通过后更新当前进程；provider=mock 为还原操作，免实测。 */
  saveLlmConfig: actionProcedure("workspace.configure")
    .input(z.object({
      provider: z.string().min(1).max(40),
      baseUrl: modelEndpointSchema.default(""),
      apiKey: z.string().max(200).default(""),
      model: z.string().max(80).default(""),
    }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const baseUrl = input.baseUrl ? safeModelEndpoint(input.baseUrl)! : "";
      if (input.provider !== "mock") {
        if (!baseUrl || !input.model) {
          throw new TRPCError({ code: "BAD_REQUEST", message: "真实模型装配需要合法的 baseUrl 与 model" });
        }
        try {
          await probeLlm({ baseUrl, apiKey: input.apiKey, model: input.model }); // 真实试调不过 → 拒绝保存
        } catch (err) {
          throw new TRPCError({ code: "BAD_REQUEST", message: "模型连接测试未通过，配置未保存；请检查部署端点和凭据。" });
        }
      }
      // D16（#1/A）：事件写入并入显式事务（配置验证事实与事件同一 COMMIT 提交）
      const llmClient = await getAppPool().connect();
      try {
        await llmClient.query("BEGIN");
        await llmClient.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await llmClient.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        await llmClient.query(
          `UPDATE profiles
           SET archive=jsonb_set(archive, '{llmVerification}', $2::jsonb, true), updated_at=now()
           WHERE workspace_id=$1`,
          [scope.workspaceId, JSON.stringify({
            provider: input.provider,
            baseUrl,
            model: input.model,
            credentialFingerprint: llmCredentialFingerprint(input.provider === "mock" ? "" : input.apiKey),
            verified: input.provider !== "mock",
            verifiedAt: input.provider !== "mock" ? new Date().toISOString() : null,
            verifiedBy: input.provider !== "mock" ? ctx.identity.memberNo : null,
          })],
        );
        await gatewayAppendOnClient(llmClient, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `onboarding-${scope.workspaceId}`,
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
          object: { type: "workspace", id: scope.workspaceId },
          decision: {
            action: "onboarding.llm_configured",
            params: {
              provider: input.provider, base_url: baseUrl, model: input.model,
              key_configured: input.provider !== "mock" && input.apiKey.length > 0,
            },
            after: { real: input.provider !== "mock" },
            basis: ["落地向导：真实大模型装配（实测通过后更新进程装配，全链即时生效）"],
          },
          rule_impact: [],
          model_trace: { model_id: "human-operator", tier: "standard" },
        });
        await llmClient.query("COMMIT");
      } catch (err) {
        await llmClient.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        llmClient.release();
      }
      persistLlmEnv({ ...input, baseUrl });
      return { ok: true, real: input.provider !== "mock", credentialStorage: "process" as const, restartNotice: LLM_RESTART_NOTICE };
    }),

  /** 第②步：经营主体信息（工作区名 + 行业 + 简介 → 档案；事件留痕） */
  setupWorkspace: actionProcedure("workspace.configure")
    .input(z.object({
      displayName: z.string().min(1).max(60),
      industry: z.string().min(1).max(40),
      note: z.string().max(300).default(""),
    }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        // 用户显式保存真实经营主体后退出“行业示例版”身份；在正式门禁通过前仍保持 simulated 数据模式。
        await client.query(
          `UPDATE workspaces SET name=$2, industry=$3, is_example=false WHERE id=$1`,
          [scope.workspaceId, input.displayName, input.industry],
        );
        // 只合并主体身份字段；保留客户已有经营档案与风险覆盖层。
        await client.query(
          `UPDATE profiles
              SET archive = jsonb_set(archive, '{business}', coalesce(archive->'business', '{}'::jsonb) || $2::jsonb),
                  industry = $3,
                  updated_at = now()
            WHERE workspace_id=$1`,
          [scope.workspaceId, JSON.stringify({
            name: input.displayName,
            industry: input.industry,
            note: input.note,
            configured_at: new Date().toISOString(),
            configured_by: ctx.identity.memberNo,
            source: "user",
          }), input.industry],
        );
        // D16（#1/A）：档案写与事件留痕同一事务同一 COMMIT
        await gatewayAppendOnClient(client, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `onboarding-${scope.workspaceId}`,
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
          object: { type: "workspace", id: scope.workspaceId },
          decision: {
            action: "onboarding.workspace_profile",
            params: { name: input.displayName, industry: input.industry, note: input.note },
            after: { name: input.displayName, industry: input.industry, is_example: false },
            basis: ["落地向导：经营主体信息写入一店一档（archive.business）"],
          },
          rule_impact: [],
          model_trace: { model_id: "human-operator", tier: "standard" },
        });
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
      return { ok: true };
    }),

  /** 第③步：服务端全量门禁通过后才启用真实模式；不能把 mock/示例/缺资产状态改名为 real。 */
  activateRealMode: actionProcedure("workspace.configure").mutation(async ({ ctx }) => {
    if (ctx.identity.role !== "owner") {
      throw new TRPCError({ code: "FORBIDDEN", message: "只有工作区所有者可以启用正式经营模式" });
    }
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
      await client.query(`SELECT id FROM workspaces WHERE id=$1 FOR UPDATE`, [scope.workspaceId]);
      await client.query(`SELECT workspace_id FROM profiles WHERE workspace_id=$1 FOR UPDATE`, [scope.workspaceId]);
      const gateSnapshot = await loadOnboardingGate(client, scope.workspaceId, true);
      if (!gateSnapshot.gate.canActivate) {
        throw new TRPCError({
          code: "PRECONDITION_FAILED",
          message: `尚不能启用真实经营模式：${gateSnapshot.gate.blockers.join("；")}`,
        });
      }
      await client.query(
        `UPDATE profiles
         SET archive=jsonb_set(
               jsonb_set(archive, '{dataMode}', '"real"'::jsonb),
               '{realModeActivation}', $2::jsonb, true
             ),
             updated_at=now()
         WHERE workspace_id=$1`,
        [scope.workspaceId, JSON.stringify({ gateVersion: 1, activatedAt: new Date().toISOString(), activatedBy: ctx.identity.memberNo })],
      );
      await advanceWizardDraftOn(client, scope.workspaceId, ctx.identity.memberId);
      // D16（#1/A）：dataMode 翻转与事件留痕同一事务同一 COMMIT
      await gatewayAppendOnClient(client, {
        ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `onboarding-${scope.workspaceId}`,
      }, {
        who: { type: "human", id: ctx.identity.memberNo },
        context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
        object: { type: "workspace", id: scope.workspaceId },
          decision: {
            action: "onboarding.real_mode_activated",
            params: { from: "simulated", to: "real" },
            after: { dataMode: "real" },
            basis: ["落地向导服务端门禁全绿：真实模型、用户经营主体、非示例装配、装配内员工与围栏均已核验；定制装配另须同版本同哈希考试通过"],
          },
        rule_impact: [],
        model_trace: { model_id: "human-operator", tier: "standard" },
      });
      await client.query("COMMIT");
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
    return { ok: true, dataMode: "real" as const };
  }),
});

/** auth router：演示身份登录（总纲 §2.4：选择种子成员签发 JWT） */
const authRouter = router({
  loginAs: publicProcedure
    .input(z.object({ workspaceSlug: z.string(), memberNo: z.string() }))
    .mutation(async ({ input }) => {
      const host = process.env.SERVER_HOST?.trim() || "127.0.0.1";
      if (process.env.NODE_ENV === "production" || !["127.0.0.1", "localhost", "::1"].includes(host)) {
        throw new TRPCError({ code: "FORBIDDEN", message: "演示身份入口仅供本机开发使用，请通过正式账号登录" });
      }
      const app = getAppPool();
      // 登录引导例外点（F7.1）：身份未建立前无法 set_config，workspace 解析走 owner 池
      const ws = await getOwnerPool().query<{ id: string; tenant_id: string }>(
        `SELECT id, tenant_id FROM workspaces WHERE slug=$1`,
        [input.workspaceSlug],
      );
      const wsRow = ws.rows[0];
      if (!wsRow) throw new TRPCError({ code: "NOT_FOUND", message: `工作区 ${input.workspaceSlug} 不存在` });
      const scope = { tenantId: wsRow.tenant_id, workspaceId: wsRow.id };
      const member = await getMember(app, scope, input.memberNo);
      if (!member) throw new TRPCError({ code: "NOT_FOUND", message: `成员 ${input.memberNo} 不存在` });
      // 租户版本（登录引导例外点：同上走 owner 池）
      const t = await getOwnerPool().query<{ plan: Identity["plan"] }>(`SELECT plan FROM tenants WHERE id=$1`, [scope.tenantId]);
      const identity: Identity = {
        memberId: member.id,
        memberNo: member.memberNo,
        name: member.name,
        role: member.role,
        tenantId: scope.tenantId,
        workspaceId: scope.workspaceId,
        plan: t.rows[0]?.plan ?? "community",
      };
      return { token: await signDemoToken(identity), identity };
    }),
  /** 版本切换演示（F12 权限态：社区版/Pro/Teams/VPC 实切，F7.2 能力矩阵即时生效）
   *  owner 专属；写 tenants.plan（登录引导例外点同口径走 owner 池）+ 留痕 plan.switch（G8）+ 重签 JWT */
  setPlan: actionProcedure("tenant.plan.manage")
    .input(z.object({ plan: z.enum(PLAN_TIERS) }))
    .mutation(async ({ ctx, input }) => {
      if (ctx.identity.role !== "owner") {
        throw new TRPCError({ code: "FORBIDDEN", message: "仅 owner 可切换租户版本（F7.1/E2.6，服务端 403）" });
      }
      const before = ctx.identity.plan;
      // D16（#1/A）：版本切换与事件同一事务（owner 通道单连接；函数 EXECUTE 对 owner 无限制）
      const ownerClient = await getOwnerPool().connect();
      try {
        await ownerClient.query("BEGIN");
        await ownerClient.query("SELECT set_config('app.tenant_id', $1, true)", [ctx.identity.tenantId]);
        await ownerClient.query("SELECT set_config('app.workspace_id', $1, true)", [ctx.identity.workspaceId]);
        await ownerClient.query(`UPDATE tenants SET plan=$2 WHERE id=$1`, [ctx.identity.tenantId, input.plan]);
        await gatewayAppendOnClient(ownerClient, {
          tenantId: ctx.identity.tenantId, workspaceId: ctx.identity.workspaceId,
          actor: { id: ctx.identity.memberNo, type: "human" },
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: {
            tenant_id: ctx.identity.tenantId, workspace_id: ctx.identity.workspaceId,
            time: new Date().toISOString(), channel: "inapp",
          },
          object: { type: "tenant", id: ctx.identity.tenantId },
          decision: {
            action: "plan.switch", before: { plan: before }, after: { plan: input.plan },
            basis: ["F7.2 版本能力矩阵即时生效", "F12 权限态演示"],
          },
          rule_impact: [],
        });
        await ownerClient.query("COMMIT");
      } catch (err) {
        await ownerClient.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        ownerClient.release();
      }
      const identity: Identity = { ...ctx.identity, plan: input.plan };
      return { token: await signDemoToken(identity), plan: input.plan };
    }),
});

function accessRethrow(error: unknown): never {
  if (error instanceof AccessAuthorityError) {
    throw new TRPCError({
      code: error.code === "SESSION_INVALID" ? "UNAUTHORIZED"
        : error.code === "TARGET_REQUIRED" ? "BAD_REQUEST"
          : "FORBIDDEN",
      message: error.message,
    });
  }
  throw error;
}

/**
 * 三端访问权威接口：导航、深链与动作均消费服务端实时身份和已验 Bundle 投影。
 * 此入口必须与 NavigationAccessProvider 同步挂载；缺失时游客首屏会失败关闭。
 */
const accessRouter = router({
  me: sessionProcedure
    .input(z.object({ tenantId: z.string().min(1), workspaceId: z.string().min(1) }).optional())
    .query(async ({ ctx, input }) => {
      try {
        return await resolveAuthoritativeClientAccess(ctx.session, input);
      } catch (error) {
        accessRethrow(error);
      }
    }),
});

/** members router：me（角色+版本能力下发，F5.6 三端一致的数据源）/ list */
const membersRouter = router({
  me: protectedProcedure.query(({ ctx }) => {
    return {
      identity: ctx.identity,
      capabilities: getCapabilities(ctx.identity.plan),
    };
  }),
  list: protectedProcedure.query(async ({ ctx }) => {
    return listMembers(getAppPool(), scopeOf(ctx.identity));
  }),
});

/** Dispatch must satisfy current task permission and the verified active Tiger execution declaration. */
const tigerDispatchProcedure = capabilityActionProcedure("quest", "task.dispatch").use(async ({ ctx, next }) => {
  const access = await resolveAuthoritativeClientAccess(ctx.identity);
  if (!access.actionPermissions.includes("trading.research.execute")) {
    throw new TRPCError({ code: "FORBIDDEN", message: "当前成员或已验活动行业包没有 Tiger 研究执行权限" });
  }
  return next();
});

const threadsRouter = router({
  list: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const client = await getAppPool().connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id',$1,true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id',$1,true)", [scope.tenantId]);
      const result = await client.query(`SELECT id,title,mode,status,progress_done,progress_total,created_by,agent_id,created_at
        FROM threads WHERE workspace_id=$1 ORDER BY created_at DESC LIMIT 50`, [scope.workspaceId]);
      await client.query("COMMIT");
      return result.rows;
    } catch (error) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw error;
    } finally { client.release(); }
  }),

  dispatch: tigerDispatchProcedure
    .input(z.object({ title: z.string().trim().min(1).max(500), presetKey: z.string().min(1).nullish(),
      goalRef: z.string().min(1).max(120).nullish(), research: tigerResearchSchema.optional(),
      runImmediately: z.boolean().default(false) }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const intent = await routeIntent(input.title, intentClassifier(scope));
      if (intent.kind === "clarify") return { kind: "clarify" as const, question: intent.clarifyQuestion, via: intent.via };
      const goalArchive = input.goalRef ? await loadGoalArchive(scope, input.goalRef) : undefined;
      if (input.goalRef && !goalArchive) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "目标档案不存在或无法读取，未派遣" });
      const goal = goalArchive?.text ?? input.title;
      const client = await getAppPool().connect();
      let threadId = "";
      let presetKey = "";
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id',$1,true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id',$1,true)", [scope.tenantId]);
        await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`thread-dispatch:${scope.workspaceId}`]);
        const count = await client.query<{ n: string }>("SELECT count(*)::text n FROM threads WHERE workspace_id=$1 AND status IN ('queued','running')", [scope.workspaceId]);
        if (Number(count.rows[0]?.n ?? 0) >= MAX_CONCURRENT_THREADS) throw new TRPCError({ code: "TOO_MANY_REQUESTS", message: `工作区并发上限 ${MAX_CONCURRENT_THREADS}，请等待当前任务结束` });
        const allocated = await insertWithReadableId(client, THREAD_ID_SOURCE, async (id) => {
          await client.query(`INSERT INTO threads(id,tenant_id,workspace_id,title,mode,status,created_by)
            VALUES($1,$2,$3,$4,$5,'queued',$6)`, [id,scope.tenantId,scope.workspaceId,input.title,intent.mode,ctx.identity.memberNo]);
          return id;
        });
        threadId = allocated.id;
        const prepared = await prepareTigerThreadOn(client, scope, { threadId, goal, presetRef: input.presetKey, research: input.research });
        presetKey = prepared.presetKey;
        await gatewayAppendOnClient(client, { ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: threadId }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString(), channel: "inapp" },
          object: { type: "thread", id: threadId },
          decision: { action: "thread.dispatch", after: { threadId, title: input.title, mode: intent.mode, presetKey,
            rationale: intent.rationale, goal_ref: input.goalRef ?? null, goal_version: goalArchive?.version ?? null } }, rule_impact: [],
        });
        await client.query("COMMIT");
      } catch (error) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw error;
      } finally { client.release(); }
      if (intent.mode === "ask") {
        const result = await runAsk(getAppPool(), getGatewayPool(), scope, { threadId, goal, presetKey,
          llmCall: llmCall("ask-synthesize", scope) });
        return { kind: "routed" as const, mode: intent.mode, via: intent.via, threadId, presetKey, status: result.status, answer: result.answer };
      }
      if (input.runImmediately) {
        const result = await runTigerQuestForThread(scope, { threadId, goal, presetRef: presetKey,
          mode: intent.mode === "agent" ? "agent" : "quest" });
        return { kind: "routed" as const, mode: intent.mode, via: intent.via, ...result };
      }
      return { kind: "routed" as const, mode: intent.mode, via: intent.via, threadId, presetKey, status: "queued" as const };
    }),

  /** 线程详情（P2 线程头/信息面板；L7.1 越权返回空） */
  get: protectedProcedure
    .input(z.object({ threadId: z.string() }))
    .query(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const r = await client.query(
          `SELECT id, title, mode, status, progress_done, progress_total, created_by, agent_id, created_at, updated_at
           FROM threads WHERE workspace_id=$1 AND id=$2`,
          [scope.workspaceId, input.threadId],
        );
        await client.query("COMMIT");
        return r.rows[0] ?? null;
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
    }),

  /** 行动消息流（P2-⑤：该线程的事件流子序列投影，按 ts 升序；含 rule_impact/model_trace 渲染位） */
  events: protectedProcedure
    .input(z.object({ threadId: z.string(), limit: z.number().min(1).max(200).default(100) }))
    .query(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const r = await client.query<{ payload: unknown }>(
          `SELECT payload FROM biz_events
           WHERE workspace_id=$1 AND session_id=$2 ORDER BY seq ASC LIMIT $3`,
          [scope.workspaceId, input.threadId, input.limit],
        );
        await client.query("COMMIT");
        return r.rows.map((x) => x.payload);
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
    }),

  run: tigerDispatchProcedure
    .input(z.object({ threadId: z.string().min(1), goal: z.string().min(1).optional(), presetKey: z.string().min(1).nullish() }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const client = await getAppPool().connect();
      let row: { title: string; mode: string; agent_id: string | null } | undefined;
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id',$1,true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id',$1,true)", [scope.tenantId]);
        row = (await client.query<{ title: string; mode: string; agent_id: string | null }>(
          "SELECT title,mode,agent_id FROM threads WHERE id=$1 AND workspace_id=$2", [input.threadId, scope.workspaceId])).rows[0];
        if (!row) throw new TRPCError({ code: "NOT_FOUND", message: "任务不存在或无权访问" });
        await client.query("COMMIT");
      } catch (error) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw error;
      } finally { client.release(); }
      if (row.mode === "ask") return runAsk(getAppPool(), getGatewayPool(), scope, { threadId: input.threadId,
        goal: input.goal ?? row.title, presetKey: input.presetKey ?? "kernel-orchestrator", llmCall: llmCall("ask-synthesize", scope) });
      return runTigerQuestForThread(scope, { threadId: input.threadId, goal: input.goal ?? row.title,
        presetRef: input.presetKey ?? row.agent_id, mode: row.mode === "agent" ? "agent" : "quest" });
    }),
});

const fenceCandidateSchema = z.object({
  ruleId: z.string().regex(/^R(?:-[A-Za-z0-9]+|[0-9]+)$/), name: z.string().min(1).max(100),
  level: z.enum(["auto", "review", "block"]),
  objectTypes: z.array(z.string().min(1).max(100)).min(1).max(50),
  actions: z.array(z.string().min(1).max(100)).min(1).max(50), when: z.string().min(1).max(2000),
}).strict();
type FenceCandidate = z.infer<typeof fenceCandidateSchema>;
function fenceCandidateHash(candidate: FenceCandidate): string {
  return createHash("sha256").update(stableStringify(candidate)).digest("hex");
}
async function ownedScopedTransaction<T>(scope: { tenantId: string; workspaceId: string }, fn: (client: pg.PoolClient) => Promise<T>): Promise<T> {
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
    catch (rollbackError) { throw new AggregateError([error, rollbackError], "治理事务失败且回滚未成功"); }
    throw error;
  } finally { client.release(); }
}

/**
 * E1 联调接线（PF.5/F2.4）：审批手势通过后的副作用分发——
 * 被审批事件为 fence.rule.propose 且手势=通过 → 激活对应围栏规则版本（activateRuleVersion）。
 * 幂等：规则已离开 pending_approval/draft（重复回调/重复提案）时跳过不报错（L5.3 同口径）。
 * 返回激活的规则行 ID（未触发接线返回 null）。
 */
async function activateFenceRuleAfterApproval(
  scope: { tenantId: string; workspaceId: string },
  approvalId: string,
  actorMemberNo: string,
): Promise<string | null> {
  return ownedScopedTransaction(scope, async (client) => {
    const r = await client.query<{
      payload: { decision?: { after?: Record<string, unknown> } }; status: string;
      snapshot: { after?: Record<string, unknown>; ruleRowId?: string; candidate_sha256?: string; expires_at?: string };
      decided_by: string | null;
    }>(
      `SELECT e.payload,a.status,a.snapshot,a.decided_by FROM approvals a
       JOIN biz_events e ON e.event_id=a.event_id AND e.workspace_id=a.workspace_id
       WHERE a.approval_id=$1 AND a.workspace_id=$2 AND a.tenant_id=$3`,
      [approvalId, scope.workspaceId, scope.tenantId],
    );
    const approved = r.rows[0];
    const params = fenceActivationFromProposal(approved?.payload, scope.workspaceId);
    if (!approved || !params) return null;
    const after = approved.payload.decision?.after ?? {};
    const candidateResult = fenceCandidateSchema.safeParse({ ruleId: after.ruleId, name: after.name, level: after.level,
      objectTypes: after.objectTypes, actions: after.actions, when: after.when });
    if (!candidateResult.success) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "围栏提案未绑定完整被审候选，不能激活" });
    const candidate = candidateResult.data;
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fence-edit:${scope.workspaceId}:${candidate.ruleId}`]);
    const row = (await client.query<{
      rule_id: string; name: string; level: "auto" | "review" | "block"; status: string;
      match_spec: { object_types: string[]; actions: string[]; when: string };
    }>("SELECT rule_id,name,level,status,match_spec FROM fence_rules WHERE id=$1 AND workspace_id=$2",
      [params.ruleRowId, scope.workspaceId])).rows[0];
    if (!row) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "被审围栏候选不存在，未激活" });
    if (row.status === "active" || row.status === "rolled_back") return null;
    const expires = approved.snapshot?.expires_at;
    const candidateSha256 = fenceCandidateHash(candidate);
    const storedCandidate = { ruleId: row.rule_id, name: row.name, level: row.level,
      objectTypes: row.match_spec.object_types, actions: row.match_spec.actions, when: row.match_spec.when };
    if (approved.status !== "approved" || approved.decided_by !== actorMemberNo
      || approved.snapshot?.ruleRowId !== params.ruleRowId || approved.snapshot?.after?.dryRunId !== params.dryRunId
      || approved.snapshot?.candidate_sha256 !== candidateSha256 || after.candidate_sha256 !== candidateSha256
      || fenceCandidateHash(storedCandidate) !== candidateSha256
      || (expires !== undefined && (!Number.isFinite(Date.parse(expires)) || Date.parse(expires) <= Date.now()))) {
      throw new TRPCError({ code: "PRECONDITION_FAILED", message: "审批人与被审候选、正文摘要或有效期不一致，未激活围栏" });
    }
    // 审批留痕 ID = 手势回写事件（approval.gesture，F5.5 经安全网关落库）
    const g = await client.query<{ event_id: string }>(
      `SELECT g.event_id FROM biz_events g
        JOIN approvals a ON a.workspace_id=g.workspace_id
          AND a.approval_id=g.payload->'decision'->'after'->>'approvalId'
        WHERE g.workspace_id=$1 AND a.approval_id=$2 AND a.status='approved'
          AND g.payload->'decision'->>'action'='approval.gesture'
          AND g.payload->'who'->>'type'='human' AND g.payload->'who'->>'id'=$3
          AND a.decided_by=$3 AND a.snapshot->>'ruleRowId'=$4
          AND a.snapshot->'after'->>'dryRunId'=$5
          AND g.payload->'decision'->'after'->>'gesture'='approve'
        ORDER BY g.seq DESC LIMIT 1`,
      [scope.workspaceId, approvalId, actorMemberNo, params.ruleRowId, params.dryRunId],
    );
    const approvalEventId = g.rows[0]?.event_id;
    if (!approvalEventId) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "审批人与候选规则的真实事件绑定不一致，未激活围栏" });
    if (row.status !== "draft" && row.status !== "pending_approval") return null;
    // 公共激活器负责最终基线单调守卫和生效事务；当前事务只持同一规则的 advisory 锁。
    // 不持候选行锁，避免公共激活器的独立连接互相等待。
    await activateRuleVersion(getAppPool(), scope, { ...params, approvalEventId });
    return params.ruleRowId;
  });
}

/** approvals router（B6：统一队列/三手势/批量/超时扫描；L5.1 服务端强制鉴权） */
const approvalsRouter = router({
  list: protectedProcedure
    .input(z.object({ status: z.enum(["pending", "approved", "edited", "rejected", "expired"]).optional() }).optional())
    .query(async ({ ctx, input }) => {
      return listQueue(getAppPool(), scopeOf(ctx.identity), { status: input?.status });
    }),

  decide: actionProcedure("approval.decide")
    .input(
      z.object({
        approvalId: z.string(),
        gesture: z.enum(["approve", "edit", "reject"]),
        reasonEnum: z.string().optional(),
        reasonText: z.string().max(200).optional(),
        editedAfter: z.unknown().optional(),
        /** M1.3 归因分流（D24 修订 3）：edit 手势必填二分（纠错/口味） */
        editKind: z.enum(["correction", "preference"]).optional(),
      }),
    )
    .mutation(async ({ ctx, input }) => {
      try {
        const res = await decide(
          getAppPool(),
          getGatewayPool(),
          scopeOf(ctx.identity),
          { memberNo: ctx.identity.memberNo, role: ctx.identity.role },
          input.approvalId,
          { type: input.gesture, reasonEnum: input.reasonEnum, reasonText: input.reasonText, editedAfter: input.editedAfter, editKind: input.editKind },
        );
        // E1 联调接线（PF.5/F2.4）：fence.rule.propose 手势通过 → 激活规则版本
        if (res.status === "approved") {
          await activateFenceRuleAfterApproval(scopeOf(ctx.identity), input.approvalId, ctx.identity.memberNo);
        }
        if (["approved", "edited", "rejected"].includes(res.status)) {
          await applyKbPublishAfterApproval(scopeOf(ctx.identity), input.approvalId);
        }
        if (!res.deduped && res.status === "approved") {
          // D22 汰换重生：hr.replacement 批准 → 旧停用 + 新员工上岗
          const scope2 = scopeOf(ctx.identity);
          const app2 = getAppPool();
          const c2 = await app2.connect();
          try {
            await c2.query("BEGIN");
            await c2.query("SELECT set_config('app.workspace_id', $1, true)", [scope2.workspaceId]);
            const snap = await c2.query<{ snapshot: Record<string, unknown> }>(
              `SELECT snapshot FROM approvals WHERE approval_id=$1`, [input.approvalId],
            );
            await c2.query("COMMIT");
            const ss = snap.rows[0]?.snapshot ?? {};
            if (ss.kind === "hr.replacement" && ss.design && typeof ss.agent_id === "string") {
              await applyReplacement(app2, scope2, ss.design as never, ss.agent_id);
            }
          } catch (e) {
            await c2.query("ROLLBACK").catch(() => undefined);
            throw e;
          } finally {
            c2.release();
          }
        }
        return res;
      } catch (err) {
        if (err instanceof ApprovalError) {
          throw new TRPCError({
            code: err.code === "FORBIDDEN_ROLE" ? "FORBIDDEN" : "BAD_REQUEST",
            message: err.message,
          });
        }
        throw err;
      }
    }),

  batchApprove: actionProcedure("approval.decide")
    .input(z.object({ approvalIds: z.array(z.string()).min(1).max(50) }))
    .mutation(async ({ ctx, input }) => {
      try {
        const res = await batchApprove(
          getAppPool(),
          getGatewayPool(),
          scopeOf(ctx.identity),
          { memberNo: ctx.identity.memberNo, role: ctx.identity.role },
          input.approvalIds,
        );
        // E1 联调接线（PF.5/F2.4）：批量采纳通过项同样触发围栏激活接线（防御性；围栏提案标记 high_risk 本不可批量）
        for (const id of res.approved) {
          await activateFenceRuleAfterApproval(scopeOf(ctx.identity), id, ctx.identity.memberNo);
          await applyKbPublishAfterApproval(scopeOf(ctx.identity), id);
        }
        return res;
      } catch (err) {
        if (err instanceof ApprovalError) {
          throw new TRPCError({ code: "FORBIDDEN", message: err.message });
        }
        throw err;
      }
    }),

  /** 超时升级扫描（F5.7；高危项不自动放行 L5.4）——由触发器/巡检调度调用 */
  sweep: actionProcedure("approval.decide").mutation(async ({ ctx }) => {
    return expireSweep(getAppPool(), getGatewayPool(), scopeOf(ctx.identity));
  }),
});

/** inspection router（B10/M9：巡检状态条 / 手动巡检 / 一键派单 / 回链） */
const inspectionRouter = router({
  /** 巡检状态条（F9.4 纯投影：正常项/总数 + 最近巡检时间 + 异常点名 ≤5 条） */
  status: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const binding = await resolveWorkspaceInspectionAdapter(scope.workspaceId);
    if (binding.state !== "ready" || !binding.adapter) return {
      enabled: false, bindingState: binding.state, message: binding.state === "inspection-disabled"
        ? "当前行业包未启用巡检" : "巡检尚未通过行业装配验证",
      lastRunAt: null, totalChecks: 0, okCount: 0, attention: [], lastRunFailed: binding.state !== "inspection-disabled",
    };
    return { ...await inspectionStatusBar(getAppPool(), scope), enabled: true, bindingState: "ready" as const, message: "" };
  }),
  /** 手动跑一轮巡检（生产由触发器引擎 cron 07:00 唤起，F9.1；演示手动触发） */
  run: writeProcedure.mutation(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const binding = await resolveWorkspaceInspectionAdapter(scope.workspaceId);
    if (binding.state !== "ready" || !binding.adapter) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前行业包未启用已验巡检，不能生成巡检结果" });
    return runInspectionScan(getAppPool(), getGatewayPool(), scope, { adapter: binding.adapter });
  }),
  /** 一键派单（F9.3：以异常事件为输入唤起业务 Agent；幂等 L9.3） */
  dispatch: writeProcedure
    .input(z.object({ anomalyEventId: z.string(), presetKey: z.string().default("review-agent") }))
    .mutation(async ({ ctx, input }) => {
      try {
        const binding = await resolveWorkspaceInspectionAdapter(scopeOf(ctx.identity).workspaceId);
        if (binding.state !== "ready" || !binding.adapter) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前行业包未启用已验巡检，不能派发巡检任务" });
        return await dispatchFromAnomaly(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          anomalyEventId: input.anomalyEventId, presetKey: input.presetKey, by: ctx.identity.memberNo,
        });
      } catch (err) {
        if (err instanceof DispatchError) {
          throw new TRPCError({ code: err.code === "ANOMALY_NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
    }),
  /** 处理结果回链（F9.3/E9.3：失败升级一级严重度 + 转需介入） */
  resolve: writeProcedure
    .input(z.object({ anomalyEventId: z.string(), threadId: z.string(), ok: z.boolean(), note: z.string().max(500).optional() }))
    .mutation(async ({ ctx, input }) => {
      try {
        const binding = await resolveWorkspaceInspectionAdapter(scopeOf(ctx.identity).workspaceId);
        if (binding.state !== "ready" || !binding.adapter) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前行业包未启用已验巡检，不能回写巡检结果" });
        return await resolveAnomaly(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          ...input, by: ctx.identity.memberNo,
        });
      } catch (err) {
        if (err instanceof DispatchError) {
          throw new TRPCError({ code: err.code === "ANOMALY_NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
    }),
});

/** skills router（B10/M8：技能广场 / 安装绑定 / 零代码锻造 / 意识系统） */
/** 技能管理写操作角色守卫（E2.6/L5.1 同口径：readonly 服务端 403，前端隐藏非置灰） */
function assertSkillManage(role: string): void {
  if (role === "readonly") {
    throw new TRPCError({ code: "FORBIDDEN", message: "readonly 角色无技能管理权限（E2.6，服务端 403）" });
  }
}

const skillsRouter = router({
  list: protectedProcedure
    .input(z.object({ level: z.enum(["official", "team", "industry"]).optional() }).optional())
    .query(async ({ ctx, input }) => listSkills(getAppPool(), scopeOf(ctx.identity), { level: input?.level })),
  installs: protectedProcedure.query(async ({ ctx }) => {
    return listInstalls(getAppPool(), scopeOf(ctx.identity));
  }),
  /** F8.5 技能使用看板：每技能 30 天事件投影——调用=绑定 Agent 动作数 / 采纳率 / 驳回模式分布
   *  归因口径：agents.skills 声明（短名/全 id 双形态匹配）→ 绑定 Agent 的 who.id 事件聚合 */
  usage: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const client = await getAppPool().connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
      const skillIds = (await client.query<{ id: string }>(`SELECT id FROM skills ORDER BY id`)).rows.map((r) => r.id);
      const out: Record<string, {
        calls30: number; adopted30: number; rejected30: number; adoptionRate: number | null;
        rejectReasons: Array<{ reason: string; count: number }>;
        boundAgents: Array<{ id: string; presetKey: string; name: string }>;
      }> = {};
      for (const skillId of skillIds) {
        const short = skillId.replace(/^skill-[ti]?-?/, "");
        const agents = await client.query<{ id: string; preset_key: string; name: string }>(
          `SELECT id, preset_key, name FROM agents
           WHERE workspace_id=$1 AND (skills ? $2 OR skills ? $3) ORDER BY preset_key`,
          [scope.workspaceId, short, skillId],
        );
        const keys = agents.rows.map((a) => a.preset_key);
        if (keys.length === 0) {
          out[skillId] = { calls30: 0, adopted30: 0, rejected30: 0, adoptionRate: null, rejectReasons: [], boundAgents: [] };
          continue;
        }
        const calls = await client.query<{ c: string }>(
          `SELECT count(*) AS c FROM biz_events
           WHERE workspace_id=$1 AND created_at > now() - interval '30 days'
             AND payload->'who'->>'type'='agent' AND payload->'who'->>'id' = ANY($2)`,
          [scope.workspaceId, keys],
        );
        const gestures = await client.query<{ status: string; c: string }>(
          `SELECT a.status, count(*) AS c FROM approvals a
           JOIN biz_events e ON e.event_id = a.event_id
           WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = ANY($2)
             AND a.created_at > now() - interval '30 days'
           GROUP BY a.status`,
          [scope.workspaceId, keys],
        );
        const reasons = await client.query<{ reason: string; c: string }>(
          `SELECT a.gesture->>'reason_enum' AS reason, count(*) AS c FROM approvals a
           JOIN biz_events e ON e.event_id = a.event_id
           WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = ANY($2)
             AND a.status='rejected' AND a.created_at > now() - interval '30 days'
           GROUP BY 1 ORDER BY 2 DESC LIMIT 3`,
          [scope.workspaceId, keys],
        );
        const adopted = gestures.rows.filter((g) => g.status === "approved" || g.status === "edited").reduce((s, g) => s + Number(g.c), 0);
        const rejected = Number(gestures.rows.find((g) => g.status === "rejected")?.c ?? 0);
        out[skillId] = {
          calls30: Number(calls.rows[0]?.c ?? 0),
          adopted30: adopted,
          rejected30: rejected,
          adoptionRate: adopted + rejected > 0 ? adopted / (adopted + rejected) : null,
          rejectReasons: reasons.rows.map((r) => ({ reason: r.reason ?? "未填", count: Number(r.c) })),
          boundAgents: agents.rows.map((a) => ({ id: a.id, presetKey: a.preset_key, name: a.name })),
        };
      }
      return out;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),
  /** 安装（F8.2 安装即绑定；L8.1 脱敏闸 / L8.2 白名单 / E8.1 冲突进审批 / F8.3 dry-run 前置） */
  install: actionProcedure("skill.manage")
    .input(z.object({ skillId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      assertSkillManage(ctx.identity.role);
      try {
        return await installSkill(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          skillId: input.skillId, by: ctx.identity.memberNo,
        });
      } catch (err) {
        if (err instanceof SkillError) {
          throw new TRPCError({ code: err.code === "NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
    }),
  /** 卸载（L8.3 卸载即撤销围栏绑定） */
  uninstall: actionProcedure("skill.manage")
    .input(z.object({ skillId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      assertSkillManage(ctx.identity.role);
      try {
        return await uninstallSkill(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          skillId: input.skillId, by: ctx.identity.memberNo,
        });
      } catch (err) {
        if (err instanceof SkillError) {
          throw new TRPCError({ code: err.code === "NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
    }),
  /** 零代码自定义技能草稿（F8.3 三要素；生成物进版本管理） */
  forge: actionProcedure("skill.manage")
    .input(z.object({
      name: z.string().min(1).max(100),
      description: z.string().max(500).default(""),
      triplet: z.object({ trigger: z.string().min(1), steps: z.array(z.string().min(1)).min(1), boundary: z.string().min(1) }),
      fenceBindings: z.array(z.string()).optional(),
    }))
    .mutation(async ({ ctx, input }) => {
      assertSkillManage(ctx.identity.role);
      return createSkillDraft(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), { ...input, by: ctx.identity.memberNo });
    }),
  /** 生效前 dry-run 预览（F8.3/F2.5：回放最近 10 条） */
  dryRun: actionProcedure("skill.manage")
    .input(z.object({ skillId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      try {
        return await dryRunSkill(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          skillId: input.skillId, by: ctx.identity.memberNo,
        });
      } catch (err) {
        if (err instanceof SkillError) {
          throw new TRPCError({ code: "NOT_FOUND", message: err.message });
        }
        throw err;
      }
    }),
  awareness: router({
    /** 高频相似任务检测（F8.4：≥3 次/周建议固化；E8.3 驳回校准） */
    suggestions: protectedProcedure.query(async ({ ctx }) => {
      return detectSuggestions(getAppPool(), scopeOf(ctx.identity));
    }),
    /** 一键确认 → 生成触发器或新技能（F8.4） */
    confirm: actionProcedure("skill.manage")
      .input(z.object({
        suggestion: z.object({
          key: z.string(), objectType: z.string(), actionCategory: z.string(),
          count: z.number(), windowDays: z.number(), threshold: z.number(), sampleEventIds: z.array(z.string()),
        }),
        target: z.enum(["trigger", "skill"]),
        schedule: z.string().optional(),
      }))
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        return confirmSuggestion(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
          suggestion: input.suggestion, target: input.target, schedule: input.schedule, by: ctx.identity.memberNo,
        });
      }),
    /** 驳回建议（E8.3 校准闭环：该类阈值 ×2） */
    reject: actionProcedure("skill.manage")
      .input(z.object({ key: z.string(), reason: z.string().max(200).optional() }))
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        return { eventId: await rejectSuggestion(getGatewayPool(), scopeOf(ctx.identity), { ...input, by: ctx.identity.memberNo }) };
      }),
  }),
  skillOps: router({
    /** 分发状态投影（技能中心：staging 列表 / 静默策略 / 同步游标） */
    status: protectedProcedure.query(async ({ ctx }) => {
      return distStatus(getAppPool(), scopeOf(ctx.identity));
    }),
    /** 立即同步（手动触发=拉取通道同路径；夜班窗口自动同步复用本函数） */
    syncNow: actionProcedure("skill.manage")
      .input(z.object({ registryUrl: z.string().url().optional() }).optional())
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        const scope = scopeOf(ctx.identity);
        const instance = await instanceProfileOf(scope);
        try {
          return await syncDistribution(getAppPool(), getGatewayPool(), scope, {
            registryUrl: input?.registryUrl ?? process.env.SKILL_DIST_REGISTRY_URL ?? "",
            signingKey: process.env.SKILL_DIST_SIGNING_KEY ?? "",
            instance,
            by: ctx.identity.memberNo,
          });
        } catch (err) {
          throw mapSkillOpsError(err);
        }
      }),
    /** 分发策略（silent=L0/L1 默认静默 / prompt=提示后升级；autoSync=夜班自动同步总开关；L2 不可配置永远审批） */
    setPolicy: actionProcedure("skill.manage")
      .input(z.object({ mode: z.enum(["silent", "prompt"]).optional(), autoSync: z.boolean().optional() }))
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        try {
          return await setSilentMode(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
            mode: input.mode, autoSync: input.autoSync, by: ctx.identity.memberNo,
          });
        } catch (err) {
          throw mapSkillOpsError(err);
        }
      }),
    /** 人工装载 staging 项（prompt 策略项 / L2 审批通过项——审批未过服务端拒绝） */
    loadStaging: actionProcedure("skill.manage")
      .input(z.object({ stagingId: z.string() }))
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        try {
          return await loadStaging(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
            stagingId: input.stagingId, by: ctx.identity.memberNo,
          });
        } catch (err) {
          throw mapSkillOpsError(err);
        }
      }),
    /** 一键回滚（恢复装载前快照：skills 行 + install 快照同事务恢复） */
    rollback: actionProcedure("skill.manage")
      .input(z.object({ skillId: z.string() }))
      .mutation(async ({ ctx, input }) => {
        assertSkillManage(ctx.identity.role);
        try {
          return await rollbackSkill(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
            skillId: input.skillId, by: ctx.identity.memberNo,
          });
        } catch (err) {
          throw mapSkillOpsError(err);
        }
      }),
    /** 上行回流（D19 四条红线：opt-in / 预览即所发 / 脱敏管道 / 发送留痕） */
    reflux: router({
      /** opt-in 状态查询（默认关） */
      optIn: protectedProcedure.query(async ({ ctx }) => {
        return { optIn: await getRefluxOptIn(getAppPool(), scopeOf(ctx.identity)) };
      }),
      /** opt-in 开关（客户治理主权，变更留痕） */
      setOptIn: actionProcedure("skill.manage")
        .input(z.object({ optIn: z.boolean() }))
        .mutation(async ({ ctx, input }) => {
          assertSkillManage(ctx.identity.role);
          try {
            return await setRefluxOptIn(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
              optIn: input.optIn, by: ctx.identity.memberNo,
            });
          } catch (err) {
            throw mapRefluxError(err);
          }
        }),
      /** 预览（预览即所发：返回脱敏后完整上送包 + 六信号摘要，可编辑后放弃） */
      preview: protectedProcedure
        .input(z.object({ skillId: z.string() }))
        .query(async ({ ctx, input }) => {
          try {
            return await previewReflux(getAppPool(), scopeOf(ctx.identity), input.skillId);
          } catch (err) {
            throw mapRefluxError(err);
          }
        }),
      /** 发送（opt-in 未开启拒发；未配端点留 outbox；发送行为留痕） */
      send: actionProcedure("skill.manage")
        .input(z.object({ skillId: z.string() }))
        .mutation(async ({ ctx, input }) => {
          assertSkillManage(ctx.identity.role);
          try {
            return await sendReflux(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
              skillId: input.skillId, by: ctx.identity.memberNo,
            });
          } catch (err) {
            throw mapRefluxError(err);
          }
        }),
    }),
    /** 官方运营台（仅 SKILL_OPS_MODE=official 部署启用；客户端调用一律 403） */
    console: router({
      health: protectedProcedure.query(async ({ ctx }) => {
        assertOfficialMode(ctx.identity.role);
        return consoleHealth(getAppPool());
      }),
      inbox: protectedProcedure.query(async ({ ctx }) => {
        assertOfficialMode(ctx.identity.role);
        return listInbox(getAppPool());
      }),
      review: actionProcedure("skill.manage")
        .input(z.object({ draftId: z.string(), gesture: z.enum(["approve", "reject"]), reason: z.string().max(200).optional() }))
        .mutation(async ({ ctx, input }) => {
          assertOfficialMode(ctx.identity.role);
          try {
            return await reviewRefluxDraft(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
              draftId: input.draftId, by: ctx.identity.memberNo, gesture: input.gesture, reason: input.reason,
            });
          } catch (err) {
            throw mapConsoleError(err);
          }
        }),
      /** 官方化（须双人复核通过且执行人为复核成员之一；可附抽象完善终稿） */
      officialize: actionProcedure("skill.manage")
        .input(z.object({
          draftId: z.string(),
          final: z.object({ name: z.string().optional(), description: z.string().optional(), body: z.string().optional() }).optional(),
        }))
        .mutation(async ({ ctx, input }) => {
          assertOfficialMode(ctx.identity.role);
          try {
            return await officializeDraft(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), {
              draftId: input.draftId, by: ctx.identity.memberNo, final: input.final,
            });
          } catch (err) {
            throw mapConsoleError(err);
          }
        }),
      /** 构建签名 manifest（官方技能库 → 分发包；GET /skill-dist/manifest.json 同逻辑对外服务） */
      buildManifest: actionProcedure("skill.manage").mutation(async ({ ctx }) => {
        assertOfficialMode(ctx.identity.role);
        const key = process.env.SKILL_DIST_SIGNING_KEY ?? "";
        if (!key) throw new TRPCError({ code: "PRECONDITION_FAILED", message: "未配置 SKILL_DIST_SIGNING_KEY" });
        return buildManifest(getAppPool(), { signingKey: key });
      }),
    }),
  }),
});

/** 官方运营台模式守卫（SKILL_OPS_MODE=official + 管理角色；客户端实例一律 403） */
function assertOfficialMode(role: string): void {
  assertSkillManage(role);
  if (process.env.SKILL_OPS_MODE !== "official") {
    throw new TRPCError({ code: "FORBIDDEN", message: "本实例非官方运营台部署（SKILL_OPS_MODE≠official），console 端点禁用" });
  }
}

function mapRefluxError(err: unknown): Error {
  if (err instanceof RefluxError) {
    const code = err.code === "NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST";
    return new TRPCError({ code, message: err.message });
  }
  return err instanceof Error ? err : new Error(String(err));
}

function mapConsoleError(err: unknown): Error {
  if (err instanceof ConsoleError) {
    const code = err.code === "NOT_FOUND" ? "NOT_FOUND" : "BAD_REQUEST";
    return new TRPCError({ code, message: err.message });
  }
  return err instanceof Error ? err : new Error(String(err));
}

/** 本实例定向标签（投放匹配面：workspaces.industry 即已装配行业 Bundle；edition 走 env，默认 community） */
async function instanceProfileOf(scope: { tenantId: string; workspaceId: string }): Promise<InstanceProfile> {
  const client = await getAppPool().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
    const r = await client.query<{ industry: string | null }>(`SELECT industry FROM workspaces WHERE id=$1`, [scope.workspaceId]);
    await client.query("COMMIT");
    return {
      bundles: r.rows[0]?.industry ? [r.rows[0].industry] : [],
      edition: process.env.SKILL_DIST_EDITION ?? "community",
    };
  } catch (err) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw err;
  } finally { client.release(); }
}

function mapSkillOpsError(err: unknown): Error {
  if (err instanceof SkillOpsError) {
    const code = err.code === "NOT_FOUND" || err.code === "NO_SNAPSHOT" ? "NOT_FOUND" : "BAD_REQUEST";
    return new TRPCError({ code, message: err.message });
  }
  return err instanceof Error ? err : new Error(String(err));
}

/** workspace router（F3 起 P1 右栏数据源：一店一档投影 + 人机混编在线成员） */
const workspaceRouter = router({
  /** 一店一档投影（档案 chips：property/audience/history_curve 等；L7.1 越权空） */
  profile: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
      const p = await client.query<{ archive: Record<string, unknown> }>(
        `SELECT archive FROM profiles WHERE workspace_id=$1`, [scope.workspaceId],
      );
      const w = await client.query<{ stage: string | null; name: string }>(
        `SELECT stage, name FROM workspaces WHERE id=$1`, [scope.workspaceId],
      );
      return { archive: p.rows[0]?.archive ?? {}, stage: w.rows[0]?.stage ?? null, name: w.rows[0]?.name ?? "" };
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),
  /** 人机混编在线成员（P1E6：Agent 夜班窗口内自动上线 M4；状态来自 agents.status） */
  agents: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const r = await client.query<{
        id: string; preset_key: string; name: string; version: string; kind: string;
        readonly: boolean; status: string; meta: { night_shift?: boolean };
      }>(
        `SELECT id, preset_key, name, version, kind, readonly, status, meta
         FROM agents WHERE workspace_id=$1 ORDER BY preset_key`,
        [scope.workspaceId],
      );
      return r.rows;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),
});

/** nightShift router（F3 起 P1 数据源：夜班状态胶囊 + 昨夜战报卡投影） */
const nightShiftRouter = router({
  /** 18:00 候选清单（F4.1：夜班 preset 覆盖过滤 + 谷时价 + 围栏摘要；E1 联调挂端点） */
  candidates: protectedProcedure.query(async ({ ctx }) => {
    return buildCandidateList(getAppPool(), scopeOf(ctx.identity));
  }),

  /** 开启夜班（F4.1 人类命令·不经模型轮次；ensureReady→confirmNight：围栏快照 F2.6 + 状态机 F4.8） */
  start: capabilityActionProcedure("nightShift", "night.manage")
    .input(z.object({
      runDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
      candidateIds: z.array(z.string()).default([]),
    }))
    .mutation(async ({ ctx, input }) => {
      if (ctx.identity.role === "readonly") {
        throw new TRPCError({ code: "FORBIDDEN", message: "readonly 角色无权开启夜班（E2.6/L3.4，服务端 403）" });
      }
      const scope = scopeOf(ctx.identity);
      const runId = await ensureReady(getAppPool(), getGatewayPool(), scope, input.runDate);
      try {
        await confirmNight(getAppPool(), getGatewayPool(), scope, runId, ctx.identity.memberNo, input.candidateIds);
      } catch (err) {
        if (err instanceof NightTransitionError) {
          throw new TRPCError({ code: "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
      return { runId, status: "running" as const };
    }),

  /** 08:30 决策包投递（F4.4 三段投影；状态机 → package_generated，统计回写 night_runs.stats） */
  deliver: capabilityActionProcedure("nightShift", "night.manage")
    .input(z.object({
      runId: z.string(),
      window: z.object({ from: z.string(), to: z.string() }),
    }))
    .mutation(async ({ ctx, input }) => {
      return deliverPackage(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input.runId, input.window);
    }),

  /** 最近班次 + 状态机投影（F4.8）+ 决策包统计（F4.4，deliverPackage 回写的 stats） */
  current: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const r = await client.query<{
        id: string; status: string; run_date: string; fence_snapshot_version: string | null;
        candidate_count: number; started_at: Date | null;
        stats: { done: number; pending: number; need_human: number; credits_used: number } | null;
      }>(
        `SELECT id, status, run_date, fence_snapshot_version, candidate_count, started_at, stats
         FROM night_runs WHERE workspace_id=$1 ORDER BY run_date DESC LIMIT 1`,
        [scope.workspaceId],
      );
      const row = r.rows[0];
      if (!row) return { configured: false as const };
      return {
        configured: true as const,
        run: {
          id: row.id, status: row.status, runDate: row.run_date,
          fenceSnapshot: row.fence_snapshot_version, candidateCount: row.candidate_count,
          startedAt: row.started_at?.toISOString() ?? null, stats: row.stats,
        },
      };
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),

  /** 班组消息流（P9E1：夜班频道事件流投影，ts 升序；夜班动作 100% 过围栏 L4.1） */
  events: protectedProcedure
    .input(z.object({ limit: z.number().min(1).max(200).default(80) }).optional())
    .query(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const r = await client.query<{ payload: unknown }>(
          `SELECT payload FROM biz_events
           WHERE workspace_id=$1 AND payload->'context'->>'channel' = '夜班'
           ORDER BY seq DESC LIMIT $2`,
          [scope.workspaceId, input?.limit ?? 80],
        );
        return r.rows.map((x) => x.payload).reverse(); // ts 升序
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        await client.query("COMMIT").catch(() => undefined);
        client.release();
      }
    }),

  /** 一键暂停（P9E2：二次确认在组件层；G5 端到端计时留痕；超时 P0 升级 E4.1） */
  pause: capabilityActionProcedure("nightShift", "night.manage")
    .input(z.object({ runId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      try {
        return await pauseAll(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input.runId, {
          memberNo: ctx.identity.memberNo, channel: "inapp",
        });
      } catch (err) {
        if (err instanceof NightTransitionError) {
          throw new TRPCError({ code: "BAD_REQUEST", message: err.message });
        }
        throw err;
      }
    }),

  /** 恢复（E4.2：断点续跑由 runtime replay 保证） */
  resume: capabilityActionProcedure("nightShift", "night.manage")
    .input(z.object({ runId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      await resumeNight(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input.runId, ctx.identity.memberNo);
      return { ok: true };
    }),

  /** 班组留言（P9E6：人给班组留言=五元事件留痕；触发的动作照常过围栏 L4.1/L4.4） */
  note: capabilityActionProcedure("nightShift", "night.manage")
    .input(z.object({ text: z.string().min(1).max(500) }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const r = await gatewayAppend(getGatewayPool(), {
        ...scope, actor: { id: ctx.identity.memberNo, type: "human" },
      }, {
        who: { type: "human", id: ctx.identity.memberNo },
        context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString(), channel: "夜班" },
        object: { type: "store", id: scope.workspaceId },
        decision: { action: "night.note", after: { text: input.text } },
        rule_impact: [],
      });
      return { eventId: r.eventId };
    }),
});

/** fence router（F8 起 P5 数据源：规则版本化投影 + 30 天触发聚合 + dry-run 生命周期 F2.4/F2.5） */
const fenceRouter = router({
  /** 规则列表（P5E2：级别 pill + 来源 + 30 天触发数；基线 🔒 集团强制 F2.3） */
  rules: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
      const r = await client.query(
        `SELECT f.id, f.rule_id, f.version, f.workspace_id, f.name, f.level, f.match_spec,
                f.is_baseline, f.status, f.created_by, f.created_at,
                (SELECT count(*) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.created_at > now() - interval '30 days'
                    AND EXISTS (SELECT 1 FROM jsonb_array_elements(e.payload->'rule_impact') ri
                                WHERE ri->>'rule_id' = f.rule_id)) AS hits30
         FROM fence_rules f
         WHERE (f.workspace_id=$1 OR f.workspace_id='*') AND f.status IN ('active','pending_approval','draft')
         ORDER BY f.rule_id, f.created_at DESC`,
        [scope.workspaceId],
      );
      return r.rows;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),

  /** 版本历史（P5E1：active/rolled_back/出厂基线 🔒；单调守卫 L2.1） */
  versions: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const r = await client.query(
        `SELECT version, status, count(*) AS rules, min(created_at) AS created_at
         FROM fence_rules WHERE (workspace_id=$1 OR workspace_id='*')
         GROUP BY version, status ORDER BY min(created_at) DESC`,
        [scope.workspaceId],
      );
      return r.rows;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),

  /** NL 新增群规 dry-run（P5E3/P5E4：候选规则回放最近 10 条 F2.5；未确认不生效 L2.4） */
  dryRun: actionProcedure("guardrail.manage")
    .input(fenceCandidateSchema)
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      return ownedScopedTransaction(scope, async (client) => {
      await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fence-edit:${scope.workspaceId}:${input.ruleId}`]);
      const currentRules = await loadActiveRulesInTx(client, scope);
      const created = await createDryRun(getAppPool(), scope, {
        ruleId: input.ruleId,
        ruleVersion: "v-next",
        rules: [{
          rule_id: input.ruleId, version: "v-next", name: input.name, level: input.level,
          is_baseline: false, objectTypes: input.objectTypes, actions: input.actions, when: input.when,
        }],
        defaultLevel: "review",
        createdBy: ctx.identity.memberNo,
        baseline: { rules: currentRules, defaultLevel: "review" },
      });
      const report = { ...created.report, candidate: input, candidateSha256: fenceCandidateHash(input),
        baselineSha256: createHash("sha256").update(stableStringify(currentRules)).digest("hex") };
      const written = await client.query("UPDATE fence_dry_runs SET report=$3::jsonb WHERE id=$1 AND workspace_id=$2 AND status='pending'",
        [created.dryRunId, scope.workspaceId, JSON.stringify(report)]);
      if (written.rowCount !== 1) throw new TRPCError({ code: "CONFLICT", message: "dry-run 候选绑定写入失败，请重新回放" });
      return { ...created, report };
      });
    }),

  /** 确认 dry-run（人看过报告才激活 L2.4）→ 规则进 pending_approval + 变更审批（F2.4，走 P4 决断流） */
  confirmDryRun: actionProcedure("guardrail.manage")
    .input(z.object({
      dryRunId: z.string(),
      rule: fenceCandidateSchema,
      /**
       * MC-103：基线 when 改写的显式放行位。when 语义无法静态证明不放严，默认一律拒绝；
       * 客户在 dry-run 回放 + 人工确认（L2.4）后显式传 true，放行事实写进提案事件（H-3 留痕）。
       */
      allowWhenChange: z.boolean().optional(),
    }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      // 规则草稿进 pending_approval（激活须审批事件 ID，activateRuleVersion 在 P4 手势后调用——E1 已接线，见下方 decide/batchApprove）
      // D16（#1/A）：规则草稿行、提案事件、审批行三者同一事务同一 COMMIT
      const app = getAppPool();
      const client = await app.connect();
      let ev: { eventId: string };
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fence-edit:${scope.workspaceId}:${input.rule.ruleId}`]);
        // MC-103：dry-run 必须属于本次提案的同一条规则——此前只按 id 确认，可以把任意一条
        // 自己名下的 dry-run 拿来给另一条规则的提案背书（回放证据与提案内容脱钩）。
        const drRow = await client.query<{ rule_id: string; status: string; report: { candidateSha256?: string; baselineSha256?: string } }>(
          `SELECT rule_id, status,report FROM fence_dry_runs WHERE id=$1 AND workspace_id=$2 FOR UPDATE`,
          [input.dryRunId, scope.workspaceId],
        );
        const dryRun = drRow.rows[0];
        if (!dryRun) {
          throw new TRPCError({ code: "BAD_REQUEST", message: `dry-run ${input.dryRunId} 不存在或不属于当前工作区` });
        }
        if (dryRun.rule_id !== input.rule.ruleId) {
          throw new TRPCError({
            code: "BAD_REQUEST",
            message: `dry-run ${input.dryRunId} 回放的是规则 ${dryRun.rule_id}，与本次提案 ${input.rule.ruleId} 不一致（禁止借用他条规则的确认）`,
          });
        }
        if (dryRun.status !== "pending") {
          throw new TRPCError({ code: "BAD_REQUEST", message: `dry-run ${input.dryRunId} 状态为 ${dryRun.status}，仅 pending 可确认` });
        }
        const candidateSha256 = fenceCandidateHash(input.rule);
        if (dryRun.report?.candidateSha256 !== candidateSha256) {
          throw new TRPCError({ code: "BAD_REQUEST", message: "提案与已回放候选正文不一致，请按当前候选重新回放" });
        }
        // HP-02：提案入口即做基线单调守卫——同 rule_id 的基线规则只可加严
        //（level 不降 / when 不变 / 覆盖集不收窄）。修复前该守卫只在测试里被调用，
        // 工作区可以把一条 block 基线规则"升级"成 review/恒假条件。
        // MC-109：锚点取同 rule_id 最严 active 行（含客户覆盖行），首次自定义后仍然生效。
        const activeBaseline = await loadActiveRulesInTx(client, scope);
        if (dryRun.report?.baselineSha256 !== createHash("sha256").update(stableStringify(activeBaseline)).digest("hex")) {
          throw new TRPCError({ code: "BAD_REQUEST", message: "现行围栏在回放后已变化，请重新回放再确认" });
        }
        const guard = checkCandidateAgainstBaseline(activeBaseline, {
          rule_id: input.rule.ruleId, version: "v-next", name: input.rule.name,
          level: input.rule.level, is_baseline: false,
          objectTypes: input.rule.objectTypes, actions: input.rule.actions, when: input.rule.when,
        }, { allowWhenChange: input.allowWhenChange === true });
        if (!guard.ok) {
          throw new TRPCError({
            code: "BAD_REQUEST",
            message: `围栏基线只可加严，本次变更被拒：${guard.violations.map((v) => v.reason).join("；")}`,
          });
        }
        // MC-102：行 ID 带版本号（v<该 rule_id 历史最大 + 1>），同 rule_id 的第 N 次提案落在新行上。
        // ON CONFLICT 只覆盖"并发提案先占了同一版本号"这一种情况——重算版本重试，绝不静默丢弃提案内容
        //（修复前固定 vnext 后缀 + DO NOTHING：第二次提案审批通过但规则行从未更新）。
        let identity: RuleRowIdentity | null = null;
        for (let attempt = 0; attempt < 5 && !identity; attempt += 1) {
          const candidate = await nextRuleRowIdentity(client, scope, input.rule.ruleId);
          const inserted = await client.query(
            `INSERT INTO fence_rules (id, rule_id, version, workspace_id, name, level, match_spec, action, is_baseline, status, created_by)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,false,'pending_approval',$9)
             ON CONFLICT (id) DO NOTHING`,
            [candidate.rowId, input.rule.ruleId, candidate.version, scope.workspaceId, input.rule.name, input.rule.level,
             JSON.stringify({ object_types: input.rule.objectTypes, actions: input.rule.actions, when: input.rule.when }),
             JSON.stringify({ result: input.rule.level }), ctx.identity.memberNo],
          );
          if (inserted.rowCount === 1) identity = candidate;
        }
        if (!identity) {
          throw new TRPCError({
            code: "CONFLICT",
            message: `规则 ${input.rule.ruleId} 的提案行版本连续冲突，未落地；请重试（未写入任何变更）`,
          });
        }
        // dry-run 确认与提案行/事件/审批行同一事务：被守卫拒绝的提案不消耗 pending 态，
        // 客户修正后可用同一 dryRunId 重试（修复前先确认后守卫，拒一次就再也确认不了）。
        await confirmDryRunOnTx(client, scope, input.dryRunId);
        ev = await gatewayAppendOnClient(client, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" },
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString(), channel: "inapp" },
          object: { type: "staff", id: input.rule.ruleId },
          // 提案事件携带本行 ID/版本与基线锚点事实：审批通过后按此行激活（MC-102），
          // allowWhenChange 放行事实一并留痕（MC-103/H-3）。
          decision: {
            action: "fence.rule.propose",
            after: {
              ...input.rule,
              candidate_sha256: candidateSha256,
              dryRunId: input.dryRunId,
              ruleRowId: identity.rowId,
              version: identity.version,
              inheritedBaseline: identity.inheritedBaseline,
              ...(input.allowWhenChange === true ? { allowWhenChange: true } : {}),
            },
          },
          rule_impact: [],
        });
        // E1 联调接线（PF.5/F2.4）：围栏变更提案进 P4 决断队列——高危（不可批量采纳，须逐条手势，F5.4/G6）
        // 幂等：UNIQUE(event_id, channel) 冲突丢弃（L5.3 同口径）
        await client.query(
          `INSERT INTO approvals (approval_id, tenant_id, workspace_id, event_id, channel, status, snapshot)
           VALUES ($1,$2,$3,$4,'inapp','pending',$5)
           ON CONFLICT (event_id, channel) DO NOTHING`,
          [`apr-${ev.eventId.toLowerCase()}`, scope.tenantId, scope.workspaceId, ev.eventId,
           JSON.stringify({ after: { ...input.rule, dryRunId: input.dryRunId }, candidate_sha256: candidateSha256,
             ruleRowId: identity.rowId, version: identity.version, high_risk: true })],
        );
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
      return { proposed: true, eventId: ev!.eventId };
    }),
});

/**
 * roster router（F9 起 P8 船员名册数据源：PRD P8-⑤ 数据来源逐条落地）
 *  - 成员列表 = 工作区成员（members）+ Agent preset 注册表投影（agents）
 *  - 工时统计 = 事件库聚合投影（动作数/采纳率/积分/峰谷占比，L6.3 账单=事件投影同口径）
 *  - 事件流 = 该 Agent 的 who.id 过滤投影（append-only 库只读）
 *  - LV/段位为游戏化界面叙事（规则手册 §3：本版不设数值门槛公式），由真实战绩确定性推导，不改业务机制
 *  - 本页无直接写入；「发消息·派遣」走 threads.dispatch（F3.1）；加装 preset 走 P7（§2.3）
 */
/** 游戏化展示层映射（界面叙事；输入全部为真实战绩聚合，确定性、零编造） */
function gameOf(xp: number): { level: number; rank: "青铜" | "白银" | "黄金" | "铂金" | "星钻"; xp: number; xpFloor: number; xpNext: number } {
  // level 阶梯：xp ≥ 8·LV² 升级（展示层自定映射，手册 §3 不定义公式）；LV.1 无门槛（floor=0）
  let level = 1;
  while (xp >= 8 * (level + 1) * (level + 1)) level += 1;
  const rank = level >= 15 ? "星钻" : level >= 10 ? "铂金" : level >= 6 ? "黄金" : level >= 3 ? "白银" : "青铜";
  return { level, rank, xp, xpFloor: level === 1 ? 0 : 8 * level * level, xpNext: 8 * (level + 1) * (level + 1) };
}

/** 夜班窗口判断（M4：22:00–08:00 内 night_shift Agent 自动上线；以服务器本地时区计，演示口径） */
function inNightWindow(now = new Date()): boolean {
  const h = now.getHours();
  return h >= 22 || h < 8;
}

const rosterRouter = router({
  /** 名册总览（p8 默认态：人类 3 + Agent 7 混编 + 30 天工时聚合 + 在线状态） */
  list: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);

      // 人类成员 + 近 24h 活动信号推导在线（事件留痕为唯一事实源，不伪造 presence）
      const humans = await client.query<{
        member_no: string; name: string; role: string;
        active24h: string; decided30: string; dispatched30: string; rules30: string;
      }>(
        `SELECT m.member_no, m.name, m.role,
                (SELECT count(*) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = m.member_no
                    AND e.created_at > now() - interval '24 hours') AS active24h,
                (SELECT count(*) FROM approvals a
                  WHERE a.workspace_id=$1 AND a.decided_by = m.member_no
                    AND a.decided_at > now() - interval '30 days') AS decided30,
                (SELECT count(*) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = m.member_no
                    AND e.payload->'decision'->>'action' = 'thread.dispatch'
                    AND e.created_at > now() - interval '30 days') AS dispatched30,
                (SELECT count(*) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = m.member_no
                    AND e.payload->'decision'->>'action' IN ('fence.rule.propose','skill.forge','awareness.confirm')
                    AND e.created_at > now() - interval '30 days') AS rules30
         FROM members m WHERE m.workspace_id=$1 ORDER BY m.member_no`,
        [scope.workspaceId],
      );

      // Agent 成员 + 30 天工时聚合（L6.3：动作数/采纳/驳回/积分/峰谷占比全部事件投影）
      const agents = await client.query<{
        id: string; preset_key: string; name: string; version: string; kind: string;
        readonly: boolean; status: string; invalid_reason: string | null;
        fence_bindings: string[]; skills: string[];
        meta: { night_shift?: boolean; high_risk?: boolean; description?: string };
        actions30: string; adopted30: string; rejected30: string; credits30: string; offpeak30: string;
      }>(
        `SELECT a.id, a.preset_key, a.name, a.version, a.kind, a.readonly, a.status, a.invalid_reason,
                a.fence_bindings, a.skills, a.meta,
                (SELECT count(*) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = a.preset_key
                    AND e.created_at > now() - interval '30 days') AS actions30,
                (SELECT count(*) FROM approvals ap JOIN biz_events e ON e.event_id = ap.event_id
                  WHERE ap.workspace_id=$1 AND e.workspace_id=$1
                    AND e.payload->'who'->>'id' = a.preset_key
                    AND ap.status IN ('approved','edited')
                    AND ap.created_at > now() - interval '30 days') AS adopted30,
                (SELECT count(*) FROM approvals ap JOIN biz_events e ON e.event_id = ap.event_id
                  WHERE ap.workspace_id=$1 AND e.workspace_id=$1
                    AND e.payload->'who'->>'id' = a.preset_key
                    AND ap.status = 'rejected'
                    AND ap.created_at > now() - interval '30 days') AS rejected30,
                (SELECT COALESCE(sum((e.payload->'model_trace'->>'credits')::numeric), 0) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = a.preset_key
                    AND e.created_at > now() - interval '30 days') AS credits30,
                (SELECT COALESCE(sum((e.payload->'model_trace'->>'credits')::numeric)
                        FILTER (WHERE e.payload->'model_trace'->>'window' = 'off-peak'), 0) FROM biz_events e
                  WHERE e.workspace_id=$1 AND e.payload->'who'->>'id' = a.preset_key
                    AND e.created_at > now() - interval '30 days') AS offpeak30
         FROM agents a WHERE a.workspace_id=$1 ORDER BY a.preset_key`,
        [scope.workspaceId],
      );

      const nightNow = inNightWindow();
      return {
        nightWindow: { open: nightNow, range: "22:00–08:00" }, // M4 夜班窗口（PRD P8 页头口径）
        humans: humans.rows.map((h) => {
          const decided = Number(h.decided30), dispatched = Number(h.dispatched30), rules = Number(h.rules30);
          // 主理人 XP：裁决 ×3 + 派遣 ×2 + 沉淀 ×5（手册 §3.1 人只有三件事：供给/裁决/沉淀；权重为展示层映射）
          const xp = decided * 3 + dispatched * 2 + rules * 5;
          return {
            memberNo: h.member_no, name: h.name, role: h.role,
            online: Number(h.active24h) > 0,
            stats: { decided30: decided, dispatched30: dispatched, settled30: rules },
            game: gameOf(xp),
          };
        }),
        agents: agents.rows.map((a) => {
          const actions = Number(a.actions30), adopted = Number(a.adopted30), rejected = Number(a.rejected30);
          const credits = Number(a.credits30), offpeak = Number(a.offpeak30);
          const decided = adopted + rejected;
          // 船员 XP：动作 ×2 + 积分 ×1（展示层映射，输入均为 L6.3 事件投影）
          const xp = actions * 2 + credits;
          return {
            id: a.id, presetKey: a.preset_key, name: a.name, version: a.version, kind: a.kind,
            readonly: a.readonly, status: a.status, invalidReason: a.invalid_reason,
            fenceBindings: a.fence_bindings, skills: a.skills,
            nightShift: a.meta?.night_shift === true, highRisk: a.meta?.high_risk === true,
            description: a.meta?.description ?? "",
            // M4：夜班 preset 窗口内自动上线；其余待命；invalid=校验失败（F2.10 错误态）
            online: a.status === "ready" && a.meta?.night_shift === true && nightNow,
            stats: {
              actions30: actions, adopted30: adopted, rejected30: rejected,
              adoptionRate: decided > 0 ? adopted / decided : null,
              credits30: credits,
              offPeakRatio: credits > 0 ? offpeak / credits : null, // G9 峰谷投影
            },
            game: gameOf(xp),
          };
        }),
      };
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      await client.query("COMMIT").catch(() => undefined);
      client.release();
    }
  }),

  /** 成员档案（p8_agent：身份与归属 / 围栏授权 F2.10 / 技能包 / 30 天战绩 L6.3 / 最近事件流） */
  profile: protectedProcedure
    .input(z.object({ agentId: z.string() }))
    .query(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);

        const ar = await client.query<{
          id: string; preset_key: string; name: string; version: string; kind: string;
          readonly: boolean; status: string; invalid_reason: string | null;
          fence_bindings: string[]; skills: string[];
          meta: {
            night_shift?: boolean; high_risk?: boolean; description?: string;
            tools?: Array<{ name: string; access: string; desc: string }>;
            write_back?: string[]; prompt?: { constraints?: string[] };
          };
        }>(
          `SELECT id, preset_key, name, version, kind, readonly, status, invalid_reason, fence_bindings, skills, meta
           FROM agents WHERE workspace_id=$1 AND id=$2`,
          [scope.workspaceId, input.agentId],
        );
        const agent = ar.rows[0];
        if (!agent) return null; // L7.1：越权/不存在一律返回空

        const ws = await client.query<{ name: string; bundle_id: string | null; is_example: boolean }>(
        `SELECT name, bundle_id, is_example FROM workspaces WHERE id=$1`, [scope.workspaceId]);

        // 航道许可：fence_bindings 逐条对账 fence_rules 当前 active 版本（缺规则=声明悬空，标红 F2.10）
        const fences = agent.fence_bindings.length === 0 ? [] : (await client.query<{
          rule_id: string; name: string; level: string; version: string; is_baseline: boolean;
        }>(
          `SELECT DISTINCT ON (rule_id) rule_id, name, level, version, is_baseline
           FROM fence_rules
           WHERE (workspace_id=$1 OR workspace_id='*') AND status='active' AND rule_id = ANY($2)
           ORDER BY rule_id, created_at DESC`,
          [scope.workspaceId, agent.fence_bindings],
        )).rows;

        // 技能包：preset 声明 skills × 技能注册表 × 本工作区安装态（F8.2 安装即绑定）
        const skillRows = agent.skills.length === 0 ? [] : (await client.query<{
          id: string; name: string; level: string; version: string; fence_bindings: string[]; installed: boolean;
        }>(
          // preset 声明为短名（revenue-manager），注册表主键带 skill- 前缀——两种形态都匹配
          `SELECT s.id, s.name, s.level, s.version, s.fence_bindings,
                  EXISTS(SELECT 1 FROM skill_installs si WHERE si.skill_id=s.id AND si.workspace_id=$1) AS installed
           FROM skills s
           WHERE s.id = ANY($2) OR s.id = ANY(ARRAY(SELECT 'skill-' || x FROM unnest($2::text[]) AS x))
           ORDER BY s.id`,
          [scope.workspaceId, agent.skills],
        )).rows;

        // 30 天战绩（L6.3 事件投影；驳回原因回流偏好记忆 F1.7 由 review-console 负责）
        const st = await client.query<{
          actions30: string; adopted30: string; rejected30: string; credits30: string; offpeak30: string;
        }>(
          `SELECT
             (SELECT count(*) FROM biz_events e WHERE e.workspace_id=$1 AND e.payload->'who'->>'id'=$2
               AND e.created_at > now() - interval '30 days') AS actions30,
             (SELECT count(*) FROM approvals ap JOIN biz_events e ON e.event_id=ap.event_id
               WHERE ap.workspace_id=$1 AND e.payload->'who'->>'id'=$2 AND ap.status IN ('approved','edited')
               AND ap.created_at > now() - interval '30 days') AS adopted30,
             (SELECT count(*) FROM approvals ap JOIN biz_events e ON e.event_id=ap.event_id
               WHERE ap.workspace_id=$1 AND e.payload->'who'->>'id'=$2 AND ap.status='rejected'
               AND ap.created_at > now() - interval '30 days') AS rejected30,
             (SELECT COALESCE(sum((e.payload->'model_trace'->>'credits')::numeric),0) FROM biz_events e
               WHERE e.workspace_id=$1 AND e.payload->'who'->>'id'=$2
               AND e.created_at > now() - interval '30 days') AS credits30,
             (SELECT COALESCE(sum((e.payload->'model_trace'->>'credits')::numeric)
                     FILTER (WHERE e.payload->'model_trace'->>'window'='off-peak'),0) FROM biz_events e
               WHERE e.workspace_id=$1 AND e.payload->'who'->>'id'=$2
               AND e.created_at > now() - interval '30 days') AS offpeak30`,
          [scope.workspaceId, agent.preset_key],
        );
        const s = st.rows[0]!;
        const actions = Number(s.actions30), adopted = Number(s.adopted30), rejected = Number(s.rejected30);
        const credits = Number(s.credits30), offpeak = Number(s.offpeak30);

        // 最近动作事件流（P8E5：who.id 过滤投影，ts 倒序取 12 条；点击进线程 → P2）
        const ev = await client.query<{
          event_id: string; session_id: string | null; created_at: Date; payload: {
            decision?: { action?: string };
            object?: { type?: string; id?: string };
            rule_impact?: Array<{ rule_id: string; result: string }>;
            receipt?: { synced?: boolean };
          };
        }>(
          `SELECT event_id, session_id, created_at, payload FROM biz_events
           WHERE workspace_id=$1 AND payload->'who'->>'id'=$2
           ORDER BY seq DESC LIMIT 12`,
          [scope.workspaceId, agent.preset_key],
        );

        return {
          agent: {
            id: agent.id, presetKey: agent.preset_key, name: agent.name, version: agent.version,
            kind: agent.kind, readonly: agent.readonly, status: agent.status, invalidReason: agent.invalid_reason,
            description: agent.meta?.description ?? "",
            nightShift: agent.meta?.night_shift === true, highRisk: agent.meta?.high_risk === true,
            tools: agent.meta?.tools ?? [], writeBack: agent.meta?.write_back ?? [],
            constraints: agent.meta?.prompt?.constraints ?? [],
          },
          workspaceName: ws.rows[0]?.name ?? "",
          bundle: ws.rows[0]?.bundle_id ?? null,
          nightWindow: { open: inNightWindow(), range: "22:00–08:00" },
          fences: agent.fence_bindings.map((ruleId) => {
            const hit = fences.find((f) => f.rule_id === ruleId);
            return hit
              ? { ruleId, name: hit.name, level: hit.level, version: hit.version, isBaseline: hit.is_baseline, declared: true as const }
              : { ruleId, declared: false as const }; // 声明悬空：preset 声明了但规则不存在 → 标红
          }),
          skills: skillRows,
          stats: {
            actions30: actions, adopted30: adopted, rejected30: rejected,
            adoptionRate: adopted + rejected > 0 ? adopted / (adopted + rejected) : null,
            credits30: credits, offPeakRatio: credits > 0 ? offpeak / credits : null,
          },
          game: gameOf(actions * 2 + credits),
          events: ev.rows.map((e) => ({
            eventId: e.event_id,
            sessionId: e.session_id,
            time: e.created_at.toISOString(),
            action: e.payload.decision?.action ?? "",
            objectType: e.payload.object?.type ?? "",
            ruleResults: (e.payload.rule_impact ?? []).map((r) => `${r.rule_id}:${r.result}`),
            receiptSynced: e.payload.receipt?.synced === true, // 无回执标未核实（E3.7）
          })),
        };
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        await client.query("COMMIT").catch(() => undefined);
        client.release();
      }
    }),
});

/** im router（B11/D14：IM 通道域 tRPC 薄壳——通道注册表/入站/审批卡片出站/手势回调）
 *  Mock 驱动默认（D4 同纪律：无真实凭据全流程可跑）；真实通道凭据在 dsh 设置页配置（dsh-im，D14），
 *  凭据永不经事件明文（L7.3）。server 层只做装配与错误映射，纪律全部内聚在 packages/base/im-channels。 */
function imDriverKind(): "mock" | "unavailable" { return (process.env.IM_DRIVER ?? "mock") === "mock" ? "mock" : "unavailable"; }
const imDrivers = new Map<ApprovalChannel, MockChannelDriver>();
function mockDriverFor(channel: ApprovalChannel): MockChannelDriver {
  if (imDriverKind() !== "mock") throw new TRPCError({ code: "PRECONDITION_FAILED", message: "当前部署未接通此即时通信驱动，未发送或回写任何消息" });
  let d = imDrivers.get(channel);
  if (!d) {
    d = new MockChannelDriver(channel);
    imDrivers.set(channel, d);
  }
  return d;
}
/** 通道域错误 → tRPC 映射：身份未映射=403（E5.2 无权审批）；其余通道错误=400 */
function imRethrow(err: unknown): never {
  if (err instanceof ChannelError) {
    throw new TRPCError({
      code: err.code === "IDENTITY_UNMAPPED" ? "FORBIDDEN" : "BAD_REQUEST",
      message: err.message,
    });
  }
  if (err instanceof ApprovalError) {
    throw new TRPCError({ code: "BAD_REQUEST", message: err.message });
  }
  throw err;
}
/** P0-1：im.inbound 服务间密钥校验（dsh-im → server 内网调用面）
 *  env IM_BRIDGE_KEY 已配置：x-workloom-key 头必须匹配，否则 401；
 *  缺省（开发）：占位放行并 console.warn（生产必须配置，与 SERVICE_C_DEMO_AUTH 同纪律）。 */
function assertBridgeKey(headers: Headers): void {
  const key = process.env.IM_BRIDGE_KEY;
  if (!key) {
    if (process.env.NODE_ENV === "production") throw new TRPCError({ code: "UNAUTHORIZED", message: "即时通信服务身份未配置，入站失败关闭" });
    console.warn("[im] 开发态即时通信桥未配置服务身份；仅用于明确标记的本地联调");
    return;
  }
  const received = Buffer.from(headers.get("x-workloom-key") ?? "", "utf8");
  const expected = Buffer.from(key, "utf8");
  if (received.length !== expected.length || !timingSafeEqual(received, expected)) {
    throw new TRPCError({ code: "UNAUTHORIZED", message: "即时通信服务身份验证失败" });
  }
}
const imRouter = router({
  /** 通道注册表 + 驱动状态（P 设置页/联调用） */
  channels: protectedProcedure.query(() => ({
    driver: imDriverKind(), available: imDriverKind() === "mock", demo: imDriverKind() === "mock",
    channels: listChannels(),
  })),
  /** 入站 webhook（dsh-im 归一化后注入；幂等+PII 脱敏+openid 映射内聚在服务层） */
  inbound: writeProcedure
    .input(
      z.object({
        channel: z.enum(["inapp", "dingtalk", "wecom", "feishu"]),
        channelMsgId: z.string().min(1),
        conversationId: z.string().min(1),
        kind: z.enum(["direct", "group"]),
        senderOpenId: z.string().min(1),
        text: z.string().min(1).max(2000),
        sentAt: z.string().optional(),
      }),
    )
    .mutation(async ({ ctx, input }) => {
      assertBridgeKey(ctx.headers); // P0-1：服务间密钥（缺省 dev 占位 warn）
      try {
        return await ingestInbound(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input);
      } catch (err) {
        imRethrow(err);
      }
    }),
  /** 审批卡片出站（F5.5 IM 卡片多通道；仅 pending 可发，出站留痕 approval.card.sent） */
  sendApprovalCard: actionProcedure("approval.decide")
    .input(
      z.object({
        approvalId: z.string().min(1),
        channel: z.enum(["dingtalk", "wecom", "feishu"]),
        conversationId: z.string().min(1),
      }),
    )
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const client = await getAppPool().connect();
      try {
        // 事务级 RLS 上下文必须在显式事务内设置：autocommit 下 set_config(...,true) 语句结束即失效
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const r = await client.query<{
          approval_id: string;
          event_id: string;
          snapshot: { expires_at?: string } | null;
          payload: unknown;
        }>(
          `SELECT a.approval_id, a.event_id, a.snapshot, e.payload
             FROM approvals a JOIN biz_events e ON e.event_id = a.event_id
            WHERE a.approval_id = $1 AND a.status = 'pending'`,
          [input.approvalId],
        );
        const row = r.rows[0];
        if (!row) {
          throw new TRPCError({ code: "NOT_FOUND", message: `审批单 ${input.approvalId} 不存在或已决（L7.1 越权返回空）` });
        }
        const card = composeApprovalCard(row as never);
        const sent = await sendApprovalCard(
          getGatewayPool(),
          scope,
          mockDriverFor(input.channel),
          { conversationId: input.conversationId },
          card,
          ctx.identity.memberNo,
        );
        return { ...sent, card };
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        imRethrow(err);
      } finally {
        await client.query("COMMIT").catch(() => undefined);
        client.release();
      }
    }),
  /** 手势回调（F5.4 手势回写多通道；decide 内聚 L5.1/L5.2/L5.3/E5.3 全纪律）
   *  P0-1 通道验签：secret 已配置 → x-channel-signature 必须通过；开发缺省 secret 降级为
   *  「仅允许会话成员本人操作」（operatorOpenId 必须等于当前会话成员在该通道绑定的 openid），响应标注 unsigned:true */
  callback: actionProcedure("approval.decide")
    .input(
      z.object({
        channel: z.enum(["dingtalk", "wecom", "feishu"]),
        approvalId: z.string().min(1),
        operatorOpenId: z.string().min(1),
        conversationId: z.string().min(1),
        gesture: z.enum(["approve", "edit", "reject"]),
        reasonEnum: z.string().optional(),
        reasonText: z.string().max(200).optional(),
        editedAfter: z.unknown().optional(),
      }),
    )
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      // P0-1 验签 seam：body = 回调 payload 稳定 JSON（与 dsh-im 桥约定口径）
      const sig = verifyChannelSignature(input.channel, ctx.headers, stableStringify(input));
      if (sig.unsigned && process.env.NODE_ENV === "production") {
        throw new TRPCError({ code: "FORBIDDEN", message: "生产即时通信通道未配置验签身份，审批回调失败关闭" });
      }
      if (!sig.verified) {
        if (!sig.unsigned) {
          // secret 已配置但验签失败（缺头/超时/比对不一致）→ 一律拒绝，不落本人降级
          throw new TRPCError({ code: "FORBIDDEN", message: `通道签名验证失败：${sig.reason}` });
        }
        // 开发降级（缺省 secret）：仅允许会话成员本人操作——冒名他人 openid 在此被拒
        const bound = await boundOpenidOfMember(getAppPool(), scope, input.channel, ctx.identity.memberNo);
        if (!bound || bound !== input.operatorOpenId) {
          throw new TRPCError({
            code: "FORBIDDEN",
            message: `未验签通道回调仅允许本人操作：operatorOpenId 须为当前会话成员 ${ctx.identity.memberNo} 在 ${input.channel} 绑定的 openid（P0-1 开发降级）`,
          });
        }
      }
      try {
        const r = await handleGestureCallback(
          getAppPool(),
          getGatewayPool(),
          scope,
          input,
          mockDriverFor(input.channel),
        );
        if (r.status === "approved") await activateFenceRuleAfterApproval(scope, r.approvalId, r.operator);
        if (["approved", "edited", "rejected"].includes(r.status)) await applyKbPublishAfterApproval(scope, r.approvalId);
        return { ...r, unsigned: sig.unsigned };
      } catch (err) {
        imRethrow(err);
      }
    }),
  /** mock 出站盒检视（IM_DRIVER=mock 联调/演示用；真实驱动下为空） */
  outbox: protectedProcedure
    .input(z.object({ channel: z.enum(["dingtalk", "wecom", "feishu"]) }))
    .query(({ input }) => ({
      driver: imDriverKind(),
      outbox: imDriverKind() === "mock" ? imDrivers.get(input.channel)?.outbox ?? [] : [],
    })),
});

/** bundles router（F11：P7 舰船换装坞——行业装配台 §2.2/§2.3；校验 F2.10/L1.6；权限 E2.6）
 *  数据来源（P7-⑤）：槽位=bundle 注册表实物投影（磁盘扫描）；校验=活算+留痕（biz_events bundle.*） */
function assertBundleManage(role: string): void {
  if (role === "readonly") {
    throw new TRPCError({ code: "FORBIDDEN", message: "readonly 角色无装配管理权限（E2.6，服务端 403）" });
  }
}
function bundleRethrow(err: unknown): never {
  if (err instanceof BundleError) {
    throw new TRPCError({
      code: err.code === "NOT_FOUND" ? "NOT_FOUND"
        : err.code === "ASSEMBLY_CHECK_FAILED" ? "PRECONDITION_FAILED"
        : "BAD_REQUEST",
      message: err.message,
      cause: err.checks ? { checks: err.checks } : undefined,
    });
  }
  throw err;
}

const bundlesRouter = router({
  /** 装配状态投影：全部 profile（注册表扫描）+ 选中 profile 六槽/检查单/班组（默认当前激活） */
  status: protectedProcedure
    .input(z.object({ slug: z.string().optional() }).optional())
    .query(async ({ ctx, input }) => {
      const app = getAppPool();
      const scope = scopeOf(ctx.identity);
      // workspaces 有 RLS：必须在事务内设上下文再查（否则恒 0 行回退默认 industry）
      const ws = await (async () => {
        const c = await app.connect();
        try {
          await c.query("BEGIN");
          await c.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
          await c.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
          const r = await c.query<{ industry: string }>(`SELECT industry FROM workspaces WHERE id=$1`, [scope.workspaceId]);
          await c.query("COMMIT");
          return r;
        } catch (err) {
          await c.query("ROLLBACK").catch(() => undefined);
          throw err;
        } finally {
          c.release();
        }
      })();
      const activeSlug = ws.rows[0]?.industry ?? null;
      const slugs = listProfileSlugs();
      const profiles = [] as Awaited<ReturnType<typeof computeAssembly>>[];
      for (const s of slugs) {
        try {
          profiles.push(await computeAssembly(app, scope, s));
        } catch {
          /* 注册表坏档不拖垮整页（L9.2：跳过并缺席，由校验页显式呈现缺失） */
        }
      }
      const selected = profiles.find((p) => p.slug === (input?.slug ?? activeSlug)) ?? null;
      return { activeSlug, profiles, selected };
    }),
  /** 重跑校验并留痕（P7E3：修复后重跑；记录可查） */
  recheck: actionProcedure("bundle.manage")
    .input(z.object({ slug: z.string() }))
    .mutation(async ({ ctx, input }) => {
      try {
        return await recheckBundle(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input.slug, ctx.identity.memberNo);
      } catch (err) {
        bundleRethrow(err);
      }
    }),
  /** 激活/切换 profile（F2.10：任一校验失败拒绝激活，PRECONDITION_FAILED 带检查单） */
  activate: actionProcedure("bundle.manage")
    .input(z.object({ slug: z.string() }))
    .mutation(async ({ ctx, input }) => {
      assertBundleManage(ctx.identity.role);
      try {
        return await activateBundle(getAppPool(), getGatewayPool(), scopeOf(ctx.identity), input.slug, ctx.identity.memberNo);
      } catch (err) {
        bundleRethrow(err);
      }
    }),
  /** 新建行业 Bundle 五要素向导（P7E5/§2.3：草稿不进分发） */
  createDraft: actionProcedure("bundle.manage")
    .input(z.object({
      slug: z.string(),
      displayName: z.string().min(1),
      version: z.string().min(1),
      changelog: z.string().min(1),
      fenceRef: z.string().min(1),
      ownerMemberNo: z.string().min(1),
    }))
    .mutation(async ({ ctx, input }) => {
      assertBundleManage(ctx.identity.role);
      try {
        return await createBundleDraft(getGatewayPool(), scopeOf(ctx.identity), input, ctx.identity.memberNo);
      } catch (err) {
        bundleRethrow(err);
      }
    }),
});

/**
 * 统一 LLM 调用面（B8/B9）：LLM_PROVIDER 非 mock 且凭据齐备 → 真实模型；
 * 默认 mock 或未配置 → undefined（各链路走确定性兜底，D4 全流程可跑）。
 * 模型出站强制脱敏（L6.2，OpenAiCompatibleProvider 内建不可绕过）。
 * 落地向导契约：写入 LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 四 env 即全链真实化。
 */
let cachedLlmCall: ((prompt: string) => Promise<string>) | null | undefined;
/**
 * 统一 LLM 调用面（v3.0 收口）：带 scope 时经 routedLlmCall 走 routeSmart 全链路
 * （场景表 × 套餐映射 × 降级链 × 真实计量 × model.call 事件留痕）；
 * 无 scope 或装配失败 → 旧轻量路径兜底；mock → undefined（via=rule 确定性兜底）。
 */
function llmCall(scene = "generic", scope?: { tenantId: string; workspaceId: string }): ((prompt: string) => Promise<string>) | undefined {
  if (scope) {
    const routed = routedLlmCall({
      gateway: getGatewayPool(), scope, scene,
      industryResolver: () => workspaceIndustry(scope),
    });
    if (routed) return routed;
  }
  if (cachedLlmCall !== undefined) return cachedLlmCall ?? undefined;
  try {
    if ((process.env.LLM_PROVIDER ?? "mock") === "mock") {
      cachedLlmCall = null;
      return undefined;
    }
    const provider = providerFromEnv(process.env.LLM_MODEL ?? "deepseek-chat");
    cachedLlmCall = async (prompt: string) => {
      const res = await provider.chat([{ role: "user", content: prompt }]);
      return res.text;
    };
    return cachedLlmCall;
  } catch {
    cachedLlmCall = null; // 配置缺失 → 兜底（via=rule 留痕）
    return undefined;
  }
}

/** 工作区行业（bundle 第⑦槽 model-policy.yml 按行业加载；进程级缓存） */
let cachedIndustry: string | null | undefined;
async function workspaceIndustry(scope: { workspaceId: string }): Promise<string | null> {
  if (cachedIndustry !== undefined) return cachedIndustry;
  try {
    const r = await getAppPool().query<{ industry: string | null }>(
      `SELECT industry FROM workspaces WHERE id=$1`, [scope.workspaceId]);
    cachedIndustry = r.rows[0]?.industry ?? null;
  } catch {
    cachedIndustry = null;
  }
  return cachedIndustry;
}

let cachedClassifier: IntentClassifier | null | undefined;
function intentClassifier(scope?: { tenantId: string; workspaceId: string }): IntentClassifier | undefined {
  if (scope) {
    const call = llmCall("intent-classify", scope);
    if (call) return new LlmIntentClassifier(call);
  }
  if (cachedClassifier !== undefined) return cachedClassifier ?? undefined;
  const call = llmCall("intent-classify");
  cachedClassifier = call ? new LlmIntentClassifier(call) : null;
  return cachedClassifier ?? undefined;
}

/**
 * 数字CEO（D21）：治理状态 + 深度授权 + 节拍手动触发 + 董事长队列 + 成绩单。
 * 写操作一律五元事件留痕；mode 守卫在节拍引擎内双保险（§12）。
 */
/** 风险揭示书版本（§12.2 第①步；文本见 docs/CEO-RISK-DISCLOSURE.md） */
const RISK_DISCLOSURE_VERSION = "tiger-research-risk-v1";
/** 深度授权必确认条款（§12.2 第②步，逐条勾选缺一不可） */
const REQUIRED_CLAUSES = ["仅授权独立模拟研究与汇报", "不连接券商或真实资金账户", "参数应用与对外发布须另行逐次审批", "试用到期降级仅汇报", "AI 非法律责任主体·授权人负责复核研究结论"];

/** 宪章读写串行锁（D26 审计#6：grant/transit 为 load→transition→save 读改写，并发互踩会留下 from/to 失真的留痕） */
let charterLock: Promise<unknown> = Promise.resolve();
function withCharterLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = charterLock.then(fn, fn);
  charterLock = run.catch(() => undefined);
  return run;
}

const captainRouter = router({
  /** 治理状态：宪章 + 模式 + 授权信息 + 待审分层 */
  state: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const charter = await loadCharter(app, scope);
    const client = await app.connect();
    let tiers: Record<string, number> = {};
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const t = await client.query<{ tier: string; n: string }>(
        `SELECT tier, count(*)::text AS n FROM approvals WHERE workspace_id=$1 AND status='pending' GROUP BY 1`,
        [scope.workspaceId],
      );
      await client.query("COMMIT");
      tiers = Object.fromEntries(t.rows.map((x) => [x.tier, Number(x.n)]));
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
    return { charter, pendingByTier: tiers, disclosureVersion: RISK_DISCLOSURE_VERSION, requiredClauses: REQUIRED_CLAUSES };
  }),

  /** 深度授权（§12.2）：条款全确认 → disabled → shadow；授权动作五元留痕（法律留痕） */
  grant: capabilityActionProcedure("quest", "workspace.configure")
    .input(z.object({
      clauses: z.array(z.string()),
      autonomy: z.object({
        ranges: z.record(z.string().min(1).max(80), z.object({
          label: z.string().min(1).max(80), lower: z.number().finite(), upper: z.number().finite(), anchor: z.number().finite(),
        }).strict().refine(({ lower, upper, anchor }) => lower <= anchor && anchor <= upper, "区间须满足下限 ≤ 锚点 ≤ 上限")),
        caps: z.record(z.string().min(1).max(80), z.object({ label: z.string().min(1).max(80), limit: z.number().finite().nonnegative() }).strict()),
      }).strict(),
      shadowDays: z.number().int().min(1).max(14).default(3),
      trialDays: z.number().int().min(3).max(30).default(7),
      identityConfirmed: z.boolean(), // §12.2 第⑤步身份核验（演示环境布尔确认）
    }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const missing = REQUIRED_CLAUSES.filter((c) => !input.clauses.includes(c));
      if (missing.length) throw new TRPCError({ code: "BAD_REQUEST", message: `授权条款未全部确认：${missing.join("、")}` });
      if (!input.identityConfirmed) throw new TRPCError({ code: "BAD_REQUEST", message: "未完成身份核验（§12.2 第⑤步）" });
      const app = getAppPool();
      return withCharterLock(async () => {
      // D16（#1/A）：宪章读改写 + 授权事件同一事务同一 COMMIT；
      // 事件先落库取真实 eventId 回填 charter.grant.event_id（替换 E-GRANT-${Date.now()} 假 id）
      const client = await app.connect();
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const row = await client.query<{ archive: Record<string, unknown> }>(
          `SELECT archive FROM profiles WHERE workspace_id=$1 FOR UPDATE`,
          [scope.workspaceId],
        );
        const charter = parseCharter(row.rows[0]?.archive?.charter);
        const grantedAt = new Date().toISOString();
        const next = transition({ ...charter, autonomy: input.autonomy }, {
          kind: "grant",
          grant: {
            event_id: "", granted_by: ctx.identity.memberNo, granted_at: grantedAt,
            disclosure_version: RISK_DISCLOSURE_VERSION, clauses: input.clauses,
            shadow_days: input.shadowDays, trial_days: input.trialDays, trial_ends_at: null, retain_until: null,
          },
        });
        const ev = await gatewayAppendOnClient(client, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `ceo-grant-${scope.workspaceId}`,
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: grantedAt },
          object: { type: "company_ceo", id: scope.workspaceId },
          decision: {
            action: "captain.grant",
            params: { disclosure_version: RISK_DISCLOSURE_VERSION, clauses: input.clauses, autonomy: input.autonomy, shadow_days: input.shadowDays, trial_days: input.trialDays },
            after: { mode: next.mode },
            basis: ["深度授权六步完成：风险揭示/逐项确认/边界设定/试用计划/身份核验/签署", "此记录不可篡改不可删除（§12.2 第⑥步）"],
          },
          rule_impact: [],
          model_trace: { model_id: "human-chairman", tier: "standard" },
        });
        // 真实 eventId 回填宪章（法律留痕锚点：宪章 ↔ 授权事件可互查）
        if (next.grant) next.grant.event_id = ev.eventId;
        await client.query(
          `UPDATE profiles SET archive = jsonb_set(archive, '{charter}', $2::jsonb), updated_at=now() WHERE workspace_id=$1`,
          [scope.workspaceId, JSON.stringify(next)],
        );
        await client.query("COMMIT");
        return { mode: next.mode, grantEventId: ev.eventId };
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
      });
    }),

  /** 治理迁移：advance/expire/keep_long/keep_until/revoke/close（§12.1 状态机） */
  transit: capabilityActionProcedure("quest", "workspace.configure")
    .input(z.object({ kind: z.enum(["advance", "expire", "keep_long", "keep_until", "revoke", "close"]), until: z.string().optional() }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      return withCharterLock(async () => {
      // D16（#1/A）：宪章读改写 + 迁移事件同一事务同一 COMMIT
      const client = await app.connect();
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const row = await client.query<{ archive: Record<string, unknown> }>(
          `SELECT archive FROM profiles WHERE workspace_id=$1 FOR UPDATE`,
          [scope.workspaceId],
        );
        const charter = parseCharter(row.rows[0]?.archive?.charter);
        const t: CeoTransition = input.kind === "keep_until"
          ? { kind: "keep_until", until: input.until ?? new Date(Date.now() + 30 * 86400e3).toISOString() }
          : { kind: input.kind };
        let next;
        try {
          next = transition(charter, t);
        } catch (e) {
          throw new TRPCError({ code: "BAD_REQUEST", message: (e as Error).message });
        }
        await client.query(
          `UPDATE profiles SET archive = jsonb_set(archive, '{charter}', $2::jsonb), updated_at=now() WHERE workspace_id=$1`,
          [scope.workspaceId, JSON.stringify(next)],
        );
        await gatewayAppendOnClient(client, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `ceo-grant-${scope.workspaceId}`,
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
          object: { type: "company_ceo", id: scope.workspaceId },
          decision: {
            action: "captain.mode_change",
            params: { from: charter.mode, to: next.mode, by: ctx.identity.memberNo, kind: input.kind },
            after: { mode: next.mode },
            basis: [`董事长手动迁移：${charter.mode} → ${next.mode}`],
          },
          rule_impact: [],
          model_trace: { model_id: "human-chairman", tier: "standard" },
        });
        await client.query("COMMIT");
        return { mode: next.mode };
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
      });
    }),

  /** 手动触发节拍（演示/调度共用入口）：briefing/queue/deviation/breaker */
  runBeat: capabilityActionProcedure("quest", "workspace.configure")
    .input(z.object({ beat: z.enum(["daily", "weekly", "monthly", "fleet_daily", "queue", "deviation", "breaker", "outcome", "hr", "board", "orgscan", "routerreview"]) }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      switch (input.beat) {
        case "queue": return runQueueBeat(app, scope, { llmCall: llmCall("ceo-decision", scope) });
        case "deviation": return runDeviationBeat(app, scope);
        case "breaker": return runBreakerBeat(app, scope);
        case "outcome": return runOutcomeReviewBeat(app, scope);
        case "hr": return runHrReviewBeat(app, scope, { llmCall: llmCall("hr-replacement", scope) });
        case "board": return runBoardPackBeat(app, scope, { llmCall: llmCall("briefing", scope) });
        case "orgscan": return runOrgScanBeat(app, scope);
        case "routerreview": return runRouterReviewBeat(app, getGatewayPool(), scope);
        default: {
          const kind = input.beat === "fleet_daily" ? "fleet_daily" : input.beat;
          // fleet_daily：单店模型退化为本店晨报口径（方案 §三：编制不空转；多店聚合在 P22 视图层轮询）
          const wsName = kind === "fleet_daily" ? "集团CEO" : undefined;
          const charter = await loadCharter(app, scope);
          const r = await runBriefingBeat(app, scope, kind, { llmCall: llmCall("briefing", scope) });
          // IM 通道推送（方案双通道；charter.briefing.channel=im|both 时推送，mock 驱动留痕）
          let imPushed = false;
          if (r.eventId && !r.skipped && charter.briefing.channel !== "app") {
            const textRow = await app.connect().then(async (c) => {
              try {
                await c.query("BEGIN");
                await c.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
                const q = await c.query<{ payload: Record<string, unknown> }>(`SELECT payload FROM biz_events WHERE event_id=$1`, [r.eventId]);
                await c.query("COMMIT");
                return q.rows[0]?.payload;
              } catch (e) { await c.query("ROLLBACK").catch(() => undefined); throw e; } finally { c.release(); }
            });
            const text = String(((textRow?.decision as Record<string, unknown>)?.after as Record<string, unknown>)?.text ?? "");
            if (text) {
              const driver = new MockChannelDriver("wecom");
              const sent = await driver.sendText({ conversationId: `chairman-${scope.workspaceId}` }, text);
              // 外发口径（D16 例外，先发后写，同 sendApprovalCard）：外发不可撤回，先发送后写事件；
              // 事件写失败补写补偿事件（im.outbound.unrecorded，best-effort 一次），再失败抛错人工对账
              const outboundActor = { id: "im-channels", type: "system" as const };
              const outboundBase = {
                who: { type: "system" as const, id: "im-channels" },
                context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString(), channel: "wecom" },
                object: { type: "conversation", id: `chairman-${scope.workspaceId}` },
                rule_impact: [] as never[],
              };
              try {
                await gatewayAppend(getGatewayPool(), { ...scope, actor: outboundActor }, {
                  ...outboundBase,
                  decision: {
                    action: "im.outbound",
                    params: { channel_msg_id: sent.channelMsgId, ref_event: r.eventId, kind },
                    after: { text: text.slice(0, 500) },
                    basis: [`简报双通道推送（charter.briefing.channel=${charter.briefing.channel}）`],
                  },
                });
              } catch (outErr) {
                console.warn(`[captain] im.outbound 留痕写失败（简报 ${r.eventId} 已外发 ${sent.channelMsgId}），补写补偿事件：`, outErr instanceof Error ? outErr.message : outErr);
                await gatewayAppend(getGatewayPool(), { ...scope, actor: outboundActor }, {
                  ...outboundBase,
                  decision: {
                    action: "im.outbound.unrecorded",
                    params: { channel_msg_id: sent.channelMsgId, ref_event: r.eventId, kind },
                    after: { original_action: "im.outbound", send_error: outErr instanceof Error ? outErr.message : String(outErr) },
                    basis: ["补偿事件：简报已外发但 im.outbound 留痕写失败（外发不可撤回，先发后写口径）"],
                  },
                });
              }
              imPushed = true;
            }
          }
          return { ...r, name: wsName ?? charter.identity.name, imPushed };
        }
      }
    }),

  /** 最近简报（P21 董事长视图数据源） */
  briefings: protectedProcedure
    .input(z.object({ limit: z.number().int().min(1).max(20).default(5) }).optional())
    .query(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        const r = await client.query<{ event_id: string; payload: Record<string, unknown>; created_at: string }>(
          `SELECT event_id, payload, created_at FROM biz_events
           WHERE workspace_id=$1 AND payload->'decision'->>'action' IN ('ceo.briefing','ceo.decision','ceo.circuit_breaker','initiative.launch')
           ORDER BY seq DESC LIMIT $2`,
          [scope.workspaceId, input?.limit ?? 5],
        );
        await client.query("COMMIT");
        return r.rows;
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
    }),

  /** 董事长请示队列（L4 pending + 事件依据链；P21 inline 三手势数据源） */
  chairmanQueue: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const client = await app.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const r = await client.query<{
        approval_id: string; event_id: string; snapshot: Record<string, unknown>; payload: Record<string, unknown>;
      }>(
        `SELECT a.approval_id, a.event_id, a.snapshot, e.payload
         FROM approvals a JOIN biz_events e ON e.event_id = a.event_id AND e.workspace_id = a.workspace_id
         WHERE a.workspace_id=$1 AND a.status='pending' AND a.tier='l4_chairman'
         ORDER BY a.approval_id LIMIT 20`,
        [scope.workspaceId],
      );
      await client.query("COMMIT");
      return r.rows;
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }),

  /** 董事长反馈（赞/踩 → ceo.feedback 事件 + 组织记忆奖励信号）
   *  D16（#1/A）：事件与 org_memory 写入同一事务同一 COMMIT；
   *  记忆写入走 workdata upsertMemoryInTx（内含 maskText 脱敏——修复 note 明文直插的 PII 漏脱敏） */
  feedback: writeProcedure
    .input(z.object({ eventId: z.string(), signal: z.enum(["up", "down"]), note: z.string().max(200).optional() }))
    .mutation(async ({ ctx, input }) => {
      const scope = scopeOf(ctx.identity);
      const app = getAppPool();
      const client = await app.connect();
      try {
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
        const ev = await gatewayAppendOnClient(client, {
          ...scope, actor: { id: ctx.identity.memberNo, type: "human" }, sessionId: `ceo-feedback-${scope.workspaceId}`,
        }, {
          who: { type: "human", id: ctx.identity.memberNo },
          context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString() },
          object: { type: "task", id: input.eventId },
          decision: {
            action: "ceo.feedback",
            params: { ref_event: input.eventId, signal: input.signal, note: input.note ?? "" },
            after: {},
            basis: [`董事长对决策 ${input.eventId} 的${input.signal === "up" ? "点赞" : "点踩"}（入组织记忆，成为后续决策奖励信号）`],
          },
          rule_impact: [],
          model_trace: { model_id: "human-chairman", tier: "standard" },
        });
        // 组织记忆写入（pattern 类：奖励/纠正信号；memoryId 由反馈事件派生可互查；内容经 maskText 脱敏）
        await upsertMemoryInTx(client, scope, {
          memoryId: `mem-fb-${ev.eventId.toLowerCase()}`,
          scope: "workspace",
          kind: "pattern",
          content: `【${ctx.identity.memberNo}】董事长${input.signal === "up" ? "认可" : "否定"}决策 ${input.eventId}${input.note ? `：${input.note}` : ""}`,
          sourceEvents: [ev.eventId, input.eventId],
        }, new MockEmbedder());
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK").catch(() => undefined);
        throw err;
      } finally {
        client.release();
      }
      return { ok: true };
    }),

  /** 经营剧场聚合态（P0 首页：治理态/请示/简报/员工卫星/实况流，5s 心跳数据源） */
  theater: protectedProcedure.query(async ({ ctx }) => {
    const scope = scopeOf(ctx.identity);
    const app = getAppPool();
    const charter = await loadCharter(app, scope);
    const client = await app.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
      const tiers = await client.query<{ tier: string; n: string }>(
        `SELECT tier, count(*)::text AS n FROM approvals WHERE workspace_id=$1 AND status='pending' GROUP BY 1`,
        [scope.workspaceId],
      );
      // 展示层过滤：E2E 测试标记（E2E-*）写入的晨报不返回给界面（哈希链不动、套件断言不受影响——套件直接查库）
      const briefing = await client.query<{ payload: Record<string, unknown>; created_at: string }>(
        `SELECT payload, created_at FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action' IN ('ceo.briefing','ceo.board_pack')
         AND payload->'decision'->'after'->>'text' NOT LIKE '%E2E-%'
         ORDER BY seq DESC LIMIT 1`,
        [scope.workspaceId],
      );
      const agents = await client.query<{ id: string; preset_key: string; name: string; alias: string | null }>(
        // 欢迎仪式必须拿到完整编制；固定 12 会静默裁掉大型 Bundle 的成员。
        `SELECT id, preset_key, name, alias FROM agents WHERE workspace_id=$1 AND status='ready' ORDER BY id LIMIT 64`,
        [scope.workspaceId],
      );
      const grades = await client.query<{ agent_id: string; grade: string }>(
        `SELECT DISTINCT ON (payload->'decision'->'params'->>'agent_id')
           payload->'decision'->'params'->>'agent_id' AS agent_id,
           payload->'decision'->'params'->>'grade' AS grade
         FROM biz_events WHERE workspace_id=$1 AND payload->'decision'->>'action'='hr.review'
         ORDER BY 1, seq DESC`,
        [scope.workspaceId],
      );
      // ticker 同口径过滤测试噪声（E2E 标记与套件 mock 数据不进界面）
      const events = await client.query<{ event_id: string; action: string; who: string; created_at: string }>(
        `SELECT event_id, payload->'decision'->>'action' AS action, payload->'who'->>'id' AS who, created_at
         FROM biz_events WHERE workspace_id=$1
         AND payload->'decision'->'after'->>'text' NOT LIKE '%E2E-%'
         AND payload->'decision'->>'action' NOT LIKE 'test.%'
         ORDER BY seq LIMIT 14`.replace("ORDER BY seq LIMIT", "ORDER BY seq DESC LIMIT"),
        [scope.workspaceId],
      );
      const ind = await client.query<{ industry: string | null }>(
        `SELECT industry FROM profiles WHERE workspace_id=$1`, [scope.workspaceId],
      );
      await client.query("COMMIT");
      const gradeMap = Object.fromEntries(grades.rows.map((g) => [g.agent_id, g.grade]));
      // D25 数字职场：行业场景包 + 员工状态派生（独立聚合，故障不阻塞剧场主数据）
      let floor: unknown = null;
      try {
        floor = await buildFloor(getAppPool(), scope, ind.rows[0]?.industry ?? null);
      } catch { floor = null; }
      return {
        mode: charter.mode,
        ceoName: charter.identity.name,
        pendingByTier: Object.fromEntries(tiers.rows.map((t) => [t.tier, Number(t.n)])),
        latestBriefing: briefing.rows[0]
          ? { text: String(((briefing.rows[0].payload.decision as Record<string, unknown>).after as Record<string, unknown>)?.text ?? ""), at: briefing.rows[0].created_at }
          : null,
        satellites: agents.rows.map((a) => ({ id: a.id, presetKey: a.preset_key, name: a.name, alias: a.alias, grade: gradeMap[a.id] ?? "正常" })),
        ticker: events.rows,
        floor,
      };
    } catch (err) {
      await client.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      client.release();
    }
  }),

  /** 成绩单（方案 §七） */
  scorecard: protectedProcedure.query(async ({ ctx }) => {
    return buildScorecard(getAppPool(), scopeOf(ctx.identity));
  }),
});

/** 组织记忆中心（D24 自我进化飞轮 M2：可读可改可禁用，纠偏与信任通道） */
const memoryRouter = router({
  /** 列表（作用域/种类/状态过滤 + 语义检索可选） */
  list: protectedProcedure
    .input(
      z.object({
        scope: z.enum(["workspace", "agent", "run"]).optional(),
        kind: z.enum(["preference", "pattern", "sop", "forbidden"]).optional(),
        status: z.enum(["active", "superseded", "recalled"]).optional(),
        subjectId: z.string().optional(),
        query: z.string().max(200).optional(),
        limit: z.number().int().min(1).max(50).optional(),
      }).optional(),
    )
    .query(async ({ ctx, input }) => {
      return searchMemories(getAppPool(), scopeOf(ctx.identity), {
        scope: input?.scope, kind: input?.kind, status: input?.status,
        subjectId: input?.subjectId, query: input?.query, limit: input?.limit,
      }, new MockEmbedder());
    }),

  /** 归因反查（验收断言：任一记忆可反查来源事件与被谁引用） */
  sources: protectedProcedure
    .input(z.object({ memoryId: z.string() }))
    .query(async ({ ctx, input }) => {
      return getMemorySources(getAppPool(), scopeOf(ctx.identity), input.memoryId);
    }),

  /** 人类编辑内容（M2.1 可读可改；写 memory.calibrate 事件留痕） */
  update: actionProcedure("memory.manage")
    .input(z.object({ memoryId: z.string(), content: z.string().min(1).max(2000) }))
    .mutation(async ({ ctx, input }) => {
      return editMemoryContent(
        getAppPool(), getGatewayPool(), scopeOf(ctx.identity),
        { memberNo: ctx.identity.memberNo }, input.memoryId, input.content,
      );
    }),

  /** 人类禁用（回收区口径 F1.11；防记忆污染越用越偏） */
  disable: actionProcedure("memory.manage")
    .input(z.object({ memoryId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      return disableMemory(
        getAppPool(), getGatewayPool(), scopeOf(ctx.identity),
        { memberNo: ctx.identity.memberNo }, input.memoryId,
      );
    }),

  /** 停用/来源清算前读取真实影响关系；无引用时返回空数组而非推测。 */
  impact: protectedProcedure
    .input(z.object({
      memoryId: z.string().optional(),
      memberId: z.string().optional(),
    }).refine((input) => Boolean(input.memoryId) !== Boolean(input.memberId), "必须且只能指定一条记忆或一名来源成员"))
    .query(async ({ ctx, input }) => {
      return previewMemoryImpact(getAppPool(), scopeOf(ctx.identity), {
        memoryIds: input.memoryId ? [input.memoryId] : undefined,
        sourceMemberId: input.memberId,
      });
    }),

  /** 回收区单条重新启用，恢复本身写独立校准事件。 */
  reactivate: actionProcedure("memory.manage")
    .input(z.object({ memoryId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      return reactivateMemory(
        getAppPool(), getGatewayPool(), scopeOf(ctx.identity),
        { memberNo: ctx.identity.memberNo }, input.memoryId,
      );
    }),

  /** 撤销最近一批来源清算；只恢复请求中仍处于回收区的本工作区记忆。 */
  restore: actionProcedure("memory.manage")
    .input(z.object({ memoryIds: z.array(z.string()).min(1).max(50) }))
    .mutation(async ({ ctx, input }) => {
      return restoreMemories(
        getAppPool(), getGatewayPool(), scopeOf(ctx.identity),
        { memberNo: ctx.identity.memberNo }, input.memoryIds,
      );
    }),

  /** 来源人一键清算（D24 修订 2：成员离任/换岗，作废其手势沉淀的偏好记忆） */
  recallBySource: actionProcedure("memory.recall")
    .input(z.object({ memberId: z.string() }))
    .mutation(async ({ ctx, input }) => {
      if (ctx.identity.role !== "owner" && ctx.identity.role !== "manager") {
        throw new TRPCError({ code: "FORBIDDEN", message: "当前角色无权清算其他成员来源的组织记忆" });
      }
      return recallMemoriesByMember(
        getAppPool(), getGatewayPool(), scopeOf(ctx.identity),
        { memberNo: ctx.identity.memberNo }, input.memberId,
      );
    }),

  /** 手动触发提炼节拍（演示/联调用；生产由夜班调度触发） */
  mineNow: actionProcedure("memory.manage").mutation(async ({ ctx }) => {
    return runMemoryMinerBeat(getAppPool(), getGatewayPool(), scopeOf(ctx.identity));
  }),

  /** 手动触发衰减扫描（同上） */
  decayNow: actionProcedure("memory.manage").mutation(async ({ ctx }) => {
    return decayMemories(getAppPool(), getGatewayPool(), scopeOf(ctx.identity));
  }),

  /** 本工作区装配的反馈枚举表（Bundle 第⑧槽；审批卡下拉数据源） */
  feedbackEnums: protectedProcedure.query(async ({ ctx }) => {
    return getFeedbackEnums(scopeOf(ctx.identity).workspaceId) ?? [];
  }),
});

/** 进化积分卡（D24 自我进化飞轮 M5：北极星=审批一次通过率，趋势看斜率） */
const evolutionRouter = router({
  scorecard: protectedProcedure.query(async ({ ctx }) => {
    return buildEvolutionScorecard(getAppPool(), scopeOf(ctx.identity));
  }),
});

export const appRouter = router({
  system: systemRouter,
  onboarding: onboardingRouter,
  auth: authRouter,
  access: accessRouter,
  overlay: overlayRouter,
  accounts: accountsRouter,
  members: membersRouter,
  threads: threadsRouter,
  approvals: approvalsRouter,
  inspection: inspectionRouter,
  skills: skillsRouter,
  workspace: workspaceRouter,
  nightShift: nightShiftRouter,
  fence: fenceRouter,
  roster: rosterRouter,
  im: imRouter,
  bundles: bundlesRouter,
  captain: captainRouter,
  service: serviceRouter,
  credits: creditsRouter,
  modelFeedback: modelFeedbackRouter,
  memory: memoryRouter,
  evolution: evolutionRouter,
});

export type AppRouter = typeof appRouter;
/** 上下文类型经 router 入口再导出（前端 AppRouter 类型可移植性，TS2742） */
export type { TrpcContext } from "./context.js";
export type { ExamSummary } from "../service/eval.js";
export type { BundleInstall, StaffingDraft } from "../service/bundle.js";
export type { IntelItem, RepoPulse } from "../service/aipm.js";
