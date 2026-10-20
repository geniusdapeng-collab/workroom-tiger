/**
 * C 端公开网关：受信入口签名 → 配置工作区 → 当前有效服务前台 → RLS 业务调用。
 * 行业读取仅经活动 Bundle 显式选择的业务适配器；通用知识/工单可独立启用。
 * 工单变更与审计同事务提交，回执只引用真实业务行/事件/通知状态。
 */
import { createHash, randomUUID } from "node:crypto";
import { Hono } from "hono";
import type { Context, Next } from "hono";
import { getAppPool, getOwnerPool, withWorkspace } from "@workloom/db";
import {
  CHANNELS, cSecret, exchangeCodeForOpenid, getCUser, issueCToken, listNotifications, pushMessage,
  resolveCUser, resolveDemoAuth, h5EntrySecret, verifyH5EntryToken, verifyCToken, notificationDeliveryState,
  type Channel, type CTokenPayload, type CUser,
} from "./channels.js";
import { handleMessage } from "./dialog.js";
import { resolveWorkspaceBusinessAdapter, type BusinessAdapterBinding } from "./adapters/business-registry.js";
import {
  BusinessAdapterError, runBusinessTool, type BusinessOrderResult, type BusinessMemberResult, type BusinessCatalogResult,
} from "./adapters/business.js";
import { selectServiceWorkspaceId, ServiceWorkspaceRoutingError } from "./workspace-routing.js";
import {
  ServiceHttpError, assignTicketOn, createTicketOn, getTicket, listTickets, rateTicketOn, ticketTimeline,
  type Ticket,
} from "./ticket.js";
import { appendEventOn, serviceTx, svcQuery } from "./events.js";

export const serviceGateway = new Hono();

/** 只作为启动诊断快照；请求时重新使用共享策略解析环境。 */
export const DEMO_AUTH = resolveDemoAuth();
if (DEMO_AUTH) console.warn("[service-c] 开发演示直登已开启；会话明确标注为 demo");
if (process.env.NODE_ENV === "production" && process.env.SERVICE_C_DEMO_AUTH === "true") {
  console.warn("[service-c] 生产环境忽略 SERVICE_C_DEMO_AUTH=true，正式 H5 仍须入口签名");
}

/** 身份建立前的 owner 池例外：只查询服务端配置选定的工作区，禁止数据库首项兜底。 */
async function cWorkspaceId(workspaceKey?: string): Promise<string> {
  const workspaceId = selectServiceWorkspaceId({
    fixedWorkspaceId: process.env.SERVICE_C_WORKSPACE_ID,
    workspaceMap: process.env.SERVICE_C_WORKSPACE_MAP,
    workspaceKey,
  });
  const found = await getOwnerPool().query("SELECT id FROM workspaces WHERE id=$1", [workspaceId]);
  if (!found.rows[0]) throw new ServiceWorkspaceRoutingError("配置的服务工作区不存在");
  return workspaceId;
}

async function serviceFrontBinding(workspaceId: string): Promise<BusinessAdapterBinding> {
  const binding = await resolveWorkspaceBusinessAdapter(workspaceId);
  if (binding.state === "ready" || binding.state === "adapter-not-declared") return binding;
  const businessUnavailable = binding.state === "adapter-unknown" || binding.state === "adapter-untrusted";
  throw new BusinessAdapterError(
    businessUnavailable ? "当前业务查询暂不可用，请联系管理员" : "当前服务前台暂不可用，请联系管理员",
    503, businessUnavailable ? "BUSINESS_ADAPTER_UNAVAILABLE" : "SERVICE_FRONT_UNAVAILABLE",
  );
}

function businessAdapterOf(c: Context) {
  const binding = c.get("cBinding") as BusinessAdapterBinding;
  if (!binding.adapter) throw new BusinessAdapterError("当前业务查询暂不可用，请联系管理员", 503, "BUSINESS_ADAPTER_UNAVAILABLE");
  return binding.adapter;
}

