/** Tiger 夜班独立研究：建档/绑定计划/五元派遣同一事务，再走公共逐步围栏与原子认领。 */
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { MAX_CONCURRENT_THREADS } from "@workloom/shared";
import { closeAllPools, getAppPool } from "../packages/db/src/client.js";
import { gatewayAppendOnClient, insertWithReadableId, THREAD_ID_SOURCE } from "@workloom/base/workdata";
import {
  prepareTigerThreadOn, runTigerQuestForThread, type TigerResearchOptions,
} from "../apps/server/src/industry/tiger-runtime.js";
import type { Scope, ThreadQuestOutcome } from "../apps/server/src/runtime/thread-runner.js";

const DEFAULT_SCOPE: Scope = { tenantId: "tiger", workspaceId: "trading" };
const DEFAULT_GOAL = "老虎夜班：独立模拟研究管线与日报";

/** Explicit fixture scope is an internal API; public clients cannot supply deployment paths or tenants. */
export async function runTradingNightly(input: {
  scope?: Scope; threadId?: string; goal?: string; research?: TigerResearchOptions;
} = {}): Promise<ThreadQuestOutcome> {
  const scope = input.scope ?? DEFAULT_SCOPE;
  const goal = input.goal?.trim() ?? DEFAULT_GOAL;
  if (!goal || goal.length > 500) throw new Error("夜班目标必须是 1 至 500 字的明确研究任务");
  const client = await getAppPool().connect();
  let threadId = input.threadId;
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.workspace_id',$1,true)", [scope.workspaceId]);
    await client.query("SELECT set_config('app.tenant_id',$1,true)", [scope.tenantId]);
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`thread-dispatch:${scope.workspaceId}`]);
    const existing = threadId ? (await client.query<{ title: string }>(
      "SELECT title FROM threads WHERE id=$1 AND workspace_id=$2 FOR UPDATE", [threadId, scope.workspaceId])).rows[0] : undefined;
    if (existing && existing.title !== goal) throw new Error("夜班续跑不能替换已有线程目标");
    if (!existing) {
      const count = await client.query<{ n: string }>(
        "SELECT count(*)::text n FROM threads WHERE workspace_id=$1 AND status IN ('queued','running')", [scope.workspaceId]);
      if (Number(count.rows[0]?.n ?? 0) >= MAX_CONCURRENT_THREADS) {
        throw Object.assign(new Error(`工作区并发上限 ${MAX_CONCURRENT_THREADS}，请等待当前任务结束`), { code: "TOO_MANY_REQUESTS" });
      }
      const insert = async (id: string) => {
        await client.query(`INSERT INTO threads(id,tenant_id,workspace_id,title,mode,status,created_by)
          VALUES($1,$2,$3,$4,'quest','queued','night-shift')`, [id, scope.tenantId, scope.workspaceId, goal]);
        return id;
      };
      if (threadId) {
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{1,119}$/.test(threadId)) throw new Error("夜班线程标识无效");
        await insert(threadId);
      } else threadId = (await insertWithReadableId(client, THREAD_ID_SOURCE, insert)).id;
      await prepareTigerThreadOn(client, scope, { threadId, goal, presetRef: "kernel-orchestrator", research: input.research });
      await gatewayAppendOnClient(client, { ...scope, actor: { id: "kernel-orchestrator", type: "agent" }, sessionId: threadId }, {
        who: { type: "agent", id: "kernel-orchestrator", version: "tiger-nightly/v1" },
        context: { tenant_id: scope.tenantId, workspace_id: scope.workspaceId, time: new Date().toISOString(), channel: "夜班" },
        object: { type: "thread", id: threadId },
        decision: { action: "thread.dispatch", after: { threadId, title: goal, mode: "quest", presetKey: "kernel-orchestrator", origin: "nightly" } },
        rule_impact: [],
      });
    }
    await client.query("COMMIT");
  } catch (error) {
    try { await client.query("ROLLBACK"); }
    catch (rollbackError) { throw new AggregateError([error, rollbackError], "夜班建档失败且回滚未成功"); }
    throw error;
  } finally { client.release(); }
  return runTigerQuestForThread(scope, { threadId: threadId!, goal, presetRef: "kernel-orchestrator" });
}

async function main(): Promise<void> {
  const started = Date.now();
  try {
    const result = await runTradingNightly({ threadId: process.argv[2], goal: process.env.TIGER_QUEST_GOAL });
    console.log(JSON.stringify({ threadId: result.threadId, status: result.status, stepsDone: result.stepsDone,
      stepsTotal: result.stepsTotal, unverified: result.unverified, blockedBy: result.blockedBy,
      pendingApprovalId: result.pendingApprovalId, elapsedMs: Date.now() - started }));
    process.exitCode = result.status === "completed" && result.unverified.length === 0 ? 0 : 1;
  } catch {
    console.error("夜班研究执行失败；未获得可交付回执。请查部署连接、已验行业包与线程事件。");
    process.exitCode = 2;
  } finally { await closeAllPools(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) await main();
