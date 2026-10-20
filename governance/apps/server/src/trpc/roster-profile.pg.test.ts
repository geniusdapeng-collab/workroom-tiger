/**
 * MC-030: actual roster.profile tRPC calls against PostgreSQL with application RLS.
 * Opt in only on a migrated loopback workloom_tiger_* disposable database.
 * Fixtures are synthetic and scoped by fresh IDs; no product seed or ledger is changed.
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { closeAllPools, getAppPool, getGatewayPool } from "@workloom/db";
import type { Identity } from "@workloom/base/tenancy";
import { fixtureDatabaseUrls } from "../test-fixtures/governance.js";

const suite = process.env.RUN_ROSTER_PROFILE_PG_TESTS === "1" ? describe : describe.skip;
const uid = () => randomUUID().replaceAll("-", "");
type Fixture = { identity: Identity; agentId: string; workspaceName: string };
let appRouter: typeof import("./router.js").appRouter;

function caller(identity: Identity | null) {
  return appRouter.createCaller({ identity, session: identity, partnerIdentity: null, headers: new Headers() });
}

suite("MC-030 roster profile bundle identity", () => {
  let owner: pg.Pool;
  const fetchAttempt = vi.fn(async () => { throw new Error("MC030 forbids external HTTP requests"); });

  async function fixture(bundleId: string | null, tenant?: string): Promise<Fixture> {
    const tenantId = tenant ?? `mc030-tenant-${uid()}`;
    const workspaceId = `mc030-workspace-${uid()}`;
    const memberId = `mc030-member-${uid()}`;
    const agentId = `mc030-agent-${uid()}`;
    const workspaceName = `MC030 synthetic workspace ${uid()}`;
    const client = await owner.connect();
    try {
      await client.query("BEGIN");
      if (!tenant) await client.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'MC030 synthetic tenant','pro')", [tenantId]);
      await client.query(
        "INSERT INTO workspaces (id,tenant_id,name,slug,industry,bundle_id,is_example) VALUES ($1,$2,$3,$1,'test',$4,false)",
        [workspaceId, tenantId, workspaceName, bundleId],
      );
      await client.query(
        "INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,'MC030-MEMBER','MC030 synthetic member','owner')",
        [memberId, workspaceId],
      );
      await client.query(
        `INSERT INTO agents (id,workspace_id,preset_key,name,version,kind,readonly,meta)
         VALUES ($1,$2,'mc030-reader','MC030 synthetic agent','fixture-v1','agent',true,$3)`,
        [agentId, workspaceId, JSON.stringify({ description: "MC030 synthetic read-only agent", high_risk: true })],
      );
      await client.query("COMMIT");
    } catch (error) {
      try {
        await client.query("ROLLBACK");
      } catch (rollbackError) {
        throw new AggregateError([error, rollbackError], "MC030 fixture failed and rollback was not confirmed");
      }
      throw error;
    } finally {
      client.release();
    }
    return {
      identity: { memberId, memberNo: "MC030-MEMBER", name: "MC030 synthetic member", role: "owner", tenantId, workspaceId, plan: "pro" },
      agentId, workspaceName,
    };
  }

  beforeAll(async () => {
    const urls = fixtureDatabaseUrls();
    vi.stubEnv("LLM_PROVIDER", "mock");
    vi.stubEnv("IM_DRIVER", "mock");
    vi.stubEnv("ASK_WEB_SEARCH", "0");
    vi.stubGlobal("fetch", fetchAttempt);
    owner = new pg.Pool({ connectionString: urls.owner, connectionTimeoutMillis: 5000, statement_timeout: 10000 });
    for (const [pool, role] of [[getAppPool(), "workloom_app"], [getGatewayPool(), "workloom_gateway"]] as const) {
      expect((await pool.query("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")).rows[0])
        .toEqual({ current_user: role, rolsuper: false, rolbypassrls: false });
    }
    appRouter = (await import("./router.js")).appRouter;
  });

  afterEach(() => {
    expect(fetchAttempt).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });

  afterAll(async () => {
    try {
      await Promise.all([owner?.end(), closeAllPools()]);
    } finally {
      vi.unstubAllGlobals();
      vi.unstubAllEnvs();
    }
  });

  it.each(["trading", "custom", "mc030-other-bundle", "MC030-'\"%组合"])(
    "projects the current workspace's %s identity without replacing it with a product default",
    async (bundleId) => {
      const subject = await fixture(bundleId);
      expect(await caller(subject.identity).roster.profile({ agentId: subject.agentId })).toMatchObject({
        bundle: bundleId,
        workspaceName: subject.workspaceName,
        agent: {
          id: subject.agentId, presetKey: "mc030-reader", readonly: true, highRisk: true,
          version: "fixture-v1", tools: [], writeBack: [], constraints: [],
        },
        fences: [], skills: [], events: [],
        stats: { actions30: 0, adopted30: 0, rejected30: 0, credits30: 0, adoptionRate: null, offPeakRatio: null },
      });
    },
  );

  it.each([null, "", " \t "])("explicitly reports an unconfigured source for %j", async (bundleId) => {
    const subject = await fixture(bundleId);
    expect(await caller(subject.identity).roster.profile({ agentId: subject.agentId }))
      .toMatchObject({ bundle: "未配置", workspaceName: subject.workspaceName });
  });

  it("keeps a nonempty configured identity verbatim, including surrounding spaces", async () => {
    const subject = await fixture(" mc030-configured ");
    expect((await caller(subject.identity).roster.profile({ agentId: subject.agentId }))?.bundle).toBe(" mc030-configured ");
  });

  it("reflects a committed workspace bundle change on the next call without changing the agent profile", async () => {
    const subject = await fixture("trading");
    const client = caller(subject.identity);
    const before = await client.roster.profile({ agentId: subject.agentId });
    expect(before?.bundle).toBe("trading");
    await owner.query("UPDATE workspaces SET bundle_id='custom' WHERE id=$1", [subject.identity.workspaceId]);
    const after = await client.roster.profile({ agentId: subject.agentId });
    expect(after?.bundle).toBe("custom");
    expect(after?.agent).toEqual(before?.agent);
    expect(after?.stats).toEqual(before?.stats);
    expect(after?.events).toEqual(before?.events);
  });

  it("returns null for another workspace's agent in the same tenant and for another tenant's agent", async () => {
    const subject = await fixture("trading");
    const sibling = await fixture("custom", subject.identity.tenantId);
    const foreign = await fixture("mc030-foreign");
    for (const outside of [sibling, foreign]) {
      expect(await caller(subject.identity).roster.profile({ agentId: outside.agentId })).toBeNull();
      expect(await caller(outside.identity).roster.profile({ agentId: subject.agentId })).toBeNull();
    }
    expect((await caller(sibling.identity).roster.profile({ agentId: sibling.agentId }))?.bundle).toBe("custom");
    expect((await caller(foreign.identity).roster.profile({ agentId: foreign.agentId }))?.bundle).toBe("mc030-foreign");
  });

  it("keeps concurrent identities scoped and leaves pooled connections without an inherited workspace", async () => {
    const subjects = await Promise.all(["trading", "custom", null].map((bundle) => fixture(bundle)));
    const expected = ["trading", "custom", "未配置"];
    const responses = await Promise.all(Array.from({ length: 18 }, async (_, index) => {
      const subject = subjects[index % subjects.length]!;
      return { index, profile: await caller(subject.identity).roster.profile({ agentId: subject.agentId }) };
    }));
    for (const { index, profile } of responses) {
      expect(profile?.bundle).toBe(expected[index % subjects.length]);
      expect(profile?.workspaceName).toBe(subjects[index % subjects.length]!.workspaceName);
    }
    expect((await getAppPool().query("SELECT id FROM workspaces WHERE id=ANY($1::text[])", [subjects.map((subject) => subject.identity.workspaceId)])).rows).toEqual([]);
  });

  it("retains the existing read permission for readonly and guest members", async () => {
    const subject = await fixture("trading");
    await owner.query("UPDATE members SET role='readonly' WHERE id=$1", [subject.identity.memberId]);
    expect((await caller(subject.identity).roster.profile({ agentId: subject.agentId }))?.bundle).toBe("trading");
    expect((await caller({ ...subject.identity, memberNo: "GUEST" }).roster.profile({ agentId: subject.agentId }))?.bundle).toBe("trading");
  });

  it("rejects anonymous or stale membership and forged tenant/workspace claims", async () => {
    const subject = await fixture("trading"), outside = await fixture("custom");
    await expect(caller(null).roster.profile({ agentId: subject.agentId })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    for (const identity of [
      { ...subject.identity, tenantId: outside.identity.tenantId },
      { ...subject.identity, workspaceId: outside.identity.workspaceId },
      { ...subject.identity, memberId: `mc030-missing-${uid()}` },
    ]) {
      await expect(caller(identity).roster.profile({ agentId: subject.agentId })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
    }
    await owner.query("UPDATE members SET status='removed' WHERE id=$1", [subject.identity.memberId]);
    await expect(caller(subject.identity).roster.profile({ agentId: subject.agentId })).rejects.toMatchObject({ code: "UNAUTHORIZED" });
  });

  it("returns null for nonexistent, empty, long and special-character IDs and rejects invalid input types", async () => {
    const subject = await fixture("trading");
    for (const agentId of [`mc030-missing-${uid()}`, "", "x".repeat(4096), "' OR 1=1 --"]) {
      expect(await caller(subject.identity).roster.profile({ agentId })).toBeNull();
    }
    await expect(caller(subject.identity).roster.profile({ agentId: 42 } as never)).rejects.toMatchObject({ code: "BAD_REQUEST" });
  });

  it("surfaces an application connection failure and successfully retries without inventing a bundle", async () => {
    const subject = await fixture("trading");
    const failure = vi.spyOn(getAppPool(), "connect").mockImplementationOnce(() => Promise.reject(new Error("MC030 synthetic connection failure")));
    try {
      await expect(caller(subject.identity).roster.profile({ agentId: subject.agentId }))
        .rejects.toMatchObject({ code: "INTERNAL_SERVER_ERROR", message: "MC030 synthetic connection failure" });
    } finally {
      failure.mockRestore();
    }
    expect((await caller(subject.identity).roster.profile({ agentId: subject.agentId }))?.bundle).toBe("trading");
  });
});
