/** Project the latest summary's own finite numeric metrics without coercion. */
export function numericKpiSnapshot(after: unknown): Record<string, number> {
  const metrics: Record<string, number> = Object.create(null);
  if (after === null || typeof after !== "object" || Array.isArray(after)) return metrics;
  for (const [key, value] of Object.entries(after)) {
    if (typeof value === "number" && Number.isFinite(value)) metrics[key] = value;
  }
  return metrics;
}
