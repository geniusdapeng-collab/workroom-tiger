/** WFA proposal approvals: bind the reviewed bytes and consume only a verified paper receipt. */
import { readdirSync } from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import pg from "pg";
import { appendEventInTx, type EventDraft } from "../packages/base/workdata/events.ts";
import { approvalSnapshot, PROPOSAL_KIND, reviewContext, sha256, TIGER_SCOPE, validateApproval, verifyExecution } from "./proposal-bridge-contract.mjs";

const require = createRequire(import.meta.url);
const { redactText } = require("../apps/desktop/electron/diagnostic-redaction.cjs");
const execFileP = promisify(execFile);
type Context = ReturnType<typeof reviewContext>;
type Approval = { approval_id: string; tenant_id: string; workspace_id: string; status: string; gesture: Record<string, unknown>; snapshot: Record<string, unknown>; decided_by: string; decided_at: string };

export async function withRls<T>(pool: pg.Pool, fn: (c: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.tenant_id', $1, true)", [TIGER_SCOPE.tenantId]);
    await client.query("SELECT set_config('app.workspace_id', $1, true)", [TIGER_SCOPE.workspaceId]);
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    try { await client.query("ROLLBACK"); } catch (rollbackError) { throw new AggregateError([error, rollbackError], "审批事务及回滚失败"); }
    throw error;
  } finally { client.release(); }
}

function eventFor(id: string, action: string, after: unknown): EventDraft {
  return {
    who: { type: "agent", id: "strategy-optimizer", version: "tiger-approval/v1" },
    context: { tenant_id: TIGER_SCOPE.tenantId, workspace_id: TIGER_SCOPE.workspaceId, time: new Date().toISOString(), channel: "review", stage: "paper" },
    object: { type: "report", id }, decision: { action, after, basis: ["只允许研究/模拟/纸面；审批与内核回执绑定受审文件、参数、配置、目录和裁决人"] },
    rule_impact: [{ rule_id: "R-P1", version: "trading-baseline/v1", result: action === "param.change" ? "review" : "pass" }],
  };
}

export async function push(app: pg.Pool, context: Context, append = appendEventInTx): Promise<number> {
  let count = 0;
  for (const file of readdirSync(context.proposalsDir).filter((name) => /^PROP-[A-Za-z0-9_-]+\.json$/u.test(name)).sort()) {
    const id = file.slice(0, -5);
    let snapshot;
    try { snapshot = approvalSnapshot(context, id); }
    catch (error) { if (String(error).includes("只有待审")) continue; throw error; }
    await withRls(app, async (client) => {
      // Serialize absent rows too; a duplicate push must not leave an orphan event.
      await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`tiger-proposal:${TIGER_SCOPE.tenantId}:${id}`]);
      const previous = await client.query<Approval>("SELECT * FROM approvals WHERE approval_id=$1 AND tenant_id=$2 AND workspace_id=$3 FOR UPDATE", [id, TIGER_SCOPE.tenantId, TIGER_SCOPE.workspaceId]);
      if (previous.rows[0]) {
        const old = previous.rows[0].snapshot;
        if (old.kind !== PROPOSAL_KIND || old.proposal_sha256 !== snapshot.proposal_sha256 || old.parameters_sha256 !== snapshot.parameters_sha256 || old.proposals_dir !== snapshot.proposals_dir || old.config_sha256 !== snapshot.config_sha256) throw new Error(`提案 ${id} 已变化，须生成新提案与新审批`);
        return;
      }
      const event = await append(client, TIGER_SCOPE, { event: eventFor(id, "param.change", snapshot.recommended_params) });
      await client.query("INSERT INTO approvals (approval_id,tenant_id,workspace_id,event_id,channel,status,snapshot) VALUES ($1,$2,$3,$4,'inapp','pending',$5)", [id, TIGER_SCOPE.tenantId, TIGER_SCOPE.workspaceId, event.eventId, JSON.stringify(snapshot)]);
      count++;
    });
  }
  return count;
}

