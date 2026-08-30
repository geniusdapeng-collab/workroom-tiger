/**
 * 老虎交易 · 夜班 Quest 编排入口（夜班自治驱动交易链路）
 *
 * 每晚（cron/触发器 tg-tiger-night-2200 调度）执行：
 *   数据源自检 → 内核全链路（扫描→六层决策→模拟盘→日报）→ 事件入库 → 官网发布
 *
 * 机制全部走 WorkLoom Quest 运行时（packages/runtime/src/loop.ts runQuest）：
 *   - 围栏瀑布逐步判定（R-T0：模拟盘阶段编排动作 auto；实盘落 default review 待审）
 *   - 每步写五元事件（含回执位；无实证产物 →「未核实」，线程不得转 completed）
 *   - replay 断点续跑：同一线程重入时已完成步骤幂等跳过（kill -9 安全）
 *
 * 用法：
 *   pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts [threadId]
 *   验证/演示（demo 内核，2 分钟级）：
 *   TIGER_KERNEL_CMD="python3 main.py --demo --out reports" \
 *     pnpm tsx --env-file=.env scripts/quest-trading-nightly.ts
 */
import { randomUUID } from "node:crypto";
import pg from "pg";
import { runQuest } from "../packages/runtime/src/loop.ts";

const APP_URL = process.env.DATABASE_APP_URL
  ?? "postgres://workloom_app:workloom_dev_app@localhost:5432/workloom";
const GW_URL = process.env.DATABASE_GATEWAY_URL
  ?? "postgres://workloom_gateway:workloom_dev_gateway@localhost:5432/workloom";

const SCOPE = { tenantId: "tiger", workspaceId: "trading" };
/** 目标决定编排模板（planQuest 关键词路由）：
 *  默认夜班（美股 daily 全链路）；A股盘后/港股盘后/A股盘中/港股盘中/美股盘中 见 loop.ts planQuest */
const GOAL = process.env.TIGER_QUEST_GOAL ?? "老虎交易夜班：全链路日报与复盘编排";

async function main() {
  const threadId = process.argv[2] ?? `quest-tiger-${new Date().toISOString().slice(0, 10)}-${randomUUID().slice(0, 8)}`;
  const app = new pg.Pool({ connectionString: APP_URL });
  const gateway = new pg.Pool({ connectionString: GW_URL });

  // 线程建档（Quest 线程卡：进度/当前动作投影到 IM 与审批台）
  // 事务级 RLS 上下文（A3：autocommit 下 set_config 语句结束即失效，必须显式事务）
  const tc = await app.connect();
  try {
    await tc.query("BEGIN");
    await tc.query("SELECT set_config('app.tenant_id', $1, true)", [SCOPE.tenantId]);
    await tc.query("SELECT set_config('app.workspace_id', $1, true)", [SCOPE.workspaceId]);
    await tc.query(
      `INSERT INTO threads (id, tenant_id, workspace_id, title, mode, status, created_by)
       VALUES ($1,$2,$3,$4,'quest','queued','night-shift')
       ON CONFLICT (id) DO NOTHING`,
      [threadId, SCOPE.tenantId, SCOPE.workspaceId, GOAL]);
    await tc.query("COMMIT");
  } catch (e) {
    await tc.query("ROLLBACK").catch(() => undefined);
    throw e;
  } finally {
    tc.release();
  }

  console.log(`[夜班 Quest] 线程 ${threadId} 启动（围栏 R-T0 自治窗口：stage=paper）`);
  const t0 = Date.now();
  const result = await runQuest(app, gateway, SCOPE, {
    threadId, goal: GOAL, presetKey: "review-chief",
  });
  const mins = ((Date.now() - t0) / 60000).toFixed(1);

  console.log(`[夜班 Quest] 完成（${mins} 分钟）：status=${result.status} `
    + `步骤 ${result.stepsDone}/${result.stepsTotal}`
    + (result.unverified.length ? ` 未核实=${result.unverified.join(",")}` : "")
    + (result.blockedBy ? ` 熔断=${result.blockedBy}` : "")
    + (result.pendingApprovalId ? ` 待审批=${result.pendingApprovalId}` : ""));

  await app.end();
  await gateway.end();
  // completed 且无未核实步骤 → 0；其余（待审/熔断/未核实）→ 1（cron 告警语义）
  process.exit(result.status === "completed" && result.unverified.length === 0 ? 0 : 1);
}

main().catch((e) => { console.error("[夜班 Quest] 系统性失败:", e); process.exit(2); });