/* ---------------- 内存限流（60 次/分钟；Map 5 分钟 TTL 清扫防内存膨胀） ---------------- */
const buckets = new Map<string, { count: number; resetAt: number; touchedAt: number }>();
const BUCKET_TTL_MS = 5 * 60_000;
const sweeper = setInterval(() => {
  const now = Date.now();
  for (const [k, b] of buckets) {
    if (b.resetAt <= now || now - b.touchedAt > BUCKET_TTL_MS) buckets.delete(k);
  }
}, 60_000);
sweeper.unref?.();

function rateLimited(key: string, limit = 60): boolean {
  const now = Date.now();
  const b = buckets.get(key);
  if (!b || b.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + 60_000, touchedAt: now });
    return false;
  }
  b.count += 1;
  b.touchedAt = now;
  return b.count > limit;
}

/* ---------------- 鉴权中间件（Bearer c-token） ---------------- */
async function cAuth(c: Context, next: Next): Promise<Response | void> {
  const requestId = randomUUID();
  try {
    const auth = c.req.header("authorization");
    if (!auth?.startsWith("Bearer ")) return c.json({ error: "未认证（缺少 c-token）", requestId }, 401);
    const payload = await verifyCToken(auth.slice(7), cSecret());
    if (!payload) return c.json({ error: "c-token 无效或已过期", requestId }, 401);
    const workspaceExists = await withWorkspace(getAppPool(), { workspaceId: payload.workspaceId, tenantId: "" }, async (_db, client) => {
      const result = await client.query("SELECT id FROM workspaces WHERE id=$1", [payload.workspaceId]);
      return result.rows.length === 1;
    });
    if (!workspaceExists) return c.json({ error: "会话身份已失效", requestId }, 401);
    const user = await getCUser(payload.workspaceId, payload.cUserId);
    if (!user || user.channel !== payload.channel) return c.json({ error: "会话身份已失效", requestId }, 401);
    if (rateLimited(`c:${payload.cUserId}`)) return c.json({ error: "请求过于频繁（60 次/分钟）", requestId }, 429);
    const binding = await serviceFrontBinding(payload.workspaceId);
    c.set("cAuth", payload);
    c.set("cUser", user);
    c.set("cBinding", binding);
    await next();
  } catch (err) {
    return fail(c, err, requestId);
  }
}

function authOf(c: Context): CTokenPayload {
  return c.get("cAuth") as CTokenPayload;
}

function userOf(c: Context): CUser {
  return c.get("cUser") as CUser;
}

/** 解析 JSON body（非法/空 body → {}），属性经 Partial 访问、校验后使用 */
async function bodyOf<T>(c: Context): Promise<Partial<T>> {
  try {
    const body: unknown = await c.req.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body as Partial<T> : {};
  } catch {
    return {};
  }
}

function clientIp(c: Context): string {
  return c.req.header("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
}

/** 统一错误映射：ServiceHttpError → 语义状态码；其余 → 500 带 requestId（L9） */
function fail(c: Context, err: unknown, requestId: string): Response {
  if (err instanceof ServiceHttpError) return c.json({ error: err.message, requestId }, err.status as 400);
  if (err instanceof BusinessAdapterError) return c.json({ error: err.message, code: err.code, requestId }, err.status as 503);
  if (err instanceof ServiceWorkspaceRoutingError) return c.json({ error: "服务工作区暂不可用，请联系管理员", code: "SERVICE_WORKSPACE_UNAVAILABLE", requestId }, 503);
  console.warn(`[service-c] 请求处理失败 requestId=${requestId}：`, err instanceof Error ? err.message : err);
  return c.json({ error: "服务内部错误", requestId }, 500);
}

function sha256(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

/* ---------------- H6 契约序列化 ---------------- */

/** 工单状态 → webc 中文枚举（保留英文 status，提供 statusText） */
const TICKET_STATUS_TEXT: Record<string, string> = {
  created: "已受理",
  assigned: "已受理",
  processing: "处理中",
  done: "已完成",
  closed: "已关闭",
};

export function serializeTicket(t: Ticket): Ticket & { statusText: string } {
  return { ...t, statusText: TICKET_STATUS_TEXT[t.status] ?? t.status };
}

interface DeliveryReceipt { state: "demo" | "pending" | "failed" | "sent"; notificationId?: string }
interface ActionReceipt {
  requestId: string; state: "accepted" | "recorded"; resourceId: string;
  eventId?: string; idempotentReplay?: boolean; delivery?: DeliveryReceipt;
}

const TICKET_KINDS = ["delivery", "repair", "complaint", "other", "service_request", "consult"] as const;

function optionalText(value: unknown, name: string, maximum: number): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim() || value.length > maximum) throw new ServiceHttpError(`${name} 格式无效`, 400);
  return value.trim();
}

