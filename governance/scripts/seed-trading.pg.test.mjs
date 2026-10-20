/**
 * Trading seed regression. Opt in with RUN_TRADING_SEED_PG_TESTS=1 on a migrated,
 * disposable loopback workloom_tiger_* database. Never loads .env implicitly.
 * This suite truncates fixture tables and temporarily creates a failure trigger;
 * run it serially, in a database dedicated to this suite.
 */
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { after, before, beforeEach, describe, it } from "node:test";
import pg from "pg";
import YAML from "yaml";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const bundleDir = join(root, "bundles/trading");
const manifest = JSON.parse(readFileSync(join(bundleDir, "bundle.json"), "utf8"));
const skillIds = manifest.workloom.provides.skills.map((file) => {
  const body = readFileSync(join(bundleDir, file), "utf8");
  const frontmatter = /^---\n([\s\S]*?)\n---/.exec(body);
  assert.ok(frontmatter, `Missing frontmatter in ${file}`);
  return `skill-${YAML.parse(frontmatter[1]).name}`;
});
const skillId = "skill-premarket-plan";
assert.ok(skillIds.includes(skillId));
const execute = promisify(execFile);
const enabled = process.env.RUN_TRADING_SEED_PG_TESTS === "1";

function guardedUrl(raw) {
  assert.ok(raw, "Explicit test database URL required");
  const url = new URL(raw);
  assert.ok(["postgres:", "postgresql:"].includes(url.protocol), "PostgreSQL URL required");
  assert.ok(["127.0.0.1", "[::1]"].includes(url.hostname), "Numeric loopback host required");
  assert.match(url.pathname, /^\/workloom_tiger_[a-z0-9_]+$/, "Disposable workloom_tiger_* database required");
  assert.equal(url.search, "", "URL options may not override the test connection target");
  assert.equal(url.hash, "", "URL fragments are not supported");
  return url;
}

describe("trading seed test target guard", () => {
  it("rejects external, ambiguous and non-disposable targets before connecting", () => {
    for (const raw of [
      undefined, "not-a-url", "https://127.0.0.1/workloom_tiger_test",
      "postgres://example.com/workloom_tiger_test", "postgres://localhost/workloom_tiger_test",
      "postgres://127.0.0.1/workloom", "postgres://127.0.0.1/workloom_tiger_test?host=example.com",
      "postgres://127.0.0.1/workloom_tiger_test#other", "postgres://127.0.0.1/workloom_tiger_test%2Fother",
    ]) assert.throws(() => guardedUrl(raw));
    assert.equal(guardedUrl("postgres://127.0.0.1:55432/workloom_tiger_test").port, "55432");
    assert.equal(guardedUrl("postgres://[::1]:55432/workloom_tiger_test").hostname, "[::1]");
  });
});

