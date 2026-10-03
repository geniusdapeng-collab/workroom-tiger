/** 模拟治理主体，在显式隔离 PG 中验证授权投影；不接券商、实盘或资金。 */
import pg from "pg";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Identity } from "@workloom/base/tenancy";
import { defaultCharter } from "@workloom/base/captain";
const authority = vi.hoisted(() => vi.fn());
const fault = vi.hoisted(() => ({ profile: false }));
vi.mock("../service/access-authority.js", async (original) => ({
  ...await original<typeof import("../service/access-authority.js")>(), currentMemberAuthority: authority,
}));
vi.mock("@workloom/db", async (original) => {
  const actual = await original<typeof import("@workloom/db")>();
  return { ...actual, getAppPool: (...args: Parameters<typeof actual.getAppPool>) => {
    const pool = actual.getAppPool(...args);
    if (!fault.profile) return pool;
    return new Proxy(pool, { get(target, key) {
      if (key === "connect") return async () => {
        const client = await target.connect();
        return new Proxy(client, { get(connection, property) {
          if (property === "query") return (...queryArgs: unknown[]) => {
            if (fault.profile && typeof queryArgs[0] === "string" && /UPDATE profiles SET archive/.test(queryArgs[0])) {
              throw new Error("模拟宪章写入失败");
            }
            return Reflect.apply(connection.query, connection, queryArgs);
          };
          const value = Reflect.get(connection, property);
          return typeof value === "function" ? value.bind(connection) : value;
        } });
      };
      const value = Reflect.get(target, key);
      return typeof value === "function" ? value.bind(target) : value;
    } });
  } };
});
import { appRouter } from "./router.js";
import { closeAllPools } from "@workloom/db";

const RUN_DB = process.env.RUN_DB_TESTS === "1" && Boolean(process.env.DATABASE_URL) && Boolean(process.env.DATABASE_APP_URL);
describe.runIf(RUN_DB)("Tiger 宪章输入真实数据库回归（模拟主体）", () => {
  const owner = new pg.Pool({ connectionString: process.env.DATABASE_URL });
  const suffix = `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
  const tenantId = `tenant-grant-${suffix}`, workspaceId = `ws-grant-${suffix}`;
  const identity: Identity = { kind: "member", memberId: `mem-${suffix}`, memberNo: "MEM-001", name: "模拟负责人", role: "owner", plan: "pro", tenantId, workspaceId };
  const context = { session: identity, identity, partnerIdentity: null, headers: new Headers() };
  const caller = appRouter.createCaller(context);
  const autonomy = { ranges: { simulated: { label: "模拟区间", lower: 0.8, upper: 1.2, anchor: 1 } }, caps: { simulated: { label: "模拟上限", limit: 5 } } };
  let clauses: string[];
  const input = () => ({ clauses, autonomy, identityConfirmed: true, shadowDays: 3, trialDays: 7 });
  beforeAll(async () => {
    authority.mockImplementation(async (current: Identity) => ({ identity: current, permissions: {} }));
    await owner.query(`INSERT INTO tenants (id,name,plan) VALUES ($1,'模拟宪章租户','pro')`, [tenantId]);
    await owner.query(`INSERT INTO workspaces (id,tenant_id,name,slug,industry) VALUES ($1,$2,'模拟宪章工作区',$1,'general')`, [workspaceId, tenantId]);
    await owner.query(`INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'general',$3)`, [workspaceId, tenantId, JSON.stringify({ charter: defaultCharter() })]);
    clauses = (await caller.captain.state()).requiredClauses;
  });
  beforeEach(async () => {
    await owner.query(`UPDATE profiles SET archive=$2 WHERE workspace_id=$1`, [workspaceId, JSON.stringify({ charter: defaultCharter() })]);
  });
  afterAll(async () => {
    await closeAllPools();
    await owner.end();
  });
  async function charter() {
    const rows = await owner.query(`SELECT archive FROM profiles WHERE workspace_id=$1`, [workspaceId]);
    return rows.rows[0].archive.charter;
  }
  it("当前 ranges/caps 契约保存原边界与真实授权事件 ID，保持 shadow", async () => {
    const result = await caller.captain.grant(input());
    expect(result.mode).toBe("shadow");
    const saved = await charter();
    expect(saved.autonomy).toEqual(autonomy);
    expect(saved.grant).toMatchObject({ event_id: result.grantEventId, granted_by: identity.memberNo });
    const event = await owner.query(`SELECT payload FROM biz_events WHERE tenant_id=$1 AND event_id=$2`, [tenantId, result.grantEventId]);
    expect(event.rows[0].payload.decision.params.autonomy).toEqual(autonomy);
  });
  it("旧未知字段被拒，不能被静默剥离成空边界", async () => {
    await expect(caller.captain.grant({ ...input(), autonomy: { price_band: [0.8, 1.2], procurement_cap: 5, campaign_cap: 5 } } as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect((await charter()).mode).toBe("disabled");
  });
  it.each([
    ["倒置区间", { ranges: { simulated: { label: "模拟", lower: 2, upper: 1, anchor: 1 } }, caps: {} }],
    ["锚点超界", { ranges: { simulated: { label: "模拟", lower: 0.8, upper: 1.2, anchor: 2 } }, caps: {} }],
    ["负上限", { ranges: {}, caps: { simulated: { label: "模拟", limit: -1 } } }],
  ])("%s 在状态写入前拒绝", async (_label, invalid) => {
    await expect(caller.captain.grant({ ...input(), autonomy: invalid })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect((await charter()).mode).toBe("disabled");
  });
  it("未完成条款或身份确认不能授权", async () => {
    await expect(caller.captain.grant({ ...input(), clauses: [] })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    await expect(caller.captain.grant({ ...input(), identityConfirmed: false })).rejects.toMatchObject({ code: "BAD_REQUEST" });
    expect((await charter()).mode).toBe("disabled");
  });
  it("写入投影失败时授权事件与宪章一起回滚", async () => {
    const count = await owner.query(`SELECT count(*)::int AS n FROM biz_events WHERE workspace_id=$1`, [workspaceId]);
    fault.profile = true;
    try {
      await expect(caller.captain.grant(input())).rejects.toThrow("模拟宪章写入失败");
      expect((await charter()).mode).toBe("disabled");
      const after = await owner.query(`SELECT count(*)::int AS n FROM biz_events WHERE workspace_id=$1`, [workspaceId]);
      expect(after.rows[0].n).toBe(count.rows[0].n);
    } finally {
      fault.profile = false;
    }
  });
  it("只读成员不能授权", async () => {
    const readonly = { ...identity, role: "readonly" as const };
    const restricted = appRouter.createCaller({ ...context, session: readonly, identity: readonly });
    await expect(restricted.captain.grant(input())).rejects.toMatchObject({ code: "FORBIDDEN" });
    expect((await charter()).mode).toBe("disabled");
  });
});