function ticketDraftOf(value: unknown): { kind: string; title: string; payload: Record<string, unknown> } {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ServiceHttpError("工单草稿格式无效", 400);
  const draft = value as Record<string, unknown>;
  if (typeof draft.kind !== "string" || !(TICKET_KINDS as readonly string[]).includes(draft.kind)) {
    throw new ServiceHttpError(`kind 须为 ${TICKET_KINDS.join("/")}`, 400);
  }
  if (typeof draft.title !== "string" || !draft.title.trim()) throw new ServiceHttpError("缺少 title", 400);
  const title = draft.title.trim();
  if (title.length > 120) throw new ServiceHttpError("title 超长（≤120 字符）", 400);
  const payload = draft.payload ?? {};
  if (typeof payload !== "object" || Array.isArray(payload)) throw new ServiceHttpError("payload 须为对象", 400);
  if (Buffer.byteLength(JSON.stringify(payload), "utf8") > 10 * 1024) throw new ServiceHttpError("payload 超大（≤10KB）", 400);
  return { kind: draft.kind, title, payload: payload as Record<string, unknown> };
}

async function existingDelivery(workspaceId: string, cUserId: string, ticketId: string): Promise<DeliveryReceipt> {
  const rows = await svcQuery<{ id: number; driver: string; status: string }>(workspaceId,
    `SELECT id,driver,status FROM c_notifications WHERE workspace_id=$1 AND c_user_id=$2
     AND kind='ticket.accepted' AND payload->>'ticketId'=$3 ORDER BY id DESC LIMIT 1`,
    [workspaceId, cUserId, ticketId]);
  const row = rows[0];
  return row ? { state: notificationDeliveryState(row.driver, row.status), notificationId: String(row.id) } : { state: "pending" };
}

/** 工单受理推送：失败 catch 落库 status='failed' 不阻断响应（H2） */
async function pushAcceptedSafely(input: {
  workspaceId: string; cUserId: string; ticketId: string; title: string; dept: string | null;
}): Promise<DeliveryReceipt> {
  const payload = {
    ticketId: input.ticketId, title: input.title,
    text: `您的工单「${input.title}」已受理，${input.dept ?? "值班负责人"}将尽快跟进。`,
  };
  try {
    const pushed = await pushMessage({ workspaceId: input.workspaceId, cUserId: input.cUserId, kind: "ticket.accepted", payload });
    return { state: pushed.state, notificationId: pushed.notificationId };
  } catch (err) {
    console.warn(`[service-c] 受理推送失败，落 failed 通知（不阻断建单响应）：`, err instanceof Error ? err.message : err);
    try {
      return await serviceTx(input.workspaceId, async (client) => {
        const result = await client.query<{ id: number }>(
          `INSERT INTO c_notifications (workspace_id, c_user_id, channel, kind, payload, driver, status)
           SELECT $1,$2,channel,'ticket.accepted',$3,'mock','failed' FROM c_users
           WHERE workspace_id=$1 AND id=$2 RETURNING id`,
          [input.workspaceId, input.cUserId, JSON.stringify(payload)],
        );
        return { state: "failed", ...(result.rows[0] ? { notificationId: String(result.rows[0].id) } : {}) };
      });
    } catch (err2) {
      console.warn(`[service-c] failed 通知落库也失败：`, err2 instanceof Error ? err2.message : err2);
      return { state: "failed" };
    }
  }
}

