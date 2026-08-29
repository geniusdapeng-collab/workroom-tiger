/**
 * workdata · 事件写入段（B1）
 * 纪律（铁律 1/2）：
 *  - 一切写入必经 gateway.appendEvent（本模块唯一入口；DB 层另有「仅 workloom_gateway 可 INSERT」双保险，F1.2）
 *  - append-only：回滚=逆向补偿事件，原事件永不修改（L1.1/F1.6，由触发器兜底）
 *  - 幂等：UNIQUE(tenant_id,event_id) 冲突丢弃不报错（L1.4）
 *  - 哈希链：prev_hash/hash = sha256(prev_hash || canonical(payload))（技术新增量 A1）
 *
 * 事件编号：单租户内 advisory 锁串行化（网关单写者假设），取 MAX(seq)+1 格式化为 E-N。
 * 演示编号段 E-88xx 由种子占用，运行时自然续接；高并发分片编号进停车场（总纲 §7）。
 */
import { createHash } from "node:crypto";
import type pg from "pg";
import {
  formatEventId,
  safeParseBusinessEvent,
  type BusinessEvent,
  type Decision,
  type EventContext,
  type EventObject,
  type ModelTrace,
  type Receipt,
  type RuleImpact,
  type Who,
} from "@workloom/shared";
import { maskDeep } from "./pii.js";

/**
 * 事件草稿：不带 event_id/hash/ts（由写入段分配/计算）。
 * 显式罗列字段而非 Omit——BusinessEvent 源自 z.looseObject，索引签名会让 Omit 丢失字段类型。
 * 行业扩展字段经 context/object/decision 的 loose 位透传（附录 E 冻结纪律）。
 */
export interface EventDraft {
  who: Who;
  context: EventContext;
  object: EventObject;
  decision: Decision;
  rule_impact: RuleImpact[];
  receipt?: Receipt;
  model_trace?: ModelTrace;
  links?: string[];
}

export const GENESIS_HASH = "GENESIS";

/** 规范化序列化（键序稳定，保证哈希链可复算） */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
}

export function eventHash(prevHash: string, payload: unknown): string {
  return createHash("sha256").update(prevHash + canonicalJson(payload), "utf-8").digest("hex");
}

export interface AppendInput {
  /** 不带 event_id/hash 的五元事件（由写入段分配） */
  event: EventDraft;
  sessionId?: string | null;
}

export interface AppendResult {
  eventId: string;
  seq: bigint;
  hash: string;
  /** true = 同 (tenant_id,event_id) 已存在，本次为幂等丢弃（L1.4） */
  deduped: boolean;
  /** 脱敏命中次数（第二段瀑布留痕） */
  piiHits: number;
}

/**
 * 追加一条五元事件（须以 workloom_gateway 角色连接；调用方须先完成三段瀑布——见 gateway.ts）
 * 单事务：advisory 锁（租户级串行）→ 读链尾 → 分配 E-N → zod 校验 → INSERT（冲突丢弃）
 */
