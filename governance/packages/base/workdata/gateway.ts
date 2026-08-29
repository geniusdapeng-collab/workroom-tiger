/**
 * workdata · 安全网关（B1 核心）：一切事件写入的三段瀑布（F1.2/L1.2）
 *
 *   ① 权限校验   —— Agent 未声明 fence_bindings 且发起写类动作 → 拒（F2.10 复查位）；
 *                   只读 preset 发起写类动作 → 拒（L9.1 复查位）
 *   ② PII 脱敏   —— maskDeep（pii.ts；占位符协议 [PII:KIND:hash8]）
 *   ③ 高风险授权 —— 高危 Agent（meta.high_risk=true，如 desktop-agent）的写类动作
 *                   必须携带 approvalRef（逐次授权，L3.5）；缺失 → 拒绝并提示走审批
 *
 * 三段任一不过 → GatewayReject（不写库、留 reject 原因供调用方写「需介入」事件，L4.2）
 * 全过 → appendEvent（events.ts）落 append-only 事件库
 */
import type pg from "pg";
import { safeParseBusinessEvent, type BusinessEvent } from "@workloom/shared";
import { appendEvent, appendEventIdempotent, type AppendResult, type EventDraft } from "./events.js";
import { maskDeep } from "./pii.js";

/** 写类动作前缀（判定器完整版在 B4 围栏引擎；此处为网关复查位的最小集合） */
const WRITE_ACTION_PREFIXES = [
  "price.adjust",
  "order.refund",
  "review.reply",
  "content.draft",
  "content.publish",
  "refund.apply",
  "desktop.gui",
  "trigger.",
] as const;

/**
 * #5 修复：运行时可注册额外写类动作前缀（由围栏规则包装载时调用 registerWriteActions）。
 * 这样行业 Bundle 新增写类动作（如 inventory.adjust）后，网关段①权限校验能识别，
 * 不会因硬编码前缀未覆盖而放行未声明 fence_bindings 的 Agent 写动作（F2.10）。
 */
const extraWriteActions: Set<string> = new Set();
const extraWritePrefixes: string[] = [];

export function registerWriteActions(actions: string[]): void {
  for (const a of actions) {
    if (a.endsWith(".")) {
      extraWritePrefixes.push(a);
    } else {
      extraWriteActions.add(a);
    }
  }
}

export function isWriteAction(action: string): boolean {
  if (WRITE_ACTION_PREFIXES.some((p) => action === p || action.startsWith(p))) return true;
  if (extraWriteActions.has(action)) return true;
  if (extraWritePrefixes.some((p) => action.startsWith(p))) return true;
  return false;
}

export class GatewayReject extends Error {
  constructor(
    public readonly stage: "permission" | "pii" | "authorization",
    message: string,
  ) {
    super(message);
    this.name = "GatewayReject";
  }
}

export interface ActorInfo {
  /** who.id（agent preset_key 或 MEM-xxx 或 system 名） */
  id: string;
  type: "human" | "agent" | "system";
  /** Agent 专有：fence_bindings 声明（F2.10）；readonly preset（L9.1）；high_risk（L3.5） */
  fenceBindings?: string[];
  readonly?: boolean;
  highRisk?: boolean;
}

export interface GatewayContext {
  tenantId: string;
  workspaceId: string;
  actor: ActorInfo;
  /** 高风险授权的审批引用（事件 ID / approval_id），仅高危写动作需要 */
  approvalRef?: string;
  sessionId?: string | null;
}

export type { EventDraft } from "./events.js";

/** 段①：权限校验（纯函数，可单测） */
export function checkPermission(actor: ActorInfo, event: EventDraft): void {
  // #35 身份一致性：操作者声明（actor）与事件归因（who）必须同一主体——
  // 此前仅约定（全仓 26 处调用点人工保持一致），无机制防止分叉伪造留痕
  if (actor.id !== event.who.id || actor.type !== event.who.type) {
    throw new GatewayReject(
      "permission",
      `身份不一致：actor=${actor.type}:${actor.id} vs who=${event.who.type}:${event.who.id}，拒绝（防身份伪造留痕，G8 归因可信前提）`,
    );
  }
  const action = event.decision.action;
  if (actor.type !== "agent") return; // 人类/系统权限矩阵在 B5（tenancy+鉴权）落地
  if (!isWriteAction(action)) return; // 只读动作放行
  if (actor.readonly) {
    throw new GatewayReject(
      "permission",
      `只读 preset「${actor.id}」发起写类动作 ${action}，拒绝（L9.1）`,
    );
  }
  if (!actor.fenceBindings || actor.fenceBindings.length === 0) {
    throw new GatewayReject(
      "permission",
      `Agent「${actor.id}」未声明 fence_bindings，系统级禁写（F2.10），动作 ${action} 拒绝`,
    );
  }
}

/** 段③：高风险授权（纯函数） */
export function checkHighRiskAuthorization(actor: ActorInfo, event: EventDraft, approvalRef?: string): void {
  if (actor.type !== "agent" || !actor.highRisk) return;
  if (!isWriteAction(event.decision.action)) return;
  if (!approvalRef) {
    throw new GatewayReject(
      "authorization",
      `高危 Agent「${actor.id}」的写类动作 ${event.decision.action} 缺少逐次授权引用（L3.5），须先走审批`,
    );
  }
}

/**
 * 网关主入口：三段瀑布 → 落库
 * @returns AppendResult（含 piiHits 留痕）
 * @throws GatewayReject（任一瀑布段拒绝；调用方可选择把拒绝写成「需介入」系统事件）
 */
export async function gatewayAppend(
  gateway: pg.Pool,
  ctx: GatewayContext,
  draft: EventDraft,
): Promise<AppendResult> {
  // 段① 权限
  checkPermission(ctx.actor, draft);
  // 段② 脱敏
  const masked = maskDeep(draft);
  // 段③ 高风险授权
  checkHighRiskAuthorization(ctx.actor, masked.value, ctx.approvalRef);
  // 落库（append-only + 哈希链 + 幂等）
  const result = await appendEvent(
    gateway,
    { tenantId: ctx.tenantId, workspaceId: ctx.workspaceId },
    { event: masked.value, sessionId: ctx.sessionId },
  );
  return { ...result, piiHits: masked.hits };
}

/** 回放/补偿场景的显式幂等写入（E3.3）：同样过三段瀑布 */
export async function gatewayAppendIdempotent(
  gateway: pg.Pool,
  ctx: GatewayContext,
  event: BusinessEvent,
): Promise<AppendResult> {
  checkPermission(ctx.actor, event);
  checkHighRiskAuthorization(ctx.actor, event, ctx.approvalRef);
  const checked = safeParseBusinessEvent(event);
  if (!checked.success) throw new Error("事件未过附录 E 校验");
  return appendEventIdempotent(gateway, { tenantId: ctx.tenantId, workspaceId: ctx.workspaceId }, checked.data, ctx.sessionId);
}
