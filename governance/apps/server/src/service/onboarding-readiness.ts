/**
 * 落地向导事实采集：页面与激活事务复用已有 evaluateRealModeReadiness 门禁。
 * 模型资格来自当前工作区的成功试调事件，并绑定完整配置指纹；环境变量本身不是凭据。
 * lock=true 时调用方须已开启 RLS 事务，资格行锁持有到模式变更和事件提交。
 */
import type pg from "pg";
import type { Identity } from "@workloom/base/tenancy";
import { serviceTx } from "./events.js";
import {
  evaluateRealModeReadiness, sha256Canonical, staffingAssemblyHash, staffingDraftHash,
  type ExamBinding, type RealModeFacts,
} from "./onboarding-truth.js";

interface LlmConfiguration { provider: string; baseUrl: string; model: string; apiKey: string }
function currentLlmConfiguration(): LlmConfiguration {
  return {
    provider: process.env.LLM_PROVIDER ?? "mock", baseUrl: process.env.LLM_BASE_URL ?? "",
    model: process.env.LLM_MODEL ?? "", apiKey: process.env.LLM_API_KEY ?? "",
  };
}
export function llmConfigurationFingerprint(config: LlmConfiguration = currentLlmConfiguration()): string {
  return sha256Canonical({ schema: "workloom.llm-probe/v1", ...config });
}
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function string(value: unknown): string { return typeof value === "string" ? value : ""; }
function ids(value: unknown): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.trim()) ? value : [];
}
function matchingIds(actual: string[], expected: string[]): boolean {
  return expected.length > 0 && new Set(expected).size === expected.length
    && JSON.stringify([...actual].sort()) === JSON.stringify([...expected].sort());
}
interface InstallRow {
  id: string; bundle_id: string; assets: unknown; status: string;
  draft_id: string | null; assembly_version: number | null; assembly_hash: string | null; qualified_exam_id: string | null;
}
interface AgentRow extends Record<string, unknown> {
  id: string; preset_key: string; name: string; version: string; kind: string; readonly: boolean;
  fence_bindings: string[]; skills: string[]; meta: Record<string, unknown>; status: string;
}
interface FenceRow extends Record<string, unknown> {
  id: string; rule_id: string; version: string; name: string; level: string;
  match_spec: unknown; action: unknown; is_baseline: boolean; status: string; created_by: string;
}