export async function executeDecision(row: Approval, context: Context, expected: ReturnType<typeof validateApproval>) {
  const args = [join(context.kernelRoot, "main.py"), "--out", context.outDir,
    row.status === "approved" ? "--review-approve" : "--review-reject", row.approval_id,
    "--review-expected-sha256", String(row.snapshot.proposal_sha256), "--review-execution-id", expected.executionId];
  if (row.status === "rejected") args.push("--reason", expected.reason);
  await execFileP(context.pythonExe, args, { cwd: context.outDir, timeout: 60_000, maxBuffer: 2_000_000,
    env: { ...process.env, TIGER_KERNEL_ROOT: context.kernelRoot, TIGER_PYTHON_EXE: context.pythonExe, PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1" } });
}

export async function pull(app: pg.Pool, context: Context, execute = executeDecision, append = appendEventInTx): Promise<number> {
  const pending = await withRls(app, async (client) => (await client.query<{ approval_id: string }>(
    "SELECT approval_id FROM approvals WHERE tenant_id=$1 AND workspace_id=$2 AND snapshot->>'kind'=$3 AND status IN ('approved','rejected') AND (gesture->>'executed') IS DISTINCT FROM 'true' ORDER BY approval_id",
    [TIGER_SCOPE.tenantId, TIGER_SCOPE.workspaceId, PROPOSAL_KIND])).rows);
  let count = 0;
  const failures: Error[] = [];
  for (const { approval_id: id } of pending) {
    try {
      await withRls(app, async (client) => {
        await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`tiger-proposal-directory:${sha256(context.proposalsDir)}`]);
        const locked = await client.query<Approval>("SELECT * FROM approvals WHERE approval_id=$1 AND tenant_id=$2 AND workspace_id=$3 AND (gesture->>'executed') IS DISTINCT FROM 'true' FOR UPDATE SKIP LOCKED", [id, TIGER_SCOPE.tenantId, TIGER_SCOPE.workspaceId]);
        const row = locked.rows[0];
        if (!row) return;
        const expected = validateApproval(row, context);
        if (!expected.recovered) await execute(row, context, expected);
        const receipt = verifyExecution(row, context, expected);
        const event = await append(client, TIGER_SCOPE, { event: eventFor(id, "approval.kernel.executed", receipt) });
        const updated = await client.query("UPDATE approvals SET gesture=COALESCE(gesture,'{}'::jsonb)||$1::jsonb WHERE approval_id=$2 AND tenant_id=$3 AND workspace_id=$4 AND status=$5 AND (gesture->>'executed') IS DISTINCT FROM 'true' RETURNING approval_id", [JSON.stringify({ executed: true, executed_at: receipt.executed_at, execution_receipt: { ...receipt, event_id: event.eventId, event_hash: event.hash } }), id, TIGER_SCOPE.tenantId, TIGER_SCOPE.workspaceId, row.status]);
        if (updated.rowCount !== 1) throw new Error("审批消费状态冲突");
        count++;
      });
    } catch (error) { failures.push(new Error(`审批 ${id} 未执行完成：${redactText(error instanceof Error ? error.message : String(error))}`)); }
  }
  if (failures.length) throw new AggregateError(failures, failures.map((error) => error.message).join("；"));
  return count;
}

async function main() {
  const [, , mode, directory = process.env.TIGER_PROPOSALS_DIR] = process.argv;
  if (mode !== "push" && mode !== "pull") throw new Error("用法：proposal-bridge.ts push|pull <绝对 review_proposals 目录>");
  const context = reviewContext({ kernelRoot: process.env.TIGER_KERNEL_ROOT, pythonExe: process.env.TIGER_PYTHON_EXE, proposalsDir: directory, environment: process.env.TIGER_EXECUTION_ENVIRONMENT ?? "paper" });
  if (!process.env.DATABASE_APP_URL) throw new Error("审批桥缺少 DATABASE_APP_URL，禁止使用出厂凭据回退");
  const app = new pg.Pool({ connectionString: process.env.DATABASE_APP_URL, connectionTimeoutMillis: 5000, statement_timeout: 90_000 });
  app.on("error", (error) => console.error(redactText(error.message)));
  try { console.log(`${mode === "push" ? "审批已创建" : "内核裁决已核验"}：${await (mode === "push" ? push(app, context) : pull(app, context))} 条`); }
  finally { await app.end(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch((error) => { console.error(redactText(error instanceof Error ? error.message : String(error))); process.exitCode = 1; });
