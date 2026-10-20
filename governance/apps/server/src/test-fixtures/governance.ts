/**
 * Disposable PostgreSQL fixtures for the governance suites.
 * These synthetic rules and staff are test inputs, never product seed data.
 */
import pg from "pg";
import { loadBundleUiProjection } from "@workloom/base/bundles";
import { charterSchema } from "@workloom/base/captain";
import { gatewayAppend } from "@workloom/base/workdata";
import type { InspectionAdapter } from "@workloom/base/inspection";

export const TEST_RANGE_KEY = "fixture-change-ratio";
export const TEST_CAP_KEY = "fixture-spend";
export const TEST_AUTONOMY = {
  ranges: { [TEST_RANGE_KEY]: { label: "测试变动比例", lower: 0.85, anchor: 1, upper: 1.15 } },
  caps: { [TEST_CAP_KEY]: { label: "测试支出", limit: 5000 } },
};

export function fixtureDatabaseUrls(): { owner: string; app: string; gateway: string } {
  const names = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const;
  const urls = names.map((name) => {
    const raw = process.env[name];
    if (!raw) throw new Error(`${name} must explicitly identify a disposable test database`);
    const parsed = new URL(raw);
    if (!["postgres:", "postgresql:"].includes(parsed.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname)
      || !/^\/workloom_tiger_[a-z0-9_]+$/.test(parsed.pathname)) {
      throw new Error(`${name} must use loopback and a workloom_tiger_* test database`);
    }
    return { raw, parsed };
  });
  const target = ({ parsed }: (typeof urls)[number]) => `${parsed.hostname}:${parsed.port || "5432"}${parsed.pathname}`;
  if (!urls.every((url) => target(url) === target(urls[0]!))) {
    throw new Error("All fixture database roles must target the same disposable database");
  }
  if (decodeURIComponent(urls[1]!.parsed.username) !== "workloom_app"
    || decodeURIComponent(urls[2]!.parsed.username) !== "workloom_gateway") {
    throw new Error("Fixtures must exercise workloom_app and workloom_gateway role boundaries");
  }
  return { owner: urls[0]!.raw, app: urls[1]!.raw, gateway: urls[2]!.raw };
}

export interface GovernanceFixture {
  tenantId: string;
  workspaceId: string;
  workspaceSlug: string;
  skillName: string;
}

export function governanceFixtureIdentity(run: string): GovernanceFixture {
  if (!/^[a-z0-9-]+$/.test(run)) throw new Error("Invalid fixture run identifier");
  return {
    tenantId: `tenant-fixture-${run}`,
    workspaceId: `ws-fixture-${run}`,
    workspaceSlug: `fixture-${run}`,
    skillName: `fixture-skill-${run}`,
  };
}

