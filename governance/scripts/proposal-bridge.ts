/**
 * 老虎交易 · WFA 参数提案 × 审批控制台 双向桥（T3）
 *
 * 把内核复盘团队（诸葛·策略优化师）的月度 WFA 提案接入 WorkLoom 审批流：
 *
 * push 模式（提案 → 审批卡片）：
 *   扫描内核 reports/review_proposals/*.json 中 status=pending_review 的提案，
 *   逐条写五元事件（action=param.change，命中 R-P1 review 规则）+ 创建审批卡片
 *   （approvals 表 pending，snapshot 为提案快照）。幂等（approval_id=proposal_id）。
 *
 * pull 模式（裁决 → 内核执行）：
 *   读取审批控制台已裁决（approved/rejected）的提案审批，
 *   调内核审批 CLI（main.py --review-approve / --review-reject --reason）执行状态机：
 *   approve → 次日生效 + tuned_params.json 披露；reject → 原因回流（必填）。
 *   已执行的审批在 gesture.executed=true 标记，幂等。
 *
 * 用法：
 *   pnpm tsx --env-file=.env scripts/proposal-bridge.ts push [proposalsDir]
 *   pnpm tsx --env-file=.env scripts/proposal-bridge.ts pull [proposalsDir]
 *   默认 proposalsDir = ../reports/review_proposals
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { join } from "node:path";
import pg from "pg";
import { appendEventIdempotent } from "../packages/base/workdata/events.ts";
import { getGatewayPool, closeAllPools } from "../packages/db/src/client.ts";
import type { BusinessEvent } from "../packages/shared/src/event-schema.ts";

const execFileP = promisify(execFile);
const [, , mode, proposalsDir = "../reports/review_proposals"] = process.argv;
const KERNEL_ROOT = join(process.cwd(), "..");
const SCOPE = { tenantId: "tiger", workspaceId: "trading" };
const APP_URL = process.env.DATABASE_APP_URL
  ?? "postgres://workloom_app:workloom_dev_app@localhost:5432/workloom";

interface Proposal {
  proposal_id: string; status: string; verdict: string;
  dsr: number; oos_expectancy: number;
  grid_result: Record<string, unknown>; created_at: string;
  reason?: string; effective_from?: string;
}

function loadProposals(): Proposal[] {
  if (!existsSync(proposalsDir)) return [];
  return readdirSync(proposalsDir).filter((f) => f.endsWith(".json")).sort()
    .map((f) => JSON.parse(readFileSync(join(proposalsDir, f), "utf-8")) as Proposal);
}

/** 事务级 RLS 上下文包装（A3：autocommit 下 set_config 即失效） */
async function withRls<T>(pool: pg.Pool, fn: (c: pg.PoolClient) => Promise<T>): Promise<T> {
  const c = await pool.connect();
  try {
    await c.query("BEGIN");
    await c.query("SELECT set_config('app.tenant_id', $1, true)", [SCOPE.tenantId]);
    await c.query("SELECT set_config('app.workspace_id', $1, true)", [SCOPE.workspaceId]);
    const r = await fn(c);
    await c.query("COMMIT");
    return r;
  } catch (e) {
    await c.query("ROLLBACK").catch(() => undefined);
    throw e;
  } finally {
    c.release();
  }
}

async function push(): Promise<void> {
  const pending = loadProposals().filter((p) => p.status === "pending_review");
  if (!pending.length) {
    console.log("无待审批提案（DSR 不显著的提案已由内核自动 reject）。");
    return;
  }
  const gateway = getGatewayPool();
  const app = new pg.Pool({ connectionString: APP_URL });
  for (const p of pending) {
    // event_id 须形如 E-12345：由 proposal_id 确定性派生（幂等键）
    let h = 0;
    for (const ch of p.proposal_id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const evtId = `E-9${String(h % 100000).padStart(5, "0")}`;
    const event: BusinessEvent = {
      event_id: evtId,
      who: { type: "agent", id: "strategy-optimizer", version: "v0.1" },
      context: { tenant_id: SCOPE.tenantId, workspace_id: SCOPE.workspaceId,
                 time: p.created_at || new Date().toISOString(),
                 channel: "review", stage: "paper" },
      object: { type: "report", id: p.proposal_id },
      decision: {
        action: "param.change",
        before: null,
        after: p.grid_result,
        basis: [
          `WFA 提案：DSR=${p.dsr}，OOS 期望=${p.oos_expectancy}R`,
          "纪律：DSR 显著方可进入审批；生效次日披露（白皮书§14）",
        ],
      },
      rule_impact: [{ rule_id: "R-P1", version: "trading-baseline/v1", result: "review" }],
    } as BusinessEvent;
    await appendEventIdempotent(gateway, SCOPE, event);
    await withRls(app, async (c) => {
      await c.query(
        `INSERT INTO approvals (approval_id, tenant_id, workspace_id, event_id, channel, status, snapshot)
         VALUES ($1,$2,$3,$4,'inapp','pending',$5)
         ON CONFLICT (approval_id) DO NOTHING`,
        [p.proposal_id, SCOPE.tenantId, SCOPE.workspaceId, evtId,
         JSON.stringify({
           kind: "wfa.param.proposal", dsr: p.dsr,
           oos_expectancy: p.oos_expectancy,
           recommended_params: p.grid_result?.recommended_params,
           gestures: ["approve", "edit_approve", "reject（原因必填）"],
         })]);
    });
    console.log(`✓ 审批卡片已创建：${p.proposal_id}（DSR=${p.dsr}，OOS=${p.oos_expectancy}R）`);
  }
  await app.end();
}

async function pull(): Promise<void> {
  const app = new pg.Pool({ connectionString: APP_URL });
  const decided = await withRls(app, async (c) => {
    const r = await c.query<{
      approval_id: string; status: string; gesture: Record<string, unknown> | null;
    }>(
      `SELECT approval_id, status, gesture FROM approvals
       WHERE workspace_id=$1 AND status IN ('approved','rejected')
         AND (gesture->>'executed') IS DISTINCT FROM 'true'`,
      [SCOPE.workspaceId]);
    return r.rows;
  });
  if (!decided.length) {
    console.log("无新裁决需要回写内核。");
    await app.end();
    return;
  }
  for (const a of decided) {
    const reason = String(a.gesture?.reason ?? "");
    const args = a.status === "approved"
      ? ["main.py", "--review-approve", a.approval_id]
      : ["main.py", "--review-reject", a.approval_id, "--reason",
         reason || "审批控制台驳回（未附原因）"];
    const { stdout } = await execFileP("python3", args, { cwd: KERNEL_ROOT });
    await withRls(app, async (c) => {
      await c.query(
        `UPDATE approvals SET gesture = COALESCE(gesture,'{}'::jsonb) || $1::jsonb
         WHERE approval_id=$2`,
        [JSON.stringify({ executed: true, executed_at: new Date().toISOString() }),
         a.approval_id]);
    });
    console.log(`✓ 裁决已回写内核：${a.approval_id} [${a.status}] ${String(stdout).split("\n")[0]}`);
  }
  await app.end();
}

async function main() {
  if (mode === "push") await push();
  else if (mode === "pull") await pull();
  else { console.error("用法：proposal-bridge.ts push|pull [proposalsDir]"); process.exit(2); }
  await closeAllPools();
}

main().catch((e) => { console.error(e); process.exit(1); });
