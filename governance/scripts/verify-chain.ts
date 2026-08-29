/**
 * scripts/verify-chain.ts · 事件库哈希链完整性验证（生产口径，审计 #32 配套工具）
 *
 * 按 workspace 分段，逐条重算 sha256(prev_hash ‖ canonicalJson(payload))：
 *  - prev_hash 必须严格接龙（首条 = GENESIS）
 *  - hash 必须与 canonicalJson 重算一致（任何 payload 篡改/算法漂移都会暴露）
 * 用法：pnpm tsx --env-file=.env scripts/verify-chain.ts
 * 退出码：0 = 全库链完整；1 = 有断点/篡改
 */
import pg from "pg";
import { canonicalJson } from "@workloom/base/workdata";
import { createHash } from "node:crypto";

const DATABASE_URL =
  process.env.DATABASE_URL ?? "postgres://postgres:workloom@localhost:5432/workloom";

const sha256 = (s: string) => createHash("sha256").update(s, "utf-8").digest("hex");

async function main(): Promise<void> {
  const client = new pg.Client({ connectionString: DATABASE_URL });
  await client.connect();
  try {
    const ws = await client.query<{ workspace_id: string }>(
      `SELECT DISTINCT workspace_id FROM biz_events ORDER BY 1`,
    );
    let bad = 0;
    let total = 0;
    for (const { workspace_id } of ws.rows) {
      const r = await client.query<{ event_id: string; payload: unknown; prev_hash: string; hash: string }>(
        `SELECT event_id, payload, prev_hash, hash FROM biz_events WHERE workspace_id=$1 ORDER BY seq`,
        [workspace_id],
      );
      let prev = "GENESIS";
      let wsBad = 0;
      for (const row of r.rows) {
        const expect = sha256(prev + canonicalJson(row.payload));
        if (row.prev_hash !== prev) {
          console.error(`✗ [${workspace_id}] ${row.event_id}: prev_hash 断链`);
          wsBad++;
        }
        if (row.hash !== expect) {
          console.error(`✗ [${workspace_id}] ${row.event_id}: hash 重算不符（payload 篡改或口径漂移）`);
          wsBad++;
        }
        prev = row.hash;
      }
      total += r.rows.length;
      bad += wsBad;
      console.log(`${wsBad ? "✗" : "✓"} [${workspace_id}] ${r.rows.length} 条${wsBad ? `（${wsBad} 处异常）` : "，逐条重算一致"}`);
    }
    if (bad > 0) {
      console.error(`❌ 哈希链验证失败：共 ${bad} 处异常（${total} 条事件）`);
      process.exit(1);
    }
    console.log(`✅ 全库哈希链完整：${total} 条事件逐条重算全部一致`);
  } finally {
    await client.end();
  }
}

await main();
