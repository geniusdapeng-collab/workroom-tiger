/** Public task-detail projection. Stored execution parameters never cross here. */
export interface DisplayPlanStep {
  stepId: string;
  label: string;
}

export type DisplayPlan =
  | { plan_state: "pending" | "invalid"; plan: null }
  | { plan_state: "empty" | "ready"; plan: DisplayPlanStep[] };

export function projectThreadPlan(value: unknown): DisplayPlan {
  if (value == null) return { plan_state: "pending", plan: null };
  if (!Array.isArray(value)) return { plan_state: "invalid", plan: null };
  if (value.length === 0) return { plan_state: "empty", plan: [] };
  const ids = new Set<string>();
  const plan: DisplayPlanStep[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)
      || typeof item.stepId !== "string" || !item.stepId.trim()
      || typeof item.label !== "string" || !item.label.trim() || ids.has(item.stepId)) {
      // A partial list would misrepresent the saved plan and its step count.
      return { plan_state: "invalid", plan: null };
    }
    ids.add(item.stepId);
    // Keep order and the complete human-readable label, including warnings.
    // No spreading: tool/action/params/context are private execution data.
    plan.push({ stepId: item.stepId, label: item.label });
  }
  return { plan_state: "ready", plan };
}
