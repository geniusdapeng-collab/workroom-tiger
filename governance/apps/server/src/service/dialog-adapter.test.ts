/** 模拟行业适配器，不调用真实交易工具、资金或经营库。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ServiceFrontBusinessAdapter } from "./adapters/business.js";

const mocks = vi.hoisted(() => ({
  search: vi.fn(), query: vi.fn(), write: vi.fn(), ensure: vi.fn(),
}));
vi.mock("./store.js", () => ({ ensureServiceSchema: mocks.ensure }));
vi.mock("./kb.js", () => ({ searchKB: mocks.search }));
vi.mock("./llm.js", () => ({ llmCall: () => null }));
vi.mock("./events.js", () => ({
  svcQuery: mocks.query,
  serviceTx: async (_ws: string, fn: (client: unknown) => unknown) => fn({ query: mocks.write }),
}));
import { classify, handleMessage, LOW_REFUSAL, sharesDistinctiveEvidence, ticketKindOf } from "./dialog.js";

const adapter: ServiceFrontBusinessAdapter = {
  id: "mock-simulated-service", kbLexicon: { synonyms: [["询价", "模拟目录"]], weakTokens: ["订单"] },
  classify: (text) => text === "模拟目录" ? { tool: "query_catalog", answer: "正在查询模拟服务目录。", params: { simulated: true } } : null,
  ticketKind: (text) => text.includes("修复模拟数据") ? "repair" : null,
  queryOrder: async () => ({ demo: true, orders: [] }),
  queryMember: async () => ({ demo: true, member: null }),
  queryCatalog: async () => ({ demo: true, cardTitle: "模拟目录", items: [] }),
};
const input = { workspaceId: "ws-simulated", cUserId: "cu-simulated", channel: "h5" as const, text: "模拟目录" };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.ensure.mockResolvedValue(undefined);
  mocks.query.mockResolvedValue([]);
  mocks.write.mockResolvedValue({ rows: [] });
  mocks.search.mockResolvedValue([]);
});
describe("已验证适配器对话契约", () => {
  it("消费适配器的工具、中文文案与参数，不改用酒店工具文案", async () => {
    const result = await handleMessage({ ...input, businessAdapter: adapter });
    expect(result).toMatchObject({ intent: "biz_query", answer: "正在查询模拟服务目录。", toolCall: { tool: "query_catalog", params: { simulated: true } }, mock: true });
    expect(mocks.search).not.toHaveBeenCalled();
    expect(mocks.write).toHaveBeenCalledTimes(4);
  });
  it("没有适配器时不触发行业工具；无知识证据则诚实拒答", async () => {
    const result = await handleMessage({ ...input, text: "查一下我的订单" });
    expect(result).toMatchObject({ intent: "kb_qa", answer: LOW_REFUSAL, mock: true });
    expect(result.toolCall).toBeUndefined();
  });
  it("词表沿 handleMessage 传到真实检索接口", async () => {
    await handleMessage({ ...input, text: "询价规则怎么说明", businessAdapter: adapter });
    expect(mocks.search).toHaveBeenCalledWith({ workspaceId: input.workspaceId, query: "询价规则怎么说明", limit: 5, lexicon: adapter.kbLexicon });
  });
  it("履约类型只消费显式适配器；疑问句不误建单", () => {
    expect(classify("修复模拟数据", adapter)).toMatchObject({ intent: "service_request" });
    expect(ticketKindOf("修复模拟数据", adapter)).toBe("repair");
    expect(classify("如何修复模拟数据", adapter)).toEqual({ intent: "kb_qa" });
    expect(ticketKindOf("修复模拟数据")).toBe("other");
  });
  it("投诉优先于适配器的业务查询", () => {
    const broad = { ...adapter, classify: () => ({ tool: "query_order" as const, answer: "正在查询模拟订单。" }) };
    expect(classify("我要投诉", broad)).toEqual({ intent: "complaint" });
  });
  it("非法适配器展示文案被拒，不能进入回复", async () => {
    const invalid = { ...adapter, classify: () => ({ tool: "query_order" as const, answer: "SELECT * FROM orders" }) };
    await expect(handleMessage({ ...input, businessAdapter: invalid })).rejects.toThrow("安全校验");
  });
  it("检索失败向调用者传播，不伪造成功回复", async () => {
    mocks.search.mockRejectedValueOnce(new Error("模拟检索不可用"));
    await expect(handleMessage({ ...input, text: "使用说明" })).rejects.toThrow("模拟检索不可用");
  });
  it("仅共享显式弱词的知识块不合并", () => {
    const first = { heading: "订单", content: "第一段" };
    const second = { heading: "订单", content: "第二段" };
    expect(sharesDistinctiveEvidence(first, second)).toBe(true);
    expect(sharesDistinctiveEvidence(first, second, adapter.kbLexicon)).toBe(false);
  });
});
