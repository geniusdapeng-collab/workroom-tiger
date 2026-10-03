import { beforeEach, describe, expect, it, vi } from "vitest";
const query = vi.hoisted(() => vi.fn());
vi.mock("./store.js", () => ({ ensureServiceSchema: async () => undefined, indexChunks: vi.fn() }));
vi.mock("./events.js", () => ({ svcQuery: query, serviceTx: vi.fn() }));
import { searchKB } from "./kb.js";
beforeEach(() => {
  vi.clearAllMocks();
  query.mockResolvedValue([{ document_id: "doc-mock", heading: "settlement", content: "模拟 settlement 规则", title: "模拟规则" }]);
});
describe("显式 Bundle 词表检索", () => {
  it("同义词同时进入候选召回与打分", async () => {
    const hits = await searchKB({ workspaceId: "ws-mock", query: "simword", lexicon: { synonyms: [["simword", "settlement"]] } });
    expect(query.mock.calls[0]![2]).toEqual(["ws-mock", ["%simword%", "%settlement%"]]);
    expect(hits).toHaveLength(1);
    expect(hits[0]!.score).toBeGreaterThan(0.45);
  });
  it("未注入词表时不猜测行业同义词", async () => {
    expect(await searchKB({ workspaceId: "ws-mock", query: "simword" })).toEqual([]);
    expect(query.mock.calls[0]![2]).toEqual(["ws-mock", ["%simword%"]]);
  });
  it("空查询不访问检索库", async () => {
    expect(await searchKB({ workspaceId: "ws-mock", query: "" })).toEqual([]);
    expect(query).not.toHaveBeenCalled();
  });
  it("外部检索失败传播", async () => {
    query.mockRejectedValueOnce(new Error("模拟 DB 失败"));
    await expect(searchKB({ workspaceId: "ws-mock", query: "settlement" })).rejects.toThrow("模拟 DB 失败");
  });
});
