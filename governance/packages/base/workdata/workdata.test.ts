/**
 * B1 测试：纯函数单测（PII/权限段/高风险段/哈希链规范化）+ PG 集成（幂等/链序/门禁）
 * 集成用例仅在 RUN_DB_TESTS=1 且 DATABASE_GATEWAY_URL 可达时运行（CI/沙箱实测口径），
 * 否则自动跳过——单元用例永远全量跑。
 */
import { describe, expect, it } from "vitest";
import { maskDeep, maskText } from "./pii.js";
import { checkHighRiskAuthorization, checkPermission, GatewayReject, isWriteAction } from "./gateway.js";
import { canonicalJson, eventHash, GENESIS_HASH } from "./events.js";

const draft = (action: string, whoId = "pricing-agent") => ({
  who: { type: "agent" as const, id: whoId, version: "v2.3" },
  context: { tenant_id: "tenant-demo", workspace_id: "ws-yunqi", time: "2026-08-16T22:10:00+08:00" },
  object: { type: "room_price", id: "RT-DLX-KING" },
  decision: { action },
  rule_impact: [],
});

describe("PII 脱敏（瀑布段②）", () => {
  it("手机号/身份证/邮箱命中占位符协议，同值同占位", () => {
    const a = maskText("客人电话 13812345678，邮箱 a.b@c.com");
    expect(a.hits).toBe(2);
    expect(a.text).not.toContain("13812345678");
    expect(a.text).toMatch(/\[PII:PHONE:[0-9a-f]{8}\]/);
    const b = maskText("回访 13812345678");
    // 同值同占位（可关联、无明文）
    expect(b.text).toContain(a.text.match(/\[PII:PHONE:[0-9a-f]{8}\]/)![0]);
  });

  it("身份证号优先于银行卡，不误伤价格数字", () => {
    const r = maskText("身份证 110101199003077758，担保卡 6222021001114329，退款金额 500");
    expect(r.text).toContain("[PII:IDCARD:");
    expect(r.text).toContain("退款金额 500"); // 纯业务数字不误伤
  });

  it("maskDeep 递归到嵌套叶子", () => {
    const r = maskDeep({ decision: { after: { note: "联系 13900001111" }, basis: ["OCC 0.78"] } });
    expect(r.hits).toBe(1);
    expect(JSON.stringify(r.value)).not.toContain("13900001111");
  });
});

describe("权限段①（F2.10/L9.1 复查位）", () => {
  it("未声明 fence_bindings 的 Agent 写动作被拒", () => {
    expect(() =>
      checkPermission({ id: "rogue", type: "agent", fenceBindings: [] }, draft("price.adjust", "rogue")),
    ).toThrow(GatewayReject);
  });

  it("只读 preset 写动作被拒（L9.1）", () => {
    expect(() =>
      checkPermission({ id: "inspection-agent", type: "agent", readonly: true, fenceBindings: [] }, draft("price.adjust", "inspection-agent")),
    ).toThrow(/L9\.1/);
  });

  it("声明了围栏的 Agent 写动作放行；只读动作不受限", () => {
    expect(() =>
      checkPermission({ id: "pricing-agent", type: "agent", fenceBindings: ["R1", "R2"] }, draft("price.adjust")),
    ).not.toThrow();
    expect(() => checkPermission({ id: "inspection-agent", type: "agent", readonly: true }, draft("inspection.scan", "inspection-agent"))).not.toThrow();
  });
});

describe("高风险授权段③（L3.5）", () => {
  it("高危 Agent 写动作缺授权引用被拒，带引用放行", () => {
    const desktop = { id: "desktop-agent", type: "agent" as const, highRisk: true, fenceBindings: ["R2"] };
    expect(() => checkHighRiskAuthorization(desktop, draft("desktop.gui", "desktop-agent"))).toThrow(/L3\.5/);
    expect(() => checkHighRiskAuthorization(desktop, draft("desktop.gui", "desktop-agent"), "apr-e-8888")).not.toThrow();
  });
});

