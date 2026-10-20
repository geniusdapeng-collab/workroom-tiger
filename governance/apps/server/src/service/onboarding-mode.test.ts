import type pg from "pg";
import type { Identity } from "@workloom/base/tenancy";
import { executeDeclaredTool } from "@workloom/runtime";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { serviceTx } from "./events.js";
import { llmConfigurationFingerprint, onboardingStatus, onboardingStatusOn } from "./onboarding-readiness.js";

vi.mock("./events.js", () => ({ serviceTx: vi.fn() }));

const identity: Identity = {
  memberId: "mc027-member", memberNo: "MEM-MODE", name: "模式契约夹具", role: "owner",
  tenantId: "mc027-tenant", workspaceId: "mc027-workspace", plan: "pro",
};
const business = { name: "模式契约测试主体", industry: "fixture", note: "", source: "user", configuredAt: "2026-10-20T00:00:00Z" };

/** Only database reads are substituted. Qualification, fingerprints and default tool execution are real code. */
function fixture(archive: Record<string, unknown> = {}) {
  const state = {
    workspace: { name: "模式契约夹具", bundle_id: "fixture", is_example: false },
    profile: { archive: { business: { ...business }, ...archive } as unknown },
    installs: [{ id: "mc027-install", bundle_id: "fixture", status: "active", draft_id: null, assembly_version: null,
      assembly_hash: null, qualified_exam_id: null, assets: { preset_ids: ["mc027-agent"], fence_rule_ids: ["mc027-fence"], skill_ids: [] } }],
    agents: [{ id: "mc027-agent", preset_key: "fixture-reader", name: "测试观察员", version: "1", kind: "analyst",
      readonly: true, fence_bindings: ["RF-MODE"], skills: [], meta: {}, status: "ready" }],
    fences: [{ id: "mc027-fence", rule_id: "RF-MODE", version: "1", name: "测试围栏", level: "review",
      match_spec: {}, action: {}, is_baseline: true, status: "active", created_by: identity.memberNo }],
    modelProof: { probeVerified: true, configFingerprint: llmConfigurationFingerprint() },
    activationProof: {} as Record<string, unknown>,
  };
  const query = vi.fn(async (sql: string, params: unknown[]) => {
    expect(sql.trimStart()).toMatch(/^SELECT /);
    expect(params[0]).toBe(identity.workspaceId);
    if (sql.includes("onboarding.llm_configured")) {
      expect(params[1]).toBe(identity.tenantId);
      return { rows: [{ after: state.modelProof }] };
    }
    if (sql.includes("onboarding.real_mode_activated")) {
      expect(params[1]).toBe(identity.tenantId);
      return { rows: [{ after: state.activationProof }] };
    }
    if (sql.startsWith("SELECT (SELECT count")) return { rows: [{ events: 2, agents: 1, members: 1, memories: 0 }] };
    if (sql.includes("FROM workspaces")) return { rows: [state.workspace] };
    if (sql.includes("FROM profiles")) return { rows: [state.profile] };
    if (sql.includes("FROM bundle_installs")) return { rows: state.installs };
    if (sql.includes("FROM agents")) return { rows: state.agents };
    if (sql.includes("FROM fence_rules")) return { rows: state.fences };
    if (sql.includes("FROM skill_installs")) return { rows: [] };
    throw new Error(`Unexpected read in mode fixture: ${sql}`);
  });
  const client = { query } as unknown as pg.PoolClient;
  const read = (viewer = identity, lock = false) => onboardingStatusOn(client, viewer, lock);
  const qualify = async () => {
    state.activationProof = { activationFingerprint: (await read()).activationFingerprint };
  };
  return { state, client, query, read, qualify };
}

beforeEach(() => {
  vi.stubEnv("LLM_PROVIDER", "openai");
  vi.stubEnv("LLM_BASE_URL", "http://127.0.0.1:1/v1");
  vi.stubEnv("LLM_MODEL", "mc027-no-network-fixture");
  vi.stubEnv("LLM_API_KEY", "");
  vi.clearAllMocks();
});
afterEach(() => vi.unstubAllEnvs());