export async function createGovernanceFixture(fixture: GovernanceFixture): Promise<void> {
  const urls = fixtureDatabaseUrls();
  const owner = new pg.Client({ connectionString: urls.owner });
  const gateway = new pg.Pool({ connectionString: urls.gateway });
  await owner.connect();
  try {
    const roles = await owner.query<{ rolname: string; rolsuper: boolean; rolbypassrls: boolean }>(
      "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('workloom_app','workloom_gateway')",
    );
    if (roles.rows.length !== 2 || roles.rows.some((role) => role.rolsuper || (role.rolname === "workloom_app" && role.rolbypassrls))) {
      throw new Error("Fixture roles must preserve the application RLS boundary");
    }
    const projection = loadBundleUiProjection("trading");
    const charter = charterSchema.parse({
      mode: "trial", autonomy: TEST_AUTONOMY,
      circuit_breaker: { kpi_floor: { quality: 0.7 } },
      grant: {
        event_id: `fixture-grant-${fixture.workspaceId}`, granted_by: "MEM-001",
        granted_at: new Date().toISOString(), disclosure_version: "fixture-v1", clauses: ["test-only"],
        trial_ends_at: new Date(Date.now() + 86_400_000).toISOString(),
      },
    });
    await owner.query("BEGIN");
    await owner.query("INSERT INTO tenants (id,name,plan) VALUES ($1,'隔离测试租户','pro')", [fixture.tenantId]);
    await owner.query(
      `INSERT INTO workspaces (id,tenant_id,slug,name,industry,stage,bundle_id,is_example)
       VALUES ($1,$2,$3,'隔离测试工作区','trading','paper','trading',true)`,
      [fixture.workspaceId, fixture.tenantId, fixture.workspaceSlug],
    );
    for (const [number, role] of [["001", "owner"], ["002", "manager"], ["003", "readonly"]]) {
      await owner.query("INSERT INTO members (id,workspace_id,member_no,name,role) VALUES ($1,$2,$3,$4,$5)",
        [`${fixture.workspaceId}-member-${number}`, fixture.workspaceId, `MEM-${number}`, `测试${role}`, role]);
    }
    const agentIds: string[] = [];
    const presets = [
      { key: "pricing-agent", bindings: ["R1", "R2"], tools: [{ name: "biz.price.read", access: "read" }, { name: "biz.price.write", access: "write" }] },
      { key: "review-agent", bindings: ["R6"], tools: [{ name: "biz.review.reply", access: "write" }] },
      { key: "content-agent", bindings: ["R4"], tools: [{ name: "content.draft", access: "write" }] },
      { key: "inspection-agent", bindings: [], tools: [{ name: "fixture.read", access: "read" }], readonly: true },
      { key: "desktop-agent", bindings: ["R2"], tools: [{ name: "desktop.gui", access: "write" }], highRisk: true },
    ];
    for (const preset of presets) {
      const id = `${fixture.workspaceId}-${preset.key}`;
      agentIds.push(id);
      await owner.query(
        `INSERT INTO agents (id,workspace_id,preset_key,name,version,kind,readonly,fence_bindings,skills,meta)
         VALUES ($1,$2,$3,$4,'fixture-v1','agent',$5,$6,'[]',$7)`,
        [id, fixture.workspaceId, preset.key, `测试岗位 ${preset.key}`, preset.readonly ?? false, JSON.stringify(preset.bindings),
          JSON.stringify({ tools: preset.tools.map((tool) => ({ ...tool, desc: "仅限隔离测试" })), high_risk: preset.highRisk ?? false })],
      );
    }
    const rules = [
      ["R1", "auto", "fixture_price", "price.adjust", "abs(after.price - before.price) / before.price <= 0.08"],
      ["R2", "block", "fixture_price", "price.adjust", "after.price < 380"],
      ["R3", "review", "fixture_order", "order.refund", "true"],
      ["R4", "auto", "fixture_content", "content.draft", "true"],
      ["R5", "block", "fixture_channel", "channel.enable", "true"],
      ["R6", "review", "review", "review.reply", "true"],
    ];
    for (const [id, level, objectType, action, when] of rules) {
      await owner.query(
        `INSERT INTO fence_rules (id,rule_id,version,workspace_id,name,level,match_spec,action,is_baseline,status,created_by)
         VALUES ($1,$2,'fixture-v1',$3,$4,$5,$6,$7,true,'active','system:test-fixture')`,
        [`${fixture.workspaceId}-${id}`, id, fixture.workspaceId, `测试规则 ${id}`, level,
          JSON.stringify({ object_types: [objectType], actions: [action], when }),
          JSON.stringify({ result: level === "block" ? "blocked" : level === "review" ? "review" : "pass" })],
      );
    }
    await owner.query("INSERT INTO profiles (workspace_id,tenant_id,industry,archive) VALUES ($1,$2,'trading',$3)",
      [fixture.workspaceId, fixture.tenantId, JSON.stringify({ dataMode: "simulated", charter, fixture: true })]);
    await owner.query(
      `INSERT INTO skills (id,level,name,version,description,fence_bindings,body,desensitized)
       VALUES ($1,'official',$2,'1.0.0','测试用技能，不是产品技能','["R1","R2"]','测试步骤',true)`,
      [`skill-${fixture.workspaceId}`, fixture.skillName],
    );
    await owner.query(
      `INSERT INTO bundle_installs (id,workspace_id,bundle_id,assets,status)
       VALUES ($1,$2,'trading',$3,'active')`,
      [`bi-${fixture.workspaceId}`, fixture.workspaceId, JSON.stringify({
        bundle_version: projection.bundleVersion, integrity_digest: projection.integrityDigest,
        preset_ids: agentIds, fence_rule_ids: rules.map(([id]) => `${fixture.workspaceId}-${id}`), skill_ids: [],
      })],
    );
    await owner.query("COMMIT");
    // The append-only ledger is seeded only through the real gateway.
    for (let index = 0; index < 12; index++) {
      await gatewayAppend(gateway, { ...fixture, actor: { id: "MEM-001", type: "human" } }, {
        who: { type: "human", id: "MEM-001" },
        context: { tenant_id: fixture.tenantId, workspace_id: fixture.workspaceId, time: new Date().toISOString() },
        object: { type: "fixture_price", id: `sample-${index}` },
        decision: { action: "fixture.sample", after: { value: index } },
        rule_impact: index === 0 ? [{ rule_id: "R2", version: "fixture-v1", result: "blocked" }] : [],
      });
    }
  } catch (error) {
    await owner.query("ROLLBACK");
    throw error;
  } finally {
    await owner.end();
    await gateway.end();
  }
}

/** A declared synthetic adapter proves inspection mechanics without a hotel fallback. */
export const fixtureInspectionAdapter: InspectionAdapter = {
  id: "test-fixture-only", presetKey: "inspection-agent",
  checks: [{ id: "fixture-health", kind: "fixture-health", name: "测试健康检查" }],
  probes: {
    "fixture-health": (check, snapshot) => [{
      checkId: check.id, status: snapshot.healthy === false ? "anomaly" : "ok",
      severity: "high", summary: snapshot.healthy === false ? "测试异常" : "测试正常",
      objectType: "fixture", objectId: String(snapshot.id ?? "sample"), source: "test-fixture",
    }],
  },
};
