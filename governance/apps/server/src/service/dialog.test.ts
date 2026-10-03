/** Tiger-owned dialog boundary: industry facts come from an injected verified adapter. */
import { describe, expect, it } from "vitest";
import {
  classify, tierOfScore, ticketKindOf, sharesDistinctiveEvidence,
  CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
} from "./dialog.js";
import type { ServiceFrontBusinessAdapter } from "./adapters/business.js";

const researchAdapter: ServiceFrontBusinessAdapter = {
  id: "isolated-research-test",
  classify(text) {
    return text.includes("我的研究交付")
      ? { tool: "query_order", answer: "正在查询您已绑定的研究交付。", params: { scope: "bound-user" } }
      : null;
  },
  ticketKind(text) {
    if (text.includes("校验数据故障")) return "repair";
    if (text.includes("交付研究报告")) return "delivery";
    return null;
  },
  async queryOrder() { return { demo: true, orders: [] }; },
  async queryMember() { return { demo: true, member: null }; },
  async queryCatalog() { return { demo: true, cardTitle: "研究目录", items: [] }; },
};

describe("跨行业意图与显式适配器边界", () => {
  it.each([
    ["我要投诉研究结果", "complaint"],
    ["我的工单进度怎么样了", "biz_query"],
    ["请处理数据问题", "service_request"],
    ["研究报告几点交付", "kb_qa"],
    ["系统什么情况下会开仓", "kb_qa"],
    ["查一下我的订单", "kb_qa"],
    ["豪华大床房多少钱一晚", "kb_qa"],
    ["空调坏了，帮我修一下", "kb_qa"],
    ["帮我送两瓶矿泉水", "kb_qa"],
    ["", "kb_qa"],
  ])("没有行业适配器时「%s」→ %s", (text, intent) => {
    const result = classify(text);
    expect(result.intent).toBe(intent);
    expect(result.tool).toBe(text.includes("工单进度") ? "query_ticket" : undefined);
  });

  it("显式行业业务查询保持工具、中文文案和用户绑定参数", () => {
    expect(classify("我的研究交付什么时候完成", researchAdapter)).toEqual({
      intent: "biz_query", tool: "query_order", answer: "正在查询您已绑定的研究交付。", params: { scope: "bound-user" },
    });
  });

  it("投诉优先于行业查询；疑问句不会误生成行业履约单", () => {
    expect(classify("投诉我的研究交付", researchAdapter).intent).toBe("complaint");
    expect(classify("交付研究报告需要多久", researchAdapter).intent).toBe("kb_qa");
    expect(classify("请校验数据故障", researchAdapter).intent).toBe("service_request");
    expect(classify("请交付研究报告", researchAdapter).intent).toBe("service_request");
  });

  it("工单类别只采用适配器声明，未声明的行业词不被猜测", () => {
    expect(ticketKindOf("请校验数据故障", researchAdapter)).toBe("repair");
    expect(ticketKindOf("请交付研究报告", researchAdapter)).toBe("delivery");
    expect(ticketKindOf("请安排人工协助", researchAdapter)).toBe("other");
    expect(ticketKindOf("空调坏了，帮我修一下")).toBe("other");
  });
});

describe("知识证据合并", () => {
  it("只有实际共享的非弱词才合并；行业弱词必须显式注入", () => {
    const first = { heading: "研究", content: "账户风险" };
    const second = { heading: "研究", content: "交易时间" };
    expect(sharesDistinctiveEvidence(first, second)).toBe(true);
    expect(sharesDistinctiveEvidence(first, second, { weakTokens: ["研究"] })).toBe(false);
    expect(sharesDistinctiveEvidence(first, { heading: "账户", content: "风险复核" })).toBe(true);
    expect(sharesDistinctiveEvidence(first, { heading: "数据", content: "来源状态" })).toBe(false);
  });
});

describe("置信度三档归一化", () => {
  it("实际阈值与上下边界", () => {
    expect(CONFIDENCE_HIGH).toBe(0.72);
    expect(CONFIDENCE_MEDIUM).toBe(0.45);
    expect(tierOfScore(0.72)).toBe("high");
    expect(tierOfScore(0.719)).toBe("medium");
    expect(tierOfScore(0.45)).toBe("medium");
    expect(tierOfScore(0.449)).toBe("low");
    expect(tierOfScore(undefined)).toBe("low");
    expect(tierOfScore(1.2)).toBe("high");
    expect(tierOfScore(-0.3)).toBe("low");
    expect(tierOfScore(Number.NaN)).toBe("low");
  });
});