describe("哈希链工具", () => {
  it("canonicalJson 键序稳定", () => {
    expect(canonicalJson({ b: 1, a: { d: 2, c: 3 } })).toBe('{"a":{"c":3,"d":2},"b":1}');
  });
  it("eventHash 确定性", () => {
    expect(eventHash(GENESIS_HASH, { x: 1 })).toBe(eventHash(GENESIS_HASH, { x: 1 }));
    expect(eventHash(GENESIS_HASH, { x: 1 })).not.toBe(eventHash(GENESIS_HASH, { x: 2 }));
  });
  it("写类动作判定", () => {
    expect(isWriteAction("price.adjust")).toBe(true);
    expect(isWriteAction("inspection.scan")).toBe(false);
  });
});

/* ================= PG 集成（RUN_DB_TESTS=1 时启用） ================= */

const RUN_DB = process.env.RUN_DB_TESTS === "1" && !!process.env.DATABASE_GATEWAY_URL;
const d = RUN_DB ? describe : describe.skip;

d("PG 集成（H-2/L1.4：幂等丢弃、哈希链序）", async () => {
  const pg = (await import("pg")).default;
  const { gatewayAppend, gatewayAppendIdempotent } = await import("./gateway.js");
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_GATEWAY_URL });
  const scope = { tenantId: "tenant-demo", workspaceId: "ws-yunqi" };
  const ctx = { ...scope, actor: { id: "pricing-agent", type: "agent" as const, fenceBindings: ["R1", "R2"] } };

  /** 读侧辅助：RLS 要求会话级 workspace 上下文（L7.1），否则返回空 */
  const readEvent = async (eventId: string) => {
    const c = await pool.connect();
    try {
      await c.query("SELECT set_config('app.workspace_id', $1, false)", [scope.workspaceId]);
      const r = await c.query("SELECT prev_hash, hash, payload FROM biz_events WHERE tenant_id=$1 AND event_id=$2", [scope.tenantId, eventId]);
      return r.rows[0];
    } finally {
      c.release();
    }
  };

  it("网关落库 → 事件编号续接 + 哈希链推进", async () => {
    const r = await gatewayAppend(pool, ctx, draft("price.adjust"));
    expect(r.eventId).toMatch(/^E-\d+$/);
    expect(r.deduped).toBe(false);
    const row = await readEvent(r.eventId);
    expect(row.hash).toBe(r.hash);
  });

  it("#32 链可重算：网关写入与种子同口径（canonicalJson 生产口径逐条复验）", async () => {
    const { canonicalJson, eventHash, GENESIS_HASH } = await import("./events.js");
    const { createHash } = await import("node:crypto");
    const sha256 = (s: string) => createHash("sha256").update(s, "utf-8").digest("hex");
    // 种子链首条必须从 GENESIS 起且可重算（#32 前：种子 stringify 口径重算全部不符）
    const c = await pool.connect();
    try {
      await c.query("SELECT set_config('app.workspace_id', $1, false)", [scope.workspaceId]);
      const rows = await c.query<{ event_id: string; payload: unknown; prev_hash: string; hash: string }>(
        `SELECT event_id, payload, prev_hash, hash FROM biz_events WHERE tenant_id=$1 AND workspace_id=$2 ORDER BY seq LIMIT 10`,
        [scope.tenantId, scope.workspaceId],
      );
      expect(rows.rows.length).toBeGreaterThan(0);
      let prev = GENESIS_HASH;
      for (const row of rows.rows) {
        expect(row.prev_hash).toBe(prev);
        expect(row.hash).toBe(sha256(prev + canonicalJson(row.payload)));
        expect(row.hash).toBe(eventHash(prev, row.payload)); // 与生产 eventHash 同结果
        prev = row.hash;
      }
    } finally {
      c.release();
    }
  });

  it("重复 event_id 写入幂等丢弃不报错（L1.4）", async () => {
    const ev = { ...draft("price.adjust"), event_id: "E-8801" } as const;
    // E-8801 为种子事件，重复写入必须 deduped=true 且不抛错
    const r = await gatewayAppendIdempotent(pool, ctx, ev as never);
    expect(r.deduped).toBe(true);
    expect(r.eventId).toBe("E-8801");
  });

  it("#26 appendEvent 幂等丢弃返回 DB 真实 hash/seq（不返回新算值）", async () => {
    // 独立租户隔离构造（不碰种子事件库——append-only 不可清理）：
    // 占位行用 CTE 取 nextval 同时决定 seq 与 event_id=E-(seq+1)——appendEvent 读链尾后
    // 分配的 event_id 恰为 E-(seq+1)，与占位行撞上 ON CONFLICT → deduped 分支；hash 用哨兵值
    const iso = { tenantId: `tenant-t26-${Date.now().toString(36)}`, workspaceId: "ws-t26" };
    const isoCtx = { ...iso, actor: ctx.actor };
    await gatewayAppend(pool, isoCtx, draft("price.adjust")); // iso 租户首条（起链）
    const sentinelHash = "sentinel-hash-26";
    const c = await pool.connect();
    let occupiedId = "";
    try {
      await c.query("BEGIN");
      await c.query("SELECT set_config('app.workspace_id', $1, true)", [iso.workspaceId]);
      await c.query("SELECT set_config('app.tenant_id', $1, true)", [iso.tenantId]);
      const ins = await c.query<{ event_id: string }>(
        `WITH s AS (SELECT nextval('biz_events_seq_seq') AS v)
         INSERT INTO biz_events (seq, event_id, tenant_id, workspace_id, payload, prev_hash, hash)
         SELECT s.v, 'E-' || (s.v + 1), $1, $2, $3, $4, $5 FROM s
         RETURNING event_id`,
        [iso.tenantId, iso.workspaceId, JSON.stringify({ marker: "occupy-26" }), "occupy-prev", sentinelHash],
      );
      occupiedId = ins.rows[0]!.event_id;
      await c.query("COMMIT");
    } catch (err) {
      await c.query("ROLLBACK").catch(() => undefined);
      throw err;
    } finally {
      c.release();
    }
    // appendEvent 分配同一 event_id → ON CONFLICT 丢弃 → 必须返回 DB 中真实 hash/seq
    const r = await gatewayAppend(pool, isoCtx, draft("price.adjust"));
    expect(r.eventId).toBe(occupiedId);
    expect(r.deduped).toBe(true);
    expect(r.hash).toBe(sentinelHash); // #26：此前会返回按本 payload 新算的 hash（断链风险）
  });

  it("#35 actor 与 who 身份分叉被拒（防伪造留痕），且不落库", async () => {
    const forged = draft("price.adjust");
    (forged.who as { id: string }).id = "MEM-999"; // who 伪造他人归因，actor 仍是 pricing-agent
    await expect(gatewayAppend(pool, ctx, forged)).rejects.toThrow(/身份不一致/);
    const c = await pool.connect();
    try {
      await c.query("SELECT set_config('app.workspace_id', $1, false)", [scope.workspaceId]);
      const n = await c.query<{ c: string }>(
        `SELECT count(*) AS c FROM biz_events WHERE workspace_id=$1 AND payload->'who'->>'id'='MEM-999'`,
        [scope.workspaceId],
      );
      expect(Number(n.rows[0]!.c)).toBe(0); // 伪造事件未落库
    } finally { c.release(); }
  });

  it("脱敏落库：事件库无明文手机号（F1.10 机制位）", async () => {
    const r = await gatewayAppend(pool, ctx, {
      ...draft("price.adjust"),
      decision: { action: "price.adjust", after: { note: "客人 13812345678 要求保留房价" } },
    });
    const row = await readEvent(r.eventId);
    expect(JSON.stringify(row.payload)).not.toContain("13812345678");
    expect(JSON.stringify(row.payload)).toContain("[PII:PHONE:");
    expect(r.piiHits).toBe(1);
  });
});