describe("trading seed PostgreSQL contract", { skip: !enabled, concurrency: false }, () => {
  let db;
  let ownerUrl;
  let appUrl;
  const tables = ["tenants", "workspaces", "members", "agents", "fence_rules", "skills", "skill_installs", "bundle_installs", "profiles", "triggers"];

  before(async () => {
    ownerUrl = guardedUrl(process.env.DATABASE_URL);
    appUrl = guardedUrl(process.env.DATABASE_APP_URL);
    assert.equal(appUrl.host, ownerUrl.host, "Owner and app URLs must target the same local cluster");
    assert.equal(appUrl.pathname, ownerUrl.pathname, "Owner and app URLs must target the same disposable database");
    db = new pg.Client({ connectionString: ownerUrl.toString(), connectionTimeoutMillis: 5000 });
    await db.connect();
    assert.equal((await db.query("SELECT current_database() AS name")).rows[0].name, ownerUrl.pathname.slice(1));
    await db.query("SET client_min_messages TO warning");
    const migrations = await db.query("SELECT name FROM _migrations ORDER BY name");
    assert.ok(migrations.rows.some((row) => row.name === "0037_thread_number_sequence.sql"), "Apply the current migration chain first");
  });

  after(async () => { if (db) await db.end(); });

  beforeEach(async () => {
    await db.query(`TRUNCATE ${tables.join(", ")} CASCADE`);
  });

  async function seed(url = ownerUrl) {
    try {
      const result = await execute(process.execPath, ["--import", "tsx", "scripts/seed-trading.ts"], {
        cwd: root,
        env: { ...process.env, DATABASE_URL: guardedUrl(url.toString()).toString(), NODE_ENV: "test", LLM_PROVIDER: "mock" },
        timeout: 30000,
        maxBuffer: 1024 * 1024,
      });
      return { code: 0, stdout: result.stdout, stderr: result.stderr };
    } catch (error) {
      // Do not print the exec error object: it contains the command environment.
      return { code: typeof error.code === "number" ? error.code : 1, stdout: error.stdout ?? "", stderr: error.stderr ?? "", signal: error.signal };
    }
  }

  async function successfulSeed() {
    const result = await seed();
    assert.equal(result.code, 0, `Seed failed: ${result.stderr}`);
    assert.match(result.stdout, /老虎交易种子完成/);
  }

  async function snapshot() {
    const state = {};
    for (const table of tables) {
      state[table] = (await db.query(`SELECT to_jsonb(t) AS row FROM ${table} t ORDER BY to_jsonb(t)::text`)).rows.map((row) => row.row);
    }
    return state;
  }

  async function installation(id = skillId, workspace = "trading") {
    return (await db.query("SELECT * FROM skill_installs WHERE skill_id=$1 AND workspace_id=$2", [id, workspace])).rows[0];
  }

  async function assertFreshInstalls() {
    const rows = (await db.query(`SELECT i.skill_id, i.fence_bindings_snapshot, i.installed_version, s.fence_bindings, s.version
      FROM skill_installs i JOIN skills s ON s.id=i.skill_id WHERE i.workspace_id='trading' ORDER BY i.skill_id`)).rows;
    assert.deepEqual(rows.map((row) => row.skill_id), [...skillIds].sort());
    for (const row of rows) {
      assert.ok(row.installed_version.trim(), `${row.skill_id} is missing installed_version`);
      assert.equal(row.installed_version, row.version);
      assert.deepEqual(row.fence_bindings_snapshot, row.fence_bindings);
    }
  }

  async function addWorkspace(id = "mc016-other", tenant = "mc016-other-tenant") {
    await db.query("INSERT INTO tenants(id,name) VALUES($1,'Synthetic test tenant') ON CONFLICT DO NOTHING", [tenant]);
    await db.query("INSERT INTO workspaces(id,tenant_id,slug,name,industry,stage,bundle_id) VALUES($1,$2,$1,'Synthetic test workspace','trading','paper','trading')", [id, tenant]);
  }

  async function injectLateFailure(run) {
    await db.query(`CREATE FUNCTION public.mc016_fail_seed() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.id='tg-wfa-monthly' THEN RAISE EXCEPTION 'MC016 injected final-trigger failure'; END IF; RETURN NEW; END $$`);
    try {
      await db.query("CREATE TRIGGER mc016_seed_failure BEFORE INSERT ON triggers FOR EACH ROW EXECUTE FUNCTION public.mc016_fail_seed()");
      try { await run(); }
      finally { await db.query("DROP TRIGGER mc016_seed_failure ON triggers"); }
    } finally { await db.query("DROP FUNCTION public.mc016_fail_seed()"); }
  }

  it("fresh install records all manifest assets and each registry version/fence snapshot without business events", async () => {
    await successfulSeed();
    await assertFreshInstalls();
    const state = await snapshot();
    assert.equal(state.agents.length, manifest.workloom.provides.presets.length);
    assert.equal(state.skills.length, skillIds.length);
    assert.equal(state.bundle_installs.length, 1);
    assert.equal(state.bundle_installs[0].bundle_id, "trading");
    assert.equal(state.bundle_installs[0].status, "active");
    assert.equal(state.workspaces[0].stage, "paper");
    assert.equal((await db.query("SELECT count(*)::int AS count FROM biz_events")).rows[0].count, 0);
  });

  it("takes nondefault versions and nonempty fences from the existing authoritative registry row", async () => {
    await db.query(`INSERT INTO skills(id,level,bundle,name,version,description,fence_bindings,body)
      VALUES($1,'official','trading','premarket-plan','2.7.4','Synthetic existing registration',$2,'Synthetic registry body')`, [skillId, JSON.stringify(["R-T1", "R-T13"])]);
    const before = (await db.query("SELECT * FROM skills WHERE id=$1", [skillId])).rows[0];
    await successfulSeed();
    assert.deepEqual((await db.query("SELECT * FROM skills WHERE id=$1", [skillId])).rows[0], before);
    const installed = await installation();
    assert.equal(installed.installed_version, before.version);
    assert.deepEqual(installed.fence_bindings_snapshot, before.fence_bindings);
  });

  it("repairs the legacy double-empty seed installation using current registry values", async () => {
    await successfulSeed();
    await db.query("UPDATE skills SET version='2.7.4',fence_bindings=$2 WHERE id=$1", [skillId, JSON.stringify(["R-T1", "R-T13"])]);
    await db.query("UPDATE skill_installs SET installed_version='',fence_bindings_snapshot='[]' WHERE skill_id=$1 AND workspace_id='trading'", [skillId]);
    const before = await installation();
    await successfulSeed();
    const after = await installation();
    assert.equal(after.installed_version, "2.7.4");
    assert.deepEqual(after.fence_bindings_snapshot, ["R-T1", "R-T13"]);
    assert.equal(after.installed_by, before.installed_by);
    assert.deepEqual(after.installed_at, before.installed_at);
  });

  it("preserves valid old snapshots, including a legitimately empty snapshot, after registry changes", async () => {
    await successfulSeed();
    for (const bindings of [["R-T14"], []]) {
      await db.query("UPDATE skill_installs SET installed_version='approved-1',fence_bindings_snapshot=$2,installed_by='synthetic-owner' WHERE skill_id=$1 AND workspace_id='trading'", [skillId, JSON.stringify(bindings)]);
      const before = await installation();
      await db.query("UPDATE skills SET version='2.7.4',fence_bindings=$2 WHERE id=$1", [skillId, JSON.stringify(["R-T1", "R-T13"])]);
      await successfulSeed();
      assert.deepEqual(await installation(), before);
    }
  });

  it("preserves stricter customer budgets, charter, arbitrary archive fields and workspace customer status on reseed", async () => {
    await successfulSeed();
    const archive = {
      account: { base_currency: "USD", risk_per_trade_pct: 0.002, max_position_per_ticker_pct: 0.05, gross_cap_pct: 0.30, markets: ["us"], stage: "paper" },
      charter: { mission: "Synthetic customer charter 'quoted' 中文\nline two", target: "preserve" },
      custom: { nested: [0, false, null, ""] },
    };
    await db.query("UPDATE profiles SET archive=$1,forbidden=$2,pii_vault=$3,updated_at='2026-01-01T00:00:00Z' WHERE workspace_id='trading'", [JSON.stringify(archive), JSON.stringify(["synthetic customer rule"]), JSON.stringify({ synthetic: true })]);
    await db.query("UPDATE workspaces SET is_example=false,name='Synthetic customer workspace',night_config=$1 WHERE id='trading'", [JSON.stringify({ enabled: false })]);
    const before = await snapshot();
    await successfulSeed();
    const after = await snapshot();
    assert.deepEqual(after.profiles, before.profiles);
    assert.deepEqual(after.workspaces, before.workspaces);
  });

  it("repeated seed is row-for-row idempotent", async () => {
    await successfulSeed();
    const before = await snapshot();
    await successfulSeed();
    assert.deepEqual(await snapshot(), before);
  });

  it("does not relabel an existing customer workspace as an example", async () => {
    await successfulSeed();
    await db.query("UPDATE workspaces SET is_example=false,name='Synthetic customer workspace' WHERE id='trading'");
    const before = (await snapshot()).workspaces;
    await successfulSeed();
    assert.deepEqual((await snapshot()).workspaces, before);
  });

  it("preserves an existing active trading installation with a non-seed ID and its assembly metadata", async () => {
    await successfulSeed();
    await db.query("UPDATE bundle_installs SET status='uninstalled',uninstalled_at=now() WHERE workspace_id='trading'");
    await db.query("INSERT INTO bundle_installs(id,workspace_id,bundle_id,assets,status,assembly_version,assembly_hash) VALUES('mc016-customer-install','trading','trading',$1,'active',7,'synthetic-reviewed-hash')", [JSON.stringify({ synthetic_customer_assets: ["retained"] })]);
    const before = (await snapshot()).bundle_installs;
    await successfulSeed();
    assert.deepEqual((await snapshot()).bundle_installs, before);
  });

  it("refuses a different active bundle without changing it or committing partial seed rows", async () => {
    await addWorkspace("trading", "tiger");
    await db.query("INSERT INTO bundle_installs(id,workspace_id,bundle_id,status) VALUES('mc016-other-bundle','trading','synthetic-customer-bundle','active')");
    const before = await snapshot();
    const result = await seed();
    assert.notEqual(result.code, 0);
    assert.doesNotMatch(result.stdout, /老虎交易种子完成/);
    assert.deepEqual(await snapshot(), before);
  });

  it("does not reactivate a deliberately uninstalled or staged seed installation", async () => {
    await successfulSeed();
    for (const status of ["uninstalled", "staged"]) {
      await db.query("UPDATE bundle_installs SET status=$1 WHERE workspace_id='trading'", [status]);
      const before = await snapshot();
      const result = await seed();
      assert.notEqual(result.code, 0);
      assert.deepEqual(await snapshot(), before);
    }
  });

  it("refuses a trading workspace ID owned by another tenant without overwriting that tenant", async () => {
    await addWorkspace("trading", "mc016-other-tenant");
    const before = await snapshot();
    const result = await seed();
    assert.notEqual(result.code, 0);
    assert.deepEqual(await snapshot(), before);
  });

  it("refuses a seed installation ID collision in another workspace without rewriting its record", async () => {
    await addWorkspace();
    await db.query("INSERT INTO bundle_installs(id,workspace_id,bundle_id,assets,status) VALUES('bi-trading-trading','mc016-other','synthetic-other',$1,'staged')", [JSON.stringify({ synthetic: "retained" })]);
    const before = await snapshot();
    const result = await seed();
    assert.notEqual(result.code, 0);
    assert.deepEqual(await snapshot(), before);
  });

  it("does not change another workspace's installation or profile during legacy repair", async () => {
    await successfulSeed();
    await addWorkspace();
    await db.query("INSERT INTO skill_installs(skill_id,workspace_id,installed_by) VALUES($1,'mc016-other','synthetic-other')", [skillId]);
    await db.query("INSERT INTO profiles(workspace_id,tenant_id,industry,archive) VALUES('mc016-other','mc016-other-tenant','trading',$1)", [JSON.stringify({ synthetic: "other archive" })]);
    const beforeInstall = await installation(skillId, "mc016-other");
    const beforeProfile = (await db.query("SELECT * FROM profiles WHERE workspace_id='mc016-other'")).rows[0];
    await successfulSeed();
    assert.deepEqual(await installation(skillId, "mc016-other"), beforeInstall);
    assert.deepEqual((await db.query("SELECT * FROM profiles WHERE workspace_id='mc016-other'")).rows[0], beforeProfile);
  });

  it("rolls back the entire fresh seed when the final trigger insert fails", async () => {
    const before = await snapshot();
    await injectLateFailure(async () => {
      const result = await seed();
      assert.notEqual(result.code, 0);
      assert.match(result.stderr, /MC016 injected final-trigger failure/);
      assert.doesNotMatch(result.stdout, /老虎交易种子完成/);
      assert.deepEqual(await snapshot(), before);
    });
    await successfulSeed();
    await assertFreshInstalls();
  });

  it("rolls back legacy repairs on late failure without damaging an existing customer archive", async () => {
    await successfulSeed();
    await db.query("UPDATE skill_installs SET installed_version='',fence_bindings_snapshot='[]' WHERE workspace_id='trading'");
    await db.query("UPDATE profiles SET archive=$1 WHERE workspace_id='trading'", [JSON.stringify({ synthetic_charter: "preserve through failure", account: { risk_per_trade_pct: 0.001 } })]);
    const before = await snapshot();
    await injectLateFailure(async () => {
      const result = await seed();
      assert.notEqual(result.code, 0);
      assert.match(result.stderr, /MC016 injected final-trigger failure/);
      assert.deepEqual(await snapshot(), before);
    });
  });

  it("concurrent seed processes converge to one complete installation", async () => {
    const results = await Promise.all([seed(), seed()]);
    for (const result of results) assert.equal(result.code, 0, result.stderr);
    await assertFreshInstalls();
    assert.equal((await snapshot()).bundle_installs.length, 1);
  });

  it("fails with the application role without leaving owner-only seed writes", async () => {
    const before = await snapshot();
    const result = await seed(appUrl);
    assert.notEqual(result.code, 0);
    assert.deepEqual(await snapshot(), before);
  });
});
