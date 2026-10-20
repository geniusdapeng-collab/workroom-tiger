import { describe, expect, it } from "vitest";
import { scopedMemoryId } from "./memory-id.js";

describe("scoped automatic memory identity", () => {
  const scope = { tenantId: "tenant-a", workspaceId: "workspace-a" };

  it("is deterministic and uses a bounded namespace separate from legacy IDs", () => {
    const first = scopedMemoryId(scope, "approval-reject", "same.reason");
    expect(first).toBe(scopedMemoryId({ ...scope }, "approval-reject", "same.reason"));
    expect(first).toMatch(/^mem-scoped-v1-[0-9a-f]{64}$/);
    expect(first.startsWith("mem-reject-")).toBe(false);
    expect(first.startsWith("mem-pat-edit-")).toBe(false);
  });

  it("includes the tenant even when workspace IDs are equal", () => {
    expect(scopedMemoryId(scope, "approval-reject", "same.reason"))
      .not.toBe(scopedMemoryId({ ...scope, tenantId: "tenant-b" }, "approval-reject", "same.reason"));
  });

  it("includes workspace, producer namespace, and exact reason independently", () => {
    const ids = [
      scopedMemoryId(scope, "approval-reject", "same.reason"),
      scopedMemoryId({ ...scope, workspaceId: "workspace-b" }, "approval-reject", "same.reason"),
      scopedMemoryId(scope, "approval-edit-pattern", "same.reason"),
      scopedMemoryId(scope, "approval-reject", "other.reason"),
      scopedMemoryId(scope, "approval-reject", " same.reason"),
    ];
    expect(new Set(ids).size).toBe(ids.length);
  });

  it.each([":", "-", "/", "|", "\n", "\u0000"])("preserves tuple boundaries when every component can contain %j", (separator) => {
    const tuples = [
      ["a", `b${separator}c`, "d", "e"],
      [`a${separator}b`, "c", "d", "e"],
      ["a", "b", `c${separator}d`, "e"],
      ["a", "b", "c", `d${separator}e`],
    ] as const;
    expect(new Set(tuples.map((tuple) => tuple.join(separator))).size).toBe(1);
    const ids = tuples.map(([tenantId, workspaceId, namespace, key]) => scopedMemoryId({ tenantId, workspaceId }, namespace, key));
    expect(new Set(ids).size).toBe(tuples.length);
  });

  it("keeps Unicode, JSON punctuation and long keys distinct without exposing them in the ID", () => {
    const keys = ["\"quoted\"", "a\\b", "[a,b]", "[\"a\",\"b\"]", "工作区", "é", "e\u0301", "x".repeat(100000)];
    const ids = keys.map((key) => scopedMemoryId(scope, "approval-reject", key));
    expect(new Set(ids).size).toBe(keys.length);
    expect(ids.every((value) => /^mem-scoped-v1-[0-9a-f]{64}$/.test(value))).toBe(true);
  });
});