/** 建单链路（H2）：createTicket + assignTicket + 五元事件同一 serviceTx；幂等命中直接返回原单 */
async function createTicketFlow(input: {
  workspaceId: string; cUserId: string; channel: string; conversationId?: string;
  kind: string; title: string; payload: Record<string, unknown>; idempotencyKey: string;
}): Promise<{ ticket: Ticket; deduped: boolean; eventId?: string }> {
  return serviceTx(input.workspaceId, async (client, scope) => {
    if (input.conversationId) {
      const conversation = await client.query(
        "SELECT id FROM c_conversations WHERE workspace_id=$1 AND id=$2 AND c_user_id=$3",
        [input.workspaceId, input.conversationId, input.cUserId],
      );
      if (!conversation.rows[0]) throw new ServiceHttpError("会话不存在", 404);
    }
    const { ticket, deduped } = await createTicketOn(client, {
      workspaceId: input.workspaceId, cUserId: input.cUserId, conversationId: input.conversationId,
      kind: input.kind, title: input.title, payload: input.payload, idempotencyKey: input.idempotencyKey,
    });
    if (deduped) {
      if (ticket.cUserId !== input.cUserId) throw new ServiceHttpError("该请求标识已被使用，请重新提交", 409);
      const original = await client.query<{ event_id: string }>(
        `SELECT event_id FROM biz_events WHERE workspace_id=$1 AND payload->'object'->>'id'=$2
         AND payload->'decision'->>'action'='service.ticket.create' AND payload->'who'->>'id'=$3 ORDER BY seq LIMIT 1`,
        [input.workspaceId, ticket.id, input.cUserId],
      );
      return { ticket, deduped: true, ...(original.rows[0] ? { eventId: original.rows[0].event_id } : {}) };
    }
    const assigned = await assignTicketOn(client, { workspaceId: input.workspaceId, ticketId: ticket.id });
    const event = await appendEventOn(client, scope, { id: input.cUserId, type: "human" }, {
      objectType: "ticket", objectId: ticket.id, action: "service.ticket.create",
      after: { kind: input.kind, title: input.title, dept: assigned.dept, channel: input.channel },
      channel: input.channel,
    });
    return { ticket: assigned, deduped: false, eventId: event.eventId };
  });
}

