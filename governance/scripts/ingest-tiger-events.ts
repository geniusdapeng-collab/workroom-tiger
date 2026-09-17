/**
 * 老虎交易 · 内核五元事件入库适配器（ingestion adapter）
 *
 * 把交易内核产生的 governance_events.jsonl（Python governance_bridge 输出，
 * 自带内核侧哈希链）逐条幂等写入 WorkLoom 底座 biz_events（底座侧哈希链）。
 *
 * 设计要点：
 *  - 幂等：appendEventIdempotent（同 event_id 冲突丢弃），可安全重复执行/cron；
 *  - 双链互验：内核链 hash 以 decision.kernel_hash 随事件留痕（loose 扩展位），
 *    底座链由 appendEventIdempotent 按 DB 链尾续链——两条链各自独立可验；
 *  - 角色纪律：以 workloom_gateway 角色连接（唯一可 INSERT biz_events 的角色）。
 *
 * 用法：
 *   pnpm tsx --env-file=.env scripts/ingest-tiger-events.ts [jsonl路径] [tenant] [workspace]
 *   默认：../reports/governance_events.jsonl tiger trading
 */
import { readFileSync } from "node:fs";
import { appendEventIdempotent } from "../packages/base/workdata/events.ts";
import { getGatewayPool, closeAllPools } from "../packages/db/src/client.ts";
import type { BusinessEvent } from "../packages/shared/src/event-schema.ts";

const [, , jsonlPath = "../reports/governance_events.jsonl",
  tenantId = "tiger", workspaceId = "trading"] = process.argv;

interface KernelLine {
  payload: BusinessEvent & { event_id: string };
  prev_hash: string;
  hash: string;
}

async function main() {
  const raw = readFileSync(jsonlPath, "utf-8").trim();
  if (!raw) {
    console.log("事件文件为空，无操作。");
    return;
  }
  const lines = raw.split("\n");
  const pool = getGatewayPool();
  let ok = 0, dup = 0, fail = 0;
  for (const [i, line] of lines.entries()) {
    let rec: KernelLine;
    try {
      rec = JSON.parse(line);
    } catch {
      console.warn(`第 ${i + 1} 行 JSON 解析失败，跳过`);
      fail++;
      continue;
    }
    try {
      // 内核链 hash 留痕（loose 扩展：decision 内允许行业字段）
      const payload = {
        ...rec.payload,
        decision: { ...rec.payload.decision, kernel_hash: rec.hash },
      };
      const r = await appendEventIdempotent(
        pool, { tenantId, workspaceId }, payload as BusinessEvent);
      r.deduped ? dup++ : ok++;
    } catch (e) {
      console.warn(`第 ${i + 1} 行（${rec.payload?.event_id}）写入失败: ${(e as Error).message}`);
      fail++;
    }
  }
  console.log(`入库完成：新增 ${ok}，幂等跳过 ${dup}，失败 ${fail}（共 ${lines.length} 行）`);
  await closeAllPools();
  if (fail > 0) process.exit(1);
}

main();
