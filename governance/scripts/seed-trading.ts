/**
 * 老虎交易 · trading bundle 种子（对照 seed.ts 的 hotel 装载机制）
 * 用法：pnpm tsx --env-file=.env scripts/seed-trading.ts（幂等，可重复执行）
 *
 * 内容：tiger 租户 / trading 工作区 / 1 人类成员（投资者 owner）/
 *      bundles/trading 的 33 个 Agent preset 实例 / 三层围栏包装载 /
 *      6 个官方技能安装 / 三市交易时段 cron 触发器 / 账户档案（风险预算）
 *
 * 纪律（与 hotel 种子一致）：
 *  - 组织模型写入 ON CONFLICT DO NOTHING（幂等）；
 *  - 围栏包由 scripts/gen_fences.py 从内核 config 生成（单一口径），本脚本原样装载；
 *  - 本脚本不写任何 biz_events（事件只来自内核 ingestion adapter）。
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import YAML from "yaml";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..");
const BUNDLE_DIR = join(REPO_ROOT, "bundles/trading");

const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgres://postgres:workloom@localhost:5432/workloom";

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

async function main() {
  const pool = new pg.Pool({ connectionString: DATABASE_URL });
  const q = (text: string, params: unknown[] = []) => pool.query(text, params);

  // 租户与工作区
  await q(
    `INSERT INTO tenants (id, name) VALUES ($1,$2) ON CONFLICT (id) DO NOTHING`,
    [TENANT_ID, TENANT_NAME]);
  await q(
    `INSERT INTO workspaces (id, tenant_id, slug, name, industry, stage)
     VALUES ($1,$2,$3,$4,'trading','paper') ON CONFLICT (id) DO NOTHING`,
    [WS_ID, TENANT_ID, WS_SLUG, WS_NAME]);
  // 阶段三要素之一（装配 L3.7）：已存在工作区也确保 stage 就位
  await q(`UPDATE workspaces SET stage='paper' WHERE id=$1 AND (stage IS NULL OR stage='')`, [WS_ID]);
  console.log("✓ 租户与工作区：tiger / 老虎交易工作台");

  // 人类成员（投资者 owner）
  await q(
    `INSERT INTO members (id, workspace_id, member_no, name, role)
     VALUES ('mem-t001-id',$1,'MEM-T001','投资者','owner')
     ON CONFLICT (workspace_id, member_no) DO NOTHING`,
    [WS_ID]);
  console.log("✓ 人类成员 ×1（投资者/owner——人只做三件事：供给/裁决/沉淀）");

  // Agent presets（bundles/trading/presets/*.yml 全量装载）
  const presetFiles = readdirSync(join(BUNDLE_DIR, "presets"))
    .filter((f) => f.endsWith(".yml")).sort();
  let nPreset = 0;
  for (const f of presetFiles) {
    const p = YAML.parse(readFileSync(join(BUNDLE_DIR, "presets", f), "utf-8")) as Preset;
    await q(
      `INSERT INTO agents (id, workspace_id, preset_key, name, version, kind, readonly, fence_bindings, skills, status, meta)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'ready',$10)
       ON CONFLICT (id) DO NOTHING`,
      [
        `agt-${p.preset_key}`, WS_ID, p.preset_key, p.name,
        p.version ?? "v0.1", p.kind ?? "agent", p.readonly ?? true,
        JSON.stringify(p.fence_bindings ?? []),
        JSON.stringify(p.skills ?? []),
        JSON.stringify({
          description: p.description ?? "",
          night_shift: p.night_shift ?? false,
          high_risk: p.high_risk ?? false,
          tools: p.tools ?? [],
          prompt: p.prompt ?? {},
          write_back: p.write_back ?? [],
        }),
      ]);
    nPreset++;
  }
  console.log(`✓ Agent 实例 ×${nPreset}（研究/辩论/执行/风控/复盘/数据六条线）`);

  // 三层围栏包（gen_fences.py 从内核 config 生成，单一口径）
  const fenceDoc = YAML.parse(
    readFileSync(join(BUNDLE_DIR, "fences/trading-baseline.yml"), "utf-8")) as
    { rules?: FenceRule[] } | FenceRule[];
  const fences: FenceRule[] = Array.isArray(fenceDoc)
    ? fenceDoc : (fenceDoc.rules ?? []);
  let nFence = 0;
  for (const r of fences) {
    await q(
      `INSERT INTO fence_rules (id, rule_id, version, workspace_id, name, level, match_spec, action, is_baseline, status, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'active','system:seed-trading')
       ON CONFLICT (rule_id, version, workspace_id) DO NOTHING`,
      [
        `fr-${r.rule_id.toLowerCase()}-v1-${WS_ID}`,
        r.rule_id, FENCE_VERSION, WS_ID,
        r.name ?? r.rule_id, r.level,
        JSON.stringify({ ...(r.match ?? {}), when: r.when ?? "" }),
        JSON.stringify({
          result: r.level === "auto" ? "pass" : r.level === "review" ? "review" : "blocked",
          note: r.note ?? "",
        }),
        r.is_baseline ?? true,
      ]);
    nFence++;
  }
  console.log(`✓ 三层围栏装载 ×${nFence}（${FENCE_VERSION}，基线层 block 只可加严）`);

  // 官方技能安装（skills/*/SKILL.md）
  const skillDirs = readdirSync(join(BUNDLE_DIR, "skills"), { withFileTypes: true })
    .filter((d) => d.isDirectory()).map((d) => d.name).sort();
  let nSkill = 0;
  for (const d of skillDirs) {
    const body = readFileSync(join(BUNDLE_DIR, "skills", d, "SKILL.md"), "utf-8");
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
      [skillId, WS_ID]);
    nSkill++;
  }
  console.log(`✓ 官方技能 ×${nSkill} 已安装（安装即绑定围栏）`);

  // 账户档案（风险预算——客户 patch 层的合法来源）
  await q(
    `INSERT INTO profiles (workspace_id, tenant_id, industry, archive, forbidden, pii_vault)
     VALUES ($1,$2,'trading',$3,$4,NULL)
     ON CONFLICT (workspace_id) DO UPDATE SET archive = EXCLUDED.archive, updated_at = now()`,
    [WS_ID, TENANT_ID,
     JSON.stringify({
       account: {
         base_currency: "USD", risk_per_trade_pct: 0.008,
         max_position_per_ticker_pct: 0.20, gross_cap_pct: 0.90,
         markets: ["us"], stage: "paper",
       },
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
      [t.id, WS_ID, t.name, t.kind, t.schedule, JSON.stringify(t.action)]);
  }
  console.log(`✓ 触发器 ×${triggers.length}（三市时段 + 夜班值守 + 月度 WFA）`);

  console.log("\n老虎交易种子完成 ✅（trading bundle 全量就绪）");
  await pool.end();
}

main().catch((e) => { console.error(e); process.exit(1); });