export async function appendEvent(
  gateway: pg.Pool | pg.PoolClient,
  scope: { tenantId: string; workspaceId: string },
  input: AppendInput,
): Promise<AppendResult> {
  const client = "connect" in gateway ? await gateway.connect() : gateway;
  const owned = "connect" in gateway;
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
    await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
    // 租户级写串行化（链尾读取与插入必须原子）
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`event-chain:${scope.tenantId}`]);

    const tail = await client.query<{ seq: string; hash: string }>(
      `SELECT seq, hash FROM biz_events WHERE tenant_id = $1 ORDER BY seq DESC LIMIT 1`,
      [scope.tenantId],
    );
    const nextSeq = BigInt(tail.rows[0]?.seq ?? 8800) + 1n;
    const prevHash = tail.rows[0]?.hash ?? GENESIS_HASH;
    const eventId = formatEventId(nextSeq);

    // 第二段瀑布产物：payload 已脱敏（gateway 调用 maskDeep 后传入；此处双保险再脱一遍也无副作用——占位符不含 PII 模式）
    const event: BusinessEvent = {
      ...input.event,
      event_id: eventId,
      context: { ...input.event.context, tenant_id: scope.tenantId, workspace_id: scope.workspaceId },
    };
    const checked = safeParseBusinessEvent(event);
    if (!checked.success) {
      throw new Error(`事件未过附录 E 校验：${checked.error.issues[0]?.message ?? "unknown"}`);
    }
    const payload = checked.data;
    const hash = eventHash(prevHash, payload);

    const res = await client.query<{ seq: string }>(
      `INSERT INTO biz_events (event_id, tenant_id, workspace_id, session_id, payload, prev_hash, hash, created_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT (tenant_id, event_id) DO NOTHING
       RETURNING seq`,
      [
        eventId,
        scope.tenantId,
        scope.workspaceId,
        input.sessionId ?? null,
        JSON.stringify(payload),
        prevHash,
        hash,
        payload.context.time,
      ],
    );
    if (res.rowCount && res.rowCount > 0) {
      await client.query("COMMIT");
      return { eventId, seq: BigInt(res.rows[0]!.seq), hash, deduped: false, piiHits: 0 };
    }
    // 幂等丢弃（重复事件不报错——L1.4）
    // #26 修复：去重时从 DB 取回已存在事件的真实 hash/seq 返回（同 #4 对
    // appendEventIdempotent 的修法），避免调用方拿到按本 payload 新算的 hash 续链断裂
    const existing = await client.query<{ seq: string; hash: string }>(
      `SELECT seq, hash FROM biz_events WHERE tenant_id = $1 AND event_id = $2`,
      [scope.tenantId, eventId],
    );
    await client.query("COMMIT");
    return {
      eventId,
      seq: BigInt(existing.rows[0]?.seq ?? nextSeq),
      hash: existing.rows[0]?.hash ?? hash,
      deduped: true,
      piiHits: 0,
    };
  } catch (err) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw err;
  } finally {
    if (owned) (client as pg.PoolClient).release();
  }
}

/**
 * 显式幂等写入：调用方自带 event_id（回放/补偿场景，E3.3）
 * 与 appendEvent 的区别：不分配编号，直接以调用方 event_id 写入；冲突即丢弃。
 */
export async function appendEventIdempotent(
  gateway: pg.Pool,
  scope: { tenantId: string; workspaceId: string },
  event: BusinessEvent,
  sessionId?: string | null,
): Promise<AppendResult> {
  const masked = maskDeep(event);
  const ev = masked.value;
  const checked = safeParseBusinessEvent(ev);
  if (!checked.success) {
    throw new Error(`事件未过附录 E 校验：${checked.error.issues[0]?.message ?? "unknown"}`);
  }
  const client = await gateway.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.tenant_id', $1, true)", [scope.tenantId]);
    await client.query("SELECT set_config('app.workspace_id', $1, true)", [scope.workspaceId]);
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`event-chain:${scope.tenantId}`]);
    const tail = await client.query<{ seq: string; hash: string }>(
      `SELECT seq, hash FROM biz_events WHERE tenant_id = $1 ORDER BY seq DESC LIMIT 1`,
      [scope.tenantId],
    );
    const prevHash = tail.rows[0]?.hash ?? GENESIS_HASH;
    const payload = checked.data;
    const hash = eventHash(prevHash, payload);
    const res = await client.query<{ seq: string }>(
      `INSERT INTO biz_events (event_id, tenant_id, workspace_id, session_id, payload, prev_hash, hash, created_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT (tenant_id, event_id) DO NOTHING
       RETURNING seq`,
      [payload.event_id, scope.tenantId, scope.workspaceId, sessionId ?? null,
        JSON.stringify(payload), prevHash, hash, payload.context.time],
    );
    await client.query("COMMIT");
    const deduped = !(res.rowCount && res.rowCount > 0);
    if (deduped) {
      // #4 修复：去重时返回 DB 中已存在事件的真实 hash/seq，避免调用方拿到错误 hash 断链
      const existing = await client.query<{ seq: string; hash: string }>(
        `SELECT seq, hash FROM biz_events WHERE tenant_id = $1 AND event_id = $2`,
        [scope.tenantId, payload.event_id],
      );
      return {
        eventId: payload.event_id,
        seq: BigInt(existing.rows[0]?.seq ?? 0),
        hash: existing.rows[0]?.hash ?? hash,
        deduped,
        piiHits: masked.hits,
      };
    }
    return { eventId: payload.event_id, seq: BigInt(res.rows[0]?.seq ?? 0), hash, deduped, piiHits: masked.hits };
  } catch (err) {
    await client.query("ROLLBACK").catch(() => undefined);
    throw err;
  } finally {
    client.release();
  }
}
