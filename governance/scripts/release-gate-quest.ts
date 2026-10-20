/** Release-only evidence checks. Runtime planning, approval and execution semantics stay in runtime. */
import { isDeepStrictEqual } from "node:util";
import { BusinessEventSchema, type BusinessEvent } from "@workloom/shared";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

export interface CompletedQuest {
  threadId: string;
  stepsTotal: number;
}
export interface QuestEvidenceScope extends CompletedQuest {
  tenantId: string;
  workspaceId: string;
  memberNo: string;
}
export interface QuestEventEvidence {
  events: BusinessEvent[];
  dispatchEventId: string;
  executionEventIds: string[];
  receiptModes: string[];
}

/** runImmediately currently awaits runQuest; pending/failed results are not release completion. */
export function assertCompletedQuestDispatch(value: unknown): CompletedQuest {
  const result = object(value);
  assert(result.kind === "routed" && result.mode === "quest", `未按 quest 路由（${result.kind}/${result.mode}）`);
  assert(nonblank(result.threadId), "未建线程");
  assert(result.status === "completed", `QUEST 未完成（thread=${result.threadId}，status=${result.status}，progress=${result.stepsDone}/${result.stepsTotal}）`);
  assert(typeof result.stepsTotal === "number" && Number.isSafeInteger(result.stepsTotal) && result.stepsTotal >= 2,
    `未拆解合法多步骤（stepsTotal=${result.stepsTotal}）`);
  assert(typeof result.stepsDone === "number" && Number.isSafeInteger(result.stepsDone) && result.stepsDone === result.stepsTotal,
    `QUEST 执行进度未完成（${result.stepsDone}/${result.stepsTotal}）`);
  return { threadId: result.threadId, stepsTotal: result.stepsTotal };
}

export function assertCompletedQuestThread(quest: CompletedQuest, value: unknown): void {
  const thread = object(value);
  assert(thread.id === quest.threadId && thread.mode === "quest", "QUEST 线程身份/模式回读不一致");
  assert(thread.status === "completed", `QUEST 线程尚未完成（thread=${quest.threadId}，status=${thread.status}）`);
  assert(thread.progress_total === quest.stepsTotal && thread.progress_done === quest.stepsTotal,
    `QUEST 线程进度与派发结果不一致（${thread.progress_done}/${thread.progress_total}，expected=${quest.stepsTotal}）`);
}

/**
 * A newly dispatched gate Quest needs its own dispatch and every normalized s1..sN
 * execution receipt. Merely existing, planning, waiting for review or asking a question
 * does not prove execution. The API stream is capped at 200, while runQuest caps plans at 6.
 */
export function assertQuestEventEvidence(value: unknown, scope: QuestEvidenceScope): QuestEventEvidence {
  assert(Array.isArray(value) && value.length > 0, `QUEST ${scope.threadId} 无事件证据`);
  const seen = new Set<string>();
  const events = value.map((raw) => {
    const parsed = BusinessEventSchema.safeParse(raw);
    assert(parsed.success, `QUEST 事件结构/回执非法（${parsed.error?.issues[0]?.path.join(".") ?? "unknown"}）`);
    const event = parsed.data;
    assert(event.context.tenant_id === scope.tenantId && event.context.workspace_id === scope.workspaceId,
      `QUEST 事件 ${event.event_id} 租户/工作区不匹配`);
    assert(!seen.has(event.event_id), `QUEST 事件编号重复（${event.event_id}）`);
    seen.add(event.event_id);
    return event;
  });
  const dispatched = events.filter((event) => event.decision.action === "thread.dispatch");
  assert(dispatched.length === 1, `QUEST ${scope.threadId} 缺少唯一派发事件`);
  const dispatch = dispatched[0]!;
  const after = object(dispatch.decision.after);
  assert(dispatch.object.type === "thread" && dispatch.object.id === scope.threadId
    && after.threadId === scope.threadId && after.mode === "quest"
    && dispatch.who.type === "human" && dispatch.who.id === scope.memberNo,
  `QUEST ${scope.threadId} 派发事件身份不匹配`);

  const executions = events.filter((event) => event.decision.kind === "execute");
  assert(executions.length === scope.stepsTotal,
    `QUEST ${scope.threadId} 执行事件不完整（${executions.length}/${scope.stepsTotal}）`);
  const steps = new Set<string>();
  const modes = new Set<string>();
  for (const event of executions) {
    const decision = event.decision;
    const stepId = decision.step_id;
    assert(nonblank(stepId) && /^s[1-9]\d*$/.test(stepId)
      && Number(stepId.slice(1)) <= scope.stepsTotal && !steps.has(stepId),
    `QUEST 执行步骤缺失/重复/越界（event=${event.event_id}）`);
    assert(nonblank(decision.tool), `QUEST ${stepId} 未记录执行工具`);
    assert((decision.outcome === undefined || decision.outcome === "success") && !decision.error,
      `QUEST ${stepId} 执行失败（event=${event.event_id}）`);
    const receipt = event.receipt;
    assert(receipt?.synced === true && !receipt.error, `QUEST ${stepId} 回执未核实（event=${event.event_id}）`);
    assert((receipt.mode === "simulated" || receipt.mode === "real")
      && nonblank(receipt.snapshot_uri) && nonblank(receipt.verified_at),
    `QUEST ${stepId} 回执缺少来源模式/快照/核验时间（event=${event.event_id}）`);
    steps.add(stepId);
    modes.add(receipt.mode);
  }
  return {
    events,
    dispatchEventId: dispatch.event_id,
    executionEventIds: executions.map((event) => event.event_id),
    receiptModes: [...modes].sort(),
  };
}

/** Both readings must prove the same immutable events, not just the same number of rows. */
export function assertSameQuestEvidence(expected: QuestEventEvidence, actual: QuestEventEvidence): void {
  const ordered = (evidence: QuestEventEvidence) => [...evidence.events].sort((a, b) => a.event_id.localeCompare(b.event_id));
  assert(isDeepStrictEqual(ordered(expected), ordered(actual)), "QUEST API 与同线程账本的事件/回执不一致");
}
