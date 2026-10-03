/**
 * 老虎交易 · trading bundle 种子（对照 seed.ts 的 hotel 装载机制）
 * 用法：pnpm tsx --env-file=.env scripts/seed-trading.ts（幂等，可重复执行）
 *
 * 内容：tiger 租户 / trading 工作区 / 1 人类成员（投资者 owner）/
 *      bundles/trading 全量声明的 Agent preset 实例 / 三层围栏包装载 /
 *      已声明官方技能安装 / 三市交易时段 cron 触发器元数据 / 账户档案（风险预算）
 *
 * 纪律（与 hotel 种子一致）：
 *  - 组织模型写入 ON CONFLICT DO NOTHING（幂等）；
 *  - 围栏包由 scripts/gen_fences.py 从内核 config 生成（单一口径），本脚本原样装载；
 *  - 本脚本不写任何 biz_events（事件只来自内核 ingestion adapter）。
 */
import { readdirSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import pg from "pg";
import YAML from "yaml";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..");
const BUNDLE_DIR = join(REPO_ROOT, "bundles/trading");

// This fallback is the existing public development fixture, never a customer credential.
const DEVELOPMENT_DATABASE_PASSWORD = "workloom";
const developmentDatabase = new URL("postgres://localhost:5432/workloom");
developmentDatabase.username = "postgres";
developmentDatabase.password = DEVELOPMENT_DATABASE_PASSWORD;
const DATABASE_URL = process.env.DATABASE_URL ?? developmentDatabase.href;

const TENANT_ID = "tiger";
const TENANT_NAME = "老虎交易（Tiger Trading）";
const WS_ID = "trading";
const WS_NAME = "老虎交易工作台";
const WS_SLUG = "tiger-trading";
const FENCE_VERSION = "trading-baseline/v1";

interface Preset {
  preset_key: string; name: string; version?: string; kind?: string;
  description?: string; readonly?: boolean; night_shift?: boolean;
  high_risk?: boolean; fence_bindings?: string[]; skills?: string[];
  tools?: unknown[]; prompt?: unknown; write_back?: unknown;
}
interface FenceRule {
  rule_id: string; name?: string; level: string;
  match?: Record<string, unknown>; when?: string;
  is_baseline?: boolean; note?: string;
}

export interface TradingSeedOptions {
  tenantId?: string;
  workspaceId?: string;
  workspaceSlug?: string;
  bundleDir?: string;
}

/** 全部安装写入同一事务；重跑只补缺，客户档案、装配选择、示例模式与禁用状态均保留。 */
export async function seedTrading(pool: pg.Pool, options: TradingSeedOptions = {}): Promise<void> {
  const tenantId = options.tenantId ?? TENANT_ID;
  const workspaceId = options.workspaceId ?? WS_ID;
  const workspaceSlug = options.workspaceSlug ?? WS_SLUG;
  const bundleDir = options.bundleDir ?? BUNDLE_DIR;
  if (![tenantId, workspaceId, workspaceSlug].every((value) => /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value))) {
    throw new Error("交易种子作用域标识无效");
  }
  const suffix = workspaceId === WS_ID ? "" : `-${createHash("sha256").update(workspaceId).digest("hex").slice(0, 12)}`;
  const risk = JSON.parse(readFileSync(join(bundleDir, "schemas/risk-defaults.json"), "utf8")) as {
    schemaVersion?: string; source?: { path?: string; sha256?: string }; account?: Record<string, unknown>;
  };
  if (risk.schemaVersion !== "trading.risk-defaults/v1"
      || risk.source?.path !== "trading_system/config.py"
      || !/^[a-f0-9]{64}$/.test(risk.source?.sha256 ?? "")
      || ["risk_per_trade_pct", "max_position_per_ticker_pct", "gross_cap_pct"].some((key) => {
        const value = risk.account?.[key];
        return typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 1;
      })) throw new Error("交易默认风险资产损坏，请由内核 config 重新生成并验签行业包");
  const client = await pool.connect();
  const q = (text: string, params: unknown[] = []) => client.query(text, params);
  try {
  await q("BEGIN");
  await q("SELECT pg_advisory_xact_lock(hashtext($1))", [`seed-trading:${workspaceId}`]);

  // 租户与工作区
  await q(
    `INSERT INTO tenants (id, name) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING`,
    [tenantId, TENANT_NAME]);
  await q(
    `INSERT INTO workspaces (id, tenant_id, slug, name, industry, stage, bundle_id, is_example)
     VALUES ($1,$2,$3,$4,'trading','paper','trading',true)
     ON CONFLICT (id) DO NOTHING`,
    [workspaceId, tenantId, workspaceSlug, WS_NAME]);
  const workspace = (await q(`SELECT tenant_id, bundle_id FROM workspaces WHERE id=$1 FOR UPDATE`, [workspaceId])).rows[0] as
    { tenant_id: string; bundle_id: string | null } | undefined;
  if (!workspace || workspace.tenant_id !== tenantId) throw new Error("交易种子拒绝覆盖其他租户工作区");
  const profile = (await q(`SELECT tenant_id FROM profiles WHERE workspace_id=$1 FOR UPDATE`, [workspaceId])).rows[0] as
    { tenant_id: string } | undefined;
  if (profile && profile.tenant_id !== tenantId) throw new Error("交易种子拒绝覆盖其他租户档案");
  // 阶段三要素之一（装配 L3.7）：已存在工作区也确保 stage 就位
  await q(`UPDATE workspaces SET stage='paper' WHERE id=$1 AND (stage IS NULL OR stage='')`, [workspaceId]);
  console.log(`✓ 租户与工作区：${tenantId} / ${workspaceId}`);

  // 人类成员（投资者 owner）
  await q(
    `INSERT INTO members (id, workspace_id, member_no, name, role)
     VALUES ($1,$2,'MEM-T001','投资者','owner')
     ON CONFLICT (workspace_id, member_no) DO NOTHING`,
    [`mem-t001-id${suffix}`, workspaceId]);
  console.log("✓ 人类成员 ×1（投资者/owner——人只做三件事：供给/裁决/沉淀）");

  // Agent presets（bundles/trading/presets/*.yml 全量装载）
  const presetFiles = readdirSync(join(bundleDir, "presets"))
    .filter((f) => f.endsWith(".yml")).sort();
  let nPreset = 0;
  const presetIds: string[] = [];
  for (const f of presetFiles) {
    const p = YAML.parse(readFileSync(join(bundleDir, "presets", f), "utf-8")) as Preset;
    if (!p.preset_key || !p.name) throw new Error(`交易岗位资产损坏：${f}`);
    const agentId = `agt-${p.preset_key}${suffix}`;
    await q(
      `INSERT INTO agents (id, workspace_id, preset_key, name, version, kind, readonly, fence_bindings, skills, status, meta)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'ready',$10)
       ON CONFLICT (id) DO NOTHING`,
      [
        agentId, workspaceId, p.preset_key, p.name,
        p.version ?? "v0.1", p.kind ?? "agent", p.readonly ?? true,
        JSON.stringify(p.fence_bindings ?? []),
        JSON.stringify(p.skills ?? []),
        JSON.stringify({
          description: p.description ?? "",
          seed_origin: "seed-trading/v2",
          night_shift: p.night_shift ?? false,
          high_risk: p.high_risk ?? false,
          tools: p.tools ?? [],
          prompt: p.prompt ?? {},
          write_back: p.write_back ?? [],
        }),
      ]);
    // 只补本仓新声明的研究执行工具；保留已有提示词/自定义工具与 ready/disabled/invalid 选择。
    for (const tool of (p.tools ?? []) as Array<{ name?: string; access?: string }>) {
      if (!tool.name?.startsWith("tiger.")) continue;
      await q(
        `UPDATE agents SET meta=jsonb_set(meta, '{tools}', COALESCE(meta->'tools','[]'::jsonb) || $3::jsonb)
         WHERE id=$1 AND workspace_id=$2 AND NOT EXISTS
           (SELECT 1 FROM jsonb_array_elements(COALESCE(meta->'tools','[]'::jsonb)) t WHERE t->>'name'=$4)`,
        [agentId, workspaceId, JSON.stringify([tool]), tool.name],
      );
    }
    presetIds.push(agentId);
    nPreset++;
  }
  console.log(`✓ Agent 实例 ×${nPreset}（研究/辩论/执行/风控/复盘/数据六条线）`);

  // 三层围栏包（gen_fences.py 从内核 config 生成，单一口径）
  const fenceDoc = YAML.parse(
    readFileSync(join(bundleDir, "fences/trading-baseline.yml"), "utf-8")) as
    { rules?: FenceRule[] } | FenceRule[];
  const fences: FenceRule[] = Array.isArray(fenceDoc)
    ? fenceDoc : (fenceDoc.rules ?? []);
  let nFence = 0;
  const fenceIds: string[] = [];
  for (const r of fences) {
    await q(
      `INSERT INTO fence_rules (id, rule_id, version, workspace_id, name, level, match_spec, action, is_baseline, status, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'active','system:seed-trading')
       ON CONFLICT (rule_id, version, workspace_id) DO NOTHING`,
      [
        `fr-${r.rule_id.toLowerCase()}-v1-${workspaceId}`,
        r.rule_id, FENCE_VERSION, workspaceId,
        r.name ?? r.rule_id, r.level,
        JSON.stringify({ ...(r.match ?? {}), when: r.when ?? "" }),
        JSON.stringify({
          result: r.level === "auto" ? "pass" : r.level === "review" ? "review" : "blocked",
          note: r.note ?? "",
        }),
        r.is_baseline ?? true,
      ]);
    const existing = (await q(`SELECT id FROM fence_rules WHERE rule_id=$1 AND version=$2 AND workspace_id=$3`,
      [r.rule_id, FENCE_VERSION, workspaceId])).rows[0] as { id: string } | undefined;
    if (!existing) throw new Error(`交易围栏装载失败：${r.rule_id}`);
    fenceIds.push(existing.id);
    nFence++;
  }
  console.log(`✓ 三层围栏装载 ×${nFence}（${FENCE_VERSION}，基线层 block 只可加严）`);

  // 官方技能安装（skills/*/SKILL.md）
  const skillDirs = readdirSync(join(bundleDir, "skills"), { withFileTypes: true })
    .filter((d) => d.isDirectory()).map((d) => d.name).sort();
  let nSkill = 0;
  const skillIds: string[] = [];
  for (const d of skillDirs) {
    const body = readFileSync(join(bundleDir, "skills", d, "SKILL.md"), "utf-8");
    const fm = /^---\n([\s\S]*?)\n---/.exec(body)?.[1] ?? "";
    const meta = YAML.parse(fm) as { name?: string; description?: string } ?? {};
    const skillId = `skill-${meta.name ?? d}`;
    await q(
      `INSERT INTO skills (id, level, bundle, name, version, description, fence_bindings, body, desensitized)
       VALUES ($1,'official','trading',$2,'0.1.0',$3,'[]',$4,false)
       ON CONFLICT (id) DO NOTHING`,
      [skillId, meta.name ?? d, meta.description ?? "", body]);
    await q(
      `INSERT INTO skill_installs (skill_id, workspace_id, installed_by)
       VALUES ($1,$2,'MEM-T001') ON CONFLICT (skill_id, workspace_id) DO NOTHING`,
      [skillId, workspaceId]);
    skillIds.push(skillId);
    nSkill++;
  }
  console.log(`✓ 官方技能 ×${nSkill} 已安装（安装即绑定围栏）`);

  // 首次装载补唯一 active；升级不得重新激活被用户卸载的行业包，也不得切换其 custom/staged 装配。
  const installs = await q(`SELECT id, status FROM bundle_installs WHERE workspace_id=$1 FOR UPDATE`, [workspaceId]);
  const initialInstall = installs.rows.length === 0 && workspace.bundle_id === "trading";
  await q(
    `INSERT INTO bundle_installs (id, workspace_id, bundle_id, assets, status)
     VALUES ($1,$2,'trading',$3,$4)
     ON CONFLICT (id) DO UPDATE SET assets=bundle_installs.assets || EXCLUDED.assets
       WHERE bundle_installs.workspace_id=EXCLUDED.workspace_id AND bundle_installs.bundle_id='trading'`,
    [`bi-${workspaceId}-trading`, workspaceId,
     JSON.stringify({ seed_batch_id: `seed-trading-${workspaceId}`, preset_ids: presetIds, fence_rule_ids: fenceIds,
       skill_ids: skillIds, risk_defaults_sha256: createHash("sha256").update(readFileSync(join(bundleDir, "schemas/risk-defaults.json"))).digest("hex") }),
     initialInstall ? "active" : "uninstalled"],
  );
  console.log("✓ 交易 Bundle 装配台账已更新（保留客户选择）");

  // 账户档案（风险预算——客户 patch 层的合法来源）
  await q(
    `INSERT INTO profiles (workspace_id, tenant_id, industry, archive, forbidden, pii_vault)
     VALUES ($1,$2,'trading',$3,$4,NULL)
     ON CONFLICT (workspace_id) DO NOTHING`,
    [workspaceId, tenantId,
     JSON.stringify({
       account: risk.account,
       dataMode: "simulated",
       note: "客户 patch 层只可加严（基线单调守卫）；阈值 single source of truth = 内核 trading_system/config.py",
     }),
     JSON.stringify(["禁止任何绕过围栏的直接写单", "禁止承诺收益"])]);

  // 三市交易时段 cron 触发器（北京时间；复盘机制由内核 review 团队执行）
  const triggers = [
    { id: "tg-cn-premarket", name: "A股盘前 09:00", kind: "cron", schedule: "0 9 * * 1-5",
      action: { dispatch: "premarket-trader", template: "market.premarket", market: "cn" } },
    { id: "tg-cn-close", name: "A股盘后 15:30 结算归因", kind: "cron", schedule: "30 15 * * 1-5",
      action: { dispatch: "portfolio-ops", template: "market.settle", market: "cn" } },
    { id: "tg-hk-close", name: "港股盘后 16:30 结算归因", kind: "cron", schedule: "30 16 * * 1-5",
      action: { dispatch: "portfolio-ops", template: "market.settle", market: "hk" } },
    { id: "tg-us-premarket", name: "美股盘前 21:00", kind: "cron", schedule: "0 21 * * 1-5",
      action: { dispatch: "premarket-trader", template: "market.premarket", market: "us" } },
    { id: "tg-us-daily", name: "美股日报 06:00（收盘后全链路+复盘）", kind: "cron", schedule: "0 6 * * 2-6",
      action: { dispatch: "review-chief", template: "pipeline.daily", market: "us" } },
    { id: "tg-tiger-night-2200", name: "老虎夜班 22:00 夜班出征（美股时段值守）", kind: "cron", schedule: "0 22 * * *",
      action: { dispatch: "night-shift", template: "night.run.start" } },
    { id: "tg-wfa-monthly", name: "月度 WFA 提案（每月首个交易日 10:00）", kind: "cron", schedule: "0 10 1 * *",
      action: { dispatch: "strategy-optimizer", template: "review.wfa.propose" } },
  ];
  for (const t of triggers) {
    await q(
      `INSERT INTO triggers (id, workspace_id, name, kind, schedule, action, enabled, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,true,'system:seed-trading')
       ON CONFLICT (id) DO NOTHING`,
      [`${t.id}${suffix}`, workspaceId, t.name, t.kind, t.schedule, JSON.stringify(t.action)]);
  }
  console.log(`✓ 触发器 ×${triggers.length}（三市时段 + 夜班值守 + 月度 WFA）`);

  await q("COMMIT");
  console.log("\n老虎交易种子完成 ✅（客户档案和装配选择已保留）");
  } catch (error) {
    try { await q("ROLLBACK"); }
    catch (rollbackError) { throw new AggregateError([error, rollbackError], "交易种子失败且回滚未成功"); }
    throw error;
  } finally { client.release(); }
}

async function main() {
  const pool = new pg.Pool({ connectionString: DATABASE_URL });
  try { await seedTrading(pool); }
  finally { await pool.end(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error: unknown) => {
    console.error("交易种子安装失败：", error instanceof Error ? error.message : "数据库或资产错误");
    process.exitCode = 1;
  });
}
