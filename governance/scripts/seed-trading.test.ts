import assert from "node:assert/strict";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import test from "node:test";
import pg from "pg";
import { seedTrading } from "./seed-trading.js";

const databaseUrl = process.env.TIGER_SEED_TEST_DATABASE_URL;
const fixtureId = `mc140-seed-${randomUUID().slice(0, 8)}`;
const scope = { tenantId: `${fixtureId}-tenant`, workspaceId: fixtureId, workspaceSlug: fixtureId };
const bundle = resolve(import.meta.dirname, "../bundles/trading");

test("MC025: PostgreSQL 安装、升级、并发和失败回滚", { skip: !databaseUrl }, async (t) => {
  const pool = new pg.Pool({ connectionString: databaseUrl });
  const otherWorkspace = `${fixtureId}-rollback`;
  const otherTenant = `${fixtureId}-wrong-tenant`;
  const scratch = mkdtempSync(join(process.env.TIGER_TEST_TMP_ROOT ?? tmpdir(), "tiger-seed-"));
  try {
    await t.test("首次安装的客户默认值与生成资产一致，重跑不重复资产", async () => {
      await seedTrading(pool, scope);
      const risk = JSON.parse(readFileSync(join(bundle, "schemas/risk-defaults.json"), "utf8"));
      const before = (await pool.query(`SELECT archive FROM profiles WHERE workspace_id=$1`, [scope.workspaceId])).rows[0];
      assert.deepEqual(before.archive.account, risk.account);
      assert.equal(before.archive.dataMode, "simulated");
      const counts = async () => (await pool.query(`SELECT
        (SELECT count(*) FROM agents WHERE workspace_id=$1)::int agents,
        (SELECT count(*) FROM fence_rules WHERE workspace_id=$1)::int fences,
        (SELECT count(*) FROM skill_installs WHERE workspace_id=$1)::int skills,
        (SELECT count(*) FROM triggers WHERE workspace_id=$1)::int triggers,
        (SELECT count(*) FROM bundle_installs WHERE workspace_id=$1 AND status='active')::int active`,
      [scope.workspaceId])).rows[0];
      const initial = await counts();
      await Promise.all([seedTrading(pool, scope), seedTrading(pool, scope)]);
      assert.deepEqual(await counts(), initial);
      assert.equal(initial.active, 1);
      const install = (await pool.query(`SELECT assets FROM bundle_installs WHERE workspace_id=$1 AND status='active'`, [scope.workspaceId])).rows[0];
      assert.equal(install.assets.preset_ids.length, initial.agents);
      assert.equal(install.assets.fence_rule_ids.length, initial.fences);
      assert.equal(install.assets.skill_ids.length, initial.skills);
    });
    const tightened = { account: { risk_per_trade_pct: 0.004, max_position_per_ticker_pct: 0.10,
      gross_cap_pct: 0.50, stage: "paper" }, dataMode: "simulated", customer_note: "synthetic MC025 sentinel" };
    await t.test("完整客户档案、例子模式和被禁用岗位在升级后保持原值", async () => {
      await pool.query(`UPDATE profiles SET archive=$2 WHERE workspace_id=$1`, [scope.workspaceId, JSON.stringify(tightened)]);
      await pool.query(`UPDATE workspaces SET is_example=false WHERE id=$1`, [scope.workspaceId]);
      await pool.query(`UPDATE agents SET status='disabled' WHERE workspace_id=$1 AND preset_key='mrs'`, [scope.workspaceId]);
      await seedTrading(pool, scope);
      assert.deepEqual((await pool.query(`SELECT archive FROM profiles WHERE workspace_id=$1`, [scope.workspaceId])).rows[0].archive, tightened);
      assert.equal((await pool.query(`SELECT is_example FROM workspaces WHERE id=$1`, [scope.workspaceId])).rows[0].is_example, false);
      assert.equal((await pool.query(`SELECT status FROM agents WHERE workspace_id=$1 AND preset_key='mrs'`, [scope.workspaceId])).rows[0].status, "disabled");
    });
    await t.test("客户切换 custom 装配后重跑成功且不重激活 trading", async () => {
      await pool.query(`UPDATE bundle_installs SET status='uninstalled' WHERE workspace_id=$1`, [scope.workspaceId]);
      await pool.query(`INSERT INTO bundle_installs (id,workspace_id,bundle_id,assets,status) VALUES ($1,$2,'custom','{}','active')`, [`${fixtureId}-custom`, scope.workspaceId]);
      await pool.query(`UPDATE workspaces SET bundle_id='custom' WHERE id=$1`, [scope.workspaceId]);
      await seedTrading(pool, scope);
      assert.equal((await pool.query(`SELECT bundle_id FROM workspaces WHERE id=$1`, [scope.workspaceId])).rows[0].bundle_id, "custom");
      assert.deepEqual((await pool.query(`SELECT id,bundle_id FROM bundle_installs WHERE workspace_id=$1 AND status='active'`, [scope.workspaceId])).rows,
        [{ id: `${fixtureId}-custom`, bundle_id: "custom" }]);
      assert.deepEqual((await pool.query(`SELECT archive FROM profiles WHERE workspace_id=$1`, [scope.workspaceId])).rows[0].archive, tightened);
    });
    await t.test("相同工作区标识的其他租户被拒绝，并回滚其新租户行", async () => {
      await assert.rejects(seedTrading(pool, { ...scope, tenantId: otherTenant }), /其他租户工作区/);
      assert.equal((await pool.query(`SELECT count(*)::int n FROM tenants WHERE id=$1`, [otherTenant])).rows[0].n, 0);
      assert.equal((await pool.query(`SELECT tenant_id FROM workspaces WHERE id=$1`, [scope.workspaceId])).rows[0].tenant_id, scope.tenantId);
    });
    await t.test("岗位资产错误发生在建组织后，整个事务回滚且原工作区无污染", async () => {
      const copied = join(scratch, "bundle");
      cpSync(bundle, copied, { recursive: true });
      writeFileSync(join(copied, "presets/zz-invalid.yml"), "preset_key: invalid-fixture\n", "utf8");
      await assert.rejects(seedTrading(pool, {
        tenantId: `${otherWorkspace}-tenant`, workspaceId: otherWorkspace, workspaceSlug: otherWorkspace, bundleDir: copied,
      }), /岗位资产损坏/);
      assert.equal((await pool.query(`SELECT count(*)::int n FROM workspaces WHERE id=$1`, [otherWorkspace])).rows[0].n, 0);
      assert.equal((await pool.query(`SELECT count(*)::int n FROM tenants WHERE id=$1`, [`${otherWorkspace}-tenant`])).rows[0].n, 0);
      assert.equal((await pool.query(`SELECT count(*)::int n FROM agents WHERE workspace_id=$1`, [otherWorkspace])).rows[0].n, 0);
      assert.deepEqual((await pool.query(`SELECT archive FROM profiles WHERE workspace_id=$1`, [scope.workspaceId])).rows[0].archive, tightened);
    });
  } finally {
    for (const table of ["triggers", "skill_installs", "bundle_installs", "fence_rules", "agents", "members", "profiles"]) {
      await pool.query(`DELETE FROM ${table} WHERE workspace_id=ANY($1::text[])`, [[scope.workspaceId, otherWorkspace]]);
    }
    await pool.query(`DELETE FROM workspaces WHERE id=ANY($1::text[])`, [[scope.workspaceId, otherWorkspace]]);
    await pool.query(`DELETE FROM tenants WHERE id=ANY($1::text[])`, [[scope.tenantId, otherTenant, `${otherWorkspace}-tenant`]]);
    rmSync(scratch, { recursive: true, force: true });
    await pool.end();
  }
});