export async function onboardingStatusOn(client: pg.PoolClient, identity: Identity, lock = false) {
  const workspaceId = identity.workspaceId;
  const lockUpdate = lock ? " FOR UPDATE" : "";
  const lockShare = lock ? " FOR SHARE" : "";
  const workspace = (await client.query<{ name: string; bundle_id: string | null; is_example: boolean }>(
    `SELECT name,bundle_id,is_example FROM workspaces WHERE id=$1${lockUpdate}`, [workspaceId],
  )).rows[0];
  const profile = (await client.query<{ archive: unknown }>(
    `SELECT archive FROM profiles WHERE workspace_id=$1${lockUpdate}`, [workspaceId],
  )).rows[0];
  const archive = record(profile?.archive);
  const rawBusiness = record(archive.business);
  const business = {
    name: string(rawBusiness.name), industry: string(rawBusiness.industry), note: string(rawBusiness.note),
    source: string(rawBusiness.source), configuredAt: string(rawBusiness.configuredAt),
  };
  const config = currentLlmConfiguration();
  const configFingerprint = llmConfigurationFingerprint(config);
  const proof = (await client.query<{ after: unknown }>(
    `SELECT payload->'decision'->'after' AS after FROM biz_events
     WHERE workspace_id=$1 AND tenant_id=$2 AND payload->'decision'->>'action'='onboarding.llm_configured'
     ORDER BY seq DESC LIMIT 1`, [workspaceId, identity.tenantId],
  )).rows[0];
  const verified = record(proof?.after);
  const llmReal = config.provider !== "mock" && Boolean(config.baseUrl.trim() && config.model.trim())
    && verified.probeVerified === true && verified.configFingerprint === configFingerprint;
  const installs = (await client.query<InstallRow>(
    `SELECT id,bundle_id,assets,status,draft_id,assembly_version,assembly_hash,qualified_exam_id
     FROM bundle_installs WHERE workspace_id=$1 AND status='active'${lockShare}`, [workspaceId],
  )).rows;
  const install = installs.length === 1 ? installs[0] : undefined;
  const assets = record(install?.assets);
  const agentIds = ids(assets.preset_ids), fenceIds = ids(assets.fence_rule_ids), skillIds = ids(assets.skill_ids);
  const agents = (await client.query<AgentRow>(
    `SELECT id,preset_key,name,version,kind,readonly,fence_bindings,skills,meta,status
     FROM agents WHERE workspace_id=$1 AND id=ANY($2::text[]) ORDER BY id${lockShare}`, [workspaceId, agentIds],
  )).rows;
  const fences = (await client.query<FenceRow>(
    `SELECT id,rule_id,version,name,level,match_spec,action,is_baseline,status,created_by
     FROM fence_rules WHERE workspace_id=$1 AND id=ANY($2::text[]) ORDER BY id${lockShare}`, [workspaceId, fenceIds],
  )).rows;
  const skills = (await client.query<{ skill_id: string }>(
    `SELECT skill_id FROM skill_installs WHERE workspace_id=$1 AND skill_id=ANY($2::text[])${lockShare}`, [workspaceId, skillIds],
  )).rows;
  const installedSkills = new Set(skills.map((row) => row.skill_id));
  let capabilityClaimsSafe = Array.isArray(assets.skill_ids) && skillIds.length === assets.skill_ids.length
    && new Set(skillIds).size === skillIds.length
    && agents.every((agent) => Array.isArray(agent.skills) && agent.skills.every((skill) => installedSkills.has(skill)));
  let fenceResponsibilityReady = fences.length > 0 && fences.every((fence) => Boolean(fence.created_by?.trim()));
  let customAssembly: RealModeFacts["customAssembly"] = { required: install?.bundle_id === "custom" || workspace?.bundle_id === "custom" };
  if (customAssembly.required && install) {
    const draft = (await client.query<{
      status: string; generation_mode: string; confirmed_by: string | null; draft_hash: string | null;
      industry_text: string; compiled: unknown; assembly_hash: string | null; assembly_version: number | null;
    }>(
      `SELECT status,generation_mode,confirmed_by,draft_hash,industry_text,compiled,assembly_hash,assembly_version
       FROM wizard_staffing_drafts WHERE workspace_id=$1 AND id=$2${lockShare}`, [workspaceId, install.draft_id],
    )).rows[0];
    const exam = (await client.query<{
      status: string; verdict: string | null; assessment_kind: string; target_install_id: string;
      target_draft_id: string; target_version: number; target_hash: string;
    }>(
      `SELECT status,verdict,assessment_kind,target_install_id,target_draft_id,target_version,target_hash
       FROM eval_exams WHERE workspace_id=$1 AND id=$2${lockShare}`, [workspaceId, install.qualified_exam_id],
    )).rows[0];
    const results = (await client.query<{ agent_id: string; passed: boolean; red_line_hit: boolean }>(
      `SELECT agent_id,passed,red_line_hit FROM eval_candidate_results
       WHERE workspace_id=$1 AND exam_id=$2 AND install_id=$3 AND draft_id=$4
         AND assembly_version=$5 AND assembly_hash=$6${lockShare}`,
      [workspaceId, install.qualified_exam_id, install.id, install.draft_id, install.assembly_version, install.assembly_hash],
    )).rows;
    const installBinding: ExamBinding | null = install.draft_id && install.assembly_version && install.assembly_hash
      ? { installId: install.id, draftId: install.draft_id, version: install.assembly_version, hash: install.assembly_hash } : null;
    const candidate = record(assets.candidate);
    const candidateAgents = Array.isArray(candidate.agents) ? candidate.agents : [];
    const candidateFences = Array.isArray(candidate.fences) ? candidate.fences : [];
    const sorted = (rows: unknown[]) => [...rows].sort((a, b) => string(record(a).id).localeCompare(string(record(b).id)));
    const actualAgents = agents.map(({ status: _status, ...agent }) => agent);
    const actualFences = fences.map(({ status: _status, created_by: _createdBy, ...fence }) => fence);
    const materialized = candidateAgents.length > 0 && candidateFences.length > 0
      && sha256Canonical(sorted(candidateAgents)) === sha256Canonical(actualAgents)
      && sha256Canonical(sorted(candidateFences)) === sha256Canonical(actualFences)
      && matchingIds(candidateAgents.map((agent) => string(record(agent).id)), agentIds)
      && matchingIds(candidateFences.map((fence) => string(record(fence).id)), fenceIds);
    const hashMatches = Boolean(installBinding && draft?.draft_hash
      && staffingDraftHash(draft.industry_text, draft.compiled) === draft.draft_hash
      && draft.assembly_hash === install.assembly_hash && draft.assembly_version === install.assembly_version
      && staffingAssemblyHash({ workspaceId, draftId: installBinding.draftId, draftHash: draft.draft_hash,
        version: installBinding.version, assets: install.assets }) === install.assembly_hash);
    capabilityClaimsSafe = capabilityClaimsSafe && materialized && hashMatches;
    fenceResponsibilityReady = fenceResponsibilityReady && Boolean(draft?.confirmed_by)
      && fences.every((fence) => fence.created_by === draft?.confirmed_by);
    customAssembly = {
      required: true, installStatus: install.status,
      draftStatus: draft?.generation_mode === "real" ? draft.status : null,
      examStatus: exam?.status, examVerdict: exam?.verdict, examAssessmentKind: exam?.assessment_kind,
      candidateExpected: agentIds.length, candidateTotal: results.length,
      candidatePassed: matchingIds(results.map((row) => row.agent_id), agentIds)
        ? results.filter((row) => row.passed && !row.red_line_hit).length : 0,
      installBinding, examBinding: exam ? { installId: exam.target_install_id, draftId: exam.target_draft_id, version: exam.target_version, hash: exam.target_hash } : null,
    };
  }
  const facts: RealModeFacts = {
    activationOwner: identity.role === "owner" && identity.memberNo !== "GUEST", llmReal,
    workspaceIsExample: workspace?.is_example ?? true, business,
    activeInstall: Boolean(profile && install && install.bundle_id === workspace?.bundle_id),
    expectedAgents: agentIds.length,
    readyInstalledAgents: agents.filter((agent) => agent.status === "ready").length,
    expectedFences: fenceIds.length,
    activeInstalledFences: fences.filter((fence) => fence.status === "active").length,
    fenceResponsibilityReady, expectedSkills: skillIds.length, installedSkills: skills.length, capabilityClaimsSafe,
    pendingCapabilities: agents.reduce((count, agent) => count + ids(record(agent.meta).suggested_skills).filter((name) => !installedSkills.has(name)).length, 0),
    customAssembly,
  };
  // 历史 real 标签并非资格。启用回执必须绑定当前主体、模型和装配；观察成员的角色
  // 只影响能否再次激活，不能把同一工作区的运行事实变成另一个状态。
  const activationFingerprint = sha256Canonical({
    schema: "workloom.real-mode-activation/v1", workspaceId, business, configFingerprint,
    install: install ?? null,
  });
  const activation = (await client.query<{ after: unknown }>(
    `SELECT payload->'decision'->'after' AS after FROM biz_events
     WHERE workspace_id=$1 AND tenant_id=$2 AND payload->'decision'->>'action'='onboarding.real_mode_activated'
     ORDER BY seq DESC LIMIT 1`, [workspaceId, identity.tenantId],
  )).rows[0];
  const formalActivationRecorded = record(activation?.after).activationFingerprint === activationFingerprint;
  const persistedDataMode = archive.dataMode === "real" ? "real" as const : "simulated" as const;
  const systemReady = evaluateRealModeReadiness({ ...facts, activationOwner: true }).canActivate;
  const counts = (await client.query<{ events: number; members: number; agents: number; memories: number }>(
    `SELECT (SELECT count(*)::int FROM biz_events WHERE workspace_id=$1) AS events,
      (SELECT count(*)::int FROM members WHERE workspace_id=$1) AS members,
      (SELECT count(*)::int FROM agents WHERE workspace_id=$1) AS agents,
      (SELECT count(*)::int FROM org_memory WHERE workspace_id=$1) AS memories`, [workspaceId],
  )).rows[0]!;
  return {
    dataMode: persistedDataMode === "real" && formalActivationRecorded && systemReady ? "real" as const : "simulated" as const,
    persistedDataMode, formalActivationRecorded, activationFingerprint,
    llm: { provider: config.provider, model: config.model, baseUrl: config.baseUrl, real: llmReal },
    business, activationGate: evaluateRealModeReadiness(facts),
    workspace: { name: workspace?.name ?? "", ...counts }, workspaceId,
    bundle: { id: workspace?.bundle_id ?? null, isExample: workspace?.is_example ?? false },
  };
}

export function onboardingStatus(identity: Identity) {
  return serviceTx(identity.workspaceId, async (client) => {
    const { activationFingerprint: _fingerprint, ...status } = await onboardingStatusOn(client, identity);
    return status;
  });
}