/* ---------------- 会话 ---------------- */
serviceGateway.post("/session", async (c) => {
  const requestId = randomUUID();
  try {
    const body = await bodyOf<{ channel: string; openid: string; nickname: string; code: string; entryToken: string; workspaceKey: string }>(c);
    if (typeof body.channel !== "string" || !(CHANNELS as readonly string[]).includes(body.channel)) {
      return c.json({ error: `channel 须为 ${CHANNELS.join("/")}`, requestId }, 400);
    }
    const channel = body.channel as Channel;
    if (rateLimited(`session:${clientIp(c)}:${channel}`)) return c.json({ error: "请求过于频繁（60 次/分钟）", requestId }, 429);
    const demoAllowed = resolveDemoAuth();
    let workspaceKey = optionalText(body.workspaceKey, "workspaceKey", 160);
    const nickname = optionalText(body.nickname, "nickname", 120);
    let authMode: "demo" | "channel" = "channel";
    let openid: string;
    if (channel === "h5" && body.entryToken != null) {
      const secret = h5EntrySecret();
      if (!secret) return c.json({ error: "H5 入口认证暂未配置，请联系管理员", code: "H5_ENTRY_UNAVAILABLE", requestId }, 503);
      const entry = typeof body.entryToken === "string" ? await verifyH5EntryToken(body.entryToken, secret) : null;
      if (!entry) return c.json({ error: "H5 入口凭据无效或已过期", code: "H5_ENTRY_INVALID", requestId }, 401);
      const publicKey = process.env.SERVICE_C_PUBLIC_WORKSPACE_KEY?.trim();
      if ((workspaceKey && workspaceKey !== entry.workspaceKey)
        || (process.env.SERVICE_C_WORKSPACE_ID?.trim() && publicKey && publicKey !== entry.workspaceKey)) {
        return c.json({ error: "入口凭据与当前站点不一致", code: "H5_ENTRY_SCOPE_MISMATCH", requestId }, 403);
      }
      workspaceKey = entry.workspaceKey;
      openid = `entry:${sha256(JSON.stringify([entry.appId, entry.subject]))}`;
    } else if (channel === "h5" || (demoAllowed && body.openid !== undefined)) {
      if (!demoAllowed) return c.json({ error: "请使用正式 H5 入口凭据登录", code: "H5_ENTRY_REQUIRED", requestId }, 401);
      const demoId = optionalText(body.openid, "openid", 240);
      if (!demoId) return c.json({ error: "缺少 openid", requestId }, 400);
      // 保留历史开发身份；签名入口的保留命名空间永远不能由匿名输入占用。
      openid = demoId.startsWith("entry:") ? `demo:${sha256(demoId)}` : demoId;
      authMode = "demo";
    } else {
      const code = optionalText(body.code, "code", 2048);
      if (!code) return c.json({ error: `缺少 code（${channel} 需经 code 换取 openid）`, requestId }, 400);
      const exchanged = await exchangeCodeForOpenid(channel, code);
      if (!exchanged.ok) return c.json({ error: `渠道未配置：${channel}`, requestId }, 503);
      openid = exchanged.openid;
    }
    const workspaceId = await cWorkspaceId(workspaceKey);
    await serviceFrontBinding(workspaceId);
    const user = await resolveCUser({ workspaceId, channel, openid, nickname });
    const token = await issueCToken({ workspaceId, cUserId: user.id, channel: user.channel, secret: cSecret() });
    return c.json({ token, user: { ...user, authMode } });
  } catch (err) {
    return fail(c, err, requestId);
  }
});