describe("MC027 模式声明与正式启用事实分离", () => {
  const declarations: Array<[string, Record<string, unknown>, string, string]> = [
    ["missing", {}, "unconfigured", "connector-required"],
    ["simulated", { dataMode: "simulated" }, "simulated", "simulated"],
    ["real", { dataMode: "real" }, "real", "connector-required"],
    ["null", { dataMode: null }, "invalid", "connector-required"],
    ["blank", { dataMode: "" }, "invalid", "connector-required"],
    ["case mismatch", { dataMode: "SIMULATED" }, "invalid", "connector-required"],
    ["whitespace", { dataMode: " simulated " }, "invalid", "connector-required"],
    ["boolean", { dataMode: true }, "invalid", "connector-required"],
    ["number", { dataMode: 1 }, "invalid", "connector-required"],
    ["object", { dataMode: { mode: "simulated" } }, "invalid", "connector-required"],
    ["array", { dataMode: ["simulated"] }, "invalid", "connector-required"],
  ];
  it.each(declarations)("%s 声明原样区分，且与通用工具默认执行边界一致", async (_name, archive, declaredDataMode, defaultToolMode) => {
    const f = fixture(archive);
    const before = structuredClone(f.state);
    const status = await f.read();
    expect(status).toMatchObject({ declaredDataMode, defaultToolMode, formalModeActive: false, formalActivationRecorded: false });
    // Retain legacy qualification aliases for existing API consumers; they are not execution evidence.
    expect(status).toMatchObject({ dataMode: "simulated", persistedDataMode: archive.dataMode === "real" ? "real" : "simulated" });
    expect(f.state).toEqual(before);
    const tool = await executeDeclaredTool("fixture.read", {}, { allowedTools: ["fixture.read"], simulated: archive.dataMode === "simulated" });
    expect(tool.result.state).toBe(defaultToolMode);
    expect(tool.receipt).toMatchObject({ synced: defaultToolMode === "simulated", mode: defaultToolMode === "simulated" ? "simulated" : "real" });
  });

  it.each([undefined, null, [], "malformed archive"])("档案容器 %j 不足以证明模拟声明", async (archive) => {
    const f = fixture();
    f.state.profile.archive = archive;
    expect(await f.read()).toMatchObject({ declaredDataMode: "unconfigured", defaultToolMode: "connector-required", formalModeActive: false });
  });

  it("real 档案与有效当前资格并存时才报告正式启用，仍不声称外部执行成功", async () => {
    const f = fixture({ dataMode: "real" });
    await f.qualify();
    expect(await f.read()).toMatchObject({
      dataMode: "real", persistedDataMode: "real", declaredDataMode: "real",
      defaultToolMode: "connector-required", formalModeActive: true, formalActivationRecorded: true,
      activationGate: { canActivate: true },
    });
  });

  it.each([{}, { dataMode: "simulated" }, { dataMode: "invalid" }])("有匹配启用回执也不覆盖当前声明 %j", async (archive) => {
    const f = fixture(archive);
    await f.qualify();
    expect(await f.read()).toMatchObject({ formalActivationRecorded: true, formalModeActive: false, dataMode: "simulated" });
  });

  it.each(["activation-proof", "model-proof", "model-config", "agent", "fence", "business", "example", "install"])(
    "%s 证据失效不会把 real 档案改报为模拟",
    async (kind) => {
      const f = fixture({ dataMode: "real" });
      await f.qualify();
      if (kind === "activation-proof") f.state.activationProof = { activationFingerprint: "stale" };
      if (kind === "model-proof") f.state.modelProof.probeVerified = false;
      if (kind === "model-config") vi.stubEnv("LLM_MODEL", "changed-model-fixture");
      if (kind === "agent") f.state.agents[0]!.status = "disabled";
      if (kind === "fence") f.state.fences[0]!.status = "rolled_back";
      if (kind === "business") (f.state.profile.archive as { business: { source: string } }).business.source = "seed";
      if (kind === "example") f.state.workspace.is_example = true;
      if (kind === "install") f.state.installs[0]!.id = "changed-install";
      expect(await f.read()).toMatchObject({
        declaredDataMode: "real", persistedDataMode: "real", defaultToolMode: "connector-required",
        formalModeActive: false, dataMode: "simulated",
      });
      expect((f.state.profile.archive as { dataMode: string }).dataMode).toBe("real");
    },
  );

  it("观察者的激活权限不同不会改变同一工作区的模式事实", async () => {
    const f = fixture({ dataMode: "real" });
    await f.qualify();
    const owner = await f.read(), viewer = await f.read({ ...identity, role: "readonly" });
    expect(owner.activationGate.canActivate).toBe(true);
    expect(viewer.activationGate.canActivate).toBe(false);
    for (const field of ["declaredDataMode", "defaultToolMode", "formalModeActive", "dataMode", "formalActivationRecorded"] as const) {
      expect(viewer[field]).toBe(owner[field]);
    }
    expect(viewer.formalModeActive).toBe(true);
  });

  it("公开状态包装器保留明确字段并隐藏内部激活指纹", async () => {
    const f = fixture({ dataMode: "real" });
    await f.qualify();
    vi.mocked(serviceTx).mockImplementation(async (workspaceId, callback) => {
      expect(workspaceId).toBe(identity.workspaceId);
      return callback(f.client, { workspaceId, tenantId: identity.tenantId });
    });
    const status = await onboardingStatus(identity);
    expect(status).toMatchObject({ declaredDataMode: "real", defaultToolMode: "connector-required", formalModeActive: true });
    expect(status).not.toHaveProperty("activationFingerprint");
  });

  it("激活事务仍锁定资格行，新增显示事实不发出任何写查询", async () => {
    const f = fixture({ dataMode: "real" });
    await f.read(identity, true);
    expect(f.query.mock.calls.some(([sql]) => sql.includes("FROM profiles") && sql.endsWith("FOR UPDATE"))).toBe(true);
    expect(f.query.mock.calls.some(([sql]) => sql.includes("FROM agents") && sql.endsWith("FOR SHARE"))).toBe(true);
    expect(f.query.mock.calls.every(([sql]) => sql.trimStart().startsWith("SELECT "))).toBe(true);
  });

  it("事实读取失败向上返回错误，不补造模拟或正式状态", async () => {
    const f = fixture();
    f.query.mockRejectedValueOnce(new Error("MC027 injected read failure"));
    await expect(f.read()).rejects.toThrow("MC027 injected read failure");
  });
});
