import { afterEach, describe, expect, it, vi } from "vitest";
import { deriveTigerRequest, tigerResearchSchema, tigerScopeKey } from "./tiger-runtime.js";

const scope = { tenantId: "tenant-A", workspaceId: "workspace-A" };
const account = { risk_per_trade_pct: 0.008, max_position_per_ticker_pct: 0.2, gross_cap_pct: 0.9,
  stage: "paper", base_currency: "USD", markets: ["us"] };
const input = { scope, threadId: "T-1001", goal: "扫描美股候选池", presetKey: "universe-scanner",
  stage: "paper", archive: { account, dataMode: "simulated" } };

describe("Tiger-owned 研究请求边界", () => {
  afterEach(() => vi.unstubAllEnvs());
  it("scope-key 同作用域稳定且不同租户/工作区隔离", () => {
    expect(tigerScopeKey(scope)).toMatch(/^[a-f0-9]{40}$/);
    expect(tigerScopeKey(scope)).toBe(tigerScopeKey({ ...scope }));
    expect(tigerScopeKey(scope)).not.toBe(tigerScopeKey({ ...scope, tenantId: "tenant-B" }));
    expect(tigerScopeKey(scope)).not.toBe(tigerScopeKey({ ...scope, workspaceId: "workspace-B" }));
  });
  it("实际 scanner 请求包含确定性 Job ID、simulation 和模型禁用事实", async () => {
    const request = await deriveTigerRequest(input);
    expect(request).toMatchObject({ operation: "employee", employee: "scanner", environment: "simulation",
      market: "us", provider: "demo", llmMode: "disabled" });
    expect(request.idempotencyKey).toMatch(/^quest-[a-f0-9]{64}$/);
    expect(request).toEqual(await deriveTigerRequest(input));
    expect(request.idempotencyKey).not.toBe((await deriveTigerRequest({ ...input, threadId: "T-1002" })).idempotencyKey);
  });
  it("请求不能接路径、tenant、live、参数应用或任意 shell", () => {
    for (const injected of [{ workspace: "/tmp/x" }, { tenant: "evil" }, { environment: "live" },
      { command: "arbitrary" }, { applyParams: true }, { llmMode: "configured" }]) {
      expect(tigerResearchSchema.safeParse(injected).success).toBe(false);
    }
  });
  it("非 paper 阶段、未实现岗位和缺失复盘源会明确拒绝", async () => {
    await expect(deriveTigerRequest({ ...input, stage: "live" })).rejects.toThrow(/paper/);
    await expect(deriveTigerRequest({ ...input, presetKey: "narrative" })).rejects.toThrow(/实际执行/);
    await expect(deriveTigerRequest({ ...input, presetKey: "review-chief" })).rejects.toThrow(/sourceJob/);
  });
  it("调用者给出港元研究账户时绑定币种，币种错配不能悄悄换算", async () => {
    const research = { market: "hk" as const, account: 250000, accountCurrency: "HKD" as const };
    const request = await deriveTigerRequest({ ...input, research });
    expect(request).toMatchObject({ market: "hk", account: 250000 });
    await expect(deriveTigerRequest({ ...input, research: { ...research, accountCurrency: "USD" } })).rejects.toThrow(/币种/);
  });
  it("仅显式部署配置能启用模型，错误配置失败关闭", async () => {
    vi.stubEnv("TIGER_RESEARCH_LLM_MODE", "configured");
    expect(await deriveTigerRequest(input)).toMatchObject({ llmMode: "configured" });
    vi.stubEnv("TIGER_RESEARCH_LLM_MODE", "typo");
    await expect(deriveTigerRequest(input)).rejects.toThrow(/部署/);
  });
});