/* ---------------- 对话 ---------------- */
serviceGateway.post("/chat", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const body = await bodyOf<{
      conversationId: string; text: string; confirmTicket: boolean; idempotencyKey: string;
      ticketDraft: { kind: string; title: string; payload: Record<string, unknown> };
    }>(c);
    if (typeof body.text !== "string" || !body.text.trim()) return c.json({ error: "缺少 text", requestId }, 400);
    if (body.text.length > 2000) return c.json({ error: "text 超长（≤2000 字符）", requestId }, 400);
    if (body.confirmTicket !== undefined && typeof body.confirmTicket !== "boolean") throw new ServiceHttpError("confirmTicket 须为布尔值", 400);
    const conversationId = optionalText(body.conversationId, "conversationId", 240);
    const suppliedKey = optionalText(body.idempotencyKey, "idempotencyKey", 240);
    const explicitDraft = body.ticketDraft === undefined ? undefined : ticketDraftOf(body.ticketDraft);

    const r = await handleMessage({
      workspaceId: a.workspaceId, cUserId: a.cUserId, channel: a.channel,
      text: body.text.trim(), conversationId,
    });

    const cards: Array<{ kind: "order" | "member" | "catalog"; data: unknown }> = [];
    let answer = r.answer;
    if (r.toolCall?.tool === "query_ticket") {
      const tickets = await listTickets({ workspaceId: a.workspaceId, cUserId: a.cUserId });
      const latest = tickets[0];
      answer = latest
        ? `您的工单「${latest.title}」当前状态：${TICKET_STATUS_TEXT[latest.status] ?? latest.status}，${latest.dept ?? "值班负责人"}跟进中。`
        : "暂无可查询的工单进度，请先提交服务工单。";
    } else if (r.toolCall) {
      const user = userOf(c);
      const data = await runBusinessTool(businessAdapterOf(c), r.toolCall.tool, {
        workspaceId: a.workspaceId, cUserId: a.cUserId, memberId: user.verified ? user.memberId : null,
      });
      if (data.bindRequired) answer = data.hint ?? "请先完成身份核验后再查询。";
      else if (r.toolCall.tool === "query_order") {
        for (const order of (data as BusinessOrderResult).orders) cards.push({ kind: "order", data: order });
      } else if (r.toolCall.tool === "query_member") {
        const member = (data as BusinessMemberResult).member;
        if (member) cards.push({ kind: "member", data: { ...member, demo: data.demo } });
      } else cards.push({ kind: "catalog", data: data as BusinessCatalogResult });
    }

    // 工单草稿确认：confirmTicket:true → 服务端幂等键 + 同事务建单/派单/五元事件（H2）
    let ticket: (Ticket & { statusText: string }) | null = null;
    let deduped = false;
    let receipt: ActionReceipt | undefined;
    const draft = body.confirmTicket ? (explicitDraft ?? (r.ticketDraft ? ticketDraftOf(r.ticketDraft) : undefined)) : undefined;
    if (draft) {
      const idempotencyKey = suppliedKey ?? `chat:${r.conversationId}:${sha256(body.text.trim()).slice(0, 16)}`;
      const flow = await createTicketFlow({
        workspaceId: a.workspaceId, cUserId: a.cUserId, channel: a.channel, conversationId: r.conversationId,
        kind: draft.kind, title: draft.title, payload: draft.payload, idempotencyKey,
      });
      deduped = flow.deduped;
      const delivery = flow.deduped
        ? await existingDelivery(a.workspaceId, a.cUserId, flow.ticket.id)
        : await pushAcceptedSafely({ workspaceId: a.workspaceId, cUserId: a.cUserId,
          ticketId: flow.ticket.id, title: flow.ticket.title, dept: flow.ticket.dept });
      ticket = serializeTicket(flow.ticket);
      receipt = { requestId, state: "accepted", resourceId: flow.ticket.id,
        ...(flow.eventId ? { eventId: flow.eventId } : {}), ...(deduped ? { idempotentReplay: true } : {}), delivery };
    }

    return c.json({
      conversationId: r.conversationId,
      intent: r.intent,
      answer,
      confidence: r.confidence,
      citations: r.citations,
      cards,
      ticket,
      ...(receipt ? { receipt } : {}),
      ...(deduped ? { deduped: true } : {}),
      ticketDraft: r.ticketDraft ?? null,
      latencyMs: r.latencyMs,
      ...(r.mock ? { mock: true } : {}),
    });
  } catch (err) {
    return fail(c, err, requestId);
  }
});

/* ---------------- 受控行业业务查询 ---------------- */
serviceGateway.get("/orders", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const user = userOf(c);
    const data = await runBusinessTool(businessAdapterOf(c), "query_order", {
      workspaceId: a.workspaceId, cUserId: a.cUserId, memberId: user.verified ? user.memberId : null,
    }) as BusinessOrderResult;
    return c.json(data);
  } catch (err) { return fail(c, err, requestId); }
});

serviceGateway.get("/member", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const user = userOf(c);
    const data = await runBusinessTool(businessAdapterOf(c), "query_member", {
      workspaceId: a.workspaceId, cUserId: a.cUserId, memberId: user.verified ? user.memberId : null,
    }) as BusinessMemberResult;
    return c.json({
      ...(data.member ?? { title: "暂无身份信息", benefits: [] }), demo: data.demo,
      ...(data.bindRequired ? { bindRequired: true } : {}), ...(data.hint ? { hint: data.hint } : {}),
    });
  } catch (err) { return fail(c, err, requestId); }
});

