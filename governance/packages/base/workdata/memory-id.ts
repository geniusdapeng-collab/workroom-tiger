import { createHash } from "node:crypto";

/**
 * Deterministic IDs for automatically derived workspace memories.
 * org_memory has a global primary key, so every identity includes both scope keys.
 * JSON preserves component boundaries even when identifiers contain delimiters.
 * The versioned prefix is separate from legacy mem-reject-/mem-pat-edit- IDs:
 * existing memories, usage references, and historical events are never renamed.
 */
export function scopedMemoryId(
  scope: { tenantId: string; workspaceId: string },
  namespace: string,
  key: string,
): string {
  const identity = JSON.stringify([scope.tenantId, scope.workspaceId, namespace, key]);
  return `mem-scoped-v1-${createHash("sha256").update(identity, "utf-8").digest("hex")}`;
}
