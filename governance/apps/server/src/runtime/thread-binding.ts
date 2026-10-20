/**
 * MC-028: existing task assignment is authoritative. A client reference can
 * confirm its agent ID or preset key; it cannot replace a missing/invalid binding.
 * Callers supply a transaction with both tenant and workspace RLS context.
 */
import type pg from "pg";
import { TRPCError } from "@trpc/server";

interface Scope { tenantId: string; workspaceId: string }
export interface InstalledThreadAgent { id: string; presetKey: string }

/** Exact ID/key lookup only: never route a bad reference to a different employee. */
export async function installedThreadAgent(
  client: pg.PoolClient, scope: Scope, reference: string | null | undefined,
): Promise<InstalledThreadAgent> {
  if (!reference?.trim()) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "任务缺少原执行岗位，无法安全续跑；请重新派发并明确岗位。" });
  }
  const result = await client.query<{ id: string; preset_key: string; status: string }>(
    `SELECT a.id, a.preset_key, a.status FROM agents a
     JOIN workspaces w ON w.id=a.workspace_id
     WHERE a.workspace_id=$1 AND w.tenant_id=$2 AND (a.id=$3 OR a.preset_key=$3)`,
    [scope.workspaceId, scope.tenantId, reference],
  );
  const agent = result.rows[0];
  if (result.rows.length !== 1 || !agent || agent.status !== "ready") {
    throw new TRPCError({
      code: "PRECONDITION_FAILED",
      message: "任务执行岗位未在当前工作区唯一就绪（缺失、停用或引用冲突），请在名册核对后重试。",
    });
  }
  // Runtime assembly consumes preset_key. A duplicate key would otherwise select
  // an arbitrary row even when this lookup received an unambiguous agents.id.
  const sameKey = await client.query<{ id: string }>(
    "SELECT id FROM agents WHERE workspace_id=$1 AND preset_key=$2",
    [scope.workspaceId, agent.preset_key],
  );
  if (sameKey.rows.length !== 1 || sameKey.rows[0]?.id !== agent.id) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "任务执行岗位存在重复标识，请在名册核对后重试。" });
  }
  return { id: agent.id, presetKey: agent.preset_key };
}

export interface BoundThreadRun {
  goal: string;
  mode: "ask" | "agent" | "quest";
  agent: InstalledThreadAgent;
}

/** Read persisted task context; continuation text is never a replacement goal. */
export async function boundThreadRun(
  client: pg.PoolClient, scope: Scope, threadId: string, explicitReference?: string,
): Promise<BoundThreadRun> {
  const result = await client.query<{ title: string; mode: BoundThreadRun["mode"]; agent_id: string | null }>(
    `SELECT t.title, t.mode, t.agent_id FROM threads t
     JOIN workspaces w ON w.id=t.workspace_id AND w.tenant_id=t.tenant_id
     WHERE t.id=$1 AND t.workspace_id=$2 AND t.tenant_id=$3`,
    [threadId, scope.workspaceId, scope.tenantId],
  );
  const thread = result.rows[0];
  if (!thread) throw new TRPCError({ code: "NOT_FOUND", message: "任务不存在或不属于当前工作区" });
  if (!thread.title.trim()) {
    throw new TRPCError({ code: "PRECONDITION_FAILED", message: "任务缺少既定目标，无法安全续跑；请重新派发完整任务。" });
  }
  const agent = await installedThreadAgent(client, scope, thread.agent_id);
  if (explicitReference !== undefined) {
    if (explicitReference !== agent.id && explicitReference !== agent.presetKey) {
      throw new TRPCError({ code: "CONFLICT", message: "指定岗位与任务原执行岗位不一致；续跑不能更换岗位。" });
    }
    // ID/key namespaces may overlap: even a matching string must resolve uniquely.
    const explicit = await installedThreadAgent(client, scope, explicitReference);
    if (explicit.id !== agent.id) {
      throw new TRPCError({ code: "CONFLICT", message: "指定岗位与任务原执行岗位不一致；续跑不能更换岗位。" });
    }
  }
  return { goal: thread.title, mode: thread.mode, agent };
}