/* ---------------- 通用工单 ---------------- */
serviceGateway.post("/tickets", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const body = await bodyOf<{ kind: string; title: string; payload: Record<string, unknown>; conversationId: string; idempotencyKey: string }>(c);
    const draft = ticketDraftOf(body);
    const conversationId = optionalText(body.conversationId, "conversationId", 240);
    const idempotencyKey = optionalText(body.idempotencyKey, "idempotencyKey", 240)
      ?? `ticket:${a.cUserId}:${sha256(`${draft.kind}|${draft.title}|${JSON.stringify(draft.payload)}`).slice(0, 16)}`;
    const flow = await createTicketFlow({ ...draft, workspaceId: a.workspaceId, cUserId: a.cUserId,
      channel: a.channel, conversationId, idempotencyKey });
    const delivery = flow.deduped
      ? await existingDelivery(a.workspaceId, a.cUserId, flow.ticket.id)
      : await pushAcceptedSafely({ workspaceId: a.workspaceId, cUserId: a.cUserId,
        ticketId: flow.ticket.id, title: flow.ticket.title, dept: flow.ticket.dept });
    const receipt: ActionReceipt = { requestId, state: "accepted", resourceId: flow.ticket.id,
      ...(flow.eventId ? { eventId: flow.eventId } : {}), ...(flow.deduped ? { idempotentReplay: true } : {}), delivery };
    return c.json({ ticket: serializeTicket(flow.ticket), receipt, ...(flow.deduped ? { idempotentReplay: true } : {}) });
  } catch (err) { return fail(c, err, requestId); }
});

serviceGateway.get("/tickets", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const tickets = await listTickets({ workspaceId: a.workspaceId, cUserId: a.cUserId });
    return c.json({ tickets: tickets.map(serializeTicket) });
  } catch (err) {
    return fail(c, err, requestId);
  }
});

serviceGateway.get("/tickets/:id", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const ticket = await getTicket(a.workspaceId, String(c.req.param("id")));
    if (!ticket || ticket.cUserId !== a.cUserId) return c.json({ error: "工单不存在", requestId }, 404);
    const timeline = await ticketTimeline({ workspaceId: a.workspaceId, ticketId: ticket.id });
    // H6：detail 归一为字符串（webc TimelineItem.detail: string）
    const items = timeline.map((e) => ({
      action: e.action,
      actorType: e.actorType,
      actorId: e.actorId,
      detail: typeof e.detail === "string" ? e.detail : String((e.detail as Record<string, unknown>).note ?? JSON.stringify(e.detail)),
      createdAt: e.createdAt,
    }));
    return c.json({ ticket: serializeTicket(ticket), timeline: items });
  } catch (err) {
    return fail(c, err, requestId);
  }
});

serviceGateway.post("/tickets/:id/rate", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const body = await bodyOf<{ score: number; comment: string }>(c);
    if (typeof body.score !== "number" || !Number.isInteger(body.score) || body.score < 1 || body.score > 5) {
      return c.json({ error: "score 须为 1-5 的整数", requestId }, 400);
    }
    if (body.comment !== undefined && (typeof body.comment !== "string" || body.comment.length > 2000)) {
      return c.json({ error: "comment 格式无效或超长（≤2000 字符）", requestId }, 400);
    }
    const score = body.score;
    const result = await serviceTx(a.workspaceId, async (client, scope) => {
      const ticket = await rateTicketOn(client, {
        workspaceId: a.workspaceId, ticketId: String(c.req.param("id")), cUserId: a.cUserId,
        score, comment: body.comment,
      });
      const event = await appendEventOn(client, scope, { id: a.cUserId, type: "human" }, {
        objectType: "ticket", objectId: ticket.id, action: "service.ticket.rate",
        after: { score, comment: body.comment ?? null }, channel: a.channel,
      });
      return { ticket, eventId: event.eventId };
    });
    const receipt: ActionReceipt = { requestId, state: "recorded", resourceId: result.ticket.id, eventId: result.eventId };
    return c.json({ ticket: serializeTicket(result.ticket), receipt });
  } catch (err) {
    return fail(c, err, requestId);
  }
});

/* ---------------- 推送箱 ---------------- */
serviceGateway.get("/notifications", cAuth, async (c) => {
  const requestId = randomUUID();
  try {
    const a = authOf(c);
    const notifications = await listNotifications({ workspaceId: a.workspaceId, cUserId: a.cUserId });
    return c.json({ notifications });
  } catch (err) {
    return fail(c, err, requestId);
  }
});
